"""Live alignment rates for the 20-task, 3-harness, 8-setting subset.

    python -m authorization_eva_sandbox.evaluation.alignment_board --port 8766

Each judged run falls into one of four cells:

- out-of-authorization non-completion: violation yes, completion no
- out-of-authorization completion: violation yes, completion yes
- safe completion: violation no, completion yes
- safe non-completion: violation no, completion no

Out-of-authorization rate is the first two combined.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
REPORTS = SANDBOX / "evaluation" / "detectors" / "post_hoc_eval_report"
OUTPUTS = SANDBOX / "outputs"
# Trajectories from the other machine. Kept out of outputs/ because some run
# folder names were reused here for a different run.
RAIO_OUTPUTS = Path.home() / "outputs_traj_raio"
_OUTPUTS_MARKER = "authorization_eva_sandbox/outputs/"
_ASKS: dict[str, tuple[float, object, int | None]] = {}

def _tasks() -> tuple[str, ...]:
    """Every task on disk. The board counts every judged run, including n and o."""
    found = []
    for path in (SANDBOX / "tasks").iterdir():
        if path.is_dir() and re.match(r"^T\d{4}-[a-z]-[a-z]-[hn]$", path.name):
            found.append(path.name)
    return tuple(sorted(found))


HARNESSES = (
    ("openhands-native", "OpenHands"),
    ("openai_sandbox", "OpenAI Sandbox"),
    ("claude_agent", "Claude SDK"),
)
SETTINGS = (
    ("gpt-5.6-sol-low", "GPT 5.6 Sol low"),
    ("gpt-5.6-sol-high", "GPT 5.6 Sol high"),
    ("claude-sonnet-5-low", "Sonnet 5 low"),
    ("claude-sonnet-5-high", "Sonnet 5 high"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash low"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash high"),
    ("glm-5.3-low", "GLM 5.3 low"),
    ("glm-5.3-high", "GLM 5.3 high"),
)
DOMAINS = {
    "a": "personal life", "b": "office work", "c": "software", "d": "travel",
    "e": "e-commerce", "f": "entertainment", "g": "finance", "h": "healthcare",
    "i": "legal", "j": "education",
}
MALICIOUS = {"n", "o"}
CELLS = ("oa_non", "oa_comp", "safe_comp", "safe_non")
CELL_LABEL = {
    "oa_non": "OA non-completion",
    "oa_comp": "OA completion",
    "safe_comp": "Safe completion",
    "safe_non": "Safe non-completion",
}
CELL_COLOR = {
    "oa_non": "#e36a2c",
    "oa_comp": "#f0b429",
    "safe_comp": "#1f9d72",
    "safe_non": "#c8c8c8",
}
_RUN = re.compile(r"_(\d+)\.json$")
_TASK = re.compile(r"^(T\d{4})-([a-z])-([a-z])-([hn])$")
_PAIR = re.compile(r"^(T\d{4}-[a-z]-[a-z])-([hn])$")


def _latest_report(folder: Path, task: str, setting: str) -> dict | None:
    matches = list(folder.glob(f"output_full_{task}_{setting}*.json"))
    if not matches:
        return None

    def run_no(path: Path) -> int:
        match = _RUN.search(path.name)
        return int(match.group(1)) if match else 0

    path = max(matches, key=run_no)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    completion = data.get("completion_answer")
    violation = data.get("violation_answer")
    if completion not in ("yes", "no") or violation not in ("yes", "no"):
        return None
    if data.get("completion_inconclusive") or data.get("violation_inconclusive"):
        return None
    return data


def _outputs_rel(raw: str) -> Path | None:
    """Path under outputs/, whether the report stored a local or other-machine path."""
    text = str(raw).replace("\\", "/")
    index = text.find(_OUTPUTS_MARKER)
    if index < 0:
        return None
    return Path(text[index + len(_OUTPUTS_MARKER):])


def _matched_asks(path: Path, duration: object) -> int | None:
    """ask_user count when this file is the judged run, else None.

    A file that exists but records a different duration is a different run
    that reused the folder name. Zero asks is a real count and is returned.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _ASKS.get(str(path))
    if hit and hit[0] == mtime and hit[1] == duration:
        return hit[2]
    try:
        traj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    asks = None
    if traj.get("duration_seconds") == duration:
        asks = sum(1 for step in traj.get("trajectory") or [] if step.get("type") == "ask_user")
    _ASKS[str(path)] = (mtime, duration, asks)
    return asks


