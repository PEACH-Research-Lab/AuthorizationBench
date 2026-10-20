"""issue_tracker service — team issues with status and assignee.

Linear-shaped surface (issues, status workflow, assignees) without matching
Linear's public API. Actions:

    list_issues, get_issue, create_issue, update_issue

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

_STATUSES = ("backlog", "todo", "in_progress", "done", "cancelled")
_PRIORITIES = ("none", "low", "medium", "high", "urgent")


class ListIssuesRequest(BaseModel):
    status: str = Field(
        "",
        description="Optional filter: backlog, todo, in_progress, done, cancelled.",
    )
    assignee: str = Field(
        "",
        description="Optional filter by assignee handle / display name.",
    )


class IssueIdRequest(BaseModel):
    issue_id: str = Field(..., description="Issue id from list_issues / create_issue.")


class CreateIssueRequest(BaseModel):
    title: str = Field(..., description="Issue title.")
    description: str = ""
    status: str = Field("todo", description="Initial status.")
    assignee: str = Field("", description="Assignee handle; empty for unassigned.")
    priority: str = Field("none", description="none | low | medium | high | urgent")


class UpdateIssueRequest(BaseModel):
    issue_id: str
    status: str = Field(
        "",
        description="New status; omit or empty to leave unchanged.",
    )
    assignee: str = Field(
        "",
        description="New assignee; omit or empty to leave unchanged. "
        "Pass literal 'unassigned' to clear.",
    )


class IssueTrackerService(ServiceBase):
    _READ = ("list_issues", "get_issue")
    _MUTATE = ("create_issue", "update_issue")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("issue_tracker", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["get_issue"] = action_contract(
        "core", output={"ok": "boolean", "issue": "object"},
        errors=(("ISSUE_NOT_FOUND", 404, "issue id is absent"),),
        audit=("action", "status", "issue_id"),
        tags=("issue_tracker", "read"),
    )
    ACTION_CONTRACTS["create_issue"] = action_contract(
        "core", output={"ok": "boolean", "issue": "object"},
        effects=("append one issue to the team board",),
        audit=("action", "status", "issue_id"),
        tags=("issue_tracker", "create", "state_change"),
    )
    ACTION_CONTRACTS["update_issue"] = action_contract(
        "core", output={"ok": "boolean", "issue": "object"},
        preconditions=("issue exists",),
        effects=("update one issue status and/or assignee",),
        errors=(("ISSUE_NOT_FOUND", 404, "issue id is absent"),),
        audit=("action", "status", "issue_id"),
        tags=("issue_tracker", "update", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="issue_tracker")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "workspace_name": "str",
        "team_key": "str",
        "team_name": "str",
        "current_user": "str",
        "issues": [{
            "id": "str",
            "title": "str",
            "description": "str",
            "status": "enum:backlog|todo|in_progress|done|cancelled",
            "assignee": "str",
            "priority": "enum:none|low|medium|high|urgent",
            "created_at": "str",
            "updated_at": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.workspace_name = data.get("workspace_name", "")
        self.team_key = data.get("team_key", "TEAM")
        self.team_name = data.get("team_name", "Team")
        self.current_user = data.get("current_user", "")
        self.issues: list[dict] = []
        for raw in list(data.get("issues", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("issue"))
            entry.setdefault("title", "")
            entry.setdefault("description", "")
            entry.setdefault("status", "todo")
            entry.setdefault("assignee", "")
            entry.setdefault("priority", "none")
            entry.setdefault("created_at", "")
            entry.setdefault("updated_at", "")
            self.issues.append(entry)

    def get_state(self) -> dict:
        return {
            "workspace_name": self.workspace_name,
            "team_key": self.team_key,
            "team_name": self.team_name,
            "current_user": self.current_user,
            "issues": self.issues,
            "open_count": len(self._open_issues()),
        }

    def _open_issues(self) -> list[dict]:
        return [
            i for i in self.issues
            if i.get("status") not in ("done", "cancelled")
        ]

    def _find_issue(self, issue_id: str) -> Optional[dict]:
        return next(
            (i for i in self.issues if str(i.get("id")) == str(issue_id)),
            None,
        )

    def _ordered_issues(self, status: str = "", assignee: str = "") -> list[dict]:
        status_rank = {
            "in_progress": 0,
            "todo": 1,
            "backlog": 2,
            "done": 3,
            "cancelled": 4,
        }
        priority_rank = {
            "urgent": 0,
            "high": 1,
            "medium": 2,
            "low": 3,
            "none": 4,
        }
        items = list(self.issues)
        filter_status = (status or "").strip().lower()
        if filter_status in _STATUSES:
            items = [i for i in items if i.get("status") == filter_status]
        filter_assignee = (assignee or "").strip()
        if filter_assignee:
            items = [
                i for i in items
                if str(i.get("assignee", "")) == filter_assignee
            ]

        def key(issue: dict):
            return (
                status_rank.get(issue.get("status", "todo"), 1),
                priority_rank.get(issue.get("priority", "none"), 4),
                str(issue.get("id") or ""),
            )

        return sorted(items, key=key)

    @staticmethod
    def _normalize_status(value: str, default: str = "todo") -> str:
        s = (value or default).strip().lower()
        return s if s in _STATUSES else default

    @staticmethod
    def _normalize_priority(value: str) -> str:
        p = (value or "none").strip().lower()
        return p if p in _PRIORITIES else "none"

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/issues/list", operation_id="list_issues",
                          summary="List issues on the team board")
        def list_issues(req: ListIssuesRequest) -> dict:
            params = req.model_dump()
            issues = svc._ordered_issues(req.status, req.assignee)
            svc.log_action(
                "list_issues",
                params=params,
                issue_count=len(issues),
            )
            return {
                "ok": True,
                "workspace_name": svc.workspace_name,
                "team_key": svc.team_key,
                "team_name": svc.team_name,
                "issues": issues,
                "open_count": len(svc._open_issues()),
            }

        @self.router.post("/issues/get", operation_id="get_issue",
                          summary="Get one issue by id")
        def get_issue(req: IssueIdRequest):
            params = req.model_dump()
            issue = svc._find_issue(req.issue_id)
            if not issue:
                svc.log_action(
                    "get_issue", params=params, status=404, issue_id=req.issue_id,
                )
                return svc.error_response(
                    code="ISSUE_NOT_FOUND",
                    message="issue not found",
                    status=404,
                    details={"issue_id": req.issue_id},
                )
            svc.log_action("get_issue", params=params, issue_id=req.issue_id)
            return {"ok": True, "issue": issue}

        @self.router.post("/issues/create", operation_id="create_issue",
                          summary="Create a new issue on the team board")
        def create_issue(req: CreateIssueRequest):
            params = req.model_dump()
            title = (req.title or "").strip()
            if not title:
                svc.log_action("create_issue", params=params, status=422)
                return svc.error_response(
                    code="TITLE_REQUIRED",
                    message="issue title is required",
                    status=422,
                )
            now = svc.now_iso()
            issue = {
                "id": svc.new_id("issue"),
                "title": title,
                "description": (req.description or "").strip(),
                "status": svc._normalize_status(req.status),
                "assignee": (req.assignee or "").strip(),
                "priority": svc._normalize_priority(req.priority),
                "created_at": now,
                "updated_at": now,
            }
            svc.issues.append(issue)
            svc.log_action(
                "create_issue",
                params=params,
                effect={"issue_id": issue["id"]},
                issue_id=issue["id"],
            )
            return {"ok": True, "issue": issue}

        @self.router.post("/issues/update", operation_id="update_issue",
                          summary="Update an issue status and/or assignee")
        def update_issue(req: UpdateIssueRequest):
            params = req.model_dump()
            issue = svc._find_issue(req.issue_id)
            if not issue:
                svc.log_action(
                    "update_issue",
                    params=params, status=404, issue_id=req.issue_id,
                )
                return svc.error_response(
                    code="ISSUE_NOT_FOUND",
                    message="issue not found",
                    status=404,
                    details={"issue_id": req.issue_id},
                )

            previous = {
                "status": issue.get("status", ""),
                "assignee": issue.get("assignee", ""),
            }
            changed = False

            new_status = (req.status or "").strip().lower()
            if new_status:
                if new_status not in _STATUSES:
                    svc.log_action(
                        "update_issue",
                        params=params, status=422, issue_id=req.issue_id,
                    )
                    return svc.error_response(
                        code="INVALID_STATUS",
                        message="status must be backlog, todo, in_progress, done, or cancelled",
                        status=422,
                        details={"issue_id": req.issue_id, "status": req.status},
                    )
                if issue.get("status") != new_status:
                    issue["status"] = new_status
                    changed = True

            # Empty string means leave assignee unchanged; "unassigned" clears it.
            if "assignee" in params and req.assignee is not None:
                raw_assignee = req.assignee.strip()
                if raw_assignee:
                    new_assignee = (
                        "" if raw_assignee.lower() == "unassigned" else raw_assignee
                    )
                    if issue.get("assignee", "") != new_assignee:
                        issue["assignee"] = new_assignee
                        changed = True

            if changed:
                issue["updated_at"] = svc.now_iso()

            svc.log_action(
                "update_issue",
                params=params,
                effect={
                    "issue_id": req.issue_id,
                    "previous": previous,
                    "status": issue.get("status"),
                    "assignee": issue.get("assignee"),
                    "changed": changed,
                },
                issue_id=req.issue_id,
            )
            return {"ok": True, "issue": issue}


def create_service(service_id: str) -> IssueTrackerService:
    return IssueTrackerService(service_id)
