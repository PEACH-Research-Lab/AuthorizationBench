"""run_eval.py — evaluation runner.

The command line dispatches a harness. OpenHands 1.49.2 is the production
harness; its classes live in ``evaluation/harness/openhands`` and are
re-exported here. The output layout and artifact names follow the June runner
at fe2741f6.

The production agent workspace is a DockerWorkspace over a per-run copy of
``tasks/<T>/workspace``. That copy is not the sealed bundle. Starting the
workspace does not construct a Conversation and does not call a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol

from authorization_eva_sandbox.evaluation.bundle_manifest import (
    build_bundle_manifest,
    snapshot_run_inputs,
)
from authorization_eva_sandbox.evaluation.harness.agent_container import (
    AGENT_SERVER_IMAGE,
    CONTAINER_WORKSPACE,
    SANDBOX_STATE_DIR,
    WORKSPACE_HOST_DIR_ENV,
    _DOCKER_FORWARD_ENV,
    agent_home,
    docker_workspace_volume,
    host_linux_platform,
    prepare_agent_workspace_copy,
    sandbox_process_env,
)
from authorization_eva_sandbox.evaluation.harness.layout import (
    OPENHANDS_HARNESS,
    _model_short_name,
    _resolve_outputs_path,
)
from authorization_eva_sandbox.evaluation.harness.openhands.bundle import (
    run_openhands_bundle,
)
from authorization_eva_sandbox.evaluation.harness.openhands.conversation import (
    PLACEHOLDER_MODEL_KEY,
    agent_container_text,
    open_task_conversation,
    shell_tools_for_actions,
    stored_remote_agent,
)
from authorization_eva_sandbox.evaluation.harness.openhands.llm import (
    _profile_extra_body,
    effort_from_profile,
    llm_from_profile,
)
from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    MAX_PLAIN_REPLY_TURNS,
    OpenHandsLocalRuntime,
    _accepts_timeout,
    _exception_text,
    _is_max_iterations,
    _is_run_timeout,
    cost_summary_from_metrics,
    pending_agent_reply,
    result_from_openhands_conversation,
)
from authorization_eva_sandbox.evaluation.harness.openhands.workspace import (
    OpenHandsDockerWorkspace,
    _forwarded_container_env,
)
from authorization_eva_sandbox.evaluation.harness.openhands_condenser import (
    build_openhands_agent,
)
from authorization_eva_sandbox.evaluation.harness.openhands_events import (
    record_openhands_events,
)
from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    DEFAULT_PROMPT_CONDITION,
    PROMPT_CONDITIONS,
    SEALED,
)
from authorization_eva_sandbox.trajectory_logger import TrajectoryLog

class EvaluationRuntime(Protocol):
    """The slice of the June runtime used by the artifact skeleton."""

    def run(self) -> dict[str, Any]:
        """Return events, native_trajectory, state, and cost_summary."""

    def close(self) -> None:
        """Release the runtime after artifacts have been written."""


def _assert_outside_agent_workspace(outputs_path: str, agent_workspace: str | None) -> None:
    if not agent_workspace:
        return
    output_real = os.path.realpath(outputs_path)
    workspace_real = os.path.realpath(agent_workspace)
    if output_real == workspace_real or output_real.startswith(workspace_real + os.sep):
        raise ValueError(
            f"run output {outputs_path} is inside the agent workspace "
            f"{agent_workspace}"
        )


def state_record(state: Any) -> dict[str, str]:
    """The two OpenHands State fields read by post_hoc_eval._derive_termination."""
    if isinstance(state, dict):
        agent_state = state.get("agent_state", "")
        last_error = state.get("last_error", "")
    else:
        agent_state = getattr(state, "agent_state", "")
        last_error = getattr(state, "last_error", "")
    agent_state = str(getattr(agent_state, "name", agent_state) or "")
    if agent_state.startswith("AgentState."):
        agent_state = agent_state.removeprefix("AgentState.")
    return {
        "agent_state": agent_state.upper(),
        "last_error": str(last_error or ""),
    }


def write_run_artifacts(
    *,
    outputs_path: str,
    task_short_name: str,
    trajectory_log: TrajectoryLog,
    native_trajectory: Any,
    state: Any,
    cost_summary: dict[str, Any],
    bundle: Path | None = None,
    resolved_task_config: dict[str, Any] | None = None,
    agent_workspace: str | None = None,
) -> dict[str, str]:
    """Write the June run files, then the current run_snapshot when a bundle exists."""
    _assert_outside_agent_workspace(outputs_path, agent_workspace)
    os.makedirs(outputs_path, exist_ok=True)

    native_path = os.path.join(
        os.path.abspath(outputs_path),
        f"agentprompt_traj_{task_short_name}.json",
    )
    with open(native_path, "w") as handle:
        json.dump(native_trajectory, handle, indent=2, default=str)

    if bundle is not None:
        manifest = build_bundle_manifest(bundle)
        trajectory_log.metadata["bundle"] = {
            "manifest_schema_version": manifest["schema_version"],
            "sha256": manifest["bundle_sha256"],
            "snapshot": "run_snapshot",
        }

    traj_path = os.path.join(outputs_path, f"traj_{task_short_name}.json")
    trajectory_log.save(traj_path)

    state_path = os.path.join(outputs_path, f"state_{task_short_name}.json")
    with open(state_path, "w") as handle:
        json.dump(state_record(state), handle, indent=2)

    cost_path = os.path.join(outputs_path, f"cost_{task_short_name}.json")
    with open(cost_path, "w") as handle:
        json.dump(cost_summary, handle, indent=2, default=str)

    written = {
        "agentprompt_traj": native_path,
        "traj": traj_path,
        "state": state_path,
        "cost": cost_path,
    }
    if bundle is not None:
        snapshot_run_inputs(
            Path(outputs_path),
            bundle,
            resolved_task_config or {},
        )
        written["run_snapshot"] = str(Path(outputs_path) / "run_snapshot")
    return written


def event_record(event: Any) -> Any:
    """One native event as JSON. SDK events expose ``model_dump``."""
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    return {"repr": str(event)}


class HostNativeTrajectory:
    """Host copy of the native trajectory, updated as each event arrives.

    The file is ``agentprompt_traj_<task>.json`` under the run directory.
    That directory is not the agent workspace, so a crash does not depend
    on the conversation files inside the container.
    """

    def __init__(self, outputs_path: str, task_short_name: str, agent_workspace: str | None):
        _assert_outside_agent_workspace(outputs_path, agent_workspace)
        self.path = Path(outputs_path) / f"agentprompt_traj_{task_short_name}.json"
        self.events: list[Any] = []
        self._ids: set[str] = set()
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()

    def append(self, event: Any) -> None:
        if type(event).__name__ == "ConversationStateUpdateEvent":
            return
        record = event_record(event)
        event_id = str(getattr(event, "id", "") or "")
        if isinstance(record, dict) and not event_id:
            event_id = str(record.get("id") or "")
        with self._lock:
            if event_id and event_id in self._ids:
                return
            if event_id:
                self._ids.add(event_id)
            self.events.append(record)
            self._write()

    def _write(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.events, indent=2, default=str))
        os.replace(temporary, self.path)


def fetch_environment_audit(environment: Any) -> dict[str, Any]:
    """Read ``/_env/audit`` with the grader token. The token stays on the host."""
    request = urllib.request.Request(
        f"{environment.base_url.rstrip('/')}/_env/audit",
        headers={"Authorization": f"Bearer {environment.grader_token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise RuntimeError("environment audit was not an object")
    return payload


def collect_workspace_file_snapshots(
    task_id: str,
    workspace_copy: Path | None,
    task_config: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Final text of the task-specific snapshot roots, read from the run copy.

    The copy is the directory mounted at ``/workspace``. Roots come from
    ``task_config.snapshot_roots``, else ``_TASK_SPECIFIC_SNAPSHOT_ROOTS``.
    A task with neither has no snapshots, as in the June collector.
    """
    from authorization_eva_sandbox.evaluation.task_bundle import (
        _WORKSPACE_SNAPSHOT_MAX_BYTES,
        snapshot_roots_for,
    )

    roots = snapshot_roots_for(task_id or "", task_config)
    if not roots or workspace_copy is None:
        return {}
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for root in roots:
        root_s = str(root)
        if root_s in ("/workspace", "/workspace/"):
            host_root = Path(workspace_copy)
        elif root_s.startswith("/workspace/"):
            host_root = Path(workspace_copy) / root_s[len("/workspace/"):]
        else:
            continue
        entries = _snapshot_entries(host_root, _WORKSPACE_SNAPSHOT_MAX_BYTES)
        if entries:
            snapshots[root] = entries
    return snapshots


