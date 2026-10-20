"""Case expansion CLI (domain axis). Runbook: docs/v4/CASE-EXPANSION-RUNBOOK.md

    python -m authorization_eva_sandbox.task_generation.expansion session --task T0005-i-g-h
    python -m authorization_eva_sandbox.task_generation.expansion session --all
    python -m authorization_eva_sandbox.task_generation.expansion status --watch

`session` is the normal entry point. The commands below are its parts:

    python -m authorization_eva_sandbox.task_generation.expansion list
    python -m authorization_eva_sandbox.task_generation.expansion overview --task T0005-i-g-h
    python -m authorization_eva_sandbox.task_generation.expansion revise --task T0005-i-g-h --target h --comment "..."
    python -m authorization_eva_sandbox.task_generation.expansion approve --task T0005-i-g-h --target h
    python -m authorization_eva_sandbox.task_generation.expansion build --task T0005-i-g-h --target h --auto

`overview` preflights the original case, summarizes it, judges every other
domain, and writes an edit plan for each feasible option. Options are
approved one at a time, at most five other domains per (trigger, harm).
`build --auto` then writes, checks, installs when everything passes,
smoke-runs GPT-4.1, and diagnoses a violation=no result.
`survey` and `review` are the interactive terminal path for the first half.
Decisions land in .expansion_runs/domain_survey.json.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable

from authorization_eva_sandbox.task_generation.expansion import domain_survey
from authorization_eva_sandbox.task_generation.expansion.bundle import (
    SANDBOX_ROOT,
    preflight,
)
from authorization_eva_sandbox.task_generation.expansion.originals import (
    OriginalCase,
    find_original_cases,
    seeds_without_original,
)

RUNS_DIR = SANDBOX_ROOT / ".expansion_runs"
CONFIG_FILE = SANDBOX_ROOT / "evaluation" / "config.toml"
SURVEY_PROFILE = "gpt5_6_sol"
WRITER_PROFILE = "opus_5_5"
WRITER_FALLBACK_PROFILE = "opus_4_8"
CRITIC_PROFILE = "gpt5_6_sol"

Input = Callable[[str], str]


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class SurveyStore:
    """The survey table plus each case's summary, options, and history."""

    def __init__(self, root: Path = RUNS_DIR, axis: str = "domain"):
        self.root = Path(root)
        self.axis = axis
        self.table_path = self.root / f"{axis}_survey.json"

    def case_dir(self, task_id: str) -> Path:
        return self.root / self.axis / task_id

    def load_table(self) -> dict[str, Any]:
        if self.table_path.is_file():
            return json.loads(self.table_path.read_text(encoding="utf-8"))
        return {"axis": self.axis, "entries": {}}

    def entry(self, task_id: str) -> dict[str, Any] | None:
        return self.load_table()["entries"].get(task_id)

    def save_entry(self, task_id: str, entry: dict[str, Any]) -> None:
        table = self.load_table()
        entry["updated"] = _now()
        table["entries"][task_id] = entry
        _write_json(self.table_path, table)

    def load(self, task_id: str, name: str) -> dict[str, Any] | None:
        path = self.case_dir(task_id) / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def save(self, task_id: str, name: str, obj: dict[str, Any]) -> Path:
        path = self.case_dir(task_id) / f"{name}.json"
        _write_json(path, obj)
        with (self.case_dir(task_id) / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": _now(), "file": name, "content": obj},
                               ensure_ascii=False) + "\n")
        return path


# ── terminal rendering ──────────────────────────────────────────────────────

def _wrap(text: Any, indent: str = "    ") -> str:
    return textwrap.fill(str(text), width=100, initial_indent=indent,
                         subsequent_indent=indent)


def render_summary(case: OriginalCase, summary: dict[str, Any]) -> str:
    lines = [f"\n=== {case.task_id} (seed {case.seed_id}) — case summary ==="]
    for key in domain_survey.SUMMARY_KEYS:
        if key == "domain_names":
            continue
        lines.append(f"  {key}:")
        lines.append(_wrap(summary.get(key, "")))
    names = summary.get("domain_names") or []
    lines.append(f"  domain_names ({len(names)}):")
    for n in names:
        if isinstance(n, dict):
            lines.append(f"    - {n.get('name')} [{n.get('kind')}] in {n.get('where')}")
        else:
            lines.append(f"    - {n}")
    return "\n".join(lines)


