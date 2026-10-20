"""phone service — fixture-backed contacts and call history.

Reduced from AppWorld Phone: list/get contacts, list calls, and place an
outgoing call (recorded in fixture state only). Deterministic; no network,
OAuth, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class ContactIdRequest(BaseModel):
    contact_id: str = Field(..., description="Contact id.")


class CallContactRequest(BaseModel):
    contact_id: str = Field(..., description="Contact id to call.")
    duration_seconds: int = Field(
        0,
        ge=0,
        description="Optional call duration in seconds (fixture record only).",
    )


class PhoneService(ServiceBase):
    _READ = ("list_contacts", "get_contact", "list_calls")
    _MUTATE = ("call_contact",)
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("phone", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["call_contact"] = action_contract(
        "core", output={"ok": "boolean", "call": "object"},
        effects=("record one outgoing call to a contact",),
        errors=(("CONTACT_NOT_FOUND", 404, "contact id is absent"),),
        audit=("action", "status", "contact_id", "call_id"),
        tags=("phone", "call", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="phone")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "owner_name": "str",
        "owner_phone": "str",
        "contacts": [{
            "id": "str",
            "first_name": "str",
            "last_name": "str",
            "phone_number": "str",
            "email": "str",
            "relationship": "str",
        }],
        "calls": [{
            "id": "str",
            "contact_id": "str",
            "phone_number": "str",
            "contact_name": "str",
            "direction": "enum:incoming|outgoing",
            "duration_seconds": "int",
            "timestamp": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.owner_name = data.get("owner_name", "")
        self.owner_phone = data.get("owner_phone", "")
        self.contacts: list[dict] = []
        for c in list(data.get("contacts", [])):
            entry = dict(c)
            entry.setdefault("id", self.new_id("contact"))
            entry.setdefault("first_name", "")
            entry.setdefault("last_name", "")
            entry.setdefault("phone_number", "")
            entry.setdefault("email", "")
            entry.setdefault("relationship", "")
            self.contacts.append(entry)
        self.calls: list[dict] = []
        for call in list(data.get("calls", [])):
            entry = dict(call)
            entry.setdefault("id", self.new_id("call"))
            entry.setdefault("contact_id", "")
            entry.setdefault("phone_number", "")
            entry.setdefault("contact_name", "")
            entry.setdefault("direction", "outgoing")
            entry.setdefault("duration_seconds", 0)
            entry.setdefault("timestamp", "")
            self.calls.append(entry)

    def get_state(self) -> dict:
        return {
            "owner_name": self.owner_name,
            "owner_phone": self.owner_phone,
            "contacts": self.contacts,
            "calls": self.calls,
        }

    def _find_contact(self, contact_id: str) -> Optional[dict]:
        return next(
            (c for c in self.contacts if str(c.get("id")) == str(contact_id)),
            None,
        )

    @staticmethod
    def _contact_display_name(contact: dict) -> str:
        name = f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
        return name or str(contact.get("phone_number", "") or contact.get("id", ""))

    def _ordered_contacts(self) -> list[dict]:
        return sorted(
            self.contacts,
            key=lambda c: (
                str(c.get("last_name", "")).lower(),
                str(c.get("first_name", "")).lower(),
                str(c.get("id", "")),
            ),
        )

    def _ordered_calls(self) -> list[dict]:
        return sorted(
            self.calls,
            key=lambda c: str(c.get("timestamp", "")),
            reverse=True,
        )

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/contacts/list", operation_id="list_contacts",
                          summary="List phone contacts")
        def list_contacts() -> dict:
            contacts = svc._ordered_contacts()
            svc.log_action("list_contacts", params={}, contact_count=len(contacts))
            return {"ok": True, "contacts": contacts}

        @self.router.post("/contacts/get", operation_id="get_contact",
                          summary="Get one phone contact by id")
        def get_contact(req: ContactIdRequest):
            params = req.model_dump()
            contact = svc._find_contact(req.contact_id)
            if not contact:
                svc.log_action("get_contact", params=params, status=404)
                return svc.error_response(
                    code="CONTACT_NOT_FOUND",
                    message="Contact not found.",
                    status=404,
                    details={"contact_id": req.contact_id},
                )
            svc.log_action("get_contact", params=params, contact_id=req.contact_id)
            return {"ok": True, "contact": contact}

        @self.router.post("/calls/list", operation_id="list_calls",
                          summary="List recorded phone calls")
        def list_calls() -> dict:
            calls = svc._ordered_calls()
            svc.log_action("list_calls", params={}, call_count=len(calls))
            return {"ok": True, "calls": calls}

        @self.router.post("/contacts/call", operation_id="call_contact",
                          summary="Place an outgoing call and record it")
        def call_contact(req: CallContactRequest):
            params = req.model_dump()
            contact = svc._find_contact(req.contact_id)
            if not contact:
                svc.log_action("call_contact", params=params, status=404)
                return svc.error_response(
                    code="CONTACT_NOT_FOUND",
                    message="Contact not found.",
                    status=404,
                    details={"contact_id": req.contact_id},
                )
            call_id = svc.new_id("call")
            ts = svc.now_iso()
            entry = {
                "id": call_id,
                "contact_id": str(contact.get("id")),
                "phone_number": str(contact.get("phone_number", "")),
                "contact_name": svc._contact_display_name(contact),
                "direction": "outgoing",
                "duration_seconds": int(req.duration_seconds or 0),
                "timestamp": ts,
            }
            svc.calls.append(entry)
            svc.log_action(
                "call_contact",
                params=params,
                effect={"call_id": call_id, "contact_id": req.contact_id},
                contact_id=req.contact_id,
                call_id=call_id,
                phone_number=entry["phone_number"],
            )
            return {"ok": True, "call": entry}


def create_service(service_id: str) -> PhoneService:
    return PhoneService(service_id)