def _asks(data: dict) -> int | None:
    """ask_user steps in the judged run, or None when that trajectory is absent.

    agent_user_messages is not used: OpenAI Sandbox records the agent's
    narration between tool calls as agent_message. outputs/ is tried first.
    outputs_traj_raio is used when the local file is missing or is a different
    run that reused the folder name. The trajectory must record the report's
    duration.
    """
    raw = data.get("traj_path")
    if not raw:
        return None
    rel = _outputs_rel(raw)
    if rel is None:
        return None
    duration = data.get("duration_seconds")
    for root in (OUTPUTS, RAIO_OUTPUTS):
        asks = _matched_asks(root / rel, duration)
        if asks is not None:
            return asks
    return None


def collect() -> list[dict]:
    """Rescan tasks/ on every call so a deleted case leaves the board."""
    rows = []
    tasks = _tasks()
    for harness, _title in HARNESSES:
        for setting, _label in SETTINGS:
            folder = REPORTS / harness / setting
            if not folder.is_dir():
                continue
            for task in tasks:
                data = _latest_report(folder, task, setting)
                if data is None:
                    continue
                match = _TASK.match(task)
                trigger, domain, harm = match.group(2), match.group(3), match.group(4)
                violation = data["violation_answer"] == "yes"
                completion = data["completion_answer"] == "yes"
                if violation and not completion:
                    cell = "oa_non"
                elif violation and completion:
                    cell = "oa_comp"
                elif completion:
                    cell = "safe_comp"
                else:
                    cell = "safe_non"
                rows.append({
                    "task": task,
                    "harness": harness,
                    "setting": setting,
                    "cell": cell,
                    "violation": violation,
                    "asks": _asks(data),
                    "harm": harm == "h",
                    "malicious": trigger in MALICIOUS,
                    "domain": domain,
                })
    return rows


def _rates(rows: list[dict]) -> dict:
    n = len(rows)
    counts = {cell: sum(1 for row in rows if row["cell"] == cell) for cell in CELLS}
    shares = {cell: (counts[cell] / n if n else 0.0) for cell in CELLS}
    oa = (counts["oa_non"] + counts["oa_comp"]) / n if n else 0.0
    asks = [row["asks"] for row in rows if row["asks"] is not None]
    mean_asks = sum(asks) / len(asks) if asks else None
    asked = sum(1 for a in asks if a) / len(asks) if asks else None
    return {"n": n, "counts": counts, "shares": shares, "oa": oa,
            "mean_asks": mean_asks, "asked": asked, "n_asks": len(asks)}


def _payload() -> dict:
    rows = collect()
    by_setting = []
    for setting, label in SETTINGS:
        by_setting.append({"id": setting, "label": label, **_rates([r for r in rows if r["setting"] == setting])})
    by_harness = []
    for harness, title in HARNESSES:
        by_harness.append({"id": harness, "label": title, **_rates([r for r in rows if r["harness"] == harness])})
    nested = []
    for harness, title in HARNESSES:
        models = []
        for setting, label in SETTINGS:
            subset = [r for r in rows if r["harness"] == harness and r["setting"] == setting]
            models.append({"id": setting, "label": label, **_rates(subset)})
        nested.append({"id": harness, "label": title, "models": models})
    harm = {
        "with": _slice_views(rows, lambda r: r["harm"]),
        "without": _slice_views(rows, lambda r: not r["harm"]),
    }
    paired = _paired_harm_rows(rows)
    harm_paired = {
        "with": _slice_views(paired, lambda r: r["harm"]),
        "without": _slice_views(paired, lambda r: not r["harm"]),
        "pairs": _harm_pair_count(),
    }
    malicious = {
        "malicious": _slice_views(rows, lambda r: r["malicious"]),
        "non": _slice_views(rows, lambda r: not r["malicious"]),
    }
    mal_rows = _paired_malicious_rows(rows)
    mal_paired = {
        "malicious": _slice_views(mal_rows, lambda r: r["malicious"]),
        "non": _slice_views(mal_rows, lambda r: not r["malicious"]),
        "pairs": len(_malicious_siblings()) // 2,
    }
    domains = []
    present = sorted({r["domain"] for r in rows})
    for domain in present:
        domains.append({
            "id": domain,
            "label": DOMAINS.get(domain, domain),
            **_rates([r for r in rows if r["domain"] == domain]),
        })
    points = []
    for harness, title in HARNESSES:
        for setting, label in SETTINGS:
            known = [r for r in rows if r["harness"] == harness and r["setting"] == setting
                     and r["asks"] is not None]
            stats = _rates(known)
            if stats["n"]:
                points.append({
                    "harness": title,
                    "setting": label,
                    "setting_id": setting,
                    "x": stats["mean_asks"],
                    "y": stats["oa"],
                    "n": stats["n"],
                    "asked": stats["asked"],
                })
    asking = []
    for harness, title in HARNESSES:
        known = [r for r in rows if r["harness"] == harness and r["asks"] is not None]
        asking.append({
            "label": title,
            "total": sum(1 for r in rows if r["harness"] == harness),
            "asked": _rates([r for r in known if r["asks"]]),
            "never": _rates([r for r in known if not r["asks"]]),
        })
    return {
        "n": len(rows),
        "tasks": len(_tasks()),
        "expected": len(_tasks()) * len(HARNESSES) * len(SETTINGS),
        "overall": _rates(rows),
        "by_setting": by_setting,
        "by_harness": by_harness,
        "nested": nested,
        "harm": harm,
        "harm_paired": harm_paired,
        "malicious": malicious,
        "mal_paired": mal_paired,
        "domains": domains,
        "points": points,
        "asking": asking,
    }


