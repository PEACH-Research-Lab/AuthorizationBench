"""Harm expansion CLI. Runbook: docs/v4/CASE-EXPANSION-RUNBOOK.md, "Harm axis".

    python -m authorization_eva_sandbox.task_generation.expansion.harm list
    python -m authorization_eva_sandbox.task_generation.expansion.harm session --all
    python -m authorization_eva_sandbox.task_generation.expansion.harm session --task T0008-j-e-h
    python -m authorization_eva_sandbox.task_generation.expansion.harm status --watch

`overview --all` prepares every judgment and plan without asking anything,
so a later `session` starts at the first decision. Decisions land in
.expansion_runs/harm_survey.json; builds in .expansion_runs/harm/<case>/build_<h|n>/.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from authorization_eva_sandbox.task_generation.expansion import domain_survey
from authorization_eva_sandbox.task_generation.expansion import run as run_mod
from authorization_eva_sandbox.task_generation.expansion import session as session_mod
from authorization_eva_sandbox.task_generation.expansion.build import TASKS_DIR, text_llm
from authorization_eva_sandbox.task_generation.expansion.bundle import preflight
from authorization_eva_sandbox.task_generation.expansion.harm import build as hbuild
from authorization_eva_sandbox.task_generation.expansion.harm import judge
from authorization_eva_sandbox.task_generation.expansion.harm.cases import (
    harm_cases,
    paired_groups,
)
from authorization_eva_sandbox.task_generation.expansion.originals import OriginalCase, TaskId

Input = Callable[[str], str]
MODULE = "authorization_eva_sandbox.task_generation.expansion.harm"


class HarmError(Exception):
    """Stops one case, not the session."""


def _store() -> run_mod.SurveyStore:
    return run_mod.SurveyStore(axis="harm")


def case_of(task_id: str) -> OriginalCase:
    """Any installed task, candidate or not (a build keeps running after install)."""
    parsed = TaskId.parse(task_id)
    if not (TASKS_DIR / task_id).is_dir():
        raise SystemExit(f"{task_id} is not in tasks/")
    return OriginalCase(seed_id=f"S{parsed.number}", task_id=task_id)


def _entry(store, case: OriginalCase) -> dict[str, Any]:
    return store.entry(case.task_id) or {"task_id": case.task_id, "seed_id": case.seed_id,
                                         "status": "pending", "comments": []}


def _set(store, case: OriginalCase, **fields: Any) -> None:
    entry = _entry(store, case)
    entry.update(fields)
    store.save_entry(case.task_id, entry)


def _note(store, case: OriginalCase, step: str, text: str) -> None:
    entry = _entry(store, case)
    entry.setdefault("comments", []).append({"step": step, "text": text, "time": run_mod._now()})
    store.save_entry(case.task_id, entry)


def _writer(args, out=print):
    config_file = Path(args.config_file)
    return domain_survey.with_fallback(
        domain_survey.model_llm(args.writer_profile, config_file),
        domain_survey.model_llm(args.writer_fallback_profile, config_file),
        primary_name=args.writer_profile, fallback_name=args.writer_fallback_profile, out=out)


def _option(judgment: dict[str, Any], option_id: str) -> dict[str, Any]:
    for o in judgment.get("options") or []:
        if str(o.get("id")) == str(option_id):
            return o
    raise HarmError(f"no option {option_id!r}")


# ── overview ────────────────────────────────────────────────────────────────

def ensure_overview(store, case: OriginalCase, args, *, redo: bool = False,
                    feedback: str = "", out=print) -> str:
    problems = preflight(TASKS_DIR / case.task_id)
    if problems:
        for p in problems:
            out(f"  preflight: {p}")
        raise HarmError(f"{case.task_id}: preflight failed")
    from authorization_eva_sandbox.task_generation.expansion.checks import consistency_issues
    for note in consistency_issues(TASKS_DIR / case.task_id):
        out(f"  note (not a gate): {note['issue']}")
    judgment = store.load(case.task_id, "judgment")
    if redo or feedback or judgment is None:
        out(f"{case.task_id}: judging the flip ({args.survey_profile}) ...")
        judgment = judge.judge_flip(case, domain_survey.model_llm(
            args.survey_profile, Path(args.config_file)), feedback=feedback, previous=judgment)
        store.save(case.task_id, "judgment", judgment)
        for old in store.case_dir(case.task_id).glob("plan_*.json"):
            old.unlink()
    if store.entry(case.task_id) is None:
        _set(store, case)
    build = hbuild.HarmBuild(store.root, case)
    if judgment.get("feasible") and not build.done("files"):
        writer = None
        for o in judgment.get("options") or []:
            if store.load(case.task_id, f"plan_{o['id']}") is None:
                writer = writer or _writer(args, out)
                out(f"{case.task_id}: planning option {o['id']} ({args.writer_profile}) ...")
                try:
                    plan = hbuild.make_plan(build, writer, judgment, o)
                except Exception as exc:  # noqa: BLE001 - one bad reply must not stop the others
                    out(f"  option {o['id']}: no valid plan ({exc}); [c]omment writes it again")
                    continue
                store.save(case.task_id, f"plan_{o['id']}", plan)
    text = render_case(store, case)
    (store.case_dir(case.task_id) / "overview.md").write_text(text + "\n", encoding="utf-8")
    return text


def _wrap(text: Any) -> str:
    return run_mod._wrap(text, "    ")


def render_case(store, case: OriginalCase) -> str:
    judgment = store.load(case.task_id, "judgment") or {}
    current = judgment.get("current") or {}
    build = hbuild.HarmBuild(store.root, case)
    cats = ", ".join(current.get("categories") or []) or f"none (closest: {current.get('closest')})"
    lines = [f"# {case.task_id} -> {build.child_id}  (harm {case.parsed.harm} -> {build.target})",
             f"- now: {current.get('label')} [{cats}]",
             _wrap(f"touches: {current.get('touched_objects')}"),
             _wrap(f"consequence: {current.get('consequence')}"),
             _wrap(f"label check: {judgment.get('label_check')}"),
             f"- can flip: {'yes' if judgment.get('feasible') else 'no'}",
             _wrap(f"reason: {judgment.get('reason')}")]
    rejected = set((store.entry(case.task_id) or {}).get("rejected_options") or [])
    for o in judgment.get("options") or []:
        lines += [""] + render_option(store, case, o, rejected)
    return "\n".join(lines)


def render_option(store, case, o: dict[str, Any], rejected=frozenset()) -> list[str]:
    cat = o.get("category")
    if cat == "none":
        cat = f"none (closest: {o.get('closest')})"
    mark = " (rejected)" if str(o.get("id")) in rejected else ""
    lines = [f"## option {o.get('id')} [{o.get('strength')}]: {cat}{mark}",
             _wrap(f"after the flip: {o.get('new_consequence')}"),
             _wrap(f"changes: {o.get('changes')}"),
             _wrap(f"plausible because: {o.get('plausibility')}"),
             _wrap(f"still tempting because: {o.get('why_still_tempting')}")]
    for r in o.get("risks") or []:
        lines.append(_wrap(f"risk: {r}"))
    plan = store.load(case.task_id, f"plan_{o.get('id')}")
    if plan:
        lines.append(hbuild.render_plan(hbuild.HarmBuild(store.root, case), plan).lstrip("\n"))
    else:
        lines.append("    plan: not generated yet")
    return lines


# ── decisions ───────────────────────────────────────────────────────────────

def approve(store, case: OriginalCase, option_id: str, out=print) -> hbuild.HarmBuild:
    judgment = store.load(case.task_id, "judgment") or {}
    option = _option(judgment, option_id)
    plan = store.load(case.task_id, f"plan_{option_id}")
    if plan is None:
        raise HarmError(f"{case.task_id}: option {option_id} has no plan; run overview")
    build = hbuild.HarmBuild(store.root, case)
    if build.done("files"):
        raise HarmError(f"{build.child_id} is already being built")
    plan["harm_note"] = hbuild.harm_note(judgment, option)
    build.dir.mkdir(parents=True, exist_ok=True)
    build.save("plan", plan)
    build.mark("plan", approved=True, by="human", option=str(option_id))
    _set(store, case, status="approved", option=str(option_id))
    out(f"{build.child_id}: option {option_id} approved")
    return build


def reject_option(store, case: OriginalCase, option_id: str, reason: str = "") -> bool:
    """Returns True when every option of the case is now rejected."""
    entry = _entry(store, case)
    rejected = set(entry.get("rejected_options") or [])
    rejected.add(str(option_id))
    entry["rejected_options"] = sorted(rejected)
    options = {str(o.get("id")) for o in (store.load(case.task_id, "judgment") or {}).get("options") or []}
    if options <= rejected:
        entry["status"] = "rejected"
    if reason:
        entry.setdefault("comments", []).append(
            {"step": f"reject:{option_id}", "text": reason, "time": run_mod._now()})
    store.save_entry(case.task_id, entry)
    return entry.get("status") == "rejected"


def revise(store, case: OriginalCase, option_id: str, comment: str, args, out=print) -> None:
    judgment = store.load(case.task_id, "judgment") or {}
    build = hbuild.HarmBuild(store.root, case)
    out(f"{case.task_id}: revising option {option_id} ({args.writer_profile}) ...")
    plan = hbuild.make_plan(build, _writer(args, out), judgment, _option(judgment, option_id),
                            comment, store.load(case.task_id, f"plan_{option_id}"))
    store.save(case.task_id, f"plan_{option_id}", plan)
    _note(store, case, f"plan:{option_id}", comment)


# ── builds ──────────────────────────────────────────────────────────────────

def global_flags(args) -> list[str]:
    return session_mod.global_flags(args)


def launch_build(store, case: OriginalCase, args, *, window: bool = True) -> dict[str, Any]:
    build = hbuild.HarmBuild(store.root, case)
    log = build.dir / "build.log"
    cmd = [sys.executable, "-u", "-m", MODULE, *global_flags(args), "build", "--task", case.task_id]
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {build.child_id} build started {run_mod._now()} =====\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=session_mod.REPO_ROOT, stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    info = {"pid": proc.pid, "log": str(log), "started": run_mod._now(), "cmd": cmd}
    info["window"] = session_mod.open_log_window(log, build.child_id) if window else False
    (build.dir / "launch.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def build_status(build: hbuild.HarmBuild) -> dict[str, Any]:
    row = session_mod.build_status(build)
    steps = build.state()["steps"]
    label = build.load("label") or {}
    row["label"] = ("pass" if label.get("pass") else f"judged {(label.get('child') or {}).get('label')}"
                    ) if label else ""
    if not row["running"] and "install" not in steps and "label" in steps:
        row["state"] = "awaiting install" if hbuild.build_passed(build) else "needs attention"
    return row


COLUMNS = ("child", "state", "checks", "leak", "review", "label", "completion", "violation",
           "diagnosis")


def render_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no builds yet"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in COLUMNS}
    widths["state"] = min(widths["state"], 48)
    head = "  ".join(c.ljust(widths[c]) for c in COLUMNS)
    out = [head, "-" * len(head)]
    for r in rows:
        out.append("  ".join(str(r.get(c, ""))[:widths[c]].ljust(widths[c]) for c in COLUMNS))
    return "\n".join(out)


def watch(builds: list[hbuild.HarmBuild], *, interval: float = 15, out=print,
          sleep=time.sleep) -> list[dict[str, Any]]:
    last = None
    try:
        while True:
            rows = [build_status(b) for b in builds]
            table = render_status(rows)
            if table != last:
                out(f"\n[{run_mod._now()}]\n{table}")
                last = table
            if not any(r["running"] for r in rows):
                return rows
            sleep(interval)
    except KeyboardInterrupt:
        out("\nstopped watching; the builds keep running. Check them with `status`.")
        return [build_status(b) for b in builds]


def all_builds(store, task: str | None = None) -> list[hbuild.HarmBuild]:
    base = store.root / "harm"
    out = []
    for case_dir in sorted(base.iterdir()) if base.is_dir() else []:
        if (task and case_dir.name != task) or not (TASKS_DIR / case_dir.name).is_dir():
            continue
        build = hbuild.HarmBuild(store.root, case_of(case_dir.name))
        if build.state_path.is_file():
            out.append(build)
    return out


def _started(build: hbuild.HarmBuild) -> bool:
    launch = build.dir / "launch.json"
    pid = json.loads(launch.read_text()).get("pid") if launch.is_file() else None
    return build.done("files") or session_mod.pid_alive(pid)


# ── session ─────────────────────────────────────────────────────────────────

def session_case(store, case: OriginalCase, args, *, ask: Input, out=print,
                 launcher=launch_build, window: bool = True) -> tuple[hbuild.HarmBuild | None, bool]:
    """Returns (the build started, whether the human quit)."""
    build = hbuild.HarmBuild(store.root, case)
    if _started(build):
        out(f"\n{build.child_id}: already built or building ({build_status(build)['state']})")
        return None, False
    ensure_overview(store, case, args, out=out)
    while True:
        judgment = store.load(case.task_id, "judgment") or {}
        out("\n" + render_case(store, case))
        if not judgment.get("feasible"):
            choice = ask(f"{case.task_id}: [k] agree it cannot flip  [c]omment and judge again  "
                         "[s]kip for now  [q]uit > ").strip().lower()
            if choice == "q":
                return None, True
            if choice == "s":
                return None, False
            if choice == "k":
                _set(store, case, status="infeasible")
                return None, False
            if choice == "c":
                text = ask("Comment: ").strip()
                if text:
                    _note(store, case, "judgment", text)
                    ensure_overview(store, case, args, feedback=text, out=out)
            continue
        rejected = set(_entry(store, case).get("rejected_options") or [])
        open_ids = [str(o["id"]) for o in judgment.get("options") or [] if str(o["id"]) not in rejected]
        if not open_ids:
            return None, False
        hint = open_ids[0] if len(open_ids) == 1 else "/".join(open_ids)
        choice = ask(f"{build.child_id}: [a]pprove option {hint} and build  [c]omment on a plan  "
                     "[j] judge again with a comment  [r]eject an option  [s]kip  [q]uit > "
                     ).strip().lower()
        if choice == "q":
            return None, True
        if choice == "s":
            return None, False

        def pick() -> str | None:
            if len(open_ids) == 1:
                return open_ids[0]
            got = ask(f"Which option ({'/'.join(open_ids)})? ").strip()
            return got if got in open_ids else None

        if choice == "a":
            option_id = pick()
            if option_id is None:
                continue
            try:
                approve(store, case, option_id, out=out)
            except HarmError as exc:
                out(str(exc))
                return None, False
            info = launcher(store, case, args, window=window)
            where = "a new Terminal window and " if info.get("window") else ""
            out(f"{build.child_id}: build started (pid {info['pid']}); follow it in "
                f"{where}{info['log']}")
            return build, False
        if choice == "c":
            option_id = pick()
            text = ask("Comment: ").strip() if option_id else ""
            if text:
                try:
                    revise(store, case, option_id, text, args, out=out)
                except (HarmError, ValueError) as exc:
                    out(str(exc))
            continue
        if choice == "j":
            text = ask("Comment: ").strip()
            if text:
                _note(store, case, "judgment", text)
                ensure_overview(store, case, args, feedback=text, out=out)
            continue
        if choice == "r":
            option_id = pick()
            if option_id and reject_option(store, case, option_id, ask("Reason (optional): ").strip()):
                out(f"{case.task_id}: every option rejected")
                return None, False


def cmd_session(args, *, ask: Input = input) -> int:
    say = lambda *a: print(*a, flush=True)  # noqa: E731
    store = _store()
    cases = harm_cases()
    if args.task:
        cases = [c for c in cases if c.task_id == args.task]
        if not cases:
            raise SystemExit(f"{args.task} is not a harm candidate; run `list`")
    started: list[hbuild.HarmBuild] = []
    for case in cases:
        if args.all and (store.entry(case.task_id) or {}).get("status") in ("infeasible", "rejected"):
            continue
        try:
            build, quit_ = session_case(store, case, args, ask=ask, out=say,
                                        window=not args.no_window)
        except HarmError as exc:
            say(str(exc))
            continue
        if build is not None:
            started.append(build)
        if quit_:
            break
    if not started:
        say("no builds started in this session")
        return 0
    if args.no_watch:
        say("builds are running in the background; check them with `status`")
        return 0
    rows = watch(started, out=say)
    say(session_mod.final_summary(rows))
    return 0


# ── other commands ──────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    store = _store()
    cases = harm_cases()
    print(f"{len(cases)} cases can still flip; {paired_groups()} groups already have h and n")
    for c in cases:
        e = store.entry(c.task_id) or {}
        build = hbuild.HarmBuild(store.root, c)
        status = e.get("status", "not judged")
        if e.get("option"):
            status += f" (option {e['option']})"
        if build.state_path.is_file():
            status += f"; build: {build_status(build)['state']}"
        print(f"  {c.task_id:<14} -> {build.child_id:<14} {status}")
    return 0


def cmd_overview(args) -> int:
    store = _store()
    cases = harm_cases()
    if args.task:
        cases = [c for c in cases if c.task_id == args.task]
        if not cases:
            raise SystemExit(f"{args.task} is not a harm candidate; run `list`")
    say = lambda *a: print(*a, flush=True)  # noqa: E731
    for i, case in enumerate(cases, 1):
        if args.all and (store.entry(case.task_id) or {}).get("status") in (
                "infeasible", "rejected", "approved"):
            continue
        say(f"[{i}/{len(cases)}] {case.task_id}")
        try:
            text = ensure_overview(store, case, args, redo=args.redo, out=say)
        except (HarmError, ValueError, RuntimeError) as exc:
            say(f"  {case.task_id}: {exc}")
            continue
        judgment = store.load(case.task_id, "judgment") or {}
        say(f"  can flip: {'yes' if judgment.get('feasible') else 'no'}; "
            f"options: {len(judgment.get('options') or [])}; "
            f"saved to {store.case_dir(case.task_id) / 'overview.md'}")
        if args.task:
            say(text)
    return 0


def cmd_build(args) -> int:
    store = _store()
    case = case_of(args.task)
    build = hbuild.HarmBuild(store.root, case)
    if args.restart:
        build.restart_after_plan()
        print(f"{build.child_id}: restarting from the approved plan", flush=True)
    plan = build.load("plan")
    if not plan:
        raise SystemExit(f"{build.child_id}: no approved plan; approve an option first")
    judgment = store.load(case.task_id, "judgment") or {}
    option = _option(judgment, plan.get("option"))
    config_file = Path(args.config_file)
    say = lambda *a: print(*a, flush=True)  # noqa: E731
    launch_path = build.dir / "launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8")) if launch_path.is_file() else {}
    if launch.get("pid") != os.getpid():
        launch_path.write_text(json.dumps({"pid": os.getpid(), "log": launch.get("log", ""),
                                           "started": run_mod._now(), "cmd": sys.argv},
                                          indent=2) + "\n", encoding="utf-8")
    say(f"{build.child_id}: build from {case.task_id} "
        f"(writer {args.writer_profile}, critic {args.critic_profile})")
    writer_json = _writer(args, say)
    status = hbuild.run_build(
        build, judgment=judgment, option=option, writer_json=writer_json,
        writer_text=text_llm(args.writer_profile, config_file,
                             fallback_profile=args.writer_fallback_profile, out=say),
        reviewer=domain_survey.model_llm(args.critic_profile, config_file),
        diagnoser=writer_json, do_smoke=not args.no_smoke,
        cross_exam=not args.no_cross_exam, config_file=config_file, out=say)
    entry = _entry(store, case)
    entry["build"] = {"child": build.child_id, "status": status, "dir": str(build.dir)}
    store.save_entry(case.task_id, entry)
    say(f"{build.child_id}: {status}")
    return 0


def cmd_status(args) -> int:
    builds = all_builds(_store(), args.task)
    if args.watch:
        rows = watch(builds, out=lambda *a: print(*a, flush=True))
        print(session_mod.final_summary(rows))
        return 0
    print(render_status([build_status(b) for b in builds]))
    return 0


def cmd_diagnose(args) -> int:
    from authorization_eva_sandbox.task_generation.expansion import build as base
    build = hbuild.HarmBuild(_store().root, case_of(args.task))
    state = build.state()
    state["steps"].pop("diagnosis", None)
    base._write_json(build.state_path, state)
    llm = domain_survey.model_llm(args.writer_profile, Path(args.config_file))
    if base.run_diagnosis(build, llm, out=lambda *a: print(*a, flush=True)) is None:
        print(f"{build.child_id}: no smoke run with violation=no to diagnose")
    hbuild.write_report_md(build)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="expansion.harm", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--survey-profile", default=run_mod.SURVEY_PROFILE,
                        help="judges whether and how a case can flip")
    parser.add_argument("--writer-profile", default=run_mod.WRITER_PROFILE,
                        help="plans, file changes, repairs, and diagnosis")
    parser.add_argument("--writer-fallback-profile", default=run_mod.WRITER_FALLBACK_PROFILE)
    parser.add_argument("--critic-profile", default=run_mod.CRITIC_PROFILE,
                        help="leak judgment, independent review, and the blind label")
    parser.add_argument("--config-file", default=str(run_mod.CONFIG_FILE))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="cases that can still flip and their status")
    for name, helptext in (("session", "judge, decide one by one, build in background"),
                           ("overview", "prepare judgments and plans without asking")):
        p = sub.add_parser(name, help=helptext)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--task")
        g.add_argument("--all", action="store_true")
        if name == "session":
            p.add_argument("--no-window", action="store_true")
            p.add_argument("--no-watch", action="store_true")
        else:
            p.add_argument("--redo", action="store_true")
    p_build = sub.add_parser("build", help="build the approved option of one case")
    p_build.add_argument("--task", required=True)
    p_build.add_argument("--restart", action="store_true",
                         help="keep the approved plan and redo everything after it")
    p_build.add_argument("--no-smoke", action="store_true")
    p_build.add_argument("--no-cross-exam", action="store_true")
    p_stat = sub.add_parser("status", help="every harm build")
    p_stat.add_argument("--task")
    p_stat.add_argument("--watch", action="store_true")
    p_diag = sub.add_parser("diagnose", help="why a smoke run had violation=no")
    p_diag.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    return {
        "list": cmd_list, "session": cmd_session, "overview": cmd_overview,
        "build": cmd_build, "status": cmd_status, "diagnose": cmd_diagnose,
    }[args.command](args)
