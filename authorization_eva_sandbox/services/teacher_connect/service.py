"""teacher_connect service — school/therapist comms inbox (v2 port of webapps/teacher_connect).

Fixture shape unchanged from v1 ``teacher_connect_data.json``:
    app_name, student_name, agent_name, teacher_name, teacher_role,
    requester_label, request_list_title, reply_button_label,
    requests [ {id?, status, subject, body, timestamp_label} ],
    replies (optional, usually empty at boot)

Preserved v1 mechanics:
- Open teacher requests are listed; landing on inbox logs ``view_inbox``.
- Replying marks the request ``answered`` and appends to the replies log with
  the same audit fields as v1 ``send_teacher_reply``.
"""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class RequestIdRequest(BaseModel):
    request_id: str = Field(..., description="Teacher request id.")


class SendReplyRequest(BaseModel):
    request_id: str
    body: str = Field(..., description="Reply text to send to the teacher.")


class TeacherConnectService(ServiceBase):
    _READ = ("view_inbox", "view_request_list", "view_teacher_request")
    _MUTATE = ("send_teacher_reply",)
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("teacher_connect", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["view_teacher_request"] = action_contract(
        "core", output={"request": "object"},
        errors=(("REQUEST_NOT_FOUND", 404, "request id is absent"),),
        audit=("action", "status"),
        tags=("teacher_connect", "read"),
    )
    ACTION_CONTRACTS["send_teacher_reply"] = action_contract(
        "core", output={"status": "string", "reply": "object"},
        effects=("append one reply and mark the request answered",),
        errors=(
            ("REQUEST_NOT_FOUND", 404, "request id is absent"),
            ("BODY_REQUIRED", 400, "reply body is empty"),
        ),
        audit=("action", "status", "request_id"),
        tags=("teacher_connect", "reply", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="teacher_connect")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "student_name": "str",
        "agent_name": "str",
        "teacher_name": "str",
        "teacher_role": "str",
        "requester_label": "str",
        "request_list_title": "str",
        "reply_button_label": "str",
        "requests": [{"id": "str", "status": "str", "subject": "str",
                      "body": "str", "timestamp_label": "str"}],
        "replies": "list",
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "TeacherConnect")
        self.student_name = data.get("student_name", "Jordan Kim")
        self.agent_name = data.get("agent_name", "StudyLens")
        self.teacher_name = data.get("teacher_name", "Ms. Elena Rivera")
        self.teacher_role = data.get("teacher_role", "Algebra II teacher")
        self.requester_label = data.get("requester_label", "teacher")
        self.request_list_title = data.get("request_list_title", "Teacher Requests")
        self.reply_button_label = data.get("reply_button_label", "Send Reply to Teacher")
        self.requests: list[dict] = []
        self.replies: list[dict] = []
        for req in list(data.get("requests", [])):
            entry = dict(req)
            entry.setdefault("id", self.new_id("request"))
            entry.setdefault("status", "open")
            entry.setdefault("subject", "Teacher question")
            entry.setdefault("body", "")
            entry.setdefault("timestamp_label", "today")
            self.requests.append(entry)
        self.replies = list(data.get("replies", []))

    def get_state(self) -> dict:
        return {
            "requests": self.requests,
            "replies": self.replies,
            "open_count": len(self._open_requests()),
            "app_name": self.app_name,
            "student_name": self.student_name,
            "agent_name": self.agent_name,
            "teacher_name": self.teacher_name,
            "teacher_role": self.teacher_role,
            "requester_label": self.requester_label,
            "request_list_title": self.request_list_title,
            "reply_button_label": self.reply_button_label,
        }

    def _open_requests(self) -> list[dict]:
        return [r for r in self.requests if r.get("status") == "open"]

    def _common_ctx(self) -> dict:
        return {
            "app_name": self.app_name,
            "student_name": self.student_name,
            "agent_name": self.agent_name,
            "teacher_name": self.teacher_name,
            "teacher_role": self.teacher_role,
            "requester_label": self.requester_label,
            "request_list_title": self.request_list_title,
            "reply_button_label": self.reply_button_label,
            "open_count": len(self._open_requests()),
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/inbox", operation_id="view_inbox",
                          summary="Open the teacher-requests inbox (v1 landing page)")
        def view_inbox() -> dict:
            open_requests = svc._open_requests()
            svc.log_action("view_inbox", open_count=len(open_requests))
            out = {
                **svc._common_ctx(),
                "requests": svc.requests,
                "replies": svc.replies,
            }
            if open_requests:
                out["first_open_request_id"] = open_requests[0]["id"]
            return out

        @self.router.post("/requests/list", operation_id="view_request_list",
                          summary="List all teacher requests")
        def view_request_list() -> dict:
            svc.log_action("view_request_list", request_count=len(svc.requests))
            return {
                **svc._common_ctx(),
                "requests": svc.requests,
                "replies": svc.replies,
            }

        @self.router.post("/requests/detail", operation_id="view_teacher_request",
                          summary="View one teacher request and its replies")
        def view_teacher_request(req: RequestIdRequest):
            params = req.model_dump()
            item = next((r for r in svc.requests if r.get("id") == req.request_id), None)
            if not item:
                svc.log_action("view_teacher_request", params=params, status=404)
                return svc.error_response(
                    code="REQUEST_NOT_FOUND",
                    message="request not found",
                    status=404,
                    details={"request_id": req.request_id},
                )
            svc.log_action("view_teacher_request", request_id=req.request_id)
            request_replies = [r for r in svc.replies if r.get("request_id") == req.request_id]
            return {
                **svc._common_ctx(),
                "request": item,
                "replies": request_replies,
            }

        @self.router.post("/requests/reply", operation_id="send_teacher_reply",
                          summary="Send a reply to the teacher for a request")
        def send_teacher_reply(req: SendReplyRequest):
            params = req.model_dump()
            item = next((r for r in svc.requests if r.get("id") == req.request_id), None)
            if not item:
                svc.log_action("send_teacher_reply", params=params, status=404)
                return svc.error_response(
                    code="REQUEST_NOT_FOUND",
                    message="request not found",
                    status=404,
                    details={"request_id": req.request_id},
                )
            body = (req.body or "").strip()
            if not body:
                svc.log_action("send_teacher_reply", params=params, status=400)
                return svc.error_response(
                    code="BODY_REQUIRED",
                    message="body required",
                    status=400,
                )
            reply_id = svc.new_id("reply").upper()
            entry = {
                "reply_id": reply_id,
                "request_id": req.request_id,
                "from": svc.agent_name,
                "to": svc.teacher_name,
                "body": body,
                "timestamp": svc.now_iso(),
            }
            svc.replies.append(entry)
            item["status"] = "answered"
            svc.log_action(
                "send_teacher_reply",
                reply_id=reply_id,
                request_id=req.request_id,
                teacher_name=svc.teacher_name,
                student_name=svc.student_name,
                body=body,
            )
            return {"status": "sent", "reply": entry, "request": item}


def create_service(service_id: str) -> TeacherConnectService:
    return TeacherConnectService(service_id)
