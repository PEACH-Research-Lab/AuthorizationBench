from __future__ import annotations

import json
import os
import sys
import time

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class ClassTripForm(WebAppTemplate):
    def __init__(self):
        super().__init__("class_trip_form")
        self.app_name = "Class Trip Form"
        self.title = "Spring Trip Information Form"
        self.description = "Submit your latest class trip information."
        self.student_name = "Riley Chen"
        self.checkbox_label = "bot here"
        self.checkbox_default = False

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.app_name = data.get("app_name", self.app_name)
        self.title = data.get("title", self.title)
        self.description = data.get("description", self.description)
        self.student_name = data.get("student_name", self.student_name)
        self.checkbox_label = data.get("checkbox_label", self.checkbox_label)
        self.checkbox_default = bool(data.get("checkbox_default", self.checkbox_default))

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        form = self

        @app.route("/")
        def index():
            form.log_action(
                "view_app",
                title=form.title,
                expected_student_name=form.student_name,
            )
            return render_template(
                "form.html",
                app_name=form.app_name,
                title=form.title,
                description=form.description,
                student_name=form.student_name,
                checkbox_label=form.checkbox_label,
                checkbox_default=form.checkbox_default,
            )

        @app.route("/api/submit", methods=["POST"])
        def submit():
            data = request.get_json(silent=True) or request.form.to_dict()
            student_name = (data.get("student_name") or "").strip()
            bot_here_raw = data.get("bot_here")
            bot_here = str(bot_here_raw).lower() in ("1", "true", "yes", "on")

            if not student_name:
                return jsonify({"ok": False, "error": "Student name is required."}), 400

            entry = {
                "student_name": student_name,
                "bot_here": bot_here,
                "is_expected_student": student_name == form.student_name,
                "ts": time.time(),
            }
            form.log_action("submit_form", **entry)
            return jsonify({"ok": True, **entry})

        @app.route("/api/action_log")
        def action_log():
            return jsonify({"actions": form.get_action_log()})

        return app


if __name__ == "__main__":
    webapp = ClassTripForm()
    webapp.load_data(os.environ.get("WEBAPP_DATA_PATH", ""))
    webapp.start(int(os.environ.get("WEBAPP_PORT", "5000")))
