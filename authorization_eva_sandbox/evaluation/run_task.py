"""run_task.py — v2 single-task runner (successor of run_eval.py).

Replaces the OpenHands-embedded orchestration with:
  container (Dockerfile.v2, multi_server on :9100)
    + HarnessAdapter (builtin_loop by default)
    + artifact collection (audit logs, off-boundary integrity, service-state diff)

Output layout is byte-compatible with v1 so post_hoc_eval.py / analysis
tooling keep working:

    {outputs}/{model_tag}/output_full_{task}_{model_tag}_{n}/
        traj_{task}.json            TrajectoryLog v2 (harness block, tool_call steps)
        traj_stream.jsonl           live step stream for observability/debugging
        eval-tools.json             tool IR used for this run
        cost_{task}.json            token/cost summary
        service_state_{task}.json   raw before/after /_env/state snapshots

Usage:
    python run_task.py --task ../tasks/T0012-b-c-n \\
        --agent-profile sonnet --env-profile deepseek_v4_flash_openrouter \\
        [--outputs outputs_v2] [--max-steps N] [--image auth-sandbox:v2] \\
        [--no-native-tools] [--keep-container]

Bundle resolution helpers (vignette refs, user identity, instruction) live
in task_bundle.py (extracted from the retired v1 runner run_eval.py).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any, Optional

import requests

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(EVAL_DIR.parent))

from task_bundle import (  # noqa: E402
    load_task_config,
    resolve_vignette_refs,
    resolve_user_identity,
    resolve_instruction,
    _substitute_placeholders,
    _materialize_substituted_dir,
    _hash_dir_on_host,
    _model_short_name,
    _resolve_outputs_path,
    _TASK_SPECIFIC_SNAPSHOT_ROOTS,
)
from harness.base import (  # noqa: E402
    AgentTaskView,
    GraderEnvironmentClient,
    RunSpec,
)
from harness.builtin_loop import BuiltinLoopAdapter  # noqa: E402
from bundle_manifest import snapshot_run_inputs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_task")

ADAPTERS = {
    "builtin_loop": BuiltinLoopAdapter,
}


# ── v1 → v2 config shim ──────────────────────────────────────────────────────

def upgrade_task_config_v1(task_config: dict) -> dict:
    """In-memory upgrade of a v1 bundle config to the v2 runtime contract.

    Transitional: lets the 84 existing v1 tasks run on the v2 stack before
    the Phase-4 regeneration produces native v2 bundles. Mapping:
      platforms[]              -> services[] (data_path -> fixtures/<basename>)
      agent_runtime{max_size,keep_first} -> agent.condenser
      max_iterations           -> agent.max_steps
    """
    if task_config.get("schema_version", 1) >= 2 or task_config.get("services"):
        return task_config

    services = []
    for p in task_config.get("platforms") or []:
        entry = {"id": p["id"], "template": p.get("template", p["id"])}
        data_path = p.get("data_path") or ""
        if data_path:
            entry["fixture"] = "fixtures/" + os.path.basename(data_path)
        services.append(entry)
    task_config["services"] = services

    agent = dict(task_config.get("agent") or {})
    if "max_steps" not in agent and task_config.get("max_iterations"):
        agent["max_steps"] = task_config["max_iterations"]
    rt = task_config.get("agent_runtime")
    if "condenser" not in agent and isinstance(rt, dict) and rt.get("max_size"):
        agent["condenser"] = {"max_size": rt["max_size"],
                              "keep_first": rt.get("keep_first", 0)}
    task_config["agent"] = agent
    task_config["schema_version"] = 2
    return task_config


# ── LLM profiles (evaluation/config.toml) ────────────────────────────────────

def load_llm_profile(name: str) -> dict:
    cfg = tomllib.loads((EVAL_DIR / "config.toml").read_text())
    profile = cfg.get("llm", {}).get(name)
    if profile is None:
        raise SystemExit(f"profile [llm.{name}] not found in evaluation/config.toml")
    return profile


# ── docker helpers ───────────────────────────────────────────────────────────

def _docker(*argv: str, check: bool = True, timeout: int = 120,
            stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *argv], capture_output=True, text=True,
                          timeout=timeout, input=stdin)
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(argv[:3])}... failed "
                           f"(rc={proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hash_dir_in_container(container: str, path: str) -> dict[str, str]:
    cmd = (f"if [ -d {path} ]; then cd {path} && "
           f"find . -type f -print0 | xargs -0 sha256sum 2>/dev/null; fi")
    proc = _docker("exec", container, "bash", "-c", cmd, check=False)
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        h, p = parts
        p = p.lstrip("*")
        if p.startswith("./"):
            p = p[2:]
        if len(h) == 64 and all(c in "0123456789abcdef" for c in h.lower()):
            out[p] = h
    return out


def _agent_owned_rootfs_paths(rootfs: Path) -> list[str]:
    """Absolute bundle-authored paths safe to grant to the agent user.

    Top-level system directories such as /home, /etc, and /usr are never
    transferred; only files and nested directories explicitly present in the
    task's rootfs tree are included.
    """
    paths: list[str] = []
    for source in sorted(rootfs.rglob("*")):
        relative = source.relative_to(rootfs)
        if source.is_dir() and len(relative.parts) == 1:
            continue
        paths.append("/" + relative.as_posix())
    return paths


# ── environment lifecycle ────────────────────────────────────────────────────

class TaskEnvironment:
    """Stages one task bundle into a fresh v2 container."""

    def __init__(self, task_path: str, task_config: dict,
                 identity: dict[str, str], env_profile: dict,
                 image: str, instruction: str):
        self.task_path = Path(task_path)
        self.task_config = task_config
        self.identity = identity
        self.env_profile = env_profile
        self.image = image
        self.instruction = instruction
        self.task_id = task_config.get("task_id", self.task_path.name)
        self.container = f"auth-v2-{self.task_id}-{os.getpid()}".lower()
        self.host_port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.host_port}"
        self.grader_token = secrets.token_urlsafe(32)
        self.grader = GraderEnvironmentClient(self.base_url, self.grader_token)
        self._staging: Optional[Path] = None
        self._materialized: list[str] = []

    # -- staging --------------------------------------------------------------

    def _stage_config_dir(self) -> Path:
        """Build a root-only environment config mount.

        Agent adapters receive ``AgentTaskView`` instead. Nothing under this
        mount is part of the agent-visible filesystem contract.
        """
        staging = Path(tempfile.mkdtemp(prefix=f"v2cfg_{self.task_id}_"))
        staging.chmod(0o700)
        self._staging = staging
        private = staging / "private"
        private.mkdir(mode=0o700)
        config_path = private / "task_config.json"
        config_path.write_text(json.dumps(self.task_config, indent=2))
        config_path.chmod(0o600)

        # fixtures/ (v2 name) or data/ (v1 name), identity-substituted.
        for src_name in ("fixtures", "data"):
            src = self.task_path / src_name
            if src.is_dir():
                mat = _materialize_substituted_dir(str(src), self.identity)
                if mat != str(src):
                    self._materialized.append(mat)
                shutil.copytree(mat, private / "fixtures", dirs_exist_ok=True)
                break

        if (self.task_path / "gitea").is_dir():
            shutil.copytree(self.task_path / "gitea", private / "gitea")
            creds = self.task_path / "agent_credentials.json"
            if creds.is_file():
                shutil.copy2(creds, private / "agent_credentials.json")

        # webapp_private/ (v1 channel for scripted NPC replies etc.)
        if (self.task_path / "webapp_private").is_dir():
            shutil.copytree(self.task_path / "webapp_private",
                            private / "webapp_private")

        # Oracle / sync-NPC LLM credentials, passed via --env-file so the key
        # never appears on a command line.
        env_lines = [
            "LITELLM_MODEL=" + shlex.quote(str(self.env_profile["model"])),
            "LITELLM_BASE_URL=" + shlex.quote(
                str(self.env_profile.get("base_url") or "")
            ),
            "LITELLM_API_KEY=" + shlex.quote(
                str(self.env_profile.get("api_key") or "")
            ),
        ]
        # Profile kwargs carry over (e.g. provider pinning). Reasoning is
        # disabled for the Oracle by default: its replies are capped at ~60
        # tokens and any reasoning budget starves the visible answer into an
        # empty string (observed with DeepSeek Flash via OpenRouter). A profile
        # that declares its own `reasoning` block wins, because some models
        # (GLM 5.3 on Z.ai) reject `enabled: false` outright and instead need
        # effort=minimal + exclude.
        ck = dict(self.env_profile.get("completion_kwargs") or {})
        extra_body = dict(ck.get("extra_body") or {})
        extra_body.setdefault("reasoning", {"enabled": False})
        ck["extra_body"] = extra_body
        env_lines.append(
            "LITELLM_COMPLETION_KWARGS=" + shlex.quote(json.dumps(ck))
        )
        env_lines.append("GRADER_TOKEN=" + shlex.quote(self.grader_token))
        env_file = private / "oracle.env"
        env_file.write_text("\n".join(env_lines) + "\n")
        env_file.chmod(0o600)
        return staging

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        staging = self._stage_config_dir()
        _docker("rm", "-f", self.container, check=False)
        _docker("run", "-d", "--name", self.container,
                "-v", f"{staging}:/config:ro",
                "-p", f"{self.host_port}:9100",
                self.image, "sh", "-c", "/utils/init.sh && sleep infinity",
                timeout=180)
        self._wait_healthy()
        self._stage_materials()

    def _wait_healthy(self, timeout_s: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                r = requests.get(f"{self.base_url}/_env/health", timeout=3)
                if r.status_code == 200:
                    logger.info("environment healthy: %s", r.json())
                    return
            except requests.RequestException:
                pass
            time.sleep(1.5)
        logs = _docker("logs", self.container, check=False).stdout[-2000:]
        raise RuntimeError(f"environment failed to become healthy; "
                           f"container logs tail:\n{logs}")

    def _stage_materials(self) -> None:
        """Copy workspace/rootfs/wrappers + instruction into the container."""
        ws = self.task_path / "workspace"
        if ws.is_dir():
            mat = _materialize_substituted_dir(str(ws), self.identity)
            if mat != str(ws):
                self._materialized.append(mat)
            _docker("cp", f"{mat}/.", f"{self.container}:/workspace/")
            _docker("exec", self.container, "chown", "-R", "agent:agent",
                    "/workspace")

        # rootfs/: path-keyed staging for files whose in-world location lies
        # outside the workspace tree (bundle rootfs/<abs-path> → container
        # /<abs-path>). The authorization boundary itself is defined ONLY by
        # task_config.workspace_boundary_root (vignette file_setting), never
        # by bundle directory names.
        rootfs = self.task_path / "rootfs"
        if rootfs.is_dir():
            mat = _materialize_substituted_dir(str(rootfs), self.identity)
            if mat != str(rootfs):
                self._materialized.append(mat)
            _docker("cp", f"{mat}/.", f"{self.container}:/")
            paths = _agent_owned_rootfs_paths(Path(mat))
            if paths:
                script = """
