#!/usr/bin/env python3
"""Append the cleaned trajectory transcript to existing post-hoc reports.

Scans every .md file under detectors/post_hoc_eval_report/, finds the
referenced ``trajectory:`` path, renders the appendix via
``post_hoc_eval._render_trajectory_appendix``, and appends it to the file
(unless the file already ends with an appendix). No LLM calls are made;
existing completion/violation verdicts are preserved verbatim.

Usage:
    python scripts/append_run_appendix.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
EVAL_DIR = THIS_DIR.parent
sys.path.insert(0, str(EVAL_DIR))

from post_hoc_eval import _render_trajectory_appendix  # noqa: E402

REPORT_ROOT = EVAL_DIR / "detectors" / "post_hoc_eval_report"
APPENDIX_MARKER = "## Appendix: cleaned run transcript"
TRAJ_RE = re.compile(r"^\-\s*\*\*trajectory\*\*:\s*`([^`]+)`", re.MULTILINE)


def patch_file(md_path: Path, dry_run: bool) -> str:
    text = md_path.read_text(encoding="utf-8")
    if APPENDIX_MARKER in text:
        return "skip (already has appendix)"

    m = TRAJ_RE.search(text)
    if not m:
        return "skip (no trajectory path)"

    traj_rel = m.group(1)
    traj_path = (EVAL_DIR / traj_rel).resolve() if not Path(traj_rel).is_absolute() else Path(traj_rel)
    if not traj_path.exists():
        return f"skip (traj missing: {traj_path})"

    appendix = _render_trajectory_appendix(str(traj_path))
    if not appendix:
        return "skip (appendix empty)"

    new_text = text.rstrip() + "\n\n" + appendix + "\n"
    if dry_run:
        return f"would append {len(appendix)} chars"
    md_path.write_text(new_text, encoding="utf-8")
    return f"appended {len(appendix)} chars"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not REPORT_ROOT.exists():
        print(f"[error] report root not found: {REPORT_ROOT}")
        return 1

    md_files = sorted(REPORT_ROOT.rglob("*.md"))
    print(f"Found {len(md_files)} report file(s)")
    for md in md_files:
        rel = md.relative_to(REPORT_ROOT)
        result = patch_file(md, args.dry_run)
        print(f"  {rel}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
