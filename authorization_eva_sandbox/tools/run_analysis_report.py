#!/usr/bin/env python3
"""Generate descriptive and comparison analysis outputs from results CSV."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import re
import sys
from pathlib import Path
from typing import Any

# Allow running this file directly without installing the package:
# tools/analysis_feature_utils.py lives next to this script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from analysis_feature_utils import (  # noqa: E402
    compute_cost_metrics_from_path,
    derive_cost_path,
    resolve_output_file_to_abs,
)


NUMERIC_FIELDS = [
    "time_spent",
    "turns_total",
    "user_message_count",
    "agent_think_tokens",
    "reasoning_tokens",  # server-reported model reasoning, see reasoning_token_count.md
]
PLACEHOLDER_PATTERN = re.compile(r"^\{[^{}]+\}$")
DOMAIN_CANONICAL_MAP = {
    "healthcare": "healthcare_medical",
    "heathcare": "healthcare_medical",
}


def infer_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    repo_root = infer_repo_root()
    default_csv = (
        repo_root
        / "authorization_eva_sandbox"
        / "evaluation"
        / "outputs"
        / "batch_status_dual_models_all_models.csv"
    )
    default_out_dir = (
        repo_root
        / "authorization_eva_sandbox"
        / "evaluation"
        / "outputs"
        / "analysis"
    )
    p = argparse.ArgumentParser(description="Generate analysis summaries from evaluation CSV")
    p.add_argument("--input-csv", type=Path, default=default_csv)
    p.add_argument("--output-dir", type=Path, default=default_out_dir)
    return p


def parse_float(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_yes(v: Any) -> bool:
    return str(v or "").strip().lower() == "yes"


def is_placeholder(v: Any) -> bool:
    s = str(v or "").strip()
    if not s:
        return True
    return bool(PLACEHOLDER_PATTERN.match(s))


def infer_harm_from_task(task_id: str) -> str:
    tid = (task_id or "").strip().lower()
    if tid.endswith("-h"):
        return "Y"
    if tid.endswith("-n"):
        return "N"
    return ""


def _normalize_trigger(v: Any) -> str:
    if isinstance(v, list):
        parts = [str(x).strip() for x in v if str(x).strip()]
        return " | ".join(parts)
    return str(v or "").strip()


def canonicalize_domain(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    return DOMAIN_CANONICAL_MAP.get(s.lower(), s)


def load_vignette_meta(task_id: str, repo_root: Path, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    tid = (task_id or "").strip()
    if not tid:
        return {"domain": "", "harm": "", "situational_trigger": ""}
    if tid in cache:
        return cache[tid]

    sandbox_root = repo_root / "authorization_eva_sandbox"
    task_cfg = sandbox_root / "tasks" / tid / "task_config.json"
    vignette_id = ""
    if task_cfg.exists():
        try:
            tc = json.loads(task_cfg.read_text(encoding="utf-8"))
            vignette_id = str(tc.get("source_vignette") or "").strip()
        except Exception:
            vignette_id = ""
    if not vignette_id and tid.startswith("T"):
        vignette_id = f"V{tid[1:]}"

    meta = {"domain": "", "harm": "", "situational_trigger": ""}
    vignette_path = sandbox_root / "vignettes" / f"{vignette_id}.json"
    if vignette_path.exists():
        try:
            vj = json.loads(vignette_path.read_text(encoding="utf-8"))
            meta["domain"] = canonicalize_domain(vj.get("domain"))
            meta["harm"] = str(vj.get("harm") or "").strip()
            meta["situational_trigger"] = _normalize_trigger(vj.get("situational_trigger"))
        except Exception:
            pass

    cache[tid] = meta
    return meta


def _backfill_reasoning_tokens(row: dict[str, Any], repo_root: Path) -> None:
    """If the row's `reasoning_tokens` cell is missing/blank, try to read the
    matching `cost_<task>.json` (sibling of the trajectory file) and fill it.

    Older CSVs predate Patch 1-4 and don't have the column; cost files written
    after the patch carry `combined.reasoning_tokens`. Pre-patch runs are left
    blank with a `cost_metrics_status` so they can be distinguished from
    legitimate zeros.
    """
    existing = str(row.get("reasoning_tokens", "") or "").strip()
    if existing != "":
        return  # caller already has a value, don't clobber

    traj_rel = str(row.get("output_file", "") or "")
    if not traj_rel:
        row.setdefault("cost_metrics_status", "missing_output_file")
        return
    traj_abs = resolve_output_file_to_abs(traj_rel, repo_root)
    cost_abs = derive_cost_path(traj_abs)
    metrics = compute_cost_metrics_from_path(cost_abs)
    # only fill if we got a real number; otherwise leave blank but record status
    rt = metrics.get("reasoning_tokens")
    if isinstance(rt, (int, float)):
        row["reasoning_tokens"] = rt
    row.setdefault("cost_metrics_status", metrics.get("cost_metrics_status", ""))


def normalize_row_metadata(rows: list[dict[str, Any]], repo_root: Path) -> list[dict[str, Any]]:
    cache: dict[str, dict[str, str]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        task_id = str(r.get("task", "") or "")
        vm = load_vignette_meta(task_id, repo_root=repo_root, cache=cache)

        if is_placeholder(r.get("harm")):
            r["harm"] = vm.get("harm") or infer_harm_from_task(task_id)
        if is_placeholder(r.get("situational_trigger")):
            r["situational_trigger"] = vm.get("situational_trigger", "")
        if is_placeholder(r.get("domain")):
            r["domain"] = vm.get("domain", "")
        r["domain"] = canonicalize_domain(r.get("domain"))

        _backfill_reasoning_tokens(r, repo_root)

        out.append(r)
    return out


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return math.nan
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * p
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - idx) + sorted_vals[hi] * (idx - lo)


def summarize_numeric(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "std": None,
            "median": None,
            "iqr": None,
            "min": None,
            "max": None,
        }
    vals = sorted(values)
    q1 = percentile(vals, 0.25)
    q3 = percentile(vals, 0.75)
    return {
        "count": len(vals),
        "mean": statistics.fmean(vals),
        "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        "median": statistics.median(vals),
        "iqr": q3 - q1,
        "min": vals[0],
        "max": vals[-1],
    }


def rate(num: int, den: int) -> float | None:
    if den == 0:
        return None
    return num / den


def value_counts(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(field, "") or "")
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def trigger_value_counts(rows: list[dict[str, Any]], field: str = "situational_trigger") -> dict[str, int]:
    """Count situational triggers after splitting multi-trigger entries by '|'. """
    out: dict[str, int] = {}
    for r in rows:
        raw = str(r.get(field, "") or "")
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        if not parts and raw.strip():
            parts = [raw.strip()]
        for p in parts:
            out[p] = out.get(p, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def compute_outcome_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    completion_yes = sum(1 for r in rows if parse_yes(r.get("completion")))
    violation_yes = sum(1 for r in rows if parse_yes(r.get("violation")))
    comp_yes_and_violation_yes = sum(
        1 for r in rows if parse_yes(r.get("completion")) and parse_yes(r.get("violation"))
    )

    metrics: dict[str, Any] = {
        "n": n,
        "completion_yes_rate": rate(completion_yes, n),
        "violation_yes_rate": rate(violation_yes, n),
        "completion_yes_and_violation_yes_rate": rate(comp_yes_and_violation_yes, n),
        "violation_rate_given_completion_yes": rate(comp_yes_and_violation_yes, completion_yes),
    }
    for f in NUMERIC_FIELDS:
        vals = [parse_float(r.get(f)) for r in rows]
        vals = [v for v in vals if v is not None]
        metrics[f] = summarize_numeric(vals)
    return metrics


def group_by(rows: list[dict[str, Any]], field: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = str(r.get(field, "") or "")
        out.setdefault(key, []).append(r)
    return out


def unique_tasks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one row per task for task-side descriptive stats."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        task = str(r.get("task", "") or "")
        if not task or task in seen:
            continue
        seen.add(task)
        out.append(r)
    return out


def flatten_dict(prefix: str, obj: dict[str, Any], rows_out: list[dict[str, Any]]) -> None:
    for k, v in obj.items():
        new_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flatten_dict(new_key, v, rows_out)
        else:
            rows_out.append({"metric": new_key, "value": v})


def numeric_violation_association(
    rows: list[dict[str, Any]], field: str
) -> dict[str, Any]:
    """Generic association: bin a numeric per-run feature into quartiles and
    report violation_yes_rate per bin, plus mean of the feature split by
    violation outcome. Used for both `turns_total` and `reasoning_tokens`.
    """
    pairs: list[tuple[float, int]] = []
    for r in rows:
        t = parse_float(r.get(field))
        if t is None:
            continue
        pairs.append((t, 1 if parse_yes(r.get("violation")) else 0))
    if not pairs:
        return {
            "n": 0,
            f"mean_{field}_violation_yes": None,
            f"mean_{field}_violation_no": None,
            "bins": [],
        }

    feat_yes = [t for t, v in pairs if v == 1]
    feat_no = [t for t, v in pairs if v == 0]
    sorted_vals = sorted(t for t, _ in pairs)
    q1 = percentile(sorted_vals, 0.25)
    q2 = percentile(sorted_vals, 0.50)
    q3 = percentile(sorted_vals, 0.75)
    bins = [
        ("q1", lambda x: x <= q1),
        ("q2", lambda x: q1 < x <= q2),
        ("q3", lambda x: q2 < x <= q3),
        ("q4", lambda x: x > q3),
    ]
    out_bins = []
    for name, fn in bins:
        subset = [(t, v) for t, v in pairs if fn(t)]
        if not subset:
            continue
        vio = sum(v for _, v in subset)
        out_bins.append(
            {"bin": name, "n": len(subset), "violation_yes_rate": rate(vio, len(subset))}
        )
    return {
        "n": len(pairs),
        f"mean_{field}_violation_yes": statistics.fmean(feat_yes) if feat_yes else None,
        f"mean_{field}_violation_no": statistics.fmean(feat_no) if feat_no else None,
        "bins": out_bins,
    }


def turns_violation_association(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs: list[tuple[float, int]] = []
    for r in rows:
        t = parse_float(r.get("turns_total"))
        if t is None:
            continue
        pairs.append((t, 1 if parse_yes(r.get("violation")) else 0))
    if not pairs:
        return {"n": 0, "mean_turns_violation_yes": None, "mean_turns_violation_no": None, "bins": []}

    turns_yes = [t for t, v in pairs if v == 1]
    turns_no = [t for t, v in pairs if v == 0]
    sorted_turns = sorted(t for t, _ in pairs)
    q1 = percentile(sorted_turns, 0.25)
    q2 = percentile(sorted_turns, 0.50)
    q3 = percentile(sorted_turns, 0.75)
    bins = [
        ("q1", lambda x: x <= q1),
        ("q2", lambda x: q1 < x <= q2),
        ("q3", lambda x: q2 < x <= q3),
        ("q4", lambda x: x > q3),
    ]
    out_bins = []
    for name, fn in bins:
        subset = [(t, v) for t, v in pairs if fn(t)]
        if not subset:
            continue
        vio = sum(v for _, v in subset)
        out_bins.append(
            {"bin": name, "n": len(subset), "violation_yes_rate": rate(vio, len(subset))}
        )
    return {
        "n": len(pairs),
        "mean_turns_violation_yes": statistics.fmean(turns_yes) if turns_yes else None,
        "mean_turns_violation_no": statistics.fmean(turns_no) if turns_no else None,
        "bins": out_bins,
    }


def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_flat_csv(path: Path, section_obj: dict[str, Any]) -> None:
    rows_out: list[dict[str, Any]] = []
    flatten_dict("", section_obj, rows_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value"])
        w.writeheader()
        w.writerows(rows_out)


def _display_key(v: Any) -> str:
    s = str(v or "").strip()
    return s if s else "(empty)"


def _fmt_num(v: Any, digits: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.{digits}f}" if isinstance(v, float) else str(v)
    return str(v)


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v) * 100:.2f}%"
    except Exception:
        return str(v)


def _render_distribution_block(title: str, counts: dict[str, int]) -> str:
    total = sum(int(v) for v in counts.values()) if counts else 0
    if not counts:
        return f"<section><h3>{html.escape(title)}</h3><p>No data.</p></section>"
    rows = []
    for k, c in counts.items():
        key = html.escape(_display_key(k))
        count = int(c)
        pct = (count / total * 100.0) if total else 0.0
        rows.append(
            f"""
            <div class="bar-row">
              <div class="bar-label">{key}</div>
              <div class="bar-track"><div class="bar-fill" style="width:{pct:.2f}%"></div></div>
              <div class="bar-val">{count} ({pct:.2f}%)</div>
            </div>
            """
        )
    return f"""
    <section class="panel">
      <h3>{html.escape(title)} <span class="sub">(n={total})</span></h3>
      {''.join(rows)}
    </section>
    """


def _collapse_trigger_to_binary(counts: dict[str, int]) -> dict[str, int]:
    malicious = 0
    other = 0
    for k, v in counts.items():
        key = str(k or "").strip().lower()
        count = int(v)
        if "malicious instruction" in key:
            malicious += count
        else:
            other += count
    return {
        "Contains malicious instruction": malicious,
        "Other triggers": other,
    }


def write_descriptive_dashboard(path: Path, descriptive: dict[str, Any]) -> None:
    task_side = descriptive.get("task_side", {})
    result_side = descriptive.get("result_side", {})
    metrics = result_side.get("metrics", {})

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">Unique Tasks</div><div class="v">{_fmt_num(task_side.get("n_unique_tasks"), 0)}</div></div>
      <div class="card"><div class="k">Runs (n)</div><div class="v">{_fmt_num(metrics.get("n"), 0)}</div></div>
      <div class="card"><div class="k">Completion Yes Rate</div><div class="v">{_fmt_pct(metrics.get("completion_yes_rate"))}</div></div>
      <div class="card"><div class="k">Violation Yes Rate</div><div class="v">{_fmt_pct(metrics.get("violation_yes_rate"))}</div></div>
      <div class="card"><div class="k">Completion=Yes & Violation=Yes</div><div class="v">{_fmt_pct(metrics.get("completion_yes_and_violation_yes_rate"))}</div></div>
      <div class="card"><div class="k">Violation Rate | Completion=Yes</div><div class="v">{_fmt_pct(metrics.get("violation_rate_given_completion_yes"))}</div></div>
    </div>
    """

    numeric_rows = []
    for field in NUMERIC_FIELDS:
        s = metrics.get(field, {}) or {}
        numeric_rows.append(
            f"""
            <tr>
              <td>{html.escape(field)}</td>
              <td>{_fmt_num(s.get("count"), 0)}</td>
              <td>{_fmt_num(s.get("mean"))}</td>
              <td>{_fmt_num(s.get("std"))}</td>
              <td>{_fmt_num(s.get("median"))}</td>
              <td>{_fmt_num(s.get("iqr"))}</td>
              <td>{_fmt_num(s.get("min"))}</td>
              <td>{_fmt_num(s.get("max"))}</td>
            </tr>
            """
        )

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Descriptive Summary Dashboard</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; margin: 24px; color: #111827; }}
    h1 {{ margin: 0 0 12px; }}
    h2 {{ margin: 24px 0 10px; }}
    h3 {{ margin: 0 0 10px; font-size: 16px; }}
    .sub {{ color: #6b7280; font-weight: 400; font-size: 12px; }}
    .panel {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin: 12px 0 20px; }}
    .card {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 10px 12px; }}
    .card .k {{ color: #6b7280; font-size: 12px; margin-bottom: 6px; }}
    .card .v {{ font-size: 20px; font-weight: 700; }}
    .bar-row {{ display: grid; grid-template-columns: minmax(200px, 1fr) 2fr 130px; gap: 10px; align-items: center; margin: 8px 0; }}
    .bar-label {{ font-size: 12px; overflow-wrap: anywhere; }}
    .bar-track {{ background: #f3f4f6; border-radius: 999px; height: 10px; overflow: hidden; }}
    .bar-fill {{ background: #3b82f6; height: 100%; }}
    .bar-val {{ text-align: right; font-size: 12px; color: #374151; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border: 1px solid #e5e7eb; padding: 6px 8px; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #f9fafb; }}
  </style>
</head>
<body>
  <h1>Descriptive Summary Dashboard</h1>
  <p>Auto-generated from descriptive summary JSON.</p>
  {cards}

  <h2>Task-Side Distributions</h2>
  {_render_distribution_block("Domain Distribution", task_side.get("domain_distribution", {}))}
  {_render_distribution_block("Harm Distribution", task_side.get("harm_distribution", {}))}
  {_render_distribution_block("Situational Trigger (Binary)", _collapse_trigger_to_binary(task_side.get("situational_trigger_distribution", {})))}

  <h2>Result-Side Distributions</h2>
  {_render_distribution_block("Completion Distribution", result_side.get("completion_distribution", {}))}
  {_render_distribution_block("Violation Distribution", result_side.get("violation_distribution", {}))}

  <h2>Result-Side Numeric Metrics</h2>
  <section class="panel">
    <table>
      <thead>
        <tr>
          <th>metric</th><th>count</th><th>mean</th><th>std</th><th>median</th><th>iqr</th><th>min</th><th>max</th>
        </tr>
      </thead>
      <tbody>
        {''.join(numeric_rows)}
      </tbody>
    </table>
  </section>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")


def _render_metrics_table(title: str, grouped_metrics: dict[str, dict[str, Any]]) -> str:
    if not grouped_metrics:
        return f"<section class='panel'><h3>{html.escape(title)}</h3><p>No data.</p></section>"
    rows = []
    for key, m in grouped_metrics.items():
        rows.append(
            f"""
            <tr>
              <td>{html.escape(_display_key(key))}</td>
              <td>{_fmt_num(m.get("n"), 0)}</td>
              <td>{_fmt_pct(m.get("completion_yes_rate"))}</td>
              <td>{_fmt_pct(m.get("violation_yes_rate"))}</td>
              <td>{_fmt_pct(m.get("completion_yes_and_violation_yes_rate"))}</td>
              <td>{_fmt_pct(m.get("violation_rate_given_completion_yes"))}</td>
              <td>{_fmt_num((m.get("time_spent") or {}).get("mean"))}</td>
              <td>{_fmt_num((m.get("turns_total") or {}).get("mean"))}</td>
              <td>{_fmt_num((m.get("agent_think_tokens") or {}).get("mean"))}</td>
              <td>{_fmt_num((m.get("reasoning_tokens") or {}).get("mean"))}</td>
            </tr>
            """
        )
    return f"""
    <section class="panel">
      <h3>{html.escape(title)}</h3>
      <table>
        <thead>
          <tr>
            <th>group</th><th>n</th><th>completion_yes_rate</th><th>violation_yes_rate</th>
            <th>completion_yes_and_violation_yes_rate</th><th>violation_rate_given_completion_yes</th>
            <th>time_spent_mean</th><th>turns_mean</th><th>agent_think_tokens_mean</th>
            <th>reasoning_tokens_mean</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def _render_numeric_vs_violation(
    title: str, assoc: dict[str, Any], field: str
) -> str:
    """Generic renderer for {field}_vs_violation associations (turns, reasoning,
    etc.). Looks up `mean_{field}_violation_yes` / `mean_{field}_violation_no`
    keys produced by `numeric_violation_association`.
    """
    bins = assoc.get("bins", []) or []
    if not bins:
        return f"<section class='panel'><h3>{html.escape(title)}</h3><p>No data.</p></section>"
    rows = []
    for b in bins:
        rows.append(
            f"""
            <tr>
              <td>{html.escape(str(b.get("bin", "")))}</td>
              <td>{_fmt_num(b.get("n"), 0)}</td>
              <td>{_fmt_pct(b.get("violation_yes_rate"))}</td>
            </tr>
            """
        )
    return f"""
    <section class="panel">
      <h3>{html.escape(title)}</h3>
      <div class="mini-grid">
        <div><span class="k">n</span><span class="v">{_fmt_num(assoc.get("n"), 0)}</span></div>
        <div><span class="k">mean_{html.escape(field)}_violation_yes</span><span class="v">{_fmt_num(assoc.get(f"mean_{field}_violation_yes"))}</span></div>
        <div><span class="k">mean_{html.escape(field)}_violation_no</span><span class="v">{_fmt_num(assoc.get(f"mean_{field}_violation_no"))}</span></div>
      </div>
      <table>
        <thead><tr><th>{html.escape(field)}_bin</th><th>n</th><th>violation_yes_rate</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def _render_reasoning_vs_violation_by_model(
    by_model: dict[str, dict[str, Any]]
) -> str:
    if not by_model:
        return ""
    sections = []
    for model, assoc in by_model.items():
        if not assoc or not assoc.get("bins"):
            continue
        sections.append(
            _render_numeric_vs_violation(
                f"Reasoning Tokens vs Violation — {model}",
                assoc,
                "reasoning_tokens",
            )
        )
    if not sections:
        return ""
    return f"""
    <h2>Reasoning Tokens vs Violation (per model)</h2>
    {''.join(sections)}
    """


def _render_turns_vs_violation(turns_assoc: dict[str, Any]) -> str:
    bins = turns_assoc.get("bins", []) or []
    if not bins:
        return "<section class='panel'><h3>Turns vs Violation</h3><p>No data.</p></section>"
    rows = []
    for b in bins:
        rows.append(
            f"""
            <tr>
              <td>{html.escape(str(b.get("bin", "")))}</td>
              <td>{_fmt_num(b.get("n"), 0)}</td>
              <td>{_fmt_pct(b.get("violation_yes_rate"))}</td>
            </tr>
            """
        )
    return f"""
    <section class="panel">
      <h3>Turns vs Violation</h3>
      <div class="mini-grid">
        <div><span class="k">n</span><span class="v">{_fmt_num(turns_assoc.get("n"), 0)}</span></div>
        <div><span class="k">mean_turns_violation_yes</span><span class="v">{_fmt_num(turns_assoc.get("mean_turns_violation_yes"))}</span></div>
        <div><span class="k">mean_turns_violation_no</span><span class="v">{_fmt_num(turns_assoc.get("mean_turns_violation_no"))}</span></div>
      </div>
      <table>
        <thead><tr><th>turn_bin</th><th>n</th><th>violation_yes_rate</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def _flatten_model_slice(model_slice: dict[str, dict[str, dict[str, Any]]], slice_name: str) -> str:
    rows = []
    for model, slice_map in model_slice.items():
        for slice_key, m in slice_map.items():
            rows.append(
                f"""
                <tr>
                  <td>{html.escape(_display_key(model))}</td>
                  <td>{html.escape(_display_key(slice_key))}</td>
                  <td>{_fmt_num(m.get("n"), 0)}</td>
                  <td>{_fmt_pct(m.get("completion_yes_rate"))}</td>
                  <td>{_fmt_pct(m.get("violation_yes_rate"))}</td>
                  <td>{_fmt_pct(m.get("completion_yes_and_violation_yes_rate"))}</td>
                  <td>{_fmt_num((m.get("turns_total") or {}).get("mean"))}</td>
                </tr>
                """
            )
    if not rows:
        return f"<section class='panel'><h3>Model x {html.escape(slice_name)}</h3><p>No data.</p></section>"
    return f"""
    <section class="panel">
      <h3>Model x {html.escape(slice_name)}</h3>
      <table>
        <thead>
          <tr>
            <th>model</th><th>{html.escape(slice_name)}</th><th>n</th>
            <th>completion_yes_rate</th><th>violation_yes_rate</th>
            <th>completion_yes_and_violation_yes_rate</th><th>turns_mean</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def _render_model_violation_chart(model_overall: dict[str, dict[str, Any]]) -> str:
    if not model_overall:
        return "<section class='panel'><h3>By Model Violation Rates</h3><p>No data.</p></section>"

    groups = []
    for model, m in model_overall.items():
        v1 = m.get("violation_yes_rate")
        v2 = m.get("violation_rate_given_completion_yes")
        h1 = max(0.0, min(100.0, float(v1) * 100.0)) if v1 is not None else 0.0
        h2 = max(0.0, min(100.0, float(v2) * 100.0)) if v2 is not None else 0.0
        groups.append(
            f"""
            <div class="group">
              <div class="bars">
                <div class="bar bar-a" style="height:{h1:.2f}%"><span>{_fmt_pct(v1)}</span></div>
                <div class="bar bar-b" style="height:{h2:.2f}%"><span>{_fmt_pct(v2)}</span></div>
              </div>
              <div class="xlab">{html.escape(_display_key(model))}</div>
            </div>
            """
        )

    return f"""
    <section class="panel">
      <h3>By Model: Violation Rates (Grouped Bars)</h3>
      <div class="legend">
        <span><i class="dot dot-a"></i> violation_yes_rate</span>
        <span><i class="dot dot-b"></i> violation_rate_given_completion_yes</span>
      </div>
      <div class="grouped-chart">
        {''.join(groups)}
      </div>
    </section>
    """


def write_comparison_dashboard(path: Path, comparison: dict[str, Any]) -> None:
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Comparison Summary Dashboard</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; margin: 24px; color: #111827; }}
    h1 {{ margin: 0 0 12px; }}
    h2 {{ margin: 24px 0 10px; }}
    h3 {{ margin: 0 0 10px; font-size: 16px; }}
    .panel {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border: 1px solid #e5e7eb; padding: 6px 8px; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    th {{ background: #f9fafb; }}
    .mini-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin: 8px 0 12px; }}
    .mini-grid > div {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px 10px; }}
    .mini-grid .k {{ display: block; font-size: 12px; color: #6b7280; }}
    .mini-grid .v {{ display: block; font-size: 18px; font-weight: 700; }}
    .legend {{ display: flex; gap: 14px; font-size: 12px; color: #374151; margin-bottom: 8px; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; }}
    .dot-a {{ background: #2563eb; }}
    .dot-b {{ background: #f59e0b; }}
    .grouped-chart {{ display: flex; gap: 16px; align-items: flex-end; min-height: 240px; border-left: 1px solid #d1d5db; border-bottom: 1px solid #d1d5db; padding: 8px 8px 0 8px; overflow-x: auto; }}
    .group {{ min-width: 140px; display: flex; flex-direction: column; align-items: center; }}
    .bars {{ height: 200px; width: 90px; display: flex; align-items: flex-end; gap: 10px; }}
    .bar {{ width: 36px; min-height: 2px; border-radius: 6px 6px 0 0; position: relative; }}
    .bar-a {{ background: #2563eb; }}
    .bar-b {{ background: #f59e0b; }}
    .bar span {{ position: absolute; top: -18px; left: 50%; transform: translateX(-50%); font-size: 11px; white-space: nowrap; color: #374151; }}
    .xlab {{ font-size: 12px; margin-top: 8px; text-align: center; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <h1>Comparison Summary Dashboard</h1>
  <p>Auto-generated from comparison summary JSON.</p>

  <h2>Overall Comparisons</h2>
  {_render_model_violation_chart(comparison.get("model_overall", {}))}
  {_render_metrics_table("By Model", comparison.get("model_overall", {}))}
  {_render_metrics_table("By Harm", comparison.get("harm_overall", {}))}
  {_render_metrics_table("By Domain", comparison.get("domain_overall", {}))}

  <h2>Turn-Level Association</h2>
  {_render_turns_vs_violation(comparison.get("turns_vs_violation", {}))}

  <h2>Reasoning Tokens vs Violation (pooled)</h2>
  {_render_numeric_vs_violation("Reasoning Tokens vs Violation (all rows)", comparison.get("reasoning_tokens_vs_violation", {}), "reasoning_tokens")}
  {_render_reasoning_vs_violation_by_model(comparison.get("reasoning_tokens_vs_violation_by_model", {}))}

  <h2>Interaction Slices</h2>
  {_flatten_model_slice(comparison.get("model_x_harm", {}), "harm")}
  {_flatten_model_slice(comparison.get("model_x_domain", {}), "domain")}
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    if not args.input_csv.exists():
        raise SystemExit(f"input csv not found: {args.input_csv}")

    with args.input_csv.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    rows = normalize_row_metadata(rows, repo_root=infer_repo_root())

    task_rows = unique_tasks(rows)
    descriptive = {
        "task_side": {
            "n_unique_tasks": len(task_rows),
            "domain_distribution": value_counts(task_rows, "domain"),
            "harm_distribution": value_counts(task_rows, "harm"),
            "situational_trigger_distribution": trigger_value_counts(task_rows, "situational_trigger"),
        },
        "result_side": {
            "completion_distribution": value_counts(rows, "completion"),
            "violation_distribution": value_counts(rows, "violation"),
            "metrics": compute_outcome_metrics(rows),
        },
    }

    by_model = {k: compute_outcome_metrics(v) for k, v in group_by(rows, "model").items()}
    by_harm = {k: compute_outcome_metrics(v) for k, v in group_by(rows, "harm").items()}
    by_domain = {k: compute_outcome_metrics(v) for k, v in group_by(rows, "domain").items()}

    model_harm: dict[str, dict[str, Any]] = {}
    model_domain: dict[str, dict[str, Any]] = {}
    for model, model_rows in group_by(rows, "model").items():
        model_harm[model] = {k: compute_outcome_metrics(v) for k, v in group_by(model_rows, "harm").items()}
        model_domain[model] = {k: compute_outcome_metrics(v) for k, v in group_by(model_rows, "domain").items()}

    comparison = {
        "model_overall": by_model,
        "harm_overall": by_harm,
        "domain_overall": by_domain,
        "turns_vs_violation": turns_violation_association(rows),
        # NEW: server-reported reasoning_tokens vs violation, both pooled and
        # per-model. Pooling across models is dominated by model differences,
        # so the per-model breakdown is the more interpretable signal.
        "reasoning_tokens_vs_violation": numeric_violation_association(
            rows, "reasoning_tokens"
        ),
        "reasoning_tokens_vs_violation_by_model": {
            model: numeric_violation_association(model_rows, "reasoning_tokens")
            for model, model_rows in group_by(rows, "model").items()
        },
        "model_x_harm": model_harm,
        "model_x_domain": model_domain,
    }

    out_dir = args.output_dir
    write_json(out_dir / "descriptive_summary.json", descriptive)
    write_json(out_dir / "comparison_summary.json", comparison)
    write_flat_csv(out_dir / "descriptive_summary.csv", descriptive)
    write_flat_csv(out_dir / "comparison_summary.csv", comparison)
    write_descriptive_dashboard(out_dir / "descriptive_dashboard.html", descriptive)
    write_comparison_dashboard(out_dir / "comparison_dashboard.html", comparison)

    print(f"[analysis] rows={len(rows)} output_dir={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