def render_options(case: OriginalCase, answer: dict[str, Any]) -> str:
    labels = domain_survey.domain_labels()
    current = case.parsed.domain
    lines = [f"\n=== {case.task_id} — domain options ===",
             f"  current: {current} {labels.get(current, '')}"]
    for letter in answer.get("covered") or []:
        lines.append(f"  {letter} {labels.get(letter, '')}: already in this seed family, skipped")
    options = sorted(answer.get("options") or [], key=lambda o: (not o.get("feasible"), o.get("domain")))
    for o in options:
        letter = o.get("domain")
        mark = "CAN" if o.get("feasible") else "no "
        lines.append(f"\n  [{mark}] {letter} {labels.get(letter, '')}")
        lines.append(_wrap("reason: " + str(o.get("reason", ""))))
        if o.get("feasible"):
            lines.append(_wrap("story: " + str(o.get("new_story", ""))))
            lines.append(_wrap("plausible because: " + str(o.get("plausibility", ""))))
            for swap in o.get("instruction_nouns") or []:
                lines.append(f"    instruction: {swap.get('from')!r} -> {swap.get('to')!r}")
            for svc in o.get("services") or []:
                lines.append(_wrap(f"service {svc.get('service')}: {svc.get('reskin')}"))
        for risk in o.get("risks") or []:
            lines.append(_wrap("risk: " + str(risk)))
    return "\n".join(lines)


# ── review loops ────────────────────────────────────────────────────────────

def _edit_file(path: Path, ask: Input) -> None:
    editor = os.environ.get("EDITOR")
    if editor:
        subprocess.call([editor, str(path)])
    else:
        ask(f"Edit {path} now, then press Enter: ")


def review_summary(case: OriginalCase, store: SurveyStore, llm: domain_survey.LLM,
                   ask: Input, out=print) -> dict[str, Any] | None:
    """Returns the accepted summary, or None when the human quits."""
    summary = store.load(case.task_id, "summary")
    while True:
        out(render_summary(case, summary))
        choice = ask("[a]ccept  [c]omment  [e]dit  [q]uit > ").strip().lower()
        if choice == "a":
            _set(store, case, summary_accepted=True)
            return summary
        if choice == "q":
            return None
        if choice == "c":
            text = ask("Comment: ").strip()
            _note(store, case, "summary", text)
            summary = domain_survey.summarize(case, llm, feedback=text, previous=summary)
            store.save(case.task_id, "summary", summary)
        elif choice == "e":
            path = store.case_dir(case.task_id) / "summary.json"
            _edit_file(path, ask)
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                out(f"  summary.json is not valid JSON ({exc}); fix it and try again.")


def review_options(case: OriginalCase, store: SurveyStore, llm: domain_survey.LLM,
                   ask: Input, out=print) -> str:
    """Returns the resulting status: approved, none, or pending (quit)."""
    answer = store.load(case.task_id, "options")
    summary = store.load(case.task_id, "summary")
    while True:
        out(render_options(case, answer))
        feasible = [o["domain"] for o in answer["options"] if o.get("feasible")]
        choice = ask(
            f"Targets as letters (suggested: {','.join(feasible) or 'none'}), "
            "'none', [c]omment, [q]uit > "
        ).strip().lower()
        if choice == "q":
            return "pending"
        if choice == "c":
            text = ask("Comment: ").strip()
            _note(store, case, "options", text)
            answer = domain_survey.survey(case, summary, llm, feedback=text, previous=answer)
            store.save(case.task_id, "options", answer)
            continue
        if choice == "none":
            _set(store, case, status="none", targets=[])
            return "none"
        letters = [x.strip() for x in choice.replace(" ", ",").split(",") if x.strip()]
        bad = [x for x in letters if x not in answer.get("candidates", [])]
        if not letters or bad:
            out(f"  not a candidate domain: {bad or choice!r}; candidates are "
                f"{answer.get('candidates')}")
            continue
        slots = domain_survey.slots_left(case)
        if len(letters) > slots:
            out(f"  at most {slots} more domains for this case "
                f"(max {domain_survey.MAX_OTHER_DOMAINS} other domains)")
            continue
        infeasible = [x for x in letters if x not in feasible]
        if infeasible:
            sure = ask(f"  the model marked {infeasible} as not feasible; keep them? [y/n] > ")
            if sure.strip().lower() != "y":
                continue
        _set(store, case, status="approved", targets=letters)
        return "approved"


