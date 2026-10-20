"""Content-addressed task manifests and immutable per-run scoring snapshots."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Iterable


SNAPSHOT_DIR = "run_snapshot"
TREE_INPUTS = ("fixtures", "data", "workspace", "rootfs", "wrappers", "gitea")
ROOT_INPUTS = ("task_config.json", "vignette.json", "prior_memory.md")
GENERATION_INPUTS = (
    "manifest.json",
    "grounding.json",
    "bindings.json",
    "baseline_state.json",
    "fill_envelope.json",
    "world_realization.json",
    "compiler_report.json",
    "boot_report.json",
    "world_review.json",
    "validation.json",
    "binding_report.json",
    "leak_report.json",
)


def _input_files(bundle: Path) -> Iterable[Path]:
    for name in ROOT_INPUTS:
        path = bundle / name
        if path.is_file():
            yield path
    for name in TREE_INPUTS:
        root = bundle / name
        if root.is_dir():
            yield from sorted(p for p in root.rglob("*") if p.is_file())
    generation = bundle / "_generation"
    for name in GENERATION_INPUTS:
        path = generation / name
        if path.is_file():
            yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_bundle_manifest(bundle: Path) -> dict:
    files = {
        path.relative_to(bundle).as_posix(): _sha256(path)
        for path in _input_files(bundle)
    }
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "task_id": bundle.name,
        "files": files,
        "bundle_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def write_bundle_manifest(bundle: Path) -> dict:
    manifest = build_bundle_manifest(bundle)
    target = bundle / "_generation" / "bundle_manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def snapshot_run_inputs(
    run_dir: Path,
    bundle: Path,
    resolved_task_config: dict,
) -> dict:
    """Copy all execution/scoring inputs used by one run."""
    snapshot = run_dir / SNAPSHOT_DIR
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.mkdir(parents=True)

    for name in ROOT_INPUTS:
        source = bundle / name
        if source.is_file():
            shutil.copy2(source, snapshot / name)
    for name in TREE_INPUTS:
        source = bundle / name
        if source.is_dir():
            shutil.copytree(source, snapshot / name)
    generation_source = bundle / "_generation"
    generation_target = snapshot / "_generation"
    for name in GENERATION_INPUTS:
        source = generation_source / name
        if source.is_file():
            generation_target.mkdir(exist_ok=True)
            shutil.copy2(source, generation_target / name)

    (snapshot / "resolved_task_config.json").write_text(
        json.dumps(resolved_task_config, indent=2) + "\n"
    )

    manifest = build_bundle_manifest(bundle)
    (snapshot / "bundle_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return manifest


def load_run_snapshot(traj_path: Path) -> tuple[dict | None, Path | None, dict | None]:
    snapshot = traj_path.parent / SNAPSHOT_DIR
    config_path = snapshot / "resolved_task_config.json"
    manifest_path = snapshot / "bundle_manifest.json"
    if not config_path.is_file() or not manifest_path.is_file():
        return None, None, None
    return (
        json.loads(config_path.read_text()),
        snapshot,
        json.loads(manifest_path.read_text()),
    )