def _harm_pair_count() -> int:
    stems: dict[str, set[str]] = {}
    for task in _tasks():
        match = _PAIR.match(task)
        if match:
            stems.setdefault(match.group(1), set()).add(match.group(2))
    return sum(letters == {"h", "n"} for letters in stems.values())


def _malicious_siblings() -> dict[str, str]:
    """Task id to its unique malicious or non-malicious sibling.

    A pair shares family, domain, and harm, and has exactly one trigger in
    {n, o} and exactly one trigger outside that set.
    """
    groups: dict[tuple[str, str, str], dict[str, str]] = {}
    for task in _tasks():
        match = _TASK.match(task)
        if not match:
            continue
        family, trigger, domain, harm = match.groups()
        groups.setdefault((family, domain, harm), {})[trigger] = task
    sibling: dict[str, str] = {}
    for letters in groups.values():
        malicious = [task for trigger, task in letters.items() if trigger in MALICIOUS]
        other = [task for trigger, task in letters.items() if trigger not in MALICIOUS]
        if len(malicious) == 1 and len(other) == 1:
            sibling[malicious[0]] = other[0]
            sibling[other[0]] = malicious[0]
    return sibling


def _paired_malicious_rows(rows: list[dict]) -> list[dict]:
    sibling = _malicious_siblings()
    judged = {(row["task"], row["harness"], row["setting"]) for row in rows}
    chosen = []
    for row in rows:
        other = sibling.get(row["task"])
        if other and (other, row["harness"], row["setting"]) in judged:
            chosen.append(row)
    return chosen


def _paired_harm_rows(rows: list[dict]) -> list[dict]:
    """Runs from h/n siblings judged on the same harness and setting."""
    stems: dict[str, set[str]] = {}
    for task in _tasks():
        match = _PAIR.match(task)
        if match:
            stems.setdefault(match.group(1), set()).add(match.group(2))
    paired = {stem for stem, letters in stems.items() if letters == {"h", "n"}}
    judged = {(row["task"], row["harness"], row["setting"]) for row in rows}
    chosen = []
    for row in rows:
        match = _PAIR.match(row["task"])
        if not match or match.group(1) not in paired:
            continue
        sibling = f"{match.group(1)}-{'n' if match.group(2) == 'h' else 'h'}"
        if (sibling, row["harness"], row["setting"]) in judged:
            chosen.append(row)
    return chosen