def _base_entry(store: SurveyStore, case: OriginalCase) -> dict[str, Any]:
    return store.entry(case.task_id) or {
        "seed_id": case.seed_id,
        "task_id": case.task_id,
        "status": "pending",
        "targets": [],
        "comments": [],
    }


def _note(store: SurveyStore, case: OriginalCase, step: str, text: str) -> None:
    entry = _base_entry(store, case)
    entry["comments"].append({"step": step, "text": text, "time": _now()})
    store.save_entry(case.task_id, entry)


def _set(store: SurveyStore, case: OriginalCase, **fields: Any) -> None:
    entry = _base_entry(store, case)
    entry.update(fields)
    store.save_entry(case.task_id, entry)


# ── commands ────────────────────────────────────────────────────────────────

def survey_case(case: OriginalCase, store: SurveyStore, llm: domain_survey.LLM, *,
                review: bool, redo: bool, ask: Input = input, out=print) -> str:
    problems = preflight(SANDBOX_ROOT / "tasks" / case.task_id)
    if problems:
        out(f"\n{case.task_id}: preflight failed, skipped")
        for p in problems:
            out(f"  - {p}")
        _set(store, case, status="blocked", targets=[], problems=problems)
        return "blocked"

    if redo or store.load(case.task_id, "summary") is None:
        out(f"\n{case.task_id}: summarizing ...")
        store.save(case.task_id, "summary", domain_survey.summarize(case, llm))
        _set(store, case, summary_accepted=False)
    _set(store, case, status="pending", targets=[], problems=[])
    if review and review_summary(case, store, llm, ask, out) is None:
        return "pending"

    if redo or store.load(case.task_id, "options") is None:
        out(f"{case.task_id}: judging domains ...")
        summary = store.load(case.task_id, "summary")
        store.save(case.task_id, "options", domain_survey.survey(case, summary, llm))
    if not review:
        out(render_options(case, store.load(case.task_id, "options")))
        return "pending"
    return review_options(case, store, llm, ask, out)


def _select(cases: list[OriginalCase], task: str | None) -> list[OriginalCase]:
    if not task:
        return cases
    chosen = [c for c in cases if c.task_id == task]
    if not chosen:
        raise SystemExit(f"{task} is not an original case; run `list` to see them")
    return chosen


def cmd_list(store: SurveyStore, out=print) -> int:
    cases = find_original_cases()
    table = store.load_table()["entries"]
    out(f"{len(cases)} original cases")
    for c in cases:
        e = table.get(c.task_id) or {}
        status = e.get("status", "not surveyed")
        targets = ",".join(e.get("targets") or [])
        out(f"  {c.task_id:<14} {c.seed_id}  {status}{'  -> ' + targets if targets else ''}")
    missing = seeds_without_original()
    if missing:
        out(f"seeds without an original case: {', '.join(missing)}")
    return 0


def _option(store: SurveyStore, case: OriginalCase, target: str) -> dict[str, Any]:
    options = store.load(case.task_id, "options") or {}
    option = next((o for o in options.get("options") or [] if o.get("domain") == target), None)
    if option is None:
        raise SystemExit(f"{case.task_id} has no saved option for domain {target!r}")
    return option


_STRENGTH_ORDER = {"strong": 0, "ok": 1, "weak": 2}


