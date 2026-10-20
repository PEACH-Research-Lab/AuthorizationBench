#!/usr/bin/env python
"""Every number, table, and figure in the paper's results section.

    au312/bin/python -m authorization_eva_sandbox.evaluation.analysis.paper_stats

Reads the judged reports the alignment board reads (latest run per harness,
setting, and task; inconclusive judgments skipped) and the matching
trajectories, writes one row per run to ``paper/analysis/runs.csv``, then
computes from that file only:

- ``paper/analysis/numbers.json``: every statistic, keyed by finding
- ``paper/analysis/summary.md``: the same, readable
- ``paper/analysis/tables/*.tex``: tables for the paper
- ``paper/figures/results_*.pdf``: figures for the paper

``--from-snapshot`` skips the report scan and recomputes from runs.csv.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from itertools import combinations
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import statsmodels.api as sm  # noqa: E402
import statsmodels.formula.api as smf  # noqa: E402
from scipy import stats  # noqa: E402
from statsmodels.stats.multitest import multipletests  # noqa: E402
from statsmodels.stats.proportion import proportion_confint  # noqa: E402

from authorization_eva_sandbox.evaluation import alignment_board as board  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
PAPER = REPO / "paper"
OUT = PAPER / "analysis"
FIGURES = PAPER / "figures"
TASKS = board.SANDBOX / "tasks"

HARNESS_LABEL = {"openhands-native": "OpenHands", "openai_sandbox": "OpenAI SDK", "claude_agent": "Claude SDK"}
MODELS = ("gpt-5.6-sol", "claude-sonnet-5", "gemini-3.8-flash", "glm-5.3")
MODEL_LABEL = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "claude-sonnet-5": "Claude Sonnet 5",
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "glm-5.3": "GLM 5.3",
}
MODEL_SHORT = {"gpt-5.6-sol": "GPT-5.6", "claude-sonnet-5": "Sonnet 5",
               "gemini-3.8-flash": "Gemini 3.8", "glm-5.3": "GLM 5.3"}
HARNESS_SHORT = HARNESS_LABEL
INSTRUCTION_TRIGGERS = {"a", "b", "c", "d", "h"}
_STEP = re.compile(r"\bt=(\d{3})\b", re.I)


# ---------------------------------------------------------------- snapshot

def _trajectory(data: dict) -> list[dict] | None:
    """Steps of the judged run, from outputs/ or the other machine's copy."""
    raw = data.get("traj_path")
    rel = board._outputs_rel(raw) if raw else None
    if rel is None:
        return None
    for root in (board.OUTPUTS, board.RAIO_OUTPUTS):
        path = root / rel
        if not path.is_file():
            continue
        try:
            traj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if traj.get("duration_seconds") == data.get("duration_seconds"):
            return traj.get("trajectory") or []
    return None


def _cited(reasoning: str | None) -> list[int]:
    return sorted({int(m.group(1)) for m in _STEP.finditer(reasoning or "")})


def _step(entry: dict) -> int:
    try:
        return int(entry.get("step", 0))
    except (TypeError, ValueError):
        return 0


def _instruction(task: str) -> str:
    vignette = json.loads((TASKS / task / "vignette.json").read_text(encoding="utf-8"))
    return (vignette.get("instruction") or "").strip()


def build_snapshot() -> pd.DataFrame:
    rows = []
    for harness, _title in board.HARNESSES:
        for setting, _label in board.SETTINGS:
            folder = board.REPORTS / harness / setting
            if not folder.is_dir():
                continue
            for task in board._tasks():
                data = board._latest_report(folder, task, setting)
                if data is None:
                    continue
                family, trigger, domain, harm = board._TASK.match(task).groups()
                violation = data["violation_answer"] == "yes"
                completion = data["completion_answer"] == "yes"
                traj = _trajectory(data)
                asks = asks_pre = asks_after_violation = None
                completion_step = max(_cited(data.get("completion_reasoning")), default=None) if completion else None
                violation_step = min(_cited(data.get("violation_reasoning")), default=None) if violation else None
                if traj is not None:
                    ask_steps = [_step(s) for s in traj if s.get("type") == "ask_user"]
                    asks = len(ask_steps)
                    # The ask that delivers the result is the completion itself, not a question.
                    asks_pre = sum(1 for s in ask_steps if completion_step is None or s < completion_step)
                    if violation_step is not None:
                        asks_after_violation = sum(1 for s in ask_steps if s > violation_step)
                model, effort = setting.rsplit("-", 1)
                rows.append({
                    "task": task, "family": family, "trigger": trigger, "domain": domain,
                    "harm": harm == "h", "malicious": trigger in board.MALICIOUS,
                    "theme": "instruction" if trigger in INSTRUCTION_TRIGGERS else "environment",
                    "harness": harness, "setting": setting, "model": model, "effort": effort,
                    "violation": violation, "completion": completion,
                    "asks": asks, "asks_pre": asks_pre, "asks_after_violation": asks_after_violation,
                    "completion_step": completion_step, "violation_step": violation_step,
                    "reasoning_tokens": data.get("reasoning_tokens"),
                    "completion_tokens": data.get("completion_tokens"),
                    "duration": data.get("duration_seconds"),
                    "termination": data.get("termination_reason"),
                    "judge_fallback": bool(data.get("completion_judge_fallback") or data.get("violation_judge_fallback")),
                })
    df = pd.DataFrame(rows)
    df["cell"] = np.select(
        [df.violation & ~df.completion, df.violation & df.completion, ~df.violation & df.completion],
        ["oa_non", "oa_comp", "safe_comp"], "safe_non",
    )
    return df


# ---------------------------------------------------------------- helpers

def ci(k: int, n: int) -> list[float]:
    lo, hi = proportion_confint(k, n, method="wilson") if n else (float("nan"), float("nan"))
    return [round(100 * lo, 1), round(100 * hi, 1)]


def pct(k: float, n: float) -> float:
    return round(100 * k / n, 1) if n else float("nan")


def rates(d: pd.DataFrame) -> dict:
    n = len(d)
    out = {"n": n}
    for cell in board.CELLS:
        out[cell] = pct(int((d.cell == cell).sum()), n)
    out["oar"] = pct(int(d.violation.sum()), n)
    out["oar_ci"] = ci(int(d.violation.sum()), n)
    out["cr"] = pct(int(d.completion.sum()), n)
    return out


def mcnemar(a: np.ndarray, b: np.ndarray) -> dict:
    """a and b are matched booleans; side A is the one named first."""
    only_a = int((a & ~b).sum())
    only_b = int((~a & b).sum())
    n = len(a)
    p = stats.binomtest(only_a, only_a + only_b, 0.5).pvalue if only_a + only_b else 1.0
    return {"n": n, "a": pct(int(a.sum()), n), "b": pct(int(b.sum()), n),
            "diff": round(pct(int(a.sum()), n) - pct(int(b.sum()), n), 1),
            "only_a": only_a, "only_b": only_b, "p": float(p)}


def holm(results: dict[str, dict]) -> dict[str, dict]:
    keys = list(results)
    if keys:
        adjusted = multipletests([results[k]["p"] for k in keys], method="holm")[1]
        for key, value in zip(keys, adjusted):
            results[key]["p_holm"] = float(value)
    return results


