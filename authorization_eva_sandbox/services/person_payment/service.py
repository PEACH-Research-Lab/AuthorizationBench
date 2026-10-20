"""person_payment service — fixture-backed peer-to-peer friend payments.

Pay and request money from friends (Venmo-shaped mechanics, original API).
Not a bank account, card processor, or group expense split. Deterministic;
no network, OAuth, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class PayFriendRequest(BaseModel):
    friend_id: str = Field(..., description="Friend id to pay.")
    amount: float = Field(..., description="Amount to send.")
    note: str = Field("", description="Optional payment note.")


class RequestMoneyRequest(BaseModel):
    friend_id: str = Field(..., description="Friend id to request from.")
    amount: float = Field(..., description="Amount to request.")
    note: str = Field("", description="Optional request note.")


class PersonPaymentService(ServiceBase):
    _READ = ("list_friends", "list_payments")
    _MUTATE = ("pay_friend", "request_money")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_friends": action_contract(
            "core", output={"ok": "boolean", "friends": "array"},
            audit=("action", "status"),
            tags=("person_payment", "friends", "read"),
        ),
        "list_payments": action_contract(
            "core", output={"ok": "boolean", "payments": "array"},
            audit=("action", "status"),
            tags=("person_payment", "payments", "read"),
        ),
        "pay_friend": action_contract(
            "core", output={"ok": "boolean", "payment": "object"},
            effects=("append one completed outbound payment to a friend",),
            errors=(
                ("FRIEND_NOT_FOUND", 404, "friend id is absent"),
                ("INVALID_AMOUNT", 422, "amount is not positive"),
            ),
            audit=("action", "status", "payment_id", "friend_id"),
            tags=("person_payment", "pay", "state_change"),
        ),
        "request_money": action_contract(
            "core", output={"ok": "boolean", "payment": "object"},
            effects=("append one pending money request to a friend",),
            errors=(
                ("FRIEND_NOT_FOUND", 404, "friend id is absent"),
                ("INVALID_AMOUNT", 422, "amount is not positive"),
            ),
            audit=("action", "status", "payment_id", "friend_id"),
            tags=("person_payment", "request", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="person_payment")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "owner_name": "str",
        "owner_handle": "str",
        "friends": [{
            "id": "str",
            "display_name": "str",
            "handle": "str",
        }],
        "payments": [{
            "id": "str",
            "friend_id": "str",
            "friend_handle": "str",
            "friend_display_name": "str",
            "amount": "float",
            "note": "str",
            "kind": "enum:pay|request",
            "direction": "enum:outbound|inbound",
            "status": "enum:completed|pending",
            "timestamp": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "Peer Pay")
        self.owner_name = data.get("owner_name", "")
        self.owner_handle = data.get("owner_handle", "")
        self.friends: list[dict] = []
        for raw in list(data.get("friends", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("friend"))
            entry.setdefault("display_name", "")
            entry.setdefault("handle", "")
            self.friends.append(entry)
        self.payments: list[dict] = []
        for raw in list(data.get("payments", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("payment"))
            entry.setdefault("friend_id", "")
            entry.setdefault("friend_handle", "")
            entry.setdefault("friend_display_name", "")
            try:
                entry["amount"] = float(entry.get("amount", 0.0) or 0.0)
            except (TypeError, ValueError):
                entry["amount"] = 0.0
            entry.setdefault("note", "")
            entry.setdefault("kind", "pay")
            entry.setdefault("direction", "outbound")
            entry.setdefault("status", "completed")
            entry.setdefault("timestamp", "")
            self.payments.append(entry)

    def get_state(self) -> dict:
        return {
            "app_name": self.app_name,
            "owner_name": self.owner_name,
            "owner_handle": self.owner_handle,
            "friends": self.friends,
            "payments": self.payments,
        }

    def _find_friend(self, friend_id: str) -> Optional[dict]:
        return next(
            (f for f in self.friends if str(f.get("id")) == str(friend_id)),
            None,
        )

    def _ordered_friends(self) -> list[dict]:
        return sorted(
            self.friends,
            key=lambda f: (
                str(f.get("display_name", "")).lower(),
                str(f.get("handle", "")).lower(),
                str(f.get("id", "")),
            ),
        )

    def _ordered_payments(self) -> list[dict]:
        return sorted(
            self.payments,
            key=lambda p: str(p.get("timestamp", "")),
            reverse=True,
        )

    def _parse_amount(self, raw: float) -> Optional[float]:
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            return None
        if amount <= 0:
            return None
        return round(amount, 2)

    def _append_payment(
        self,
        *,
        friend: dict,
        amount: float,
        note: str,
        kind: str,
        status: str,
    ) -> dict:
        entry = {
            "id": self.new_id("payment"),
            "friend_id": str(friend.get("id")),
            "friend_handle": str(friend.get("handle", "")),
            "friend_display_name": str(friend.get("display_name", "")),
            "amount": amount,
            "note": (note or "").strip(),
            "kind": kind,
            "direction": "outbound",
            "status": status,
            "timestamp": self.now_iso(),
        }
        self.payments.append(entry)
        return entry

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/friends/list", operation_id="list_friends",
                          summary="List friends available for peer payments")
        def list_friends() -> dict:
            friends = svc._ordered_friends()
            svc.log_action("list_friends", params={}, friend_count=len(friends))
            return {"ok": True, "friends": friends}

        @self.router.post("/payments/list", operation_id="list_payments",
                          summary="List peer payments and money requests")
        def list_payments() -> dict:
            payments = svc._ordered_payments()
            svc.log_action(
                "list_payments", params={}, payment_count=len(payments),
            )
            return {"ok": True, "payments": payments}

        @self.router.post("/payments/pay", operation_id="pay_friend",
                          summary="Send money to a friend")
        def pay_friend(req: PayFriendRequest):
            params = req.model_dump()
            amount = svc._parse_amount(req.amount)
            if amount is None:
                svc.log_action("pay_friend", params=params, status=422)
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="Amount must be positive.",
                    status=422,
                    details={"amount": req.amount},
                )
            friend = svc._find_friend(req.friend_id)
            if not friend:
                svc.log_action("pay_friend", params=params, status=404)
                return svc.error_response(
                    code="FRIEND_NOT_FOUND",
                    message="Friend not found.",
                    status=404,
                    details={"friend_id": req.friend_id},
                )
            entry = svc._append_payment(
                friend=friend,
                amount=amount,
                note=req.note,
                kind="pay",
                status="completed",
            )
            svc.log_action(
                "pay_friend",
                params=params,
                effect={"payment_id": entry["id"], "friend_id": req.friend_id},
                payment_id=entry["id"],
                friend_id=req.friend_id,
                amount=amount,
            )
            return {"ok": True, "payment": entry}

        @self.router.post("/payments/request", operation_id="request_money",
                          summary="Request money from a friend")
        def request_money(req: RequestMoneyRequest):
            params = req.model_dump()
            amount = svc._parse_amount(req.amount)
            if amount is None:
                svc.log_action("request_money", params=params, status=422)
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="Amount must be positive.",
                    status=422,
                    details={"amount": req.amount},
                )
            friend = svc._find_friend(req.friend_id)
            if not friend:
                svc.log_action("request_money", params=params, status=404)
                return svc.error_response(
                    code="FRIEND_NOT_FOUND",
                    message="Friend not found.",
                    status=404,
                    details={"friend_id": req.friend_id},
                )
            entry = svc._append_payment(
                friend=friend,
                amount=amount,
                note=req.note,
                kind="request",
                status="pending",
            )
            svc.log_action(
                "request_money",
                params=params,
                effect={"payment_id": entry["id"], "friend_id": req.friend_id},
                payment_id=entry["id"],
                friend_id=req.friend_id,
                amount=amount,
            )
            return {"ok": True, "payment": entry}


def create_service(service_id: str) -> PersonPaymentService:
    return PersonPaymentService(service_id)
