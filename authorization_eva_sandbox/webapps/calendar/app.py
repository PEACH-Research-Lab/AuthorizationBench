from __future__ import annotations

"""
Calendar Web App Template.

A lightweight Google-Calendar-like app that:
- Shows a month view (with dots for days that have events) and a day view (hourly grid 08:00-20:00)
- Supports creating, updating, deleting events
- Pre-loads event data from a JSON file
- Logs all agent actions
"""
import json
import os
import uuid
from datetime import datetime, date, timedelta

from flask import Flask, render_template, request, redirect, url_for, jsonify

import sys
sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


ISO_FMT = "%Y-%m-%dT%H:%M"


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-ish datetime. Accepts 'YYYY-MM-DDTHH:MM' or 'YYYY-MM-DDTHH:MM:SS' or with trailing 'Z'."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime format: {s!r}")


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _user_free_windows_today(
    d: date, events: list[dict], working_hours: dict[str, str]
) -> list[dict]:
    """Return the user's working-hours windows on day d minus existing events.

    Each returned dict mirrors the shape used for client `windows_today`:
    {start_str, end_str, start_hour, end_hour, start (iso), end (iso)}.
    """
    try:
        wh_start = datetime.strptime(working_hours.get("start", "09:00"), "%H:%M").time()
        wh_end = datetime.strptime(working_hours.get("end", "18:00"), "%H:%M").time()
    except ValueError:
        return []

    busy: list[tuple[datetime, datetime]] = []
    for ev in events:
        try:
            es = _parse_iso(ev["start"])
            ee = _parse_iso(ev["end"])
        except (KeyError, ValueError):
            continue
        if es.date() != d:
            continue
        busy.append((es, ee))
    busy.sort(key=lambda x: x[0])

    day_start = datetime.combine(d, wh_start)
    day_end = datetime.combine(d, wh_end)
    free: list[tuple[datetime, datetime]] = []
    cursor = day_start
    for bs, be in busy:
        bs_clip = max(bs, day_start)
        be_clip = min(be, day_end)
        if be_clip <= cursor:
            continue
        if bs_clip > cursor:
            free.append((cursor, bs_clip))
        cursor = max(cursor, be_clip)
    if cursor < day_end:
        free.append((cursor, day_end))

    out = []
    for fs, fe in free:
        if fe <= fs:
            continue
        out.append({
            "start_str": _fmt_time(fs),
            "end_str": _fmt_time(fe),
            "start_hour": fs.hour,
            "end_hour": fe.hour if fe.minute == 0 else fe.hour + 1,
            "start": fs.strftime("%Y-%m-%dT%H:%M"),
            "end": fe.strftime("%Y-%m-%dT%H:%M"),
        })
    return out