def scenario_tests(pairs: pd.DataFrame) -> dict:
    """One A-minus-B difference per scenario pair, then sign and Wilcoxon tests."""
    diffs = pairs.groupby("pair").apply(lambda g: g.a.mean() - g.b.mean(), include_groups=False)
    pos, neg = int((diffs > 0).sum()), int((diffs < 0).sum())
    nonzero = diffs[diffs != 0]
    wilcoxon = stats.wilcoxon(nonzero).pvalue if len(nonzero) else 1.0
    return {"pairs": int(len(diffs)), "a_higher": pos, "b_higher": neg, "tie": int(len(diffs) - pos - neg),
            "mean_diff_pp": round(100 * float(diffs.mean()), 2),
            "sign_p": float(stats.binomtest(pos, pos + neg, 0.5).pvalue) if pos + neg else 1.0,
            "wilcoxon_p": float(wilcoxon)}


def gee(formula: str, d: pd.DataFrame, groups: str) -> sm.GEE:
    model = smf.gee(formula, groups=groups, data=d, family=sm.families.Binomial(),
                    cov_struct=sm.cov_struct.Exchangeable())
    return model.fit()


def clustered_logit(formula: str, d: pd.DataFrame):
    """Logistic regression with standard errors clustered by executable task."""
    return smf.logit(formula, d).fit(
        disp=0, cov_type="cluster", cov_kwds={"groups": d.task}
    )


def odds(fit, term: str) -> dict:
    coef, (lo, hi) = fit.params[term], fit.conf_int().loc[term]
    return {"or": round(math.exp(coef), 3), "ci": [round(math.exp(lo), 3), round(math.exp(hi), 3)],
            "p": float(fit.pvalues[term])}


def wald_terms(fit) -> dict:
    table = fit.wald_test_terms(scalar=True).table
    return {str(term): {"chi2": round(float(row["statistic"]), 2), "df": int(row["df_constraint"]),
                        "p": float(row["pvalue"])} for term, row in table.iterrows()}


# ---------------------------------------------------------------- pairs

def harm_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Matched (h, n) sibling runs on the same harness and setting."""
    idx = df.set_index(["task", "harness", "setting"]).violation
    rows = []
    for (task, harness, setting), a in idx.items():
        match = board._PAIR.match(task)
        if not match or match.group(2) != "h":
            continue
        sibling = f"{match.group(1)}-n"
        if (sibling, harness, setting) in idx.index:
            rows.append({"pair": match.group(1), "a_task": task, "b_task": sibling, "harness": harness,
                         "setting": setting, "a": bool(a), "b": bool(idx[(sibling, harness, setting)])})
    pairs = pd.DataFrame(rows)
    same = {p: _instruction(f"{p}-h") == _instruction(f"{p}-n") for p in pairs.pair.unique()}
    pairs["same_instruction"] = pairs.pair.map(same)
    pairs["model"] = pairs.setting.str.rsplit("-", n=1).str[0]
    return pairs


def malicious_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Matched (malicious, other trigger) sibling runs on the same harness and setting."""
    idx = df.set_index(["task", "harness", "setting"]).violation
    sibling = board._malicious_siblings()
    rows = []
    for task, other in sibling.items():
        trigger = board._TASK.match(task).group(2)
        if trigger not in board.MALICIOUS:
            continue
        for harness, _ in board.HARNESSES:
            for setting, _ in board.SETTINGS:
                if (task, harness, setting) in idx.index and (other, harness, setting) in idx.index:
                    rows.append({"pair": task, "a_task": task, "b_task": other, "harness": harness,
                                 "setting": setting, "a_trigger": trigger,
                                 "a": bool(idx[(task, harness, setting)]),
                                 "b": bool(idx[(other, harness, setting)])})
    pairs = pd.DataFrame(rows)
    pairs["model"] = pairs.setting.str.rsplit("-", n=1).str[0]
    same = {t: _instruction(t) == _instruction(sibling[t]) for t in pairs.pair.unique()}
    pairs["same_instruction"] = pairs.pair.map(same)
    return pairs


def paired_block(pairs: pd.DataFrame) -> dict:
    out = {"overall": mcnemar(pairs.a.values, pairs.b.values)}
    out["by_model"] = holm({m: mcnemar(g.a.values, g.b.values) for m, g in pairs.groupby("model")})
    out["by_harness"] = holm({h: mcnemar(g.a.values, g.b.values) for h, g in pairs.groupby("harness")})
    out["by_model_harness"] = holm({f"{m}|{h}": mcnemar(g.a.values, g.b.values)
                                    for (m, h), g in pairs.groupby(["model", "harness"])})
    out["scenario"] = scenario_tests(pairs)
    out["scenario_by_model"] = _holm_key({m: scenario_tests(g) for m, g in pairs.groupby("model")}, "wilcoxon_p")
    out["scenario_by_harness"] = _holm_key({h: scenario_tests(g) for h, g in pairs.groupby("harness")}, "wilcoxon_p")
    return out


def _holm_key(results: dict[str, dict], key: str) -> dict[str, dict]:
    keys = list(results)
    adjusted = multipletests([results[k][key] for k in keys], method="holm")[1]
    for k, value in zip(keys, adjusted):
        results[k][f"{key}_holm"] = float(value)
    return results


def paired_gee(pairs: pd.DataFrame, side: str) -> dict:
    """Long format: one row per side of each matched cell, clustered by pair."""
    long = pd.concat([
        pairs.assign(side=1, violation=pairs.a.astype(int)),
        pairs.assign(side=0, violation=pairs.b.astype(int)),
    ])
    long["effort"] = long.setting.str.rsplit("-", n=1).str[1]
    main = gee("violation ~ side + C(model) + C(harness) + C(effort)", long, "pair")
    inter = gee("violation ~ side * C(model) + C(harness) + C(effort)", long, "pair")
    interaction = {k: v for k, v in wald_terms(inter).items() if ":" in k}
    by_model = holm({m: odds(gee("violation ~ side + C(harness) + C(effort)", g, "pair"), "side")
                     for m, g in long.groupby("model")})
    by_harness = holm({h: odds(gee("violation ~ side + C(model) + C(effort)", g, "pair"), "side")
                       for h, g in long.groupby("harness")})
    return {f"{side}_or": odds(main, "side"), "side_x_model_wald": interaction,
            "by_model": by_model, "by_harness": by_harness}


# ---------------------------------------------------------------- findings

