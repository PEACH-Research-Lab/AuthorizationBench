"""Expansion progress per seed family, with the command for each next step.

Served by the dashboard at http://localhost:8765/expansion (data at
/expansion.json). Every request rescans tasks/ and .expansion_runs/.

Rows are (seed number, trigger, domain) groups, so an h/n pair is one row.
A group is the original when it holds an original case, a domain variant
when it keeps an original's trigger, and a trigger variant otherwise.
Groups made by the expansion pipeline carry task_config.lineage; the rest
come from the June generation.
"""
from __future__ import annotations

import contextlib
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.task_generation.expansion import domain_survey
from authorization_eva_sandbox.task_generation.expansion.bundle import (
    SANDBOX_ROOT,
    load_json,
    preflight,
)
from authorization_eva_sandbox.task_generation.expansion.checks import consistency_issues
from authorization_eva_sandbox.task_generation.expansion.harm.cases import FLIP
from authorization_eva_sandbox.task_generation.expansion.originals import (
    SEED_FILE,
    TASKS_DIR,
    TaskId,
    find_original_cases,
    seeds_without_original,
    task_ids,
)

RUNS_DIR = SANDBOX_ROOT / ".expansion_runs"
REPO_ROOT = SANDBOX_ROOT.parent
PY = ".venv-openhands/bin/python -m authorization_eva_sandbox.task_generation.expansion"


def command(args: str, harm: bool = False) -> str:
    return f"cd {REPO_ROOT} && {PY}{'.harm' if harm else ''} {args}"


def _lineage(task_id: str) -> dict[str, Any]:
    path = TASKS_DIR / task_id / "task_config.json"
    return (load_json(path).get("lineage") or {}) if path.is_file() else {}


def _source(task_id: str, originals: set[str]) -> str:
    if task_id in originals:
        return "seed"
    return "pipeline" if _lineage(task_id).get("axis") in ("domain", "harm") else "June"


def _entries(name: str) -> dict[str, Any]:
    path = RUNS_DIR / name
    return json.loads(path.read_text(encoding="utf-8")).get("entries", {}) if path.is_file() else {}


