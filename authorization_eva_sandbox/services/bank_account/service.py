"""bank_account service — AgentDojo-style personal banking.

Balances, statements, peer transfers, and bill pay against fixture
payees. Deterministic; no network, OAuth, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class ListTransactionsRequest(BaseModel):
    limit: int = Field(
        0,
        ge=0,
        description="Max transactions to return (0 = all), newest first.",
    )


class TransferRequest(BaseModel):
    to_account: str = Field(
        ...,
        description="Destination account number / IBAN for the transfer.",
    )
    amount: float = Field(..., description="Amount to transfer (debit).")
    memo: str = Field("", description="Optional transfer memo.")


class PayBillRequest(BaseModel):
    payee_id: str = Field(..., description="Fixture payee id to pay.")
    amount: float = Field(..., description="Amount to pay (debit).")
    memo: str = Field("", description="Optional bill-pay memo.")


class BankAccountService(ServiceBase):
    _READ = ("get_balance", "list_transactions")
    _MUTATE = ("transfer", "pay_bill")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "get_balance": action_contract(
            "core", output={"ok": "boolean", "balance": "number"},
            audit=("action", "status"),
            tags=("bank_account", "balance", "read"),
        ),
        "list_transactions": action_contract(
            "core", output={"ok": "boolean", "transactions": "array"},
            audit=("action", "status"),
            tags=("bank_account", "transactions", "read"),
        ),
        "transfer": action_contract(
            "core", output={"ok": "boolean", "transaction": "object"},
            effects=(
                "debit balance and append one outbound transfer transaction",
            ),
            errors=(
                ("INVALID_AMOUNT", 422, "amount is not positive"),
                ("INSUFFICIENT_BALANCE", 400, "amount exceeds available balance"),
            ),
            audit=("action", "status", "transaction_id", "to_account"),
            tags=("bank_account", "transfer", "state_change"),
        ),
        "pay_bill": action_contract(
            "core", output={"ok": "boolean", "transaction": "object"},
            effects=(
                "debit balance and append one bill-pay transaction to a payee",
            ),
            errors=(
                ("PAYEE_NOT_FOUND", 404, "payee id is absent"),
                ("INVALID_AMOUNT", 422, "amount is not positive"),
                ("INSUFFICIENT_BALANCE", 400, "amount exceeds available balance"),
            ),
            audit=("action", "status", "transaction_id", "payee_id"),
            tags=("bank_account", "bill_pay", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="bank_account")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "account_name": "str",
        "account_number": "str",
        "currency": "str",
        "balance": "float",
        "payees": [{
            "id": "str",
            "name": "str",
            "account_number": "str",
            "category": "str",
        }],
        "transactions": [{
            "id": "str",
            "kind": "enum:credit|debit|transfer|bill_pay",
            "amount": "float",
            "counterparty": "str",
            "counterparty_account": "str",
            "description": "str",
            "timestamp": "str",
            "status": "enum:posted|pending",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "Personal Banking")
        self.account_name = data.get("account_name", "Checking")
        self.account_number = str(data.get("account_number") or "")
        self.currency = str(data.get("currency") or "USD")
        try:
            self.balance = float(data.get("balance", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.balance = 0.0
        self.payees: list[dict] = []
        for raw in list(data.get("payees", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("payee"))
            entry.setdefault("name", "")
            entry.setdefault("account_number", "")
            entry.setdefault("category", "")
            self.payees.append(entry)
        self.transactions: list[dict] = []
        for raw in list(data.get("transactions", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("txn"))
            entry.setdefault("kind", "debit")
            try:
                entry["amount"] = float(entry.get("amount", 0.0) or 0.0)
            except (TypeError, ValueError):
                entry["amount"] = 0.0
            entry.setdefault("counterparty", "")
            entry.setdefault("counterparty_account", "")
            entry.setdefault("description", "")
            entry.setdefault("timestamp", "")
            entry.setdefault("status", "posted")
            self.transactions.append(entry)

    def get_state(self) -> dict:
        return {
            "app_name": self.app_name,
            "account_name": self.account_name,
            "account_number": self.account_number,
            "currency": self.currency,
            "balance": self.balance,
            "payees": self.payees,
            "transactions": self.transactions,
        }

    def _find_payee(self, payee_id: str) -> Optional[dict]:
        return next(
            (p for p in self.payees if str(p.get("id")) == str(payee_id)),
            None,
        )

    def _ordered_transactions(self) -> list[dict]:
        return sorted(
            self.transactions,
            key=lambda t: str(t.get("timestamp", "")),
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

    def _append_debit(
        self,
        *,
        kind: str,
        amount: float,
        counterparty: str,
        counterparty_account: str,
        description: str,
    ) -> dict:
        entry = {
            "id": self.new_id("txn"),
            "kind": kind,
            "amount": amount,
            "counterparty": counterparty,
            "counterparty_account": counterparty_account,
            "description": description,
            "timestamp": self.now_iso(),
            "status": "posted",
        }
        self.balance = round(self.balance - amount, 2)
        self.transactions.append(entry)
        return entry

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/balance", operation_id="get_balance",
                          summary="Get checking account balance")
        def get_balance() -> dict:
            svc.log_action(
                "get_balance",
                params={},
                balance=svc.balance,
                currency=svc.currency,
            )
            return {
                "ok": True,
                "app_name": svc.app_name,
                "account_name": svc.account_name,
                "account_number": svc.account_number,
                "currency": svc.currency,
                "balance": svc.balance,
            }

        @self.router.post("/transactions/list", operation_id="list_transactions",
                          summary="List account transactions (statement)")
        def list_transactions(req: ListTransactionsRequest) -> dict:
            params = req.model_dump()
            txns = svc._ordered_transactions()
            if req.limit and req.limit > 0:
                txns = txns[: int(req.limit)]
            svc.log_action(
                "list_transactions",
                params=params,
                transaction_count=len(txns),
            )
            return {"ok": True, "transactions": txns}

        @self.router.post("/transfer", operation_id="transfer",
                          summary="Transfer funds to another account")
        def transfer(req: TransferRequest):
            params = req.model_dump()
            to_account = (req.to_account or "").strip()
            amount = svc._parse_amount(req.amount)
            if amount is None:
                svc.log_action("transfer", params=params, status=422)
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="transfer amount must be positive",
                    status=422,
                    details={"amount": req.amount},
                )
            if amount > svc.balance:
                svc.log_action(
                    "transfer",
                    params=params,
                    status=400,
                    balance=svc.balance,
                )
                return svc.error_response(
                    code="INSUFFICIENT_BALANCE",
                    message="insufficient balance for transfer",
                    status=400,
                    details={
                        "amount": amount,
                        "balance": svc.balance,
                        "to_account": to_account,
                    },
                )
            memo = (req.memo or "").strip()
            entry = svc._append_debit(
                kind="transfer",
                amount=amount,
                counterparty=to_account,
                counterparty_account=to_account,
                description=memo or f"Transfer to {to_account}",
            )
            svc.log_action(
                "transfer",
                params=params,
                effect={
                    "transaction_id": entry["id"],
                    "to_account": to_account,
                    "amount": amount,
                    "balance": svc.balance,
                },
                transaction_id=entry["id"],
                to_account=to_account,
                amount=amount,
                balance=svc.balance,
            )
            return {"ok": True, "transaction": entry, "balance": svc.balance}

        @self.router.post("/bills/pay", operation_id="pay_bill",
                          summary="Pay a bill to a known payee")
        def pay_bill(req: PayBillRequest):
            params = req.model_dump()
            payee = svc._find_payee(req.payee_id)
            if not payee:
                svc.log_action(
                    "pay_bill",
                    params=params,
                    status=404,
                    payee_id=req.payee_id,
                )
                return svc.error_response(
                    code="PAYEE_NOT_FOUND",
                    message="Payee not found.",
                    status=404,
                    details={"payee_id": req.payee_id},
                )
            amount = svc._parse_amount(req.amount)
            if amount is None:
                svc.log_action("pay_bill", params=params, status=422)
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="bill payment amount must be positive",
                    status=422,
                    details={"amount": req.amount, "payee_id": req.payee_id},
                )
            if amount > svc.balance:
                svc.log_action(
                    "pay_bill",
                    params=params,
                    status=400,
                    payee_id=req.payee_id,
                    balance=svc.balance,
                )
                return svc.error_response(
                    code="INSUFFICIENT_BALANCE",
                    message="insufficient balance for bill payment",
                    status=400,
                    details={
                        "amount": amount,
                        "balance": svc.balance,
                        "payee_id": req.payee_id,
                    },
                )
            memo = (req.memo or "").strip()
            payee_name = str(payee.get("name") or "")
            payee_account = str(payee.get("account_number") or "")
            entry = svc._append_debit(
                kind="bill_pay",
                amount=amount,
                counterparty=payee_name or str(payee.get("id")),
                counterparty_account=payee_account,
                description=memo or f"Bill pay to {payee_name or req.payee_id}",
            )
            svc.log_action(
                "pay_bill",
                params=params,
                effect={
                    "transaction_id": entry["id"],
                    "payee_id": req.payee_id,
                    "amount": amount,
                    "balance": svc.balance,
                },
                transaction_id=entry["id"],
                payee_id=req.payee_id,
                amount=amount,
                balance=svc.balance,
            )
            return {"ok": True, "transaction": entry, "balance": svc.balance}


def create_service(service_id: str) -> BankAccountService:
    return BankAccountService(service_id)
