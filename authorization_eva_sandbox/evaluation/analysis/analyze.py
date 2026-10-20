#!/usr/bin/env python
"""
Authorization eval analysis: 6 models x 84 tasks (504 trajectories).

Produces a single self-contained interactive HTML report covering:
  - dataset/task distributions (domain / situational type / trigger / harm)
  - overall performance + per-model / per-domain / per-harm / per-type performance
  - efficiency & cost (iterations, agent->user messages, tokens, reasoning)
  - cross analysis heatmaps (model x {domain, harm, type})
  - exploratory: iteration vs violation, message-count vs violation

Design decisions (confirmed with user):
  - multi-trigger tasks are counted as MULTI-LABEL (a task contributes to every
    type/trigger it carries); task total stays 84, label counts may exceed 84.
  - cost uses cost_usd_table (deterministic, our own price table).
  - the design is a perfectly balanced grid (1 trajectory per (task, model)),
    so micro == macro for every breakdown; we use pooled proportions.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.offline as pyo
import statsmodels.formula.api as smf

from pre_completion_messages import messages_before_completion

HERE = Path(__file__).resolve().parent
CSV = HERE.parent / "outputs" / "eval_full_504.csv"
OUT_HTML = HERE / "report.html"
CLEAN_CSV = HERE / "clean_trajectories.csv"
# workspace root: analysis -> evaluation -> authorization_eva_sandbox -> <repo>
REPO_ROOT = HERE.parents[2]

# trajectory step types that advance the agent's iteration counter (one LLM
# step == one iteration). Used only to locate the in-run completion point.
_ACTION_TYPES = {"browser_action", "terminal_command", "file_read", "file_edit",
                 "agent_think", "agent_message", "agent_finish", "ipython_cell"}

MODEL_ORDER = ["gpt5.5", "gpt4.1", "sonnet4.6", "gemini 3.1 pro",
               "qwen3-thinking", "qwen3-instruct"]
TYPE_ORDER = ["Instruction", "Context & Memory", "System",
              "External Environment", "Adversarial"]

MODEL_FULLNAME = {
    "gpt5.5": "GPT-5.5",
    "gpt4.1": "GPT-4.1",
    "sonnet4.6": "Claude Sonnet 4.6",
    "gemini 3.1 pro": "Gemini 3.1 Pro Preview",
    "qwen3-thinking": "Qwen3 VL 30B A3B Thinking",
    "qwen3-instruct": "Qwen3 VL 30B A3B Instruct",
}

# atomic trigger text -> (type, short label)
TRIGGER_MAP = {
    "The instruction is vague in authorization scope": ("Instruction", "a. vague auth scope"),
    "The instruction contains semantic ambiguity": ("Instruction", "b. semantic ambiguity"),
    "Prior instruction with lingering effect": ("Instruction", "c. lingering prior instruction"),
    "Multi-criteria priority blur in instruction": ("Instruction", "d. priority blur"),
    "Cross-session information accumulation in a shared context": ("Context & Memory", "f. cross-session accumulation"),
    "Input volume exceeded agent's context capacity": ("Context & Memory", "g. context overflow"),
    "Input volume exceeded the agent's context capacity": ("Context & Memory", "g. context overflow"),
    "Conflict authorizations within the systems": ("System", "h. conflicting authorizations"),
    "Attention hijacking by an adjacent-but-out-of-scope element": ("External Environment", "i. attention hijacking"),
    'The system environment raises a requirement for "inconvenient" handling': ("External Environment", "j. inconvenient handling"),
    "The technical system environment has undergone some changes": ("External Environment", "k. technical change"),
    "The social context has shifted, along with a change in norms": ("External Environment", "l. norm shift"),
    "The conversational context rationalizes an out-of-scope action": ("External Environment", "m. conversational rationalization"),
    "Malicious injection from the environment that directly induces out-of-authorization action": ("Adversarial", "n. direct malicious injection"),
    "Malicious instructions from the environment": ("Adversarial", "n. direct malicious injection"),
    "Malicious injection from the environment that distorts the authorization scope of the user's instruction": ("Adversarial", "o. scope-distorting injection"),
}

# consistent palette
PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2", "#B279A2"]
TYPE_COLOR = {t: PALETTE[i] for i, t in enumerate(TYPE_ORDER)}

# outcome composition (Okabe-Ito, colorblind-safe)
OUTCOME_CATS = [
    ("OA non-completion rate", "#D55E00", lambda c, v: (~c & v)),  # vermillion
    ("OA CR", "#E69F00", lambda c, v: (c & v)),                   # orange
    ("Safe CR", "#009E73", lambda c, v: (c & ~v)),                # bluish green
    ("Safe non-completion", "#BDBDBD", lambda c, v: (~c & ~v)),   # gray
]
PAPER_COMPOSITION_PNG = REPO_ROOT / "Paper" / "figures" / "composition.png"
PLOT_FONT = dict(family="Helvetica, Arial, DejaVu Sans, sans-serif", size=12, color="#222")


def _pct_label(rate: float, min_pct: float = 3.0) -> str:
    """Format a share as a bar label; hide tiny segments to avoid clutter."""
    if pd.isna(rate) or rate * 100 < min_pct:
        return ""
    return f"{rate * 100:.1f}%"


def _outcome_shares(df: pd.DataFrame, group_col: str, order: list) -> dict[str, list[float]]:
  shares: dict[str, list[float]] = {name: [] for name, _, _ in OUTCOME_CATS}
  for g in order:
    sub = df[df[group_col] == g]
    c, v = sub["completion_yes"], sub["violation_yes"]
    for name, _, fn in OUTCOME_CATS:
      shares[name].append(fn(c, v).mean() if len(sub) else np.nan)
  return shares


# ----------------------------------------------------------------------------- load & clean
def split_triggers(s: str) -> list[str]:
    return [p.strip() for p in str(s).split("|") if p.strip()]


def load() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    df["model_display"] = df["model_display"].astype(str).str.strip()
    df["completion_yes"] = df["completion"].eq("yes")
    df["violation_yes"] = df["violation"].eq("yes")
    df["safe"] = df["safe_completion"].astype(str).str.lower().isin(["true", "yes"])
    df["harm_flag"] = df["harm"].map({"Y": "with AI safety harm", "N": "No AI safety harm"})
    df["cost_usd"] = df["cost_usd_table"]

    # multi-label type/trigger lists per row
    def to_types(s):
        out = []
        for t in split_triggers(s):
            if t not in TRIGGER_MAP:
                raise ValueError(f"Unmapped trigger: {t!r}")
            out.append(TRIGGER_MAP[t][0])
        return sorted(set(out), key=TYPE_ORDER.index)

    def to_labels(s):
        return sorted({TRIGGER_MAP[t][1] for t in split_triggers(s)})

    # base scenario id = task without the trailing -h/-n harm-variant suffix;
    # a base with both -h and -n forms a matched pair (same domain/trigger, harm differs)
    df["base"] = df["task"].str.rsplit("-", n=1).str[0]
    df["types"] = df["situational_trigger"].map(to_types)
    df["trigger_labels"] = df["situational_trigger"].map(to_labels)
    # Agent->user messages before task completion use the post-hoc judge's
    # cited step boundary (see pre_completion_messages.py).
    pre = [
        messages_before_completion(of, rf, repo_root=REPO_ROOT)
        for of, rf in zip(df["output_file"], df["report_file"])
    ]
    df["agent_user_messages_pre_completion"] = [c for c, _ in pre]
    df["pre_completion_boundary"] = [b for _, b in pre]
    # binary partition (no double counting): malicious if any Adversarial trigger
    df["trigger_class"] = np.where(
        df["types"].map(lambda ts: "Adversarial" in ts),
        "malicious trigger", "benign trigger")
    return df


def explode_on(df: pd.DataFrame, col: str, newname: str) -> pd.DataFrame:
    e = df.explode(col).rename(columns={col: newname})
    return e


# ----------------------------------------------------------------------------- metrics
# All five metrics share the same denominator N (joint probabilities), so the
# bars are directly comparable. Self-consistent decomposition:
#   safe-completion + violated-completion       = completion rate
#   violated-completion + violated-noncompletion = violation rate
METRIC_NAMES = ["completion rate", "safe-completion rate", "violated-completion rate",
                "violated-noncompletion rate", "violation rate"]

# fixed per-metric colors (per eval-metrics.md): blue / green / yellow / red / orange
METRIC_COLOR = {
    "completion rate": "#4C78A8",            # blue
    "safe-completion rate": "#54A24B",       # green
    "violated-completion rate": "#EECA3B",   # yellow
    "violated-noncompletion rate": "#E45756",# red
    "violation rate": "#F58518",             # orange
}


def metrics(sub: pd.DataFrame) -> dict:
    n = len(sub)
    comp = sub["completion_yes"]
    viol = sub["violation_yes"]
    return {
        "n": n,
        "completion rate": comp.mean() if n else np.nan,
        "safe-completion rate": sub["safe"].mean() if n else np.nan,
        "violation rate": viol.mean() if n else np.nan,
        "violated-completion rate": (comp & viol).mean() if n else np.nan,
        "violated-noncompletion rate": (~comp & viol).mean() if n else np.nan,
    }


def grouped_bar(df: pd.DataFrame, group_col: str, order: list, title: str,
                note: str = "", metric_subset: list | None = None,
                metric_colors: dict | None = None) -> go.Figure:
    rows = {g: metrics(df[df[group_col] == g]) for g in order}
    fig = go.Figure()
    cmap = {**METRIC_COLOR, **(metric_colors or {})}
    for m in (metric_subset or METRIC_NAMES):
        fig.add_bar(
            name=m, x=order, y=[rows[g][m] for g in order],
            marker_color=cmap[m],
            text=[f"{rows[g][m]*100:.0f}%" if pd.notna(rows[g][m]) else "" for g in order],
            textposition="outside", textfont_size=9,
        )
    cap = title + (f"<br><sup>{note}</sup>" if note else "")
    fig.update_layout(barmode="group", title=cap, yaxis_title="rate",
                      yaxis_tickformat=".0%", yaxis_range=[0, 1.05],
                      legend=dict(orientation="h", y=-0.25), height=480,
                      margin=dict(t=70, b=120))
    return fig


def stacked_outcomes(df: pd.DataFrame, group_col: str, order: list, title: str,
                     label_map: dict | None = None) -> go.Figure:
    """Horizontal stacked bar of the four completion x violation outcomes per
    group, which sum to 100%. Colorblind-safe Okabe-Ito palette; segment order
    left->right: OA non-completion, OA CR, Safe CR, Safe non-completion."""
    ylabels = [(label_map or {}).get(g, g) for g in order]
    shares = _outcome_shares(df, group_col, order)
    fig = go.Figure()
    for name, color, _ in OUTCOME_CATS:
        xs = shares[name]
        fig.add_bar(
            y=ylabels, x=xs, name=name, orientation="h", marker_color=color,
            marker_line=dict(color="white", width=0.8),
            text=[_pct_label(x, min_pct=8.0) for x in xs], textposition="inside",
            insidetextfont=dict(color="white", size=13),
        )
    fig.update_layout(
        template="plotly_white", barmode="stack", title=title,
        xaxis_title="share of trajectories",
        xaxis_tickformat=".0%", xaxis_range=[0, 1],
        xaxis=dict(showgrid=True, gridcolor="#e6e6e6", zeroline=False, ticksuffix="",
                   tickfont=dict(size=13), title_font=dict(size=14)),
        yaxis=dict(categoryorder="array", categoryarray=ylabels, autorange="reversed",
                   tickfont=dict(size=13)),
        height=80 + 55 * len(order), margin=dict(t=60, l=230, r=24, b=56),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, xanchor="left",
                    font=dict(size=13), traceorder="normal"),
        font=PLOT_FONT, plot_bgcolor="white", paper_bgcolor="white",
    )
    return fig


def _annotate_stacked_bar(ax, y_idx: float, left: float, width: float, text: str,
                          *, inside_min: float = 6.5, above_min: float = 2.0,
                          inverted: bool = True) -> None:
    """Place a segment label without overlapping neighbors."""
    if width < above_min:
        return
    x_center = left + width / 2
    if width >= inside_min:
        if x_center < 6:
            x, ha = left + 0.6, "left"
        else:
            x, ha = x_center, "center"
        ax.text(
            x, y_idx, text, ha=ha, va="center", color="white",
            fontsize=12, fontweight="medium", clip_on=False,
        )
    else:
        y_off = -0.36 if inverted else 0.36
        x = left + 0.6
        ax.text(
            x, y_idx + y_off, text, ha="left", va="bottom", color="#333333",
            fontsize=10, clip_on=False,
        )


def export_composition_png(df: pd.DataFrame, path: Path = PAPER_COMPOSITION_PNG) -> Path:
    """Static paper figure: model x outcome composition (matplotlib export)."""
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 12,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.labelcolor": "#222",
        "xtick.color": "#444",
        "ytick.color": "#444",
    })
    labels = [MODEL_FULLNAME[m] for m in MODEL_ORDER]
    shares = _outcome_shares(df, "model_display", MODEL_ORDER)
    y = np.arange(len(labels))
    left = np.zeros(len(labels))
    fig, ax = plt.subplots(figsize=(9.2, 3.7))
    segment_labels: list[tuple[float, float, float, str]] = []
    for name, color, _ in OUTCOME_CATS:
        vals = np.array(shares[name]) * 100
        ax.barh(
            y, vals, left=left, height=0.62, color=color, label=name,
            edgecolor="white", linewidth=0.8,
        )
        for i, val in enumerate(vals):
            if val >= 1.0:
                segment_labels.append((y[i], left[i], val, f"{val:.1f}%"))
        left += vals
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=16)
    ax.tick_params(axis="y", pad=1)
    ax.invert_yaxis()
    for y_idx, seg_left, seg_width, seg_text in segment_labels:
        _annotate_stacked_bar(ax, y_idx, seg_left, seg_width, seg_text, inverted=True)
    ax.set_xlim(0, 100)
    ax.set_ylim(len(labels) - 0.55, -0.55)
    ax.set_xlabel("share of trajectories", fontsize=16)
    ax.tick_params(axis="x", labelsize=12)
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(xmax=100, decimals=0))
    ax.grid(axis="x", color="#e6e6e6", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, 1.22), ncol=4,
        frameon=False, fontsize=16, handlelength=1.3, columnspacing=1.4,
        handletextpad=0.5,
    )
    fig.subplots_adjust(left=0.22, right=0.995, top=0.84, bottom=0.14)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def box_scatter(df: pd.DataFrame, value_cols: list[str], labels: list[str],
                title: str, ytitle: str) -> go.Figure:
    fig = go.Figure()
    for j, (vc, lab) in enumerate(zip(value_cols, labels)):
        fig.add_box(
            x=df["model_display"], y=df[vc], name=lab,
            boxpoints="all", jitter=0.5, pointpos=0, marker_size=3,
            marker_color=PALETTE[j], line_color=PALETTE[j], opacity=0.8,
        )
    fig.update_layout(boxmode="group", title=title, yaxis_title=ytitle,
                      xaxis=dict(categoryorder="array", categoryarray=MODEL_ORDER),
                      legend=dict(orientation="h", y=-0.2), height=480,
                      margin=dict(t=60, b=80),
                      template="plotly_white", font=PLOT_FONT,
                      plot_bgcolor="white", paper_bgcolor="white",
                      yaxis=dict(showgrid=True, gridcolor="#e6e6e6", zeroline=False))
    return fig


MODEL_SYMBOL = ["circle", "square", "diamond", "triangle-up", "star", "x"]


def model_scatter(xs: dict, ys: dict, xtitle: str, ytitle: str, title: str,
                  x_pct: bool = True) -> go.Figure:
    """One point per model (distinct shape+color), labelled. y is a rate (%)."""
    fig = go.Figure()
    for i, m in enumerate(MODEL_ORDER):
        fig.add_scatter(
            x=[xs[m]], y=[ys[m]], mode="markers", name=MODEL_FULLNAME.get(m, m),
            marker=dict(size=15, symbol=MODEL_SYMBOL[i], color=PALETTE[i],
                        line=dict(width=1, color="#333")),
        )
    fig.update_layout(title=title, xaxis_title=xtitle, yaxis_title=ytitle,
                      yaxis_tickformat=".0%", height=500, margin=dict(t=60),
                      legend=dict(orientation="h", y=-0.2))
    if x_pct:
        fig.update_layout(xaxis_tickformat=".0%")
    return fig


def logit_message_effect(df: pd.DataFrame):
    """Effect of agent->user messaging on violation, binary AND continuous.

    Binary specs (M0-M2) use has_message (>=1 vs 0); continuous specs (M3-M4)
    use the raw message count and log(1+count). Nested controls show how much of
    the apparent 'protective' effect survives the Simpson confounder (model),
    domain, and task length. Returns (table_html, forest_fig, pred_fig, dose_fig).
    """
    d = df.copy()
    d["has_message"] = (d["agent_user_messages"] >= 1).astype(int)
    d["msg_count"] = d["agent_user_messages"].astype(float)
    d["log_msg"] = np.log1p(d["msg_count"])
    d["violation_yes"] = d["violation_yes"].astype(int)
    d["model_display"] = pd.Categorical(d["model_display"], categories=MODEL_ORDER)

    adj = "+ C(model_display) + C(domain)"
    specs = [
        ("M0 binary, unadjusted", "violation_yes ~ has_message", "has_message", "binary >=1 vs 0 msg"),
        ("M1 binary, +model+domain", f"violation_yes ~ has_message {adj}", "has_message", "binary, adjusted"),
        ("M2 binary, +iterations", f"violation_yes ~ has_message {adj} + agent_iterations", "has_message", "binary, +task length"),
        ("M3 count, +model+domain", f"violation_yes ~ msg_count {adj}", "msg_count", "per +1 message"),
        ("M4 log-count, +model+domain", f"violation_yes ~ log_msg {adj}", "log_msg", "per +1 in log(1+msg)"),
    ]
    rows, main = [], None
    for name, f, term, interp in specs:
        fit = smf.logit(f, data=d).fit(disp=0)
        if name.startswith("M1"):
            main = fit
        orv = np.exp(fit.params[term])
        lo, hi = np.exp(fit.conf_int().loc[term])
        rows.append({"model spec": name, "predictor": interp, "odds ratio": round(orv, 3),
                     "95% CI": f"[{lo:.2f}, {hi:.2f}]", "p-value": f"{fit.pvalues[term]:.3g}"})
    tbl = pd.DataFrame(rows)

    # forest plot of M1 odds ratios
    def nice(t):
        return (t.replace("C(model_display)[T.", "model: ").replace("C(domain)[T.", "domain: ")
                 .replace("]", "").replace("has_message", ">=1 user message"))
    terms = [t for t in main.params.index if t != "Intercept"]
    ors = np.exp(main.params[terms])
    ci = np.exp(main.conf_int().loc[terms])
    labels = [nice(t) for t in terms]
    colors = ["#E45756" if t == "has_message" else "#4C78A8" for t in terms]
    forest = go.Figure(go.Scatter(
        x=ors.values, y=labels, mode="markers",
        marker=dict(size=9, color=colors),
        error_x=dict(type="data", symmetric=False,
                     array=ci[1].values - ors.values, arrayminus=ors.values - ci[0].values),
    ))
    forest.add_vline(x=1, line_dash="dash", line_color="grey")
    forest.update_layout(title="Adjusted odds ratios (M1: violation ~ message + model + domain)",
                         xaxis_title="odds ratio (log scale, &lt;1 = lower violation)",
                         xaxis_type="log", height=520, margin=dict(t=60, l=180))

    # adjusted predicted violation rate: has_message 0 vs 1 (others held at data mix)
    d0, d1 = d.copy(), d.copy()
    d0["has_message"], d1["has_message"] = 0, 1
    p0, p1 = main.predict(d0).mean(), main.predict(d1).mean()
    raw0 = d.loc[d.has_message == 0, "violation_yes"].mean()
    raw1 = d.loc[d.has_message == 1, "violation_yes"].mean()
    pred = go.Figure()
    pred.add_bar(name="raw (unadjusted)", x=["no message", ">=1 message"], y=[raw0, raw1],
                 marker_color=PALETTE[1], text=[f"{raw0*100:.0f}%", f"{raw1*100:.0f}%"], textposition="outside")
    pred.add_bar(name="adjusted (M1)", x=["no message", ">=1 message"], y=[p0, p1],
                 marker_color=PALETTE[0], text=[f"{p0*100:.0f}%", f"{p1*100:.0f}%"], textposition="outside")
    pred.update_layout(barmode="group", title="Violation rate: raw vs model/domain-adjusted",
                       yaxis_tickformat=".0%", yaxis_range=[0, 0.55], yaxis_title="violation rate",
                       legend=dict(orientation="h", y=-0.2), height=440, margin=dict(t=60))

    # dose-response: raw violation rate by message-count bucket (shape check for the continuous fit)
    dose_bins = [-0.5, 0.5, 1.5, 2.5, 5.5, 1e9]
    dose_lab = ["0", "1", "2", "3-5", "6+"]
    d["msg_bin"] = pd.cut(d["msg_count"], bins=dose_bins, labels=dose_lab)
    db = d.groupby("msg_bin", observed=True).agg(
        vr=("violation_yes", "mean"), n=("violation_yes", "size")).reset_index()
    dose = go.Figure(go.Bar(
        x=db["msg_bin"].astype(str), y=db["vr"], marker_color=PALETTE[5],
        text=[f"{v*100:.0f}%<br>n={n}" for v, n in zip(db.vr, db.n)], textposition="outside"))
    dose.update_layout(title="Dose-response: violation rate by agent->user message count (raw)",
                       xaxis_title="number of agent->user messages", yaxis_title="violation rate",
                       yaxis_tickformat=".0%", yaxis_range=[0, 0.55], height=440, margin=dict(t=60))
    return df_to_html(tbl), forest, pred, dose


def rate_table(df: pd.DataFrame, row_col: str, row_order: list, row_labels: list,
               metric: str, first_col: str, title: str) -> str:
    """HTML table: rows x (models + Ave) of a rate. Grid is balanced, so the
    Ave column (mean over models) equals the pooled rate for that row."""
    header = [first_col, "Ave."] + [MODEL_FULLNAME[m] for m in MODEL_ORDER]
    out = []
    for r, lab in zip(row_order, row_labels):
        vals = [metrics(df[(df[row_col] == r) & (df["model_display"] == m)])[metric]
                for m in MODEL_ORDER]
        ave = float(np.mean(vals))
        out.append([lab, f"{ave*100:.0f}%"] + [f"{v*100:.0f}%" for v in vals])
    tbl = pd.DataFrame(out, columns=header)
    return f"<h3>{title}</h3>" + df_to_html(tbl)


def pie(labels, values, title: str, colors=None, note: str = "") -> go.Figure:
    cap = title + (f"<br><sup>{note}</sup>" if note else "")
    fig = go.Figure(go.Pie(
        labels=list(labels), values=list(values), sort=False,
        textinfo="label+value+percent", textfont_size=12,
        marker=dict(colors=colors) if colors else None, hole=0.35,
    ))
    fig.update_layout(title=cap, height=440, margin=dict(t=70))
    return fig


def heatmap(df: pd.DataFrame, row_col: str, row_order: list, metric: str,
            title: str, row_labels: list | None = None) -> go.Figure:
    z, txt = [], []
    for r in row_order:
        zr, tr = [], []
        for m in MODEL_ORDER:
            sub = df[(df[row_col] == r) & (df["model_display"] == m)]
            v = metrics(sub)[metric]
            zr.append(v)
            tr.append(f"{v*100:.0f}%" if pd.notna(v) else "")
        z.append(zr)
        txt.append(tr)
    scale = "Reds" if "violation" in metric else "Greens"
    xlabels = [MODEL_FULLNAME.get(m, m) for m in MODEL_ORDER]
    fig = go.Figure(go.Heatmap(
        z=z, x=xlabels, y=row_labels or row_order, text=txt, texttemplate="%{text}",
        colorscale=scale, zmin=0, zmax=1, colorbar=dict(title=metric, tickformat=".0%"),
    ))
    fig.update_layout(title=title, height=120 + 55 * len(row_order),
                      margin=dict(t=60, l=160, b=120), xaxis_tickangle=-30)
    return fig


# ----------------------------------------------------------------------------- report assembly
def fig_html(fig: go.Figure) -> str:
    return pyo.plot(fig, include_plotlyjs=False, output_type="div")


def df_to_html(df: pd.DataFrame) -> str:
    return df.to_html(index=False, border=0, classes="tbl", float_format=lambda x: f"{x:.3f}")


def main():
    df = load()
    df.to_csv(CLEAN_CSV, index=False)
    tasks = df.drop_duplicates("task").copy()  # 84 task-level rows
    n_traj, n_tasks = len(df), len(tasks)

    sections: list[str] = []

    def add(title: str, *html_parts: str):
        sections.append(f"<section><h2>{title}</h2>" + "".join(html_parts) + "</section>")

    # -- overview ------------------------------------------------------------
    overall = metrics(df)
    ov = pd.DataFrame({
        "metric": METRIC_NAMES,
        "value": [f"{overall[m]*100:.1f}%" for m in METRIC_NAMES],
    })
    summary = (f"<p><b>{n_traj}</b> trajectories = <b>6</b> models x <b>{n_tasks}</b> tasks "
               f"(balanced grid, 1 trajectory per cell). "
               f"Domains: {tasks['domain'].nunique()}. "
               f"Harm: {(tasks['harm']=='Y').sum()} with-harm / {(tasks['harm']=='N').sum()} no-harm tasks. "
               f"Multi-trigger tasks: {tasks['situational_trigger'].str.contains('|', regex=False).sum()}.</p>")
    add("Overview & overall performance", summary, df_to_html(ov))

    # -- distributions (task level, multi-label) -- pie charts ---------------
    dom_d = tasks["domain"].value_counts()
    fdom = pie(dom_d.index, dom_d.values, "Domain distribution (84 tasks)")

    tt = explode_on(tasks, "types", "type")
    type_d = tt["type"].value_counts().reindex(TYPE_ORDER)
    ftype = pie(type_d.index, type_d.values, "Situational type distribution",
                colors=[TYPE_COLOR[t] for t in type_d.index],
                note="multi-label: a multi-trigger task is counted in each type; slices sum &gt; 84")

    tg = explode_on(tasks, "trigger_labels", "trigger")
    trig_d = tg["trigger"].value_counts()
    ftrig = go.Figure(go.Bar(x=trig_d.index, y=trig_d.values, marker_color=PALETTE[2],
                             text=trig_d.values, textposition="outside"))
    ftrig.update_layout(title="Situational trigger distribution (multi-label)",
                        height=480, margin=dict(t=60, b=200), xaxis_tickangle=-30)

    harm_d = tasks["harm_flag"].value_counts()
    fharm = pie(harm_d.index, harm_d.values, "Harm distribution (84 tasks)",
                colors=[PALETTE[3], PALETTE[4]])
    add("Task distributions",
        fig_html(fdom), fig_html(ftype), fig_html(ftrig), fig_html(fharm))

    # -- performance by model ------------------------------------------------
    add("Performance by model",
        fig_html(grouped_bar(df, "model_display", MODEL_ORDER, "Performance by model (5 metrics)")),
        fig_html(stacked_outcomes(df, "model_display", MODEL_ORDER,
                                  "Performance by model (4-way outcome composition, stacked to 100%)",
                                  label_map=MODEL_FULLNAME)))

    # -- capability vs out-of-authorization (per-model scatter) --------------
    comp_r = {m: df[df.model_display == m]["completion_yes"].mean() for m in MODEL_ORDER}
    viol_r = {m: df[df.model_display == m]["violation_yes"].mean() for m in MODEL_ORDER}
    comp_among_viol = {m: df[(df.model_display == m) & (df.violation_yes)]["completion_yes"].mean()
                       for m in MODEL_ORDER}
    avg_msg = {m: df[df.model_display == m]["agent_user_messages"].mean() for m in MODEL_ORDER}
    add("Capability vs out-of-authorization (per-model scatter)",
        fig_html(model_scatter(comp_r, viol_r,
                               "Capability — completion rate", "Out-of-authorization — violation rate",
                               "Plot 1: completion rate vs violation rate")),
        fig_html(model_scatter(comp_among_viol, viol_r,
                               "Capability — completion rate among violations",
                               "Out-of-authorization — violation rate",
                               "Plot 2: completion rate | violated vs violation rate")),
        fig_html(model_scatter(avg_msg, viol_r,
                               "Delegation to user — average agent&rarr;user messages",
                               "Out-of-authorization — violation rate",
                               "Plot 3: average agent&rarr;user messages vs violation rate",
                               x_pct=False)))

    # -- delegation: messages BEFORE task completion -------------------------
    pre = df["agent_user_messages_pre_completion"]
    post_total = int((df["agent_user_messages"] - pre).sum())
    n_changed = int((df["agent_user_messages"] - pre).gt(0).sum())
    avg_pre = {m: df[df.model_display == m]["agent_user_messages_pre_completion"].mean()
               for m in MODEL_ORDER}
    deleg_note = (
        f"<p><b>Pre-completion messages</b> count agent&rarr;user messages sent before "
        f"task completion. The completion boundary is determined in priority order: "
        f"(1) the step cited by the post-hoc completion judge when completion=yes; "
        f"(2) otherwise all messages in the trajectory. "
        f"Across the dataset this differs from the raw message total on "
        f"<b>{n_changed}</b> trajectories, excluding <b>{post_total}</b> messages "
        f"sent after completion.</p>")
    add("Delegation: agent&rarr;user messages before task completion",
        deleg_note,
        fig_html(box_scatter(df, ["agent_user_messages", "agent_user_messages_pre_completion"],
                             ["total", "before completion"],
                             "Agent->user messages by model: total vs before completion", "count")),
        fig_html(model_scatter(avg_pre, viol_r,
                               "Delegation — avg agent&rarr;user messages before completion",
                               "Out-of-authorization — violation rate",
                               "Avg pre-completion agent&rarr;user messages vs violation rate",
                               x_pct=False)))

    # -- efficiency & cost ---------------------------------------------------
    add("Efficiency & cost by model",
        fig_html(box_scatter(df, ["agent_iterations", "agent_user_messages"],
                             ["iterations", "agent->user messages"],
                             "Iterations & agent->user messages by model", "count")),
        fig_html(box_scatter(df, ["completion_tokens", "reasoning_tokens"],
                             ["completion tokens", "reasoning tokens"],
                             "Token usage by model", "tokens")))
    # reasoning share + cost per task table
    cost_tbl = (df.groupby("model_display")
                .agg(avg_cost_usd=("cost_usd", "mean"),
                     mean_reasoning_share=("reasoning_share", "mean"),
                     mean_iterations=("agent_iterations", "mean"))
                .reindex(MODEL_ORDER).reset_index())
    cost_tbl["avg_cost_usd"] = cost_tbl["avg_cost_usd"].round(4)
    cost_tbl["mean_reasoning_share"] = (cost_tbl["mean_reasoning_share"] * 100).round(1).astype(str) + "%"
    cost_tbl["mean_iterations"] = cost_tbl["mean_iterations"].round(1)
    cost_tbl.columns = ["model", "avg cost / task (USD)", "mean reasoning share", "mean iterations"]
    sections[-1] = sections[-1].replace("</section>",
                                        "<h3>Average cost per task & reasoning share</h3>"
                                        + df_to_html(cost_tbl) + "</section>")

    # -- performance by domain (sorted by completion desc) -------------------
    dom_order = (df.groupby("domain")["completion_yes"].mean()
                 .sort_values(ascending=False).index.tolist())
    add("Performance by domain",
        fig_html(grouped_bar(df, "domain", dom_order,
                             "Performance by domain (sorted by completion rate)")))

    # -- performance by harm -------------------------------------------------
    add("Performance by harm",
        fig_html(grouped_bar(df, "harm_flag",
                             ["with AI safety harm", "No AI safety harm"],
                             "Performance by harm")))

    # -- performance by situational type (multi-label) -----------------------
    df_type = explode_on(df, "types", "type")
    add("Performance by situational type",
        fig_html(grouped_bar(df_type, "type", TYPE_ORDER,
                             "Performance by situational type",
                             note="multi-label: a multi-trigger trajectory is counted in each type")))

    # -- heatmaps ------------------------------------------------------------
    # domain heatmaps: ordered by task count (largest on top), labelled with count
    dom_counts = tasks["domain"].value_counts()
    dom_heat = dom_counts.index[::-1].tolist()  # ascending so plotly puts largest on top
    dom_heat_labels = [f"{d.replace('_', ' ').title()} ({dom_counts[d]})" for d in dom_heat]
    hm = []
    hm.append(fig_html(heatmap(df, "domain", dom_heat, "violation rate",
                               "Heatmap 1: violation rate (model x domain)",
                               row_labels=dom_heat_labels)))
    hm.append(fig_html(heatmap(df, "domain", dom_heat, "safe-completion rate",
                               "Heatmap 2: safe-completion rate (model x domain)",
                               row_labels=dom_heat_labels)))
    harm_rows = ["with AI safety harm", "No AI safety harm"]
    hm.append(fig_html(heatmap(df, "harm_flag", harm_rows, "violation rate",
                               "Heatmap 3: violation rate (model x harm)")))
    hm.append(rate_table(df, "harm_flag", harm_rows,
                         ["with AI-safety harms", "without AI-safety harms"],
                         "violation rate", "scenario",
                         "Table for Finding 1 — violation rate by AI-safety harm (from Heatmap 3, all 84 tasks)"))
    # controlled robustness: matched harm pairs (same base scenario => same domain & trigger,
    # only harm differs), which removes domain/trigger composition confounding
    harm_by_base = df.drop_duplicates("task").groupby("base")["harm"].agg(set)
    paired_bases = [b for b, s in harm_by_base.items() if {"Y", "N"} <= s]
    df_harm_pair = df[df["base"].isin(paired_bases)]
    hm.append(rate_table(df_harm_pair, "harm_flag", harm_rows,
                         ["with AI-safety harms", "without AI-safety harms"],
                         "violation rate", "scenario",
                         f"Table for Finding 1 (controlled) — matched harm pairs only "
                         f"({len(paired_bases)} pairs; within each pair domain &amp; trigger are identical, "
                         f"only harm differs)"))
    hm.append(fig_html(heatmap(df, "harm_flag", harm_rows, "safe-completion rate",
                               "Heatmap 4: safe-completion rate (model x harm)")))
    hm.append(fig_html(heatmap(df_type, "type", TYPE_ORDER, "violation rate",
                               "Heatmap 5: violation rate (model x type, multi-label)")))
    hm.append(fig_html(heatmap(df_type, "type", TYPE_ORDER, "safe-completion rate",
                               "Heatmap 6: safe-completion rate (model x type, multi-label)")))
    tc_rows = ["malicious trigger", "benign trigger"]
    hm.append(fig_html(heatmap(df, "trigger_class", tc_rows, "violation rate",
                               "Heatmap 5-1: violation rate (model x malicious/benign trigger)")))
    hm.append(rate_table(df, "trigger_class", tc_rows,
                         ["malicious environment", "benign environment"],
                         "violation rate", "environment",
                         "Table for Finding 2 — violation rate by malicious/benign environment (from Heatmap 5-1)"))
    hm.append(fig_html(heatmap(df, "trigger_class", tc_rows, "safe-completion rate",
                               "Heatmap 6-1: safe-completion rate (model x malicious/benign trigger)")))
    add("Cross analysis (heatmaps)", *hm)

    # -- exploratory ---------------------------------------------------------
    # iteration vs violation
    bins = [0, 3, 6, 10, 15, 20, 100]
    blab = ["1-3", "4-6", "7-10", "11-15", "16-20", "21+"]
    df["iter_bin"] = pd.cut(df["agent_iterations"], bins=bins, labels=blab, right=True, include_lowest=True)
    ib = df.groupby("iter_bin", observed=True).agg(
        violation_rate=("violation_yes", "mean"), n=("violation_yes", "size")).reset_index()
    fiter = go.Figure()
    fiter.add_bar(x=ib["iter_bin"].astype(str), y=ib["violation_rate"], marker_color=PALETTE[3],
                  text=[f"{v*100:.0f}%<br>n={n}" for v, n in zip(ib.violation_rate, ib.n)],
                  textposition="outside")
    corr = df[["agent_iterations", "violation_yes"]].astype(float).corr().iloc[0, 1]
    fiter.update_layout(title=f"Violation rate by iteration count (point-biserial r={corr:.2f})",
                        yaxis_tickformat=".0%", yaxis_range=[0, 1.05],
                        xaxis_title="agent iterations", yaxis_title="violation rate",
                        height=440, margin=dict(t=60))

    # messages: 0 vs >=1
    df["msg_grp"] = np.where(df["agent_user_messages"] >= 1, ">=1 message", "no message")
    mg = df.groupby("msg_grp").agg(violation_rate=("violation_yes", "mean"),
                                   n=("violation_yes", "size")).reindex(["no message", ">=1 message"]).reset_index()
    fmsg = go.Figure(go.Bar(x=mg["msg_grp"], y=mg["violation_rate"], marker_color=PALETTE[0],
                            text=[f"{v*100:.0f}%<br>n={n}" for v, n in zip(mg.violation_rate, mg.n)],
                            textposition="outside"))
    fmsg.update_layout(title="Violation rate: no message vs >=1 agent->user message (raw)",
                       yaxis_tickformat=".0%", yaxis_range=[0, 1.05], yaxis_title="violation rate",
                       height=440, margin=dict(t=60))

    logit_tbl, forest, pred, dose = logit_message_effect(df)
    msg_note = """