def _build_rows() -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Domain builds by original case, harm builds by the case they flip."""
    domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    harm: dict[str, dict[str, Any]] = {}
    try:
        from authorization_eva_sandbox.task_generation.expansion import run as run_mod
        from authorization_eva_sandbox.task_generation.expansion import session as session_mod
        for b in session_mod.all_builds(run_mod.SurveyStore()):
            r = session_mod.build_status(b)
            domain[r["parent"]].append({"target": b.target, "child": r["child"],
                                        "state": r["state"], "violation": r.get("violation", "")})
    except Exception as exc:  # noqa: BLE001 - a broken run dir must not take the page down
        domain["?"].append({"target": "?", "child": "?", "state": f"error: {exc}"})
    try:
        from authorization_eva_sandbox.task_generation.expansion.harm import cli as harm_cli
        for b in harm_cli.all_builds(harm_cli._store()):
            r = harm_cli.build_status(b)
            harm[r["parent"]] = {"child": r["child"], "state": r["state"],
                                 "violation": r.get("violation", "")}
    except Exception as exc:  # noqa: BLE001
        harm["?"] = {"child": "?", "state": f"error: {exc}"}
    return domain, harm


def _harm_step(task_id: str, twin: str | None, originals: set[str],
               survey: dict[str, Any], builds: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if twin:
        return {"state": "done", "twin": twin, "twin_source": _source(twin, originals),
                "text": f"{twin} ({_source(twin, originals)})", "commands": []}
    build = builds.get(task_id)
    if build:
        cmds = [command(f"status --task {task_id}", harm=True)]
        if build["state"] in ("needs attention", "install failed") or build["state"].startswith("stopped"):
            cmds.append(command(f"build --task {task_id} --restart", harm=True))
        return {"state": "building", "text": f"{build['child']}: {build['state']}", "commands": cmds}
    entry = survey.get(task_id) or {}
    status = entry.get("status", "")
    judgment = RUNS_DIR / "harm" / task_id / "judgment.json"
    feasible = (bool(load_json(judgment).get("feasible")) if judgment.is_file() else None)
    if status == "infeasible" or feasible is False:
        return {"state": "cannot flip", "text": "judged: cannot flip", "commands": []}
    if status == "rejected":
        return {"state": "rejected", "text": "rejected", "commands": []}
    if status == "approved":
        return {"state": "approved", "text": "approved, not built",
                "commands": [command(f"build --task {task_id}", harm=True)]}
    text = "judged: can flip, awaiting decision" if feasible else "not started"
    return {"state": "todo", "text": text,
            "commands": [command(f"session --task {task_id}", harm=True)]}


def _domain_step(case, survey: dict[str, Any], builds: list[dict[str, Any]],
                 problems: list[str]) -> dict[str, Any]:
    entry = survey.get(case.task_id) or {}
    status = entry.get("status") or "not surveyed"
    slots = domain_survey.slots_left(case, entry.get("targets"))
    out = {"status": status, "targets": entry.get("targets") or [],
           "rejected": entry.get("rejected") or [], "slots_left": slots,
           "builds": sorted(builds, key=lambda b: b["target"]), "commands": []}
    if problems:
        out.update(state="blocked", text="preflight fails: " + "; ".join(problems[:2]))
        return out
    finished = ("done", "installed, no smoke run")
    builds = [b for b in builds if b["target"] not in out["rejected"]]
    out["builds"] = sorted(builds, key=lambda b: b["target"])
    drafts = [b for b in builds if b["state"] == "plan awaiting approval"]
    unfinished = [b for b in builds if b["state"] not in finished and b not in drafts]
    out["builds"] = [b for b in out["builds"] if b not in drafts or slots]
    if builds:
        out["commands"].append(command(f"status --task {case.task_id}"))
    for b in unfinished:
        if b["state"] in ("needs attention", "install failed") or b["state"].startswith("stopped"):
            out["commands"].append(command(f"build --task {case.task_id} --target {b['target']} "
                                           f"--auto --restart"))
    built = [b["target"] for b in builds if b["state"] in finished]
    if status == "none":
        out.update(state="closed", text="decided: no more domains")
    elif unfinished:
        out.update(state="building", text=f"{len(unfinished)} build(s) not finished")
    elif slots == 0:
        out.update(state="full", text=f"full ({domain_survey.MAX_OTHER_DOMAINS} other domains)")
    else:
        if drafts:
            text = f"{len(drafts)} plan(s) awaiting approval"
        elif status == "not surveyed":
            text = "not started"
        elif status == "pending":
            text = "surveyed, awaiting decision"
        elif built:
            text = f"built {', '.join(built)}"
        else:
            text = status
        out.update(state="todo", text=f"{text}; {slots} slot(s) left")
        out["commands"].insert(0, command(f"session --task {case.task_id}"))
    return out


def collect_tracker() -> dict[str, Any]:
    cases = find_original_cases()
    originals = {c.task_id for c in cases}
    seeds = {s["seed_id"]: s for s in json.loads(SEED_FILE.read_text(encoding="utf-8"))}
    domain_survey_entries, harm_survey_entries = _entries("domain_survey.json"), _entries("harm_survey.json")
    domain_builds, harm_builds = _build_rows()

    groups: dict[tuple[str, str, str], dict[str, str]] = defaultdict(dict)
    for task_id in task_ids(TASKS_DIR):
        p = TaskId.parse(task_id)
        groups[(p.number, p.trigger, p.domain)][p.harm] = task_id

    by_seed: dict[str, list[Any]] = defaultdict(list)
    for c in cases:
        by_seed[c.seed_id].append(c)

    families = []
    numbers = sorted({k[0] for k in groups})
    for number in numbers:
        seed_id = f"S{number}"
        fam_cases = by_seed.get(seed_id, [])
        seed_triggers = {c.parsed.trigger for c in fam_cases}
        seed_harm = fam_cases[0].parsed.harm if fam_cases else "h"
        seed_domain = fam_cases[0].parsed.domain if fam_cases else ""
        rows = []
        for (num, trig, dom), harms in sorted(groups.items()):
            if num != number:
                continue
            primary = harms.get(seed_harm) or harms.get("h") or harms.get("n")
            twin = harms.get(FLIP[TaskId.parse(primary).harm])
            if primary in originals or (twin and twin in originals):
                if twin in originals and primary not in originals:
                    primary, twin = twin, primary
                kind = "original"
            elif trig in seed_triggers:
                kind = "domain variant"
            elif dom == seed_domain:
                kind = "trigger variant"
            else:
                kind = "trigger + domain variant"
            with contextlib.redirect_stdout(io.StringIO()):
                problems = preflight(TASKS_DIR / primary)
            row = {"task": primary, "trigger": trig, "domain": dom, "kind": kind,
                   "source": _source(primary, originals), "preflight": problems,
                   "notes": [i["issue"] for i in consistency_issues(TASKS_DIR / primary)],
                   "harm": _harm_step(primary, twin, originals, harm_survey_entries, harm_builds)}
            if kind == "original":
                case = next(c for c in fam_cases if c.task_id == primary)
                row["domain_step"] = _domain_step(case, domain_survey_entries,
                                                  domain_builds.get(primary, []), problems)
            rows.append(row)
        order = {"original": 0, "domain variant": 1, "trigger variant": 2, "trigger + domain variant": 3}
        rows.sort(key=lambda r: (order[r["kind"]], r["trigger"], r["domain"]))
        seed = seeds.get(seed_id) or {}
        families.append({"seed": seed_id, "instruction": seed.get("instruction", ""),
                         "has_original": bool(fam_cases), "rows": rows})

    all_rows = [r for f in families for r in f["rows"]]
    variants = [r for r in all_rows if r["kind"] == "domain variant"]
    summary = {
        "original cases": len(cases),
        "originals with a domain variant": sum(
            1 for f in families for r in f["rows"] if r["kind"] == "original"
            and any(v["kind"] == "domain variant" and v["trigger"] == r["trigger"] for v in f["rows"])),
        "domain variants (June)": sum(r["source"] == "June" for r in variants),
        "domain variants (pipeline)": sum(r["source"] == "pipeline" for r in variants),
        "groups with both h and n": sum(r["harm"]["state"] == "done" for r in all_rows),
        "groups still to flip": sum(r["harm"]["state"] in ("todo", "approved", "building") for r in all_rows),
    }
    return {"summary": summary, "families": families,
            "seeds_without_original": seeds_without_original(),
            "status_commands": [command("status --watch"), command("status --watch", harm=True)]}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>AuthorizationBench — expansion progress</title>
<style>
 body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;color:#1d1d1f;background:#fafafa}
 h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:22px 0 6px}
 a{color:#0a64d6}
 .muted{color:#6e6e73;font-size:12px}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}
 .card{background:#fff;border:1px solid #e5e5ea;border-radius:10px;padding:10px 14px;min-width:120px}
 .card b{display:block;font-size:22px}
 .filters{margin:10px 0 4px} .filters label{margin-right:14px}
 .fam{background:#fff;border:1px solid #e5e5ea;border-radius:10px;padding:10px 14px;margin:12px 0}
 .fam h2{margin:0 0 2px} .seedtext{font-size:12px;color:#3a3a3c;margin-bottom:8px}
 table{border-collapse:collapse;width:100%;table-layout:fixed}
 td{overflow:hidden}
 th,td{border-top:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
 th{font-size:12px;color:#6e6e73;font-weight:600}
 td.id{font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:nowrap}
 .pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;color:#fff;margin-right:4px;white-space:nowrap}
 .s-done{background:#30d158}.s-full{background:#30d158}.s-closed{background:#8e8e93}.s-todo{background:#0a84ff}
 .s-building{background:#bf5af2}.s-approved{background:#5e5ce6}.s-blocked{background:#ff453a}
 .s-cannot{background:#8e8e93}.s-rejected{background:#8e8e93}.s-na{background:#c7c7cc}
 .src{font-size:11px;color:#6e6e73}
 .cmd{display:flex;gap:6px;align-items:center;margin-top:4px}
 .cmd code{font:11px ui-monospace,Menlo,monospace;background:#f2f2f7;border-radius:5px;padding:3px 6px;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;display:inline-block}
 .cmd button{font-size:11px;padding:2px 8px;border:1px solid #c7c7cc;border-radius:5px;background:#fff;cursor:pointer}
 .builds{font-size:12px;color:#3a3a3c;margin-top:3px}
</style></head><body>
<h1>Expansion progress</h1>
<div class="muted">Rescans tasks/ and .expansion_runs/ every 10 s. Last update: <span id="ts"></span>
 · <a href="/">task distribution</a></div>
<div class="muted" style="margin-top:6px">A row is one (seed, trigger, domain) group; its harm column shows the h/n twin.
 Source: <b>seed</b> = original case, <b>June</b> = June generation, <b>pipeline</b> = current expansion pipeline.
 Commands show their tail; <b>copy</b> gives the full line (cd into the repo included), ready to paste into any terminal.</div>
<div class="cards" id="cards"></div>
<div id="watch"></div>
<div class="filters muted">
 <label><input type="checkbox" id="f-todo"> only families with a next step</label>
 <label><input type="checkbox" id="f-orig"> only seeds with an original case</label>
 <input id="f-q" placeholder="filter: S0005 or T0005-i-g-h" style="font-size:12px;padding:2px 6px;width:220px">
</div>
<div id="fams"></div>
<script>
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
const CLS={done:"s-done",full:"s-full",closed:"s-closed",todo:"s-todo",building:"s-building",approved:"s-approved",
 blocked:"s-blocked","cannot flip":"s-cannot",rejected:"s-rejected"};
const LABEL={done:"done",full:"full",closed:"closed",todo:"to do",building:"building",approved:"approved",
 blocked:"blocked","cannot flip":"cannot flip",rejected:"rejected"};
function pill(state){return `<span class="pill ${CLS[state]||"s-na"}">${esc(LABEL[state]||state)}</span>`}
function short(c){const i=c.indexOf("task_generation.");return i<0?c:c.slice(i+"task_generation.".length)}
function cmds(list){return (list||[]).map(c=>`<div class="cmd"><button data-c="${esc(c)}">copy</button><code title="${esc(c)}">${esc(short(c))}</code></div>`).join("")}
function domainCell(r){
 if(r.kind!=="original")return `<span class="muted">—</span>`;
 const d=r.domain_step;
 let h=pill(d.state)+`<span>${esc(d.text)}</span>`;
 if(d.targets.length)h+=`<div class="builds">approved: ${d.targets.join(", ")}</div>`;
 if(d.rejected.length)h+=`<div class="builds">rejected: ${d.rejected.join(", ")}</div>`;
 if(d.builds.length)h+=`<div class="builds">`+d.builds.map(b=>`${esc(b.child)}: ${esc(b.state)}${b.violation?` (violation ${esc(b.violation)})`:""}`).join("<br>")+`</div>`;
 return h+cmds(d.commands)}
function harmCell(r){const s=r.harm;return pill(s.state)+`<span>${esc(s.text)}</span>`+cmds(s.commands)}
function hasTodo(f){return f.rows.some(r=>["todo","approved","building"].includes(r.harm.state)||(r.domain_step&&["todo","building"].includes(r.domain_step.state)))}
let DATA=null;
function render(){
 const d=DATA;if(!d)return;
 document.getElementById("cards").innerHTML=Object.entries(d.summary).map(([k,v])=>`<div class="card"><span class="muted">${esc(k)}</span><b>${v}</b></div>`).join("");
 document.getElementById("watch").innerHTML=`<div class="muted">Watch every build:</div>`+cmds(d.status_commands)+
  (d.seeds_without_original.length?`<div class="muted" style="margin-top:6px">Seeds without an original case: ${esc(d.seeds_without_original.join(", "))}</div>`:"");
 const onlyTodo=document.getElementById("f-todo").checked,onlyOrig=document.getElementById("f-orig").checked;
 const q=document.getElementById("f-q").value.trim().toUpperCase();
 document.getElementById("fams").innerHTML=d.families.filter(f=>(!onlyTodo||hasTodo(f))&&(!onlyOrig||f.has_original)&&
   (!q||f.seed.toUpperCase().includes(q)||f.rows.some(r=>r.task.toUpperCase().includes(q)))).map(f=>
  `<div class="fam"><h2>${esc(f.seed)}${f.has_original?"":" <span class='muted'>(no original case)</span>"}</h2>
   <div class="seedtext">${esc(f.instruction)}</div>
   <table><tr><th style="width:140px">case</th><th style="width:150px">kind</th><th>domain expansion</th><th>harm flip (h ↔ n)</th></tr>`+
   f.rows.map(r=>`<tr><td class="id">${esc(r.task)}<div class="src">${esc(r.source)}${r.preflight.length?" · <span style='color:#ff453a'>preflight fails</span>":""}</div>${r.notes.length?`<div class="src" style="color:#b25000" title="${esc(r.notes.join("\n"))}">${r.notes.length} note(s), not a gate</div>`:""}</td>
     <td>${esc(r.kind)}</td><td>${domainCell(r)}</td><td>${harmCell(r)}</td></tr>`).join("")+`</table></div>`).join("");
 document.getElementById("ts").textContent=new Date().toLocaleTimeString()}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-c]");if(!b)return;
 navigator.clipboard.writeText(b.dataset.c).then(()=>{b.textContent="copied";setTimeout(()=>b.textContent="copy",1200)})});
for(const id of ["f-todo","f-orig"])document.getElementById(id).addEventListener("change",render);
document.getElementById("f-q").addEventListener("input",render);
async function tick(){try{DATA=await (await fetch("expansion.json",{cache:"no-store"})).json();render()}
 catch(e){document.getElementById("ts").textContent="update failed: "+e}}
tick();setInterval(tick,10000);
</script></body></html>"""
