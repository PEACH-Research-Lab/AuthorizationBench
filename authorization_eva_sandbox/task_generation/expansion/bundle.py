"""Read a sealed task bundle: preflight it and build the text digest a model reads."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.task_generation.shared.canonical_vignette import (
    canonical_vignette_issues,
)

SANDBOX_ROOT = Path(__file__).resolve().parents[2]
VIGNETTES_DIR = SANDBOX_ROOT / "vignettes"

BUNDLE_DIRS = ("fixtures", "workspace", "rootfs", "wrappers", "data")


def vignette_stem(task_id: str) -> str:
    return "V" + task_id[1:]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _boot_problems(task_dir: Path, task_config: dict[str, Any]) -> list[str]:
    """Mount every declared service with its fixture, the way run_eval does."""
    for p in (str(SANDBOX_ROOT.parent), str(SANDBOX_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from services._base import fixture_env_var
    from services.multi_server import create_app

    entries = []
    for svc in task_config.get("services") or []:
        entry = dict(svc)
        fixture = entry.get("fixture")
        if fixture and not os.path.isabs(fixture):
            entry["fixture"] = str(task_dir / fixture)
        entries.append(entry)

    keys = [fixture_env_var(e["id"]) for e in entries if e.get("fixture")]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        app = create_app(
            entries,
            task_config.get("environment_events") or [],
            reference_time=task_config.get("reference_time") or None,
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return [f"services do not boot: {type(exc).__name__}: {exc}"]
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    paths = set((app.openapi().get("paths") or {}).keys())
    problems = []
    for entry in entries:
        prefix = f"/{entry['id']}/"
        if not any(path.startswith(prefix) for path in paths):
            problems.append(
                f"service {entry['id']!r} (template {entry.get('template')!r}) did not mount"
            )
    return problems


def preflight(task_dir: Path, vignettes_dir: Path = VIGNETTES_DIR) -> list[str]:
    """Problems that stop an expansion before any model is called."""
    task_dir = Path(task_dir)
    task_id = task_dir.name
    stem = vignette_stem(task_id)
    problems: list[str] = []

    for name in ("task_config.json", "vignette.json", "system_prompt.txt"):
        if not (task_dir / name).is_file():
            problems.append(f"missing {name}")
    if problems:
        return problems

    task_config = load_json(task_dir / "task_config.json")
    if task_config.get("task_id") != task_id:
        problems.append(f"task_config.task_id is {task_config.get('task_id')!r}")
    if task_config.get("source_vignette") != stem:
        problems.append(f"task_config.source_vignette is {task_config.get('source_vignette')!r}")

    canonical = Path(vignettes_dir) / f"{stem}.json"
    if not canonical.is_file():
        problems.append(f"missing canonical vignette {canonical.name}")
    elif canonical.read_bytes() != (task_dir / "vignette.json").read_bytes():
        problems.append("tasks/ and vignettes/ copies of the vignette differ")

    vignette = load_json(task_dir / "vignette.json")
    issues = canonical_vignette_issues(vignette, expected_stem=stem)
    problems.extend(f"vignette: {issue}" for issue in issues)
    from authorization_eva_sandbox.task_generation.expansion.checks import tools_block_problem
    tools = tools_block_problem((task_dir / "system_prompt.txt").read_text(encoding="utf-8"),
                                vignette)
    if tools:
        problems.append(tools)
    problems.extend(_boot_problems(task_dir, task_config))
    return problems


def _is_text(path: Path) -> bool:
    """Decided by content: placeholder .docx or .jpg fixtures are often text."""
    head = path.read_bytes()[:4096]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut at the 4096-byte boundary is still text.
        if exc.start < len(head) - 3:
            return False
    return True


ROOT_SPEC_FILES = ("task_config.json", "vignette.json", "system_prompt.txt")


def bundle_files(task_dir: Path, *, include_spec: bool = False) -> list[dict[str, Any]]:
    """Every file the bundle ships, with size and whether it is text.

    Top-level files such as ``prior_memory.md`` are included. The three spec
    files are included only when ``include_spec`` is set. ``_generation/``
    process files are never part of the bundle.
    """
    out = []
    for path in sorted(Path(task_dir).iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name in ROOT_SPEC_FILES and not include_spec:
            continue
        out.append({"path": path.name, "bytes": path.stat().st_size, "text": _is_text(path)})
    for dirname in BUNDLE_DIRS:
        base = Path(task_dir) / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                out.append({
                    "path": str(path.relative_to(task_dir)),
                    "bytes": path.stat().st_size,
                    "text": _is_text(path),
                })
    return out


def digest(
    task_dir: Path,
    *,
    max_file_chars: int = 6000,
    max_total_chars: int = 120_000,
) -> dict[str, Any]:
    """What the expansion model reads about one bundle.

    Evaluator-side fields are included on purpose: the model has to know
    the violated actions to keep them intact.
    """
    task_dir = Path(task_dir)
    task_config = load_json(task_dir / "task_config.json")
    vignette = load_json(task_dir / "vignette.json")
    surface = vignette.get("agent_surface") or {}
    out: dict[str, Any] = {
        "task_id": task_dir.name,
        "task_config": task_config,
        "system_prompt": (task_dir / "system_prompt.txt").read_text(encoding="utf-8"),
        "vignette": {
            "narrative": vignette.get("narrative"),
            "situational_trigger": vignette.get("situational_trigger"),
            "trigger_manifestation": vignette.get("trigger_manifestation"),
            "authorization_scope": vignette.get("authorization_scope"),
            "agent_context": surface.get("agent_context") or vignette.get("agent_context"),
        },
        "files": bundle_files(task_dir),
        "file_text": {},
        "truncated": [],
    }
    budget = max_total_chars
    for entry in out["files"]:
        if not entry["text"]:
            continue
        text = (task_dir / entry["path"]).read_text(encoding="utf-8", errors="replace")
        if len(text) > max_file_chars:
            text = text[:max_file_chars]
            out["truncated"].append(entry["path"])
        if budget <= 0:
            out["truncated"].append(entry["path"])
            continue
        text = text[:budget]
        budget -= len(text)
        out["file_text"][entry["path"]] = text
    return out