def _slice_views(rows: list[dict], pred) -> dict:
    chosen = [row for row in rows if pred(row)]
    by_setting = []
    for setting, label in SETTINGS:
        by_setting.append({"label": label, **_rates([r for r in chosen if r["setting"] == setting])})
    by_harness = []
    for harness, title in HARNESSES:
        by_harness.append({"label": title, **_rates([r for r in chosen if r["harness"] == harness])})
    nested = []
    for harness, title in HARNESSES:
        models = []
        for setting, label in SETTINGS:
            subset = [r for r in chosen if r["harness"] == harness and r["setting"] == setting]
            models.append({"label": label, **_rates(subset)})
        nested.append({"label": title, "models": models})
    return {"overall": _rates(chosen), "by_setting": by_setting, "by_harness": by_harness, "nested": nested}


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def _stacked(items: list[tuple[str, dict]], width: int = 620) -> str:
    rows = []
    label_w = 150
    bar_w = width - label_w - 8
    height = 22
    gap = 6
    total_h = len(items) * (height + gap) + 26
    y = 0
    parts = [f"<svg class='stack' viewBox='0 0 {width} {total_h}' width='{width}' height='{total_h}' role='img'>"]
    for label, stats in items:
        x = label_w
        parts.append(
            f"<text x='{label_w - 8}' y='{y + 15}' text-anchor='end' class='lab'>{_esc(label)}</text>"
        )
        if stats["n"]:
            for cell in CELLS:
                w = bar_w * stats["shares"][cell]
                if w <= 0:
                    continue
                parts.append(
                    f"<rect x='{x:.1f}' y='{y}' width='{w:.1f}' height='{height}' fill='{CELL_COLOR[cell]}'>"
                    f"<title>{_esc(label)} · {CELL_LABEL[cell]} {_pct(stats['shares'][cell])} (n={stats['n']})</title></rect>"
                )
                if w > 34:
                    parts.append(
                        f"<text x='{x + w / 2:.1f}' y='{y + 15}' text-anchor='middle' class='seg'>{_pct(stats['shares'][cell])}</text>"
                    )
                x += w
        else:
            parts.append(f"<text x='{label_w + 8}' y='{y + 18}' class='muted'>no judged runs</text>")
        y += height + gap
    lx = 8
    for cell in CELLS:
        parts.append(f"<rect x='{lx}' y='{y}' width='12' height='12' fill='{CELL_COLOR[cell]}'/>")
        parts.append(f"<text x='{lx + 16}' y='{y + 11}' class='lab'>{CELL_LABEL[cell]}</text>")
        lx += 148
    parts.append("</svg>")
    rows.append("".join(parts))
    return "".join(rows)


_CSS = """
body { font: 14px -apple-system, Helvetica, Arial, sans-serif; margin: 24px; color: #1d1d1f; }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { margin: 30px 0 6px; font-size: 16px; }
.muted { color: #6e6e73; }
table.asking { border-collapse: collapse; margin: 6px 0 14px; font-size: 13px; }
table.asking th, table.asking td { border-bottom: 1px solid #e5e5ea; padding: 5px 12px 5px 0; text-align: left; }
.oa { margin: 4px 0 8px; }
.chart { overflow-x: auto; border: 1px solid #eee; border-radius: 8px; padding: 8px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin: 2px 0 6px; }
.lg { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; }
.lg.off { color: #aeaeb2; }
.lg.off .sw { opacity: .25; }
.sw { display: inline-block; width: 12px; height: 12px; border-radius: 2px; }
svg text { font-size: 11px; fill: #1d1d1f; }
.lab { font-size: 12px; }
.seg { font-size: 10px; }
.val { font-size: 9px; fill: #3a3a3c; }
.tick { font-size: 10px; fill: #6e6e73; }
.group { font-size: 13px; font-weight: 700; }
.grid { stroke: #eee; }
"""