<p><b>Question.</b> Does the agent sending message(s) to the user (to confirm /
clarify) reduce the chance it violates authorization?</p>
<p><b>Method.</b> We fit logistic regressions predicting <code>violation</code> (yes/no).
The outcome is binary, so a coefficient is read as an <b>odds ratio (OR)</b>: the
multiplicative change in the <i>odds</i> of violating. <b>OR&lt;1 = fewer violations,
OR=1 = no effect, OR&gt;1 = more violations.</b> We fit a ladder of nested models that
progressively control for confounders, because messaging is not randomly assigned:</p>
<ul>
<li><b>M0 — unadjusted.</b> Just compares the violation odds of trajectories with
&ge;1 message vs 0 messages. Controls for nothing, so it can be contaminated by the
<i>Simpson confounder</i>: strong models (sonnet, gpt5.5) both message more AND violate
less, which makes messaging look better than it really is.</li>
<li><b>M1 — + model + domain.</b> Adds the model and the domain as covariates, i.e. it
compares messaging vs no-messaging <i>within the same model and the same domain</i>.
This strips out the "strong models message more" effect. <b>M1 is our main estimate.</b></li>
<li><b>M2 — + iterations.</b> Further adds task length (iteration count), since longer
trajectories naturally accumulate more messages. Tests whether the effect is just a
proxy for "long task".</li>
<li><b>M3 / M4 — continuous (non-binary).</b> Instead of &ge;1-vs-0, these use the raw
message <i>count</i> (M3, OR per +1 message) and <code>log(1+count)</code> (M4) as the
predictor, both adjusted for model+domain. They test a dose-response: does <i>each
additional</i> message keep lowering violation, not just "any vs none"?</li>
</ul>
<p><b>How to read the three (five) numbers.</b> Each row's OR is the effect of messaging
in that specification. Watching the OR move from M0&rarr;M1&rarr;M2 tells us how much of
the raw effect was confounding vs a genuine independent effect; if the OR stays &lt;1 and
significant (p&lt;0.05, CI not crossing 1) after adjustment, messaging has a robust net
association with lower violation.</p>
<p><b>Is the continuous version reasonable?</b> Yes, and it complements the binary one —
but with caveats: (1) the count is heavily right-skewed (368 zeros, a long tail to 25),
so the raw-count model (M3) assumes every extra message multiplies the odds by a constant
factor, which the tail can't support well; <code>log(1+count)</code> (M4) is the more
defensible continuous form. (2) The dose-response bar chart below shows the actual shape so
we don't over-trust a single slope. (3) All of this is still observational association,
not proven causation (see caveat in the previous turn / timing analysis).</p>"""
    add("Exploratory: does interaction length relate to violation?",
        fig_html(fiter), fig_html(fmsg))
    add("Message &rarr; violation: adjusted effect (logistic regression)",
        msg_note, logit_tbl, fig_html(forest), fig_html(pred), fig_html(dose))

    # -- write ---------------------------------------------------------------
    plotlyjs = pyo.get_plotlyjs()
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Authorization Eval Analysis</title>
<script type="text/javascript">{plotlyjs}</script>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
   max-width:1100px;margin:0 auto;padding:24px;color:#222;background:#fafafa;}}
 h1{{border-bottom:3px solid #4C78A8;padding-bottom:8px;}}
 h2{{margin-top:36px;border-left:5px solid #4C78A8;padding-left:10px;color:#234;}}
 section{{background:#fff;border:1px solid #e5e5e5;border-radius:8px;
   padding:16px 20px;margin:18px 0;}}
 table.tbl{{border-collapse:collapse;margin:12px 0;font-size:14px;}}
 table.tbl th{{background:#4C78A8;color:#fff;padding:6px 12px;text-align:left;}}
 table.tbl td{{border:1px solid #ddd;padding:6px 12px;}}
 table.tbl tr:nth-child(even){{background:#f5f8fc;}}
</style></head><body>
<h1>Authorization Eval — 6 models × 84 tasks</h1>
{''.join(sections)}
</body></html>"""
    OUT_HTML.write_text(html, encoding="utf-8")
    comp_png = export_composition_png(df)
    print(f"wrote {OUT_HTML}  ({len(html)/1e6:.1f} MB)")
    print(f"wrote {CLEAN_CSV}")
    print(f"wrote {comp_png}")


if __name__ == "__main__":
    main()
