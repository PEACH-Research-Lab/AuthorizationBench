"""dev_forum service — internal company technical forum (v2 port of webapps/dev_forum).

Fixture shape unchanged from v1 ``dev_forum_data.json``:
    forum_title, user_name, user_handle, guidelines, posts[]

Preserved v1 mechanics: expand_post logs answer count; post_reply logs full body.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class PostIdRequest(BaseModel):
    post_id: str


class ReplyRequest(BaseModel):
    post_id: str
    body: str = ""


class DevForumService(ServiceBase):
    _READ = ("view_home", "expand_post")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + ("post_reply",),
        "full-v1": _READ + ("post_reply",),
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("forum", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["post_reply"] = action_contract(
        "core", output={"ok": "boolean"},
        effects=("publish one reply on a forum post",),
        audit=("action", "status", "post_id"),
        tags=("forum", "reply", "publish", "state_change"),
    )


    def __init__(self, service_id: str):
        super().__init__(service_id, template="dev_forum")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "forum_title": "str",
        "user_name": "str",
        "user_handle": "str",
        "posts": [{"id": "str", "title": "str", "body": "str", "author": "str",
                   "timestamp": "str", "pinned": "bool", "tags": "list",
                   "answers": "list"}],
        "guidelines": {"title": "str", "body": "str"},
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.forum_title = data.get("forum_title", "Acme Internal Dev Forum")
        self.user_name = data.get("user_name", "Arya Stark")
        self.user_handle = data.get("user_handle", "arya")
        self.posts = list(data.get("posts", []))
        for p in self.posts:
            p.setdefault("answers", [])
            p.setdefault("pinned", False)
            p.setdefault("tags", [])
        self.guidelines = data.get(
            "guidelines",
            {"title": "Welcome to the Acme Internal Dev Forum", "body": "(no guidelines loaded)"},
        )

    def get_state(self) -> dict:
        return {"posts": self.posts, "forum_title": self.forum_title,
                "user_name": self.user_name, "user_handle": self.user_handle,
                "guidelines": self.guidelines}

    def _find_post(self, post_id: str) -> Optional[dict]:
        return next((p for p in self.posts if str(p.get("id")) == str(post_id)), None)

    def _sorted_posts(self) -> list[dict]:
        return sorted(
            self.posts,
            key=lambda p: (not p.get("pinned", False), -p.get("created_at", 0)),
        )

    def _serialize_post(self, p: dict) -> dict:
        return {
            **p,
            "answer_count": len(p.get("answers", [])),
            "answers": list(p.get("answers", [])),
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/home", operation_id="view_home",
                          summary="View the forum home page with all posts")
        def view_home() -> dict:
            posts = [svc._serialize_post(p) for p in svc._sorted_posts()]
            svc.log_action("view_home", post_count=len(posts))
            return {
                "forum_title": svc.forum_title,
                "user_name": svc.user_name,
                "user_handle": svc.user_handle,
                "guidelines": svc.guidelines,
                "posts": posts,
            }

        @self.router.post("/post/expand", operation_id="expand_post",
                          summary="Expand a post to read its replies")
        def expand_post(req: PostIdRequest):
            p = svc._find_post(req.post_id)
            if not p:
                return JSONResponse(status_code=404, content={"error": "post not found"})
            svc.log_action(
                "expand_post",
                post_id=req.post_id,
                title=p.get("title", ""),
                answer_count=len(p.get("answers", [])),
            )
            return svc._serialize_post(p)

        @self.router.post("/post/reply", operation_id="post_reply",
                          summary="Publish a reply on a forum post")
        def post_reply(req: ReplyRequest):
            p = svc._find_post(req.post_id)
            if not p:
                return JSONResponse(status_code=404, content={"error": "post not found"})
            body = (req.body or "").strip()
            new_answer = {
                "author": svc.user_handle,
                "author_display": svc.user_name,
                "body": body,
                "created_at": time.time(),
            }
            p.setdefault("answers", []).append(new_answer)
            svc.log_action(
                "post_reply",
                post_id=req.post_id,
                post_title=p.get("title", ""),
                author=svc.user_handle,
                body=body,
                body_chars=len(body),
            )
            return {"ok": True, "post": svc._serialize_post(p)}


def create_service(service_id: str) -> DevForumService:
    return DevForumService(service_id)
