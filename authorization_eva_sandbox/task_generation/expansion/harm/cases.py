"""Which installed cases can still get the opposite harm label."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from authorization_eva_sandbox.task_generation.expansion.originals import (
    TASKS_DIR,
    OriginalCase,
    TaskId,
    task_ids,
)

FLIP = {"h": "n", "n": "h"}
LABEL_VALUE = {"h": "Y", "n": "N"}


def flipped_id(task_id: str) -> str:
    p = TaskId.parse(task_id)
    return f"T{p.number}-{p.trigger}-{p.domain}-{FLIP[p.harm]}"


def harm_cases(tasks_dir: Path = TASKS_DIR) -> list[OriginalCase]:
    """Installed cases whose (number, trigger, domain) group lacks the other label.

    The returned objects reuse OriginalCase so the domain build code can
    work with them; ``family`` holds every task of the same seed number.
    """
    ids = task_ids(tasks_dir)
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    families: dict[str, list[str]] = defaultdict(list)
    for task_id in ids:
        p = TaskId.parse(task_id)
        groups[(p.number, p.trigger, p.domain)].add(p.harm)
        families[p.number].append(task_id)
    out = []
    for task_id in ids:
        p = TaskId.parse(task_id)
        if p.harm not in FLIP or FLIP[p.harm] in groups[(p.number, p.trigger, p.domain)]:
            continue
        out.append(OriginalCase(seed_id=f"S{p.number}", task_id=task_id,
                                family=list(families[p.number])))
    return out


def paired_groups(tasks_dir: Path = TASKS_DIR) -> int:
    """How many (number, trigger, domain) groups already have both labels."""
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for task_id in task_ids(tasks_dir):
        p = TaskId.parse(task_id)
        groups[(p.number, p.trigger, p.domain)].add(p.harm)
    return sum(1 for labels in groups.values() if {"h", "n"} <= labels)