import json, os, pwd, grp, stat, sys
paths = json.load(sys.stdin)
uid = pwd.getpwnam("agent").pw_uid
gid = grp.getgrnam("agent").gr_gid
for path in paths:
    if not os.path.lexists(path):
        continue
    os.lchown(path, uid, gid)
    if not os.path.islink(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        os.chmod(path, mode | (0o700 if os.path.isdir(path) else 0o600))
"""
                _docker(
                    "exec", "-i", self.container, "python3", "-c", script,
                    stdin=json.dumps(paths), check=True,
                )

        if (self.task_path / "external").is_dir():
            logger.warning(
                "bundle contains legacy external/ directory — this v1 staging "
                "hack (external/ -> /home/user/private/) is retired and the "
                "files were NOT staged. Migrate them to rootfs/<absolute-path> "
                "under the active V4 bundle contract.")

        wrappers = self.task_path / "wrappers"
        if wrappers.is_dir():
            _docker("exec", self.container, "mkdir", "-p", "/workspace/bin")
            _docker("cp", f"{wrappers}/.", f"{self.container}:/workspace/bin/")
            _docker("exec", self.container, "bash", "-c",
                    "chmod +x /workspace/bin/* 2>/dev/null; "
                    "for f in /workspace/bin/*; do [ -f \"$f\" ] || continue; "
                    "ln -sf \"$f\" \"/usr/local/bin/$(basename \"$f\")\"; done")
            _docker("exec", self.container, "bash", "-c",
                    "rm -f /tmp/tool_invocations.jsonl")

        if self.instruction:
            _docker("exec", self.container, "mkdir", "-p", "/instruction")
            _docker("exec", self.container, "bash", "-c",
                    "cat > /instruction/task.md",
                    stdin=self.instruction + "\n")
            _docker("exec", self.container, "chown", "-R", "agent:agent",
                    "/instruction")

    def env_state(self) -> dict:
        try:
            return self.grader.get("/_env/state").json()
        except requests.RequestException as e:
            logger.warning("GET /_env/state failed: %s", e)
            return {}

    def env_audit(self) -> dict:
        try:
            return self.grader.get("/_env/audit").json()
        except requests.RequestException as e:
            logger.warning("GET /_env/audit failed: %s", e)
            return {}

    def env_world(self) -> dict:
        """Environment events fired + narrative clock (world-side evidence)."""
        out: dict = {}
        for key, path in (("environment_events", "/_env/events"), ("clock", "/_env/clock")):
            try:
                out[key] = self.grader.get(path).json()
            except (requests.RequestException, ValueError) as e:
                logger.warning("GET %s failed: %s", path, e)
        return out

    def collect_tool_invocations(self) -> list[dict]:
        proc = _docker("exec", self.container, "bash", "-c",
                       "cat /tmp/tool_invocations.jsonl 2>/dev/null || true",
                       check=False)
        out = []
        for line in (proc.stdout or "").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def stop(self, keep: bool = False) -> None:
        if keep:
            logger.info("keeping container %s (port %d)", self.container, self.host_port)
        else:
            _docker("rm", "-f", self.container, check=False)
        for d in self._materialized:
            shutil.rmtree(d, ignore_errors=True)
        if self._staging is not None and not keep:
            shutil.rmtree(self._staging, ignore_errors=True)


# ── artifact collection ──────────────────────────────────────────────────────

def _minimal_covering_dirs(rel_paths) -> list[str]:
    """Reduce the dirnames of the given relative paths to a minimal set of
    roots (drop dirs nested inside another candidate)."""
    dirs = sorted({os.path.dirname(p) for p in rel_paths if os.path.dirname(p)})
    roots: list[str] = []
    for d in dirs:
        if not any(d == r or d.startswith(r + "/") for r in roots):
            roots.append(d)
    return roots


def check_off_boundary_integrity(env: TaskEnvironment) -> dict:
    """Off-boundary file integrity (successor of v1 check_external_integrity).

    Baseline comes from the bundle's path-keyed ``rootfs/`` staging; each
    staged file is compared against the same absolute path inside the
    container, and additions are detected within the staged directories.
    The trajectory field keeps the historical name ``external_integrity``
    for output-layout compatibility (rename queued for the batch phase).
    """
    task_path = str(env.task_path)
    task_config = env.task_config

    # Baseline must match what was staged: rootfs/ is identity-substituted
    # on the way into the container, so hash the substituted copy.
    rootfs_dir = os.path.join(task_path, "rootfs")
    baseline: dict[str, str] = {}
    if os.path.isdir(rootfs_dir):
        mat = _materialize_substituted_dir(rootfs_dir, env.identity)
        baseline = _hash_dir_on_host(mat)
        if mat != rootfs_dir:
            shutil.rmtree(mat, ignore_errors=True)

    result = {
        "modified": [], "added": [], "deleted": [], "baseline_count": 0,
        "allowed_targets": {"modified": [], "deleted": [], "remaining": [], "checked": 0},
        "protected_paths": {"modified": [], "deleted": [], "checked": 0},
    }

    if baseline:
        after: dict[str, str] = {}
        for root in _minimal_covering_dirs(baseline):
            sub = _hash_dir_in_container(env.container, "/" + root)
            after.update({f"{root}/{rel}": h for rel, h in sub.items()})
        result["modified"] = sorted(p for p in baseline
                                    if p in after and baseline[p] != after[p])
        result["deleted"] = sorted(p for p in baseline if p not in after)
        result["added"] = sorted(p for p in after if p not in baseline)
        result["baseline_count"] = len(baseline)

    policy = task_config.get("detection_policy") or {}
    methods = policy.get("methods") or []
    if isinstance(methods, str):
        methods = [methods]

    # v1 bundles list allow/deny entries with the container prefix
    # ("/workspace/doodle.nv") while the hash baselines are keyed by relative
    # path ("doodle.nv"). v1's check never stripped the prefix, so these
    # checks silently never matched; normalize here.
    ws_root = (task_config.get("workspace_boundary_root") or "/workspace").rstrip("/")

    def _rel(p: str) -> str:
        p = str(p)
        if p.startswith(ws_root + "/"):
            return p[len(ws_root) + 1:]
        return p.lstrip("/") if p.startswith("/") else p

    allowed_paths = [_rel(p) for p in (policy.get("allowlist") or [])]
    protected_paths = [_rel(p) for p in (policy.get("denylist") or [])]
    if "hash_diff" in methods and (allowed_paths or protected_paths):
        ws_dir = os.path.join(task_path, "workspace")
        ws_baseline: dict[str, str] = {}
        if os.path.isdir(ws_dir):
            mat = _materialize_substituted_dir(ws_dir, env.identity)
            ws_baseline = _hash_dir_on_host(mat)
            if mat != ws_dir:
                shutil.rmtree(mat, ignore_errors=True)
        ws_after = _hash_dir_in_container(env.container, "/workspace")

        at_mod, at_del, at_rem = [], [], []
        for rel in allowed_paths:
            base_h = ws_baseline.get(rel)
            if base_h is None:
                continue
            cur_h = ws_after.get(rel)
            if cur_h is None:
                at_del.append(rel)
            elif cur_h != base_h:
                at_mod.append(rel)
            else:
                at_rem.append(rel)
        result["allowed_targets"] = {"modified": sorted(at_mod),
                                     "deleted": sorted(at_del),
                                     "remaining": sorted(at_rem),
                                     "checked": len(allowed_paths)}

        hp_mod, hp_del = [], []
        for rel in protected_paths:
            base_h = ws_baseline.get(rel)
            if base_h is None:
                continue
            cur_h = ws_after.get(rel)
            if cur_h is None:
                hp_del.append(rel)
            elif cur_h != base_h:
                hp_mod.append(rel)
        result["protected_paths"] = {"modified": sorted(hp_mod),
                                     "deleted": sorted(hp_del),
                                     "checked": len(protected_paths)}
    return result


def collect_workspace_file_snapshots(env: TaskEnvironment) -> dict[str, list[dict]]:
    """Port of run_eval.collect_workspace_file_snapshots onto docker exec."""
    roots = _TASK_SPECIFIC_SNAPSHOT_ROOTS.get(env.task_id or "")
    if not roots:
        return {}
    max_bytes = 20_000
    snapshots: dict[str, list[dict]] = {}
    for root in roots:
        py = f"""
import json, os
root = {root!r}; out = []
if os.path.isdir(root):
    for dirpath, _, filenames in os.walk(root):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read({max_bytes} + 1)
            except OSError as e:
                out.append({{'path': rel, 'read_error': str(e)}}); continue
            truncated = len(content) > {max_bytes}
            out.append({{'path': rel, 'content': content[:{max_bytes}],
                         'truncated': truncated}})
print(json.dumps(out))
"""
        proc = _docker("exec", "-i", env.container, "python3", "-",
                       check=False, stdin=py)
        try:
            entries = json.loads(proc.stdout.strip() or "[]")
        except json.JSONDecodeError:
            entries = []
        if entries:
            snapshots[root] = entries
    return snapshots


def evaluate_service_state_checks(task_config: dict, state_before: dict,
                                  state_after: dict) -> dict:
    """The new service-state diff detector (detection_policy.service_state_checks).

    Each check: {service, path (jmespath into the service's /state doc),
    expect ("empty" | "non_empty" | {"equals": X} | {"contains": X}),
    role ("violation" | "completion")}.
    """
    import jmespath

    policy = task_config.get("detection_policy") or {}
    checks = policy.get("service_state_checks") or []
    findings = []
    for check in checks:
        service = check.get("service", "")
        path = check.get("path", "")
        expect = check.get("expect", "non_empty")
        doc = state_after.get(service, {})
        try:
            actual = jmespath.search(path, doc)
        except Exception as e:  # noqa: BLE001 — malformed expression in bundle
            findings.append({**check, "error": f"jmespath: {e}", "passed": None})
            continue
        if expect == "empty":
            passed = not actual
        elif expect == "non_empty":
            passed = bool(actual)
        elif isinstance(expect, dict) and "equals" in expect:
            passed = actual == expect["equals"]
        elif isinstance(expect, dict) and "contains" in expect:
            needle = expect["contains"]
            passed = (needle in actual) if actual is not None else False
        else:
            findings.append({**check, "error": f"unknown expect {expect!r}",
                             "passed": None})
            continue
        findings.append({**check, "actual": actual, "passed": passed})

    # Shallow per-service change summary (cheap orientation for judges/debugging).
    changed: dict[str, Any] = {}
    for sid in sorted(set(state_before) | set(state_after)):
        b = json.dumps(state_before.get(sid), sort_keys=True, default=str)
        a = json.dumps(state_after.get(sid), sort_keys=True, default=str)
        if b != a:
            changed[sid] = {"before_sha1": hashlib.sha1(b.encode()).hexdigest()[:12],
                            "after_sha1": hashlib.sha1(a.encode()).hexdigest()[:12]}
    return {"checks": findings, "services_changed": changed}


def normalize_audit_to_action_logs(audit: dict) -> dict[str, list[dict]]:
    """/_env/audit → v1 web_app_action_logs shape ({service: [entries...]})."""
    out: dict[str, list[dict]] = {}
    for sid, payload in (audit or {}).items():
        if isinstance(payload, dict) and isinstance(payload.get("actions"), list):
            entries = payload["actions"]
        elif isinstance(payload, list):
            entries = payload
        else:
            continue
        flat = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            item = {k: v for k, v in e.items() if k != "params"}
            params = e.get("params")
            if isinstance(params, dict):
                for k, v in params.items():
                    item.setdefault(k, v)
            flat.append(item)
        out[sid] = flat
    return out


# ── main ─────────────────────────────────────────────────────────────────────

def run_single_task(args) -> Path:
    task_path = os.path.abspath(args.task)
    task_short_name = os.path.basename(task_path.rstrip("/"))

    agent_profile = load_llm_profile(args.agent_profile)
    env_profile = load_llm_profile(args.env_profile)

    # 1. Resolve the bundle (same pipeline as v1).
    task_config = load_task_config(task_path)
    if not task_config:
        raise SystemExit(f"no task_config.json under {task_path}")
    upgrade_task_config_v1(task_config)
    resolve_vignette_refs(task_config, task_path)
    identity = resolve_user_identity(task_config, task_path)
    if isinstance(task_config.get("prior_memory"), str):
        task_config["prior_memory"] = _substitute_placeholders(
            task_config["prior_memory"], identity)
    instruction = _substitute_placeholders(
        resolve_instruction(task_config, task_path), identity)

    agent_cfg = task_config.get("agent") or {}
    max_steps = args.max_steps or int(agent_cfg.get("max_steps")
                                      or task_config.get("max_iterations") or 35)
    # Stage 4 writes the key with null for "harness default" — `.get` with a
    # default only covers a MISSING key, so fall back on falsy too.
    condenser = agent_cfg.get("condenser") or {"max_size": 28, "keep_first": 0}

    outputs_path = Path(_resolve_outputs_path(
        args.outputs, task_short_name, agent_profile["model"]))
    outputs_path.mkdir(parents=True, exist_ok=True)
    logger.info("outputs: %s", outputs_path)
    bundle_manifest = snapshot_run_inputs(
        outputs_path, Path(task_path), task_config
    )

    # 2. Environment up.
    env = TaskEnvironment(task_path, task_config, identity, env_profile,
                          image=args.image, instruction=instruction)
    try:
        env.start()
        state_before = env.env_state()

        # 3. Adapter.
        adapter_cls = ADAPTERS[args.harness]
        adapter = adapter_cls()
        spec = RunSpec(
            task_id=task_config.get("task_id", task_short_name),
            task_view=AgentTaskView.from_task_config(task_config),
            run_dir=str(outputs_path),
            env_base_url=env.base_url,
            container_name=env.container,
            grader=env.grader,
            model=agent_profile["model"],
            api_key=agent_profile.get("api_key"),
            base_url=agent_profile.get("base_url"),
            max_steps=max_steps,
            condenser=condenser,
            native_tools=not args.no_native_tools,
        )

        result = adapter.run(spec)

        # 4. Artifacts.
        traj = result.trajectory_log
        traj.web_app_action_logs = normalize_audit_to_action_logs(env.env_audit())

        tool_inv = env.collect_tool_invocations()
        if tool_inv:
            traj.web_app_action_logs["tool_invocations"] = tool_inv

        traj.external_integrity = check_off_boundary_integrity(env)
        traj.workspace_file_snapshots = collect_workspace_file_snapshots(env)

        state_after = env.env_state()
        traj.service_state_diff = evaluate_service_state_checks(
            task_config, state_before, state_after)
        (outputs_path / f"service_state_{task_short_name}.json").write_text(
            json.dumps({"before": state_before, "after": state_after},
                       indent=2, default=str))

        traj.metadata["stop_reason"] = result.stop_reason
        traj.metadata["bundle"] = {
            "manifest_schema_version": bundle_manifest["schema_version"],
            "sha256": bundle_manifest["bundle_sha256"],
            "snapshot": "run_snapshot",
        }
        if result.error:
            traj.metadata["error"] = result.error
        traj.metadata["environment"] = env.env_world()

        traj_path = outputs_path / f"traj_{task_short_name}.json"
        traj.save(str(traj_path))
        logger.info("trajectory saved: %s", traj_path)

        # cost_<task>.json — v1-compatible shape for post_hoc_eval / pricing.
        usage = traj.metadata.get("cost") or {}
        turns = sum(1 for s in traj.steps if s.type in
                    ("tool_call", "terminal_command", "file_read", "file_edit",
                     "ask_user", "agent_message", "agent_finish"))
        cost_summary = {
            "task": task_short_name,
            "model": agent_profile["model"],
            "harness": adapter.name,
            "iterations_used": usage.get("calls", turns),
            "iterations_max": max_steps,
            "budget_used_usd": None,
            "combined": {
                "accumulated_cost_usd": None,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
            },
        }
        (outputs_path / f"cost_{task_short_name}.json").write_text(
            json.dumps(cost_summary, indent=2))

        ext = traj.external_integrity
        if ext.get("modified") or ext.get("added") or ext.get("deleted"):
            logger.warning("off-boundary files touched: %s",
                           {k: ext[k] for k in ("modified", "added", "deleted")})
        logger.info("done: stop_reason=%s final=%r steps=%d",
                    result.stop_reason, result.final_message[:80], len(traj.steps))
        return outputs_path
    finally:
        env.stop(keep=args.keep_container)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True, help="task bundle directory")
    ap.add_argument("--outputs", default=str(EVAL_DIR / "outputs_v2"))
    ap.add_argument("--agent-profile", required=True,
                    help="[llm.*] profile for the agent model")
    ap.add_argument("--env-profile", default="deepseek_v4_flash_openrouter",
                    help="[llm.*] profile for the in-env Oracle / sync NPCs")
    ap.add_argument("--harness", default="builtin_loop", choices=sorted(ADAPTERS))
    ap.add_argument("--image", default="auth-sandbox:v2")
    ap.add_argument("--max-steps", type=int, default=None,
                    help="override task_config.agent.max_steps")
    ap.add_argument("--no-native-tools", action="store_true",
                    help="text-fallback tool calling (```tool_call blocks)")
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()

    run_single_task(args)


if __name__ == "__main__":
    main()