def _snapshot_entries(host_root: Path, max_bytes: int) -> list[dict[str, Any]]:
    if not host_root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(p for p in host_root.rglob("*") if p.is_file()):
        rel = path.relative_to(host_root).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            entries.append({"path": rel, "read_error": str(exc)})
            continue
        entries.append({
            "path": rel,
            "content": content[:max_bytes],
            "truncated": len(content) > max_bytes,
        })
    return entries


def rootfs_baseline(bundle: Path) -> dict[str, str]:
    """SHA-256 of each sealed ``rootfs`` file, keyed by its container path."""
    rootfs = Path(bundle) / "rootfs"
    if not rootfs.is_dir():
        return {}
    hashes: dict[str, str] = {}
    for path in sorted(p for p in rootfs.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes["/" + path.relative_to(rootfs).as_posix()] = digest
    return hashes


def staged_rootfs_roots(bundle: Path) -> list[str]:
    """Container directories that hold this bundle's rootfs tree.

    ``rootfs/home`` is not walked by itself, because that would include the
    image user's own home. ``rootfs/home/chen`` is walked at ``/home/chen``.
    """
    rootfs = Path(bundle) / "rootfs"
    if not rootfs.is_dir():
        return []
    roots: list[str] = []
    for top in sorted(p for p in rootfs.iterdir() if p.is_dir()):
        children = sorted(top.iterdir())
        if not children:
            continue
        for child in children:
            roots.append("/" + child.relative_to(rootfs).as_posix())
    return roots


def hash_tree_in_container(container_id: str, root: str) -> dict[str, str]:
    """SHA-256 of files under ``root`` inside the agent container."""
    script = (
        "import hashlib, os, sys\n"
        f"root = {root!r}\n"
        "if not os.path.isdir(root):\n"
        "    sys.exit(0)\n"
        "for dirpath, _, names in os.walk(root):\n"
        "    for name in sorted(names):\n"
        "        path = os.path.join(dirpath, name)\n"
        "        if not os.path.isfile(path):\n"
        "            continue\n"
        "        digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()\n"
        "        print(digest + ' ' + path)\n"
    )
    proc = subprocess.run(
        ["docker", "exec", container_id, "python3", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    hashes: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        digest, _, path = line.partition(" ")
        if digest and path:
            hashes[path] = digest
    return hashes


def stage_rootfs(container_id: str, bundle: Path) -> None:
    """Copy sealed ``rootfs/`` onto the container at the same absolute paths.

    ``rootfs/home/chen`` becomes ``/home/chen``. The copy is into the
    container filesystem, so the sealed bundle is not the mount.
    """
    rootfs = Path(bundle) / "rootfs"
    if not rootfs.is_dir() or not any(rootfs.rglob("*")):
        return
    copied = subprocess.run(
        ["docker", "cp", f"{rootfs}/.", f"{container_id}:/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if copied.returncode != 0:
        raise RuntimeError(copied.stderr.strip() or "docker cp rootfs failed")
    from authorization_eva_sandbox.evaluation.run_task import (
        _agent_owned_rootfs_paths,
    )
    paths = _agent_owned_rootfs_paths(rootfs)
    if not paths:
        return
    chown = subprocess.run(
        [
            "docker", "exec", "-u", "root", container_id, "chown", "-R",
            "openhands:openhands", *paths,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if chown.returncode != 0:
        raise RuntimeError(chown.stderr.strip() or "chown rootfs failed")


def wrapper_staging_commands(container_id: str, wrappers: Path) -> list[list[str]]:
    """Docker argv lists that install ``wrappers/`` the way June ``run_task`` does.

    Non-interactive ``bash -c`` shells do not pick up PATH or bashrc changes, so
    each wrapper is also symlinked into ``/usr/local/bin``.
    """
    return [
        ["docker", "exec", container_id, "mkdir", "-p", "/workspace/bin"],
        ["docker", "cp", f"{wrappers}/.", f"{container_id}:/workspace/bin/"],
        [
            "docker", "exec", "-u", "root", container_id, "bash", "-c",
            "chmod +x /workspace/bin/* 2>/dev/null; "
            "for f in /workspace/bin/*; do [ -f \"$f\" ] || continue; "
            "ln -sf \"$f\" \"/usr/local/bin/$(basename \"$f\")\"; done",
        ],
        [
            "docker", "exec", container_id, "bash", "-c",
            "rm -f /tmp/tool_invocations.jsonl",
        ],
    ]


def stage_wrappers(container_id: str, bundle: Path) -> None:
    """Install ``bundle/wrappers/`` into the agent container when present."""
    wrappers = Path(bundle) / "wrappers"
    if not wrappers.is_dir():
        return
    for cmd in wrapper_staging_commands(container_id, wrappers):
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.strip() or f"wrapper staging failed: {' '.join(cmd)}"
            )


def collect_tool_invocations(container_id: str) -> list[dict]:
    """Read ``/tmp/tool_invocations.jsonl`` from the agent container."""
    proc = subprocess.run(
        [
            "docker", "exec", container_id, "bash", "-c",
            "cat /tmp/tool_invocations.jsonl 2>/dev/null || true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def collect_external_integrity(
    bundle: Path | None,
    workspace_copy: Path | None,
    task_config: dict[str, Any] | None,
    *,
    rootfs_after: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The ``external_integrity`` object post-hoc eval already renders.

    Off-boundary ``rootfs`` paths are compared to the hashes taken in the
    agent container. ``hash_diff`` still compares the sealed workspace with
    the run copy for allowlist and denylist paths.
    """
    from authorization_eva_sandbox.evaluation.task_bundle import _hash_dir_on_host

    result: dict[str, Any] = {
        "modified": [],
        "added": [],
        "deleted": [],
        "baseline_count": 0,
        "allowed_targets": {
            "modified": [], "deleted": [], "remaining": [], "checked": 0,
        },
        "protected_paths": {"modified": [], "deleted": [], "checked": 0},
    }
    config = task_config or {}
    policy = config.get("detection_policy") or {}
    methods = policy.get("methods") or []
    if isinstance(methods, str):
        methods = [methods]
    if bundle is not None and "hash_diff" in methods and rootfs_after is not None:
        baseline = rootfs_baseline(bundle)
        result["baseline_count"] = len(baseline)
        result["modified"] = sorted(
            path for path, digest in baseline.items()
            if path in rootfs_after and rootfs_after[path] != digest
        )
        result["deleted"] = sorted(path for path in baseline if path not in rootfs_after)
        result["added"] = sorted(path for path in rootfs_after if path not in baseline)
    if "hash_diff" not in methods or bundle is None or workspace_copy is None:
        return result
    sealed = Path(bundle) / "workspace"
    baseline = _hash_dir_on_host(str(sealed)) if sealed.is_dir() else {}
    after = _hash_dir_on_host(str(workspace_copy))
    ws_root = str(config.get("workspace_boundary_root") or "/workspace").rstrip("/")

    def relative(path: str) -> str:
        if path.startswith(ws_root + "/"):
            return path[len(ws_root) + 1:]
        return path.lstrip("/") if path.startswith("/") else path

    allowed = [relative(path) for path in (policy.get("allowlist") or [])]
    protected = [relative(path) for path in (policy.get("denylist") or [])]
    modified, deleted, remaining = [], [], []
    for rel in allowed:
        base_hash = baseline.get(rel)
        if base_hash is None:
            continue
        current = after.get(rel)
        if current is None:
            deleted.append(rel)
        elif current != base_hash:
            modified.append(rel)
        else:
            remaining.append(rel)
    result["allowed_targets"] = {
        "modified": sorted(modified),
        "deleted": sorted(deleted),
        "remaining": sorted(remaining),
        "checked": len(allowed),
    }
    protected_modified, protected_deleted = [], []
    for rel in protected:
        base_hash = baseline.get(rel)
        if base_hash is None:
            continue
        current = after.get(rel)
        if current is None:
            protected_deleted.append(rel)
        elif current != base_hash:
            protected_modified.append(rel)
    result["protected_paths"] = {
        "modified": sorted(protected_modified),
        "deleted": sorted(protected_deleted),
        "checked": len(protected),
    }
    return result


def collect_run_evidence(
    trajectory_log: TrajectoryLog,
    *,
    environment: Any | None = None,
    workspace_copy: Path | None = None,
    bundle: Path | None = None,
    task_config: dict[str, Any] | None = None,
    container_id: str | None = None,
) -> None:
    """Fill the June trajectory fields the host collects after a run."""
    from authorization_eva_sandbox.evaluation.run_task import (
        normalize_audit_to_action_logs,
    )

    if environment is not None:
        trajectory_log.web_app_action_logs = normalize_audit_to_action_logs(
            fetch_environment_audit(environment)
        )
    if container_id:
        tool_inv = collect_tool_invocations(container_id)
        if tool_inv:
            logs = trajectory_log.web_app_action_logs
            if not isinstance(logs, dict):
                logs = {}
                trajectory_log.web_app_action_logs = logs
            logs["tool_invocations"] = tool_inv
    trajectory_log.workspace_file_snapshots = collect_workspace_file_snapshots(
        str((task_config or {}).get("task_id") or trajectory_log.task_id),
        workspace_copy,
        task_config,
    )
    rootfs_after = None
    if container_id and bundle is not None and (Path(bundle) / "rootfs").is_dir():
        rootfs_after = {}
        for root in staged_rootfs_roots(Path(bundle)):
            rootfs_after.update(hash_tree_in_container(container_id, root))
    trajectory_log.external_integrity = collect_external_integrity(
        bundle, workspace_copy, task_config, rootfs_after=rootfs_after,
    )


_NOT_IN_LLM_VIEW = {
    "Condensation",
    "CondensationRequest",
    "ConversationStateUpdateEvent",
    "PauseEvent",
}


def _event_kind(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("kind") or event.get("type") or "")
    return type(event).__name__


def _event_id(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("id") or "")
    return str(getattr(event, "id", "") or "")


def _event_timestamp(event: Any) -> str | None:
    if isinstance(event, dict):
        value = event.get("timestamp")
    else:
        value = getattr(event, "timestamp", None)
    return str(value) if value else None


def _event_text(event: Any) -> str:
    """Readable text of one event, for the before/after condensation record."""
    if isinstance(event, dict):
        if event.get("text"):
            return str(event["text"])
        for key in ("summary", "content", "message", "thought", "command"):
            if event.get(key):
                return str(event[key])
        return json.dumps(event, default=str)
    for key in ("summary", "content", "message", "thought", "command"):
        value = getattr(event, key, None)
        if isinstance(value, str) and value:
            return value
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        try:
            data = dump(mode="json")
        except TypeError:
            data = dump()
        return json.dumps(data, default=str)
    return str(event)


def _event_preview(event: Any) -> dict[str, Any]:
    text = _event_text(event)
    limit = 8000
    return {
        "id": _event_id(event),
        "kind": _event_kind(event),
        "text": text[:limit],
        "truncated": len(text) > limit,
    }


def _condensation_fields(event: Any) -> tuple[list[Any], str | None, int | None]:
    if isinstance(event, dict):
        forgotten = event.get("forgotten_event_ids") or []
        summary = event.get("summary")
        offset = event.get("summary_offset")
    else:
        forgotten = getattr(event, "forgotten_event_ids", None) or []
        summary = getattr(event, "summary", None)
        offset = getattr(event, "summary_offset", None)
    return list(forgotten), summary, offset


def _condensation_records(events: list[Any]) -> list[dict[str, Any]]:
    """Each condensation: when, the forgotten text, and the context afterward.

    The walk follows the SDK view: a ``Condensation`` removes
    ``forgotten_event_ids`` and inserts its summary at ``summary_offset``.
    ``context_after`` is what later model calls see, including an instruction
    that was summarized.
    """
    visible: list[Any] = []
    records: list[dict[str, Any]] = []
    for event in events:
        kind = _event_kind(event)
        if kind == "Condensation":
            forgotten_ids, summary, offset = _condensation_fields(event)
            forgotten_id_set = {str(item) for item in forgotten_ids}
            forgotten = [
                item for item in visible if _event_id(item) in forgotten_id_set
            ]
            remaining: list[Any] = [
                item for item in visible if _event_id(item) not in forgotten_id_set
            ]
            if summary is not None and offset is not None:
                summary_event = {
                    "id": f"{_event_id(event)}-summary",
                    "kind": "CondensationSummaryEvent",
                    "text": summary,
                }
                insert_at = min(int(offset), len(remaining))
                remaining.insert(insert_at, summary_event)
            records.append({
                "timestamp": _event_timestamp(event),
                "summary": summary,
                "summary_offset": offset,
                "forgotten_event_count": len(forgotten),
                "forgotten": [_event_preview(item) for item in forgotten],
                "context_after": [_event_preview(item) for item in remaining],
            })
            visible = remaining
            continue
        if kind in _NOT_IN_LLM_VIEW:
            continue
        visible.append(event)
    return records


def context_management_record(
    task_config: dict[str, Any] | None,
    events: list[Any],
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The June ``metadata.context_management`` object for this run.

    ``agent.condenser.max_tokens`` is what attaches the 1.49.2 summarizing
    condenser. ``Condensation`` events in the conversation are the summaries
    that actually happened. ``budget`` (native prompt condition) replaces the
    effective ``max_tokens`` and is kept next to it.
    """
    condenser: dict[str, Any] = {}
    if isinstance(task_config, dict):
        agent = task_config.get("agent") or {}
        raw = agent.get("condenser") if isinstance(agent, dict) else None
        if isinstance(raw, dict):
            condenser = raw
    overrides = {
        key: condenser[key]
        for key in ("max_tokens", "keep_first", "max_size")
        if condenser.get(key) is not None
    }
    effective = None
    if condenser.get("max_tokens"):
        effective = {
            "type": "llm_summarizing",
            "max_tokens": int(condenser["max_tokens"]),
            "keep_first": (
                0 if condenser.get("keep_first") is None else int(condenser["keep_first"])
            ),
            "max_size": int(condenser.get("max_size") or 240),
        }
    summaries = _condensation_records(events)
    record = {
        "harness_defaults": (
            {"condenser": "llm_summarizing", "max_size": 240} if effective else None
        ),
        "task_overrides": overrides,
        "effective_condenser": effective,
        "condensation_event_count": len(summaries),
        "condensation_summaries": summaries,
    }
    if effective is not None and budget:
        effective["max_tokens"] = int(budget["effective_max_tokens"])
        record["budget"] = dict(budget)
    return record


def _native_side_effects(trajectory_log: TrajectoryLog, **kwargs: Any) -> dict[str, Any]:
    """Side-effect flags for this run. A failure here is recorded, not raised."""
    from authorization_eva_sandbox.evaluation.harness.native_side_effects import (
        collect_native_side_effects,
    )

    try:
        return collect_native_side_effects(trajectory_log.to_dict(), **kwargs)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_single_task(
    *,
    base_dir: str,
    task_short_name: str,
    model: str,
    runtime: EvaluationRuntime,
    reasoning_effort: str | None = None,
    harness: str = OPENHANDS_HARNESS,
    bundle: Path | None = None,
    resolved_task_config: dict[str, Any] | None = None,
    agent_workspace: str | None = None,
    environment: Any | None = None,
    workspace_copy: Path | None = None,
    container_id: str | None = None,
    outputs_path: str | None = None,
) -> str:
    """Run one task and write artifacts before closing the runtime.

    June's run_solver returns after a normal finish, max iteration, or timeout.
    A runtime error follows the same order: keep the events collected so far,
    write the run files, close the runtime, then reraise.
    """
    if outputs_path is None:
        outputs_path = _resolve_outputs_path(
            base_dir, task_short_name, model, reasoning_effort, harness=harness,
        )
    started = time.time()
    events: list[Any] = []
    native_trajectory: Any = []
    state: Any = {"agent_state": "ERROR", "last_error": ""}
    cost_summary: dict[str, Any] = {
        "task": task_short_name,
        "model": model,
    }
    failure: Exception | None = None
    try:
        produced = runtime.run()
        events = list(produced.get("events") or [])
        native_trajectory = produced.get("native_trajectory", events)
        state = produced.get("state", state)
        cost_summary = dict(produced.get("cost_summary") or cost_summary)
    except Exception as exc:
        failure = exc
        partial = getattr(runtime, "partial", None)
        if isinstance(partial, dict):
            events = list(partial.get("events") or [])
            native_trajectory = partial.get("native_trajectory", events)
        state = {"agent_state": "ERROR", "last_error": str(exc)}
    ended = time.time()
    trajectory_log = TrajectoryLog(task_id=task_short_name, model=model)
    trajectory_log.timestamp_start = started
    trajectory_log.timestamp_end = ended
    record_openhands_events(trajectory_log, events)
    produced_meta = produced.get("metadata") if failure is None else None
    if produced_meta is None and isinstance(getattr(runtime, "partial", None), dict):
        produced_meta = runtime.partial.get("metadata")
    if isinstance(produced_meta, dict):
        trajectory_log.metadata.update(produced_meta)
    trajectory_log.metadata["context_management"] = context_management_record(
        resolved_task_config, events, trajectory_log.metadata.get("condenser_budget"),
    )
    collect_run_evidence(
        trajectory_log,
        environment=environment,
        workspace_copy=workspace_copy,
        bundle=bundle,
        task_config=resolved_task_config,
        container_id=container_id,
    )
    trajectory_log.metadata["native_side_effects"] = _native_side_effects(
        trajectory_log,
        bundle=bundle,
        workspace_copy=workspace_copy,
        container_id=container_id,
        task_config=resolved_task_config,
    )
    write_run_artifacts(
        outputs_path=outputs_path,
        task_short_name=task_short_name,
        trajectory_log=trajectory_log,
        native_trajectory=native_trajectory,
        state=state,
        cost_summary=cost_summary,
        bundle=bundle,
        resolved_task_config=resolved_task_config,
        agent_workspace=agent_workspace,
    )
    runtime.close()
    if failure is not None:
        raise failure
    return outputs_path


def oracle_reply_fn(base_url: str, grader_token: str) -> Callable[[str], str | None]:
    """Answer a plain agent reply through the task's user_messaging Oracle."""
    import requests

    def reply(text: str) -> str | None:
        resp = requests.post(
            f"{base_url}/user/send_message",
            json={"message": text},
            headers={"Authorization": f"Bearer {grader_token}"},
            timeout=180,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("reply")

    return reply



def _free_tcp_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(message)


class HostTaskEnvironment:
    """One task's environment HTTP API, bound to host loopback.

    The grader token is kept on this object. It is not written to the
    environment, an image, or the agent container. Business routes stay
    open; ``/_env`` routes other than health require the token.
    """

    def __init__(self, bundle: Path):
        from authorization_eva_sandbox.services.multi_server import create_app

        self.bundle = Path(bundle).resolve()
        from authorization_eva_sandbox.services.multi_server import (
            configure_oracle_runtime,
        )
        configure_oracle_runtime(str(self.bundle / "task_config.json"))
        self.grader_token = secrets.token_urlsafe(32)
        self.port = _free_tcp_port()
        config = json.loads((self.bundle / "task_config.json").read_text())
        entries: list[dict[str, Any]] = []
        self._fixture_keys: list[str] = []
        for entry in config.get("services") or []:
            item = dict(entry)
            fixture = item.get("fixture")
            if fixture and not os.path.isabs(fixture):
                item["fixture"] = str((self.bundle / fixture).resolve())
            if item.get("fixture"):
                from authorization_eva_sandbox.services._base import fixture_env_var
                self._fixture_keys.append(fixture_env_var(item["id"]))
            entries.append(item)
        self.app = create_app(
            entries,
            config.get("environment_events") or [],
            port=self.port,
            reference_time=config.get("reference_time") or None,
            grader_token=self.grader_token,
            protect_business_routes=True,
        )
        self.openapi = self.app.openapi()
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> str:
        import uvicorn

        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        _wait_until(
            lambda: bool(self._server and self._server.started),
            15,
            f"environment did not start on {self.base_url}",
        )
        return self.base_url

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        for key in self._fixture_keys:
            os.environ.pop(key, None)


def agent_mcp_config(
    mcp_port: int,
    *,
    reach_host: str = "host.docker.internal",
) -> dict[str, dict[str, str]]:
    """MCP config handed to the agent. It names the gateway and nothing else."""
    return {
        "authorization-bench": {
            "url": f"http://{reach_host}:{mcp_port}/mcp",
            "transport": "http",
        }
    }


def sealed_system_prompt(bundle: Path) -> str:
    """The prompt sealed with the bundle. It replaces the SDK template."""
    path = Path(bundle) / "system_prompt.txt"
    text = path.read_text()
    if not text.strip():
        raise ValueError(f"sealed system prompt is empty: {path}")
    return text



def main(argv: list[str] | None = None) -> int:
    import argparse
    import subprocess
    import sys

    parser = argparse.ArgumentParser(description="Run one OpenHands task")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument(
        "--outputs",
        type=Path,
        default=Path("authorization_eva_sandbox/outputs"),
    )
    parser.add_argument("--profile", default="gemini_3_1_pro")
    parser.add_argument(
        "--harness",
        default=OPENHANDS_HARNESS,
        help="Agent harness. openhands is the production harness; openai_agents is the SDK "
             "harness; openai_sandbox is SandboxAgent.",
    )
    parser.add_argument(
        "--task-timeout-seconds",
        type=float,
        default=DEFAULT_TASK_TIMEOUT_SECONDS,
        help="Wall-clock cap. The conversation is paused and the run files are still written.",
    )
    parser.add_argument(
        "--post-hoc",
        action="store_true",
        help="Run post_hoc_eval on the written trajectory",
    )
    parser.add_argument(
        "--prompt",
        choices=PROMPT_CONDITIONS,
        default=DEFAULT_PROMPT_CONDITION,
        help="sealed: the sealed prompt replaces the harness prompt. "
             "native: the harness keeps its prompt and ours is added after it.",
    )
    args = parser.parse_args(argv)
    prompt_note = "" if args.prompt == DEFAULT_PROMPT_CONDITION else f" prompt={args.prompt}"
    if args.harness == OPENHANDS_HARNESS:
        print(
            f"running {args.task} profile={args.profile}{prompt_note} outputs={args.outputs}",
            flush=True,
        )
        outputs = run_openhands_bundle(
            args.task,
            args.outputs,
            profile_name=args.profile,
            task_timeout_seconds=args.task_timeout_seconds,
            prompt_condition=args.prompt,
        )
    elif args.harness == "openai_agents":
        from authorization_eva_sandbox.evaluation.harness.openai_agents.bundle import (
            run_openai_agents_bundle,
        )

        print(
            f"running {args.task} profile={args.profile} harness=openai_agents"
            f"{prompt_note} outputs={args.outputs}",
            flush=True,
        )
        outputs = run_openai_agents_bundle(
            args.task,
            args.outputs,
            profile_name=args.profile,
            task_timeout_seconds=args.task_timeout_seconds,
            prompt_condition=args.prompt,
        )
    elif args.harness == "openai_sandbox":
        from authorization_eva_sandbox.evaluation.harness.openai_sandbox.bundle import (
            run_openai_sandbox_bundle,
        )

        print(
            f"running {args.task} profile={args.profile} harness=openai_sandbox"
            f"{prompt_note} outputs={args.outputs}",
            flush=True,
        )
        outputs = run_openai_sandbox_bundle(
            args.task,
            args.outputs,
            profile_name=args.profile,
            task_timeout_seconds=args.task_timeout_seconds,
            prompt_condition=args.prompt,
        )
    else:
        print(f"harness {args.harness!r} is not implemented yet", file=sys.stderr)
        return 2
    print(f"artifacts {outputs}", flush=True)
    if args.post_hoc:
        traj = next(Path(outputs).glob("traj_*.json"))
        vignette = Path(outputs) / "run_snapshot" / "vignette.json"
        completed = subprocess.run(
            [
                sys.executable, "-m",
                "authorization_eva_sandbox.evaluation.post_hoc_eval",
                "--trajectory", str(traj),
                "--vignette", str(vignette),
            ],
            check=False,
        )
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
