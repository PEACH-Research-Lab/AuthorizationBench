"""
traj_renderer.py — pure-script preprocessor that converts a `traj_*.json`
into a chronological markdown digest used as the `${trajectory}` placeholder
in judge prompts.

See README.md §2.5 for the design.

No LLM calls happen here. Same input -> same output.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# Per-event payload renderers ------------------------------------------------

_BASH_OUTPUT_TRUNCATE = 200
_IPYTHON_TRUNCATE = 1200
_AGENTPROMPT_SNIPPET_LIMIT = 1200
_AGENTPROMPT_MAX_SNIPPETS = 3
# A normal report is well under this. Longer bodies stay in the trajectory.
_DISPLAY_TEXT_LIMIT = 20_000


def _norm(s: Any) -> str:
    """Single-line, whitespace-collapsed string for compact rendering."""
    if s is None:
        return ""
    return " ".join(str(s).split())


def _render_user_message(data: dict) -> str:
    return _norm(data.get("content", ""))


def _render_agent_message(data: dict) -> str:
    return _norm(data.get("content", ""))


def _render_agent_think(data: dict) -> str:
    return _norm(data.get("content") or data.get("thought", ""))


def _render_terminal_command(data: dict) -> str:
    cmd = _norm(data.get("command", ""))
    exit_code = data.get("exit_code", 0)
    output = (data.get("output") or "").strip()
    out_part = ""
    if output:
        truncated = output[:_BASH_OUTPUT_TRUNCATE]
        if len(output) > _BASH_OUTPUT_TRUNCATE:
            truncated += " …(truncated)"
        out_part = f"  output: {_norm(truncated)}"
    return f"{cmd} (exit={exit_code}){out_part}"


def _render_ipython_cell(data: dict) -> str:
    code = str(data.get("code") or "").strip()
    if len(code) > _IPYTHON_TRUNCATE:
        code = code[:_IPYTHON_TRUNCATE].rstrip() + " …(truncated)"
    thought = _norm(data.get("thought", ""))
    prefix = f"thought={thought}  " if thought else ""
    return prefix + "code:\n" + code


def _render_ipython_output(data: dict) -> str:
    code = str(data.get("code") or "").strip()
    output = str(data.get("output") or "").strip()
    if len(code) > _IPYTHON_TRUNCATE:
        code = code[:_IPYTHON_TRUNCATE].rstrip() + " …(truncated)"
    if len(output) > _IPYTHON_TRUNCATE:
        output = output[:_IPYTHON_TRUNCATE].rstrip() + " …(truncated)"
    parts = []
    if code:
        parts.append("code:\n" + code)
    if output:
        parts.append("output:\n" + output)
    return "\n".join(parts) if parts else "(empty IPython output)"


def _render_browser_action(data: dict) -> str:
    action = _norm(data.get("action", ""))
    url = _norm(data.get("url", ""))
    if url:
        return f"{action}  url={url}"
    return action


def _render_tool_call(data: dict) -> str:
    """v2 (trajectory-log-v2.md): one environment-API tool invocation."""
    tool = _norm(data.get("tool", ""))
    service = _norm(data.get("service", ""))
    status = data.get("status", "")
    args = data.get("arguments") or {}
    try:
        args_str = _norm(json.dumps(args, ensure_ascii=False))
    except (TypeError, ValueError):
        args_str = _norm(args)
    if len(args_str) > 800:
        args_str = args_str[:800] + " …(truncated)"
    result = str(data.get("result") or "").strip()
    if len(result) > _BASH_OUTPUT_TRUNCATE:
        result = result[:_BASH_OUTPUT_TRUNCATE] + " …(truncated)"
    parts = [f"{tool}"]
    if service:
        parts.append(f"[{service}]")
    parts.append(f"args={args_str}")
    if status != "":
        parts.append(f"(status={status})")
    line = " ".join(parts)
    if result:
        line += f"  result: {_norm(result)}"
    return line


def _clip_display(text: str, limit: int = _DISPLAY_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit].rstrip() + f"\n…(truncated for display, {omitted} chars omitted)"


def attachment_evidence_text(att: dict) -> str:
    """Readable attachment body. PDF extracted text, never raw base64."""
    if not isinstance(att, dict):
        return ""
    text = att.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = att.get("content")
    if isinstance(content, str) and content:
        return content
    if att.get("content_base64"):
        kind = att.get("media_type") or "binary"
        size = att.get("size", "?")
        return f"(binary {kind}, {size} bytes; no extracted text)"
    return ""


def _render_file_edit(data: dict) -> str:
    """Render a file_edit step as a unified-style diff so it is unambiguous
    to the judge which lines were actually changed by the edit. We avoid
    quoting raw old_str / new_str blobs because models routinely mistake the
    presence of a column header inside new_str (which was already in old_str)
    for an edit to that column. A write/create shows the new file text."""
    import difflib

    path = _norm(data.get("path", ""))
    cmd = _norm(data.get("command", "") or "edit")
    parts = [f"{cmd} {path}"]

    old = data.get("old_str", "") or ""
    new = data.get("new_str", "") or ""
    file_text = data.get("file_text", "") or ""
    insert_line = data.get("insert_line", None)

    if old or new:
        old_lines = old.splitlines() or [""]
        new_lines = new.splitlines() or [""]
        diff = list(difflib.unified_diff(
            old_lines, new_lines, lineterm="", n=0
        ))
        # Strip the @@ hunk headers and +++/--- file headers; keep only
        # the +/- payload so the judge sees what actually changed.
        changed = [ln for ln in diff if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
        if not changed:
            parts.append("(no-op edit: old_str == new_str)")
        else:
            preview = _clip_display("\n".join(changed), 4000)
            parts.append("diff (lines that changed):\n" + preview)
    if file_text:
        parts.append("file contents:\n" + _clip_display(str(file_text)))
    if data.get("content_truncated"):
        parts.append("(file content truncated in the trajectory)")
    if insert_line is not None:
        parts.append(f"insert_line={insert_line}")
    return "\n".join(parts)


def _render_file_read(data: dict) -> str:
    path = _norm(data.get("path", ""))
    start = data.get("start", 0)
    end = data.get("end", -1)
    return f"read {path} (lines {start}..{end})"


def _render_webapp_entry(source: str, entry: dict) -> str:
    """Render one webapp/wrapper action_log entry into a readable block."""
    action = entry.get("action", "<unknown>")
    attachment_contents = entry.get("attachment_contents")
    skip_keys = {"action", "timestamp", "template", "attachment_contents"}
    parts = [str(action)]
    for k, v in entry.items():
        if k in skip_keys:
            continue
        parts.append(f"{k}={_norm(v)}")
    header = " ".join(parts)
    if not attachment_contents or not isinstance(attachment_contents, list):
        return header
    blocks = [header]
    for att in attachment_contents:
        if not isinstance(att, dict):
            continue
        name = att.get("filename") or "<unnamed>"
        size = att.get("size")
        truncated = att.get("truncated")
        meta = f"--- attachment: {name}"
        if size is not None:
            meta += f" ({size} bytes"
            if truncated:
                meta += ", truncated in log"
            meta += ")"
        meta += " ---"
        blocks.append(meta)
        blocks.append(_clip_display(attachment_evidence_text(att)).rstrip())
    return "\n".join(blocks)


def _load_agentprompt_snippets(traj_path: str) -> list[tuple[str, str]]:
    """Extract compact supplemental evidence from sibling agentprompt traj.

    Some decisions (e.g. whether generated attachments include restricted
    columns) are visible in the richer `agentprompt_traj_*.json` sidecar but
    not in the slim `traj_*.json`. We surface short, high-signal snippets so
    the judge has concrete evidence without overwhelming context.
    """
    p = Path(traj_path)
    if not p.name.startswith("traj_"):
        return []
    sidecar = p.with_name(f"agentprompt_{p.name}")
    if not sidecar.exists():
        return []

    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []

    def _trim(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + f"\n\n…[truncated {len(text) - limit} chars]"

    snippets: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        action = _norm(entry.get("action", ""))

        # Prefer code cells that materialize outbound artifacts.
        if action == "run_ipython":
            code = ""
            args = entry.get("args")
            if isinstance(args, dict):
                code = str(args.get("code") or "")
            if not code:
                extras = entry.get("extras")
                if isinstance(extras, dict):
                    code = str(extras.get("code") or "")
            if code and ("/workspace/" in code or "to_csv(" in code):
                snippets.append(("run_ipython", _trim(code, _AGENTPROMPT_SNIPPET_LIMIT)))

        # Also include explicit file-view output that exposes source headers.
        content = entry.get("content")
        if isinstance(content, str) and "cat -n" in content and "/workspace/" in content:
            snippets.append(("file_view", _trim(content, _AGENTPROMPT_SNIPPET_LIMIT)))

        if len(snippets) >= _AGENTPROMPT_MAX_SNIPPETS:
            break

    return snippets


# Trajectory walker ----------------------------------------------------------


def _normalize_webapp_log(log: Any) -> list[dict]:
    """
    Backward-compatible parser for web_app_action_logs entries.

    New trajectories should already store list[dict] per source (normalized in
    run_eval.collect_webapp_logs). We still accept legacy dict payloads like
    {"actions": [...]} so older traj_*.json files remain renderable.
    """
    if isinstance(log, list):
        return log
    if isinstance(log, dict):
        actions = log.get("actions")
        if isinstance(actions, list):
            return actions
    return []


def _to_event_timestamp(value: Any) -> float:
    """Best-effort conversion for webapp timestamp values."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    # Handle ISO format values like "2026-05-02T10:27:51.684165".
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def _collect_events(traj: dict) -> list[tuple[float, int, str, str]]:
    """
    Walk traj_*.json and collect (sort_ts, sort_idx, type_label, payload) tuples.

    Why two sort keys?
    -----------------
    `trajectory[*]` entries currently all share the same timestamp (the
    `_populate_trajectory_from_state` rebuild assigns `time.time()` at run end
    to every step), so the list order is the only reliable ordering signal
    there. `web_app_action_logs[<source>]` entries have real per-event
    timestamps written at the moment each webapp/wrapper handled a request.

    We therefore sort by (timestamp, secondary_index) where:
      - For trajectory entries: timestamp = traj_log.timestamp_start, and
        secondary_index = the entry's list position. This makes them sort in
        their original order while still letting real webapp timestamps
        interleave correctly when they fall within the same range.
      - For webapp entries: secondary_index = a high constant so that within a
        tied timestamp, traj entries appear first (rare, mostly cosmetic).

    The overall result: webapp events with timestamps in the middle of the run
    interleave correctly with trajectory entries that were captured at run end.
    """
    events: list[tuple[float, int, str, str]] = []

    # Use timestamp_start as the anchor for trajectory entries, since their
    # individual timestamps are all set to the run-end time and are useless.
    anchor_ts = float(traj.get("timestamp_start") or 0.0)

    # 1) trajectory[*]: list order is authoritative; we synthesize per-step
    # timestamps as anchor + tiny epsilon * idx so they remain in list order
    # but real webapp timestamps still interleave naturally.
    traj_steps = traj.get("trajectory", []) or []
    end_ts = float(traj.get("timestamp_end") or anchor_ts + 1.0)
    span = max(end_ts - anchor_ts, 1.0)

    for idx, step in enumerate(traj_steps):
        # Spread trajectory steps evenly across [start, end]; this gives them
        # real-ish timestamps so webapp events with their own timestamps can
        # interleave correctly.
        ts = anchor_ts + (span * idx / max(len(traj_steps), 1))
        stype = step.get("type", "")
        data = step.get("data", {}) or {}

        if stype == "user_message":
            events.append((ts, idx, "user", _render_user_message(data)))
        elif stype == "agent_message":
            events.append((ts, idx, "agent", _render_agent_message(data)))
        elif stype == "agent_think":
            events.append((ts, idx, "agent_think", _render_agent_think(data)))
        elif stype == "terminal_command":
            events.append((ts, idx, "bash", _render_terminal_command(data)))
        elif stype == "ipython_cell":
            events.append((ts, idx, "ipython", _render_ipython_cell(data)))
        elif stype == "ipython_output":
            events.append((ts, idx, "ipython_output", _render_ipython_output(data)))
        elif stype == "browser_action":
            events.append((ts, idx, "browser", _render_browser_action(data)))
        elif stype == "tool_call":
            events.append((ts, idx, "tool", _render_tool_call(data)))
        elif stype == "file_edit":
            events.append((ts, idx, "file_edit", _render_file_edit(data)))
        elif stype == "file_read":
            events.append((ts, idx, "file_read", _render_file_read(data)))
        elif stype == "webapp_action":
            # Already represented in web_app_action_logs; skip to avoid dupes.
            continue
        elif stype == "agent_finish":
            # `final_thought` carries the agent's farewell message to the user
            # (this is what shows up in the UI as the agent's last reply).
            # `outputs` is a structured-result holder used by some agent
            # implementations; surface both, preferring final_thought when
            # present so the judge sees what the user actually got told.
            final_thought = _norm(data.get("final_thought", ""))
            outputs = _norm(data.get("outputs", ""))
            payload = final_thought or outputs or "(no farewell message)"
            events.append((ts, idx, "agent_finish", payload))
        elif stype == "ask_user":
            # v2 builtin loop logs message_user exchanges as ask_user steps
            # (v1 OpenHands trajectories used user_message/agent_message pairs
            # instead, so this branch never fires on legacy logs).
            agent_msg = _norm(data.get("agent_message", ""))
            oracle_resp = _norm(data.get("oracle_response", ""))
            if agent_msg:
                events.append((ts, idx, "agent", agent_msg))
            if oracle_resp:
                events.append((ts, idx, "user", oracle_resp))
        # npc_interaction handled elsewhere if needed

    # 2) web_app_action_logs[<source>]: real per-event timestamps.
    # We use a high secondary index so that on timestamp ties, webapp events
    # come after trajectory entries (rare but consistent).
    wal = traj.get("web_app_action_logs", {}) or {}
    for source, log in wal.items():
        for entry in _normalize_webapp_log(log):
            ts = _to_event_timestamp(entry.get("timestamp", 0.0))
            events.append((ts, 10**9, f"webapp:{source}", _render_webapp_entry(source, entry)))

    # 3) oracle_interactions: only used as a fallback when trajectory[*] is
    # empty (legacy traj_*.json with the total_steps=0 bug). When trajectory
    # is well-populated, agent/user messages are already covered there.
    if not traj_steps:
        for i, oi in enumerate(traj.get("oracle_interactions", []) or []):
            agent_msg = _norm(oi.get("agent_message", ""))
            oracle_resp = _norm(oi.get("oracle_response", ""))
            ts = anchor_ts + (span * i / max(len(traj.get("oracle_interactions", [])) or 1, 1))
            if oracle_resp:
                events.append((ts, 2 * i, "user", oracle_resp))
            if agent_msg:
                events.append((ts, 2 * i + 1, "agent", agent_msg))

    return events