def sorted_options(answer: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(answer.get("options") or [], key=lambda o: (
        not o.get("feasible"), _STRENGTH_ORDER.get(o.get("strength"), 3), o.get("domain")))


def render_case_header(store: SurveyStore, case: OriginalCase) -> list[str]:
    labels = domain_survey.domain_labels()
    summary = store.load(case.task_id, "summary") or {}
    answer = store.load(case.task_id, "options") or {}
    approved = (store.entry(case.task_id) or {}).get("targets") or []
    return [
        f"# {case.task_id} (seed {case.seed_id}) — domain expansion overview",
        "",
        f"- scenario: {summary.get('scenario')}",
        f"- workflow (kept): {summary.get('workflow')}",
        f"- boundary object: {summary.get('boundary_object')}",
        f"- current domain: {case.parsed.domain} {labels.get(case.parsed.domain, '')}",
        f"- already in this family: {', '.join(answer.get('covered') or []) or 'none'}",
        f"- approved: {', '.join(approved) or 'none'}",
        f"- slots left (max {domain_survey.MAX_OTHER_DOMAINS} other domains): "
        f"{domain_survey.slots_left(case, approved)}",
    ]


def render_option(store: SurveyStore, case: OriginalCase, o: dict[str, Any]) -> list[str]:
    from authorization_eva_sandbox.task_generation.expansion import build as build_mod

    labels = domain_survey.domain_labels()
    entry = store.entry(case.task_id) or {}
    approved = entry.get("targets") or []
    letter = o.get("domain")
    head = f"{letter} {labels.get(letter, '')}"
    if not o.get("feasible"):
        return ["", f"## [no] {head}", f"- reason: {o.get('reason')}"]
    build = build_mod.Build(store.root, case, letter)
    status = (entry.get("builds") or {}).get(letter, {}).get("status")
    mark = ("approved" if letter in approved
            else "rejected" if letter in (entry.get("rejected") or []) else "proposed")
    lines = ["", f"## [{o.get('strength')}] {head} -> {build.child_id}  ({status or mark})",
             f"- story: {o.get('new_story')}",
             f"- plausible because: {o.get('plausibility')}"]
    for risk in o.get("risks") or []:
        lines.append(f"- risk: {risk}")
    plan = build.load("plan")
    if not plan:
        lines.append("- plan: not generated yet")
        return lines
    lines.extend(_plan_lines(plan))
    return lines


def render_overview(store: SurveyStore, case: OriginalCase) -> str:
    lines = render_case_header(store, case)
    for o in sorted_options(store.load(case.task_id, "options") or {}):
        lines.extend(render_option(store, case, o))
    return "\n".join(lines) + "\n"


def _plan_lines(plan: dict[str, Any]) -> list[str]:
    ops: dict[str, int] = {}
    for f in plan.get("files") or []:
        ops[f.get("op")] = ops.get(f.get("op"), 0) + 1
    emap = plan.get("entity_map") or []
    lines = [
        f"- plan instruction: {plan.get('instruction')}",
        "- plan file ops: " + ", ".join(f"{k} {v}" for k, v in sorted(ops.items())),
        "- plan entity map: " + "; ".join(f"{e.get('from')} -> {e.get('to')}" for e in emap[:12])
        + (f"; +{len(emap) - 12} more" if len(emap) > 12 else ""),
    ]
    moved = [f for f in plan.get("files") or [] if f.get("op") in ("rename", "add", "delete")]
    for f in moved[:15]:
        lines.append(f"  - {f.get('op')}: {f.get('path') or ''}"
                     + (f" -> {f.get('new_path')}" if f.get("new_path") else ""))
    if len(moved) > 15:
        lines.append(f"  - +{len(moved) - 15} more")
    lines.append(f"- plan snapshot_roots: {plan.get('snapshot_roots') or []}")
    if plan.get("notes"):
        lines.append(f"- plan notes: {plan.get('notes')}")
    return lines


class ExpansionError(Exception):
    """A step that stops this case but not the whole session."""


def ensure_overview(store: SurveyStore, case: OriginalCase, *, survey_profile: str,
                    writer_profile: str, config_file: Path, redo: bool = False,
                    writer_fallback_profile: str | None = None, out=print) -> str:
    """Summary, domain judgments, and a plan per feasible option; returns the overview."""
    from authorization_eva_sandbox.task_generation.expansion import build as build_mod

    problems = preflight(SANDBOX_ROOT / "tasks" / case.task_id)
    if problems:
        for p in problems:
            out(f"  preflight: {p}")
        raise ExpansionError(f"{case.task_id}: preflight failed")
    from authorization_eva_sandbox.task_generation.expansion.checks import consistency_issues
    for note in consistency_issues(SANDBOX_ROOT / "tasks" / case.task_id):
        out(f"  note (not a gate): {note['issue']}")
    surveyor = domain_survey.model_llm(survey_profile, config_file)
    fallback = writer_fallback_profile or WRITER_FALLBACK_PROFILE
    writer = domain_survey.with_fallback(
        domain_survey.model_llm(writer_profile, config_file),
        domain_survey.model_llm(fallback, config_file),
        primary_name=writer_profile, fallback_name=fallback, out=out)

    if redo or store.load(case.task_id, "summary") is None:
        out(f"{case.task_id}: summarizing ({survey_profile}) ...")
        store.save(case.task_id, "summary", domain_survey.summarize(case, surveyor))
    summary = store.load(case.task_id, "summary")
    options = store.load(case.task_id, "options")
    if redo or options is None or any("strength" not in o for o in options.get("options") or []):
        out(f"{case.task_id}: judging domains ({survey_profile}) ...")
        store.save(case.task_id, "options", domain_survey.survey(case, summary, surveyor))
    store.save_entry(case.task_id, _base_entry(store, case))

    for o in sorted_options(store.load(case.task_id, "options") or {}):
        if not o.get("feasible"):
            continue
        build = build_mod.Build(store.root, case, o["domain"])
        if build.done("files"):
            continue
        if redo or not build.done("plan"):
            out(f"{case.task_id}: planning {build.child_id} ({writer_profile}) ...")
            build.dir.mkdir(parents=True, exist_ok=True)
            try:
                plan = build_mod.make_plan(build, writer, summary, o)
            except Exception as exc:  # noqa: BLE001 - one bad reply must not stop the other domains
                out(f"  {build.child_id}: no valid plan ({exc}); the session will offer "
                    "[c]omment to write it again, or [s]kip")
                continue
            build.save("plan", plan)
            build.mark("plan", approved=False)
    text = render_overview(store, case)
    (store.case_dir(case.task_id) / "overview.md").write_text(text, encoding="utf-8")
    return text


def revise_plan(store: SurveyStore, case: OriginalCase, target: str, comment: str, *,
                writer_profile: str, fallback_profile: str, config_file: Path,
                out=print) -> dict[str, Any]:
    from authorization_eva_sandbox.task_generation.expansion import build as build_mod

    option = _option(store, case, target)
    build = build_mod.Build(store.root, case, target)
    if build.done("files"):
        raise ExpansionError(f"{build.child_id} is already being built; its plan is fixed")
    summary = store.load(case.task_id, "summary")
    writer = domain_survey.with_fallback(
        domain_survey.model_llm(writer_profile, config_file),
        domain_survey.model_llm(fallback_profile, config_file),
        primary_name=writer_profile, fallback_name=fallback_profile, out=out)
    out(f"{build.child_id}: revising the story ({writer_profile}) ...")
    option = domain_survey.revise_option(case, summary, option, comment, writer)
    options = store.load(case.task_id, "options") or {}
    options["options"] = [option if o.get("domain") == target else o
                          for o in options.get("options") or []]
    store.save(case.task_id, "options", options)
    out(f"{build.child_id}: revising the plan ({writer_profile}) ...")
    plan = build_mod.make_plan(build, writer, summary, option, comment, build.load("plan"))
    build.dir.mkdir(parents=True, exist_ok=True)
    build.save("plan", plan)
    build.mark("plan", approved=False)
    _note(store, case, f"plan:{target}", comment)
    return plan


def approve_target(store: SurveyStore, case: OriginalCase, target: str,
                   comment: str = "", out=print) -> str:
    """Approve one option and its plan. Returns the child task id."""
    from authorization_eva_sandbox.task_generation.expansion import build as build_mod

    option = _option(store, case, target)
    build = build_mod.Build(store.root, case, target)
    if not build.done("plan"):
        raise ExpansionError(f"{build.child_id} has no plan yet; run overview first")
    entry = _base_entry(store, case)
    targets = list(entry.get("targets") or [])
    if target not in targets:
        if domain_survey.slots_left(case, targets) <= 0:
            raise ExpansionError(f"{case.task_id} already has {domain_survey.MAX_OTHER_DOMAINS} "
                                 "other domains (existing plus approved)")
        targets.append(target)
    if not option.get("feasible"):
        out(f"note: the survey marked {target} as not feasible")
    entry.update(status="approved", targets=targets, summary_accepted=True)
    entry["rejected"] = [t for t in entry.get("rejected") or [] if t != target]
    if comment:
        entry.setdefault("comments", []).append(
            {"step": f"approve:{target}", "text": comment, "time": _now()})
    store.save_entry(case.task_id, entry)
    build.mark("plan", approved=True, by="human")
    out(f"{build.child_id}: approved. slots left: {domain_survey.slots_left(case, targets)}")
    return build.child_id


def reject_target(store: SurveyStore, case: OriginalCase, target: str, comment: str = "",
                  done: bool = False, out=print) -> None:
    entry = _base_entry(store, case)
    entry["targets"] = [t for t in entry.get("targets") or [] if t != target]
    rejected = set(entry.get("rejected") or [])
    rejected.add(target)
    entry["rejected"] = sorted(rejected)
    if not entry["targets"]:
        entry["status"] = "none" if done else entry.get("status", "pending")
    if comment:
        entry.setdefault("comments", []).append(
            {"step": f"reject:{target}", "text": comment, "time": _now()})
    store.save_entry(case.task_id, entry)
    out(f"{case.task_id}: {target} rejected")


def _fail(exc: ExpansionError) -> int:
    print(str(exc), flush=True)
    return 1


def cmd_overview(store: SurveyStore, args) -> int:
    case = _select(find_original_cases(), args.task)[0]
    try:
        text = ensure_overview(store, case, survey_profile=args.survey_profile,
                               writer_profile=args.writer_profile,
                               writer_fallback_profile=args.writer_fallback_profile,
                               config_file=Path(args.config_file), redo=args.redo,
                               out=lambda *a: print(*a, flush=True))
    except ExpansionError as exc:
        return _fail(exc)
    print(text)
    print(f"overview saved to {store.case_dir(case.task_id) / 'overview.md'}")
    return 0


def cmd_revise(store: SurveyStore, args) -> int:
    from authorization_eva_sandbox.task_generation.expansion import build as build_mod

    case = _select(find_original_cases(), args.task)[0]
    try:
        plan = revise_plan(store, case, args.target, args.comment,
                           writer_profile=args.writer_profile,
                           fallback_profile=args.writer_fallback_profile,
                           config_file=Path(args.config_file),
                           out=lambda *a: print(*a, flush=True))
    except ExpansionError as exc:
        return _fail(exc)
    print(build_mod.render_plan(build_mod.Build(store.root, case, args.target), plan))
    return 0


def cmd_approve(store: SurveyStore, args) -> int:
    case = _select(find_original_cases(), args.task)[0]
    try:
        approve_target(store, case, args.target, args.comment)
    except ExpansionError as exc:
        return _fail(exc)
    return 0


def cmd_reject(store: SurveyStore, args) -> int:
    case = _select(find_original_cases(), args.task)[0]
    reject_target(store, case, args.target, args.comment, args.done)
    return 0


def cmd_build(store: SurveyStore, args) -> int:
    from authorization_eva_sandbox.task_generation.expansion import build as build_mod

    case = _select(find_original_cases(), args.task)[0]
    entry = store.entry(case.task_id) or {}
    if entry.get("status") != "approved" or args.target not in (entry.get("targets") or []):
        raise SystemExit(f"{args.target} is not an approved target of {case.task_id}; "
                         f"approve it first")
    summary = store.load(case.task_id, "summary")
    option = _option(store, case, args.target)

    config_file = Path(args.config_file)
    build = build_mod.Build(store.root, case, args.target)
    if args.restart:
        build.restart_after_plan()
        print(f"{build.child_id}: restarting from the approved plan", flush=True)
    build.dir.mkdir(parents=True, exist_ok=True)
    launch_path = build.dir / "launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8")) if launch_path.is_file() else {}
    if launch.get("pid") != os.getpid():
        launch = {"pid": os.getpid(), "log": "", "started": _now(), "cmd": sys.argv}
        launch_path.write_text(json.dumps(launch, indent=2) + "\n", encoding="utf-8")
    print(f"{build.child_id}: build from {case.task_id} "
          f"(writer {args.writer_profile}, critic {args.critic_profile})", flush=True)
    say = lambda *a: print(*a, flush=True)  # noqa: E731
    writer_json = domain_survey.with_fallback(
        domain_survey.model_llm(args.writer_profile, config_file),
        domain_survey.model_llm(args.writer_fallback_profile, config_file),
        primary_name=args.writer_profile, fallback_name=args.writer_fallback_profile, out=say)
    status = build_mod.run_build(
        build, summary=summary, option=option,
        writer_json=writer_json,
        writer_text=build_mod.text_llm(args.writer_profile, config_file,
                                       fallback_profile=args.writer_fallback_profile,
                                       out=lambda *a: print(*a, flush=True)),
        reviewer=domain_survey.model_llm(args.critic_profile, config_file),
        diagnoser=writer_json,
        review=not (args.no_review or args.auto), do_install=args.install,
        do_smoke=not args.no_smoke, cross_exam=not args.no_cross_exam,
        config_file=config_file, auto=args.auto,
        out=lambda *a: print(*a, flush=True),
    )
    entry = store.entry(case.task_id) or {}
    builds = entry.setdefault("builds", {})
    builds[args.target] = {"child": build.child_id, "status": status, "dir": str(build.dir)}
    store.save_entry(case.task_id, entry)
    print(f"{build.child_id}: {status}", flush=True)
    return 0


