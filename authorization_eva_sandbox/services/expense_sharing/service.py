"""expense_sharing service — Splitwise-style group expense splitting.

Reduced from AppWorld Splitwise:

    list_groups, get_group, add_expense, settle_expense

Deterministic fixture state; no network, OAuth, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class GroupIdRequest(BaseModel):
    group_id: str = Field(..., description="Expense group id.")


class AddExpenseRequest(BaseModel):
    group_id: str
    description: str = Field(..., description="Short expense description.")
    amount: float = Field(..., description="Total cost of the expense.")
    paid_by: str = Field(
        "",
        description="Member user id who paid; defaults to current_user_id.",
    )
    currency: str = Field("USD", description="Currency code.")
    split_equally: bool = Field(
        True,
        description="When true, split amount equally across group members.",
    )
    shares: list[dict] = Field(
        default_factory=list,
        description=(
            "Optional custom shares: "
            "[{user_id, owed_share, paid_share}, ...]. "
            "Used when split_equally is false."
        ),
    )


class SettleExpenseRequest(BaseModel):
    expense_id: str = Field(..., description="Expense id to mark settled.")


class ExpenseSharingService(ServiceBase):
    _READ = ("list_groups", "get_group")
    _MUTATE = ("add_expense", "settle_expense")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_groups": action_contract(
            "core", output={"ok": "boolean", "groups": "array"},
            audit=("action", "status"),
            tags=("expense_sharing", "groups", "read"),
        ),
        "get_group": action_contract(
            "core", output={"ok": "boolean", "group": "object"},
            errors=(("GROUP_NOT_FOUND", 404, "group id is absent"),),
            audit=("action", "status", "group_id"),
            tags=("expense_sharing", "groups", "read"),
        ),
        "add_expense": action_contract(
            "core", output={"ok": "boolean", "expense": "object"},
            effects=("append one open expense to a group",),
            errors=(("GROUP_NOT_FOUND", 404, "group id is absent"),),
            audit=("action", "status", "expense_id", "group_id"),
            tags=("expense_sharing", "expense", "state_change"),
        ),
        "settle_expense": action_contract(
            "core", output={"ok": "boolean", "expense": "object"},
            preconditions=("expense exists and is open",),
            effects=("set one expense status to settled",),
            errors=(
                ("EXPENSE_NOT_FOUND", 404, "expense id is absent"),
                ("ALREADY_SETTLED", 409, "expense already settled"),
            ),
            audit=("action", "status", "expense_id"),
            tags=("expense_sharing", "expense", "settle", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="expense_sharing")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "current_user_id": "str",
        "users": [{
            "id": "str",
            "name": "str",
            "email": "str",
        }],
        "groups": [{
            "id": "str",
            "name": "str",
            "member_ids": "list",
            "created_at": "str",
        }],
        "expenses": [{
            "id": "str",
            "group_id": "str",
            "description": "str",
            "amount": "float",
            "currency": "str",
            "paid_by": "str",
            "split_equally": "bool",
            "shares": "list",
            "status": "enum:open|settled",
            "created_at": "str",
            "settled_at": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "Expense Sharing")
        self.current_user_id = str(data.get("current_user_id") or "")
        self.users: list[dict] = []
        for raw in list(data.get("users", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("user"))
            entry.setdefault("name", "")
            entry.setdefault("email", "")
            self.users.append(entry)
        if not self.current_user_id and self.users:
            self.current_user_id = str(self.users[0].get("id", ""))
        self.groups: list[dict] = []
        for raw in list(data.get("groups", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("group"))
            entry.setdefault("name", "")
            entry.setdefault("member_ids", [])
            entry.setdefault("created_at", "")
            entry["member_ids"] = [str(m) for m in list(entry.get("member_ids") or [])]
            self.groups.append(entry)
        self.expenses: list[dict] = []
        for raw in list(data.get("expenses", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("expense"))
            entry.setdefault("group_id", "")
            entry.setdefault("description", "")
            try:
                entry["amount"] = float(entry.get("amount", 0.0) or 0.0)
            except (TypeError, ValueError):
                entry["amount"] = 0.0
            entry.setdefault("currency", "USD")
            entry.setdefault("paid_by", self.current_user_id)
            entry.setdefault("split_equally", True)
            entry.setdefault("shares", [])
            entry.setdefault("status", "open")
            entry.setdefault("created_at", "")
            entry.setdefault("settled_at", "")
            self.expenses.append(entry)

    def get_state(self) -> dict:
        return {
            "app_name": self.app_name,
            "current_user_id": self.current_user_id,
            "users": self.users,
            "groups": self.groups,
            "expenses": self.expenses,
            "open_expense_count": len(self._open_expenses()),
        }

    def _open_expenses(self) -> list[dict]:
        return [e for e in self.expenses if e.get("status") == "open"]

    def _find_user(self, user_id: str) -> Optional[dict]:
        return next(
            (u for u in self.users if str(u.get("id")) == str(user_id)),
            None,
        )

    def _find_group(self, group_id: str) -> Optional[dict]:
        return next(
            (g for g in self.groups if str(g.get("id")) == str(group_id)),
            None,
        )

    def _find_expense(self, expense_id: str) -> Optional[dict]:
        return next(
            (e for e in self.expenses if str(e.get("id")) == str(expense_id)),
            None,
        )

    def _group_expenses(self, group_id: str) -> list[dict]:
        return [
            e for e in self.expenses
            if str(e.get("group_id")) == str(group_id)
        ]

    def _resolve_members(self, member_ids: list[str]) -> list[dict]:
        members: list[dict] = []
        for mid in member_ids:
            user = self._find_user(mid)
            if user:
                members.append(dict(user))
            else:
                members.append({"id": mid, "name": "", "email": ""})
        return members

    def _equal_shares(
        self, member_ids: list[str], amount: float, paid_by: str,
    ) -> list[dict]:
        n = len(member_ids) or 1
        # Keep cents-friendly 2-decimal shares; remainder on the last member.
        base = round(amount / n, 2)
        shares: list[dict] = []
        allocated = 0.0
        for i, uid in enumerate(member_ids):
            if i == n - 1:
                owed = round(amount - allocated, 2)
            else:
                owed = base
                allocated = round(allocated + base, 2)
            shares.append({
                "user_id": str(uid),
                "owed_share": owed,
                "paid_share": round(amount, 2) if str(uid) == str(paid_by) else 0.0,
            })
        return shares

    def _normalize_shares(
        self,
        raw_shares: list[dict],
        amount: float,
        paid_by: str,
        member_ids: list[str],
    ) -> list[dict]:
        if not raw_shares:
            return self._equal_shares(member_ids, amount, paid_by)
        shares: list[dict] = []
        for raw in raw_shares:
            uid = str(raw.get("user_id") or "")
            try:
                owed = float(raw.get("owed_share", 0.0) or 0.0)
            except (TypeError, ValueError):
                owed = 0.0
            try:
                paid = float(raw.get("paid_share", 0.0) or 0.0)
            except (TypeError, ValueError):
                paid = 0.0
            if "paid_share" not in raw and uid == str(paid_by):
                paid = round(amount, 2)
            shares.append({
                "user_id": uid,
                "owed_share": round(owed, 2),
                "paid_share": round(paid, 2),
            })
        return shares

    def _balances_for_group(self, group_id: str) -> list[dict]:
        """Net balance per member from open expenses (positive = owed money)."""
        nets: dict[str, float] = {}
        group = self._find_group(group_id)
        for mid in (group.get("member_ids", []) if group else []):
            nets[str(mid)] = 0.0
        for expense in self._group_expenses(group_id):
            if expense.get("status") != "open":
                continue
            for share in list(expense.get("shares") or []):
                uid = str(share.get("user_id") or "")
                if not uid:
                    continue
                try:
                    paid = float(share.get("paid_share", 0.0) or 0.0)
                except (TypeError, ValueError):
                    paid = 0.0
                try:
                    owed = float(share.get("owed_share", 0.0) or 0.0)
                except (TypeError, ValueError):
                    owed = 0.0
                nets[uid] = round(nets.get(uid, 0.0) + paid - owed, 2)
        balances = []
        for uid, net in sorted(nets.items(), key=lambda x: x[0]):
            user = self._find_user(uid) or {"id": uid, "name": "", "email": ""}
            balances.append({
                "user_id": uid,
                "name": user.get("name", ""),
                "net_balance": net,
            })
        return balances

    def _serialize_group(self, group: dict) -> dict:
        member_ids = list(group.get("member_ids") or [])
        expenses = self._group_expenses(group["id"])
        return {
            **group,
            "members": self._resolve_members(member_ids),
            "expenses": expenses,
            "balances": self._balances_for_group(group["id"]),
            "open_expense_count": len(
                [e for e in expenses if e.get("status") == "open"]
            ),
        }

    def _group_summary(self, group: dict) -> dict:
        expenses = self._group_expenses(group["id"])
        return {
            "id": group.get("id"),
            "name": group.get("name", ""),
            "member_ids": list(group.get("member_ids") or []),
            "member_count": len(group.get("member_ids") or []),
            "expense_count": len(expenses),
            "open_expense_count": len(
                [e for e in expenses if e.get("status") == "open"]
            ),
            "created_at": group.get("created_at", ""),
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/groups/list", operation_id="list_groups",
                          summary="List expense-sharing groups")
        def list_groups() -> dict:
            groups = [svc._group_summary(g) for g in svc.groups]
            svc.log_action("list_groups", params={}, group_count=len(groups))
            return {
                "ok": True,
                "app_name": svc.app_name,
                "current_user_id": svc.current_user_id,
                "groups": groups,
            }

        @self.router.post("/groups/get", operation_id="get_group",
                          summary="Get one group with members, expenses, balances")
        def get_group(req: GroupIdRequest):
            params = req.model_dump()
            group = svc._find_group(req.group_id)
            if not group:
                svc.log_action(
                    "get_group", params=params, status=404, group_id=req.group_id,
                )
                return svc.error_response(
                    code="GROUP_NOT_FOUND",
                    message="group not found",
                    status=404,
                    details={"group_id": req.group_id},
                )
            payload = svc._serialize_group(group)
            svc.log_action(
                "get_group",
                params=params,
                group_id=req.group_id,
                expense_count=len(payload["expenses"]),
            )
            return {"ok": True, "group": payload}

        @self.router.post("/expenses/add", operation_id="add_expense",
                          summary="Add an open expense to a group")
        def add_expense(req: AddExpenseRequest):
            params = req.model_dump()
            group = svc._find_group(req.group_id)
            if not group:
                svc.log_action(
                    "add_expense",
                    params=params, status=404, group_id=req.group_id,
                )
                return svc.error_response(
                    code="GROUP_NOT_FOUND",
                    message="group not found",
                    status=404,
                    details={"group_id": req.group_id},
                )
            description = (req.description or "").strip()
            if not description:
                svc.log_action("add_expense", params=params, status=422)
                return svc.error_response(
                    code="DESCRIPTION_REQUIRED",
                    message="expense description is required",
                    status=422,
                    details={"group_id": req.group_id},
                )
            try:
                amount = float(req.amount)
            except (TypeError, ValueError):
                amount = 0.0
            if amount <= 0:
                svc.log_action("add_expense", params=params, status=422)
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="expense amount must be positive",
                    status=422,
                    details={"group_id": req.group_id, "amount": req.amount},
                )
            paid_by = (req.paid_by or "").strip() or svc.current_user_id
            member_ids = [str(m) for m in list(group.get("member_ids") or [])]
            if paid_by and paid_by not in member_ids:
                member_ids = member_ids + [paid_by]
            shares = svc._normalize_shares(
                list(req.shares or []),
                round(amount, 2),
                paid_by,
                member_ids,
            )
            expense = {
                "id": svc.new_id("expense"),
                "group_id": str(group["id"]),
                "description": description,
                "amount": round(amount, 2),
                "currency": (req.currency or "USD").strip() or "USD",
                "paid_by": paid_by,
                "split_equally": bool(req.split_equally) and not req.shares,
                "shares": shares,
                "status": "open",
                "created_at": svc.now_iso(),
                "settled_at": "",
            }
            svc.expenses.append(expense)
            svc.log_action(
                "add_expense",
                params=params,
                effect={"expense_id": expense["id"], "group_id": expense["group_id"]},
                expense_id=expense["id"],
                group_id=expense["group_id"],
                amount=expense["amount"],
                paid_by=paid_by,
            )
            return {"ok": True, "expense": expense}

        @self.router.post("/expenses/settle", operation_id="settle_expense",
                          summary="Mark an open expense as settled")
        def settle_expense(req: SettleExpenseRequest):
            params = req.model_dump()
            expense = svc._find_expense(req.expense_id)
            if not expense:
                svc.log_action(
                    "settle_expense",
                    params=params, status=404, expense_id=req.expense_id,
                )
                return svc.error_response(
                    code="EXPENSE_NOT_FOUND",
                    message="expense not found",
                    status=404,
                    details={"expense_id": req.expense_id},
                )
            if expense.get("status") == "settled":
                svc.log_action(
                    "settle_expense",
                    params=params, status=409, expense_id=req.expense_id, no_op=True,
                )
                return svc.error_response(
                    code="ALREADY_SETTLED",
                    message="expense already settled",
                    status=409,
                    details={"expense_id": req.expense_id},
                )
            expense["status"] = "settled"
            expense["settled_at"] = svc.now_iso()
            svc.log_action(
                "settle_expense",
                params=params,
                effect={"expense_id": req.expense_id, "status": "settled"},
                expense_id=req.expense_id,
                group_id=expense.get("group_id", ""),
            )
            return {"ok": True, "expense": expense}


def create_service(service_id: str) -> ExpenseSharingService:
    return ExpenseSharingService(service_id)