# Public renderer ------------------------------------------------------------


def render_trajectory(traj: dict | str) -> str:
    """
    Render a TrajectoryLog dict (or path to a traj_*.json) into a markdown
    digest suitable for the `${trajectory}` placeholder in judge prompts.

    Output format (see README §2.5.4):

        ## Trajectory (chronological)
        [t=001 user] ...
        [t=002 webapp:email_client] ...
        [t=003 agent] ...
        ...

        ## Workspace file snapshots
        /workspace/clinic_archive:
        ### Marcus Webb/2026-04-29_session_note.md
        ...

        ## Final filesystem changes
        modified: [...]
        added: [...]
        deleted: [...]
        allowed_targets: { modified: [...], deleted: [...], remaining: [...], checked: N }
        protected_paths: { modified: [...], deleted: [...], checked: N }
    """
    traj_source_path: Optional[str] = None
    if isinstance(traj, str):
        traj_source_path = traj
        with open(traj) as f:
            traj = json.load(f)

    if not isinstance(traj, dict):
        raise TypeError(
            f"render_trajectory expects a TrajectoryLog dict or path, got {type(traj).__name__}"
        )

    events = _collect_events(traj)
    # Sort by (timestamp, secondary_index) — see _collect_events for rationale.
    events.sort(key=lambda e: (e[0], e[1]))

    lines: list[str] = ["## Trajectory (chronological)"]
    if not events:
        lines.append("(no agent activity recorded)")
    else:
        for i, (_, _, typ, payload) in enumerate(events, 1):
            lines.append(f"[t={i:03d} {typ}] {payload}")

    # Workspace file snapshots — final file contents captured after the agent
    # finishes. These are primary evidence for archive-writing tasks where the
    # clinical record's final contents matter more than generic filesystem diff.
    snapshots = traj.get("workspace_file_snapshots", {}) or {}
    if snapshots:
        lines.append("")
        lines.append("## Workspace file snapshots")
        for root, entries in snapshots.items():
            lines.append(f"### {root}")
            if not entries:
                lines.append("(no files captured)")
                continue
            for entry in entries:
                path = entry.get("path", "<unknown>")
                lines.append(f"#### {path}")
                if entry.get("read_error"):
                    lines.append(f"(read error: {entry.get('read_error')})")
                    continue
                content = entry.get("content", "")
                lines.append("```")
                lines.append(str(content).rstrip())
                lines.append("```")
                if entry.get("truncated"):
                    lines.append("(truncated)")

    # Filesystem changes — separate section, no per-event timestamp.
    lines.append("")
    lines.append("## Final filesystem changes")
    ext = traj.get("external_integrity", {}) or {}
    modified = ext.get("modified", []) or []
    added = ext.get("added", []) or []
    deleted = ext.get("deleted", []) or []
    baseline_count = int(ext.get("baseline_count") or 0)
    protected = ext.get("protected_paths", {}) or {}
    allowed = ext.get("allowed_targets", {}) or {}
    at_mod = allowed.get("modified", []) or []
    at_del = allowed.get("deleted", []) or []
    at_rem = allowed.get("remaining", []) or []
    at_chk = int(allowed.get("checked") or 0) if allowed else 0
    hp_mod = protected.get("modified", []) or []
    hp_del = protected.get("deleted", []) or []
    hp_chk = int(protected.get("checked") or 0) if protected else 0

    # If both external baseline and protected-path checks are disabled for this
    # task, avoid emitting empty lists like "deleted: []" that can be misread
    # as hard evidence of "no deletion happened".
    if baseline_count == 0 and at_chk == 0 and hp_chk == 0 and not (modified or added or deleted):
        lines.append("external_integrity: (not applicable for this task)")
    else:
        lines.append(f"baseline_count: {baseline_count}")
        lines.append(f"modified: {modified}")
        lines.append(f"added: {added}")
        lines.append(f"deleted: {deleted}")
        if allowed:
            lines.append(
                f"allowed_targets: {{ modified: {at_mod}, deleted: {at_del}, remaining: {at_rem}, checked: {at_chk} }}"
            )
        else:
            lines.append("allowed_targets: (not configured)")
        if protected:
            lines.append(
                f"protected_paths: {{ modified: {hp_mod}, deleted: {hp_del}, checked: {hp_chk} }}"
            )
        else:
            lines.append("protected_paths: (not configured)")

    # Supplement with richer execution evidence when available.
    if traj_source_path:
        snippets = _load_agentprompt_snippets(traj_source_path)
        if snippets:
            lines.append("")
            lines.append("## Supplementary execution evidence")
            lines.append(
                "Extracted from sibling `agentprompt_traj_*.json` "
                "(e.g. code used to generate outbound artifacts)."
            )
            for i, (kind, snippet) in enumerate(snippets, 1):
                lines.append(f"[s={i:03d} {kind}]")
                lines.append("```")
                lines.append(snippet)
                lines.append("```")

    return "\n".join(lines)


def render_trajectory_from_path(path: str) -> str:
    """Convenience wrapper for CLI / tests."""
    return render_trajectory(path)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m authorization_eva_sandbox.evaluation.detectors.traj_renderer <path/to/traj_*.json>")
        sys.exit(1)
    print(render_trajectory_from_path(sys.argv[1]))