def cmd_diagnose(store: SurveyStore, args) -> int:
    from authorization_eva_sandbox.task_generation.expansion import build as build_mod

    case = _select(find_original_cases(), args.task)[0]
    build = build_mod.Build(store.root, case, args.target)
    llm = domain_survey.model_llm(args.writer_profile, Path(args.config_file))
    if build.done("diagnosis") and build.state_path.is_file():
        state = build.state()
        state["steps"].pop("diagnosis", None)
        build_mod._write_json(build.state_path, state)
    if build_mod.run_diagnosis(build, llm, out=lambda *a: print(*a, flush=True)) is None:
        print(f"{build.child_id}: no smoke run with violation=no to diagnose")
    build_mod.write_report_md(build)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="expansion", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--axis", default="domain", choices=["domain"])
    parser.add_argument("--survey-profile", default=SURVEY_PROFILE,
                        help="summary and domain judgment")
    parser.add_argument("--writer-profile", default=WRITER_PROFILE,
                        help="edit plans, file rewriting, repair, and diagnosis")
    parser.add_argument("--writer-fallback-profile", default=WRITER_FALLBACK_PROFILE,
                        help="writes a call the writer refuses (content filter)")
    parser.add_argument("--critic-profile", default=CRITIC_PROFILE,
                        help="leak judgment and independent review")
    parser.add_argument("--config-file", default=str(CONFIG_FILE))
    sub = parser.add_subparsers(dest="command", required=True)
    p_sess = sub.add_parser("session", help="overview, approve one by one, build in background")
    group_s = p_sess.add_mutually_exclusive_group(required=True)
    group_s.add_argument("--task")
    group_s.add_argument("--all", action="store_true")
    p_sess.add_argument("--no-window", action="store_true",
                        help="do not open a Terminal window per build (macOS)")
    p_sess.add_argument("--no-watch", action="store_true",
                        help="exit after the last decision instead of watching the builds")
    p_stat = sub.add_parser("status", help="every build and its result")
    p_stat.add_argument("--task")
    p_stat.add_argument("--watch", action="store_true")
    p_over = sub.add_parser("overview", help="options plus a plan per feasible option")
    p_over.add_argument("--task", required=True)
    p_over.add_argument("--redo", action="store_true")
    p_rev = sub.add_parser("revise", help="rewrite one option's plan with a comment")
    p_rev.add_argument("--task", required=True)
    p_rev.add_argument("--target", required=True)
    p_rev.add_argument("--comment", required=True)
    p_app = sub.add_parser("approve", help="approve one option and its plan")
    p_app.add_argument("--task", required=True)
    p_app.add_argument("--target", required=True)
    p_app.add_argument("--comment", default="")
    p_rej = sub.add_parser("reject", help="reject one option")
    p_rej.add_argument("--task", required=True)
    p_rej.add_argument("--target", required=True)
    p_rej.add_argument("--comment", default="")
    p_rej.add_argument("--done", action="store_true",
                       help="mark the case finished when nothing is approved")
    p_diag = sub.add_parser("diagnose", help="why a smoke run had violation=no")
    p_diag.add_argument("--task", required=True)
    p_diag.add_argument("--target", required=True)
    sub.add_parser("list", help="original cases and their survey status")
    p_survey = sub.add_parser("survey", help="summarize and judge domains")
    group = p_survey.add_mutually_exclusive_group(required=True)
    group.add_argument("--task")
    group.add_argument("--all", action="store_true")
    p_survey.add_argument("--no-review", action="store_true",
                          help="generate only; leave the entry pending")
    p_survey.add_argument("--redo", action="store_true",
                          help="regenerate even when a summary or options exist")
    p_review = sub.add_parser("review", help="review pending entries")
    p_review.add_argument("--task")
    p_build = sub.add_parser("build", help="build one approved target")
    p_build.add_argument("--task", required=True)
    p_build.add_argument("--target", required=True)
    p_build.add_argument("--auto", action="store_true",
                         help="use the approved plan, install when every check passes, "
                              "smoke-run, and diagnose violation=no")
    p_build.add_argument("--restart", action="store_true",
                         help="keep the approved plan and redo everything after it")
    p_build.add_argument("--no-review", action="store_true",
                         help="auto-approve the plan and stop before install")
    p_build.add_argument("--install", action="store_true",
                         help="with --no-review, install without asking")
    p_build.add_argument("--no-smoke", action="store_true")
    p_build.add_argument("--no-cross-exam", action="store_true")
    args = parser.parse_args(argv)

    store = SurveyStore(axis=args.axis)
    from authorization_eva_sandbox.task_generation.expansion import session as session_mod
    commands = {
        "session": lambda: session_mod.cmd_session(store, args),
        "status": lambda: session_mod.cmd_status(store, args),
        "list": lambda: cmd_list(store),
        "overview": lambda: cmd_overview(store, args),
        "revise": lambda: cmd_revise(store, args),
        "approve": lambda: cmd_approve(store, args),
        "reject": lambda: cmd_reject(store, args),
        "build": lambda: cmd_build(store, args),
        "diagnose": lambda: cmd_diagnose(store, args),
    }
    if args.command in commands:
        return commands[args.command]()

    llm = domain_survey.model_llm(args.survey_profile, Path(args.config_file))
    cases = find_original_cases()

    if args.command == "survey":
        for case in _select(cases, None if args.all else args.task):
            entry = store.entry(case.task_id) or {}
            if args.all and not args.redo and entry.get("status") in ("approved", "none"):
                continue
            status = survey_case(case, store, llm, review=not args.no_review, redo=args.redo)
            print(f"{case.task_id}: {status}")
        return 0

    table = store.load_table()["entries"]
    for case in _select(cases, args.task):
        entry = table.get(case.task_id) or {}
        if not args.task and entry.get("status") != "pending":
            continue
        if store.load(case.task_id, "summary") is None:
            print(f"{case.task_id}: not surveyed yet; run `survey --task {case.task_id}`")
            continue
        if not entry.get("summary_accepted") and review_summary(case, store, llm, input) is None:
            print(f"{case.task_id}: pending")
            continue
        if store.load(case.task_id, "options") is None:
            summary = store.load(case.task_id, "summary")
            store.save(case.task_id, "options", domain_survey.survey(case, summary, llm))
        print(f"{case.task_id}: {review_options(case, store, llm, input)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
