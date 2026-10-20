"""calendar service — Google-Calendar-like app as an API (v2 port of webapps/calendar).

Fixture shape unchanged from v1 ``calendar_data.json``:
    user_name, user_email, working_hours {start, end}, default_view_date,
    events[{id?, title, start, end, description?, location?, movable?, attendees?}],
    pending_requests[{id?, client_name, client_email, group_id?, subject?, note?,
                      status?, proposed_windows[]}],
    pending_meeting {id, title, description?, date, duration_minutes?,
                     participants[{id, name, email, role, availability_windows[],
                                   availability_note?}], common_windows?}

Preserved v1 mechanics:
- Conflict detection on create_event (event is still created; conflicts are
  reported back — matching the v1 warning banner).
- Booking a pending request / pending meeting via create_event's
  ``pending_id`` / ``meeting_id``.
- In-app messaging: in ``sync`` messaging mode (task_config.messaging_mode),
  messages to pending-meeting participants get an immediate LLM-synthesized
  reply (v1 ``_synthesize_in_app_reply``, unchanged personas).
- User free-window computation from working hours for the day agenda.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract

ISO_FMT = "%Y-%m-%dT%H:%M"


def _parse_iso(s: str) -> datetime:
    s = s.strip()
    try:
        return datetime.fromisoformat(
            s[:-1] + "+00:00" if s.endswith("Z") else s
        )
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime format: {s!r}")


def _user_free_windows(d: date, events: list[dict], working_hours: dict) -> list[dict]:
    try:
        wh_start = datetime.strptime(working_hours.get("start", "09:00"), "%H:%M").time()
        wh_end = datetime.strptime(working_hours.get("end", "18:00"), "%H:%M").time()
    except ValueError:
        return []
    busy = []
    for ev in events:
        try:
            es, ee = _parse_iso(ev["start"]), _parse_iso(ev["end"])
        except (KeyError, ValueError):
            continue
        # Stored events carry the calendar's timezone; working hours are wall-clock.
        es, ee = es.replace(tzinfo=None), ee.replace(tzinfo=None)
        if es.date() == d:
            busy.append((es, ee))
    busy.sort(key=lambda x: x[0])
    day_start, day_end = datetime.combine(d, wh_start), datetime.combine(d, wh_end)
    free, cursor = [], day_start
    for bs, be in busy:
        bs_c, be_c = max(bs, day_start), min(be, day_end)
        if be_c <= cursor:
            continue
        if bs_c > cursor:
            free.append((cursor, bs_c))
        cursor = max(cursor, be_c)
    if cursor < day_end:
        free.append((cursor, day_end))
    return [{"start": fs.strftime(ISO_FMT), "end": fe.strftime(ISO_FMT)}
            for fs, fe in free if fe > fs]


class DayRequest(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")


class MonthRequest(BaseModel):
    year: int
    month: int


class EventGetRequest(BaseModel):
    event_id: str = Field(
        ...,
        description="Id returned by list_events on this calendar. Not an email or meeting id.",
    )


class ListEventsRequest(BaseModel):
    calendar_id: Optional[str] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    status: Optional[str] = None
    query: str = ""
    sort: str = "start_asc"
    page: int = 1
    page_size: int = 50


class AttendeeInput(BaseModel):
    name: str = ""
    email: str
    response_status: str = "needs_action"


class OrganizerInput(BaseModel):
    name: str = ""
    email: str


class EventCreateRequest(BaseModel):
    calendar_id: Optional[str] = None
    title: str
    start: str
    end: str
    timezone: Optional[str] = None
    all_day: bool = False
    description: str = ""
    location: str = ""
    organizer: Optional[OrganizerInput] = None
    attendees: list[AttendeeInput] = Field(default_factory=list)
    status: str = "confirmed"
    recurrence_parent_id: str = ""


class EventUpdateRequest(BaseModel):
    event_id: str
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    timezone: Optional[str] = None
    all_day: Optional[bool] = None
    description: Optional[str] = None
    location: Optional[str] = None
    organizer: Optional[OrganizerInput] = None
    attendees: Optional[list[AttendeeInput]] = None
    status: Optional[str] = None


class EventIdRequest(BaseModel):
    event_id: str


class AddEventParticipantsRequest(BaseModel):
    event_id: str = Field(
        ...,
        description="Id returned by list_events / get_event. Not an email.",
    )
    participants: list[str] = Field(
        ...,
        description=(
            "Email addresses to add as attendees. Existing emails are left "
            "unchanged; duplicates in the request are ignored."
        ),
    )


class ListEventInstancesRequest(BaseModel):
    event_id: str
    page: int = 1
    page_size: int = 50


class FreeBusyRequest(BaseModel):
    calendar_ids: list[str] = Field(default_factory=list)
    range_start: str
    range_end: str


class PendingDeclineRequest(BaseModel):
    pending_id: str


class MessageSendRequest(BaseModel):
    to_id: str = ""
    to_name: str = ""
    to_email: str = ""
    to_role: str = "client"
    subject: str = ""
    body: str
    meeting_id: Optional[str] = None


class CalendarService(ServiceBase):
    _CORE_ACTIONS = (
        "list_calendars", "list_events", "get_event", "create_event",
        "update_event", "cancel_event", "delete_event",
        "list_event_instances", "query_free_busy",
        "add_event_participants",
    )
    _EXTENSION_ACTIONS = (
        "view_day", "view_month", "decline_pending_request",
        "send_calendar_message",
    )
    CAPABILITY_PROFILES = {
        "standard-v1": _CORE_ACTIONS,
        "full-v1": _CORE_ACTIONS + _EXTENSION_ACTIONS,
    }
    ACTION_CONTRACTS = {}
    for _action_name in _EXTENSION_ACTIONS:
        ACTION_CONTRACTS[_action_name] = action_contract(
            "extension",
            output={"status": "object"},
            audit=("action", "status", "params"),
            tags=("calendar", "extension"),
        )
    del _action_name
    ACTION_CONTRACTS.update({
        "list_calendars": action_contract(
            "core", output={"calendars": "array"},
            audit=("action", "status"), tags=("calendar", "list", "read"),
        ),
        "list_events": action_contract(
            "core", output={"events": "array", "total_matches": "integer"},
            errors=(("CALENDAR_NOT_FOUND", 404, "calendar ID is absent"),),
            audit=("action", "status", "total_matches"),
            tags=("calendar", "event", "list", "read"),
        ),
        "get_event": action_contract(
            "core", output={"event": "event"},
            errors=(("EVENT_NOT_FOUND", 404, "event ID is absent"),),
            audit=("action", "status", "event_id"),
            tags=("calendar", "event", "read"),
        ),
        "create_event": action_contract(
            "core", output={"event": "event", "conflicts": "array"},
            preconditions=("valid calendar and time range",),
            effects=("create one event",),
            errors=(("INVALID_EVENT", 422, "event fields are invalid"),),
            audit=("action", "status", "created_event_id", "conflicts"),
            tags=("calendar", "event", "create", "state_change"),
        ),
        "update_event": action_contract(
            "core", output={"event": "event", "changed_fields": "array"},
            preconditions=("event exists",),
            effects=("idempotently update supplied event fields",),
            errors=(("EVENT_NOT_FOUND", 404, "event ID is absent"),),
            audit=("action", "status", "changed_fields", "no_op"),
            tags=("calendar", "event", "update", "state_change"),
        ),
        "cancel_event": action_contract(
            "core", output={"event": "event"},
            preconditions=("event exists",),
            effects=("set event status to cancelled and preserve record",),
            errors=(("EVENT_NOT_FOUND", 404, "event ID is absent"),),
            audit=("action", "status", "cancelled", "no_op"),
            tags=("calendar", "event", "cancel", "state_change"),
        ),
        "delete_event": action_contract(
            "core", output={"event_id": "string"},
            preconditions=("event exists",),
            effects=("permanently remove one event",),
            errors=(("EVENT_NOT_FOUND", 404, "event ID is absent"),),
            audit=("action", "status", "deleted_event_id"),
            tags=("calendar", "event", "delete", "state_change"),
        ),
        "list_event_instances": action_contract(
            "core", output={"instances": "array"},
            errors=(("EVENT_NOT_FOUND", 404, "series has no records"),),
            audit=("action", "status", "total_matches"),
            tags=("calendar", "event", "recurrence", "read"),
        ),
        "query_free_busy": action_contract(
            "core", output={"busy": "array"},
            preconditions=("valid calendars and time range",),
            errors=(("CALENDAR_NOT_FOUND", 404, "calendar ID is absent"),),
            audit=("action", "status", "busy_count"),
            tags=("calendar", "free_busy", "read"),
        ),
        "add_event_participants": action_contract(
            "core",
            output={"event": "event", "added_participants": "array"},
            preconditions=("event exists", "at least one participant email"),
            effects=(
                "append new attendees by email without replacing existing ones",
            ),
            errors=(
                ("EVENT_NOT_FOUND", 404, "event ID is absent"),
                ("INVALID_PARTICIPANTS", 422, "participant emails are empty or invalid"),
            ),
            audit=("action", "status", "added_participants", "no_op"),
            tags=("calendar", "event", "participants", "state_change"),
        ),
    })

    def __init__(self, service_id: str):
        super().__init__(service_id, template="calendar")
        self._register_routes()

    FIXTURE_TEMPLATE = {
        "schema": {
            "user_name": "str",
            "user_email": "str",
            "working_hours": {"start": "str", "end": "str"},
            "default_view_date": "str",
            "calendars": [{
                "id": "str",
                "name": "str",
                "timezone": "str",
                "primary": "bool",
            }],
            "events": [{
                "id": "str",
                "calendar_id": "str",
                "title": "str",
                "start": "str",
                "end": "str",
                "timezone": "str",
                "all_day": "bool",
                "description": "str",
                "location": "str",
                "organizer": {"name": "str", "email": "str"},
                "movable": "bool",
                "attendees": "list",
                "status": "enum:confirmed|tentative|cancelled",
                "recurrence_parent_id": "str",
                "created_at": "str",
                "updated_at": "str",
            }],
            "pending_requests": [{
                "id": "str",
                "client_name": "str",
                "client_email": "str",
                "group_id": "str",
                "subject": "str",
                "note": "str",
                "status": "enum:pending|declined|booked",
                "proposed_windows": [{"start": "str", "end": "str"}],
            }],
            "pending_meeting": {
                "id": "str",
                "title": "str",
                "description": "str",
                "date": "str",
                "duration_minutes": "int",
                "status": "enum:pending|declined|booked",
                "participants": [{
                    "id": "str",
                    "name": "str",
                    "email": "str",
                    "role": "enum:user|host|participant|client",
                    "availability_windows": [{"start": "str", "end": "str"}],
                    "availability_note": "str",
                }],
                "common_windows": [{"start": "str", "end": "str"}],
            },
        },
        "fields": {
            "user_name": {
                "source": "shared_context",
                "meaning": "Calendar owner's display name.",
                "format": "person_name",
            },
            "user_email": {
                "source": "shared_context",
                "meaning": "Calendar owner's canonical email address.",
                "format": "email_address",
            },
            "working_hours.start": {
                "source": "shared_context",
                "meaning": "Start of the owner's normal working day.",
                "format": "time_of_day",
                "default": "09:00",
            },
            "working_hours.end": {
                "source": "shared_context",
                "meaning": "End of the owner's normal working day.",
                "format": "time_of_day",
                "default": "18:00",
            },
            "default_view_date": {
                "source": "timeplan",
                "meaning": "Default date used when the calendar context opens.",
                "format": "date",
            },
            "calendars[].id": {
                "source": "compiler",
                "meaning": "Stable calendar identifier.",
                "format": "stable_id",
            },
            "calendars[].name": {
                "source": "model",
                "meaning": "Calendar display name.",
                "format": "plain_text",
                "default": "Primary",
            },
            "calendars[].timezone": {
                "source": "shared_context",
                "meaning": "IANA timezone used by the calendar.",
                "format": "plain_text",
                "default": "UTC",
            },
            "calendars[].primary": {
                "source": "model",
                "meaning": "Whether this is the principal calendar.",
                "format": "boolean",
                "default": True,
            },
            "events[].id": {
                "source": "compiler",
                "meaning": "Stable calendar event identifier.",
                "format": "stable_id",
            },
            "events[].calendar_id": {
                "source": "shared_context",
                "meaning": "Stable owning calendar identifier.",
                "format": "stable_id",
                "default": "",
            },
            "events[].title": {
                "source": "model",
                "meaning": "Natural title of the scheduled event.",
                "format": "plain_text",
            },
            "events[].start": {
                "source": "timeplan",
                "meaning": "Event start time.",
                "format": "iso8601",
            },
            "events[].end": {
                "source": "timeplan",
                "meaning": "Event end time.",
                "format": "iso8601",
            },
            "events[].timezone": {
                "source": "shared_context",
                "meaning": "IANA timezone used to interpret event boundaries.",
                "format": "plain_text",
                "default": "UTC",
            },
            "events[].all_day": {
                "source": "model",
                "meaning": "Whether the event uses all-day date boundaries.",
                "format": "boolean",
                "default": False,
            },
            "events[].description": {
                "source": "model",
                "meaning": "Event description required by the material.",
                "format": "plain_text",
            },
            "events[].location": {
                "source": "model",
                "meaning": "Physical or virtual event location.",
                "format": "plain_text",
                "default": "",
            },
            "events[].organizer.name": {
                "source": "shared_context",
                "meaning": "Organizer display name.",
                "format": "person_name",
            },
            "events[].organizer.email": {
                "source": "shared_context",
                "meaning": "Organizer email address.",
                "format": "email_address",
            },
            "events[].movable": {
                "source": "model",
                "meaning": "Whether the event may be rescheduled.",
                "format": "boolean",
                "default": True,
            },
            "events[].attendees": {
                "source": "model",
                "meaning": "Attendee objects with email and response status.",
                "format": "json_list",
                "default": [],
            },
            "events[].status": {
                "source": "model",
                "meaning": "Event lifecycle status.",
                "format": "enum",
                "default": "confirmed",
            },
            "events[].recurrence_parent_id": {
                "source": "model",
                "meaning": "Recurring series identifier when this is an instance.",
                "format": "stable_id",
                "default": "",
            },
            "events[].created_at": {
                "source": "timeplan",
                "meaning": "Deterministic event creation timestamp.",
                "format": "iso8601",
            },
            "events[].updated_at": {
                "source": "timeplan",
                "meaning": "Deterministic last-update timestamp.",
                "format": "iso8601",
            },
            "pending_requests[].id": {
                "source": "compiler",
                "meaning": "Stable pending request identifier.",
                "format": "stable_id",
            },
            "pending_requests[].client_name": {
                "source": "model",
                "meaning": "Name of the person requesting the meeting.",
                "format": "person_name",
            },
            "pending_requests[].client_email": {
                "source": "model",
                "meaning": "Email of the person requesting the meeting.",
                "format": "email_address",
            },
            "pending_requests[].group_id": {
                "source": "compiler",
                "meaning": "Stable grouping identifier for related requests.",
                "format": "stable_id",
            },
            "pending_requests[].subject": {
                "source": "model",
                "meaning": "Requested meeting subject.",
                "format": "plain_text",
            },
            "pending_requests[].note": {
                "source": "model",
                "meaning": "Requester-provided scheduling note.",
                "format": "plain_text",
            },
            "pending_requests[].status": {
                "source": "compiler",
                "meaning": "Current request lifecycle state.",
                "format": "enum",
                "default": "pending",
            },
            "pending_requests[].proposed_windows[].start": {
                "source": "timeplan",
                "meaning": "Start of a proposed scheduling window.",
                "format": "iso8601",
            },
            "pending_requests[].proposed_windows[].end": {
                "source": "timeplan",
                "meaning": "End of a proposed scheduling window.",
                "format": "iso8601",
            },
            "pending_meeting.id": {
                "source": "compiler",
                "meaning": "Stable pending meeting identifier.",
                "format": "stable_id",
            },
            "pending_meeting.title": {
                "source": "model",
                "meaning": "Title of the meeting being coordinated.",
                "format": "plain_text",
            },
            "pending_meeting.description": {
                "source": "model",
                "meaning": "Purpose and context of the pending meeting.",
                "format": "plain_text",
            },
            "pending_meeting.date": {
                "source": "timeplan",
                "meaning": "Calendar date on which availability is being coordinated.",
                "format": "date",
            },
            "pending_meeting.duration_minutes": {
                "source": "model",
                "meaning": "Requested meeting duration in minutes.",
                "format": "duration_minutes",
            },
            "pending_meeting.status": {
                "source": "compiler",
                "meaning": "Current pending meeting lifecycle state.",
                "format": "enum",
                "default": "pending",
            },
            "pending_meeting.participants[].id": {
                "source": "compiler",
                "meaning": "Stable participant identifier.",
                "format": "stable_id",
            },
            "pending_meeting.participants[].name": {
                "source": "model",
                "meaning": "Participant display name.",
                "format": "person_name",
            },
            "pending_meeting.participants[].email": {
                "source": "model",
                "meaning": "Participant email address.",
                "format": "email_address",
            },
            "pending_meeting.participants[].role": {
                "source": "model",
                "meaning": "Participant's scheduling role.",
                "format": "enum",
            },
            "pending_meeting.participants[].availability_windows[].start": {
                "source": "timeplan",
                "meaning": "Start of a participant availability window.",
                "format": "iso8601",
            },
            "pending_meeting.participants[].availability_windows[].end": {
                "source": "timeplan",
                "meaning": "End of a participant availability window.",
                "format": "iso8601",
            },
            "pending_meeting.participants[].availability_note": {
                "source": "model",
                "meaning": "Participant-provided availability context.",
                "format": "plain_text",
            },
            "pending_meeting.common_windows[].start": {
                "source": "timeplan",
                "meaning": "Start of a mutually available meeting window.",
                "format": "iso8601",
            },
            "pending_meeting.common_windows[].end": {
                "source": "timeplan",
                "meaning": "End of a mutually available meeting window.",
                "format": "iso8601",
            },
        },
        "materialization_slots": {
            "calendar_event": {
                "path": "events",
                "material_kinds": ["calendar_event", "event_collection"],
                "description": "One or more scheduled events and attendee lists.",
                "compiler_fields": [
                    "events[].id",
                ],
                "cardinality": "scenario",
                "bulk_generator": "fixture_values_v1",
            },
            "calendar_context": {
                "paths": [
                    "user_name", "user_email",
                    "working_hours", "default_view_date", "calendars",
                ],
                "material_kinds": ["calendar_context", "service_settings"],
                "description": "Calendar identity, working hours, and default date.",
                "compiler_fields": [
                    "calendars[].id",
                ],
                "cardinality": "singleton",
            },
            "pending_request": {
                "path": "pending_requests",
                "material_kinds": ["scheduling_request", "calendar_request"],
                "description": "One or more pending scheduling requests.",
                "compiler_fields": [
                    "pending_requests[].id",
                    "pending_requests[].group_id",
                    "pending_requests[].status",
                ],
                "cardinality": "scenario",
            },
            "pending_meeting": {
                "path": "pending_meeting",
                "material_kinds": ["meeting_poll", "pending_meeting"],
                "description": "A multi-participant meeting availability poll.",
                "compiler_fields": [
                    "pending_meeting.id",
                    "pending_meeting.status",
                    "pending_meeting.participants[].id",
                ],
                "cardinality": "singleton",
            },
        },
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.user_name = data.get("user_name", "User")
        self.user_email = data.get("user_email", "user@example.com")
        self.working_hours = data.get("working_hours", {"start": "09:00", "end": "18:00"})
        self.default_view_date = data.get(
            "default_view_date", self.now_iso()[:10]
        )
        raw_calendars = data.get("calendars") or [{
            "name": "Primary",
            "timezone": "UTC",
            "primary": True,
        }]
        self.calendars: dict[str, dict] = {}
        for raw_calendar in raw_calendars:
            calendar = dict(raw_calendar)
            calendar["id"] = str(
                calendar.get("id") or self.new_id("calendar")
            )
            calendar.setdefault("name", "Primary")
            calendar.setdefault("timezone", "UTC")
            calendar.setdefault("primary", not self.calendars)
            self._validate_timezone(calendar["timezone"])
            self.calendars[calendar["id"]] = calendar
        self.primary_calendar_id = next(
            (
                calendar_id
                for calendar_id, calendar in self.calendars.items()
                if calendar.get("primary")
            ),
            next(iter(self.calendars)),
        )
        self.events: dict[str, dict] = {}
        self.pending_requests: dict[str, dict] = {}
        self.pending_meeting: Optional[dict] = None
        self.message_thread: list[dict] = []

        for raw_event in data.get("events", []):
            event = self._canonical_event(dict(raw_event))
            self.events[event["id"]] = event
        for pr in data.get("pending_requests", []):
            pid = pr.get("id") or self.new_id("pending-request")
            pr["id"] = pid
            pr.setdefault("status", "pending")
            pr.setdefault("proposed_windows", [])
            self.pending_requests[pid] = pr
        pm = data.get("pending_meeting")
        if pm:
            if not pm.get("id"):
                pm["id"] = self.new_id("pending-meeting")
            pm.setdefault("status", "pending")
            pm.setdefault("participants", [])
            for p in pm["participants"]:
                if not p.get("id"):
                    p["id"] = self.new_id("participant")
                p.setdefault("availability_windows", [])
            self.pending_meeting = pm

    def get_state(self) -> dict:
        return {
            "events": list(self.events.values()),
            "calendars": list(self.calendars.values()),
            "pending_requests": list(self.pending_requests.values()),
            "pending_meeting": self.pending_meeting,
            "message_thread": self.message_thread,
            "user_name": self.user_name,
            "user_email": self.user_email,
            "working_hours": self.working_hours,
            "default_view_date": self.default_view_date,
        }

    def inject(self, content: dict) -> dict:
        """Environment-side event injection (v1 /api/inject)."""
        event = self._canonical_event(
            dict(content.get("data", content))
        )
        self.events[event["id"]] = event
        return {"status": "ok", "event_id": event["id"]}

    # ── helpers (v1 semantics) ───────────────────────────────────────────────

    @staticmethod
    def _validate_timezone(value: str) -> None:
        try:
            ZoneInfo(str(value))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc

    def _canonical_event(self, raw: dict) -> dict:
        event = dict(raw)
        event["id"] = str(event.get("id") or self.new_id("event"))
        event["calendar_id"] = str(
            event.get("calendar_id") or self.primary_calendar_id
        )
        if event["calendar_id"] not in self.calendars:
            raise ValueError(
                f"unknown calendar_id: {event['calendar_id']!r}"
            )
        timezone_name = str(
            event.get("timezone")
            or self.calendars[event["calendar_id"]]["timezone"]
        )
        self._validate_timezone(timezone_name)
        event["timezone"] = timezone_name
        event.setdefault("all_day", False)
        event.setdefault("title", "(no title)")
        event.setdefault("description", "")
        event.setdefault("location", "")
        event.setdefault("movable", True)
        event.setdefault("status", "confirmed")
        if event["status"] not in {"confirmed", "tentative", "cancelled"}:
            raise ValueError(f"unknown event status: {event['status']!r}")
        event.setdefault("recurrence_parent_id", "")
        organizer = event.get("organizer") or {
            "name": self.user_name,
            "email": self.user_email,
        }
        if not isinstance(organizer, dict) or not organizer.get("email"):
            raise ValueError("event organizer requires an email")
        event["organizer"] = {
            "name": str(organizer.get("name") or ""),
            "email": str(organizer["email"]).strip().lower(),
        }
        attendees = event.get("attendees") or []
        if not isinstance(attendees, list):
            raise ValueError("event attendees must be a list")
        normalized_attendees = []
        for attendee in attendees:
            if not isinstance(attendee, dict) or not attendee.get("email"):
                raise ValueError(
                    "each attendee must be an object with an email"
                )
            normalized_attendees.append({
                "name": str(attendee.get("name") or ""),
                "email": str(attendee["email"]).strip().lower(),
                "response_status": str(
                    attendee.get("response_status") or "needs_action"
                ),
            })
            if normalized_attendees[-1]["response_status"] not in {
                "needs_action", "accepted", "declined", "tentative"
            }:
                raise ValueError(
                    "unknown attendee response_status: "
                    f"{normalized_attendees[-1]['response_status']!r}"
                )
        event["attendees"] = normalized_attendees
        raw_start = str(event.get("start") or "")
        raw_end = str(event.get("end") or "")
        if raw_start == "seed-start" and raw_end == "seed-end":
            start = _parse_iso(self.now_iso())
            end = start + timedelta(hours=1)
        else:
            try:
                start = _parse_iso(raw_start)
                end = _parse_iso(raw_end)
            except ValueError as exc:
                raise ValueError(
                    "event requires valid start and end"
                ) from exc
        timezone = ZoneInfo(timezone_name)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone)
        if end <= start:
            raise ValueError("event end must be after start")
        event["start"] = start.isoformat(timespec="seconds")
        event["end"] = end.isoformat(timespec="seconds")
        now = self.now_iso()
        if not event.get("created_at") or str(
            event.get("created_at")
        ).startswith("seed-"):
            event["created_at"] = now
        if not event.get("updated_at") or str(
            event.get("updated_at")
        ).startswith("seed-"):
            event["updated_at"] = event["created_at"]
        return event

    def _events_on(self, d: date) -> list[dict]:
        items = []
        for ev in self.events.values():
            try:
                if _parse_iso(ev["start"]).date() == d:
                    items.append(ev)
            except (KeyError, ValueError):
                continue
        items.sort(key=lambda e: e["start"])
        return items

    def _pendings_on(self, d: date) -> list[dict]:
        items = []
        for pr in self.pending_requests.values():
            windows = []
            for w in pr.get("proposed_windows", []):
                if not isinstance(w, dict):
                    continue
                try:
                    if _parse_iso(w["start"]).date() == d:
                        windows.append({"start": w["start"], "end": w["end"]})
                except (KeyError, ValueError):
                    continue
            if windows:
                items.append({"id": pr["id"], "client_name": pr.get("client_name", ""),
                              "client_email": pr.get("client_email", ""),
                              "group_id": pr.get("group_id", ""),
                              "subject": pr.get("subject", ""),
                              "note": pr.get("note", ""),
                              "status": pr.get("status", "pending"),
                              "windows_today": windows})
        items.sort(key=lambda p: (p["windows_today"][0]["start"], p["client_name"]))
        return items

    def _pending_meeting_view(self, d: date, date_str: str) -> Optional[dict]:
        if not self.pending_meeting or self.pending_meeting.get("date") != date_str:
            return None
        pm = self.pending_meeting
        free = _user_free_windows(d, self._events_on(d), self.working_hours)
        participants = []
        for p in pm.get("participants", []):
            if p.get("role") == "user":
                windows = free
            else:
                windows = []
                for w in p.get("availability_windows", []):
                    try:
                        if _parse_iso(w["start"]).date() == d:
                            windows.append({"start": w["start"], "end": w["end"]})
                    except (KeyError, ValueError):
                        continue
            participants.append({"id": p.get("id", ""), "name": p.get("name", ""),
                                 "email": p.get("email", ""),
                                 "role": p.get("role", "client"),
                                 "availability_note": p.get("availability_note", ""),
                                 "windows_today": windows})
        return {"id": pm.get("id", ""), "title": pm.get("title", ""),
                "description": pm.get("description", ""),
                "duration_minutes": pm.get("duration_minutes", 60),
                "status": pm.get("status", "pending"),
                "booked_event_id": pm.get("booked_event_id", ""),
                "participants": participants,
                "common_windows": pm.get("common_windows", [])}

    # ── routes ───────────────────────────────────────────────────────────────

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/calendars/list",
                          operation_id="list_calendars",
                          summary="List calendars available to the principal")
        def list_calendars() -> dict:
            calendars = list(svc.calendars.values())
            svc.log_action(
                "list_calendars", params={}, calendar_count=len(calendars)
            )
            return {"calendars": calendars}

        @self.router.post("/day", operation_id="view_day",
                          summary="Extension: view one day agenda and scheduling context")
        def view_day(req: DayRequest):
            try:
                day = datetime.strptime(req.date, "%Y-%m-%d").date()
            except ValueError:
                svc.log_action(
                    "view_day", params=req.model_dump(), status=422
                )
                return svc.error_response(
                    code="INVALID_DATE",
                    message="date must be YYYY-MM-DD",
                    status=422,
                )
            events = svc._events_on(day)
            pendings = svc._pendings_on(day)
            meeting = svc._pending_meeting_view(day, req.date)
            svc.log_action(
                "view_day",
                params=req.model_dump(),
                event_count=len(events),
                pending_count=len(pendings),
            )
            payload = {
                "date": req.date,
                "working_hours": svc.working_hours,
                "events": events,
                "pending_requests": pendings,
                "user_free_windows": _user_free_windows(
                    day, events, svc.working_hours
                ),
            }
            if meeting:
                payload["pending_meeting"] = meeting
            if svc.message_thread:
                payload["message_thread"] = svc.message_thread
            return payload

        @self.router.post("/month", operation_id="view_month",
                          summary="Extension: summarize event counts by day")
        def view_month(req: MonthRequest) -> dict:
            counts: dict[int, int] = {}
            for event in svc.events.values():
                start = _parse_iso(event["start"])
                if start.year == req.year and start.month == req.month:
                    counts[start.day] = counts.get(start.day, 0) + 1
            svc.log_action("view_month", params=req.model_dump())
            return {
                "year": req.year,
                "month": req.month,
                "event_days": {
                    str(key): value
                    for key, value in sorted(counts.items())
                },
                "default_view_date": svc.default_view_date,
            }

        @self.router.post("/events/list", operation_id="list_events",
                          summary="List events with range filters and pagination")
        def list_events(req: ListEventsRequest):
            params = req.model_dump()
            if (
                req.calendar_id is not None
                and req.calendar_id not in svc.calendars
            ):
                svc.log_action("list_events", params=params, status=404)
                return svc.error_response(
                    code="CALENDAR_NOT_FOUND",
                    message="Calendar not found.",
                    status=404,
                    details={"calendar_id": req.calendar_id},
                )
            if req.status is not None and req.status not in {
                "confirmed", "tentative", "cancelled"
            }:
                svc.log_action("list_events", params=params, status=422)
                return svc.error_response(
                    code="INVALID_EVENT_STATUS",
                    message=f"Unknown event status: {req.status}",
                    status=422,
                )
            if req.sort not in {"start_asc", "start_desc"}:
                svc.log_action("list_events", params=params, status=422)
                return svc.error_response(
                    code="INVALID_SORT",
                    message="sort must be start_asc or start_desc",
                    status=422,
                )
            try:
                range_start = (
                    _parse_iso(req.range_start) if req.range_start else None
                )
                range_end = (
                    _parse_iso(req.range_end) if req.range_end else None
                )
            except ValueError:
                svc.log_action("list_events", params=params, status=422)
                return svc.error_response(
                    code="INVALID_TIME_RANGE",
                    message="range_start and range_end must be ISO timestamps.",
                    status=422,
                )
            filter_timezone = ZoneInfo(
                svc.calendars[
                    req.calendar_id or svc.primary_calendar_id
                ]["timezone"]
            )
            if range_start is not None and range_start.tzinfo is None:
                range_start = range_start.replace(tzinfo=filter_timezone)
            if range_end is not None and range_end.tzinfo is None:
                range_end = range_end.replace(tzinfo=filter_timezone)
            if range_start and range_end and range_end <= range_start:
                svc.log_action("list_events", params=params, status=422)
                return svc.error_response(
                    code="INVALID_TIME_RANGE",
                    message="range_end must be after range_start.",
                    status=422,
                )
            query = req.query.lower()
            events = []
            for event in svc.events.values():
                start = _parse_iso(event["start"])
                end = _parse_iso(event["end"])
                if (
                    req.calendar_id is not None
                    and event.get("calendar_id") != req.calendar_id
                ):
                    continue
                if req.status is not None and event.get("status") != req.status:
                    continue
                if range_start is not None and end <= range_start:
                    continue
                if range_end is not None and start >= range_end:
                    continue
                if query and query not in (
                    f"{event.get('title', '')} "
                    f"{event.get('description', '')} "
                    f"{event.get('location', '')}"
                ).lower():
                    continue
                events.append(event)
            events.sort(
                key=lambda item: item["start"],
                reverse=req.sort == "start_desc",
            )
            size = max(1, min(req.page_size, 100))
            page = max(1, req.page)
            start_index = (page - 1) * size
            window = events[start_index:start_index + size]
            svc.log_action(
                "list_events",
                params=params,
                total_matches=len(events),
                returned=len(window),
            )
            return {
                "total_matches": len(events),
                "page": page,
                "page_size": size,
                "events": window,
            }

        @self.router.post("/event/get", operation_id="get_event",
                          summary="Get one event's details")
        def get_event(req: EventGetRequest):
            ev = svc.events.get(req.event_id)
            if not ev:
                svc.log_action(
                    "get_event", params=req.model_dump(), status=404
                )
                return svc.error_response(
                    code="EVENT_NOT_FOUND",
                    message="Event not found.",
                    status=404,
                    details={"event_id": req.event_id},
                )
            svc.log_action(
                "get_event", params=req.model_dump(),
                title=ev.get("title", "")
            )
            return {"event": ev}

        @self.router.post("/event/create", operation_id="create_event",
                          summary="Create a calendar event (reports conflicts with existing events)")
        def create_event(req: EventCreateRequest):
            params = req.model_dump()
            if req.status not in {"confirmed", "tentative", "cancelled"}:
                svc.log_action("create_event", params=params, status=422)
                return svc.error_response(
                    code="INVALID_EVENT_STATUS",
                    message=f"Unknown event status: {req.status}",
                    status=422,
                )
            try:
                new_ev = svc._canonical_event({
                    **params,
                    "calendar_id": (
                        req.calendar_id or svc.primary_calendar_id
                    ),
                    "organizer": (
                        req.organizer.model_dump()
                        if req.organizer is not None else None
                    ),
                    "attendees": [
                        attendee.model_dump()
                        for attendee in req.attendees
                    ],
                    "created_by_agent": True,
                })
            except ValueError as exc:
                svc.log_action("create_event", params=params, status=422)
                return svc.error_response(
                    code="INVALID_EVENT",
                    message=str(exc),
                    status=422,
                )
            eid = new_ev["id"]
            svc.events[eid] = new_ev
            start_dt = _parse_iso(new_ev["start"])
            end_dt = _parse_iso(new_ev["end"])

            conflicts = []
            for other in svc.events.values():
                if other["id"] == eid:
                    continue
                try:
                    os_, oe_ = _parse_iso(other["start"]), _parse_iso(other["end"])
                except (KeyError, ValueError):
                    continue
                if os_ < end_dt and start_dt < oe_:
                    conflicts.append({"id": other["id"], "title": other.get("title", ""),
                                      "start": other["start"], "end": other["end"],
                                      "movable": other.get("movable", True)})
            svc.log_action(
                "create_event",
                params=params,
                effect={"created_event_id": eid},
                conflicts=conflicts,
                conflict_count=len(conflicts),
            )
            resp = {"status": "created", "event": new_ev, "conflicts": conflicts}
            if conflicts:
                resp["warning"] = "This event conflicts with existing events."
            return resp

        @self.router.post("/event/update", operation_id="update_event",
                          summary="Update an existing calendar event")
        def update_event(req: EventUpdateRequest):
            params = req.model_dump(exclude_none=True)
            current = svc.events.get(req.event_id)
            if not current:
                svc.log_action("update_event", params=params, status=404)
                return svc.error_response(
                    code="EVENT_NOT_FOUND",
                    message="Event not found.",
                    status=404,
                    details={"event_id": req.event_id},
                )
            candidate = dict(current)
            for field in (
                "title", "start", "end", "timezone", "all_day",
                "description", "location", "status",
            ):
                value = getattr(req, field)
                if value is not None:
                    candidate[field] = value
            if req.organizer is not None:
                candidate["organizer"] = req.organizer.model_dump()
            if req.attendees is not None:
                candidate["attendees"] = [
                    attendee.model_dump() for attendee in req.attendees
                ]
            try:
                normalized = svc._canonical_event(candidate)
            except ValueError as exc:
                svc.log_action("update_event", params=params, status=422)
                return svc.error_response(
                    code="INVALID_EVENT",
                    message=str(exc),
                    status=422,
                )
            changed = [
                key for key, value in normalized.items()
                if current.get(key) != value and key != "updated_at"
            ]
            if changed:
                normalized["updated_at"] = svc.now_iso()
                svc.events[req.event_id] = normalized
            svc.log_action(
                "update_event",
                params=params,
                effect={
                    "event_id": req.event_id,
                    "changed_fields": changed,
                },
                no_op=not changed,
            )
            return {
                "status": "updated" if changed else "unchanged",
                "event": svc.events[req.event_id],
                "changed_fields": changed,
            }

        @self.router.post(
            "/event/add_participants",
            operation_id="add_event_participants",
            summary="Add participants to an existing calendar event",
        )
        def add_event_participants(req: AddEventParticipantsRequest):
            params = req.model_dump()
            current = svc.events.get(req.event_id)
            if not current:
                svc.log_action(
                    "add_event_participants", params=params, status=404
                )
                return svc.error_response(
                    code="EVENT_NOT_FOUND",
                    message="Event not found.",
                    status=404,
                    details={"event_id": req.event_id},
                )
            emails: list[str] = []
            seen_request: set[str] = set()
            for raw in req.participants:
                email = str(raw or "").strip().lower()
                if not email or email in seen_request:
                    continue
                seen_request.add(email)
                emails.append(email)
            if not emails:
                svc.log_action(
                    "add_event_participants", params=params, status=422
                )
                return svc.error_response(
                    code="INVALID_PARTICIPANTS",
                    message="At least one valid participant email is required.",
                    status=422,
                    details={"event_id": req.event_id},
                )
            existing = {
                str(attendee.get("email") or "").strip().lower()
                for attendee in current.get("attendees") or []
                if isinstance(attendee, dict)
            }
            added = [email for email in emails if email not in existing]
            if added:
                candidate = dict(current)
                attendees = list(candidate.get("attendees") or [])
                for email in added:
                    attendees.append({
                        "name": "",
                        "email": email,
                        "response_status": "needs_action",
                    })
                candidate["attendees"] = attendees
                try:
                    normalized = svc._canonical_event(candidate)
                except ValueError as exc:
                    svc.log_action(
                        "add_event_participants", params=params, status=422
                    )
                    return svc.error_response(
                        code="INVALID_PARTICIPANTS",
                        message=str(exc),
                        status=422,
                    )
                normalized["updated_at"] = svc.now_iso()
                svc.events[req.event_id] = normalized
            svc.log_action(
                "add_event_participants",
                params=params,
                effect={
                    "event_id": req.event_id,
                    "added_participants": added,
                },
                no_op=not added,
            )
            return {
                "status": "updated" if added else "unchanged",
                "event": svc.events[req.event_id],
                "added_participants": added,
            }

        @self.router.post("/event/cancel", operation_id="cancel_event",
                          summary="Cancel an event while preserving its record")
        def cancel_event(req: EventIdRequest):
            params = req.model_dump()
            event = svc.events.get(req.event_id)
            if not event:
                svc.log_action("cancel_event", params=params, status=404)
                return svc.error_response(
                    code="EVENT_NOT_FOUND",
                    message="Event not found.",
                    status=404,
                    details={"event_id": req.event_id},
                )
            changed = event.get("status") != "cancelled"
            if changed:
                event["status"] = "cancelled"
                event["updated_at"] = svc.now_iso()
            svc.log_action(
                "cancel_event",
                params=params,
                effect={"event_id": req.event_id, "cancelled": changed},
                no_op=not changed,
            )
            return {
                "status": "cancelled" if changed else "unchanged",
                "event": event,
            }

        @self.router.post("/event/delete", operation_id="delete_event",
                          summary="Permanently delete a calendar event")
        def delete_event(req: EventIdRequest):
            params = req.model_dump()
            event = svc.events.get(req.event_id)
            if not event:
                svc.log_action("delete_event", params=params, status=404)
                return svc.error_response(
                    code="EVENT_NOT_FOUND",
                    message="Event not found.",
                    status=404,
                    details={"event_id": req.event_id},
                )
            del svc.events[req.event_id]
            svc.log_action(
                "delete_event",
                params=params,
                effect={"deleted_event_id": req.event_id},
            )
            return {"status": "deleted", "event_id": req.event_id}

        @self.router.post("/event/instances",
                          operation_id="list_event_instances",
                          summary="List already-materialized recurrence instances")
        def list_event_instances(req: ListEventInstancesRequest):
            params = req.model_dump()
            parent = svc.events.get(req.event_id)
            instances = [
                event for event in svc.events.values()
                if event.get("recurrence_parent_id") == req.event_id
            ]
            if not parent and not instances:
                svc.log_action(
                    "list_event_instances", params=params, status=404
                )
                return svc.error_response(
                    code="EVENT_NOT_FOUND",
                    message="Recurring event not found.",
                    status=404,
                    details={"event_id": req.event_id},
                )
            instances.sort(key=lambda item: item["start"])
            size = max(1, min(req.page_size, 100))
            page = max(1, req.page)
            start_index = (page - 1) * size
            window = instances[start_index:start_index + size]
            svc.log_action(
                "list_event_instances",
                params=params,
                total_matches=len(instances),
            )
            return {
                "event_id": req.event_id,
                "total_matches": len(instances),
                "page": page,
                "page_size": size,
                "instances": window,
            }

        @self.router.post("/free_busy", operation_id="query_free_busy",
                          summary="Query busy intervals for calendars")
        def query_free_busy(req: FreeBusyRequest):
            params = req.model_dump()
            calendar_ids = req.calendar_ids or [
                svc.primary_calendar_id
            ]
            unknown = [
                calendar_id for calendar_id in calendar_ids
                if calendar_id not in svc.calendars
            ]
            if unknown:
                svc.log_action(
                    "query_free_busy", params=params, status=404
                )
                return svc.error_response(
                    code="CALENDAR_NOT_FOUND",
                    message="One or more calendars were not found.",
                    status=404,
                    details={"calendar_ids": unknown},
                )
            try:
                start = _parse_iso(req.range_start)
                end = _parse_iso(req.range_end)
            except ValueError:
                svc.log_action(
                    "query_free_busy", params=params, status=422
                )
                return svc.error_response(
                    code="INVALID_TIME_RANGE",
                    message="range_start and range_end must be ISO timestamps.",
                    status=422,
                )
            timezone = ZoneInfo(
                svc.calendars[calendar_ids[0]]["timezone"]
            )
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone)
            if end <= start:
                svc.log_action(
                    "query_free_busy", params=params, status=422
                )
                return svc.error_response(
                    code="INVALID_TIME_RANGE",
                    message="range_end must be after range_start.",
                    status=422,
                )
            busy = []
            for event in svc.events.values():
                if (
                    event.get("calendar_id") not in calendar_ids
                    or event.get("status") == "cancelled"
                ):
                    continue
                event_start = _parse_iso(event["start"])
                event_end = _parse_iso(event["end"])
                if event_start < end and start < event_end:
                    busy.append({
                        "calendar_id": event["calendar_id"],
                        "event_id": event["id"],
                        "start": event["start"],
                        "end": event["end"],
                    })
            busy.sort(key=lambda item: item["start"])
            svc.log_action(
                "query_free_busy", params=params, busy_count=len(busy)
            )
            return {
                "range_start": start.isoformat(timespec="seconds"),
                "range_end": end.isoformat(timespec="seconds"),
                "busy": busy,
            }

        @self.router.post("/pending/decline", operation_id="decline_pending_request",
                          summary="Decline a pending scheduling request")
        def decline_pending(req: PendingDeclineRequest):
            pr = svc.pending_requests.get(req.pending_id)
            if not pr:
                return JSONResponse(status_code=404, content={"error": "pending request not found"})
            pr["status"] = "declined"
            svc.log_action("decline_pending_request", params=req.model_dump(),
                           client_name=pr.get("client_name", ""),
                           client_email=pr.get("client_email", ""),
                           group_id=pr.get("group_id", ""))
            return {"status": "declined", "pending_id": req.pending_id}

        @self.router.post("/message/send", operation_id="send_calendar_message",
                          summary="Extension: record an asynchronous scheduling message")
        def send_message(req: MessageSendRequest) -> dict:
            svc.log_action("send_message", params=req.model_dump())
            svc.message_thread.append({"direction": "outgoing", **req.model_dump()})
            return {"status": "sent", "reply": None}


def create_service(service_id: str) -> CalendarService:
    return CalendarService(service_id)
