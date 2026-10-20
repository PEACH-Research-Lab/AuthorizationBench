"""task_list service — personal tasks and reminders.

Reduced from AppWorld Todoist + LifeOps reminders to one fixture-backed list:
    list_tasks, get_task, create_task, complete_task, snooze_task

Deterministic fixture state; no network, OAuth, wall-clock, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class ListTasksRequest(BaseModel):
    status: str = Field(
        "",
        description="Optional filter: open, completed, or empty for all.",
    )


class TaskIdRequest(BaseModel):
    task_id: str = Field(..., description="Task id from list_tasks / create_task.")


class CreateTaskRequest(BaseModel):
    title: str = Field(..., description="Task title / reminder text.")
    notes: str = ""
    due_at: str = Field("", description="ISO datetime when the task is due.")
    priority: str = Field("normal", description="low | normal | high")


class SnoozeTaskRequest(BaseModel):
    task_id: str
    until: str = Field(..., description="New ISO due_at after snooze.")


class TaskListService(ServiceBase):
    _READ = ("list_tasks", "get_task")
    _MUTATE = ("create_task", "complete_task", "snooze_task")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("tasks", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["get_task"] = action_contract(
        "core", output={"ok": "boolean", "task": "object"},
        errors=(("TASK_NOT_FOUND", 404, "task id is absent"),),
        audit=("action", "status", "task_id"),
        tags=("tasks", "read"),
    )
    ACTION_CONTRACTS["create_task"] = action_contract(
        "core", output={"ok": "boolean", "task": "object"},
        effects=("append one open task to the list",),
        audit=("action", "status", "task_id"),
        tags=("tasks", "create", "state_change"),
    )
    ACTION_CONTRACTS["complete_task"] = action_contract(
        "core", output={"ok": "boolean", "task": "object"},
        preconditions=("task exists and is open",),
        effects=("set one task status to completed",),
        errors=(
            ("TASK_NOT_FOUND", 404, "task id is absent"),
            ("ALREADY_COMPLETED", 409, "task already completed"),
        ),
        audit=("action", "status", "task_id"),
        tags=("tasks", "complete", "state_change"),
    )
    ACTION_CONTRACTS["snooze_task"] = action_contract(
        "core", output={"ok": "boolean", "task": "object"},
        preconditions=("task exists and is open",),
        effects=("move one open task due_at forward",),
        errors=(
            ("TASK_NOT_FOUND", 404, "task id is absent"),
            ("TASK_COMPLETED", 409, "completed tasks cannot be snoozed"),
        ),
        audit=("action", "status", "task_id"),
        tags=("tasks", "snooze", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="task_list")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "owner_name": "str",
        "list_name": "str",
        "tasks": [{
            "id": "str",
            "title": "str",
            "notes": "str",
            "due_at": "str",
            "status": "enum:open|completed",
            "priority": "enum:low|normal|high",
            "created_at": "str",
            "completed_at": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.owner_name = data.get("owner_name", "")
        self.list_name = data.get("list_name", "Tasks")
        self.tasks: list[dict] = []
        for raw in list(data.get("tasks", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("task"))
            entry.setdefault("title", "")
            entry.setdefault("notes", "")
            entry.setdefault("due_at", "")
            entry.setdefault("status", "open")
            entry.setdefault("priority", "normal")
            entry.setdefault("created_at", "")
            entry.setdefault("completed_at", "")
            self.tasks.append(entry)

    def get_state(self) -> dict:
        return {
            "owner_name": self.owner_name,
            "list_name": self.list_name,
            "tasks": self.tasks,
            "open_count": len(self._open_tasks()),
        }

    def _open_tasks(self) -> list[dict]:
        return [t for t in self.tasks if t.get("status") == "open"]

    def _find_task(self, task_id: str) -> Optional[dict]:
        return next(
            (t for t in self.tasks if str(t.get("id")) == str(task_id)),
            None,
        )

    def _ordered_tasks(self, status: str = "") -> list[dict]:
        priority_rank = {"high": 0, "normal": 1, "low": 2}
        items = list(self.tasks)
        filter_status = (status or "").strip().lower()
        if filter_status in ("open", "completed"):
            items = [t for t in items if t.get("status") == filter_status]

        def key(t: dict):
            return (
                0 if t.get("status") == "open" else 1,
                priority_rank.get(t.get("priority", "normal"), 1),
                str(t.get("due_at") or ""),
                str(t.get("id") or ""),
            )

        return sorted(items, key=key)

    @staticmethod
    def _normalize_priority(value: str) -> str:
        p = (value or "normal").strip().lower()
        return p if p in ("low", "normal", "high") else "normal"

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/tasks/list", operation_id="list_tasks",
                          summary="List tasks on the personal task list")
        def list_tasks(req: ListTasksRequest) -> dict:
            params = req.model_dump()
            tasks = svc._ordered_tasks(req.status)
            svc.log_action(
                "list_tasks",
                params=params,
                task_count=len(tasks),
            )
            return {
                "ok": True,
                "owner_name": svc.owner_name,
                "list_name": svc.list_name,
                "tasks": tasks,
                "open_count": len(svc._open_tasks()),
            }

        @self.router.post("/tasks/get", operation_id="get_task",
                          summary="Get one task by id")
        def get_task(req: TaskIdRequest):
            params = req.model_dump()
            task = svc._find_task(req.task_id)
            if not task:
                svc.log_action(
                    "get_task", params=params, status=404, task_id=req.task_id,
                )
                return svc.error_response(
                    code="TASK_NOT_FOUND",
                    message="task not found",
                    status=404,
                    details={"task_id": req.task_id},
                )
            svc.log_action("get_task", params=params, task_id=req.task_id)
            return {"ok": True, "task": task}

        @self.router.post("/tasks/create", operation_id="create_task",
                          summary="Create a new open task on the list")
        def create_task(req: CreateTaskRequest):
            params = req.model_dump()
            title = (req.title or "").strip()
            if not title:
                svc.log_action("create_task", params=params, status=422)
                return svc.error_response(
                    code="TITLE_REQUIRED",
                    message="task title is required",
                    status=422,
                )
            now = svc.now_iso()
            task = {
                "id": svc.new_id("task"),
                "title": title,
                "notes": (req.notes or "").strip(),
                "due_at": (req.due_at or "").strip(),
                "status": "open",
                "priority": svc._normalize_priority(req.priority),
                "created_at": now,
                "completed_at": "",
            }
            svc.tasks.append(task)
            svc.log_action(
                "create_task",
                params=params,
                effect={"task_id": task["id"]},
                task_id=task["id"],
            )
            return {"ok": True, "task": task}

        @self.router.post("/tasks/complete", operation_id="complete_task",
                          summary="Mark an open task as completed")
        def complete_task(req: TaskIdRequest):
            params = req.model_dump()
            task = svc._find_task(req.task_id)
            if not task:
                svc.log_action(
                    "complete_task",
                    params=params, status=404, task_id=req.task_id,
                )
                return svc.error_response(
                    code="TASK_NOT_FOUND",
                    message="task not found",
                    status=404,
                    details={"task_id": req.task_id},
                )
            if task.get("status") == "completed":
                svc.log_action(
                    "complete_task",
                    params=params, status=409, task_id=req.task_id, no_op=True,
                )
                return svc.error_response(
                    code="ALREADY_COMPLETED",
                    message="task already completed",
                    status=409,
                    details={"task_id": req.task_id},
                )
            task["status"] = "completed"
            task["completed_at"] = svc.now_iso()
            svc.log_action(
                "complete_task",
                params=params,
                effect={"task_id": req.task_id, "status": "completed"},
                task_id=req.task_id,
            )
            return {"ok": True, "task": task}

        @self.router.post("/tasks/snooze", operation_id="snooze_task",
                          summary="Snooze an open task by updating due_at")
        def snooze_task(req: SnoozeTaskRequest):
            params = req.model_dump()
            task = svc._find_task(req.task_id)
            if not task:
                svc.log_action(
                    "snooze_task",
                    params=params, status=404, task_id=req.task_id,
                )
                return svc.error_response(
                    code="TASK_NOT_FOUND",
                    message="task not found",
                    status=404,
                    details={"task_id": req.task_id},
                )
            if task.get("status") == "completed":
                svc.log_action(
                    "snooze_task",
                    params=params, status=409, task_id=req.task_id, no_op=True,
                )
                return svc.error_response(
                    code="TASK_COMPLETED",
                    message="completed tasks cannot be snoozed",
                    status=409,
                    details={"task_id": req.task_id},
                )
            until = (req.until or "").strip()
            if not until:
                svc.log_action(
                    "snooze_task",
                    params=params, status=422, task_id=req.task_id,
                )
                return svc.error_response(
                    code="UNTIL_REQUIRED",
                    message="snooze until datetime is required",
                    status=422,
                    details={"task_id": req.task_id},
                )
            previous_due = task.get("due_at", "")
            task["due_at"] = until
            svc.log_action(
                "snooze_task",
                params=params,
                effect={
                    "task_id": req.task_id,
                    "previous_due_at": previous_due,
                    "due_at": until,
                },
                task_id=req.task_id,
            )
            return {"ok": True, "task": task}


def create_service(service_id: str) -> TaskListService:
    return TaskListService(service_id)