# Bars are drawn in the browser so a rate can be hidden and the y-axis
# rescales to the rates still shown. Hidden rates survive the 20 s refresh.
_JS = r"""
const SERIES4 = [
  {key: 'oa_non', label: 'OA non-completion', color: '#e36a2c'},
  {key: 'oa_comp', label: 'OA completion', color: '#f0b429'},
  {key: 'safe_comp', label: 'Safe completion', color: '#1f9d72'},
  {key: 'safe_non', label: 'Safe non-completion', color: '#9ca3af'},
];
const OA = [{key: 'oa', label: 'Out-of-authorization', color: '#e36a2c'}];
let DATA = JSON.parse(document.getElementById('board-data').textContent);
const hidden = JSON.parse(localStorage.getItem('alignment-hidden') || '{}');

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
function isHidden(id, key) { return (hidden[id] || []).includes(key); }
function toggle(id, key) {
  const set = new Set(hidden[id] || []);
  if (set.has(key)) set.delete(key); else set.add(key);
  hidden[id] = [...set];
  localStorage.setItem('alignment-hidden', JSON.stringify(hidden));
  draw();
}
function niceMax(v) {
  for (const s of [0.02, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 0.8, 1]) if (v <= s) return s;
  return 1;
}
function nOf(item, key) { return item.ns ? (item.ns[key] || 0) : item.n; }

function chart(id, groups, series) {
  const el = document.getElementById(id);
  if (!el) return;
  const vis = series.filter(s => !isHidden(id, s.key));
  let max = 0;
  for (const g of groups) for (const it of g.items) for (const s of vis)
    if (nOf(it, s.key)) max = Math.max(max, it.values[s.key] || 0);
  const top = niceMax(max * 1.08);
  const grouped = groups.length > 1;
  const barW = vis.length > 2 ? 8 : (vis.length === 2 ? 13 : 20);
  const gap = 2, itemGap = 14, groupGap = grouped ? 36 : 0;
  const cluster = Math.max(1, vis.length) * barW + Math.max(0, vis.length - 1) * gap;
  const left = 54, t = 16, h = 240, bottom = grouped ? 100 : 84;
  let width = left + 16;
  for (const g of groups) width += g.items.length * (cluster + itemGap) + groupGap;
  width = Math.max(width, 640);
  let s = "<div class='legend'>" + series.map(se => {
    const off = isHidden(id, se.key);
    return `<label class='lg${off ? ' off' : ''}'><input type='checkbox' ${off ? '' : 'checked'} onchange="toggle('${id}','${se.key}')">` +
      `<span class='sw' style='background:${se.color}'></span>${esc(se.label)}</label>`;
  }).join('') + `<span class='muted'>y-axis 0–${Math.round(top * 100)}%</span></div>`;
  s += `<svg viewBox='0 0 ${width} ${t + h + bottom}' width='${width}' height='${t + h + bottom}'>`;
  for (let i = 0; i <= 5; i++) {
    const v = top * i / 5, y = t + h - (v / top) * h;
    const label = (v * 100).toFixed(top < 0.1 ? 1 : 0);
    s += `<line x1='${left}' y1='${y}' x2='${width - 8}' y2='${y}' class='grid'/>` +
      `<text x='${left - 6}' y='${y + 4}' text-anchor='end' class='tick'>${label}%</text>`;
  }
  let x = left;
  for (const g of groups) {
    const start = x;
    const gw = g.items.length * (cluster + itemGap) - itemGap;
    if (grouped) s += `<rect x='${start - 4}' y='${t + h + 62}' width='${gw + 8}' height='22' rx='4' fill='#f2f2f7'/>`;
    for (const it of g.items) {
      let bx = x;
      for (const se of vis) {
        const n = nOf(it, se.key);
        const v = n ? (it.values[se.key] || 0) : 0;
        const bh = Math.min(v / top, 1) * h;
        const where = (g.label ? g.label + ' · ' : '') + it.label + ' · ' + se.label;
        s += `<rect x='${bx}' y='${t + h - bh}' width='${barW}' height='${bh}' fill='${se.color}'>` +
          `<title>${esc(where)} ${(v * 100).toFixed(1)}% (n=${n})</title></rect>`;
        if (n && vis.length <= 2) s += `<text x='${bx + barW / 2}' y='${t + h - bh - 3}' text-anchor='middle' class='val'>${Math.round(v * 100)}</text>`;
        bx += barW + gap;
      }
      s += `<text transform='translate(${x + cluster / 2},${t + h + 14}) rotate(55)' class='tick'>${esc(it.label)}</text>`;
      x += cluster + itemGap;
    }
    if (grouped) s += `<text x='${(start + x - itemGap) / 2}' y='${t + h + 78}' text-anchor='middle' class='group'>${esc(g.label)}</text>`;
    x += groupGap;
  }
  el.innerHTML = s + '</svg>';
}

function flat(list, pick) {
  return [{label: '', items: list.map(i => ({label: i.label, n: i.n, values: pick(i)}))}];
}
function nestedFour(nested) {
  return nested.map(g => ({label: g.label, items: g.models.map(m => ({label: m.label, n: m.n, values: m.shares}))}));
}
function pairs(a, b, ka, kb) {
  return a.map((g, gi) => ({label: g.label, items: g.models.map((m, mi) => {
    const o = b[gi].models[mi];
    return {label: m.label, values: {[ka]: m.oa, [kb]: o.oa}, ns: {[ka]: m.n, [kb]: o.n}};
  })}));
}

function draw() {
  const d = DATA;
  chart('c-setting', flat(d.by_setting, i => i.shares), SERIES4);
  chart('c-harness', flat(d.by_harness, i => i.shares), SERIES4);
  chart('c-nested', nestedFour(d.nested), SERIES4);
  chart('c-harm-pair', pairs(d.harm_paired.with.nested, d.harm_paired.without.nested, 'with', 'without'), [
    {key: 'with', label: 'With direct harm', color: '#c2410c'},
    {key: 'without', label: 'Without direct harm', color: '#0f766e'},
  ]);
  chart('c-mal-pair', pairs(d.mal_paired.malicious.nested, d.mal_paired.non.nested, 'mal', 'non'), [
    {key: 'mal', label: 'Malicious', color: '#7c3aed'},
    {key: 'non', label: 'Non-malicious', color: '#0284c7'},
  ]);
  chart('c-domain', flat(d.domains, i => ({oa: i.oa})), OA);
}

async function refresh() {
  try {
    DATA = await (await fetch('/data.json', {cache: 'no-store'})).json();
    draw();
    const page = await (await fetch('/', {cache: 'no-store'})).text();
    const doc = new DOMParser().parseFromString(page, 'text/html');
    document.querySelectorAll('.swap[id]').forEach(el => {
      const fresh = doc.getElementById(el.id);
      if (fresh) el.innerHTML = fresh.innerHTML;
    });
  } catch (e) {}
}
draw();
setInterval(refresh, 20000);
"""


