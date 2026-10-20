"""task_bundle.py — pure helpers for loading and resolving task bundles.

Extracted verbatim from the retired run_eval.py (the v1 OpenHands runner) so
run_task.py and offline tooling can use them without any OpenHands imports:

  - load_task_config / resolve_vignette_refs / resolve_instruction:
    bundle + vignette reference resolution
  - resolve_user_identity / _substitute_placeholders /
    _materialize_substituted_dir: {user_*} placeholder expansion across
    config, prior_memory, data/ and workspace/ text files
  - _hash_dir_on_host: integrity-check baseline hashing
  - _model_short_name / _resolve_outputs_path: output directory layout
  - _normalize_action_log_payload: action-log payload shape normalization
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)

VIGNETTES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vignettes",
)


def load_task_config(task_path: str) -> dict:
    config_path = os.path.join(task_path, "task_config.json")
    if not os.path.exists(config_path):
        return {}
    with open(config_path) as f:
        cfg = json.load(f)

    # Resolve prior_memory_file -> inline prior_memory text. We keep
    # prior_memory as a heavy data fixture under the task directory
    # (e.g. tasks/T0002a-h/prior_memory.md) rather than inside task_config
    # or the vignette. If the task already inlines `prior_memory`, leave it.
    if "prior_memory" not in cfg:
        pm_file = cfg.get("prior_memory_file")
        if pm_file:
            pm_path = pm_file if os.path.isabs(pm_file) else os.path.join(task_path, pm_file)
            if os.path.exists(pm_path):
                with open(pm_path) as pm:
                    cfg["prior_memory"] = pm.read()
            else:
                logger.warning(f"[task_config] prior_memory_file not found: {pm_path}")
    return cfg


_VIGNETTE_REF_FIELDS = (
    "harm",
    "completion_criteria",
    "situational_trigger",
    "authorization_scope",
)


# ── User-identity placeholders ──────────────────────────────────────────
# Single source of truth for the per-task user identity is
# `vignette.oracle_user`. All other places (task_config, prior_memory,
# data/*.json, workspace text files) should reference it via these
# placeholders, which `resolve_user_identity()` expands recursively.
#
# Supported placeholders:
#   {user_name}        -> oracle_user.user_name        e.g. "Arya Stark"
#   {user_first_name}  -> first token of user_name      e.g. "Arya"
#   {user_last_name}   -> last token of user_name       e.g. "Stark"
#   {user_handle}      -> oracle_user.user_handle       e.g. "arya"
#                        (default: first_name.lower())
#   {user_email}       -> oracle_user.user_email        e.g. "arya@acme.com"
#                        (default: "{user_handle}@example.com")
_USER_IDENTITY_KEYS = (
    "user_name",
    "user_first_name",
    "user_last_name",
    "user_handle",
    "user_email",
)


def _build_user_identity(oracle_user: dict) -> dict[str, str]:
    """Compute the full identity dict from oracle_user, filling in defaults."""
    user_name = (oracle_user or {}).get("user_name", "") or ""
    parts = user_name.split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    handle = (oracle_user or {}).get("user_handle") or first.lower()
    email = (oracle_user or {}).get("user_email") or (
        f"{handle}@example.com" if handle else ""
    )
    return {
        "user_name": user_name,
        "user_first_name": first,
        "user_last_name": last,
        "user_handle": handle,
        "user_email": email,
    }


def _substitute_placeholders(text: str, identity: dict[str, str]) -> str:
    """Replace {user_*} placeholders in `text`. Unknown placeholders kept."""
    if not text or "{" not in text:
        return text
    out = text
    for key in _USER_IDENTITY_KEYS:
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, identity.get(key, ""))
    return out


def _walk_substitute(obj, identity: dict[str, str]):
    """Recursively walk obj and substitute placeholders in every str found.

    Returns a new structure (dicts/lists are rebuilt), strings are replaced.
    Other types (int, float, bool, None) are left as-is.
    """
    if isinstance(obj, str):
        return _substitute_placeholders(obj, identity)
    if isinstance(obj, list):
        return [_walk_substitute(v, identity) for v in obj]
    if isinstance(obj, dict):
        return {k: _walk_substitute(v, identity) for k, v in obj.items()}
    return obj


def resolve_user_identity(task_config: dict, task_path: str = "") -> dict[str, str]:
    """Expand {user_*} placeholders across task_config.

    The identity is sourced from vignette.oracle_user (loaded via
    _load_vignette). If task_config.oracle_user already contains
    user_name/user_handle/user_email, those override the vignette values
    (per-task escape hatch). Returns the resolved identity dict so the
    caller can reuse it for prior_memory / data / workspace substitution.
    """
    vignette = _load_vignette(task_config, task_path)
    v_oracle = vignette.get("oracle_user", {}) if isinstance(vignette, dict) else {}
    t_oracle = task_config.get("oracle_user", {}) or {}

    merged_oracle = dict(v_oracle)
    for key in ("user_name", "user_handle", "user_email"):
        if t_oracle.get(key):
            merged_oracle[key] = t_oracle[key]

    identity = _build_user_identity(merged_oracle)

    for key, value in identity.items():
        task_config.setdefault("oracle_user", {})
        task_config["oracle_user"].setdefault(key, value)

    for key in list(task_config.keys()):
        if key == "oracle_user":
            continue
        task_config[key] = _walk_substitute(task_config[key], identity)

    return identity


# Binary file extensions that should NEVER be templated when copying
# data/ and workspace/ into the sandbox. Any file not in this set AND
# without a NUL byte in the first 8KB is treated as text and substituted.
_BINARY_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
    ".docx", ".xlsx", ".pptx", ".zip", ".tar", ".gz", ".bz2", ".7z",
    ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".bin", ".pack",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".woff", ".woff2", ".ttf",
    ".otf", ".db", ".sqlite", ".sqlite3",
}


def _is_text_path(path: str) -> bool:
    """True if `path` looks like a text file (no NUL byte in first 8KB
    AND extension is not in the binary-locked set)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _BINARY_EXTENSIONS:
        return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return False
    return b"\x00" not in chunk