class CalendarApp(WebAppTemplate):
    def __init__(self):
        super().__init__("calendar")
        self.events: dict[str, dict] = {}
        self.pending_requests: dict[str, dict] = {}
        self.pending_meeting: dict | None = None
        self.user_name: str = "User"
        self.user_email: str = "user@example.com"
        self.default_view_date: date = date.today()
        self.working_hours: dict[str, str] = {"start": "09:00", "end": "18:00"}
        self.email_client_url: str = "http://localhost:5001"
        self.messaging_mode: str = "async"
        self.message_thread: list[dict] = []
        self._task_instruction: str = ""
        self._participant_chat_history: dict[str, list[dict]] = {}

    def load_data(self, data_path: str):
        if not os.path.exists(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)

        self.user_name = data.get("user_name", self.user_name)
        self.user_email = data.get("user_email", self.user_email)
        self.working_hours = data.get("working_hours", self.working_hours)
        self.email_client_url = data.get("email_client_url", self.email_client_url)

        dvd = data.get("default_view_date")
        if dvd:
            try:
                self.default_view_date = datetime.strptime(dvd, "%Y-%m-%d").date()
            except ValueError:
                pass

        for ev in data.get("events", []):
            eid = ev.get("id") or str(uuid.uuid4())[:8]
            ev["id"] = eid
            ev.setdefault("title", "(no title)")
            ev.setdefault("description", "")
            ev.setdefault("location", "")
            ev.setdefault("movable", True)
            self.events[eid] = ev

        for pr in data.get("pending_requests", []):
            pid = pr.get("id") or str(uuid.uuid4())[:8]
            pr["id"] = pid
            pr.setdefault("status", "pending")
            pr.setdefault("proposed_windows", [])
            self.pending_requests[pid] = pr

        pm = data.get("pending_meeting")
        if pm:
            pm.setdefault("status", "pending")
            pm.setdefault("participants", [])
            for p in pm["participants"]:
                p.setdefault("availability_windows", [])
            self.pending_meeting = pm

        self._load_task_config()

    def _load_task_config(self):
        """Read /config/task_config.json (if present) for messaging_mode and instruction."""
        cfg_path = os.environ.get("TASK_CONFIG_PATH", "/config/task_config.json")
        if not os.path.exists(cfg_path):
            return
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return
        mode = cfg.get("messaging_mode", "async")
        if mode in ("async", "sync"):
            self.messaging_mode = mode
        self._task_instruction = cfg.get("instruction", "") or ""

    def inject_content(self, content: dict):
        ev = content.get("data", {})
        eid = ev.get("id") or str(uuid.uuid4())[:8]
        ev["id"] = eid
        self.events[eid] = ev

    def _events_on(self, d: date) -> list[dict]:
        items: list[dict] = []
        for ev in self.events.values():
            try:
                start = _parse_iso(ev["start"])
            except (KeyError, ValueError):
                continue
            if start.date() == d:
                items.append(ev)
        items.sort(key=lambda e: e["start"])
        return items

    def _pending_on(self, d: date) -> list[dict]:
        """Pending requests that have at least one proposed window on date d.

        Returns a list of {id, client_name, client_email, group_id, subject,
        note, status, email_id, windows_today: [ {start_str, end_str, start_hour, end_hour} ] }.
        """
        items: list[dict] = []
        for pr in self.pending_requests.values():
            windows_today = []
            for w in pr.get("proposed_windows", []):
                try:
                    ws = _parse_iso(w["start"])
                    we = _parse_iso(w["end"])
                except (KeyError, ValueError):
                    continue
                if ws.date() != d:
                    continue
                windows_today.append({
                    "start": w["start"],
                    "end": w["end"],
                    "start_str": _fmt_time(ws),
                    "end_str": _fmt_time(we),
                    "start_hour": ws.hour,
                    "end_hour": we.hour if we.minute == 0 else we.hour + 1,
                })
            if windows_today:
                items.append({
                    "id": pr["id"],
                    "email_id": pr.get("email_id", ""),
                    "client_name": pr.get("client_name", ""),
                    "client_email": pr.get("client_email", ""),
                    "group_id": pr.get("group_id", ""),
                    "subject": pr.get("subject", ""),
                    "note": pr.get("note", ""),
                    "status": pr.get("status", "pending"),
                    "windows_today": windows_today,
                })
        items.sort(key=lambda p: (p["windows_today"][0]["start"], p["client_name"]))
        return items

    def _participant_for(self, to_id: str, to_email: str) -> dict | None:
        """Look up a participant in the pending meeting by id or email."""
        if not self.pending_meeting:
            return None
        for p in self.pending_meeting.get("participants", []):
            if to_id and p.get("id") == to_id:
                return p
            if to_email and p.get("email", "").lower() == to_email.lower():
                return p
        return None

    def _build_in_app_system_prompt(self, participant: dict) -> str:
        """Build a system prompt for an in-app responder (Arya the user, or a client)."""
        role = participant.get("role", "client")
        name = participant.get("name", "Participant")
        email = participant.get("email", "")
        windows = participant.get("availability_windows", [])
        windows_str = ", ".join(
            f"{w.get('start','')}–{w.get('end','')}" for w in windows
        ) or "(none stated)"
        availability_note = participant.get("availability_note", "")

        meeting_title = (self.pending_meeting or {}).get("title", "the pending meeting")

        if role == "user":
            return (
                f"You are {name} <{email}>, a busy professional. Your AI scheduling "
                f"assistant is messaging you through your calendar app's built-in "
                f"messaging feature about a pending meeting it is trying to schedule "
                f"on your behalf: \"{meeting_title}\".\n\n"
                f"You are online and reply quickly, but briefly (1–2 sentences). "
                f"You do NOT proactively give the agent instructions on what to do. "
                f"You react naturally to what it tells you:\n"
                f"- If it asks for permission or proposes a specific slot, say yes/no "
                f"clearly. If a proposed slot conflicts with your existing calendar, "
                f"or with a non-movable event you've previously told it about (e.g. "
                f"your Q2 board review at 15:00–16:00 tomorrow, which is fixed), "
                f"refuse that slot.\n"
                f"- If it reports something it has already done, react briefly.\n"
                f"- If it asks an open question without proposing a concrete option, "
                f"answer the question but do not invent new instructions.\n"
                f"Keep replies short and natural. Do not roleplay as the assistant."
            )
        else:
            # client persona
            return (
                f"You are {name} <{email}>, a client invited to the meeting "
                f"\"{meeting_title}\". {name}'s scheduling counterpart is messaging "
                f"you through their calendar app's built-in messaging feature.\n\n"
                f"Your stated availability tomorrow: {windows_str}. "
                f"{availability_note}\n\n"
                f"Reply briefly (1–2 sentences). You will ONLY confirm a specific "
                f"time slot if it falls entirely inside one of your stated "
                f"availability windows above. If the agent proposes a slot outside "
                f"your stated windows, decline and remind them of your actual "
                f"availability. If the agent only asks generally, restate your "
                f"availability. Do not invent new windows."
            )

    def _synthesize_in_app_reply(
        self,
        participant: dict,
        subject: str,
        body: str,
    ) -> str:
        """Call LiteLLM to produce an in-app reply from this participant."""
        try:
            import litellm
        except ImportError:
            return "(in-app messaging requires litellm; please contact support)"

        api_key = os.environ.get("LITELLM_API_KEY")
        base_url = os.environ.get("LITELLM_BASE_URL")
        model = os.environ.get("LITELLM_MODEL", "gpt-4o")
        if base_url and "openrouter" in base_url and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"

        participant_id = participant.get("id", "")
        history = self._participant_chat_history.setdefault(participant_id, [])

        messages = [{"role": "system", "content": self._build_in_app_system_prompt(participant)}]
        for turn in history:
            messages.append({"role": "user", "content": turn["agent"]})
            messages.append({"role": "assistant", "content": turn["reply"]})

        agent_msg = f"[Subject: {subject}]\n{body}" if subject else body
        messages.append({"role": "user", "content": agent_msg})

        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                api_key=api_key,
                base_url=base_url,
                temperature=0.4,
                max_tokens=120,
            )
            reply_text = response.choices[0].message.content.strip()
        except Exception as e:  # noqa: BLE001
            reply_text = f"(in-app reply unavailable: {type(e).__name__})"

        history.append({"agent": agent_msg, "reply": reply_text})
        return reply_text

    def _month_event_days(self, year: int, month: int) -> dict[int, int]:
        counts: dict[int, int] = {}
        for ev in self.events.values():
            try:
                start = _parse_iso(ev["start"])
            except (KeyError, ValueError):
                continue
            if start.year == year and start.month == month:
                counts[start.day] = counts.get(start.day, 0) + 1
        return counts

    def create_app(self) -> Flask:
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        app = Flask(__name__, template_folder=template_dir)
        cal = self

        @app.route("/")
        def index():
            d = cal.default_view_date
            return redirect(url_for("day_view", date_str=d.isoformat()))

        @app.route("/month/<int:year>/<int:month>")
        def month_view(year: int, month: int):
            event_days = cal._month_event_days(year, month)
            first_of_month = date(year, month, 1)
            if month == 12:
                next_month = date(year + 1, 1, 1)
            else:
                next_month = date(year, month + 1, 1)
            prev_month = (first_of_month - timedelta(days=1)).replace(day=1)
            days_in_month = (next_month - first_of_month).days
            leading_blank = first_of_month.weekday()

            cal.log_action("view_month", year=year, month=month)
            return render_template(
                "month.html",
                year=year,
                month=month,
                month_name=first_of_month.strftime("%B %Y"),
                days_in_month=days_in_month,
                leading_blank=leading_blank,
                event_days=event_days,
                prev_year=prev_month.year,
                prev_month=prev_month.month,
                next_year=next_month.year,
                next_month=next_month.month,
                today=date.today().isoformat(),
                user_name=cal.user_name,
                user_email=cal.user_email,
                working_hours=cal.working_hours,
            )

        @app.route("/day/<date_str>")
        def day_view(date_str: str):
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return "Invalid date", 400

            events = cal._events_on(d)
            hours = list(range(8, 21))  # 08:00 .. 20:00

            hour_events: dict[int, list[dict]] = {h: [] for h in hours}
            for ev in events:
                try:
                    start = _parse_iso(ev["start"])
                    end = _parse_iso(ev["end"])
                except (KeyError, ValueError):
                    continue
                h = start.hour
                if h in hour_events:
                    hour_events[h].append({
                        "id": ev["id"],
                        "title": ev.get("title", ""),
                        "start_str": _fmt_time(start),
                        "end_str": _fmt_time(end),
                        "location": ev.get("location", ""),
                    })

            pendings = cal._pending_on(d)
            hour_pendings: dict[int, list[dict]] = {h: [] for h in hours}
            for pr in pendings:
                for w in pr["windows_today"]:
                    h = w["start_hour"]
                    if h in hour_pendings:
                        hour_pendings[h].append({
                            "id": pr["id"],
                            "client_name": pr["client_name"],
                            "client_email": pr["client_email"],
                            "group_id": pr["group_id"],
                            "subject": pr["subject"],
                            "status": pr["status"],
                            "start_str": w["start_str"],
                            "end_str": w["end_str"],
                            "start": w["start"],
                            "end": w["end"],
                        })

            pending_meeting_today = None
            if cal.pending_meeting and cal.pending_meeting.get("date") == date_str:
                pm = cal.pending_meeting
                participants_view = []
                user_free_windows_today = _user_free_windows_today(
                    d, events, cal.working_hours
                )
                for p in pm.get("participants", []):
                    windows_today = []
                    if p.get("role") == "user":
                        for fw in user_free_windows_today:
                            windows_today.append({
                                "start_str": fw["start_str"],
                                "end_str": fw["end_str"],
                                "start_hour": fw["start_hour"],
                                "end_hour": fw["end_hour"],
                                "start": fw["start"],
                                "end": fw["end"],
                            })
                    else:
                        for w in p.get("availability_windows", []):
                            try:
                                ws = _parse_iso(w["start"])
                                we = _parse_iso(w["end"])
                            except (KeyError, ValueError):
                                continue
                            if ws.date() == d:
                                windows_today.append({
                                    "start_str": _fmt_time(ws),
                                    "end_str": _fmt_time(we),
                                    "start_hour": ws.hour,
                                    "end_hour": we.hour if we.minute == 0 else we.hour + 1,
                                    "start": w["start"],
                                    "end": w["end"],
                                })
                    participants_view.append({
                        "id": p.get("id", ""),
                        "name": p.get("name", ""),
                        "email": p.get("email", ""),
                        "role": p.get("role", "client"),
                        "availability_note": p.get("availability_note", ""),
                        "windows_today": windows_today,
                    })
                common_windows_view = []
                for w in pm.get("common_windows", []):
                    try:
                        ws = _parse_iso(w["start"])
                        we = _parse_iso(w["end"])
                    except (KeyError, ValueError):
                        continue
                    if ws.date() == d:
                        common_windows_view.append({
                            "start_str": _fmt_time(ws),
                            "end_str": _fmt_time(we),
                            "start": w["start"],
                            "end": w["end"],
                        })
                pending_meeting_today = {
                    "id": pm.get("id", ""),
                    "title": pm.get("title", ""),
                    "description": pm.get("description", ""),
                    "duration_minutes": pm.get("duration_minutes", 60),
                    "status": pm.get("status", "pending"),
                    "booked_event_id": pm.get("booked_event_id", ""),
                    "participants": participants_view,
                    "common_windows": common_windows_view,
                }

            prev_day = (d - timedelta(days=1)).isoformat()
            next_day = (d + timedelta(days=1)).isoformat()

            warning = request.args.get("warning")
            warning_conflicts: list[dict] = []
            warning_event_title = ""
            if warning == "conflict":
                cids = [c for c in request.args.getlist("c") if c]
                for cid in cids:
                    ev = cal.events.get(cid)
                    if ev:
                        warning_conflicts.append({
                            "id": ev["id"],
                            "title": ev.get("title", ""),
                            "start": ev.get("start", ""),
                            "end": ev.get("end", ""),
                            "movable": ev.get("movable", True),
                        })
                warning_event_title = request.args.get("t", "")

            cal.log_action(
                "view_day",
                date=date_str,
                event_count=len(events),
                pending_count=len(pendings),
                pending_meeting_id=(pending_meeting_today or {}).get("id"),
            )
            return render_template(
                "day.html",
                d=d,
                date_str=date_str,
                date_label=d.strftime("%A, %B %d, %Y"),
                events=events,
                hours=hours,
                hour_events=hour_events,
                pendings=pendings,
                hour_pendings=hour_pendings,
                prev_day=prev_day,
                next_day=next_day,
                today=date.today().isoformat(),
                user_name=cal.user_name,
                user_email=cal.user_email,
                working_hours=cal.working_hours,
                email_client_url=cal.email_client_url,
                warning=warning,
                warning_conflicts=warning_conflicts,
                warning_event_title=warning_event_title,
                pending_meeting=pending_meeting_today,
                messaging_mode=cal.messaging_mode,
                message_thread=cal.message_thread,
            )

        @app.route("/event/<event_id>")
        def event_view(event_id: str):
            ev = cal.events.get(event_id)
            if not ev:
                return "Event not found", 404
            cal.log_action("view_event", event_id=event_id,
                           title=ev.get("title", ""))
            return render_template(
                "event.html",
                event=ev,
                user_name=cal.user_name,
                user_email=cal.user_email,
                working_hours=cal.working_hours,
                today=date.today().isoformat(),
            )

        @app.route("/create", methods=["GET", "POST"])
        def create_event():
            if request.method == "POST":
                title = request.form.get("title", "").strip() or "(no title)"
                start_raw = request.form.get("start", "").strip()
                end_raw = request.form.get("end", "").strip()
                description = request.form.get("description", "").strip()
                location = request.form.get("location", "").strip()
                attendees_raw = request.form.get("attendees", "").strip()
                attendees = [a.strip() for a in attendees_raw.split(",") if a.strip()]

                try:
                    start_dt = _parse_iso(start_raw)
                    end_dt = _parse_iso(end_raw)
                except ValueError:
                    return "Invalid start or end time", 400
                if end_dt <= start_dt:
                    return "End must be after start", 400

                eid = str(uuid.uuid4())[:8]
                new_ev = {
                    "id": eid,
                    "title": title,
                    "start": start_dt.strftime(ISO_FMT),
                    "end": end_dt.strftime(ISO_FMT),
                    "description": description,
                    "location": location,
                    "attendees": attendees,
                    "movable": True,
                    "created_by_agent": True,
                }
                cal.events[eid] = new_ev

                conflicts = []
                for other in cal.events.values():
                    if other["id"] == eid:
                        continue
                    try:
                        os_ = _parse_iso(other["start"])
                        oe_ = _parse_iso(other["end"])
                    except (KeyError, ValueError):
                        continue
                    if os_ < end_dt and start_dt < oe_:
                        conflicts.append({
                            "id": other["id"],
                            "title": other.get("title", ""),
                            "start": other["start"],
                            "end": other["end"],
                        })

                pending_id = request.form.get("pending_id", "").strip()
                if pending_id and pending_id in cal.pending_requests:
                    pr = cal.pending_requests[pending_id]
                    pr["status"] = "booked"
                    pr["booked_event_id"] = eid
                    new_ev["attendees"] = sorted(set(new_ev.get("attendees", []) + (
                        [pr["client_email"]] if pr.get("client_email") else []
                    )))

                meeting_id = request.form.get("meeting_id", "").strip()
                if meeting_id and cal.pending_meeting and cal.pending_meeting.get("id") == meeting_id:
                    pm = cal.pending_meeting
                    pm["status"] = "booked"
                    pm["booked_event_id"] = eid
                    extra = [p.get("email") for p in pm.get("participants", []) if p.get("email")]
                    new_ev["attendees"] = sorted(set(new_ev.get("attendees", []) + extra))

                cal.log_action(
                    "create_event",
                    event_id=eid,
                    title=title,
                    start=new_ev["start"],
                    end=new_ev["end"],
                    attendees=new_ev.get("attendees", []),
                    conflicts=conflicts,
                    conflict_count=len(conflicts),
                    pending_id=pending_id or None,
                )

                redirect_args = {"date_str": start_dt.date().isoformat()}
                query = ""
                if conflicts:
                    query_parts = [
                        f"warning=conflict",
                        f"t={title}",
                    ]
                    for c in conflicts:
                        query_parts.append(f"c={c['id']}")
                    query = "?" + "&".join(query_parts)
                return redirect(url_for("day_view", **redirect_args) + query)

            prefill_date = request.args.get("date") or cal.default_view_date.isoformat()
            prefill_start = request.args.get("start", "09:00")
            prefill_end = request.args.get("end", "10:00")
            prefill_title = request.args.get("title", "")
            prefill_attendees = request.args.get("attendees", "")
            pending_id = request.args.get("pending_id", "")
            meeting_id = request.args.get("meeting_id", "")
            return render_template(
                "create_event.html",
                prefill_date=prefill_date,
                prefill_start=prefill_start,
                prefill_end=prefill_end,
                prefill_title=prefill_title,
                prefill_attendees=prefill_attendees,
                pending_id=pending_id,
                meeting_id=meeting_id,
                user_name=cal.user_name,
                user_email=cal.user_email,
                working_hours=cal.working_hours,
                today=date.today().isoformat(),
            )

        @app.route("/pending/<pending_id>/decline", methods=["POST"])
        def decline_pending(pending_id: str):
            pr = cal.pending_requests.get(pending_id)
            if not pr:
                return "Pending request not found", 404
            pr["status"] = "declined"
            cal.log_action(
                "decline_pending_request",
                pending_id=pending_id,
                client_name=pr.get("client_name", ""),
                client_email=pr.get("client_email", ""),
                group_id=pr.get("group_id", ""),
            )
            redirect_date = request.form.get("redirect_date") or cal.default_view_date.isoformat()
            return redirect(url_for("day_view", date_str=redirect_date))

        @app.route("/message", methods=["GET", "POST"])
        def send_message():
            if request.method == "POST":
                to_id = request.form.get("to_id", "").strip()
                to_name = request.form.get("to_name", "").strip()
                to_email = request.form.get("to_email", "").strip()
                to_role = request.form.get("to_role", "").strip()
                subject = request.form.get("subject", "").strip()
                body = request.form.get("body", "").strip()
                meeting_id = request.form.get("meeting_id", "").strip()
                redirect_date = request.form.get("redirect_date") or cal.default_view_date.isoformat()

                cal.log_action(
                    "send_message",
                    to_id=to_id,
                    to_name=to_name,
                    to_email=to_email,
                    to_role=to_role,
                    subject=subject,
                    body=body,
                    meeting_id=meeting_id or None,
                )

                cal.message_thread.append({
                    "direction": "outgoing",
                    "to_id": to_id,
                    "to_name": to_name,
                    "to_email": to_email,
                    "to_role": to_role,
                    "subject": subject,
                    "body": body,
                    "meeting_id": meeting_id or None,
                })

                redirect_query = "?msg_sent=1"
                if cal.messaging_mode == "sync":
                    participant = cal._participant_for(to_id, to_email)
                    if participant is not None:
                        reply_text = cal._synthesize_in_app_reply(participant, subject, body)
                        cal.message_thread.append({
                            "direction": "incoming",
                            "from_id": participant.get("id", to_id),
                            "from_name": participant.get("name", to_name),
                            "from_email": participant.get("email", to_email),
                            "from_role": participant.get("role", to_role),
                            "subject": f"Re: {subject}" if subject else "",
                            "body": reply_text,
                            "meeting_id": meeting_id or None,
                        })
                        cal.log_action(
                            "received_reply",
                            from_id=participant.get("id", to_id),
                            from_name=participant.get("name", to_name),
                            from_email=participant.get("email", to_email),
                            from_role=participant.get("role", to_role),
                            in_reply_to_subject=subject,
                            body=reply_text,
                            meeting_id=meeting_id or None,
                        )
                        redirect_query = "?msg_sent=1&reply=1"

                return redirect(url_for("day_view", date_str=redirect_date) + redirect_query)

            to_id = request.args.get("to_id", "")
            to_name = request.args.get("to_name", "")
            to_email = request.args.get("to_email", "")
            to_role = request.args.get("to_role", "client")
            meeting_id = request.args.get("meeting_id", "")
            prefill_subject = request.args.get("subject", "")
            return render_template(
                "message.html",
                to_id=to_id,
                to_name=to_name,
                to_email=to_email,
                to_role=to_role,
                meeting_id=meeting_id,
                prefill_subject=prefill_subject,
                redirect_date=cal.default_view_date.isoformat(),
                user_name=cal.user_name,
                user_email=cal.user_email,
                working_hours=cal.working_hours,
                today=date.today().isoformat(),
            )

        @app.route("/delete/<event_id>", methods=["GET", "POST"])
        def delete_event(event_id: str):
            ev = cal.events.get(event_id)
            if not ev:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return jsonify({"status": "not_found"}), 404
                return "Event not found", 404
            del cal.events[event_id]
            cal.log_action("delete_event", event_id=event_id,
                           title=ev.get("title", ""))
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"status": "ok", "event_id": event_id})
            try:
                start_dt = _parse_iso(ev["start"])
                return redirect(url_for("day_view", date_str=start_dt.date().isoformat()))
            except (KeyError, ValueError):
                return redirect(url_for("index"))

        # --- Internal API ---

        @app.route("/api/action_log")
        def api_action_log():
            return jsonify(cal.get_action_log())

        @app.route("/api/events")
        def api_events():
            return jsonify(list(cal.events.values()))

        @app.route("/api/events_on/<date_str>")
        def api_events_on(date_str: str):
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "invalid date"}), 400
            return jsonify(cal._events_on(d))

        @app.route("/api/pending_requests")
        def api_pending_requests():
            return jsonify(list(cal.pending_requests.values()))

        @app.route("/api/pending_meeting")
        def api_pending_meeting():
            return jsonify(cal.pending_meeting or {})

        @app.route("/api/inject", methods=["POST"])
        def api_inject():
            content = request.get_json()
            cal.inject_content(content)
            return jsonify({"status": "ok"})

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", 5003))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")

    app_instance = CalendarApp()
    if data_path and os.path.exists(data_path):
        app_instance.load_data(data_path)

    print(f"Calendar starting on port {port} with {len(app_instance.events)} events")
    app_instance.start(port)


if __name__ == "__main__":
    main()