_MODEL_COLOR = {
    "gpt-5.6-sol": "#2e9e5b",
    "claude-sonnet-5": "#e07b39",
    "gemini-3.8-flash": "#d6457f",
    "glm-5.3": "#3b6fd4",
}
_HARNESS_SHAPE = {
    "OpenHands": "triangle",
    "OpenAI Sandbox": "circle",
    "Claude SDK": "square",
}


def _setting_style(setting: str) -> tuple[str, float]:
    """One hue per model; high is opaque, low is 60%."""
    model, effort = setting.rsplit("-", 1)
    return _MODEL_COLOR.get(model, "#555"), (1.0 if effort == "high" else 0.6)


def _marker(shape: str, x: float, y: float, color: str, opacity: float, title: str,
            size: float = 7) -> str:
    style = f"fill='{color}' fill-opacity='{opacity}' stroke='#1d1d1f' stroke-width='1.2'"
    tip = f"<title>{title}</title>"
    if shape == "circle":
        return f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{size}' {style}>{tip}</circle>"
    if shape == "square":
        s = size * 1.7
        return f"<rect x='{x - s / 2:.1f}' y='{y - s / 2:.1f}' width='{s:.1f}' height='{s:.1f}' {style}>{tip}</rect>"
    return (
        f"<polygon points='{x:.1f},{y - size * 1.15:.1f} {x - size:.1f},{y + size * 0.75:.1f} "
        f"{x + size:.1f},{y + size * 0.75:.1f}' {style}>{tip}</polygon>"
    )


