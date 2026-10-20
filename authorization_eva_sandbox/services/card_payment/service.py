"""card_payment service — fixture-backed card charges and refunds.

Stripe-shaped card capture (charges + refunds) without copying Stripe's API.
Deterministic; no network, OAuth, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class ChargeIdRequest(BaseModel):
    charge_id: str = Field(..., description="Charge id.")


class ListChargesRequest(BaseModel):
    limit: int = Field(
        0,
        ge=0,
        description="Max charges to return (0 = all), newest first.",
    )
    status: str = Field(
        "",
        description="Optional status filter (succeeded|pending|failed|refunded|"
        "partially_refunded).",
    )


class CreateChargeRequest(BaseModel):
    amount: float = Field(..., description="Charge amount in major currency units.")
    currency: str = Field("USD", description="ISO currency code.")
    description: str = Field("", description="Optional charge description.")
    customer_email: str = Field("", description="Optional customer email.")
    card_last4: str = Field("4242", description="Last four digits of the card.")
    card_brand: str = Field("visa", description="Card brand label.")


class RefundChargeRequest(BaseModel):
    charge_id: str = Field(..., description="Charge id to refund.")
    amount: float = Field(
        0,
        description="Refund amount (0 = remaining capturable amount).",
    )
    reason: str = Field("", description="Optional refund reason.")


class CardPaymentService(ServiceBase):
    _READ = ("list_charges", "get_charge")
    _MUTATE = ("create_charge", "refund_charge")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_charges": action_contract(
            "core", output={"ok": "boolean", "charges": "array"},
            audit=("action", "status"),
            tags=("card_payment", "charges", "read"),
        ),
        "get_charge": action_contract(
            "core", output={"ok": "boolean", "charge": "object"},
            errors=(("CHARGE_NOT_FOUND", 404, "charge id is absent"),),
            audit=("action", "status", "charge_id"),
            tags=("card_payment", "charges", "read"),
        ),
        "create_charge": action_contract(
            "core", output={"ok": "boolean", "charge": "object"},
            effects=("append one succeeded card charge",),
            errors=(("INVALID_AMOUNT", 422, "amount is not positive"),),
            audit=("action", "status", "charge_id"),
            tags=("card_payment", "charge", "state_change"),
        ),
        "refund_charge": action_contract(
            "core", output={"ok": "boolean", "charge": "object", "refund": "object"},
            effects=(
                "append one refund and update charge amount_refunded / status",
            ),
            errors=(
                ("CHARGE_NOT_FOUND", 404, "charge id is absent"),
                ("INVALID_AMOUNT", 422, "refund amount is not positive"),
                ("ALREADY_REFUNDED", 400, "charge is fully refunded"),
                (
                    "REFUND_EXCEEDS_REMAINING",
                    400,
                    "refund amount exceeds remaining capturable amount",
                ),
            ),
            audit=("action", "status", "charge_id", "refund_id"),
            tags=("card_payment", "refund", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="card_payment")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "merchant_name": "str",
        "default_currency": "str",
        "charges": [{
            "id": "str",
            "amount": "float",
            "currency": "str",
            "status": "enum:succeeded|pending|failed|refunded|partially_refunded",
            "description": "str",
            "customer_email": "str",
            "card_last4": "str",
            "card_brand": "str",
            "amount_refunded": "float",
            "created_at": "str",
            "refunds": [{
                "id": "str",
                "amount": "float",
                "reason": "str",
                "created_at": "str",
                "status": "enum:succeeded",
            }],
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "CardPay")
        self.merchant_name = data.get("merchant_name", "Acme Merchant")
        self.default_currency = str(data.get("default_currency") or "USD")
        self.charges: list[dict] = []
        for raw in list(data.get("charges", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("charge"))
            try:
                entry["amount"] = float(entry.get("amount", 0.0) or 0.0)
            except (TypeError, ValueError):
                entry["amount"] = 0.0
            entry.setdefault("currency", self.default_currency)
            entry.setdefault("status", "succeeded")
            entry.setdefault("description", "")
            entry.setdefault("customer_email", "")
            entry.setdefault("card_last4", "4242")
            entry.setdefault("card_brand", "visa")
            try:
                entry["amount_refunded"] = float(
                    entry.get("amount_refunded", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                entry["amount_refunded"] = 0.0
            entry.setdefault("created_at", "")
            refunds: list[dict] = []
            for refund_raw in list(entry.get("refunds", [])):
                refund = dict(refund_raw)
                refund.setdefault("id", self.new_id("refund"))
                try:
                    refund["amount"] = float(refund.get("amount", 0.0) or 0.0)
                except (TypeError, ValueError):
                    refund["amount"] = 0.0
                refund.setdefault("reason", "")
                refund.setdefault("created_at", "")
                refund.setdefault("status", "succeeded")
                refunds.append(refund)
            entry["refunds"] = refunds
            self.charges.append(entry)

    def get_state(self) -> dict:
        return {
            "app_name": self.app_name,
            "merchant_name": self.merchant_name,
            "default_currency": self.default_currency,
            "charges": self.charges,
        }

    def _find_charge(self, charge_id: str) -> Optional[dict]:
        return next(
            (c for c in self.charges if str(c.get("id")) == str(charge_id)),
            None,
        )

    def _ordered_charges(self) -> list[dict]:
        return sorted(
            self.charges,
            key=lambda c: str(c.get("created_at", "")),
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

    def _remaining_refundable(self, charge: dict) -> float:
        try:
            amount = float(charge.get("amount", 0.0) or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        try:
            refunded = float(charge.get("amount_refunded", 0.0) or 0.0)
        except (TypeError, ValueError):
            refunded = 0.0
        return round(max(amount - refunded, 0.0), 2)

    def _sync_charge_status(self, charge: dict) -> None:
        remaining = self._remaining_refundable(charge)
        amount = float(charge.get("amount", 0.0) or 0.0)
        if remaining <= 0 and amount > 0:
            charge["status"] = "refunded"
        elif float(charge.get("amount_refunded", 0.0) or 0.0) > 0:
            charge["status"] = "partially_refunded"

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/charges/list", operation_id="list_charges",
                          summary="List card charges")
        def list_charges(req: ListChargesRequest) -> dict:
            params = req.model_dump()
            charges = svc._ordered_charges()
            status_filter = (req.status or "").strip().lower()
            if status_filter:
                charges = [
                    c for c in charges
                    if str(c.get("status", "")).lower() == status_filter
                ]
            if req.limit and req.limit > 0:
                charges = charges[: int(req.limit)]
            svc.log_action(
                "list_charges",
                params=params,
                charge_count=len(charges),
            )
            return {"ok": True, "charges": charges}

        @self.router.post("/charges/get", operation_id="get_charge",
                          summary="Get one card charge by id")
        def get_charge(req: ChargeIdRequest):
            params = req.model_dump()
            charge = svc._find_charge(req.charge_id)
            if not charge:
                svc.log_action("get_charge", params=params, status=404)
                return svc.error_response(
                    code="CHARGE_NOT_FOUND",
                    message="Charge not found.",
                    status=404,
                    details={"charge_id": req.charge_id},
                )
            svc.log_action(
                "get_charge",
                params=params,
                charge_id=req.charge_id,
            )
            return {"ok": True, "charge": charge}

        @self.router.post("/charges/create", operation_id="create_charge",
                          summary="Create a card charge")
        def create_charge(req: CreateChargeRequest):
            params = req.model_dump()
            amount = svc._parse_amount(req.amount)
            if amount is None:
                svc.log_action("create_charge", params=params, status=422)
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="charge amount must be positive",
                    status=422,
                    details={"amount": req.amount},
                )
            currency = (req.currency or svc.default_currency).strip().upper() or (
                svc.default_currency
            )
            charge_id = svc.new_id("charge")
            ts = svc.now_iso()
            entry = {
                "id": charge_id,
                "amount": amount,
                "currency": currency,
                "status": "succeeded",
                "description": (req.description or "").strip(),
                "customer_email": (req.customer_email or "").strip(),
                "card_last4": (req.card_last4 or "4242").strip()[-4:],
                "card_brand": (req.card_brand or "visa").strip().lower() or "visa",
                "amount_refunded": 0.0,
                "created_at": ts,
                "refunds": [],
            }
            svc.charges.append(entry)
            svc.log_action(
                "create_charge",
                params=params,
                effect={"charge_id": charge_id, "amount": amount},
                charge_id=charge_id,
                amount=amount,
                currency=currency,
            )
            return {"ok": True, "charge": entry}

        @self.router.post("/charges/refund", operation_id="refund_charge",
                          summary="Refund a card charge (full or partial)")
        def refund_charge(req: RefundChargeRequest):
            params = req.model_dump()
            charge = svc._find_charge(req.charge_id)
            if not charge:
                svc.log_action(
                    "refund_charge",
                    params=params,
                    status=404,
                    charge_id=req.charge_id,
                )
                return svc.error_response(
                    code="CHARGE_NOT_FOUND",
                    message="Charge not found.",
                    status=404,
                    details={"charge_id": req.charge_id},
                )
            remaining = svc._remaining_refundable(charge)
            if remaining <= 0:
                svc.log_action(
                    "refund_charge",
                    params=params,
                    status=400,
                    charge_id=req.charge_id,
                )
                return svc.error_response(
                    code="ALREADY_REFUNDED",
                    message="charge is already fully refunded",
                    status=400,
                    details={"charge_id": req.charge_id},
                )
            if req.amount and float(req.amount) > 0:
                amount = svc._parse_amount(req.amount)
                if amount is None:
                    svc.log_action("refund_charge", params=params, status=422)
                    return svc.error_response(
                        code="INVALID_AMOUNT",
                        message="refund amount must be positive",
                        status=422,
                        details={"amount": req.amount, "charge_id": req.charge_id},
                    )
            else:
                amount = remaining
            if amount > remaining:
                svc.log_action(
                    "refund_charge",
                    params=params,
                    status=400,
                    charge_id=req.charge_id,
                )
                return svc.error_response(
                    code="REFUND_EXCEEDS_REMAINING",
                    message="refund amount exceeds remaining capturable amount",
                    status=400,
                    details={
                        "charge_id": req.charge_id,
                        "amount": amount,
                        "remaining": remaining,
                    },
                )
            refund_id = svc.new_id("refund")
            ts = svc.now_iso()
            refund = {
                "id": refund_id,
                "amount": amount,
                "reason": (req.reason or "").strip(),
                "created_at": ts,
                "status": "succeeded",
            }
            charge.setdefault("refunds", []).append(refund)
            charge["amount_refunded"] = round(
                float(charge.get("amount_refunded", 0.0) or 0.0) + amount,
                2,
            )
            svc._sync_charge_status(charge)
            svc.log_action(
                "refund_charge",
                params=params,
                effect={
                    "charge_id": req.charge_id,
                    "refund_id": refund_id,
                    "amount": amount,
                    "amount_refunded": charge["amount_refunded"],
                },
                charge_id=req.charge_id,
                refund_id=refund_id,
                amount=amount,
            )
            return {"ok": True, "charge": charge, "refund": refund}


def create_service(service_id: str) -> CardPaymentService:
    return CardPaymentService(service_id)
