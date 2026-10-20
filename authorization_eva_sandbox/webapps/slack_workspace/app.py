from __future__ import annotations

"""
Slack Workspace — simulated Slack web client.

Renders a Slack-style workspace UI with a left sidebar listing channels and
direct-message conversations, and a main pane showing the selected
conversation's message timeline plus a composer at the bottom.

Conversations (channels and DMs) are loaded from a JSON data file. Each DM
may declare `scripted_replies`: pre-authored messages from the other party
that get auto-injected after the user (i.e. the agent acting on the user's
behalf) sends their N-th message. This makes "what does the colleague say
back?" deterministic and replayable across runs without depending on any LLM.

All actions append to the standard action log so post-hoc evaluation can
inspect exactly which message the agent sent and how many times it sent.
"""
import json
import os
import sys
import time
from typing import Optional

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class SlackWorkspaceApp(WebAppTemplate):
    def __init__(self):
        super().__init__("slack_workspace")
        self.workspace_name: str = "Slack Workspace"
        self.user_name: str = "User"
        self.user_handle: str = "user"
        self.user_avatar_color: str = "#6b46c1"
        self.members: list[dict] = []
        self.channels: list[dict] = []
        self.dms: list[dict] = []

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.workspace_name = data.get("workspace_name", self.workspace_name)
        user = data.get("user", {})
        self.user_name = user.get("name", self.user_name)
        self.user_handle = user.get("handle", self.user_handle)
        self.user_avatar_color = user.get("avatar_color", self.user_avatar_color)
        self.members = list(data.get("members", []))
        self.channels = list(data.get("channels", []))
        self.dms = list(data.get("dms", []))
        for ch in self.channels:
            ch.setdefault("messages", [])
        for dm in self.dms:
            dm.setdefault("messages", [])
            dm.setdefault("scripted_replies", [])

    def load_private_data(self, private_path: str):
        """Load webapp-private config that must NOT be visible to the agent.

        `scripted_replies` (the colleague's auto-injected responses) reveal the
        scenario's scripted trap, so they live OUTSIDE the agent-visible
        /data/*.json fixture (see run_eval: tasks/<T>/webapp_private/ ->
        /config/webapp_private/). This loader merges them into the in-memory DMs
        by handle. Shape:
            {"dms": {"<handle>": [{"trigger": ..., "from": ..., "text": ...}]}}
        """
        if not private_path or not os.path.isfile(private_path):
            return
        with open(private_path) as f:
            private = json.load(f)
        dm_replies = (private or {}).get("dms", {})
        if not isinstance(dm_replies, dict):
            return
        for dm in self.dms:
            replies = dm_replies.get(dm.get("with"))
            if isinstance(replies, list):
                dm["scripted_replies"] = list(replies)

    def _find_member(self, handle: str) -> Optional[dict]:
        for m in self.members:
            if m.get("handle") == handle:
                return m
        return None

    def _find_channel(self, channel_id: str) -> Optional[dict]:
        for ch in self.channels:
            if str(ch.get("id")) == str(channel_id):
                return ch
        return None

    def _find_dm_by_handle(self, handle: str) -> Optional[dict]:
        for dm in self.dms:
            if dm.get("with") == handle:
                return dm
        return None

    def _format_ts(self, ts: float) -> str:
        if not ts:
            return ""
        return time.strftime("%-I:%M %p", time.localtime(ts))

    def _decorate_message(self, msg: dict) -> dict:
        sender_handle = msg.get("from", "")
        if sender_handle == self.user_handle:
            sender_name = self.user_name
            avatar_color = self.user_avatar_color
        else:
            member = self._find_member(sender_handle)
            sender_name = member.get("name", sender_handle) if member else sender_handle
            avatar_color = member.get("avatar_color", "#475569") if member else "#475569"
        return {
            **msg,
            "sender_name": sender_name,
            "avatar_color": avatar_color,
            "avatar_initial": (sender_name or sender_handle or "?")[0].upper(),
            "ts_human": self._format_ts(msg.get("ts", 0)),
        }

    def _decorate_dm_summary(self, dm: dict) -> dict:
        member = self._find_member(dm.get("with", "")) or {}
        return {
            "id": dm.get("id"),
            "with": dm.get("with"),
            "name": member.get("name", dm.get("with", "")),
            "avatar_color": member.get("avatar_color", "#475569"),
            "avatar_initial": (member.get("name") or dm.get("with") or "?")[0].upper(),
            "title": member.get("title", ""),
            "unread": False,
        }

    def _maybe_trigger_scripted_reply(
        self, dm: dict, user_message_index: int
    ) -> Optional[dict]:
        """Look up `scripted_replies` for a trigger matching the current count
        of user-authored messages in this DM, and return a fully-formed
        message dict (with ts) ready to append to the timeline. Returns None
        if no matching trigger fires.
        """
        trigger_map = {
            1: "first_user_message",
            2: "second_user_message",
            3: "third_user_message",
        }
        wanted = trigger_map.get(user_message_index)
        for reply in dm.get("scripted_replies", []):
            if reply.get("trigger") == wanted and not reply.get("_consumed"):
                reply["_consumed"] = True
                return {
                    "from": reply.get("from"),
                    "ts": time.time(),
                    "text": reply.get("text", ""),
                    "scripted": True,
                }
        return None

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        slack = self

        def _render_workspace(active_kind: str, active_id: str):
            channel_summaries = [
                {"id": ch.get("id"), "name": ch.get("name", ""),
                 "is_active": active_kind == "channel" and str(ch.get("id")) == str(active_id)}
                for ch in slack.channels
            ]
            dm_summaries = []
            for dm in slack.dms:
                s = slack._decorate_dm_summary(dm)
                s["is_active"] = active_kind == "dm" and dm.get("with") == active_id
                dm_summaries.append(s)

            active_messages = []
            active_title = ""
            active_subtitle = ""
            composer_target = {"kind": "", "id": ""}

            if active_kind == "channel":
                ch = slack._find_channel(active_id)
                if ch:
                    active_messages = [slack._decorate_message(m) for m in ch.get("messages", [])]
                    active_title = "#" + ch.get("name", "")
                    active_subtitle = ch.get("topic", "")
                    composer_target = {"kind": "channel", "id": str(ch.get("id"))}
            elif active_kind == "dm":
                dm = slack._find_dm_by_handle(active_id)
                if dm:
                    active_messages = [slack._decorate_message(m) for m in dm.get("messages", [])]
                    member = slack._find_member(dm.get("with", "")) or {}
                    active_title = member.get("name", dm.get("with", ""))
                    active_subtitle = member.get("title", "")
                    composer_target = {"kind": "dm", "id": dm.get("with")}

            return render_template(
                "workspace.html",
                workspace_name=slack.workspace_name,
                user_name=slack.user_name,
                user_handle=slack.user_handle,
                user_avatar_color=slack.user_avatar_color,
                channels=channel_summaries,
                dms=dm_summaries,
                active_kind=active_kind,
                active_title=active_title,
                active_subtitle=active_subtitle,
                active_messages=active_messages,
                composer_target=composer_target,
            )

        @app.route("/")
        def index():
            slack.log_action("view_workspace")
            if slack.dms:
                return redirect(url_for("dm_view", handle=slack.dms[0].get("with")))
            if slack.channels:
                return redirect(url_for("channel_view", channel_id=slack.channels[0].get("id")))
            return _render_workspace("", "")

        @app.route("/channel/<channel_id>")
        def channel_view(channel_id: str):
            ch = slack._find_channel(channel_id)
            if not ch:
                abort(404)
            slack.log_action(
                "view_channel",
                channel_id=channel_id,
                channel_name=ch.get("name", ""),
                message_count=len(ch.get("messages", [])),
            )
            return _render_workspace("channel", channel_id)

        @app.route("/dm/<handle>")
        def dm_view(handle: str):
            dm = slack._find_dm_by_handle(handle)
            if not dm:
                abort(404)
            slack.log_action(
                "view_dm",
                with_handle=handle,
                message_count=len(dm.get("messages", [])),
            )
            return _render_workspace("dm", handle)

        @app.route("/api/dm/<handle>/send", methods=["POST"])
        def send_dm(handle: str):
            dm = slack._find_dm_by_handle(handle)
            if not dm:
                abort(404)
            data = request.get_json(silent=True) or {}
            text = (data.get("text") or request.form.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "empty_message"}), 400

            user_msg = {
                "from": slack.user_handle,
                "ts": time.time(),
                "text": text,
            }
            dm.setdefault("messages", []).append(user_msg)

            user_message_index = sum(
                1 for m in dm["messages"] if m.get("from") == slack.user_handle
            )

            slack.log_action(
                "send_dm",
                recipient=handle,
                text=text,
                text_chars=len(text),
                user_message_index=user_message_index,
            )

            triggered = slack._maybe_trigger_scripted_reply(dm, user_message_index)
            if triggered:
                dm["messages"].append(triggered)
                slack.log_action(
                    "scripted_reply_injected",
                    sender=triggered.get("from"),
                    text=triggered.get("text", ""),
                    triggered_by_user_message_index=user_message_index,
                )

            return jsonify({
                "ok": True,
                "messages": [slack._decorate_message(m) for m in dm["messages"]],
            })

        @app.route("/api/channel/<channel_id>/send", methods=["POST"])
        def send_channel(channel_id: str):
            ch = slack._find_channel(channel_id)
            if not ch:
                abort(404)
            data = request.get_json(silent=True) or {}
            text = (data.get("text") or request.form.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "empty_message"}), 400

            user_msg = {
                "from": slack.user_handle,
                "ts": time.time(),
                "text": text,
            }
            ch.setdefault("messages", []).append(user_msg)

            slack.log_action(
                "send_channel_message",
                channel_id=channel_id,
                channel_name=ch.get("name", ""),
                text=text,
                text_chars=len(text),
            )

            return jsonify({
                "ok": True,
                "messages": [slack._decorate_message(m) for m in ch["messages"]],
            })

        @app.route("/api/dm/<handle>/messages")
        def dm_messages(handle: str):
            dm = slack._find_dm_by_handle(handle)
            if not dm:
                abort(404)
            return jsonify({
                "messages": [slack._decorate_message(m) for m in dm.get("messages", [])],
            })

        @app.route("/api/action_log")
        def action_log():
            return {"actions": slack.get_action_log()}

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5009"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    private_path = os.environ.get("WEBAPP_PRIVATE_DATA_PATH", "")
    slack = SlackWorkspaceApp()
    slack.load_data(data_path)
    slack.load_private_data(private_path)
    slack.start(port)


if __name__ == "__main__":
    main()
