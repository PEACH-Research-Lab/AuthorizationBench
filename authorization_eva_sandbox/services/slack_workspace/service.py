"""slack_workspace service — Slack-style workspace as an API (v2 port of webapps/slack_workspace).

Fixture shape unchanged from v1:
    workspace_name, user {name, handle}, members[], channels[{id, name, topic,
    messages[]}], dms[{id, with, messages[], scripted_replies?[]}]

Scripted replies (the scenario's deterministic trap responses) stay OUT of
the agent-visible fixture: they load from the path in the
``{SERVICE_ID}_PRIVATE`` env var (v1 ``webapp_private`` mechanism), shape
``{"dms": {"<handle>": [{"trigger": "first_user_message", "from": ..., "text": ...}]}}``.

External-gap actions (AgentDojo / SmolClaw / Agent-Diff):
    list_members, invite_member, add_reaction.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract

_TRIGGER_MAP = {1: "first_user_message", 2: "second_user_message", 3: "third_user_message"}


class ChannelRequest(BaseModel):
    channel_id: str


class DmRequest(BaseModel):
    handle: str


class SendDmRequest(BaseModel):
    handle: str
    text: str


class SendChannelRequest(BaseModel):
    channel_id: str
    text: str


class AddReactionRequest(BaseModel):
    channel_id: str = Field(..., description="Channel containing the message.")
    message_ts: str = Field(..., description="Message timestamp to react to.")
    reaction: str = Field(..., description="Emoji name without colons.")


class InviteMemberRequest(BaseModel):
    handle: str = Field(..., description="Workspace handle for the invited member.")
    name: str = Field("", description="Display name.")
    title: str = Field("", description="Job title.")
    email: str = Field("", description="Invite email address.")


class SlackWorkspaceService(ServiceBase):
    _EXISTING = (
        "list_conversations",
        "read_channel",
        "read_dm",
        "send_dm",
        "send_channel_message",
    )
    _READ = _EXISTING[:3] + ("list_members",)
    _MUTATE = _EXISTING[3:] + ("add_reaction", "invite_member")
    CAPABILITY_PROFILES = {
        "standard-v1": _EXISTING + ("add_reaction", "list_members", "invite_member"),
        "full-v1": _EXISTING + ("add_reaction", "list_members", "invite_member"),
    }
    ACTION_CONTRACTS = {
        "list_conversations": action_contract(
            "core",
            output={"workspace_name": "string", "channels": "array", "dms": "array"},
            audit=("action", "status"),
            tags=("slack", "conversation", "list", "read"),
        ),
        "read_channel": action_contract(
            "core",
            output={"channel": "object", "messages": "array"},
            errors=(("CHANNEL_NOT_FOUND", 404, "channel id is absent"),),
            audit=("action", "status", "channel_name", "message_count"),
            tags=("slack", "channel", "read"),
        ),
        "read_dm": action_contract(
            "core",
            output={"with": "object", "messages": "array"},
            errors=(("DM_NOT_FOUND", 404, "dm handle is absent"),),
            audit=("action", "status", "message_count"),
            tags=("slack", "dm", "read"),
        ),
        "send_dm": action_contract(
            "core",
            output={"ok": "boolean", "messages": "array"},
            preconditions=("dm conversation exists", "non-empty text"),
            effects=("append one outbound DM message",),
            errors=(("DM_NOT_FOUND", 404, "dm handle is absent"),),
            audit=("action", "status", "text_chars", "user_message_index"),
            tags=("slack", "dm", "send", "state_change"),
        ),
        "send_channel_message": action_contract(
            "core",
            output={"ok": "boolean", "messages": "array"},
            preconditions=("channel exists", "non-empty text"),
            effects=("append one channel message",),
            errors=(("CHANNEL_NOT_FOUND", 404, "channel id is absent"),),
            audit=("action", "status", "channel_name", "text_chars"),
            tags=("slack", "channel", "send", "state_change"),
        ),
        "list_members": action_contract(
            "core",
            output={"ok": "boolean", "members": "array"},
            audit=("action", "status", "member_count"),
            tags=("slack", "members", "list", "read"),
        ),
        "invite_member": action_contract(
            "core",
            output={"ok": "boolean", "member": "object"},
            preconditions=("non-empty handle"),
            effects=("append one workspace member when handle is new",),
            errors=(("INVALID_HANDLE", 422, "handle is empty"),),
            audit=("action", "status", "handle", "no_op"),
            tags=("slack", "members", "invite", "state_change"),
        ),
        "add_reaction": action_contract(
            "core",
            output={"ok": "boolean", "reaction": "object"},
            preconditions=("channel exists", "message exists", "non-empty reaction"),
            effects=("append one emoji reaction to a channel message",),
            errors=(
                ("CHANNEL_NOT_FOUND", 404, "channel id is absent"),
                ("MESSAGE_NOT_FOUND", 404, "message timestamp is absent"),
                ("INVALID_REACTION", 422, "reaction emoji is empty"),
            ),
            audit=("action", "status", "channel_id", "message_ts", "reaction"),
            tags=("slack", "reaction", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="slack_workspace")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "workspace_name": "str",
        "user": {"name": "str", "handle": "str"},
        "members": [{"handle": "str", "name": "str", "title": "str"}],
        "channels": [{"id": "str", "name": "str", "topic": "str",
                      "messages": [{"from": "str", "text": "str",
                                    "ts": "str",
                                    "reactions": [{"id": "str", "emoji": "str",
                                                   "from": "str", "ts": "str"}]}]}],
        "dms": [{"with": "str",
                 "messages": [{"from": "str", "text": "str", "ts": "str"}],
                 "scripted_replies": [{"match": "str", "reply": "str",
                                       "from": "str"}]}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.workspace_name = data.get("workspace_name", "Slack Workspace")
        user = data.get("user", {})
        self.user_name = user.get("name", "User")
        self.user_handle = user.get("handle", "user")
        self.members = list(data.get("members", []))
        self.channels = list(data.get("channels", []))
        self.dms = list(data.get("dms", []))
        for ch in self.channels:
            ch.setdefault("messages", [])
            for msg in ch["messages"]:
                msg.setdefault("reactions", [])
        for dm in self.dms:
            dm.setdefault("messages", [])
            dm.setdefault("scripted_replies", [])
        self._load_private()

    def _load_private(self) -> None:
        path = os.environ.get(f"{self.service_id.upper()}_PRIVATE", "")
        if not path or not os.path.isfile(path):
            return
        try:
            private = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            return
        dm_replies = (private or {}).get("dms", {})
        if not isinstance(dm_replies, dict):
            return
        for dm in self.dms:
            replies = dm_replies.get(dm.get("with"))
            if isinstance(replies, list):
                dm["scripted_replies"] = list(replies)

    def get_state(self) -> dict:
        return {
            "channels": self.channels,
            "dms": self.dms,
            "workspace_name": self.workspace_name,
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "members": self.members,
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _find_member(self, handle: str) -> Optional[dict]:
        return next((m for m in self.members if m.get("handle") == handle), None)

    def _find_channel(self, channel_id: str) -> Optional[dict]:
        return next((c for c in self.channels if str(c.get("id")) == str(channel_id)), None)

    def _find_dm(self, handle: str) -> Optional[dict]:
        return next((d for d in self.dms if d.get("with") == handle), None)

    def _find_channel_message(self, channel: dict, message_ts: str) -> Optional[dict]:
        wanted = str(message_ts)
        return next(
            (m for m in channel.get("messages", []) if str(m.get("ts", "")) == wanted),
            None,
        )

    def _decorate(self, msg: dict) -> dict:
        sender = msg.get("from", "")
        if sender == self.user_handle:
            name = self.user_name
        else:
            member = self._find_member(sender)
            name = member.get("name", sender) if member else sender
        return {**msg, "sender_name": name}

    def _maybe_scripted_reply(self, dm: dict, user_msg_index: int) -> Optional[dict]:
        wanted = _TRIGGER_MAP.get(user_msg_index)
        for reply in dm.get("scripted_replies", []):
            if reply.get("trigger") == wanted and not reply.get("_consumed"):
                reply["_consumed"] = True
                return {"from": reply.get("from"), "ts": time.time(),
                        "text": reply.get("text", ""), "scripted": True}
        return None

    # ── routes ───────────────────────────────────────────────────────────────

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/conversations", operation_id="list_conversations",
                          summary="List channels and direct-message conversations")
        def list_conversations() -> dict:
            svc.log_action("view_workspace", params={})
            return {
                "workspace_name": svc.workspace_name,
                "me": {"name": svc.user_name, "handle": svc.user_handle},
                "channels": [{"id": c.get("id"), "name": c.get("name"),
                              "topic": c.get("topic", ""),
                              "message_count": len(c.get("messages", []))}
                             for c in svc.channels],
                "dms": [{"with": d.get("with"),
                         "name": (svc._find_member(d.get("with", "")) or {}).get("name", d.get("with")),
                         "title": (svc._find_member(d.get("with", "")) or {}).get("title", ""),
                         "message_count": len(d.get("messages", []))}
                        for d in svc.dms],
            }

        @self.router.post("/channel/read", operation_id="read_channel",
                          summary="Read a channel's message timeline")
        def read_channel(req: ChannelRequest):
            ch = svc._find_channel(req.channel_id)
            if not ch:
                return JSONResponse(status_code=404, content={"error": "channel not found"})
            svc.log_action("view_channel", params=req.model_dump(),
                           channel_name=ch.get("name", ""),
                           message_count=len(ch.get("messages", [])))
            return {"channel": {"id": ch.get("id"), "name": ch.get("name"),
                                "topic": ch.get("topic", "")},
                    "messages": [svc._decorate(m) for m in ch.get("messages", [])]}

        @self.router.post("/dm/read", operation_id="read_dm",
                          summary="Read a direct-message conversation")
        def read_dm(req: DmRequest):
            dm = svc._find_dm(req.handle)
            if not dm:
                return JSONResponse(status_code=404, content={"error": "dm not found"})
            svc.log_action("view_dm", params=req.model_dump(),
                           message_count=len(dm.get("messages", [])))
            member = svc._find_member(req.handle) or {}
            return {"with": {"handle": req.handle, "name": member.get("name", req.handle),
                             "title": member.get("title", "")},
                    "messages": [svc._decorate(m) for m in dm.get("messages", [])]}

        @self.router.post("/dm/send", operation_id="send_dm",
                          summary="Send a direct message to a workspace member")
        def send_dm(req: SendDmRequest):
            dm = svc._find_dm(req.handle)
            if not dm:
                return JSONResponse(status_code=404, content={"error": "dm not found"})
            text = req.text.strip()
            if not text:
                return JSONResponse(status_code=400, content={"error": "empty_message"})
            dm.setdefault("messages", []).append(
                {"from": svc.user_handle, "ts": time.time(), "text": text})
            idx = sum(1 for m in dm["messages"] if m.get("from") == svc.user_handle)
            svc.log_action("send_dm", params=req.model_dump(),
                           text_chars=len(text), user_message_index=idx)
            triggered = svc._maybe_scripted_reply(dm, idx)
            if triggered:
                dm["messages"].append(triggered)
                svc.log_action("scripted_reply_injected", params={},
                               sender=triggered.get("from"),
                               text=triggered.get("text", ""),
                               triggered_by_user_message_index=idx)
            return {"ok": True,
                    "messages": [svc._decorate(m) for m in dm["messages"]]}

        @self.router.post("/channel/send", operation_id="send_channel_message",
                          summary="Post a message to a channel")
        def send_channel(req: SendChannelRequest):
            ch = svc._find_channel(req.channel_id)
            if not ch:
                return JSONResponse(status_code=404, content={"error": "channel not found"})
            text = req.text.strip()
            if not text:
                return JSONResponse(status_code=400, content={"error": "empty_message"})
            ch.setdefault("messages", []).append(
                {"from": svc.user_handle, "ts": time.time(), "text": text})
            svc.log_action("send_channel_message", params=req.model_dump(),
                           channel_name=ch.get("name", ""), text_chars=len(text))
            return {"ok": True,
                    "messages": [svc._decorate(m) for m in ch["messages"]]}

        @self.router.post("/members/list", operation_id="list_members",
                          summary="List workspace members")
        def list_members() -> dict:
            members = list(svc.members)
            svc.log_action(
                "list_members",
                params={},
                member_count=len(members),
            )
            return {"ok": True, "members": members}

        @self.router.post("/members/invite", operation_id="invite_member",
                          summary="Invite a member to the workspace")
        def invite_member(req: InviteMemberRequest):
            params = req.model_dump()
            handle = str(req.handle or "").strip()
            if not handle:
                svc.log_action("invite_member", params=params, status=422)
                return svc.error_response(
                    code="INVALID_HANDLE",
                    message="Member handle is required.",
                    status=422,
                )
            existing = svc._find_member(handle)
            if existing:
                svc.log_action(
                    "invite_member",
                    params=params,
                    handle=handle,
                    no_op=True,
                )
                return {"ok": True, "status": "unchanged", "member": existing}
            member = {
                "handle": handle,
                "name": str(req.name or "").strip() or handle,
                "title": str(req.title or "").strip(),
            }
            email = str(req.email or "").strip()
            if email:
                member["email"] = email
            svc.members.append(member)
            svc.log_action(
                "invite_member",
                params=params,
                effect={"handle": handle},
                handle=handle,
                no_op=False,
            )
            return {"ok": True, "status": "invited", "member": member}

        @self.router.post("/channel/react", operation_id="add_reaction",
                          summary="Add an emoji reaction to a channel message")
        def add_reaction(req: AddReactionRequest):
            params = req.model_dump()
            ch = svc._find_channel(req.channel_id)
            if not ch:
                svc.log_action("add_reaction", params=params, status=404)
                return svc.error_response(
                    code="CHANNEL_NOT_FOUND",
                    message="Channel not found.",
                    status=404,
                    details={"channel_id": req.channel_id},
                )
            emoji = str(req.reaction or "").strip().strip(":")
            if not emoji:
                svc.log_action("add_reaction", params=params, status=422)
                return svc.error_response(
                    code="INVALID_REACTION",
                    message="Reaction emoji is required.",
                    status=422,
                    details={"channel_id": req.channel_id},
                )
            message = svc._find_channel_message(ch, req.message_ts)
            if not message:
                svc.log_action("add_reaction", params=params, status=404)
                return svc.error_response(
                    code="MESSAGE_NOT_FOUND",
                    message="Message not found.",
                    status=404,
                    details={
                        "channel_id": req.channel_id,
                        "message_ts": req.message_ts,
                    },
                )
            reaction = {
                "id": svc.new_id("reaction"),
                "emoji": emoji,
                "from": svc.user_handle,
                "ts": svc.now_iso(),
            }
            message.setdefault("reactions", []).append(reaction)
            svc.log_action(
                "add_reaction",
                params=params,
                effect={
                    "channel_id": req.channel_id,
                    "message_ts": str(req.message_ts),
                    "reaction_id": reaction["id"],
                    "emoji": emoji,
                },
                channel_id=req.channel_id,
                message_ts=str(req.message_ts),
                reaction=emoji,
            )
            return {"ok": True, "reaction": reaction}


def create_service(service_id: str) -> SlackWorkspaceService:
    return SlackWorkspaceService(service_id)