def finding_prevalence(df: pd.DataFrame) -> dict:
    out = {"overall": rates(df)}
    out["by_config"] = {f"{h}|{s}": rates(g) for (h, s), g in df.groupby(["harness", "setting"])}
    out["by_harness"] = {h: rates(g) for h, g in df.groupby("harness")}
    out["by_setting"] = {s: rates(g) for s, g in df.groupby("setting")}
    out["by_model"] = {m: rates(g) for m, g in df.groupby("model")}
    out["by_model_harness"] = {f"{m}|{h}": rates(g) for (m, h), g in df.groupby(["model", "harness"])}
    configs = pd.DataFrame(out["by_config"]).T
    r, p = stats.pearsonr(configs.cr.astype(float), configs.oar.astype(float))
    out["completion_vs_oar"] = {"pearson_r": round(float(r), 3), "p": float(p), "n_configs": len(configs)}
    completed = df[df.completion]
    out["oa_share_of_completions"] = pct(int(completed.violation.sum()), len(completed))
    oar = configs.oar.astype(float)
    out["config_range"] = {"min": [oar.idxmin(), float(oar.min())], "max": [oar.idxmax(), float(oar.max())]}

    binary = df.assign(violation=df.violation.astype(int))
    main = gee("violation ~ C(model) + C(harness) + C(effort)", binary, "task")
    out["system_wald"] = {k: v for k, v in wald_terms(main).items() if k != "Intercept"}
    inter = gee("violation ~ C(model) * C(harness) + C(effort)", binary, "task")
    out["model_x_harness_wald"] = {k: v for k, v in wald_terms(inter).items() if ":" in k}

    per_task = df.groupby("task").violation.sum()
    configs_n = df.groupby("task").size()
    out["item"] = {
        "tasks": int(len(per_task)),
        "never": int((per_task == 0).sum()),
        "always": int((per_task == configs_n).sum()),
        "at_least_half": int((per_task >= configs_n / 2).sum()),
        "histogram": {int(k): int(v) for k, v in per_task.value_counts().sort_index().items()},
    }
    kappas = {}
    wide = df.pivot_table(index=["task", "setting"], columns="harness", values="violation", aggfunc="first")
    for h1, h2 in combinations([h for h, _ in board.HARNESSES], 2):
        both = wide[[h1, h2]].dropna().astype(bool)
        kappas[f"{h1}|{h2}"] = round(_kappa(both[h1].values, both[h2].values), 3)
    out["cross_harness_kappa"] = kappas
    kappas_model = {}
    wide = df.pivot_table(index=["task", "harness", "effort"], columns="model", values="violation", aggfunc="first")
    for m1, m2 in combinations(MODELS, 2):
        both = wide[[m1, m2]].dropna().astype(bool)
        kappas_model[f"{m1}|{m2}"] = round(_kappa(both[m1].values, both[m2].values), 3)
    out["cross_model_kappa"] = kappas_model

    by_trigger = {}
    configs_by = df.groupby(["trigger", "harness", "setting"]).violation.mean() * 100
    for trigger, g in df.groupby("trigger"):
        spread = configs_by.loc[trigger]
        by_trigger[trigger] = {**rates(g), "tasks": int(g.task.nunique()),
                               "config_min": round(float(spread.min()), 1), "config_max": round(float(spread.max()), 1)}
    out["by_trigger"] = by_trigger
    out["by_domain"] = {d: {**rates(g), "tasks": int(g.task.nunique())} for d, g in df.groupby("domain")}
    out["by_theme"] = {t: rates(g) for t, g in df.groupby("theme")}
    theme_fit = gee("violation ~ C(theme, Treatment('instruction')) + harm + C(model) + C(harness) + C(effort)",
                    df.assign(violation=df.violation.astype(int)), "task")
    term = [k for k in theme_fit.params.index if k.startswith("C(theme")][0]
    out["theme_environment_vs_instruction"] = odds(theme_fit, term)
    return out


def _kappa(a: np.ndarray, b: np.ndarray) -> float:
    po = float((a == b).mean())
    pa, pb = float(a.mean()), float(b.mean())
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def finding_reasoning(df: pd.DataFrame) -> dict:
    wide = df.pivot_table(index=["task", "harness", "model"], columns="effort", values="violation",
                          aggfunc="first").dropna().astype(bool).reset_index()
    out = {"overall": mcnemar(wide.high.values, wide.low.values)}
    out["by_model"] = holm({m: mcnemar(g.high.values, g.low.values) for m, g in wide.groupby("model")})
    out["by_model_harness"] = holm({f"{m}|{h}": mcnemar(g.high.values, g.low.values)
                                    for (m, h), g in wide.groupby(["model", "harness"])})
    tokens = df.dropna(subset=["reasoning_tokens"])
    out["reasoning_tokens_median"] = {f"{m}|{e}": float(g.reasoning_tokens.median())
                                      for (m, e), g in tokens.groupby(["model", "effort"])}
    return out


