"""Live distribution of installed tasks over domain, harm, and trigger.

    python -m authorization_eva_sandbox.task_generation.expansion.dashboard
    python -m authorization_eva_sandbox.task_generation.expansion.dashboard --port 8765

Serves http://localhost:<port>/, and the expansion progress page (tracker.py)
at /expansion. Every request rescans tasks/ and the expansion
runs, and the page refreshes itself, so installs and build progress show up
without restarting anything.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.task_generation.expansion import tracker
from authorization_eva_sandbox.task_generation.expansion.bundle import SANDBOX_ROOT, load_json
from authorization_eva_sandbox.task_generation.expansion.originals import (
    TASKS_DIR,
    TaskId,
    find_original_cases,
    task_ids,
)

TAXONOMY = SANDBOX_ROOT / "task_generation" / "taxonomy"
RUNS_DIR = SANDBOX_ROOT / ".expansion_runs"


def _labels() -> tuple[dict[str, str], dict[str, str]]:
    from authorization_eva_sandbox.task_generation.shared.grounding import load_trigger_contracts
    domains = {o["id"]: o["label"] for o in load_json(TAXONOMY / "domain_taxonomy.json")["options"]}
    triggers = {k: (v.get("name") or v.get("label") or v.get("title") or k)
                if isinstance(v, dict) else str(v)
                for k, v in load_trigger_contracts().items()}
    return domains, triggers


def _source(task_id: str, originals: set[str]) -> str:
    """Original means the task id matches the seed; every other installed task is an expansion."""
    return "original" if task_id in originals else "expansion"


def _builds() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from authorization_eva_sandbox.task_generation.expansion import run as run_mod
        from authorization_eva_sandbox.task_generation.expansion import session as session_mod
        for b in session_mod.all_builds(run_mod.SurveyStore()):
            r = session_mod.build_status(b)
            rows.append({"axis": "domain", **{k: r.get(k, "") for k in (
                "child", "parent", "state", "completion", "violation", "diagnosis")}})
    except Exception as exc:  # noqa: BLE001 - a broken run dir must not take the page down
        rows.append({"axis": "domain", "child": "?", "state": f"error: {exc}"})
    try:
        from authorization_eva_sandbox.task_generation.expansion.harm import cli as harm_cli
        for b in harm_cli.all_builds(harm_cli._store()):
            r = harm_cli.build_status(b)
            rows.append({"axis": "harm", **{k: r.get(k, "") for k in (
                "child", "parent", "state", "completion", "violation", "diagnosis", "label")}})
    except Exception as exc:  # noqa: BLE001
        rows.append({"axis": "harm", "child": "?", "state": f"error: {exc}"})
    return rows


def _harm_candidates() -> dict[str, Any]:
    from authorization_eva_sandbox.task_generation.expansion.harm.cases import harm_cases
    table = {}
    path = RUNS_DIR / "harm_survey.json"
    if path.is_file():
        table = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
    out = []
    for c in harm_cases():
        entry = table.get(c.task_id) or {}
        judgment_path = RUNS_DIR / "harm" / c.task_id / "judgment.json"
        feasible = None
        if judgment_path.is_file():
            feasible = bool(json.loads(judgment_path.read_text(encoding="utf-8")).get("feasible"))
        out.append({"task": c.task_id, "status": entry.get("status", "not judged"),
                    "feasible": feasible})
    return {"cases": out, "counts": dict(Counter(
        ("can flip" if r["feasible"] else "cannot flip") if r["feasible"] is not None
        else "not judged" for r in out))}


def collect() -> dict[str, Any]:
    domains, triggers = _labels()
    originals = {c.task_id for c in find_original_cases()}
    tasks = []
    for task_id in task_ids(TASKS_DIR):
        p = TaskId.parse(task_id)
        tasks.append({"id": task_id, "seed": f"S{p.number}", "trigger": p.trigger,
                      "domain": p.domain, "harm": p.harm,
                      "source": _source(task_id, originals)})
    families: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        families[t["seed"]].append(t["id"])
    builds = _builds()
    pending = [b for b in builds if b.get("state") not in ("done", "installed, no smoke run")
               and not str(b.get("state", "")).startswith("error")]
    return {
        "tasks": tasks,
        "domains": domains,
        "triggers": triggers,
        "families": dict(sorted(families.items())),
        "builds": builds,
        "pending": [{"axis": b["axis"], "child": b["child"], "state": b["state"]} for b in pending],
        "harm_candidates": _harm_candidates(),
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>AuthorizationBench — task distribution</title>
<style>
 body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;color:#1d1d1f;background:#fafafa}
 h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px}
 .muted{color:#6e6e73;font-size:12px}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}
 .card{background:#fff;border:1px solid #e5e5ea;border-radius:10px;padding:10px 14px;min-width:110px}
 .card b{display:block;font-size:22px}
 table{border-collapse:collapse;background:#fff;border:1px solid #e5e5ea}
 th,td{border:1px solid #e5e5ea;padding:5px 8px;text-align:center;vertical-align:middle}
 th{background:#f2f2f7;font-weight:600} td.l,th.l{text-align:left}
 .bar{display:flex;height:14px;min-width:4px;border-radius:3px;overflow:hidden}
 .bar span{display:block;height:100%}
 .original{background:#0a84ff}.expansion{background:#ff9f0a}
 .legend span{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px;vertical-align:-1px}
 .pend{color:#bf5af2;font-size:12px}
 .fam td{font-family:ui-monospace,Menlo,monospace;font-size:12px}
 .tag{display:inline-block;padding:1px 5px;border-radius:4px;margin:1px;font-size:11px;color:#fff}
 .grid{display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start}
</style></head><body>
<h1>Task distribution</h1>
<div class="muted">Scans tasks/ and .expansion_runs/ on every refresh (every 10 s). Last update: <span id="ts"></span>
 · <a href="/expansion">expansion progress and commands</a></div>
<div class="legend muted">source:<span class="original"></span>original (same trigger, domain, and harm as the seed)<span class="expansion"></span>expansion
 &nbsp; <span class="pend">+k</span> = in a build or awaiting approval</div>
<div class="cards" id="cards"></div>
<div class="grid">
 <div><h2>Domain × harm</h2><div id="dh"></div></div>
 <div><h2>Trigger × harm</h2><div id="th"></div></div>
</div>
<h2>Trigger × domain (all harms)</h2><div id="td"></div>
<h2>Builds not finished</h2><div id="pending"></div>
<h2>Harm candidates</h2><div id="hc"></div>
<h2>Seed families</h2><div id="fam"></div>
<script>
const SRC=["original","expansion"];
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function bar(counts,max){const tot=SRC.reduce((a,s)=>a+(counts[s]||0),0);if(!tot)return"";
 return `<div class="bar" style="width:${Math.max(6,160*tot/max)}px">`+SRC.map(s=>counts[s]?`<span class="${s}" style="width:${100*counts[s]/tot}%" title="${s}: ${counts[s]}"></span>`:"").join("")+"</div>"}
function pendFor(d,key,val){return d.pending.filter(p=>{const id=p.child.split("-");const m={trigger:id[1],domain:id[2],harm:id[3]};return key.every((k,i)=>m[k]===val[i])}).length}
function matrix(d,rowKey,rowLabels,colKey,cols){
 const cell={},rowTot={},colTot={};let max=1;
 for(const t of d.tasks){const r=t[rowKey],c=t[colKey];const k=r+"|"+c;cell[k]=cell[k]||{};cell[k][t.source]=(cell[k][t.source]||0)+1;
  rowTot[r]=(rowTot[r]||0)+1;colTot[c]=(colTot[c]||0)+1}
 for(const k in cell){max=Math.max(max,SRC.reduce((a,s)=>a+(cell[k][s]||0),0))}
 let h=`<table><tr><th class="l">${rowKey}</th>`+cols.map(c=>`<th>${esc(c)}</th>`).join("")+"<th>total</th></tr>";
 for(const [r,label] of Object.entries(rowLabels)){
  h+=`<tr><td class="l"><b>${r}</b> ${esc(label)}</td>`;
  for(const c of cols){const cc=cell[r+"|"+c]||{};const n=SRC.reduce((a,s)=>a+(cc[s]||0),0);const p=pendFor(d,[rowKey,colKey],[r,c]);
   h+=`<td><div style="display:flex;gap:6px;align-items:center"><b>${n||""}</b>${bar(cc,max)}${p?`<span class="pend">+${p}</span>`:""}</div></td>`}
  h+=`<td><b>${rowTot[r]||0}</b></td></tr>`}
 h+=`<tr><th class="l">total</th>`+cols.map(c=>`<th>${colTot[c]||0}</th>`).join("")+`<th>${d.tasks.length}</th></tr></table>`;
 return h}
function heat(d){const doms=Object.keys(d.domains);const cell={};let max=1;
 for(const t of d.tasks){const k=t.trigger+"|"+t.domain;cell[k]=cell[k]||{};cell[k][t.source]=(cell[k][t.source]||0)+1}
 for(const k in cell){max=Math.max(max,SRC.reduce((a,s)=>a+(cell[k][s]||0),0))}
 let h=`<table><tr><th class="l">trigger \\ domain</th>`+doms.map(x=>`<th title="${esc(d.domains[x])}">${x}<br><span class="muted">${esc(d.domains[x])}</span></th>`).join("")+"</tr>";
 for(const [tr,label] of Object.entries(d.triggers)){h+=`<tr><td class="l"><b>${tr}</b> <span class="muted">${esc(label)}</span></td>`;
  for(const x of doms){const cc=cell[tr+"|"+x]||{};const n=SRC.reduce((a,s)=>a+(cc[s]||0),0);
   h+=`<td><div style="display:flex;gap:6px;align-items:center"><b>${n||""}</b>${bar(cc,max)}</div></td>`}h+="</tr>"}
 return h+"</table>"}
function render(d){
 const bySrc=s=>d.tasks.filter(t=>t.source===s).length;
 document.getElementById("cards").innerHTML=[["tasks",d.tasks.length],["h",d.tasks.filter(t=>t.harm==="h").length],["n",d.tasks.filter(t=>t.harm==="n").length],
  ["original",bySrc("original")],["expansion",bySrc("expansion")],["builds not finished",d.pending.length]]
  .map(([k,v])=>`<div class="card"><span class="muted">${k}</span><b>${v}</b></div>`).join("");
 document.getElementById("dh").innerHTML=matrix(d,"domain",d.domains,"harm",["h","n"]);
 document.getElementById("th").innerHTML=matrix(d,"trigger",d.triggers,"harm",["h","n"]);
 document.getElementById("td").innerHTML=heat(d);
 document.getElementById("pending").innerHTML=d.pending.length?"<table><tr><th>axis</th><th class='l'>child</th><th class='l'>state</th></tr>"+
  d.pending.map(p=>`<tr><td>${p.axis}</td><td class="l">${esc(p.child)}</td><td class="l">${esc(p.state)}</td></tr>`).join("")+"</table>":"<span class='muted'>none</span>";
 const hc=d.harm_candidates;
 document.getElementById("hc").innerHTML=`<div class="muted">${Object.entries(hc.counts).map(([k,v])=>`${k}: ${v}`).join(" · ")}</div><table><tr><th class="l">case</th><th>judged</th><th class="l">status</th></tr>`+
  hc.cases.map(c=>`<tr><td class="l">${c.task}</td><td>${c.feasible===null?"":c.feasible?"can flip":"cannot flip"}</td><td class="l">${esc(c.status)}</td></tr>`).join("")+"</table>";
 const color={original:"#0a84ff",expansion:"#ff9f0a"};const src=Object.fromEntries(d.tasks.map(t=>[t.id,t.source]));
 document.getElementById("fam").innerHTML="<table class='fam'><tr><th>seed</th><th class='l'>tasks</th></tr>"+
  Object.entries(d.families).map(([s,ids])=>`<tr><td>${s}</td><td class="l">${ids.map(i=>`<span class="tag" style="background:${color[src[i]]}">${i}</span>`).join("")}</td></tr>`).join("")+"</table>";
 document.getElementById("ts").textContent=new Date().toLocaleTimeString()}
async function tick(){try{render(await (await fetch("data.json",{cache:"no-store"})).json())}catch(e){document.getElementById("ts").textContent="update failed: "+e}}
tick();setInterval(tick,10000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/data.json"):
            body = json.dumps(collect(), ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        elif self.path.startswith("/expansion.json"):
            body = json.dumps(tracker.collect_tracker(), ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
        elif self.path in ("/expansion", "/expansion.html"):
            body, ctype = tracker.PAGE.encode("utf-8"), "text/html; charset=utf-8"
        elif self.path in ("/runs", "/runs.html"):
            import importlib
            from authorization_eva_sandbox.evaluation import run_records
            importlib.reload(run_records)
            body = run_records.render(run_records.collect(), live=True).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"task distribution: http://localhost:{args.port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