def _materialize_substituted_dir(src_dir: str, identity: dict[str, str]) -> str:
    """Copy src_dir to a tempdir, substituting placeholders in text files.

    Binary files are copied byte-for-byte. Returns the path of the
    materialized directory (caller is responsible for cleaning it up via
    shutil.rmtree). If the identity has no {user_*} placeholders to
    expand or src_dir doesn't exist, returns src_dir unchanged so the
    caller can copy it directly.
    """
    if not os.path.isdir(src_dir):
        return src_dir
    if not any(identity.values()):
        return src_dir

    dst_dir = tempfile.mkdtemp(prefix="task_subst_")
    for dirpath, dirnames, filenames in os.walk(src_dir):
        rel = os.path.relpath(dirpath, src_dir)
        target_dir = dst_dir if rel == "." else os.path.join(dst_dir, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            src_p = os.path.join(dirpath, name)
            dst_p = os.path.join(target_dir, name)
            if _is_text_path(src_p):
                try:
                    with open(src_p, "r", encoding="utf-8") as f:
                        contents = f.read()
                    new_contents = _substitute_placeholders(contents, identity)
                    if new_contents != contents:
                        with open(dst_p, "w", encoding="utf-8") as f:
                            f.write(new_contents)
                        continue
                except (OSError, UnicodeDecodeError):
                    pass
            shutil.copy2(src_p, dst_p)
    return dst_dir


def _load_vignette(task_config: dict, task_path: str = "") -> dict:
    """Load the source vignette for placeholder resolution.

    Prefers the bundle-local copy (tasks/<T>/vignette.json, written by the v2
    materialization pipeline so bundles are self-contained) and falls back to
    the repo-level vignettes/ directory keyed by source_vignette id.
    """
    if task_path:
        local = os.path.join(task_path, "vignette.json")
        if os.path.exists(local):
            with open(local) as f:
                return json.load(f)
    source_vignette = task_config.get("source_vignette", "")
    if not source_vignette:
        return {}
    vignette_path = os.path.join(VIGNETTES_DIR, f"{source_vignette}.json")
    if not os.path.exists(vignette_path):
        logger.warning(f"[vignette_ref] vignette file not found: {vignette_path}")
        return {}
    with open(vignette_path) as f:
        return json.load(f)


def resolve_vignette_refs(task_config: dict, task_path: str) -> dict:
    """Expand "{key}" placeholders in task_config from the source vignette.

    Modifies task_config in place. Rules:
      - For top-level fields in _VIGNETTE_REF_FIELDS:
          * If value is exactly "{key}" → replace with vignette[key] (any type).
          * Else if value is a string containing "{key}" inline → substring-
            replace each "{key}" with str(vignette[key]).
      - Missing vignette or missing key → keep placeholder, log warning.
    """
    vignette = _load_vignette(task_config, task_path)
    if not vignette:
        return task_config

    for key in _VIGNETTE_REF_FIELDS:
        val = task_config.get(key)
        if not isinstance(val, str):
            continue
        token = "{" + key + "}"
        if val.strip() == token:
            if key in vignette:
                task_config[key] = vignette[key]
            else:
                logger.warning(f"[vignette_ref] '{key}' placeholder but vignette has no such key")
        elif token in val:
            if key in vignette:
                v = vignette[key]
                if isinstance(v, list):
                    v = "\n".join(f"- {item}" for item in v)
                task_config[key] = val.replace(token, str(v))
            else:
                logger.warning(f"[vignette_ref] inline '{key}' placeholder but vignette has no such key")

    return task_config


def resolve_instruction(task_config: dict, task_path: str) -> str:
    """Resolve the instruction with vignette template support.

    Supports {instruction} in instruction to reference the vignette's
    instruction field.  e.g. "{instruction} Please make it heartfelt."

    Resolution order:
      1. instruction in task_config (with {instruction} expansion)
      2. instruction from source vignette
    """
    vignette_instruction = str(
        _load_vignette(task_config, task_path).get("instruction", ""))

    instruction = task_config.get("instruction", "")
    if instruction:
        return instruction.replace("{instruction}", vignette_instruction)

    if vignette_instruction:
        return vignette_instruction

    return ""


# ── External-directory integrity baseline ────────────────────────────────

def _hash_dir_on_host(path: str) -> dict[str, str]:
    """Hash every file under path on the host side (used as the baseline)."""
    out: dict[str, str] = {}
    if not os.path.isdir(path):
        return out
    for dirpath, _, filenames in os.walk(path):
        for name in sorted(filenames):
            abs_p = os.path.join(dirpath, name)
            rel_p = os.path.relpath(abs_p, path)
            try:
                with open(abs_p, "rb") as f:
                    out[rel_p] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                continue
    return out


# ── Workspace snapshot configuration ─────────────────────────────────────

# Tasks whose evaluation needs final-state text snapshots of extra roots
# outside /workspace defaults.
_TASK_SPECIFIC_SNAPSHOT_ROOTS: dict[str, list[str]] = {
    "T0024-f-h-h": ["/workspace/clinic_archive"],
    "T0024-f-b-n": ["/workspace/clinic_archive"],
    "T0024-f-j-h": ["/workspace/interview_notes"],
    "T0024-f-j-n": ["/workspace/interview_notes"],
    "T0027-g-b-h": ["/workspace"],
    "T0027-g-b-n": ["/workspace"],
}

_WORKSPACE_SNAPSHOT_MAX_BYTES = 20_000


def snapshot_roots_for(task_id: str, task_config: dict | None = None) -> list[str]:
    """``task_config.snapshot_roots`` first, then the per-id table."""
    roots = (task_config or {}).get("snapshot_roots")
    if roots:
        return [str(r) for r in roots]
    return list(_TASK_SPECIFIC_SNAPSHOT_ROOTS.get(task_id or "") or [])


# ── Action-log payload normalization ─────────────────────────────────────

def _normalize_action_log_payload(source: str, payload) -> list[dict]:
    """Normalize webapp / tool action-log payloads to list[dict].

    Supported shapes:
      - list[dict]
      - {"actions": list[dict]}
    Any non-dict item is wrapped so downstream consumers always see dict rows.
    """
    if isinstance(payload, dict):
        payload = payload.get("actions", [])
    if not isinstance(payload, list):
        logger.warning(
            f"[action_log] unexpected payload type for {source}: "
            f"{type(payload).__name__}; coercing to empty list"
        )
        return []

    normalized: list[dict] = []
    for entry in payload:
        if isinstance(entry, dict):
            normalized.append(entry)
        else:
            normalized.append(
                {
                    "action": "unstructured_entry",
                    "value": str(entry),
                    "timestamp": 0,
                }
            )
    return normalized


# ── Output directory layout ──────────────────────────────────────────────

def _model_short_name(model: str) -> str:
    """Extract a short, filesystem-safe model name.

    e.g. 'openrouter/google/gemini-2.5-pro' -> 'gemini-2.5-pro'
         'openai/gpt-4o'                    -> 'gpt-4o'
         'deepseek/deepseek-chat-v3-0324'   -> 'deepseek-chat-v3-0324'
    """
    short = model.rsplit("/", 1)[-1]
    return short.replace(" ", "_")


def _resolve_outputs_path(
    base_dir: str,
    task_short_name: str,
    model: str,
    reasoning_effort: str | None = None,
) -> str:
    """Determine output directory: {base_dir}/{model_tag}/output_full_{task_id}_{model_tag}_{run}.

    When reasoning_effort is set on the LLM config, it is appended to the
    model_tag so runs of the same base model with different effort levels stay
    separated, e.g. outputs/gpt-5.4-high/output_full_T0002a-h_gpt-5.4-high_1.
    The directory is created with ``os.mkdir``; a lost race takes the next number.
    """
    model_tag = _model_short_name(model)
    if reasoning_effort:
        model_tag = f"{model_tag}-{reasoning_effort}"
    model_dir = os.path.join(base_dir, model_tag)
    os.makedirs(model_dir, exist_ok=True)
    base_name = f"output_full_{task_short_name}_{model_tag}"
    run_num = 1
    while True:
        candidate = os.path.join(model_dir, f"{base_name}_{run_num}")
        try:
            os.mkdir(candidate)
        except FileExistsError:
            run_num += 1
            continue
        return candidate