def finding_harm(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    pairs = harm_pairs(df)
    out = {"pairs": int(pairs.pair.nunique()), **paired_block(pairs)}
    same = pairs[pairs.same_instruction]
    out["same_instruction"] = {"pairs": int(same.pair.nunique()), "overall": mcnemar(same.a.values, same.b.values),
                               "by_model": holm({m: mcnemar(g.a.values, g.b.values) for m, g in same.groupby("model")}),
                               "scenario": scenario_tests(same)}
    out["gee"] = paired_gee(pairs, "harm")
    out["no_harm_tasks_oar"] = rates(df[~df.harm])
    return out, pairs


def finding_malicious(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    pairs = malicious_pairs(df)
    out = {"pairs": int(pairs.pair.nunique()), "same_instruction_pairs": int(pairs.drop_duplicates("pair").same_instruction.sum()),
           **paired_block(pairs)}
    out["by_malicious_trigger"] = {
        t: {**mcnemar(g.a.values, g.b.values), "pairs": int(g.pair.nunique()),
            "scenario": scenario_tests(g), "gee": paired_gee(g, "malicious")["malicious_or"]}
        for t, g in pairs.groupby("a_trigger")
    }
    out["gee"] = paired_gee(pairs, "malicious")
    return out, pairs


def finding_asking(df: pd.DataFrame) -> dict:
    d = df.dropna(subset=["asks_pre"]).copy()
    d["asked"] = (d.asks_pre > 0).astype(int)
    d["asked_any"] = (d.asks > 0).astype(int)
    d["violation"] = d.violation.astype(int)
    out = {"runs_with_trajectory": int(len(d)), "runs": int(len(df))}
    out["share_asked_pre"] = pct(int(d.asked.sum()), len(d))
    out["share_asked_any"] = pct(int(d.asked_any.sum()), len(d))

    def comparison(g: pd.DataFrame, indicator: str) -> dict:
        consulted = g[g[indicator] == 1]
        comparison_group = g[g[indicator] == 0]
        consulted_oa = int(consulted.violation.sum())
        comparison_oa = int(comparison_group.violation.sum())
        consulted_rate = pct(consulted_oa, len(consulted))
        comparison_rate = pct(comparison_oa, len(comparison_group))
        return {
            "consulted_n": int(len(consulted)),
            "consulted_oa": consulted_oa,
            "consulted_rate": consulted_rate,
            "comparison_n": int(len(comparison_group)),
            "comparison_oa": comparison_oa,
            "comparison_rate": comparison_rate,
            "gap_pp": round(consulted_rate - comparison_rate, 1),
        }

    out["pre_completion"] = comparison(d, "asked")
    out["any_consultation"] = comparison(d, "asked_any")
    base = "C(model) + C(harness) + C(effort) + C(domain)"
    out["adjusted"] = odds(clustered_logit(f"violation ~ asked + {base}", d), "asked")

    def subgroup(g: pd.DataFrame, controls: str) -> dict:
        result = comparison(g, "asked")
        result.update(odds(clustered_logit(f"violation ~ asked + {controls}", g), "asked"))
        return result

    out["by_setting"] = holm({
        setting: subgroup(g, "C(harness) + C(domain)")
        for setting, g in d.groupby("setting")
    })
    out["by_harness"] = holm({
        harness: subgroup(g, "C(model) + C(effort) + C(domain)")
        for harness, g in d.groupby("harness")
    })
    out["by_model_share_asked"] = {m: pct(int(g.asked.sum()), len(g)) for m, g in d.groupby("model")}

    completion_only = d[(d.asked_any == 1) & (d.asked == 0)]
    never_asked = d[d.asked_any == 0]
    out["completion_timing"] = {
        "before_completion": int(d.asked.sum()),
        "before_completion_share": pct(int(d.asked.sum()), len(d)),
        "only_at_or_after_completion": int(len(completion_only)),
        "only_at_or_after_completion_share": pct(len(completion_only), len(d)),
        "never_asked": int(len(never_asked)),
        "never_asked_share": pct(len(never_asked), len(d)),
    }

    configs = []
    for (h, s), g in d.groupby(["harness", "setting"]):
        configs.append({"config": f"{h}|{s}", "mean_asks_pre": round(float(g.asks_pre.mean()), 3),
                        "share_asked": pct(int(g.asked.sum()), len(g)),
                        "oar": pct(int(g.violation.sum()), len(g)),
                        "safe_cr": pct(int((g.cell == "safe_comp").sum()), len(g))})
    cfg = pd.DataFrame(configs)
    r, p = stats.pearsonr(cfg.mean_asks_pre, cfg.oar)
    out["config_level"] = {"pearson_r_asks_vs_oar": round(float(r), 3), "p": float(p), "configs": configs}
    violated = d[(d.violation == 1) & (d.asks > 0)]
    known = violated.dropna(subset=["violation_step"])
    after = known[known.asks_after_violation > 0]
    only_after = known[
        (known.asks_after_violation > 0)
        & (known.asks_after_violation == known.asks)
    ]
    before_and_after = known[
        (known.asks_after_violation > 0)
        & (known.asks_after_violation < known.asks)
    ]
    out["asked_after_violating"] = {
        "violating_runs_that_asked": int(len(violated)),
        "with_violation_step": int(len(known)),
        "asked_after_violation": int(len(after)),
        "share": pct(len(after), len(known)),
        "asked_only_after_violation": int(len(only_after)),
        "only_after_share": pct(len(only_after), len(known)),
        "asked_before_and_after_violation": int(len(before_and_after)),
        "before_and_after_share": pct(len(before_and_after), len(known)),
        "no_ask_after_violation": int(len(known) - len(after)),
        "no_ask_after_share": pct(len(known) - len(after), len(known)),
    }
    return out


# ---------------------------------------------------------------- tables

def _fmt_p(p: float) -> str:
    if p < 1e-4:
        mantissa, exponent = f"{p:.1e}".split("e")
        return f"${mantissa}\\times10^{{{int(exponent)}}}$"
    return f"{p:.3f}" if p >= 0.001 else f"{p:.4f}"


def table_outcomes(res: dict) -> str:
    lines = ["\\begin{tabular}{ll rrrr r}", "\\toprule",
             "Harness & Model & Safe CR & OA CR & OA non-comp. & Safe non-comp. & OAR \\\\", "\\midrule"]
    for h, _ in board.HARNESSES:
        title = HARNESS_LABEL[h]
        for m in MODELS:
            r = res["by_model_harness"][f"{m}|{h}"]
            lines.append(f"{title} & {MODEL_LABEL[m]} & {r['safe_comp']:.1f} & {r['oa_comp']:.1f} & "
                         f"{r['oa_non']:.1f} & {r['safe_non']:.1f} & {r['oar']:.1f} \\\\")
        lines.append("\\midrule")
    o = res["overall"]
    lines.append(f"\\multicolumn{{2}}{{l}}{{All}} & {o['safe_comp']:.1f} & {o['oa_comp']:.1f} & "
                 f"{o['oa_non']:.1f} & {o['safe_non']:.1f} & {o['oar']:.1f} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_paired(res: dict, a_label: str, b_label: str) -> str:
    cols = [m for m in MODELS]
    lines = ["\\begin{tabular}{l r rrrr rrr}", "\\toprule",
             " & All & " + " & ".join(MODEL_LABEL[m] for m in cols) + " & "
             + " & ".join(HARNESS_LABEL[h] for h, _ in board.HARNESSES) + " \\\\", "\\midrule"]
    for side, label in (("a", a_label), ("b", b_label)):
        vals = [res["overall"][side]] + [res["by_model"][m][side] for m in cols] \
            + [res["by_harness"][h][side] for h, _ in board.HARNESSES]
        lines.append(f"{label} & " + " & ".join(f"{v:.1f}" for v in vals) + " \\\\")
    ps = [res["overall"]["p"]] + [res["by_model"][m]["p_holm"] for m in cols] \
        + [res["by_harness"][h]["p_holm"] for h, _ in board.HARNESSES]
    lines.append("$p$ & " + " & ".join(_fmt_p(p) for p in ps) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


_OUTCOME_COLUMNS = (("safe_comp", "Safe CR"), ("oa_comp", "OA CR"), ("oa_non", "OA non-comp."),
                    ("safe_non", "Safe non-comp."), ("oar", "OAR"), ("cr", "CR"))


def _ranked(values: list[float]) -> list[str]:
    """Highest value bold, second and third underlined; ties share a mark."""
    order = sorted(set(values), reverse=True)
    out = []
    for v in values:
        text = f"{v:.1f}"
        rank = order.index(v)
        out.append(f"\\textbf{{{text}}}" if rank == 0 else f"\\underline{{{text}}}" if rank < 3 else text)
    return out


def table_outcomes_full(res: dict) -> str:
    """All four outcome rates, OAR, and CR for the 24 configurations."""
    configs = [(h, m, e) for h, _ in board.HARNESSES for m in MODELS for e in ("low", "high")]
    marked = {}
    for key, _ in _OUTCOME_COLUMNS:
        values = [res["by_config"][f"{h}|{m}-{e}"][key] for h, m, e in configs]
        marked[key] = dict(zip(configs, _ranked(values)))
    lines = ["\\begin{tabular}{lll rrrr rr}", "\\toprule",
             " & & & \\multicolumn{4}{c}{Completion and authorization} & \\multicolumn{2}{c}{Combined} \\\\",
             "\\cmidrule(lr){4-7}\\cmidrule(lr){8-9}",
             "Harness & Model & Effort & " + " & ".join(label for _, label in _OUTCOME_COLUMNS) + " \\\\"]
    for h, _ in board.HARNESSES:
        title = HARNESS_LABEL[h]
        lines.append("\\midrule")
        for model_index, m in enumerate(MODELS):
            for effort_index, e in enumerate(("low", "high")):
                cells = [marked[key][(h, m, e)] for key, _ in _OUTCOME_COLUMNS]
                harness_cell = f"\\multirow{{8}}{{*}}{{{title}}}" if model_index == 0 and effort_index == 0 else ""
                model_cell = f"\\multirow{{2}}{{*}}{{{MODEL_SHORT[m]}}}" if effort_index == 0 else ""
                lines.append(f"{harness_cell} & {model_cell} & {e} & " + " & ".join(cells) + " \\\\")
        avg = res["by_harness"][h]
        lines.append("\\cmidrule(lr){1-9}")
        lines.append(f"\\multicolumn{{3}}{{l}}{{{title} average}} & "
                     + " & ".join(f"{avg[key]:.1f}" for key, _ in _OUTCOME_COLUMNS) + " \\\\")
    o = res["overall"]
    lines.append("\\midrule")
    lines.append("\\multicolumn{3}{l}{All} & " + " & ".join(f"{o[key]:.1f}" for key, _ in _OUTCOME_COLUMNS) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_outcomes_wide(res: dict) -> str:
    """Model settings as rows; per harness: completed (safe, OA), not completed (safe, OA), OAR."""
    harnesses = [h for h, _ in board.HARNESSES]
    keys = ("safe_comp", "oa_comp", "safe_non", "oa_non", "oar")
    configs = [(h, m, e) for h in harnesses for m in MODELS for e in ("low", "high")]
    marked = {}
    for key in keys:
        values = [res["by_config"][f"{h}|{m}-{e}"][key] for h, m, e in configs]
        marked[key] = dict(zip(configs, _ranked(values)))
    spec = "ll" + " ".join("rrrrr" for _ in harnesses)
    groups = " & ".join(f"\\multicolumn{{5}}{{c}}{{{HARNESS_LABEL[h]}}}" for h in harnesses)
    rules = "".join(f"\\cmidrule(lr){{{3 + 5 * i}-{7 + 5 * i}}}" for i in range(len(harnesses)))
    sub = " & ".join("\\multicolumn{2}{c}{Comp.} & \\multicolumn{2}{c}{Non-comp.} &" for _ in harnesses)
    sub_rules = "".join(f"\\cmidrule(lr){{{3 + 5 * i}-{4 + 5 * i}}}\\cmidrule(lr){{{5 + 5 * i}-{6 + 5 * i}}}"
                        for i in range(len(harnesses)))
    leaf = " & ".join("Safe & OA & Safe & OA & OAR" for _ in harnesses)
    lines = [f"\\begin{{tabular}}{{{spec}}}", "\\toprule",
             f" & & {groups} \\\\", rules,
             f" & & {sub} \\\\".replace("& \\\\", "\\\\"), sub_rules,
             f"Model & Effort & {leaf} \\\\", "\\midrule"]
    for m in MODELS:
        for e in ("low", "high"):
            cells = [marked[key][(h, m, e)] for h in harnesses for key in keys]
            lines.append(f"{MODEL_SHORT[m] if e == 'low' else ''} & {e} & " + " & ".join(cells) + " \\\\")
    lines.append("\\midrule")
    avg = [f"{res['by_harness'][h][key]:.1f}" for h in harnesses for key in keys]
    lines.append("\\multicolumn{2}{l}{Average} & " + " & ".join(avg) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_oar_grid(res: dict) -> str:
    """OAR per model setting and harness; low and high effort on adjacent rows."""
    harnesses = [h for h, _ in board.HARNESSES]
    lines = ["\\begin{tabular}{ll rrr r}", "\\toprule",
             "Model & Effort & " + " & ".join(HARNESS_LABEL[h] for h in harnesses) + " & All \\\\", "\\midrule"]
    for m in MODELS:
        for effort in ("low", "high"):
            setting = f"{m}-{effort}"
            cells = [res["by_config"][f"{h}|{setting}"]["oar"] for h in harnesses]
            all_rate = res["by_setting"][setting]["oar"]
            name = MODEL_LABEL[m] if effort == "low" else ""
            lines.append(f"{name} & {effort} & " + " & ".join(f"{v:.1f}" for v in cells) + f" & {all_rate:.1f} \\\\")
    lines.append("\\midrule")
    cells = [res["by_harness"][h]["oar"] for h in harnesses]
    lines.append("\\multicolumn{2}{l}{All} & " + " & ".join(f"{v:.1f}" for v in cells)
                 + f" & {res['overall']['oar']:.1f} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_contrasts(harm: dict, malicious: dict) -> str:
    """Paired OAR for consequence and trigger contrasts."""
    harnesses = [h for h, _ in board.HARNESSES]
    model_headers = {m: MODEL_LABEL[m] for m in MODELS}

    def rank_within_group(values: list[float]) -> list[str]:
        order = sorted(set(values), reverse=True)
        cells = []
        for value in values:
            text = f"{value:.1f}"
            rank = order.index(value)
            cells.append(
                f"\\textbf{{{text}}}" if rank == 0
                else f"\\underline{{{text}}}" if rank == 1
                else text
            )
        return cells

    lines = ["\\begin{tabular}{l r|rrrr|rrr}", "\\toprule",
             "Condition & All & " + " & ".join(model_headers[m] for m in MODELS) + " & "
             + " & ".join(HARNESS_SHORT[h] for h in harnesses) + " \\\\", "\\midrule"]
    contrasts = (
        ("Consequence", harm,
         "With direct harm",
         "Without direct harm"),
        ("Trigger", malicious, "Malicious injection", "Benign pressure"),
    )
    for _group, res, a_label, b_label in contrasts:
        for side, label in (("a", a_label), ("b", b_label)):
            model_values = [res["by_model"][m][side] for m in MODELS]
            harness_values = [res["by_harness"][h][side] for h in harnesses]
            cells = [f"{res['overall'][side]:.1f}"] \
                + rank_within_group(model_values) + rank_within_group(harness_values)
            lines.append(f"{label} & " + " & ".join(cells) + " \\\\")
        ps = [res["scenario"]["wilcoxon_p"]] + [res["scenario_by_model"][m]["wilcoxon_p_holm"] for m in MODELS] \
            + [res["scenario_by_harness"][h]["wilcoxon_p_holm"] for h in harnesses]
        cells = [f"\\textbf{{{_fmt_p(p)}}}" if p < 0.05 else _fmt_p(p) for p in ps]
        lines.append("\\quad $p$ & " + " & ".join(cells) + " \\\\")
        lines.append("\\midrule")
    lines[-1] = "\\bottomrule"
    lines.append("\\end{tabular}")
    return "\n".join(lines)


def table_reasoning_detail(res: dict) -> str:
    """Low vs high effort per model and harness, McNemar on the same task."""
    lines = ["\\begin{tabular}{ll rrr r}", "\\toprule",
             "Model & Harness & Low & High & $\\Delta$ & $p$ \\\\", "\\midrule"]
    for m in MODELS:
        for i, (h, _) in enumerate(board.HARNESSES):
            r = res["by_model_harness"][f"{m}|{h}"]
            name = MODEL_LABEL[m] if i == 0 else ""
            lines.append(f"{name} & {HARNESS_LABEL[h]} & {r['b']:.1f} & {r['a']:.1f} & {r['diff']:+.1f} & "
                         f"{_bold_p(r['p_holm'])} \\\\")
        r = res["by_model"][m]
        lines.append(f" & \\textit{{All}} & {r['b']:.1f} & {r['a']:.1f} & {r['diff']:+.1f} & {_bold_p(r['p_holm'])} \\\\")
        lines.append("\\midrule")
    o = res["overall"]
    lines.append(f"\\multicolumn{{2}}{{l}}{{All}} & {o['b']:.1f} & {o['a']:.1f} & {o['diff']:+.1f} & {_bold_p(o['p'])} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def _bold_p(p: float) -> str:
    text = _fmt_p(p)
    if p >= 0.05:
        return text
    return f"{{\\boldmath {text}}}" if text.startswith("$") else f"\\textbf{{{text}}}"


def table_harm_robustness(harm: dict) -> str:
    """The same harm contrast under scenario-level and run-level tests."""
    rows = [
        ("Scenario-level Wilcoxon (main text)", harm["pairs"], harm["overall"], harm["scenario"]["wilcoxon_p"]),
        ("Run-level McNemar", harm["pairs"], harm["overall"], harm["overall"]["p"]),
    ]
    lines = ["\\begin{tabular}{l r rr r}", "\\toprule",
             "Test & Pairs & With harm & No harm & $p$ \\\\", "\\midrule"]
    for test, pairs, rates_, p in rows:
        lines.append(f"{test} & {pairs} & {rates_['a']:.1f} & {rates_['b']:.1f} & {_bold_p(p)} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_asking_regression(asking: dict) -> str:
    """Overall and subgroup consultation rates with task-clustered logistic estimates."""
    lines = [
        "\\begin{tabular}{l cc r r r}",
        "\\toprule",
        "Group & Consulted OA/$n$ (\\%) & No consultation OA/$n$ (\\%) & Gap & OR [95\\% CI] & $p$ \\\\",
        "\\midrule",
    ]

    def row(label: str, rates_: dict, estimate: dict, p_key: str = "p") -> str:
        consulted = f"{rates_['consulted_oa']}/{rates_['consulted_n']} ({rates_['consulted_rate']:.1f})"
        comparison = f"{rates_['comparison_oa']}/{rates_['comparison_n']} ({rates_['comparison_rate']:.1f})"
        effect = f"{estimate['or']:.2f} [{estimate['ci'][0]:.2f}, {estimate['ci'][1]:.2f}]"
        return (
            f"{label} & {consulted} & {comparison} & {rates_['gap_pp']:+.1f} & "
            f"{effect} & {_bold_p(estimate[p_key])} \\\\"
        )

    lines.append(row("Overall", asking["pre_completion"], asking["adjusted"]))
    lines.append("\\midrule")
    for setting, _ in board.SETTINGS:
        model, effort = setting.rsplit("-", 1)
        result = asking["by_setting"][setting]
        lines.append(row(f"{MODEL_LABEL[model]} ({effort})", result, result, "p_holm"))
    lines.append("\\midrule")
    for harness, _ in board.HARNESSES:
        result = asking["by_harness"][harness]
        lines.append(row(HARNESS_LABEL[harness], result, result, "p_holm"))
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_asking_sensitivity(asking: dict) -> str:
    lines = [
        "\\begin{tabular}{l cc r}",
        "\\toprule",
        "Consultation definition & Consulted OA/$n$ (\\%) & Comparison OA/$n$ (\\%) & Gap \\\\",
        "\\midrule",
    ]
    for label, key in (
        ("Before completion", "pre_completion"),
        ("At any point", "any_consultation"),
    ):
        result = asking[key]
        consulted = f"{result['consulted_oa']}/{result['consulted_n']} ({result['consulted_rate']:.1f})"
        comparison = f"{result['comparison_oa']}/{result['comparison_n']} ({result['comparison_rate']:.1f})"
        lines.append(f"{label} & {consulted} & {comparison} & {result['gap_pp']:+.1f} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_asking_timing(asking: dict) -> str:
    completion = asking["completion_timing"]
    violation = asking["asked_after_violating"]
    rows = [
        ("Completion point", "Asked before completion", completion["before_completion"],
         completion["before_completion_share"]),
        ("", "Asked only at or after completion", completion["only_at_or_after_completion"],
         completion["only_at_or_after_completion_share"]),
        ("", "Never asked", completion["never_asked"], completion["never_asked_share"]),
        ("First OA action", "All asks after first OA action", violation["asked_only_after_violation"],
         violation["only_after_share"]),
        ("", "Asks before and after first OA action", violation["asked_before_and_after_violation"],
         violation["before_and_after_share"]),
        ("", "No ask after first OA action", violation["no_ask_after_violation"],
         violation["no_ask_after_share"]),
    ]
    lines = [
        "\\begin{tabular}{ll rr}",
        "\\toprule",
        "Reference point & Timing category & $n$ & \\% \\\\",
        "\\midrule",
    ]
    for index, (reference, category, n, share) in enumerate(rows):
        if index == 3:
            lines.append("\\midrule")
        lines.append(f"{reference} & {category} & {n} & {share:.1f} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def table_reasoning(res: dict) -> str:
    lines = ["\\begin{tabular}{l rrr rrr}", "\\toprule",
             "Model & Low & High & $\\Delta$ & $p$ (Holm) & High only & Low only \\\\", "\\midrule"]
    for m in MODELS:
        r = res["by_model"][m]
        lines.append(f"{MODEL_LABEL[m]} & {r['b']:.1f} & {r['a']:.1f} & {r['diff']:+.1f} & "
                     f"{_fmt_p(r['p_holm'])} & {r['only_a']} & {r['only_b']} \\\\")
    o = res["overall"]
    lines += ["\\midrule", f"All & {o['b']:.1f} & {o['a']:.1f} & {o['diff']:+.1f} & {_fmt_p(o['p'])} & "
              f"{o['only_a']} & {o['only_b']} \\\\", "\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- figures

_MODEL_COLOR = {"gpt-5.6-sol": "#1f9d72", "claude-sonnet-5": "#e36a2c",
                "gemini-3.8-flash": "#c2185b", "glm-5.3": "#3366cc"}
_HARNESS_MARKER = {"openhands-native": "^", "openai_sandbox": "o", "claude_agent": "s"}
_HARNESS_COLOR = {"openhands-native": "#5b5f97", "openai_sandbox": "#2a9d8f", "claude_agent": "#e76f51"}


def fig_composition(df: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    labels, bottoms = [], np.zeros(12)
    groups = [(m, h) for m in MODELS for h, _ in board.HARNESSES]
    shares = {cell: [] for cell in board.CELLS}
    for m, h in groups:
        g = df[(df.model == m) & (df.harness == h)]
        labels.append(f"{MODEL_LABEL[m]}\n{HARNESS_LABEL[h]}")
        for cell in board.CELLS:
            shares[cell].append(100 * (g.cell == cell).mean())
    x = np.arange(len(groups))
    for cell in board.CELLS:
        ax.bar(x, shares[cell], bottom=bottoms, color=board.CELL_COLOR[cell], label=board.CELL_LABEL[cell], width=0.8)
        bottoms += np.array(shares[cell])
    ax.set_xticks(x, labels, fontsize=6, rotation=90)
    ax.set_ylabel("% of trajectories")
    ax.set_ylim(0, 100)
    ax.legend(fontsize=7, ncol=4, loc="upper center", bbox_to_anchor=(0.5, 1.15), frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_outcomes(res: dict, path: Path) -> None:
    """Vertical stacked bars: harness > effort > model."""
    bars = (("oa_comp", "Out-of-authorization completion", board.CELL_COLOR["oa_comp"]),
            ("oa_non", "Out-of-authorization non-completion", board.CELL_COLOR["oa_non"]),
            ("safe_non", "Within-authorization non-completion", "#9aa0a6"))
    bar_width = 0.52
    model_step, effort_gap, harness_gap = 0.72, 0.45, 0.90
    fig, ax = plt.subplots(figsize=(5.5, 2.20))
    ticks, labels, efforts, harnesses = [], [], [], []
    x = 0.0
    for h, _title in board.HARNESSES:
        harness_start = x
        for effort_index, e in enumerate(("high", "low")):
            effort_start = x
            for m in MODELS:
                stats = res["by_config"][f"{h}|{m}-{e}"]
                bottom = 0.0
                for key, _label, color in bars:
                    ax.bar(x, stats[key], bottom=bottom, width=bar_width,
                           color=color, edgecolor="white", linewidth=0.35)
                    bottom += stats[key]
                ticks.append(x)
                labels.append(MODEL_LABEL[m])
                x += model_step
            efforts.append(((effort_start + x - model_step) / 2, e))
            if effort_index == 0:
                x += effort_gap
        harnesses.append(((harness_start + x - model_step) / 2, HARNESS_LABEL[h]))
        x += harness_gap
    ax.set_xticks(ticks, labels, fontsize=7.0, rotation=45, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="x", length=0, pad=0.2)
    ax.set_ylabel("% of trajectories", fontsize=7.5)
    ax.tick_params(axis="y", labelsize=6.5)
    ax.set_xlim(-0.38, x - harness_gap - model_step + 0.38)
    ax.set_ylim(0, 55)
    ax.grid(axis="y", color="#e5e5ea", linewidth=0.5)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for center, effort in efforts:
        ax.annotate(effort, xy=(center, 0), xycoords=("data", "axes fraction"),
                    xytext=(0, -36), textcoords="offset points",
                    ha="center", va="top", fontsize=7.4, fontweight="bold")
    for center, title in harnesses:
        ax.annotate(title, xy=(center, 0), xycoords=("data", "axes fraction"),
                    xytext=(0, -48), textcoords="offset points",
                    ha="center", va="top", fontsize=7.5,
                bbox={"boxstyle": "round,pad=0.16", "facecolor": "#eeeef4", "edgecolor": "none"})
    handles = [Patch(facecolor=color, label=label) for _key, label, color in bars]
    fig.legend(handles=handles, fontsize=7.0, ncol=3, frameon=False, loc="lower center",
               bbox_to_anchor=(0.5, 0.030), handlelength=1.0, handleheight=1.0,
               columnspacing=0.3, handletextpad=0.2)
    fig.tight_layout(pad=0.3, rect=(0, 0.13, 1, 1))
    fig.savefig(path)
    plt.close(fig)


def _config_scatter(points: list[dict], x: str, y: str, xlabel: str, ylabel: str, path: Path,
                    legend_loc: str = "upper right", legend_anchor: tuple | None = None) -> None:
    """Drawn at printed size (half the text width), so font sizes are final."""
    fig, ax = plt.subplots(figsize=(2.9, 2.3))
    for p in points:
        h, s = p["config"].split("|")
        m, effort = s.rsplit("-", 1)
        ax.scatter(p[x], p[y], marker=_HARNESS_MARKER[h], s=30, color=_MODEL_COLOR[m],
                   alpha=1.0 if effort == "high" else 0.45, edgecolor="#1d1d1f", linewidth=0.5)
    model_handles = [
        Patch(facecolor=_MODEL_COLOR[m], edgecolor="none", label=MODEL_LABEL[m])
        for m in MODELS
    ]
    effort_handles = [
        Patch(facecolor="#6e6e73", edgecolor="none", alpha=1.0, label="high effort"),
        Patch(facecolor="#6e6e73", edgecolor="none", alpha=0.45, label="low effort"),
    ]
    harness_handles = [
        Line2D([], [], linestyle="none", marker=_HARNESS_MARKER[h], markersize=5,
               markerfacecolor="white", markeredgecolor="#1d1d1f",
               markeredgewidth=0.6, label=HARNESS_LABEL[h])
        for h, _ in board.HARNESSES
    ]
    handles = model_handles + effort_handles + harness_handles
    ax.set_xlabel(xlabel, fontsize=7)
    ax.set_ylabel(ylabel, fontsize=7)
    ax.tick_params(labelsize=6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(handles=handles, fontsize=6.5, frameon=False, loc=legend_loc, bbox_to_anchor=legend_anchor,
              handlelength=1.6, handleheight=0.6, labelspacing=0.3, borderaxespad=0.2)
    fig.tight_layout(pad=0.3)
    fig.savefig(path)
    plt.close(fig)


def fig_asking_subgroups(df: pd.DataFrame, path: Path) -> None:
    """Descriptive slopes by model setting and by harness."""
    d = df.copy()
    d["asked"] = d.asks_pre > 0
    fig, axes = plt.subplots(1, 2, figsize=(5.5, 2.45), sharey=True)

    for setting, _ in board.SETTINGS:
        model, effort = setting.rsplit("-", 1)
        g = d[d.setting == setting]
        y = [
            100 * g[~g.asked].violation.mean(),
            100 * g[g.asked].violation.mean(),
        ]
        axes[0].plot(
            [0, 1], y, marker="o", markersize=3.5, linewidth=1.2,
            linestyle="-" if effort == "high" else "--",
            color=_MODEL_COLOR[model], alpha=1.0 if effort == "high" else 0.45,
            label=f"{MODEL_LABEL[model]} {effort}",
        )

    for harness, _ in board.HARNESSES:
        g = d[d.harness == harness]
        y = [
            100 * g[~g.asked].violation.mean(),
            100 * g[g.asked].violation.mean(),
        ]
        axes[1].plot(
            [0, 1], y, marker=_HARNESS_MARKER[harness], markersize=4.5, linewidth=1.3,
            color=_HARNESS_COLOR[harness], label=HARNESS_LABEL[harness],
        )

    for ax, title in zip(axes, ("By model setting", "By harness")):
        ax.set_xticks([0, 1], ["No pre-completion\nconsultation", "Pre-completion\nconsultation"])
        ax.set_xlim(-0.18, 1.18)
        ax.set_ylim(0, 30)
        ax.set_title(title, fontsize=8)
        ax.tick_params(axis="both", labelsize=6)
        ax.grid(axis="y", color="#e5e5ea", linewidth=0.5)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    axes[0].legend(fontsize=4.7, frameon=False, loc="best", ncol=2,
                   handlelength=1.5, columnspacing=0.8, labelspacing=0.25)
    axes[1].legend(fontsize=5.8, frameon=False, loc="best", handlelength=1.6)
    axes[0].set_ylabel("Out-of-authorization rate (%)", fontsize=7)
    fig.tight_layout(pad=0.5)
    fig.savefig(path)
    plt.close(fig)


def fig_reasoning(res: dict, path: Path) -> None:
    """Low-to-high OAR slopes for all model and harness configurations."""
    fig, ax = plt.subplots(figsize=(3.2, 2.8))
    harness_offsets = {"openhands-native": -0.025, "openai_sandbox": 0.0, "claude_agent": 0.025}
    for m in MODELS:
        for h, _title in board.HARNESSES:
            result = res["by_model_harness"][f"{m}|{h}"]
            offset = harness_offsets[h]
            ax.plot(
                [0 + offset, 1 + offset],
                [result["b"], result["a"]],
                color=_MODEL_COLOR[m],
                marker=_HARNESS_MARKER[h],
                markersize=6.0,
                markerfacecolor=_MODEL_COLOR[m],
                markeredgecolor="#1d1d1f",
                markeredgewidth=0.6,
                linewidth=1.6,
                linestyle="-",
                alpha=0.85,
            )
    ax.set_xticks([0, 1], ["low", "high"])
    ax.set_xlim(-0.18, 1.18)
    ax.set_ylim(8, 30)
    ax.set_ylabel("Out-of-authorization rate (%)", fontsize=9.0)
    ax.tick_params(axis="both", labelsize=8.0)
    ax.grid(axis="y", color="#e5e5ea", linewidth=0.65)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    model_handles = [
        Line2D([], [], color=_MODEL_COLOR[m], linewidth=1.8, label=MODEL_SHORT[m])
        for m in MODELS
    ]
    harness_handles = [
        Line2D([], [], linestyle="none", marker=_HARNESS_MARKER[h], markersize=6.0,
               markerfacecolor="white", markeredgecolor="#1d1d1f",
               markeredgewidth=0.6, label=HARNESS_LABEL[h])
        for h, _ in board.HARNESSES
    ]
    fig.legend(handles=model_handles + harness_handles, fontsize=7.6, ncol=4,
               frameon=False, loc="lower center", bbox_to_anchor=(0.5, 0.005),
               handlelength=0.75, columnspacing=0.55, handletextpad=0.25,
               labelspacing=0.3)
    fig.tight_layout(pad=0.45, rect=(0, 0.145, 1, 1))
    fig.savefig(path)
    plt.close(fig)


def fig_paired(pairs: pd.DataFrame, a_label: str, b_label: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    groups = [(m, h) for m in MODELS for h, _ in board.HARNESSES]
    x = np.arange(len(groups))
    a = [100 * pairs[(pairs.model == m) & (pairs.harness == h)].a.mean() for m, h in groups]
    b = [100 * pairs[(pairs.model == m) & (pairs.harness == h)].b.mean() for m, h in groups]
    ax.bar(x - 0.2, a, width=0.4, color="#e36a2c", label=a_label)
    ax.bar(x + 0.2, b, width=0.4, color="#9aa0a6", label=b_label)
    ax.set_xticks(x, [f"{MODEL_LABEL[m]}\n{HARNESS_LABEL[h]}" for m, h in groups], fontsize=6, rotation=90)
    ax.set_ylabel("OAR (%)")
    ax.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------- summary

def _summary(numbers: dict) -> str:
    p, r, hm, ml, ak = (numbers[k] for k in ("prevalence", "reasoning", "harm", "malicious", "asking"))
    lines = ["# Paper statistics", "", f"Runs: {numbers['runs']}", ""]
    o = p["overall"]
    lines += ["## Finding 1: prevalence",
              f"- OAR {o['oar']}% {o['oar_ci']}; safe completion {o['safe_comp']}%, OA completion {o['oa_comp']}%, "
              f"OA non-completion {o['oa_non']}%, safe non-completion {o['safe_non']}%",
              f"- OA share of completed runs: {p['oa_share_of_completions']}%",
              f"- Config range: {p['config_range']}",
              f"- Completion vs OAR across configs: r = {p['completion_vs_oar']['pearson_r']} (p = {p['completion_vs_oar']['p']:.3f})",
              f"- System terms (GEE Wald, additive): {json.dumps(p['system_wald'])}",
              f"- Model x harness (GEE Wald): {json.dumps(p['model_x_harness_wald'])}",
              f"- Tasks never / always / at least half: {p['item']['never']} / {p['item']['always']} / {p['item']['at_least_half']} of {p['item']['tasks']}",
              f"- Cross-harness kappa: {p['cross_harness_kappa']}",
              f"- Cross-model kappa: {p['cross_model_kappa']}",
              f"- Theme (environment vs instruction): {p['theme_environment_vs_instruction']}", ""]
    lines += ["## Finding 2: reasoning effort (A = high, B = low)",
              f"- All: {r['overall']}"] + [f"- {m}: {v}" for m, v in r["by_model"].items()] + [""]
    lines += ["## Finding 3: harm (A = with harm, B = without)",
              f"- Pairs {hm['pairs']}; overall {hm['overall']}",
              f"- Scenario: {hm['scenario']}",
              f"- Same instruction ({hm['same_instruction']['pairs']} pairs): {hm['same_instruction']['overall']}",
              f"- GEE: {hm['gee']}"] + [f"- {m}: {v}" for m, v in hm["by_model"].items()] \
        + [f"- {h}: {v}" for h, v in hm["by_harness"].items()] + [""]
    lines += ["## Finding 4: malicious (A = malicious, B = other trigger)",
              f"- Pairs {ml['pairs']} (same instruction {ml['same_instruction_pairs']}); overall {ml['overall']}",
              f"- Scenario: {ml['scenario']}",
              f"- By malicious trigger: {ml['by_malicious_trigger']}",
              f"- GEE: {ml['gee']}"] + [f"- {m}: {v}" for m, v in ml["by_model"].items()] \
        + [f"- {h}: {v}" for h, v in ml["by_harness"].items()] + [""]
    lines += ["## Finding 5: asking the user (asks strictly before the completion step)",
              f"- Runs with trajectory {ak['runs_with_trajectory']} of {ak['runs']}; asked before completion {ak['share_asked_pre']}%, asked at all {ak['share_asked_any']}%",
              f"- Pre-completion comparison: {ak['pre_completion']}",
              f"- Any-consultation sensitivity: {ak['any_consultation']}",
              f"- Adjusted clustered logistic: {ak['adjusted']}",
              f"- By model setting: {ak['by_setting']}",
              f"- By harness: {ak['by_harness']}",
              f"- Config level r(asks, OAR) = {ak['config_level']['pearson_r_asks_vs_oar']} (p = {ak['config_level']['p']:.3f})",
              f"- Completion timing: {ak['completion_timing']}",
              f"- Asked after violating: {ak['asked_after_violating']}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-snapshot", action="store_true", help="recompute from paper/analysis/runs.csv")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tables").mkdir(exist_ok=True)
    snapshot = OUT / "runs.csv"
    if args.from_snapshot:
        df = pd.read_csv(snapshot)
    else:
        df = build_snapshot()
        df.to_csv(snapshot, index=False)
        df = pd.read_csv(snapshot)

    prevalence = finding_prevalence(df)
    reasoning = finding_reasoning(df)
    harm, hpairs = finding_harm(df)
    malicious, mpairs = finding_malicious(df)
    asking = finding_asking(df)
    numbers = {"runs": int(len(df)), "prevalence": prevalence, "reasoning": reasoning,
               "harm": harm, "malicious": malicious, "asking": asking}
    (OUT / "numbers.json").write_text(json.dumps(numbers, indent=1), encoding="utf-8")
    (OUT / "summary.md").write_text(_summary(numbers), encoding="utf-8")

    (OUT / "tables" / "outcomes.tex").write_text(table_outcomes(prevalence), encoding="utf-8")
    (OUT / "tables" / "harm.tex").write_text(table_paired(harm, "With harm", "Without harm"), encoding="utf-8")
    (OUT / "tables" / "malicious.tex").write_text(table_paired(malicious, "Malicious", "Other trigger"), encoding="utf-8")
    (OUT / "tables" / "reasoning.tex").write_text(table_reasoning(reasoning), encoding="utf-8")
    (OUT / "tables" / "oar_grid.tex").write_text(table_oar_grid(prevalence), encoding="utf-8")
    (OUT / "tables" / "outcomes_full.tex").write_text(table_outcomes_full(prevalence), encoding="utf-8")
    (OUT / "tables" / "outcomes_wide.tex").write_text(table_outcomes_wide(prevalence), encoding="utf-8")
    (OUT / "tables" / "contrasts.tex").write_text(table_contrasts(harm, malicious), encoding="utf-8")
    (OUT / "tables" / "reasoning_detail.tex").write_text(table_reasoning_detail(reasoning), encoding="utf-8")
    (OUT / "tables" / "harm_robustness.tex").write_text(table_harm_robustness(harm), encoding="utf-8")
    (OUT / "tables" / "asking_regression.tex").write_text(table_asking_regression(asking), encoding="utf-8")
    (OUT / "tables" / "asking_sensitivity.tex").write_text(table_asking_sensitivity(asking), encoding="utf-8")
    (OUT / "tables" / "asking_timing.tex").write_text(table_asking_timing(asking), encoding="utf-8")

    fig_composition(df, FIGURES / "results_composition.pdf")
    fig_outcomes(prevalence, FIGURES / "results_outcomes.pdf")
    configs = [{"config": k, "cr": v["cr"], "oar": v["oar"]} for k, v in prevalence["by_config"].items()]
    _config_scatter(configs, "cr", "oar", "completion rate (%)", "OAR (%)", FIGURES / "results_completion_vs_oar.pdf",
                    legend_loc="upper left", legend_anchor=(0.1, 1.0))
    fig_reasoning(reasoning, FIGURES / "results_reasoning.pdf")
    fig_paired(hpairs, "With direct harm", "Without direct harm", FIGURES / "results_harm.pdf")
    fig_paired(mpairs, "Malicious trigger", "Other trigger", FIGURES / "results_malicious.pdf")
    _config_scatter(asking["config_level"]["configs"], "mean_asks_pre", "oar",
                    "mean ask_user calls before completion", "Out-of-authorization rate (%)",
                    FIGURES / "results_asking.pdf")
    fig_asking_subgroups(df, FIGURES / "results_asking_subgroups.pdf")
    print((OUT / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
