from __future__ import annotations

import json
import os
import sys
import time
import uuid

from flask import Flask, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class TeacherConnectApp(WebAppTemplate):
    def __init__(self):
        super().__init__("teacher_connect")
        self.app_name = "TeacherConnect"
        self.student_name = "Jordan Kim"
        self.agent_name = "StudyLens"
        self.teacher_name = "Ms. Elena Rivera"
        self.teacher_role = "Algebra II teacher"
        self.requester_label = "teacher"
        self.request_list_title = "Teacher Requests"
        self.reply_button_label = "Send Reply to Teacher"
        self.requests: list[dict] = []
        self.replies: list[dict] = []

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.app_name = data.get("app_name", self.app_name)
        self.student_name = data.get("student_name", self.student_name)
        self.agent_name = data.get("agent_name", self.agent_name)
        self.teacher_name = data.get("teacher_name", self.teacher_name)
        self.teacher_role = data.get("teacher_role", self.teacher_role)
        self.requester_label = data.get("requester_label", self.requester_label)
        self.request_list_title = data.get("request_list_title", self.request_list_title)
        self.reply_button_label = data.get("reply_button_label", self.reply_button_label)
        self.requests = list(data.get("requests", []))
        for req in self.requests:
            req.setdefault("id", str(uuid.uuid4())[:8])
            req.setdefault("status", "open")
            req.setdefault("subject", "Teacher question")
            req.setdefault("body", "")
            req.setdefault("timestamp_label", "today")

    def _open_requests(self) -> list[dict]:
        return [r for r in self.requests if r.get("status") == "open"]

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        platform = self

        def _common_ctx():
            return {
                "app_name": platform.app_name,
                "student_name": platform.student_name,
                "agent_name": platform.agent_name,
                "teacher_name": platform.teacher_name,
                "teacher_role": platform.teacher_role,
                "requester_label": platform.requester_label,
                "request_list_title": platform.request_list_title,
                "reply_button_label": platform.reply_button_label,
                "open_count": len(platform._open_requests()),
            }

        @app.route("/")
        def index():
            platform.log_action("view_inbox", open_count=len(platform._open_requests()))
            open_requests = platform._open_requests()
            if open_requests:
                return redirect(url_for("request_detail", request_id=open_requests[0]["id"]))
            return render_template(
                "inbox.html",
                requests=platform.requests,
                replies=platform.replies,
                **_common_ctx(),
            )

        @app.route("/requests")
        def request_list():
            platform.log_action("view_request_list", request_count=len(platform.requests))
            return render_template(
                "inbox.html",
                requests=platform.requests,
                replies=platform.replies,
                **_common_ctx(),
            )

        @app.route("/requests/<request_id>")
        def request_detail(request_id: str):
            req = next((r for r in platform.requests if r.get("id") == request_id), None)
            if not req:
                return "Request not found", 404
            platform.log_action("view_teacher_request", request_id=request_id)
            request_replies = [r for r in platform.replies if r.get("request_id") == request_id]
            return render_template(
                "request_detail.html",
                request_item=req,
                replies=request_replies,
                **_common_ctx(),
            )

        @app.route("/requests/<request_id>/reply", methods=["POST"])
        def send_reply(request_id: str):
            req = next((r for r in platform.requests if r.get("id") == request_id), None)
            if not req:
                return "Request not found", 404
            body = (request.form.get("body") or "").strip()
            if not body:
                return redirect(url_for("request_detail", request_id=request_id))
            reply_id = str(uuid.uuid4())[:8].upper()
            entry = {
                "reply_id": reply_id,
                "request_id": request_id,
                "from": platform.agent_name,
                "to": platform.teacher_name,
                "body": body,
                "timestamp": time.time(),
            }
            platform.replies.append(entry)
            req["status"] = "answered"
            platform.log_action(
                "send_teacher_reply",
                reply_id=reply_id,
                request_id=request_id,
                teacher_name=platform.teacher_name,
                student_name=platform.student_name,
                body=body,
            )
            return redirect(url_for("request_detail", request_id=request_id))

        @app.route("/api/action_log")
        def action_log():
            return jsonify(platform.get_action_log())

        @app.route("/api/requests")
        def api_requests():
            return jsonify(platform.requests)

        @app.route("/api/replies")
        def api_replies():
            return jsonify(platform.replies)

        return app


if __name__ == "__main__":
    webapp = TeacherConnectApp()
    webapp.load_data(os.environ.get("WEBAPP_DATA_PATH", ""))
    webapp.start(int(os.environ.get("WEBAPP_PORT", "5011")))
