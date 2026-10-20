from __future__ import annotations

import json
import os
import sys
import time

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class SavedInfoApp(WebAppTemplate):
    def __init__(self):
        super().__init__("saved_info")
        self.app_name = "Saved Info"
        self.title = "Saved Info"
        self.description = "Long-term facts the assistant may use in future sessions."
        self.user_name = "User"
        self.memories: list[dict] = []

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.app_name = data.get("app_name", self.app_name)
        self.title = data.get("title", self.title)
        self.description = data.get("description", self.description)
        self.user_name = data.get("user_name", self.user_name)
        self.memories = list(data.get("memories", self.memories))

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        saved_info = self

        @app.route("/")
        def index():
            saved_info.log_action("view_saved_info", memory_count=len(saved_info.memories))
            return render_template(
                "index.html",
                app_name=saved_info.app_name,
                title=saved_info.title,
                description=saved_info.description,
                user_name=saved_info.user_name,
                memories=saved_info.memories,
            )

        @app.route("/api/memories", methods=["GET"])
        def list_memories():
            return jsonify({"memories": saved_info.memories})

        @app.route("/api/memories", methods=["POST"])
        def add_memory():
            data = request.get_json(silent=True) or request.form.to_dict()
            text = (data.get("text") or "").strip()
            source = (data.get("source") or "agent").strip()
            confirmed_raw = data.get("confirmed")
            confirmed = str(confirmed_raw).lower() in ("1", "true", "yes", "on")

            if not text:
                return jsonify({"ok": False, "error": "Memory text is required."}), 400

            memory = {
                "id": f"mem-{len(saved_info.memories) + 1:03d}",
                "text": text,
                "source": source,
                "confirmed": confirmed,
                "created_at": time.time(),
            }
            saved_info.memories.append(memory)
            saved_info.log_action("add_memory", **memory)
            return jsonify({"ok": True, "memory": memory})

        @app.route("/api/action_log")
        def action_log():
            return jsonify({"actions": saved_info.get_action_log()})

        return app


if __name__ == "__main__":
    webapp = SavedInfoApp()
    webapp.load_data(os.environ.get("WEBAPP_DATA_PATH", ""))
    webapp.start(int(os.environ.get("WEBAPP_PORT", "5000")))
