"""discord_app service — simulated Discord web client (v2 port of webapps/discord_app).

Fixture shape unchanged from v1 ``discord_app_data.json``:
    app_name, user {name, handle, discriminator, avatar_color},
    servers [ {id, name, icon_color?, channels [ {id, name, topic, messages} ],
        members [], roles [] } ]

Preserved v1 mechanics:
- Per-message sender provenance (display name, discriminator, account age,
  role badge) exposed on ``view_message`` for impersonation-trap scenarios.

External-gap actions (SmolClaw Discord / Slack parity):
    list_members, invite_member, add_reaction.
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


class ServerRequest(BaseModel):
    server_id: str


class ChannelRequest(BaseModel):
    server_id: str
    channel_id: str


class MessageRequest(BaseModel):
    server_id: str
    channel_id: str
    msg_index: int = Field(..., ge=0)


class SendMessageRequest(BaseModel):
    server_id: str
    channel_id: str
    text: str


class ListMembersRequest(BaseModel):
    server_id: str = Field(..., description="Guild whose roster to list.")


class InviteMemberRequest(BaseModel):
    server_id: str = Field(..., description="Guild to invite the member into.")
    handle: str = Field(..., description="Discord handle for the invited member.")
    name: str = Field("", description="Display name.")
    discriminator: str = Field("", description="Four-digit discriminator.")


class AddReactionRequest(BaseModel):
    server_id: str = Field(..., description="Guild containing the channel.")
    channel_id: str = Field(..., description="Channel containing the message.")
    message_ts: str = Field(..., description="Message timestamp to react to.")
    reaction: str = Field(..., description="Emoji name without colons.")


class DiscordAppService(ServiceBase):
    _EXISTING = (
        "view_app",
        "view_server",
        "view_channel",
        "view_message",
        "send_message",
    )
    CAPABILITY_PROFILES = {
        "standard-v1": _EXISTING + ("add_reaction", "list_members", "invite_member"),
        "full-v1": _EXISTING + ("add_reaction", "list_members", "invite_member"),
    }
    ACTION_CONTRACTS = {
        "view_app": action_contract(
            "core",
            output={"app_name": "string", "servers": "array"},
            audit=("action", "status"),
            tags=("discord", "app", "read"),
        ),
        "view_server": action_contract(
            "core",
            output={"server": "object"},
            errors=(("SERVER_NOT_FOUND", 404, "server id is absent"),),
            audit=("action", "status", "server_id", "channel_count"),
            tags=("discord", "server", "read"),
        ),
        "view_channel": action_contract(
            "core",
            output={"messages": "array", "channel_name": "string"},
            errors=(("CHANNEL_NOT_FOUND", 404, "channel id is absent"),),
            audit=("action", "status", "channel_id", "message_count"),
            tags=("discord", "channel", "read"),
        ),
        "view_message": action_contract(
            "core",
            output={"ok": "boolean", "message": "object"},
            errors=(
                ("CHANNEL_NOT_FOUND", 404, "channel id is absent"),
                ("MESSAGE_NOT_FOUND", 404, "message index is absent"),
            ),
            audit=("action", "status", "msg_index", "sender_handle"),
            tags=("discord", "message", "read"),
        ),
        "send_message": action_contract(
            "core",
            output={"ok": "boolean", "messages": "array"},
            preconditions=("channel exists", "non-empty text"),
            effects=("append one channel message",),
            errors=(("CHANNEL_NOT_FOUND", 404, "channel id is absent"),),
            audit=("action", "status", "channel_id", "text_chars"),
            tags=("discord", "message", "send", "state_change"),
        ),
        "list_members": action_contract(
            "core",
            output={"ok": "boolean", "members": "array"},
            errors=(("SERVER_NOT_FOUND", 404, "server id is absent"),),
            audit=("action", "status", "server_id", "member_count"),
            tags=("discord", "members", "list", "read"),
        ),
        "invite_member": action_contract(
            "core",
            output={"ok": "boolean", "member": "object"},
            preconditions=("server exists", "non-empty handle"),
            effects=("append one guild member when handle is new",),
            errors=(
                ("SERVER_NOT_FOUND", 404, "server id is absent"),
                ("INVALID_HANDLE", 422, "handle is empty"),
            ),
            audit=("action", "status", "server_id", "handle", "no_op"),
            tags=("discord", "members", "invite", "state_change"),
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
            tags=("discord", "reaction", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="discord_app")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "user": {"name": "str", "handle": "str", "discriminator": "str",
                 "avatar_color": "str"},
        "servers": [{"id": "str", "name": "str",
                     "channels": [{"id": "str", "name": "str", "topic": "str",
                                   "messages": [{"from": "str", "text": "str",
                                                 "ts": "str",
                                                 "reactions": [{"id": "str",
                                                                "emoji": "str",
                                                                "from": "str",
                                                                "ts": "str"}]}]}],
                     "members": [{"handle": "str", "name": "str"}],
                     "roles": "list"}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "Discord")
        user = data.get("user", {})
        self.user_name = user.get("name", "User")
        self.user_handle = user.get("handle", "user")
        self.user_discriminator = str(user.get("discriminator", "0001"))
        self.user_avatar_color = user.get("avatar_color", "#5865f2")
        self.servers = list(data.get("servers", []))
        self.member_index: dict[str, dict] = {}
        for srv in self.servers:
            srv.setdefault("channels", [])
            srv.setdefault("members", [])
            srv.setdefault("roles", [])
            for ch in srv["channels"]:
                ch.setdefault("topic", "")
                ch.setdefault("messages", [])
                for msg in ch["messages"]:
                    msg.setdefault("reactions", [])
            for m in srv["members"]:
                self.member_index[m.get("handle", "")] = m

    def get_state(self) -> dict:
        return {"servers": self.servers, "app_name": self.app_name,
                "user_name": self.user_name, "user_handle": self.user_handle,
                "user_discriminator": self.user_discriminator}

    def _find_server(self, server_id: str) -> Optional[dict]:
        return next((s for s in self.servers if str(s.get("id")) == str(server_id)), None)

    def _find_channel(self, server_id: str, channel_id: str) -> Optional[dict]:
        srv = self._find_server(server_id)
        if not srv:
            return None
        return next(
            (c for c in srv.get("channels", []) if str(c.get("id")) == str(channel_id)),
            None,
        )

    def _find_member_in(self, server: dict, handle: str) -> Optional[dict]:
        for m in server.get("members", []):
            if m.get("handle") == handle:
                return m
        return self.member_index.get(handle)

    def _find_server_member(self, server: dict, handle: str) -> Optional[dict]:
        return next(
            (m for m in server.get("members", []) if m.get("handle") == handle),
            None,
        )

    def _find_channel_message(self, channel: dict, message_ts: str) -> Optional[dict]:
        wanted = str(message_ts)
        return next(
            (m for m in channel.get("messages", []) if str(m.get("ts", "")) == wanted),
            None,
        )

    def _decorate_message(self, server: dict, msg: dict) -> dict:
        sender_handle = msg.get("from", "")
        if sender_handle == self.user_handle:
            sender = {
                "handle": self.user_handle,
                "display_name": self.user_name,
                "avatar_color": self.user_avatar_color,
                "discriminator": self.user_discriminator,
                "account_age_days": None,
                "badge": None,
            }
            is_self = True
        else:
            member = self._find_member_in(server, sender_handle) or {}
            sender = {
                "handle": sender_handle,
                "display_name": member.get("display_name") or member.get("name") or sender_handle,
                "avatar_color": member.get("avatar_color", "#7c8a99"),
                "discriminator": str(member.get("discriminator", "")),
                "account_age_days": member.get("account_age_days"),
                "badge": member.get("badge"),
            }
            is_self = False
        return {
            **msg,
            "sender": sender,
            "is_self": is_self,
            "kind": msg.get("kind", "text"),
        }

    def _user_ctx(self) -> dict:
        return {
            "app_name": self.app_name,
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "user_discriminator": self.user_discriminator,
            "user_avatar_color": self.user_avatar_color,
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/app", operation_id="view_app",
                          summary="Open the Discord client (v1 landing page)")
        def view_app() -> dict:
            svc.log_action("view_app")
            hint: dict = {}
            if svc.servers:
                first_srv = svc.servers[0]
                channels = first_srv.get("channels") or []
                hint = {
                    "default_server_id": str(first_srv.get("id")),
                    "default_channel_id": str(channels[0].get("id")) if channels else None,
                }
            return {**svc._user_ctx(), "servers": svc.servers, **hint}

        @self.router.post("/server", operation_id="view_server",
                          summary="Open a Discord server (guild)")
        def view_server(req: ServerRequest):
            srv = svc._find_server(req.server_id)
            if not srv:
                return JSONResponse(status_code=404, content={"error": "server not found"})
            svc.log_action(
                "view_server",
                server_id=req.server_id,
                server_name=srv.get("name", ""),
                channel_count=len(srv.get("channels", [])),
            )
            channels = srv.get("channels") or []
            out = {**svc._user_ctx(), "server": srv}
            if channels:
                out["default_channel_id"] = str(channels[0].get("id"))
            return out

        @self.router.post("/channel", operation_id="view_channel",
                          summary="Open a channel and read its message stream")
        def view_channel(req: ChannelRequest):
            srv = svc._find_server(req.server_id)
            ch = svc._find_channel(req.server_id, req.channel_id)
            if not srv or not ch:
                return JSONResponse(status_code=404, content={"error": "channel not found"})
            svc.log_action(
                "view_channel",
                server_id=req.server_id,
                server_name=srv.get("name", ""),
                channel_id=req.channel_id,
                channel_name=ch.get("name", ""),
                channel_topic=ch.get("topic", ""),
                message_count=len(ch.get("messages", [])),
            )
            messages = [svc._decorate_message(srv, m) for m in ch.get("messages", [])]
            return {
                **svc._user_ctx(),
                "server_id": req.server_id,
                "channel_id": req.channel_id,
                "channel_name": ch.get("name", ""),
                "channel_topic": ch.get("topic", ""),
                "messages": messages,
            }

        @self.router.post("/message", operation_id="view_message",
                          summary="Fetch one message with full sender provenance")
        def view_message(req: MessageRequest):
            srv = svc._find_server(req.server_id)
            ch = svc._find_channel(req.server_id, req.channel_id)
            if not srv or not ch:
                return JSONResponse(status_code=404, content={"error": "channel not found"})
            msgs = ch.get("messages", [])
            if req.msg_index < 0 or req.msg_index >= len(msgs):
                return JSONResponse(status_code=404, content={"error": "message not found"})
            msg = msgs[req.msg_index]
            decorated = svc._decorate_message(srv, msg)
            sender = decorated.get("sender", {})
            svc.log_action(
                "view_message",
                server_id=req.server_id,
                server_name=srv.get("name", ""),
                channel_id=req.channel_id,
                channel_name=ch.get("name", ""),
                msg_index=req.msg_index,
                sender_handle=sender.get("handle"),
                sender_display_name=sender.get("display_name"),
                sender_discriminator=sender.get("discriminator"),
                sender_account_age_days=sender.get("account_age_days"),
                sender_badge=sender.get("badge"),
            )
            return {"ok": True, "message": decorated}

        @self.router.post("/send", operation_id="send_message",
                          summary="Post a message into a channel")
        def send_message(req: SendMessageRequest):
            srv = svc._find_server(req.server_id)
            ch = svc._find_channel(req.server_id, req.channel_id)
            if not srv or not ch:
                return JSONResponse(status_code=404, content={"error": "channel not found"})
            text = (req.text or "").strip()
            if not text:
                return JSONResponse(status_code=400, content={"ok": False, "error": "empty_message"})
            msg = {
                "from": svc.user_handle,
                "ts": time.time(),
                "text": text,
                "kind": "text",
                "reactions": [],
            }
            ch.setdefault("messages", []).append(msg)
            svc.log_action(
                "send_message",
                server_id=req.server_id,
                server_name=srv.get("name", ""),
                channel_id=req.channel_id,
                channel_name=ch.get("name", ""),
                text=text,
                text_chars=len(text),
            )
            messages = [svc._decorate_message(srv, m) for m in ch["messages"]]
            return {"ok": True, "messages": messages}

        @self.router.post("/members/list", operation_id="list_members",
                          summary="List members of a Discord server")
        def list_members(req: ListMembersRequest) -> dict:
            params = req.model_dump()
            srv = svc._find_server(req.server_id)
            if not srv:
                svc.log_action("list_members", params=params, status=404)
                return svc.error_response(
                    code="SERVER_NOT_FOUND",
                    message="Server not found.",
                    status=404,
                    details={"server_id": req.server_id},
                )
            members = list(srv.get("members", []))
            svc.log_action(
                "list_members",
                params=params,
                server_id=req.server_id,
                member_count=len(members),
            )
            return {"ok": True, "server_id": req.server_id, "members": members}

        @self.router.post("/members/invite", operation_id="invite_member",
                          summary="Invite a member to a Discord server")
        def invite_member(req: InviteMemberRequest):
            params = req.model_dump()
            srv = svc._find_server(req.server_id)
            if not srv:
                svc.log_action("invite_member", params=params, status=404)
                return svc.error_response(
                    code="SERVER_NOT_FOUND",
                    message="Server not found.",
                    status=404,
                    details={"server_id": req.server_id},
                )
            handle = str(req.handle or "").strip()
            if not handle:
                svc.log_action("invite_member", params=params, status=422)
                return svc.error_response(
                    code="INVALID_HANDLE",
                    message="Member handle is required.",
                    status=422,
                    details={"server_id": req.server_id},
                )
            existing = svc._find_server_member(srv, handle)
            if existing:
                svc.log_action(
                    "invite_member",
                    params=params,
                    server_id=req.server_id,
                    handle=handle,
                    no_op=True,
                )
                return {"ok": True, "status": "unchanged", "member": existing}
            member = {
                "handle": handle,
                "name": str(req.name or "").strip() or handle,
            }
            discriminator = str(req.discriminator or "").strip()
            if discriminator:
                member["discriminator"] = discriminator
            srv.setdefault("members", []).append(member)
            svc.member_index[handle] = member
            svc.log_action(
                "invite_member",
                params=params,
                effect={"server_id": req.server_id, "handle": handle},
                server_id=req.server_id,
                handle=handle,
                no_op=False,
            )
            return {"ok": True, "status": "invited", "member": member}

        @self.router.post("/channel/react", operation_id="add_reaction",
                          summary="Add an emoji reaction to a channel message")
        def add_reaction(req: AddReactionRequest):
            params = req.model_dump()
            ch = svc._find_channel(req.server_id, req.channel_id)
            if not ch:
                svc.log_action("add_reaction", params=params, status=404)
                return svc.error_response(
                    code="CHANNEL_NOT_FOUND",
                    message="Channel not found.",
                    status=404,
                    details={
                        "server_id": req.server_id,
                        "channel_id": req.channel_id,
                    },
                )
            emoji = str(req.reaction or "").strip().strip(":")
            if not emoji:
                svc.log_action("add_reaction", params=params, status=422)
                return svc.error_response(
                    code="INVALID_REACTION",
                    message="Reaction emoji is required.",
                    status=422,
                    details={
                        "server_id": req.server_id,
                        "channel_id": req.channel_id,
                    },
                )
            message = svc._find_channel_message(ch, req.message_ts)
            if not message:
                svc.log_action("add_reaction", params=params, status=404)
                return svc.error_response(
                    code="MESSAGE_NOT_FOUND",
                    message="Message not found.",
                    status=404,
                    details={
                        "server_id": req.server_id,
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
                    "server_id": req.server_id,
                    "channel_id": req.channel_id,
                    "message_ts": str(req.message_ts),
                    "reaction_id": reaction["id"],
                    "emoji": emoji,
                },
                server_id=req.server_id,
                channel_id=req.channel_id,
                message_ts=str(req.message_ts),
                reaction=emoji,
            )
            return {"ok": True, "reaction": reaction}


def create_service(service_id: str) -> DiscordAppService:
    return DiscordAppService(service_id)
