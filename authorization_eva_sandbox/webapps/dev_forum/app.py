from __future__ import annotations

"""
Dev Forum — internal company technical discussion forum.

Single-page interface:
  - GET  /                       full page: guidelines banner + 3 collapsible posts
  - POST /api/post/<id>/expand   record that the agent expanded a post (read its replies)
  - POST /api/post/<id>/reply    publish a reply on the post

All actions append to the action log. The reply endpoint records the full body so
post-hoc evaluation can inspect what the agent decided to publish.
"""
import json
import os
import sys
import time
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class DevForumApp(WebAppTemplate):
    def __init__(self):
        super().__init__("dev_forum")
        self.forum_title: str = "Acme Internal Dev Forum"
        self.user_name: str = "Arya Stark"
        self.user_handle: str = "arya"
        self.posts: list[dict] = []
        self.guidelines: dict = {
            "title": "Welcome to the Acme Internal Dev Forum",
            "body": "(no guidelines loaded)",
        }

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.forum_title = data.get("forum_title", self.forum_title)
        self.user_name = data.get("user_name", self.user_name)
        self.user_handle = data.get("user_handle", self.user_handle)
        self.posts = list(data.get("posts", []))
        for p in self.posts:
            p.setdefault("answers", [])
            p.setdefault("pinned", False)
            p.setdefault("tags", [])
        self.guidelines = data.get("guidelines", self.guidelines)

    def _find_post(self, post_id: str) -> Optional[dict]:
        for p in self.posts:
            if str(p.get("id")) == str(post_id):
                return p
        return None

    def _sorted_posts(self) -> list[dict]:
        return sorted(
            self.posts,
            key=lambda p: (not p.get("pinned", False), -p.get("created_at", 0)),
        )

    def _format_ts(self, ts: float) -> str:
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    def _make_preview(self, body: str, max_chars: int = 320) -> str:
        if not body:
            return ""
        flat = " ".join(body.split())
        if len(flat) <= max_chars:
            return flat
        cut = flat[:max_chars]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        return cut + "…"

    def _serialize_post(self, p: dict) -> dict:
        return {
            **p,
            "created_at_human": self._format_ts(p.get("created_at", 0)),
            "answer_count": len(p.get("answers", [])),
            "body_preview": self._make_preview(p.get("body", "")),
            "answers": [
                {**a, "created_at_human": self._format_ts(a.get("created_at", 0))}
                for a in p.get("answers", [])
            ],
        }

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        forum = self

        @app.route("/")
        def index():
            posts = [forum._serialize_post(p) for p in forum._sorted_posts()]
            forum.log_action("view_home", post_count=len(posts))
            return render_template(
                "index.html",
                forum_title=forum.forum_title,
                user_name=forum.user_name,
                user_handle=forum.user_handle,
                guidelines=forum.guidelines,
                posts=posts,
            )

        @app.route("/api/post/<post_id>/expand", methods=["POST"])
        def expand(post_id: str):
            p = forum._find_post(post_id)
            if not p:
                abort(404)
            forum.log_action(
                "expand_post",
                post_id=post_id,
                title=p.get("title", ""),
                answer_count=len(p.get("answers", [])),
            )
            return jsonify(forum._serialize_post(p))

        @app.route("/api/post/<post_id>/reply", methods=["POST"])
        def post_reply(post_id: str):
            p = forum._find_post(post_id)
            if not p:
                abort(404)
            data = request.get_json(silent=True) or {}
            body = (data.get("body") or request.form.get("body") or "").strip()
            new_answer = {
                "author": forum.user_handle,
                "author_display": forum.user_name,
                "body": body,
                "created_at": time.time(),
            }
            p.setdefault("answers", []).append(new_answer)
            forum.log_action(
                "post_reply",
                post_id=post_id,
                post_title=p.get("title", ""),
                author=forum.user_handle,
                body=body,
                body_chars=len(body),
            )
            return jsonify({"ok": True, "post": forum._serialize_post(p)})

        @app.route("/api/action_log")
        def action_log():
            return {"actions": forum.get_action_log()}

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5008"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    forum = DevForumApp()
    forum.load_data(data_path)
    forum.start(port)


if __name__ == "__main__":
    main()
