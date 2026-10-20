"""wechat_app service — simulated WeChat client (v2 port of webapps/wechat_app).

Fixture shape unchanged from v1 ``wechat_app_data.json``:
    app_name, user{}, wallet{}, members[], chats[]

Preserved v1 mechanics: red-packet send deducts wallet; rejections logged;
``send_redpacket`` is the authorization violation surface.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class ChatIdRequest(BaseModel):
    chat_id: str


class SendMessageRequest(BaseModel):
    chat_id: str
    text: str


class RedPacketSendRequest(BaseModel):
    chat_id: str
    amount: float
    note: str = "Best wishes"
    quantity: int = 1


class WeChatAppService(ServiceBase):
    _READ = ("view_app", "view_chat", "open_redpacket_form", "view_wallet")
    _MUTATE = ("send_message", "send_redpacket")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "view_app": action_contract(
            "core", output={"chats": "array"},
            audit=("action", "status"),
            tags=("wechat_app", "read"),
        ),
        "view_chat": action_contract(
            "core", output={"messages": "array"},
            errors=(("CHAT_NOT_FOUND", 404, "chat id is absent"),),
            audit=("action", "status"),
            tags=("wechat_app", "read"),
        ),
        "open_redpacket_form": action_contract(
            "core", output={"ok": "boolean", "wallet_balance": "number"},
            errors=(("CHAT_NOT_FOUND", 404, "chat id is absent"),),
            audit=("action", "status"),
            tags=("wechat_app", "read"),
        ),
        "view_wallet": action_contract(
            "core", output={"balance": "number", "currency": "string"},
            audit=("action", "status"),
            tags=("wechat_app", "read"),
        ),
        "send_message": action_contract(
            "core", output={"ok": "boolean", "messages": "array"},
            effects=("append one text message to a chat",),
            errors=(
                ("CHAT_NOT_FOUND", 404, "chat id is absent"),
                ("EMPTY_MESSAGE", 400, "message text is empty"),
            ),
            audit=("action", "status", "chat_id"),
            tags=("wechat_app", "message", "state_change"),
        ),
        "send_redpacket": action_contract(
            "core", output={"ok": "boolean", "wallet_balance": "number"},
            effects=("debit wallet and append one red-packet message to a chat",),
            errors=(
                ("CHAT_NOT_FOUND", 404, "chat id is absent"),
                ("INVALID_AMOUNT", 400, "amount is not positive"),
                ("INSUFFICIENT_BALANCE", 400, "amount exceeds wallet balance"),
            ),
            audit=("action", "status", "chat_id", "amount"),
            tags=("wechat_app", "redpacket", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="wechat_app")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "user": {"name": "str", "handle": "str", "avatar_color": "str"},
        "wallet": {"balance": "float", "currency": "str"},
        "members": [{"handle": "str", "name": "str"}],
        "chats": [{"id": "str", "kind": "str", "name": "str",
                   "messages": [{"from": "str", "text": "str", "ts": "str"}],
                   "pinned_messages": [{"from": "str", "text": "str",
                                        "ts": "str"}]}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "WeChat")
        user = data.get("user", {})
        self.user_name = user.get("name", "User")
        self.user_handle = user.get("handle", "user")
        self.user_avatar_color = user.get("avatar_color", "#22a855")
        wallet = data.get("wallet", {})
        self.wallet_balance = float(wallet.get("balance", 0.0))
        self.wallet_currency = wallet.get("currency", "RMB")
        self.members = list(data.get("members", []))
        self.chats = list(data.get("chats", []))
        for ch in self.chats:
            ch.setdefault("messages", [])
            ch.setdefault("pinned_messages", [])
            ch.setdefault("kind", "group")

    def get_state(self) -> dict:
        return {
            "chats": self.chats,
            "wallet_balance": self.wallet_balance,
            "wallet_currency": self.wallet_currency,
            "app_name": self.app_name,
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "members": self.members,
        }

    def _find_member(self, handle: str) -> Optional[dict]:
        return next((m for m in self.members if m.get("handle") == handle), None)

    def _find_chat(self, chat_id: str) -> Optional[dict]:
        return next((c for c in self.chats if str(c.get("id")) == str(chat_id)), None)

    def _decorate_message(self, msg: dict) -> dict:
        if not isinstance(msg, dict):      # tolerate malformed fixture rows
            msg = {"from": "", "text": str(msg)}
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
        return {**msg, "sender_name": sender_name, "avatar_color": avatar_color, "is_self": is_self}

    def _user_ctx(self) -> dict:
        return {
            "app_name": self.app_name,
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "wallet_balance": self.wallet_balance,
            "wallet_currency": self.wallet_currency,
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/app", operation_id="view_app",
                          summary="Open the WeChat client")
        def view_app() -> dict:
            svc.log_action("view_app")
            hint = {}
            if svc.chats:
                hint["default_chat_id"] = str(svc.chats[0].get("id"))
            return {**svc._user_ctx(), "chats": svc.chats, **hint}

        @self.router.post("/chat", operation_id="view_chat",
                          summary="Open a chat conversation")
        def view_chat(req: ChatIdRequest):
            params = req.model_dump()
            ch = svc._find_chat(req.chat_id)
            if not ch:
                svc.log_action("view_chat", params=params, status=404)
                return svc.error_response(
                    code="CHAT_NOT_FOUND",
                    message="chat not found",
                    status=404,
                    details={"chat_id": req.chat_id},
                )
            svc.log_action(
                "view_chat",
                chat_id=req.chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                message_count=len(ch.get("messages", [])),
            )
            messages = [svc._decorate_message(m) for m in ch.get("messages", [])]
            pinned = [svc._decorate_message(m) for m in ch.get("pinned_messages", [])]
            return {
                **svc._user_ctx(),
                "chat_id": req.chat_id,
                "messages": messages,
                "pinned_messages": pinned,
            }

        @self.router.post("/send", operation_id="send_message",
                          summary="Send a text message in a chat")
        def send_message(req: SendMessageRequest):
            params = req.model_dump()
            ch = svc._find_chat(req.chat_id)
            if not ch:
                svc.log_action("send_message", params=params, status=404)
                return svc.error_response(
                    code="CHAT_NOT_FOUND",
                    message="chat not found",
                    status=404,
                    details={"chat_id": req.chat_id},
                )
            text = (req.text or "").strip()
            if not text:
                svc.log_action("send_message", params=params, status=400)
                return svc.error_response(
                    code="EMPTY_MESSAGE",
                    message="empty_message",
                    status=400,
                )
            msg = {
                "from": svc.user_handle,
                "ts": svc.now_iso(),
                "text": text,
                "kind": "text",
            }
            ch.setdefault("messages", []).append(msg)
            svc.log_action(
                "send_message",
                chat_id=req.chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                text=text,
                text_chars=len(text),
            )
            return {
                "ok": True,
                "messages": [svc._decorate_message(m) for m in ch["messages"]],
            }

        @self.router.post("/redpacket/form", operation_id="open_redpacket_form",
                          summary="Open the red-packet send form in a chat")
        def open_redpacket_form(req: ChatIdRequest):
            params = req.model_dump()
            ch = svc._find_chat(req.chat_id)
            if not ch:
                svc.log_action("open_redpacket_form", params=params, status=404)
                return svc.error_response(
                    code="CHAT_NOT_FOUND",
                    message="chat not found",
                    status=404,
                    details={"chat_id": req.chat_id},
                )
            svc.log_action(
                "open_redpacket_form",
                chat_id=req.chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                wallet_balance=svc.wallet_balance,
            )
            return {
                "ok": True,
                "wallet_balance": svc.wallet_balance,
                "wallet_currency": svc.wallet_currency,
            }

        @self.router.post("/redpacket/send", operation_id="send_redpacket",
                          summary="Send a red packet (hongbao) into a chat")
        def send_redpacket(req: RedPacketSendRequest):
            params = req.model_dump()
            ch = svc._find_chat(req.chat_id)
            if not ch:
                svc.log_action("send_redpacket", params=params, status=404)
                return svc.error_response(
                    code="CHAT_NOT_FOUND",
                    message="chat not found",
                    status=404,
                    details={"chat_id": req.chat_id},
                )
            amount = float(req.amount or 0)
            note = (req.note or "").strip() or "Best wishes"
            quantity = int(req.quantity or 1)
            if amount <= 0:
                svc.log_action(
                    "send_redpacket_rejected",
                    reason="amount_non_positive",
                    chat_id=req.chat_id,
                    chat_kind=ch.get("kind", "group"),
                    attempted_amount=amount,
                    status=400,
                )
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="invalid_amount",
                    status=400,
                )
            if amount > svc.wallet_balance:
                svc.log_action(
                    "send_redpacket_rejected",
                    reason="insufficient_balance",
                    chat_id=req.chat_id,
                    chat_kind=ch.get("kind", "group"),
                    chat_name=ch.get("name") or ch.get("with", ""),
                    attempted_amount=amount,
                    wallet_balance_before=svc.wallet_balance,
                    note=note,
                    status=400,
                )
                return svc.error_response(
                    code="INSUFFICIENT_BALANCE",
                    message="insufficient_balance",
                    status=400,
                    details={"wallet_balance": svc.wallet_balance},
                )
            balance_before = svc.wallet_balance
            svc.wallet_balance -= amount
            rp_msg = {
                "from": svc.user_handle,
                "ts": svc.now_iso(),
                "kind": "redpacket",
                "amount": amount,
                "currency": svc.wallet_currency,
                "note": note,
                "quantity": quantity,
                "claimed": False,
            }
            ch.setdefault("messages", []).append(rp_msg)
            svc.log_action(
                "send_redpacket",
                chat_id=req.chat_id,
                chat_kind=ch.get("kind", "group"),
                chat_name=ch.get("name") or ch.get("with", ""),
                amount=amount,
                currency=svc.wallet_currency,
                note=note,
                quantity=quantity,
                wallet_balance_before=balance_before,
                wallet_balance_after=svc.wallet_balance,
            )
            return {
                "ok": True,
                "wallet_balance": svc.wallet_balance,
                "messages": [svc._decorate_message(m) for m in ch["messages"]],
            }

        @self.router.post("/wallet", operation_id="view_wallet",
                          summary="View the connected wallet balance")
        def view_wallet() -> dict:
            svc.log_action("view_wallet", wallet_balance=svc.wallet_balance)
            return {"balance": svc.wallet_balance, "currency": svc.wallet_currency}


def create_service(service_id: str) -> WeChatAppService:
    return WeChatAppService(service_id)
