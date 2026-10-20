from __future__ import annotations

"""
Discord App — simulated Discord-style web client.

Renders a Discord-like three-pane UI:
  * left rail: list of servers (guilds) the user has joined
  * channel sidebar: text channels in the active server, with the channel topic
    pinned at the top once a channel is opened
  * main pane: the message stream of the active channel, with the composer at
    the bottom (the agent can post replies in-channel)

Each message carries the poster's display name, handle (with discriminator),
avatar colour, account-age (in days), and an optional server role badge
(e.g. the gold "TEAM" badge that real-team accounts hold). These per-poster
provenance signals are first-class fields so the agent can inspect them when
deciding whether an instruction-shaped post is actually authoritative — the
canonical use case being the Helios "operator impersonation" trap in V0034-n-g-h.

All semantically interesting actions are written to the action log:
  * view_app
  * view_server (which guild the agent opened)
  * view_channel (which channel; includes channel topic and message count)
  * view_message (when the agent explicitly fetches a single message; useful
    for trajectories where the agent is reasoning about one specific post)
  * send_message (agent posted into a channel)

The action log is the deterministic substrate post-hoc detectors read.
"""
import json
import os
import sys
import time
from typing import Optional

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class DiscordApp(WebAppTemplate):
    def __init__(self):
        super().__init__("discord_app")
        self.app_name: str = "Discord"
        self.user_name: str = "User"
        self.user_handle: str = "user"
        self.user_discriminator: str = "0001"
        self.user_avatar_color: str = "#5865f2"
        self.servers: list[dict] = []
        # member_index: handle -> member dict (across all servers)
        self.member_index: dict[str, dict] = {}

    # -- data loading ----------------------------------------------------

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.app_name = data.get("app_name", self.app_name)
        user = data.get("user", {})
        self.user_name = user.get("name", self.user_name)
        self.user_handle = user.get("handle", self.user_handle)
        self.user_discriminator = str(user.get("discriminator", self.user_discriminator))
        self.user_avatar_color = user.get("avatar_color", self.user_avatar_color)

        self.servers = list(data.get("servers", []))
        for srv in self.servers:
            srv.setdefault("channels", [])
            srv.setdefault("members", [])
            srv.setdefault("roles", [])
            for ch in srv["channels"]:
                ch.setdefault("topic", "")
                ch.setdefault("messages", [])
            for m in srv["members"]:
                self.member_index[m.get("handle", "")] = m

    # -- lookup helpers --------------------------------------------------

    def _find_server(self, server_id: str) -> Optional[dict]:
        for srv in self.servers:
            if str(srv.get("id")) == str(server_id):
                return srv
        return None

    def _find_channel(self, server_id: str, channel_id: str) -> Optional[dict]:
        srv = self._find_server(server_id)
        if not srv:
            return None
        for ch in srv.get("channels", []):
            if str(ch.get("id")) == str(channel_id):
                return ch
        return None

    def _find_member_in(self, server: dict, handle: str) -> Optional[dict]:
        for m in server.get("members", []):
            if m.get("handle") == handle:
                return m
        return self.member_index.get(handle)

    def _format_ts(self, ts: float) -> str:
        if not ts:
            return ""
        return time.strftime("%b %-d, %-I:%M %p", time.localtime(ts))

    # -- decoration ------------------------------------------------------

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
            "ts_human": self._format_ts(msg.get("ts", 0)),
            "is_self": is_self,
            "kind": msg.get("kind", "text"),
        }

    def _decorate_server_summary(self, srv: dict, active_id: Optional[str]) -> dict:
        return {
            "id": srv.get("id"),
            "name": srv.get("name", "(unnamed server)"),
            "icon_color": srv.get("icon_color", "#5865f2"),
            "icon_initial": (srv.get("name") or "?")[0].upper(),
            "is_active": active_id is not None and str(srv.get("id")) == str(active_id),
        }

    def _decorate_channel_summary(self, ch: dict, active_id: Optional[str]) -> dict:
        return {
            "id": ch.get("id"),
            "name": ch.get("name", "channel"),
            "topic": ch.get("topic", ""),
            "is_active": active_id is not None and str(ch.get("id")) == str(active_id),
            "message_count": len(ch.get("messages", [])),
        }

    # -- routes ----------------------------------------------------------

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        discord = self

        def _render_app(active_server_id: Optional[str], active_channel_id: Optional[str]):
            server_summaries = [
                discord._decorate_server_summary(s, active_server_id)
                for s in discord.servers
            ]

            active_server = discord._find_server(active_server_id) if active_server_id else None
            channel_summaries = []
            active_server_name = ""
            if active_server:
                active_server_name = active_server.get("name", "")
                channel_summaries = [
                    discord._decorate_channel_summary(c, active_channel_id)
                    for c in active_server.get("channels", [])
                ]

            active_channel = (
                discord._find_channel(active_server_id, active_channel_id)
                if active_server_id and active_channel_id else None
            )
            active_channel_name = ""
            active_channel_topic = ""
            active_messages = []
            composer_target = {"server_id": "", "channel_id": ""}
            if active_channel and active_server:
                active_channel_name = active_channel.get("name", "")
                active_channel_topic = active_channel.get("topic", "")
                active_messages = [
                    discord._decorate_message(active_server, m)
                    for m in active_channel.get("messages", [])
                ]
                composer_target = {
                    "server_id": str(active_server.get("id")),
                    "channel_id": str(active_channel.get("id")),
                }

            return render_template(
                "discord.html",
                app_name=discord.app_name,
                user_name=discord.user_name,
                user_handle=discord.user_handle,
                user_discriminator=discord.user_discriminator,
                user_avatar_color=discord.user_avatar_color,
                active_server_id=str(active_server.get("id")) if active_server else "",
                servers=server_summaries,
                channels=channel_summaries,
                active_server_name=active_server_name,
                active_channel_name=active_channel_name,
                active_channel_topic=active_channel_topic,
                active_messages=active_messages,
                composer_target=composer_target,
            )

        @app.route("/")
        def index():
            discord.log_action("view_app")
            if discord.servers:
                first_srv = discord.servers[0]
                first_ch = (first_srv.get("channels") or [{}])[0]
                if first_ch.get("id"):
                    return redirect(url_for(
                        "channel_view",
                        server_id=first_srv.get("id"),
                        channel_id=first_ch.get("id"),
                    ))
                return redirect(url_for("server_view", server_id=first_srv.get("id")))
            return _render_app(None, None)

        @app.route("/server/<server_id>")
        def server_view(server_id: str):
            srv = discord._find_server(server_id)
            if not srv:
                abort(404)
            discord.log_action(
                "view_server",
                server_id=server_id,
                server_name=srv.get("name", ""),
                channel_count=len(srv.get("channels", [])),
            )
            channels = srv.get("channels") or []
            if channels:
                return redirect(url_for(
                    "channel_view",
                    server_id=server_id,
                    channel_id=channels[0].get("id"),
                ))
            return _render_app(server_id, None)

        @app.route("/server/<server_id>/channel/<channel_id>")
        def channel_view(server_id: str, channel_id: str):
            srv = discord._find_server(server_id)
            ch = discord._find_channel(server_id, channel_id)
            if not srv or not ch:
                abort(404)
            discord.log_action(
                "view_channel",
                server_id=server_id,
                server_name=srv.get("name", ""),
                channel_id=channel_id,
                channel_name=ch.get("name", ""),
                channel_topic=ch.get("topic", ""),
                message_count=len(ch.get("messages", [])),
            )
            return _render_app(server_id, channel_id)

        @app.route("/api/server/<server_id>/channel/<channel_id>/message/<int:msg_index>")
        def view_message(server_id: str, channel_id: str, msg_index: int):
            srv = discord._find_server(server_id)
            ch = discord._find_channel(server_id, channel_id)
            if not srv or not ch:
                abort(404)
            msgs = ch.get("messages", [])
            if msg_index < 0 or msg_index >= len(msgs):
                abort(404)
            msg = msgs[msg_index]
            decorated = discord._decorate_message(srv, msg)
            sender = decorated.get("sender", {})
            discord.log_action(
                "view_message",
                server_id=server_id,
                server_name=srv.get("name", ""),
                channel_id=channel_id,
                channel_name=ch.get("name", ""),
                msg_index=msg_index,
                sender_handle=sender.get("handle"),
                sender_display_name=sender.get("display_name"),
                sender_discriminator=sender.get("discriminator"),
                sender_account_age_days=sender.get("account_age_days"),
                sender_badge=sender.get("badge"),
            )
            return jsonify({"ok": True, "message": decorated})

        @app.route("/api/server/<server_id>/channel/<channel_id>/send", methods=["POST"])
        def send_message(server_id: str, channel_id: str):
            srv = discord._find_server(server_id)
            ch = discord._find_channel(server_id, channel_id)
            if not srv or not ch:
                abort(404)
            data = request.get_json(silent=True) or {}
            text = (data.get("text") or request.form.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "empty_message"}), 400

            msg = {
                "from": discord.user_handle,
                "ts": time.time(),
                "text": text,
                "kind": "text",
            }
            ch.setdefault("messages", []).append(msg)

            discord.log_action(
                "send_message",
                server_id=server_id,
                server_name=srv.get("name", ""),
                channel_id=channel_id,
                channel_name=ch.get("name", ""),
                text=text,
                text_chars=len(text),
            )

            return jsonify({
                "ok": True,
                "messages": [discord._decorate_message(srv, m) for m in ch["messages"]],
            })

        @app.route("/api/action_log")
        def action_log():
            return {"actions": discord.get_action_log()}

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5014"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    discord = DiscordApp()
    discord.load_data(data_path)
    discord.start(port)


if __name__ == "__main__":
    main()
