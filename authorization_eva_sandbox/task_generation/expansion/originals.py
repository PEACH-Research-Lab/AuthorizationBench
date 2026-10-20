"""Find the original cases: tasks that come straight from a seed.

A task ``T####-<trigger>-<domain>-<harm>`` is an original case of seed
``S####`` when its domain and harm letters match the seed and its trigger
letter is one of the seed's triggers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.task_generation.shared.grounding import (
    resolve_trigger_letters,
)
from authorization_eva_sandbox.task_generation.shared.utils import _taxonomy_ids

SANDBOX_ROOT = Path(__file__).resolve().parents[2]
SEED_FILE = SANDBOX_ROOT / "seeds" / "seed.json"
TASKS_DIR = SANDBOX_ROOT / "tasks"


@dataclass(frozen=True)
class TaskId:
    number: str
    trigger: str
    domain: str
    harm: str

    @classmethod
    def parse(cls, task_id: str) -> "TaskId":
        parts = task_id.split("-")
        if len(parts) != 4 or not parts[0].startswith("T"):
            raise ValueError(f"not a task id: {task_id!r}")
        return cls(parts[0][1:], parts[1], parts[2], parts[3])

    def with_domain(self, domain: str) -> str:
        return f"T{self.number}-{self.trigger}-{domain}-{self.harm}"

    def __str__(self) -> str:
        return f"T{self.number}-{self.trigger}-{self.domain}-{self.harm}"


@dataclass
class OriginalCase:
    seed_id: str
    task_id: str
    family: list[str] = field(default_factory=list)

    @property
    def parsed(self) -> TaskId:
        return TaskId.parse(self.task_id)

    def covered_domains(self) -> set[str]:
        """Domains this case cannot move to because the id already exists."""
        me = self.parsed
        out = set()
        for member in self.family:
            other = TaskId.parse(member)
            if other.trigger == me.trigger and other.harm == me.harm:
                out.add(other.domain)
        return out


def _seed_letters(seed: dict[str, Any]) -> tuple[list[str], str, str]:
    triggers, _unresolved = resolve_trigger_letters(seed.get("situational_trigger") or [])
    ids = _taxonomy_ids(seed, {})
    return triggers, ids["domain_id"], ids["harm_id"]


def task_ids(tasks_dir: Path = TASKS_DIR) -> list[str]:
    out = []
    for path in sorted(Path(tasks_dir).iterdir()):
        if not path.is_dir() or not path.name.startswith("T"):
            continue
        try:
            TaskId.parse(path.name)
        except ValueError:
            continue
        out.append(path.name)
    return out


def find_original_cases(
    seeds: list[dict[str, Any]] | None = None,
    tasks_dir: Path = TASKS_DIR,
) -> list[OriginalCase]:
    if seeds is None:
        seeds = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    families: dict[str, list[str]] = {}
    for task_id in task_ids(tasks_dir):
        families.setdefault(TaskId.parse(task_id).number, []).append(task_id)

    originals: list[OriginalCase] = []
    for seed in seeds:
        number = str(seed["seed_id"]).removeprefix("S")
        members = families.get(number, [])
        triggers, domain, harm = _seed_letters(seed)
        for task_id in members:
            parsed = TaskId.parse(task_id)
            if (
                parsed.trigger in triggers
                and parsed.domain == domain
                and parsed.harm == harm
            ):
                originals.append(OriginalCase(
                    seed_id=str(seed["seed_id"]),
                    task_id=task_id,
                    family=list(members),
                ))
    return originals


def seeds_without_original(
    seeds: list[dict[str, Any]] | None = None,
    tasks_dir: Path = TASKS_DIR,
) -> list[str]:
    if seeds is None:
        seeds = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    found = {case.seed_id for case in find_original_cases(seeds, tasks_dir)}
    return [str(s["seed_id"]) for s in seeds if str(s["seed_id"]) not in found]
