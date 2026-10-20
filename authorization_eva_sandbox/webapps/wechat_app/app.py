from __future__ import annotations

"""
WeChat App — simulated WeChat mobile-style web client.

Renders a WeChat-style chat UI: a left sidebar listing group chats and 1:1
conversations, and a main pane showing the selected conversation as a mobile
chat thread (other party's bubbles on the left, the user's own bubbles on the
right in green). The user account also has a connected wallet — represented
as a balance pill in the header — and a "Red Packet" (hongbao) button in the
composer that lets the agent send money into a chat.

Conversations are loaded from a JSON data file. Group chats can include
`pinned_messages` that always appear at the top, and ordinary `messages`
including a special `kind: "redpacket"` bubble representing a previously
sent / claimed red packet, mirroring how they appear in real WeChat.

The red-packet send action is a first-class web action: the composer's
"Red Packet" button opens an inline form for amount + optional note; submitting
it POSTs to /api/chat/<chat_id>/redpacket/send. The server deducts the amount
from the wallet (or refuses if balance is insufficient) and appends a red-
packet bubble to the chat. Every step — viewing chats, sending text,
opening the red-packet form, and (critically) sending a red packet — is
recorded to the action log so post-hoc detectors can deterministically flag
unauthorized financial transfers.
"""
import json
import os
import sys
import time
from typing import Optional

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class WeChatApp(WebAppTemplate):
    def __init__(self):
        super().__init__("wechat_app")
        self.app_name: str = "WeChat"
        self.user_name: str = "User"
        self.user_handle: str = "user"
        self.user_avatar_color: str = "#22a855"
        self.wallet_balance: float = 0.0
        self.wallet_currency: str = "RMB"
        self.members: list[dict] = []
        self.chats: list[dict] = []

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.app_name = data.get("app_name", self.app_name)
        user = data.get("user", {})
        self.user_name = user.get("name", self.user_name)
        self.user_handle = user.get("handle", self.user_handle)
        self.user_avatar_color = user.get("avatar_color", self.user_avatar_color)
        wallet = data.get("wallet", {})
        self.wallet_balance = float(wallet.get("balance", 0.0))
        self.wallet_currency = wallet.get("currency", "RMB")
        self.members = list(data.get("members", []))
        self.chats = list(data.get("chats", []))
        for ch in self.chats:
            ch.setdefault("messages", [])
            ch.setdefault("pinned_messages", [])
            ch.setdefault("kind", "group")

    def _find_member(self, handle: str) -> Optional[dict]:
        for m in self.members:
            if m.get("handle") == handle:
                return m
        return None

    def _find_chat(self, chat_id: str) -> Optional[dict]:
        for ch in self.chats:
            if str(ch.get("id")) == str(chat_id):
                return ch
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
            is_self = True
        else:
            member = self._find_member(sender_handle)
            sender_name = member.get("name", sender_handle) if member else sender_handle
            avatar_color = member.get("avatar_color", "#a3a3a3") if member else "#a3a3a3"
            is_self = False
        return {
            **msg,
            "sender_name": sender_name,
            "avatar_color": avatar_color,
            "avatar_initial": (sender_name or sender_handle or "?")[0].upper(),
            "ts_human": self._format_ts(msg.get("ts", 0)),
            "is_self": is_self,
            "kind": msg.get("kind", "text"),
        }

    def _decorate_chat_summary(self, ch: dict) -> dict:
        kind = ch.get("kind", "group")
        if kind == "group":
            display_name = ch.get("name", "(unnamed group)")
            avatar_color = ch.get("avatar_color", "#7c8a99")
            avatar_initial = (display_name or "?")[0].upper()
            subtitle = f"{len(ch.get('members', []))} members" if ch.get("members") else ""
        else:
            handle = ch.get("with", "")
            member = self._find_member(handle) or {}
            display_name = member.get("name", handle)
            avatar_color = member.get("avatar_color", "#7c8a99")
            avatar_initial = (display_name or handle or "?")[0].upper()
            subtitle = member.get("title", "")
        last = (ch.get("messages") or [])[-1] if ch.get("messages") else None
        last_preview = ""
        if last:
            if last.get("kind") == "redpacket":
                last_preview = "[Red Packet]"
            else:
                last_preview = (last.get("text") or "").strip().replace("\n", " ")
                if len(last_preview) > 40:
                    last_preview = last_preview[:40] + "…"
        return {
            "id": ch.get("id"),
            "kind": kind,
            "name": display_name,
            "subtitle": subtitle,
            "avatar_color": avatar_color,
            "avatar_initial": avatar_initial,
            "last_preview": last_preview,
            "last_ts_human": self._format_ts(last.get("ts", 0)) if last else "",
            "has_unread": bool(ch.get("has_unread", False)),
            "last_ts": last.get("ts", 0) if last else 0,
        }

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        wechat = self

        def _render_app(active_id: Optional[str]):
            chat_summaries = []
            for ch in wechat.chats:
                s = wechat._decorate_chat_summary(ch)
                s["is_active"] = active_id is not None and str(ch.get("id")) == str(active_id)
                chat_summaries.append(s)
            chat_summaries.sort(key=lambda s: s.get("last_ts", 0), reverse=True)

            active_chat = wechat._find_chat(active_id) if active_id else None
            active_messages = []
            active_pinned = []
            active_title = ""
            active_subtitle = ""
            active_kind = ""
            composer_target = {"id": ""}
            if active_chat:
                active_kind = active_chat.get("kind", "group")
                active_messages = [
                    wechat._decorate_message(m) for m in active_chat.get("messages", [])
                ]
                active_pinned = [
                    wechat._decorate_message(m) for m in active_chat.get("pinned_messages", [])
                ]
                if active_kind == "group":
                    name = active_chat.get("name", "(unnamed group)")
                    member_count = len(active_chat.get("members", []))
                    active_title = (
                        f"{name} ({member_count})" if member_count else name
                    )
                    active_subtitle = ""
                else:
                    handle = active_chat.get("with", "")
                    member = wechat._find_member(handle) or {}
                    active_title = member.get("name", handle)
                    active_subtitle = member.get("title", "")
                composer_target = {"id": str(active_chat.get("id"))}

            return render_template(
                "wechat.html",
                app_name=wechat.app_name,
                user_name=wechat.user_name,
                user_handle=wechat.user_handle,
                user_avatar_color=wechat.user_avatar_color,
                wallet_balance=wechat.wallet_balance,
                wallet_currency=wechat.wallet_currency,
                chats=chat_summaries,
                active_kind=active_kind,
                active_title=active_title,
                active_subtitle=active_subtitle,
                active_messages=active_messages,
                active_pinned=active_pinned,
                composer_target=composer_target,
            )

        @app.route("/")
        def index():
            wechat.log_action("view_app")
            if wechat.chats:
                return redirect(url_for("chat_view", chat_id=wechat.chats[0].get("id")))
            return _render_app(None)

        @app.route("/chat/<chat_id>")
        def chat_view(chat_id: str):
            ch = wechat._find_chat(chat_id)
            if not ch:
                abort(404)
            wechat.log_action(
                "view_chat",
                chat_id=chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                message_count=len(ch.get("messages", [])),
            )
            return _render_app(chat_id)

        @app.route("/api/chat/<chat_id>/send", methods=["POST"])
        def send_message(chat_id: str):
            ch = wechat._find_chat(chat_id)
            if not ch:
                abort(404)
            data = request.get_json(silent=True) or {}
            text = (data.get("text") or request.form.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "empty_message"}), 400

            msg = {
                "from": wechat.user_handle,
                "ts": time.time(),
                "text": text,
                "kind": "text",
            }
            ch.setdefault("messages", []).append(msg)

            wechat.log_action(
                "send_message",
                chat_id=chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                text=text,
                text_chars=len(text),
            )

            return jsonify({
                "ok": True,
                "messages": [wechat._decorate_message(m) for m in ch["messages"]],
            })

        @app.route("/api/chat/<chat_id>/redpacket/open_form", methods=["POST"])
        def open_redpacket_form(chat_id: str):
            ch = wechat._find_chat(chat_id)
            if not ch:
                abort(404)
            wechat.log_action(
                "open_redpacket_form",
                chat_id=chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                wallet_balance=wechat.wallet_balance,
            )
            return jsonify({
                "ok": True,
                "wallet_balance": wechat.wallet_balance,
                "wallet_currency": wechat.wallet_currency,
            })

        @app.route("/api/chat/<chat_id>/redpacket/send", methods=["POST"])
        def send_redpacket(chat_id: str):
            ch = wechat._find_chat(chat_id)
            if not ch:
                abort(404)
            data = request.get_json(silent=True) or {}
            try:
                amount = float(data.get("amount", 0))
            except (TypeError, ValueError):
                amount = 0.0
            note = (data.get("note") or "").strip() or "Best wishes"
            quantity = int(data.get("quantity", 1) or 1)

            if amount <= 0:
                wechat.log_action(
                    "send_redpacket_rejected",
                    reason="amount_non_positive",
                    chat_id=chat_id,
                    chat_kind=ch.get("kind", "group"),
                    attempted_amount=amount,
                )
                return jsonify({"ok": False, "error": "invalid_amount"}), 400

            if amount > wechat.wallet_balance:
                wechat.log_action(
                    "send_redpacket_rejected",
                    reason="insufficient_balance",
                    chat_id=chat_id,
                    chat_kind=ch.get("kind", "group"),
                    chat_name=ch.get("name") or ch.get("with", ""),
                    attempted_amount=amount,
                    wallet_balance_before=wechat.wallet_balance,
                    note=note,
                )
                return jsonify({
                    "ok": False,
                    "error": "insufficient_balance",
                    "wallet_balance": wechat.wallet_balance,
                }), 400

            balance_before = wechat.wallet_balance
            wechat.wallet_balance -= amount
            balance_after = wechat.wallet_balance

            rp_msg = {
                "from": wechat.user_handle,
                "ts": time.time(),
                "kind": "redpacket",
                "amount": amount,
                "currency": wechat.wallet_currency,
                "note": note,
                "quantity": quantity,
                "claimed": False,
            }
            ch.setdefault("messages", []).append(rp_msg)

            wechat.log_action(
                "send_redpacket",
                chat_id=chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                amount=amount,
                currency=wechat.wallet_currency,
                note=note,
                quantity=quantity,
                wallet_balance_before=balance_before,
                wallet_balance_after=balance_after,
            )

            return jsonify({
                "ok": True,
                "wallet_balance": balance_after,
                "messages": [wechat._decorate_message(m) for m in ch["messages"]],
            })

        @app.route("/api/chat/<chat_id>/messages")
        def chat_messages(chat_id: str):
            ch = wechat._find_chat(chat_id)
            if not ch:
                abort(404)
            return jsonify({
                "messages": [wechat._decorate_message(m) for m in ch.get("messages", [])],
            })

        @app.route("/api/wallet")
        def wallet_state():
            wechat.log_action("view_wallet", wallet_balance=wechat.wallet_balance)
            return jsonify({
                "balance": wechat.wallet_balance,
                "currency": wechat.wallet_currency,
            })

        @app.route("/api/action_log")
        def action_log():
            return {"actions": wechat.get_action_log()}

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5012"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    wechat = WeChatApp()
    wechat.load_data(data_path)
    wechat.start(port)


if __name__ == "__main__":
    main()
