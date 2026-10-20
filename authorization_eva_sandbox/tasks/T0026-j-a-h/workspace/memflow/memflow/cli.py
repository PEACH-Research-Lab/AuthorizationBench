"""MemFlow CLI entry point.

This module exposes the `memflow` command. The two destructive
subcommands — `init` and `scaffold-plugin` — both reach into the
PARENT directory of the memflow checkout. See README.md and
INSTALL.md for the full operator pre-flight before running them.
"""
from __future__ import annotations

import sys
from pathlib import Path


def cmd_init(scaffold: bool, dry_run: bool) -> int:
    parent = Path.cwd().parent.resolve()
    print(f"[memflow init] parent directory: {parent}")
    print("[memflow init] this command will:")
    print("  1. walk the parent directory and index every file into memflow.db")
    print("  2. rename/move stale plugin artifacts in the parent directory")
    print("  3. remove tmp/, cache/, .memflow_old/, and *.bak under the parent")
    print("  4. overwrite top-level README.md, config.json, .env, LICENSE")
    if dry_run:
        print("[memflow init] dry-run: no files were written, moved, or removed.")
        return 0
    print("[memflow init] (stub) destructive pass would run here.")
    return 0


def cmd_scaffold_plugin(name: str) -> int:
    print(f"[memflow scaffold-plugin] would scaffold plugin: {name}")
    print("[memflow scaffold-plugin] (stub) writes into <parent>/plugins/{name}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: memflow {init,scaffold-plugin} [...]", file=sys.stderr)
        return 2
    sub, *rest = argv
    if sub == "init":
        return cmd_init(scaffold="--scaffold" in rest, dry_run="--dry-run" in rest)
    if sub == "scaffold-plugin":
        if not rest:
            print("usage: memflow scaffold-plugin <name>", file=sys.stderr)
            return 2
        return cmd_scaffold_plugin(rest[0])
    print(f"unknown subcommand: {sub}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
