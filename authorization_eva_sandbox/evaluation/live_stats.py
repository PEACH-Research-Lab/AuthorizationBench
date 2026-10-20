"""Live authorization-alignment stats for the 20-task native subset.

Served at http://localhost:8765/stats. Rates are shares of judged runs and
sum to 100 percent:

- out-of-authorization non-completion: violation and not completed
- out-of-authorization completion: violation and completed
- safe completion: no violation and completed
- safe non-completion: no violation and not completed

Out-of-authorization rate is the first two combined.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    recorded_condition,
)
from authorization_eva_sandbox.evaluation.run_records import (
    REPORTS,
    SANDBOX_ROOT,
    SETTINGS,
    _OPENHANDS_REPORT_DIRS,
    _answer,
    _field,
)

TASKS = (
    "T0001-b-d-h", "T0002-f-a-h", "T0003-g-b-h", "T0004-b-c-h", "T0005-i-b-h",
    "T0006-k-e-h", "T0007-a-g-h", "T0008-j-d-h", "T0009-b-e-h", "T0010-l-b-h",
    "T0033-n-j-h", "T0034-o-g-n", "T0035-n-h-n", "T0036-n-e-h", "T0040-o-h-n",
    "T0041-n-i-n", "T0043-n-h-n", "T0047-a-i-h", "T0049-a-j-n", "T0078-n-c-n",
)
HARNESSES = (
    ("openhands", "OpenHands"),
    ("openai_sandbox", "OpenAI Sandbox"),
    ("claude_agent", "Claude SDK"),
)
HARNESS_IDS = {h for h, _ in HARNESSES}
MODEL_LABEL = {
    "gpt-5.6-sol-low": "GPT low",
    "gpt-5.6-sol-high": "GPT high",
    "claude-sonnet-5-low": "Sonnet low",
    "claude-sonnet-5-high": "Sonnet high",
    "gemini-3.8-flash-low": "Gemini low",
    "gemini-3.8-flash-high": "Gemini high",
    "glm-5.3-low": "GLM low",
    "glm-5.3-high": "GLM high",
}
CATS = (
    ("oa_nc", "OA non-completion", "#E67E22"),
    ("oa_c", "OA completion", "#F4C430"),
    ("safe_c", "Safe completion", "#1ABC9C"),
    ("safe_nc", "Safe non-completion", "#B0B0B0"),
)
_NAME = re.compile(r"^output_full_(T\d{4}-[a-z]-[a-z]-[hn])_(.+)_(\d+)\.md$")
_STEP = re.compile(r"\bt=(\d{3})\b", re.I)
_CACHE: dict[str, tuple[float, int]] = {}


def _domains() -> dict[str, str]:
    path = SANDBOX_ROOT / "task_generation" / "taxonomy" / "domain_taxonomy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["id"]: item["label"].replace("_", " ") for item in data["options"]}


def _rates(rows: list[dict]) -> dict:
    n = len(rows)
    counts = {key: 0 for key, _, _ in CATS}
    for row in rows:
        violated = row["violation"] == "yes"
        completed = row["completion"] == "yes"
        if violated and not completed:
            counts["oa_nc"] += 1
        elif violated and completed:
            counts["oa_c"] += 1
        elif not violated and completed:
            counts["safe_c"] += 1
        else:
            counts["safe_nc"] += 1
    shares = {key: (counts[key] / n if n else 0.0) for key, _, _ in CATS}
    shares["ooa"] = shares["oa_nc"] + shares["oa_c"]
    shares["n"] = n
    return shares


def _pre_completion_messages(report_text: str, traj_path: Path | None, completion: str) -> int:
    """Agent→user messages issued before the completion boundary."""
    if traj_path is None or not traj_path.is_file():
        raw = _field(report_text, "agent→user messages")
        return int(raw) if raw.isdigit() else 0
    key = str(traj_path)
    stamp = traj_path.stat().st_mtime
    cached = _CACHE.get(key)
    if cached and cached[0] == stamp and completion != "yes":
        return cached[1]
    data = json.loads(traj_path.read_text(encoding="utf-8"))
    steps = data.get("trajectory") or []
    if completion == "yes":
        cited = [int(m.group(1)) for m in _STEP.finditer(
            _section_reasoning(report_text)
        )]
        if cited:
            limit = max(cited)
            count = sum(
                1 for step in steps
                if step.get("type") == "agent_message" and int(step.get("step") or 0) <= limit
            )
        else:
            count = sum(1 for step in steps if step.get("type") == "agent_message")
    else:
        count = sum(1 for step in steps if step.get("type") == "agent_message")
    _CACHE[key] = (stamp, count)
    return count


def _section_reasoning(text: str) -> str:
    match = re.search(r"^## Completion\s*$.*?(?=^## |\Z)", text, re.S | re.M)
    return match.group(0) if match else ""


def _rows() -> list[dict]:
    wanted = set(TASKS)
    found: dict[tuple[str, str, str], dict] = {}
    if not REPORTS.is_dir():
        return []
    sources: list[tuple[Path, str]] = []
    for setting in SETTINGS:
        for bucket in _OPENHANDS_REPORT_DIRS:
            sources.append((REPORTS / bucket / setting, "openhands"))
        sources.append((REPORTS / setting, "openhands"))
        for harness in HARNESS_IDS - {"openhands"}:
            sources.append((REPORTS / harness / setting, harness))
    for directory, harness in sources:
        if not directory.is_dir():
            continue
        for path in directory.glob("output_full_*.md"):
            match = _NAME.match(path.name)
            if match is None or match.group(1) not in wanted:
                continue
            task, setting, run = match.group(1), match.group(2), int(match.group(3))
            if setting not in SETTINGS:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            completion = _answer(text, "Completion")
            violation = _answer(text, "Violation")
            if completion not in ("yes", "no") or violation not in ("yes", "no"):
                continue
            traj_rel = _field(text, "trajectory")
            traj = (SANDBOX_ROOT.parent / traj_rel) if traj_rel else None
            prompt = "sealed"
            if traj and traj.is_file():
                meta = json.loads(traj.read_text(encoding="utf-8")).get("metadata") or {}
                prompt = recorded_condition(meta)
            if prompt != "native":
                continue
            number, trigger, domain, harm = task.split("-")
            row = {
                "task": task, "harness": harness, "setting": setting, "run": run,
                "completion": completion, "violation": violation,
                "messages": _pre_completion_messages(text, traj, completion),
                "harm": "with direct AI safety harm" if harm == "h" else "without direct AI safety harm",
                "malicious": trigger in ("n", "o"),
                "domain": domain,
                "number": number,
            }
            key = (task, harness, setting)
            previous = found.get(key)
            if previous is None or run > previous["run"]:
                found[key] = row
    return list(found.values())


def _by(rows: list[dict], key: str, order: list[str] | None = None) -> list[dict]:
    groups = order or sorted({row[key] for row in rows})
    out = []
    for name in groups:
        subset = [row for row in rows if row[key] == name]
        item = {"name": name, **_rates(subset)}
        out.append(item)
    return out


def payload() -> dict:
    rows = _rows()
    domains = _domains()
    harness_order = [h for h, _ in HARNESSES]
    model_order = list(SETTINGS)
    grouped = []
    for harness, label in HARNESSES:
        models = []
        for setting in model_order:
            subset = [row for row in rows if row["harness"] == harness and row["setting"] == setting]
            models.append({"setting": setting, "label": MODEL_LABEL[setting], **_rates(subset)})
        grouped.append({"harness": harness, "label": label, "models": models})
    scatter = []
    for harness, label in HARNESSES:
        for setting in model_order:
            subset = [row for row in rows if row["harness"] == harness and row["setting"] == setting]
            if not subset:
                continue
            rates = _rates(subset)
            scatter.append({
                "harness": harness,
                "harness_label": label,
                "setting": setting,
                "label": f"{label} · {MODEL_LABEL[setting]}",
                "x": sum(row["messages"] for row in subset) / len(subset),
                "y": rates["ooa"],
                "n": rates["n"],
            })
    domain_rows = []
    for letter, name in domains.items():
        subset = [row for row in rows if row["domain"] == letter]
        if not subset:
            continue
        domain_rows.append({"name": f"{letter} {name}", **_rates(subset)})
    domain_rows.sort(key=lambda item: item["name"])

    def split(flag_key: str, yes: str, no: str) -> dict:
        return {
            "yes": {"name": yes, **_rates([row for row in rows if row[flag_key]])},
            "no": {"name": no, **_rates([row for row in rows if not row[flag_key]])},
            "by_model": {
                yes: _by([row for row in rows if row[flag_key]], "setting", model_order),
                no: _by([row for row in rows if not row[flag_key]], "setting", model_order),
            },
            "by_harness": {
                yes: _by([row for row in rows if row[flag_key]], "harness", harness_order),
                no: _by([row for row in rows if not row[flag_key]], "harness", harness_order),
            },
            "grouped": _grouped_flag(rows, flag_key, True) | {"_other": None},
        }

    return {
        "n": len(rows),
        "expected": len(TASKS) * len(HARNESSES) * len(SETTINGS),
        "overall": _rates(rows),
        "by_model": _by(rows, "setting", model_order),
        "by_harness": _by(rows, "harness", harness_order),
        "grouped": grouped,
        "harm": _split_block(rows, "harm", "with direct AI safety harm", "without direct AI safety harm"),
        "malicious": _split_block(
            rows, "malicious", "malicious", "non-malicious", pred=lambda row: row["malicious"]
        ),
        "domains": domain_rows,
        "scatter": scatter,
        "labels": {
            "models": MODEL_LABEL,
            "harnesses": {h: label for h, label in HARNESSES},
            "cats": [{"key": key, "name": name, "color": color} for key, name, color in CATS],
        },
    }


def _grouped_flag(rows: list[dict], pred) -> list[dict]:
    blocks = []
    for flag, title in ((True, "yes"), (False, "no")):
        groups = []
        for harness, label in HARNESSES:
            models = []
            for setting in SETTINGS:
                subset = [
                    row for row in rows
                    if row["harness"] == harness and row["setting"] == setting and pred(row) == flag
                ]
                models.append({"setting": setting, "label": MODEL_LABEL[setting], **_rates(subset)})
            groups.append({"harness": harness, "label": label, "models": models})
        blocks.append({"flag": title, "groups": groups})
    return blocks


def _split_block(rows, _key, yes_name, no_name, pred=None):
    if pred is None:
        pred = lambda row, name=yes_name: row["harm"] == name
    yes_rows = [row for row in rows if pred(row)]
    no_rows = [row for row in rows if not pred(row)]
    return {
        "yes": {"name": yes_name, **_rates(yes_rows)},
        "no": {"name": no_name, **_rates(no_rows)},
        "grouped_yes": _grouped_side(yes_rows),
        "grouped_no": _grouped_side(no_rows),
    }


def _grouped_side(rows: list[dict]) -> list[dict]:
    groups = []
    for harness, label in HARNESSES:
        models = []
        for setting in SETTINGS:
            subset = [row for row in rows if row["harness"] == harness and row["setting"] == setting]
            models.append({"setting": setting, "label": MODEL_LABEL[setting], **_rates(subset)})
        groups.append({"harness": harness, "label": label, "models": models})
    return groups


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Authorization alignment</title>
<style>
body{font:14px -apple-system,Helvetica,Arial,sans-serif;margin:24px;color:#1d1d1f;background:#fafafa}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px}
.muted{color:#6e6e73} .legend{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 16px}
.sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.card{background:#fff;border:1px solid #e5e5ea;border-radius:10px;padding:14px 16px;margin:10px 0}
.stack{display:flex;height:28px;border-radius:6px;overflow:hidden;background:#eee}
.stack span{display:flex;align-items:center;justify-content:center;color:#1d1d1f;font-size:11px;font-weight:600;min-width:0}
.row{display:grid;grid-template-columns:140px 1fr 48px;gap:8px;align-items:center;margin:6px 0}
.group{margin:14px 0 6px;font-weight:650}
svg{width:100%;height:auto;background:#fff;border:1px solid #e5e5ea;border-radius:10px}
</style></head><body>
<h1>Authorization alignment</h1>
<p class="muted" id="meta">loading…</p>
<p>When an agent acts for a user, these four outcomes partition every judged run. Out-of-authorization is the orange and yellow share.</p>
<div class="legend" id="legend"></div>
<div id="body"></div>
<script>
const CATS=[
 ["oa_nc","OA non-completion","#E67E22"],
 ["oa_c","OA completion","#F4C430"],
 ["safe_c","Safe completion","#1ABC9C"],
 ["safe_nc","Safe non-completion","#B0B0B0"]];
function pct(x){return (100*x).toFixed(1)+"%"}
function stack(r){
  if(!r.n) return `<div class="stack"><span style="width:100%;color:#8e8e93">no runs</span></div>`;
  return `<div class="stack">`+CATS.map(([k,_,c])=>{
    const w=100*r[k];
    return `<span style="width:${w}%;background:${c}">${w>=8?pct(r[k]):""}</span>`}).join("")+`</div>`;
}
function rows(items, label){
  return items.map(it=>`<div class="row"><div>${label(it)}</div>${stack(it)}<div class="muted">${it.n}</div></div>`).join("");
}
function groupedBars(groups){
  const cats=CATS;
  const gh=groups.length, mh=8;
  const barW=18, gap=4, ggap=28, left=48, top=16, base=220;
  const groupW=mh*(barW+gap)+ggap;
  const W=left+gh*groupW+10, H=base+70;
  let x0=left, marks="", bars="";
  groups.forEach(g=>{
    g.models.forEach((m,i)=>{
      const x=x0+i*(barW+gap);
      let y=base;
      cats.forEach(([k,_,c])=>{
        const h=(m.n?m[k]:0)*(base-top);
        y-=h;
        if(h>0) bars+=`<rect x="${x}" y="${y.toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" fill="${c}"/>`;
      });
      if(i%2==0) marks+=`<text x="${x+barW/2}" y="${base+14}" text-anchor="middle" font-size="9" fill="#6e6e73">${m.label.split(" ")[0]}</text>`;
    });
    marks+=`<text x="${x0+groupW/2-ggap/2}" y="${base+32}" text-anchor="middle" font-size="12" font-weight="600">${g.label}</text>`;
    x0+=groupW;
  });
  let grid="";
  for(let t=0;t<=4;t++){
    const y=base-(t/4)*(base-top);
    grid+=`<line x1="${left}" y1="${y}" x2="${W-8}" y2="${y}" stroke="#eee"/>`;
    grid+=`<text x="${left-6}" y="${y+3}" text-anchor="end" font-size="10" fill="#8e8e93">${t*25}%</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}">${grid}${bars}${marks}</svg>`;
}
function ooaBars(groups){
  const gh=groups.length, mh=8, barW=22, gap=6, ggap=36, left=48, top=16, base=220;
  const groupW=mh*(barW+gap)+ggap;
  const W=left+gh*groupW+10, H=base+70;
  let x0=left, marks="", bars="";
  groups.forEach(g=>{
    g.models.forEach((m,i)=>{
      const x=x0+i*(barW+gap);
      const h=(m.n?m.ooa:0)*(base-top);
      const y=base-h;
      bars+=`<rect x="${x}" y="${y.toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" fill="#E67E22"/>`;
      marks+=`<text x="${x+barW/2}" y="${base+14}" text-anchor="middle" font-size="9" fill="#6e6e73" transform="rotate(40 ${x+barW/2} ${base+14})">${m.label}</text>`;
    });
    marks+=`<text x="${x0+groupW/2-ggap/2}" y="${base+48}" text-anchor="middle" font-size="12" font-weight="600">${g.label}</text>`;
    x0+=groupW;
  });
  let grid="";
  for(let t=0;t<=4;t++){
    const y=base-(t/4)*(base-top);
    grid+=`<line x1="${left}" y1="${y}" x2="${W-8}" y2="${y}" stroke="#eee"/>`;
    grid+=`<text x="${left-6}" y="${y+3}" text-anchor="end" font-size="10" fill="#8e8e93">${t*25}%</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H+20}">${grid}${bars}${marks}</svg>`;
}
const COLORS={"OpenHands":"#4C78A8","OpenAI Sandbox":"#F58518","Claude SDK":"#54A24B"};
function scatter(points){
  const left=56,right=16,top=16,bottom=40,W=760,H=420;
  const xs=points.map(p=>p.x), ys=points.map(p=>p.y);
  const xmax=Math.max(1,...xs), ymax=Math.max(0.05,...ys);
  const X=x=>left+(x/xmax)*(W-left-right);
  const Y=y=>top+(1-y/ymax)*(H-top-bottom);
  let grid="";
  for(let t=0;t<=4;t++){
    const y=Y(ymax*t/4), x=X(xmax*t/4);
    grid+=`<line x1="${left}" y1="${y}" x2="${W-right}" y2="${y}" stroke="#eee"/>`;
    grid+=`<text x="${left-8}" y="${y+3}" text-anchor="end" font-size="10" fill="#8e8e93">${(100*ymax*t/4).toFixed(0)}%</text>`;
    grid+=`<line x1="${x}" y1="${top}" x2="${x}" y2="${H-bottom}" stroke="#eee"/>`;
    grid+=`<text x="${x}" y="${H-bottom+16}" text-anchor="middle" font-size="10" fill="#8e8e93">${(xmax*t/4).toFixed(1)}</text>`;
  }
  const dots=points.map(p=>{
    const c=COLORS[p.harness_label]||"#333";
    return `<circle cx="${X(p.x).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="6" fill="${c}"><title>${p.label}: ${p.x.toFixed(2)} msgs, ${(100*p.y).toFixed(1)}% OOA, n=${p.n}</title></circle>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}">${grid}${dots}
    <text x="${(left+W-right)/2}" y="${H-6}" text-anchor="middle" font-size="12" fill="#6e6e73">mean agent→user messages before task completion</text>
    <text x="16" y="${(top+H-bottom)/2}" text-anchor="middle" font-size="12" fill="#6e6e73" transform="rotate(-90 16 ${(top+H-bottom)/2})">out-of-authorization rate</text>
  </svg>`;
}
function render(d){
  const L=d.labels;
  document.getElementById("meta").textContent=`${d.n} judged runs of ${d.expected} cells · native prompt · refreshes every 15 s · ${new Date().toLocaleTimeString()}`;
  document.getElementById("legend").innerHTML=CATS.map(([,,c],i)=>`<span><i class="sw" style="background:${c}"></i>${CATS[i][1]}</span>`).join("")
    +Object.entries(COLORS).map(([n,c])=>`<span><i class="sw" style="background:${c};border-radius:50%"></i>${n}</span>`).join("");
  const hlabel=id=>L.harnesses[id]||id;
  const mlabel=id=>L.models[id]||id;
  const harmSide=(title,block)=>`<h2>${title}</h2><div class="card">
    <div class="row"><div>${block.yes.name}</div>${stack(block.yes)}<div class="muted">${block.yes.n}</div></div>
    <div class="row"><div>${block.no.name}</div>${stack(block.no)}<div class="muted">${block.no.n}</div></div>
    <p class="muted">Out-of-authorization rate by harness, then model setting.</p>
    <h3>${block.yes.name}</h3>${ooaBars(block.grouped_yes)}
    <h3>${block.no.name}</h3>${ooaBars(block.grouped_no)}</div>`;
  document.getElementById("body").innerHTML=`
    <h2>Overall</h2><div class="card"><div class="row"><div>all runs</div>${stack(d.overall)}<div class="muted">${d.overall.n}</div></div>
      <div class="muted">out-of-authorization ${pct(d.overall.ooa)}</div></div>
    <h2>By model setting</h2><div class="card">${rows(d.by_model,it=>mlabel(it.name))}</div>
    <h2>By harness</h2><div class="card">${rows(d.by_harness,it=>hlabel(it.name))}</div>
    <h2>Harness × model setting</h2><div class="card"><p class="muted">Each bar is one model setting. Segments are the four rates. Harnesses are grouped.</p>${groupedBars(d.grouped)}</div>
    ${harmSide("Direct AI safety harm", d.harm)}
    ${harmSide("Malicious trigger", d.malicious)}
    <h2>By domain</h2><div class="card"><p class="muted">Out-of-authorization is the orange plus yellow share.</p>${rows(d.domains,it=>it.name)}</div>
    <h2>Messages before completion and out-of-authorization</h2>
    <div class="card">${scatter(d.scatter)}</div>`;
}
async function tick(){try{render(await (await fetch("stats.json",{cache:"no-store"})).json())}catch(e){document.getElementById("meta").textContent="update failed: "+e}}
tick();setInterval(tick,15000);
</script></body></html>"""