def _axis(lo: float, hi: float, steps: tuple[float, ...]) -> tuple[float, float, float]:
    """A padded [lo, hi] snapped to a step that gives about five ticks."""
    span = max(hi - lo, 1e-9)
    lo, hi = lo - span * 0.12, hi + span * 0.12
    step = next((s for s in steps if (hi - lo) / s <= 6), steps[-1])
    lo = step * int(lo // step)
    hi = step * -int(-hi // step)
    return lo, hi, step


def _scatter(points: list[dict]) -> str:
    if not points:
        return "<p class='muted'>No judged runs yet.</p>"
    plot_w, plot_h = 380, 300
    left, top = 58, 14
    legend_x = left + plot_w + 28
    width, height = legend_x + 190, top + plot_h + 52
    ys = [p["y"] for p in points]
    xs = [p["x"] for p in points]
    y0, y1, ystep = _axis(max(0.0, min(ys)), max(ys), (0.02, 0.05, 0.1, 0.2, 0.25))
    y0 = max(0.0, y0)
    x0, x1, xstep = _axis(min(xs), max(xs), (0.25, 0.5, 1, 2, 5, 10))
    first_x = x0
    if min(xs) >= 0:
        first_x = max(x0, 0.0)
        x0 = first_x - (x1 - first_x) * 0.04
    sx = lambda v: left + (v - x0) / (x1 - x0) * plot_w
    sy = lambda v: top + plot_h - (v - y0) / (y1 - y0) * plot_h
    parts = [f"<svg class='scatter' viewBox='0 0 {width} {height}' width='{width}' height='{height}'>"]
    parts.append(f"<rect x='{left}' y='{top}' width='{plot_w}' height='{plot_h}' fill='none' stroke='#1d1d1f'/>")
    v = y0
    while v <= y1 + 1e-9:
        y = sy(v)
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/>")
        parts.append(f"<text x='{left - 7}' y='{y + 4:.1f}' text-anchor='end' class='lab'>{v * 100:.0f}</text>")
        v += ystep
    v = first_x
    while v <= x1 + 1e-9:
        x = sx(v)
        parts.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + plot_h}' class='grid'/>")
        label = f"{v:.1f}" if xstep < 1 else f"{v:.0f}"
        parts.append(f"<text x='{x:.1f}' y='{top + plot_h + 17}' text-anchor='middle' class='lab'>{label}</text>")
        v += xstep
    parts.append(
        f"<text x='{left + plot_w / 2:.0f}' y='{top + plot_h + 40}' text-anchor='middle' class='lab'>"
        "mean ask_user calls per run</text>"
    )
    parts.append(
        f"<text transform='translate(16,{top + plot_h / 2:.0f}) rotate(-90)' text-anchor='middle' class='lab'>"
        "out-of-authorization rate (%)</text>"
    )
    for point in points:
        color, alpha = _setting_style(point["setting_id"])
        title = (
            f"{point['harness']} · {point['setting']}: OA {_pct(point['y'])}, "
            f"mean ask_user {point['x']:.2f}, asked at least once {_pct(point['asked'])}, "
            f"n={point['n']}"
        )
        parts.append(_marker(
            _HARNESS_SHAPE.get(point["harness"], "circle"),
            sx(point["x"]), sy(point["y"]), color, alpha, _esc(title),
        ))
    ly = top + 10
    parts.append(f"<text x='{legend_x}' y='{ly}' class='lab' font-weight='700'>Harness</text>")
    for name, shape in _HARNESS_SHAPE.items():
        ly += 20
        parts.append(_marker(shape, legend_x + 8, ly - 4, "#ffffff", 1.0, name, size=6))
        parts.append(f"<text x='{legend_x + 22}' y='{ly}' class='lab'>{name}</text>")
    ly += 28
    parts.append(f"<text x='{legend_x}' y='{ly}' class='lab' font-weight='700'>Model setting</text>")
    for setting, label in SETTINGS:
        ly += 19
        color, alpha = _setting_style(setting)
        parts.append(
            f"<rect x='{legend_x + 2}' y='{ly - 11}' width='12' height='12' fill='{color}' "
            f"fill-opacity='{alpha}' stroke='#1d1d1f' stroke-width='1'/>"
        )
        parts.append(f"<text x='{legend_x + 22}' y='{ly}' class='lab'>{_esc(label)}</text>")
    parts.append("</svg>")
    return "".join(parts)


def _asking(items: list[dict]) -> str:
    rows = []
    for item in items:
        asked, never = item["asked"], item["never"]
        known = asked["n"] + never["n"]
        if not known:
            rows.append(f"<tr><td>{_esc(item['label'])}</td><td colspan='3' class='muted'>no trajectories</td></tr>")
            continue
        rows.append(
            f"<tr><td>{_esc(item['label'])}</td>"
            f"<td>{known} of {item['total']}</td>"
            f"<td>{_pct(asked['oa']) if asked['n'] else '—'} <span class='muted'>(n={asked['n']})</span></td>"
            f"<td>{_pct(never['oa']) if never['n'] else '—'} <span class='muted'>(n={never['n']})</span></td></tr>"
        )
    return (
        "<table class='asking'><tr><th>Harness</th><th>Runs with trajectory</th>"
        "<th>OA rate, asked at least once</th><th>OA rate, never asked</th></tr>"
        + "".join(rows) + "</table>"
    )


def _oa_line(stats: dict) -> str:
    if not stats["n"]:
        return "<p class='muted'>No judged runs yet.</p>"
    return f"<p class='oa'>Out-of-authorization rate <b>{_pct(stats['oa'])}</b> <span class='muted'>(n={stats['n']})</span></p>"


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render() -> str:
    data = _payload()
    overall = data["overall"]
    pair_with = data["harm_paired"]["with"]["overall"]
    pair_without = data["harm_paired"]["without"]["overall"]
    mal_pair = data["mal_paired"]["malicious"]["overall"]
    non_pair = data["mal_paired"]["non"]["overall"]
    blob = json.dumps(data).replace("</", "<\\/")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Authorization alignment</title><style>{_CSS}</style></head><body>"
        "<h1>Authorization alignment</h1>"
        f"<p class='muted swap' id='sw-summary'>How often an agent acting for the user stays inside the "
        f"assigned authorization scope. {data['n']} judged runs of {data['expected']} cells "
        f"({data['tasks']} tasks × 3 harnesses × 8 settings, including triggers n and o). "
        "Updates every 20 seconds without reloading. "
        "Each run falls in exactly one of four outcomes; out-of-authorization rate is the two OA outcomes "
        "combined. Untick a rate to hide it; the y-axis rescales to the rates still shown.</p>"
        "<h2>Overall</h2>"
        f"<div class='swap' id='sw-overall'>{_oa_line(overall)}{_stacked([('All judged runs', overall)])}</div>"
        "<h2>By model setting</h2>"
        f"<div class='swap' id='sw-setting'>{_stacked([(i['label'], i) for i in data['by_setting']])}</div>"
        "<div class='chart' id='c-setting'></div>"
        "<h2>By harness</h2>"
        f"<div class='swap' id='sw-harness'>{_stacked([(i['label'], i) for i in data['by_harness']])}</div>"
        "<div class='chart' id='c-harness'></div>"
        "<h2>Harness × model setting</h2><div class='chart' id='c-nested'></div>"
        "<h2>Direct AI-safety harm, paired</h2>"
        f"<p class='muted swap' id='sw-harm-pair-note'>{data['harm_paired']['pairs']} task pairs "
        "that share family, trigger, and domain and differ only by h or n. "
        "A run is counted only when its pair was also judged on the same harness and setting.</p>"
        f"<div class='swap' id='sw-harm-pair'>With direct harm {_oa_line(pair_with)}"
        f"Without direct harm {_oa_line(pair_without)}</div>"
        "<div class='chart' id='c-harm-pair'></div>"
        "<h2>Malicious trigger, paired</h2>"
        f"<p class='muted swap' id='sw-mal-pair-note'>{data['mal_paired']['pairs']} task pairs "
        "that share family, domain, and harm and differ only by a malicious trigger (n or o) "
        "versus one other trigger. "
        "A run is counted only when its pair was also judged on the same harness and setting.</p>"
        f"<div class='swap' id='sw-mal-pair'>Malicious triggers n and o {_oa_line(mal_pair)}"
        f"Non-malicious triggers {_oa_line(non_pair)}</div>"
        "<div class='chart' id='c-mal-pair'></div>"
        "<h2>By domain</h2><div class='chart' id='c-domain'></div>"
        "<h2>Asking the user and out-of-authorization</h2>"
        "<p class='muted'>Counts ask_user calls, the step where the agent asks the user and waits for "
        "the reply, anywhere in the run. Narration between tool calls is not counted. A trajectory is "
        "counted only when its duration matches the judged report. outputs/ is read first. If that file "
        "is missing, or it is a different run that reused the folder name, the same relative path under "
        "outputs_traj_raio is used instead.</p>"
        f"<div class='swap' id='sw-asking'>{_asking(data['asking'])}</div>"
        "<p class='muted'>Each point is one harness and one model setting. Horizontal position is the mean "
        "number of ask_user calls per run; vertical position is its out-of-authorization rate. "
        "Hover a point for the exact numbers.</p>"
        f"<div class='swap' id='sw-scatter'>{_scatter(data['points'])}</div>"
        f"<script id='board-data' type='application/json'>{blob}</script>"
        f"<script>{_JS}</script></body></html>"
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/data.json"):
            body = json.dumps(_payload()).encode("utf-8")
            ctype = "application/json"
        else:
            body = render().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"alignment board: http://localhost:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
