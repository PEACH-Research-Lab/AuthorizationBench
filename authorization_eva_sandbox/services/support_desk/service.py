"""support_desk service — first-line customer support desk.

Reduced desk surface adapted from webapps/customer_support_console and the
tau-bench retail/airline customer-service shape:

    list_tickets, get_ticket, search_kb, send_reply, escalate

Replies are logged and appended to the ticket thread. Escalation marks the
ticket escalated and hands off to a human queue. Deterministic fixture state;
no network and no LLM.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class TicketIdRequest(BaseModel):
    ticket_id: str = Field(..., description="Support ticket id.")


class SearchKbRequest(BaseModel):
    query: str = Field("", description="Substring / tag search over KB articles.")


class SendReplyRequest(BaseModel):
    ticket_id: str
    body: str = Field(..., description="Customer-facing reply text.")
    cited_kb_slugs: list[str] = Field(
        default_factory=list,
        description="Optional KB article slugs cited in the reply.",
    )


class EscalateRequest(BaseModel):
    ticket_id: str
    note: str = Field("", description="Internal handoff note for the human queue.")
    queue: str = Field("tier-2", description="Human queue name.")


class SupportDeskService(ServiceBase):
    _READ = ("list_tickets", "get_ticket", "search_kb")
    _MUTATE = ("send_reply", "escalate")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("support_desk", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["send_reply"] = action_contract(
        "core", output={"ok": "boolean"},
        effects=("append one customer-facing reply on a ticket",),
        audit=("action", "status", "ticket_id"),
        tags=("support_desk", "reply", "state_change"),
    )
    ACTION_CONTRACTS["escalate"] = action_contract(
        "core", output={"ok": "boolean"},
        effects=("mark one ticket escalated and hand off to a human queue",),
        audit=("action", "status", "ticket_id"),
        tags=("support_desk", "escalate", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="support_desk")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "company_name": "str",
        "console_name": "str",
        "agent_display_name": "str",
        "agent_handle": "str",
        "tickets": [{
            "id": "str",
            "subject": "str",
            "customer_name": "str",
            "customer_email": "str",
            "status": "str",
            "urgency": "str",
            "opened_ts": "float",
            "messages": "list",
        }],
        "kb_articles": [{
            "slug": "str",
            "title": "str",
            "summary": "str",
            "body": "str",
            "tags": "list",
        }],
        "replies": "list",
        "escalations": "list",
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.company_name = data.get("company_name", "Acme")
        self.console_name = data.get("console_name", "Acme Support Desk")
        self.agent_display_name = data.get("agent_display_name", "Support Bot")
        self.agent_handle = data.get("agent_handle", "support-bot")
        self.tickets: list[dict] = []
        for t in list(data.get("tickets", [])):
            entry = dict(t)
            entry.setdefault("id", self.new_id("ticket"))
            entry.setdefault("subject", "Support request")
            entry.setdefault("customer_name", "")
            entry.setdefault("customer_email", "")
            entry.setdefault("status", "open")
            entry.setdefault("urgency", "normal")
            entry.setdefault("opened_ts", 0.0)
            entry.setdefault("messages", [])
            self.tickets.append(entry)
        self.kb_articles: list[dict] = []
        for a in list(data.get("kb_articles", [])):
            article = dict(a)
            article.setdefault(
                "slug",
                article.get("id") or self.new_id("kb"),
            )
            article.setdefault("title", "")
            article.setdefault("summary", "")
            article.setdefault("body", "")
            article.setdefault("tags", [])
            self.kb_articles.append(article)
        self.replies: list[dict] = list(data.get("replies", []))
        self.escalations: list[dict] = list(data.get("escalations", []))

    def get_state(self) -> dict:
        return {
            "company_name": self.company_name,
            "console_name": self.console_name,
            "agent_display_name": self.agent_display_name,
            "agent_handle": self.agent_handle,
            "tickets": self.tickets,
            "kb_articles": self.kb_articles,
            "replies": self.replies,
            "escalations": self.escalations,
            "open_count": len(self._open_tickets()),
        }

    def _open_tickets(self) -> list[dict]:
        return [t for t in self.tickets if t.get("status") == "open"]

    def _find_ticket(self, ticket_id: str) -> Optional[dict]:
        return next(
            (t for t in self.tickets if str(t.get("id")) == str(ticket_id)),
            None,
        )

    def _ordered_tickets(self) -> list[dict]:
        urgency_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}

        def key(t: dict):
            return (
                urgency_rank.get(t.get("urgency", "normal"), 2),
                -float(t.get("opened_ts", 0) or 0),
            )

        return sorted(self.tickets, key=key)

    def _ordered_articles(self) -> list[dict]:
        return sorted(self.kb_articles, key=lambda a: a.get("title", ""))

    def _kb_search(self, q: str) -> list[dict]:
        q = (q or "").strip().lower()
        if not q:
            return self._ordered_articles()
        terms = [t for t in re.split(r"\s+", q) if t]
        scored: list[tuple[int, dict]] = []
        for a in self.kb_articles:
            haystack = " ".join([
                a.get("title", ""),
                a.get("summary", ""),
                a.get("body", ""),
                " ".join(a.get("tags", [])),
            ]).lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                scored.append((score, a))
        scored.sort(key=lambda x: -x[0])
        return [a for _, a in scored]

    def _common_ctx(self) -> dict:
        return {
            "company_name": self.company_name,
            "console_name": self.console_name,
            "agent_display_name": self.agent_display_name,
            "agent_handle": self.agent_handle,
            "open_count": len(self._open_tickets()),
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/tickets/list", operation_id="list_tickets",
                          summary="List support tickets ordered by urgency")
        def list_tickets() -> dict:
            tickets = svc._ordered_tickets()
            svc.log_action("list_tickets", params={}, ticket_count=len(tickets))
            return {**svc._common_ctx(), "tickets": tickets}

        @self.router.post("/tickets/get", operation_id="get_ticket",
                          summary="Get one support ticket and its message thread")
        def get_ticket(req: TicketIdRequest):
            params = req.model_dump()
            ticket = svc._find_ticket(req.ticket_id)
            if not ticket:
                svc.log_action("get_ticket", params=params, status=404)
                return svc.error_response(
                    code="TICKET_NOT_FOUND",
                    message="Ticket not found.",
                    status=404,
                    details={"ticket_id": req.ticket_id},
                )
            svc.log_action("get_ticket", params=params)
            return {**svc._common_ctx(), "ticket": ticket}

        @self.router.post("/kb/search", operation_id="search_kb",
                          summary="Search the knowledge base by substring and tags")
        def search_kb(req: SearchKbRequest):
            params = req.model_dump()
            results = svc._kb_search(req.query)
            svc.log_action(
                "search_kb",
                params=params,
                result_count=len(results),
                result_slugs=[a.get("slug") for a in results],
            )
            return {
                **svc._common_ctx(),
                "query": req.query,
                "articles": results,
                "kb_articles_total": len(svc.kb_articles),
            }

        @self.router.post("/tickets/reply", operation_id="send_reply",
                          summary="Send a customer-facing reply on a ticket")
        def send_reply(req: SendReplyRequest):
            params = req.model_dump()
            ticket = svc._find_ticket(req.ticket_id)
            if not ticket:
                svc.log_action("send_reply", params=params, status=404)
                return svc.error_response(
                    code="TICKET_NOT_FOUND",
                    message="Ticket not found.",
                    status=404,
                    details={"ticket_id": req.ticket_id},
                )
            body = (req.body or "").strip()
            if not body:
                svc.log_action("send_reply", params=params, status=422)
                return svc.error_response(
                    code="BODY_REQUIRED",
                    message="Reply body is required.",
                    status=422,
                )
            cited = list(req.cited_kb_slugs or [])
            reply_id = svc.new_id("reply")
            ts = svc.now_iso()
            entry = {
                "reply_id": reply_id,
                "ticket_id": req.ticket_id,
                "from": svc.agent_display_name,
                "from_handle": svc.agent_handle,
                "body": body,
                "cited_kb_slugs": cited,
                "timestamp": ts,
            }
            svc.replies.append(entry)
            ticket.setdefault("messages", []).append({
                "role": "agent",
                "from": svc.agent_display_name,
                "body": body,
                "cited_kb_slugs": cited,
                "timestamp": ts,
            })
            ticket["status"] = "awaiting_customer"
            svc.log_action(
                "send_reply",
                params=params,
                effect={"reply_id": reply_id, "ticket_id": req.ticket_id},
                ticket_id=req.ticket_id,
                reply_id=reply_id,
                body=body,
                cited_kb_slugs=cited,
                customer_email=ticket.get("customer_email", ""),
            )
            return {"ok": True, "reply": entry, "ticket": ticket}

        @self.router.post("/tickets/escalate", operation_id="escalate",
                          summary="Escalate a ticket to a human support queue")
        def escalate(req: EscalateRequest):
            params = req.model_dump()
            ticket = svc._find_ticket(req.ticket_id)
            if not ticket:
                svc.log_action("escalate", params=params, status=404)
                return svc.error_response(
                    code="TICKET_NOT_FOUND",
                    message="Ticket not found.",
                    status=404,
                    details={"ticket_id": req.ticket_id},
                )
            note = (req.note or "").strip()
            queue = (req.queue or "tier-2").strip() or "tier-2"
            esc_id = svc.new_id("escalation")
            ts = svc.now_iso()
            entry = {
                "escalation_id": esc_id,
                "ticket_id": req.ticket_id,
                "queue": queue,
                "note": note,
                "filed_by": svc.agent_handle,
                "timestamp": ts,
            }
            svc.escalations.append(entry)
            ticket.setdefault("messages", []).append({
                "role": "system",
                "from": "Console",
                "body": f"Escalated to {queue}. Internal note: {note or '(none)'}",
                "timestamp": ts,
            })
            ticket["status"] = "escalated"
            svc.log_action(
                "escalate",
                params=params,
                effect={"escalation_id": esc_id, "ticket_id": req.ticket_id},
                ticket_id=req.ticket_id,
                escalation_id=esc_id,
                queue=queue,
                note=note,
            )
            return {"ok": True, "escalation": entry, "ticket": ticket}


def create_service(service_id: str) -> SupportDeskService:
    return SupportDeskService(service_id)
