"""Canonical Meeting service with standard core and convenience extensions."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


_DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_join_meetings": True,
    "auto_join_countdown_seconds": 10,
    "auto_record": True,
    "auto_transcribe": True,
    "auto_share_screen": False,
    "join_audio_on_start": True,
    "auto_share_summary": "all_participants",
}
_SUMMARY_AUDIENCES = {"all_participants", "host_only", "off"}
_ARTIFACT_STATES = {"off", "active", "processing", "ready", "failed"}


class TranscriptQueryRequest(BaseModel):
    start_time: Optional[str] = Field(
        None,
        description="ISO-8601 window start. A segment is included when it overlaps the window.",
    )
    end_time: Optional[str] = Field(
        None,
        description="ISO-8601 window end. A segment is included when it overlaps the window.",
    )
    cursor: Optional[str] = None
    limit: int = Field(
        100, ge=1, le=500,
        description="Maximum segments to return. Integer from 1 to 500.",
    )


class ToggleSettingRequest(BaseModel):
    key: str
    value: Any = None


class SettingsUpdateRequest(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


def _setting_value(key: str, value: Any) -> Any:
    if key == "auto_share_summary":
        value = str(value)
        return value if value in _SUMMARY_AUDIENCES else "all_participants"
    if key == "auto_join_countdown_seconds":
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 10
    return bool(value)


class ZoomService(ServiceBase):
    """One canonical Meeting resource; name retained as the template ID."""

    _CORE_ACTIONS = (
        "get_meeting", "accept_invitation", "decline_invitation",
        "leave_meeting", "list_participants", "start_recording",
        "stop_recording", "start_transcription", "stop_transcription",
        "get_transcript", "get_summary", "create_summary_email_draft",
    )
    _EXTENSION_ACTIONS = (
        "view_desktop", "join_meeting", "decline_meeting",
        "continue_recording", "open_summary_in_email", "view_settings",
        "toggle_setting", "api_set_settings",
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
            tags=("meeting", "extension"),
        )
    del _action_name
    ACTION_CONTRACTS.update({
        "get_meeting": action_contract(
            "core", output={"meeting": "meeting"},
            audit=("action", "status"), tags=("meeting", "state", "read"),
        ),
        "accept_invitation": action_contract(
            "core", output={"meeting": "meeting"},
            effects=("set invitation accepted",),
            audit=("action", "status", "invitation_status", "no_op"),
            tags=("meeting", "invitation", "accept", "state_change"),
        ),
        "decline_invitation": action_contract(
            "core", output={"meeting": "meeting"},
            preconditions=("local participant has not joined",),
            effects=("set invitation declined",),
            errors=(("ALREADY_JOINED", 409, "local participant joined"),),
            audit=("action", "status", "invitation_status", "no_op"),
            tags=("meeting", "invitation", "decline", "state_change"),
        ),
        "leave_meeting": action_contract(
            "core", output={"meeting": "meeting"},
            preconditions=("local participant is joined",),
            effects=("set local participant left",),
            errors=(("NOT_JOINED", 409, "local participant is not joined"),),
            audit=("action", "status", "local_participant_status"),
            tags=("meeting", "participant", "leave", "state_change"),
        ),
        "list_participants": action_contract(
            "core", output={"participants": "array"},
            audit=("action", "status", "participant_count"),
            tags=("meeting", "participant", "list", "read"),
        ),
        "start_recording": action_contract(
            "core", output={"recording_status": "string"},
            preconditions=("conference active", "local participant joined"),
            effects=("set recording active",),
            errors=(("MEETING_NOT_ACTIVE", 409, "meeting is not active"),),
            audit=("action", "status", "recording_status", "no_op"),
            tags=("meeting", "recording", "start", "state_change"),
        ),
        "stop_recording": action_contract(
            "core", output={"recording_status": "string"},
            preconditions=("recording active",),
            effects=("set recording processing",),
            errors=(("RECORDING_NOT_ACTIVE", 409, "recording is not active"),),
            audit=("action", "status", "recording_status"),
            tags=("meeting", "recording", "stop", "state_change"),
        ),
        "start_transcription": action_contract(
            "core", output={"transcription_status": "string"},
            preconditions=("conference active", "local participant joined"),
            effects=("set transcription active",),
            errors=(("MEETING_NOT_ACTIVE", 409, "meeting is not active"),),
            audit=("action", "status", "transcription_status", "no_op"),
            tags=("meeting", "transcription", "start", "state_change"),
        ),
        "stop_transcription": action_contract(
            "core", output={"transcription_status": "string"},
            preconditions=("transcription active",),
            effects=("set transcription processing",),
            errors=(("TRANSCRIPTION_NOT_ACTIVE", 409, "transcription inactive"),),
            audit=("action", "status", "transcription_status"),
            tags=("meeting", "transcription", "stop", "state_change"),
        ),
        "get_transcript": action_contract(
            "core", output={"segments": "array", "next_cursor": "string|null"},
            errors=(("INVALID_CURSOR", 422, "cursor is invalid"),),
            audit=("action", "status", "returned", "total_matches"),
            tags=("meeting", "transcript", "read", "pagination"),
        ),
        "get_summary": action_contract(
            "core", output={"summary": "summary"},
            preconditions=("summary ready",),
            errors=(("SUMMARY_NOT_READY", 409, "summary is not ready"),),
            audit=("action", "status", "summary_id"),
            tags=("meeting", "summary", "read"),
        ),
        "create_summary_email_draft": action_contract(
            "core",
            output={
                "summary_id": "string",
                "email_draft_id": "string",
                "recipients": "array",
            },
            preconditions=(
                "summary ready", "recipients resolved",
                "standard Email service mounted",
            ),
            effects=("create one persistent Email draft",),
            errors=(
                ("SUMMARY_NOT_READY", 409, "summary is not ready"),
                ("RECIPIENTS_UNRESOLVED", 409, "no recipients"),
                ("EMAIL_SERVICE_UNAVAILABLE", 503, "Email not mounted"),
            ),
            audit=("action", "status", "summary_id", "email_draft_id"),
            tags=(
                "meeting", "summary", "email", "draft",
                "cross_service", "state_change",
            ),
        ),
    })
    ENVIRONMENT_ACTION_CONTRACTS = {
        name: action_contract(
            "environment_only",
            output={"ok": "boolean"},
            effects=(f"apply environment transition {name}",),
            audit=("action", "status", "source", "effect"),
            tags=("meeting", "environment"),
        )
        for name in (
            "conference_start", "conference_end", "participant_join",
            "participant_leave", "append_transcript", "summary_ready",
            "recording_ready", "transcription_ready", "ringing_timed_out",
        )
    }

    FIXTURE_TEMPLATE = {
        "schema": {
            "meeting": {
                "meeting_id": "str",
                "topic": "str",
                "host": "str",
                "scheduled_start": "str",
                "scheduled_end": "str",
                "invitation_status": "enum:pending|accepted|declined",
                "local_participant_status": "enum:not_joined|joined|left",
                "conference_status": "enum:scheduled|in_progress|ended",
                "recording_status": "enum:off|active|processing|ready|failed",
                "transcription_status": "enum:off|active|processing|ready|failed",
                "participants": [{
                    "participant_id": "str",
                    "display_name": "str",
                    "email": "str",
                    "sessions": [{
                        "joined_at": "str",
                        "left_at": "str",
                    }],
                }],
                "transcript": {
                    "transcript_id": "str",
                    "state": "enum:off|active|processing|ready|failed",
                    "language": "str",
                    "segments": [{
                        "segment_id": "str",
                        "participant_id": "str",
                        "start_time": "str",
                        "end_time": "str",
                        "text": "str",
                        "language": "str",
                        "final": "bool",
                    }],
                },
                "summary": {
                    "summary_id": "str",
                    "state": "enum:off|processing|ready|failed",
                    "coverage_start": "str",
                    "coverage_end": "str",
                    "title": "str",
                    "content": "str",
                    "created_at": "str",
                    "updated_at": "str",
                    "default_recipients": "list",
                },
            },
            "settings": {
                "auto_join_meetings": "bool",
                "auto_join_countdown_seconds": "int",
                "auto_record": "bool",
                "auto_transcribe": "bool",
                "auto_share_screen": "bool",
                "join_audio_on_start": "bool",
                "auto_share_summary": "enum:all_participants|host_only|off",
            },
        },
        "fields": {
            "meeting.meeting_id": {
                "source": "compiler", "meaning": "Stable meeting identifier.",
                "format": "stable_id",
            },
            "meeting.topic": {
                "source": "model", "meaning": "Natural meeting topic.",
                "format": "plain_text",
            },
            "meeting.host": {
                "source": "model",
                "meaning": "Host identity independent of the principal.",
                "format": "person_name",
            },
            "meeting.scheduled_start": {
                "source": "timeplan", "meaning": "Scheduled start.",
                "format": "iso8601",
            },
            "meeting.scheduled_end": {
                "source": "timeplan", "meaning": "Scheduled end.",
                "format": "iso8601",
            },
            "meeting.invitation_status": {
                "source": "model", "meaning": "Invitation lifecycle state.",
                "format": "enum", "default": "accepted",
            },
            "meeting.local_participant_status": {
                "source": "model", "meaning": "Local participant state.",
                "format": "enum", "default": "joined",
            },
            "meeting.conference_status": {
                "source": "model", "meaning": "Conference lifecycle state.",
                "format": "enum", "default": "in_progress",
            },
            "meeting.recording_status": {
                "source": "model", "meaning": "Recording lifecycle state.",
                "format": "enum", "default": "off",
            },
            "meeting.transcription_status": {
                "source": "model", "meaning": "Transcription lifecycle state.",
                "format": "enum", "default": "ready",
            },
            "meeting.participants[].participant_id": {
                "source": "compiler", "meaning": "Stable participant ID.",
                "format": "stable_id",
            },
            "meeting.participants[].display_name": {
                "source": "model", "meaning": "Participant display name.",
                "format": "person_name",
            },
            "meeting.participants[].email": {
                "source": "model", "meaning": "Optional participant email.",
                "format": "email_address", "default": "",
            },
            "meeting.participants[].sessions[].joined_at": {
                "source": "timeplan", "meaning": "Session join time.",
                "format": "iso8601",
            },
            "meeting.participants[].sessions[].left_at": {
                "source": "timeplan", "meaning": "Session leave time.",
                "format": "iso8601", "default": "",
            },
            "meeting.transcript.transcript_id": {
                "source": "compiler", "meaning": "Stable transcript ID.",
                "format": "stable_id",
            },
            "meeting.transcript.state": {
                "source": "model", "meaning": "Transcript lifecycle state.",
                "format": "enum", "default": "ready",
            },
            "meeting.transcript.language": {
                "source": "model", "meaning": "Default transcript language.",
                "format": "plain_text", "default": "en",
            },
            "meeting.transcript.segments[].segment_id": {
                "source": "compiler", "meaning": "Stable segment ID.",
                "format": "stable_id",
            },
            "meeting.transcript.segments[].participant_id": {
                "source": "model", "meaning": "Participant reference.",
                "format": "stable_id", "default": "",
            },
            "meeting.transcript.segments[].start_time": {
                "source": "timeplan", "meaning": "Segment start.",
                "format": "iso8601",
            },
            "meeting.transcript.segments[].end_time": {
                "source": "timeplan", "meaning": "Segment end.",
                "format": "iso8601",
            },
            "meeting.transcript.segments[].text": {
                "source": "model", "meaning": "Transcript text.",
                "format": "plain_text",
            },
            "meeting.transcript.segments[].language": {
                "source": "model", "meaning": "Segment language.",
                "format": "plain_text", "default": "en",
            },
            "meeting.transcript.segments[].final": {
                "source": "model", "meaning": "Whether the segment is final.",
                "format": "boolean", "default": True,
            },
            "meeting.summary.summary_id": {
                "source": "compiler", "meaning": "Stable summary ID.",
                "format": "stable_id",
            },
            "meeting.summary.state": {
                "source": "model", "meaning": "Summary lifecycle state.",
                "format": "enum", "default": "off",
            },
            "meeting.summary.coverage_start": {
                "source": "timeplan", "meaning": "Summary coverage start.",
                "format": "iso8601", "default": "",
            },
            "meeting.summary.coverage_end": {
                "source": "timeplan", "meaning": "Summary coverage end.",
                "format": "iso8601", "default": "",
            },
            "meeting.summary.title": {
                "source": "model", "meaning": "Summary title.",
                "format": "plain_text", "default": "",
            },
            "meeting.summary.content": {
                "source": "model", "meaning": "Summary Markdown content.",
                "format": "markdown", "default": "",
            },
            "meeting.summary.created_at": {
                "source": "timeplan", "meaning": "Summary creation time.",
                "format": "iso8601", "default": "",
            },
            "meeting.summary.updated_at": {
                "source": "timeplan", "meaning": "Summary update time.",
                "format": "iso8601", "default": "",
            },
            "meeting.summary.default_recipients": {
                "source": "model", "meaning": "Default summary recipients.",
                "format": "email_list", "default": [],
            },
            "settings.auto_join_meetings": {
                "source": "model", "meaning": "Auto-join extension setting.",
                "format": "boolean", "default": True,
            },
            "settings.auto_join_countdown_seconds": {
                "source": "model", "meaning": "Auto-join delay.",
                "format": "positive_integer", "default": 10,
            },
            "settings.auto_record": {
                "source": "model", "meaning": "Auto-record extension setting.",
                "format": "boolean", "default": True,
            },
            "settings.auto_transcribe": {
                "source": "model", "meaning": "Auto-transcribe setting.",
                "format": "boolean", "default": True,
            },
            "settings.auto_share_screen": {
                "source": "model", "meaning": "Screen-share extension setting.",
                "format": "boolean", "default": False,
            },
            "settings.join_audio_on_start": {
                "source": "model", "meaning": "Audio extension setting.",
                "format": "boolean", "default": True,
            },
            "settings.auto_share_summary": {
                "source": "model", "meaning": "Summary audience setting.",
                "format": "enum", "default": "all_participants",
            },
        },
        "materialization_slots": {
            "meeting_record": {
                "path": "meeting",
                "material_kinds": ["meeting_record", "meeting", "transcript"],
                "description": (
                    "Meeting lifecycle, participants, transcript and summary."
                ),
                "compiler_fields": [
                    "meeting.meeting_id",
                    "meeting.participants[].participant_id",
                    "meeting.transcript.transcript_id",
                    "meeting.transcript.segments[].segment_id",
                    "meeting.summary.summary_id",
                ],
                "cardinality": "singleton",
                "bulk_generator": "fixture_values_v1",
            },
            "meeting_settings": {
                "path": "settings",
                "material_kinds": ["meeting_settings", "service_settings"],
                "description": "Optional Meeting extension settings.",
                "cardinality": "singleton",
            },
        },
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="zoom")
        self._register_standard_routes()
        self._register_extension_routes()

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        raw = data.get("meeting")
        if not isinstance(raw, dict):
            raise ValueError("Meeting fixture requires a canonical 'meeting' object")
        self.user_name = str(data.get("user_name") or "User")
        self.meeting = {
            "meeting_id": str(raw.get("meeting_id") or self.new_id("meeting")),
            "topic": str(raw.get("topic") or "(no meeting)"),
            "host": str(raw.get("host") or "Unknown"),
            "scheduled_start": str(raw.get("scheduled_start") or self.now_iso()),
            "scheduled_end": str(raw.get("scheduled_end") or self.now_iso()),
        }
        self.invitation_status = str(
            raw.get("invitation_status") or "accepted"
        )
        self.local_participant_status = str(
            raw.get("local_participant_status") or "joined"
        )
        self.conference_status = str(
            raw.get("conference_status") or "in_progress"
        )
        self.recording_status = str(raw.get("recording_status") or "off")
        self.transcription_status = str(
            raw.get("transcription_status") or "ready"
        )
        self.participants = [
            self._participant(dict(item))
            for item in (raw.get("participants") or [])
        ]
        transcript = raw.get("transcript") or {}
        self.transcript_id = str(
            transcript.get("transcript_id") or self.new_id("transcript")
        )
        self.transcript_state = str(transcript.get("state") or "off")
        self.transcript_language = str(
            transcript.get("language") or "en"
        )
        self.transcript_segments = [
            self._segment(dict(item))
            for item in (transcript.get("segments") or [])
        ]
        summary = raw.get("summary") or {}
        now = self.now_iso()
        self.summary = {
            "summary_id": str(
                summary.get("summary_id") or self.new_id("summary")
            ),
            "state": str(summary.get("state") or "off"),
            "coverage_start": str(summary.get("coverage_start") or ""),
            "coverage_end": str(summary.get("coverage_end") or ""),
            "title": str(summary.get("title") or ""),
            "content": str(summary.get("content") or ""),
            "created_at": str(summary.get("created_at") or now),
            "updated_at": str(summary.get("updated_at") or now),
            "default_recipients": self._addresses(
                summary.get("default_recipients") or []
            ),
        }
        self.settings = dict(_DEFAULT_SETTINGS)
        for key, value in (data.get("settings") or {}).items():
            if key in self.settings:
                self.settings[key] = _setting_value(key, value)

    def _participant(self, raw: dict) -> dict:
        sessions = []
        for item in raw.get("sessions") or []:
            sessions.append({
                "joined_at": str(item.get("joined_at") or self.now_iso()),
                "left_at": str(item.get("left_at") or ""),
            })
        return {
            "participant_id": str(
                raw.get("participant_id") or self.new_id("participant")
            ),
            "display_name": str(raw.get("display_name") or "Participant"),
            "email": str(raw.get("email") or "").strip().lower(),
            "sessions": sessions,
        }

    def _segment(self, raw: dict) -> dict:
        return {
            "segment_id": str(
                raw.get("segment_id") or self.new_id("segment")
            ),
            "participant_id": str(raw.get("participant_id") or ""),
            "start_time": str(raw.get("start_time") or self.now_iso()),
            "end_time": str(
                raw.get("end_time")
                or raw.get("start_time")
                or self.now_iso()
            ),
            "text": str(raw.get("text") or ""),
            "language": str(raw.get("language") or self.transcript_language),
            "final": bool(raw.get("final", True)),
        }

    @staticmethod
    def _addresses(raw: list[Any]) -> list[str]:
        if not isinstance(raw, list):
            raise ValueError("default_recipients must be a list")
        values = []
        for item in raw:
            address = str(item).strip().lower()
            if not address or "@" not in address:
                raise ValueError(f"invalid recipient: {item!r}")
            if address not in values:
                values.append(address)
        return values

    def _meeting_view(self) -> dict:
        return {
            **self.meeting,
            "invitation_status": self.invitation_status,
            "local_participant_status": self.local_participant_status,
            "conference_status": self.conference_status,
            "recording_status": self.recording_status,
            "transcription_status": self.transcription_status,
        }

    def get_state(self) -> dict:
        return {
            "meeting": self._meeting_view(),
            "participants": self.participants,
            "transcript": {
                "transcript_id": self.transcript_id,
                "state": self.transcript_state,
                "language": self.transcript_language,
                "segments": self.transcript_segments,
            },
            "summary": dict(self.summary),
            "settings": dict(self.settings),
        }

    def inject(self, content: dict) -> dict:
        action = str(content.get("action") or content.get("type") or "")
        if action in {"end_ringing", "ringing_timed_out"}:
            action = "conference_end"
        if action == "conference_start":
            self.conference_status = "in_progress"
            if self.invitation_status == "accepted":
                self.local_participant_status = "joined"
            return self._environment_result(action, {
                "conference_status": self.conference_status,
                "local_participant_status": self.local_participant_status,
            })
        if action == "conference_end":
            self.conference_status = "ended"
            if self.recording_status == "active":
                self.recording_status = "processing"
            if self.transcription_status == "active":
                self.transcription_status = "processing"
            return self._environment_result(
                action, {"conference_status": "ended"}
            )
        if action in {"participant_join", "participant_leave"}:
            participant_id = str(content.get("participant_id") or "")
            participant = next(
                (
                    item for item in self.participants
                    if item["participant_id"] == participant_id
                ),
                None,
            )
            if participant is None:
                return {"ok": False, "error": "participant not found"}
            timestamp = str(content.get("timestamp") or self.now_iso())
            if action == "participant_join":
                participant["sessions"].append({
                    "joined_at": timestamp, "left_at": "",
                })
            else:
                active = next(
                    (
                        item for item in reversed(participant["sessions"])
                        if not item["left_at"]
                    ),
                    None,
                )
                if active:
                    active["left_at"] = timestamp
            return self._environment_result(
                action, {"participant_id": participant_id},
                participant=participant,
            )
        if action == "append_transcript":
            for item in content.get("segments") or []:
                self.transcript_segments.append(self._segment(dict(item)))
            self.transcript_state = "ready"
            return self._environment_result(
                action, {"segment_count": len(self.transcript_segments)}
            )
        if action == "summary_ready":
            self.summary.update({
                "state": "ready",
                "coverage_start": str(
                    content.get("coverage_start")
                    or self.summary["coverage_start"]
                ),
                "coverage_end": str(
                    content.get("coverage_end")
                    or self.summary["coverage_end"]
                ),
                "title": str(content.get("title") or self.summary["title"]),
                "content": str(
                    content.get("content") or self.summary["content"]
                ),
                "updated_at": self.now_iso(),
            })
            return self._environment_result(
                action, {"summary_id": self.summary["summary_id"]}
            )
        if action == "recording_ready":
            self.recording_status = "ready"
            return {"ok": True, "recording_status": "ready"}
        if action == "transcription_ready":
            self.transcription_status = "ready"
            return {"ok": True, "transcription_status": "ready"}
        return {"ok": False, "error": "unsupported inject action"}

    def _environment_result(
        self,
        action: str,
        effect: dict[str, Any],
        **payload: Any,
    ) -> dict:
        self.log_action(action, source="environment", effect=effect)
        return {"ok": True, **effect, **payload}

    def _require_active(self, action: str):
        if (
            self.conference_status != "in_progress"
            or self.local_participant_status != "joined"
        ):
            self.log_action(action, params={}, status=409)
            return self.error_response(
                code="MEETING_NOT_ACTIVE",
                message="The local participant must be in an active meeting.",
                status=409,
            )
        return None

    def _register_standard_routes(self) -> None:
        svc = self

        @self.router.post("/meeting/get", operation_id="get_meeting")
        def get_meeting():
            svc.log_action("get_meeting", params={})
            return {"meeting": svc._meeting_view()}

        @self.router.post(
            "/invitation/accept", operation_id="accept_invitation"
        )
        def accept_invitation():
            changed = svc.invitation_status != "accepted"
            svc.invitation_status = "accepted"
            if svc.conference_status == "in_progress":
                svc.local_participant_status = "joined"
            svc.log_action(
                "accept_invitation",
                params={},
                effect={"invitation_status": "accepted"},
                no_op=not changed,
            )
            return {"meeting": svc._meeting_view()}

        @self.router.post(
            "/invitation/decline", operation_id="decline_invitation"
        )
        def decline_invitation():
            if svc.local_participant_status == "joined":
                svc.log_action(
                    "decline_invitation", params={}, status=409
                )
                return svc.error_response(
                    code="ALREADY_JOINED",
                    message="Cannot decline after joining.",
                    status=409,
                )
            changed = svc.invitation_status != "declined"
            svc.invitation_status = "declined"
            svc.log_action(
                "decline_invitation",
                params={},
                effect={"invitation_status": "declined"},
                no_op=not changed,
            )
            return {"meeting": svc._meeting_view()}

        @self.router.post("/meeting/leave", operation_id="leave_meeting")
        def leave_meeting():
            if svc.local_participant_status != "joined":
                svc.log_action("leave_meeting", params={}, status=409)
                return svc.error_response(
                    code="NOT_JOINED",
                    message="The local participant is not joined.",
                    status=409,
                )
            svc.local_participant_status = "left"
            svc.log_action(
                "leave_meeting",
                params={},
                effect={"local_participant_status": "left"},
            )
            return {"meeting": svc._meeting_view()}

        @self.router.post(
            "/participants/list", operation_id="list_participants"
        )
        def list_participants():
            svc.log_action(
                "list_participants",
                params={},
                participant_count=len(svc.participants),
            )
            return {"participants": svc.participants}

        @self.router.post(
            "/recording/start", operation_id="start_recording"
        )
        def start_recording():
            error = svc._require_active("start_recording")
            if error:
                return error
            changed = svc.recording_status != "active"
            svc.recording_status = "active"
            svc.log_action(
                "start_recording",
                params={},
                effect={"recording_status": "active"},
                no_op=not changed,
            )
            return {"recording_status": svc.recording_status}

        @self.router.post(
            "/recording/stop", operation_id="stop_recording"
        )
        def stop_recording():
            if svc.recording_status != "active":
                svc.log_action("stop_recording", params={}, status=409)
                return svc.error_response(
                    code="RECORDING_NOT_ACTIVE",
                    message="Recording is not active.",
                    status=409,
                )
            svc.recording_status = "processing"
            svc.log_action(
                "stop_recording",
                params={},
                effect={"recording_status": "processing"},
            )
            return {"recording_status": "processing"}

        @self.router.post(
            "/transcription/start", operation_id="start_transcription"
        )
        def start_transcription():
            error = svc._require_active("start_transcription")
            if error:
                return error
            changed = svc.transcription_status != "active"
            svc.transcription_status = "active"
            svc.log_action(
                "start_transcription",
                params={},
                effect={"transcription_status": "active"},
                no_op=not changed,
            )
            return {"transcription_status": "active"}

        @self.router.post(
            "/transcription/stop", operation_id="stop_transcription"
        )
        def stop_transcription():
            if svc.transcription_status != "active":
                svc.log_action(
                    "stop_transcription", params={}, status=409
                )
                return svc.error_response(
                    code="TRANSCRIPTION_NOT_ACTIVE",
                    message="Transcription is not active.",
                    status=409,
                )
            svc.transcription_status = "processing"
            svc.log_action(
                "stop_transcription",
                params={},
                effect={"transcription_status": "processing"},
            )
            return {"transcription_status": "processing"}

        @self.router.post(
            "/transcript/get", operation_id="get_transcript"
        )
        def get_transcript(req: TranscriptQueryRequest):
            params = req.model_dump()
            segments = [
                item for item in svc.transcript_segments
                if (
                    not req.start_time
                    or item["end_time"] >= req.start_time
                )
                and (
                    not req.end_time
                    or item["start_time"] < req.end_time
                )
            ]
            try:
                offset = int(req.cursor or "0")
            except ValueError:
                offset = -1
            if offset < 0:
                svc.log_action(
                    "get_transcript", params=params, status=422
                )
                return svc.error_response(
                    code="INVALID_CURSOR",
                    message="cursor must be a non-negative integer offset.",
                    status=422,
                )
            window = segments[offset:offset + req.limit]
            next_cursor = (
                str(offset + len(window))
                if offset + len(window) < len(segments)
                else None
            )
            svc.log_action(
                "get_transcript",
                params=params,
                returned=len(window),
                total_matches=len(segments),
            )
            return {
                "transcript_id": svc.transcript_id,
                "state": svc.transcript_state,
                "segments": window,
                "next_cursor": next_cursor,
                "total_matches": len(segments),
            }

        @self.router.post("/summary/get", operation_id="get_summary")
        def get_summary():
            if svc.summary["state"] != "ready":
                svc.log_action("get_summary", params={}, status=409)
                return svc.error_response(
                    code="SUMMARY_NOT_READY",
                    message="Meeting summary is not ready.",
                    status=409,
                )
            svc.log_action("get_summary", params={})
            return {"summary": svc.summary}

        @self.router.post(
            "/summary/email_draft",
            operation_id="create_summary_email_draft",
        )
        def create_summary_email_draft():
            if svc.summary["state"] != "ready":
                svc.log_action(
                    "create_summary_email_draft", params={}, status=409
                )
                return svc.error_response(
                    code="SUMMARY_NOT_READY",
                    message="Meeting summary is not ready.",
                    status=409,
                )
            recipients = list(svc.summary["default_recipients"])
            if not recipients:
                svc.log_action(
                    "create_summary_email_draft", params={}, status=409
                )
                return svc.error_response(
                    code="RECIPIENTS_UNRESOLVED",
                    message="Summary recipients could not be resolved.",
                    status=409,
                )
            email_service = svc.resolve_service("email_client")
            create_draft = getattr(
                email_service, "create_draft_record", None
            )
            if not callable(create_draft):
                svc.log_action(
                    "create_summary_email_draft", params={}, status=503
                )
                return svc.error_response(
                    code="EMAIL_SERVICE_UNAVAILABLE",
                    message="A standard Email service is not mounted.",
                    status=503,
                )
            draft = create_draft(
                to=recipients,
                subject=svc.summary["title"],
                body=svc.summary["content"],
                source=f"service:{svc.template}",
            )
            svc.log_action(
                "create_summary_email_draft",
                params={},
                effect={
                    "summary_id": svc.summary["summary_id"],
                    "email_draft_id": draft["id"],
                },
            )
            return {
                "summary_id": svc.summary["summary_id"],
                "email_draft_id": draft["id"],
                "recipients": recipients,
            }

    def _register_extension_routes(self) -> None:
        svc = self

        @self.router.post("/desktop", operation_id="view_desktop")
        def view_desktop():
            svc.log_action("view_desktop", params={})
            return {"app_title": "Meeting", **svc.get_state()}

        @self.router.post("/join", operation_id="join_meeting")
        def join_meeting():
            svc.invitation_status = "accepted"
            svc.conference_status = "in_progress"
            svc.local_participant_status = "joined"
            if svc.settings["auto_record"]:
                svc.recording_status = "active"
            if svc.settings["auto_transcribe"]:
                svc.transcription_status = "active"
            svc.log_action("join_meeting", params={})
            return {
                "state": "in_meeting",
                "recording": svc.recording_status == "active",
                "transcribing": svc.transcription_status == "active",
            }

        @self.router.post("/decline", operation_id="decline_meeting")
        def decline_meeting():
            if svc.local_participant_status == "joined":
                return svc.error_response(
                    code="ALREADY_JOINED",
                    message="Cannot decline after joining.",
                    status=409,
                )
            svc.invitation_status = "declined"
            svc.log_action("decline_meeting", params={})
            return {"state": "declined"}

        @self.router.post(
            "/recording/continue", operation_id="continue_recording"
        )
        def continue_recording():
            if svc.recording_status != "active":
                svc.log_action(
                    "continue_recording", params={}, status=409
                )
                return svc.error_response(
                    code="RECORDING_NOT_ACTIVE",
                    message="Recording is not active.",
                    status=409,
                )
            svc.log_action("continue_recording", params={})
            return {
                "recording_status": "active",
                "participants": svc.participants,
            }

        @self.router.post(
            "/summary/email", operation_id="open_summary_in_email"
        )
        def open_summary_in_email():
            if svc.summary["state"] != "ready":
                return svc.error_response(
                    code="SUMMARY_NOT_READY",
                    message="Meeting summary is not ready.",
                    status=409,
                )
            svc.log_action("open_summary_in_email", params={})
            return {
                "compose_url": "http://localhost:5001/compose",
                "to": list(svc.summary["default_recipients"]),
                "subject": svc.summary["title"],
                "body": svc.summary["content"],
            }

        @self.router.post("/settings", operation_id="view_settings")
        def view_settings():
            svc.log_action("view_settings", params={})
            return {"settings": dict(svc.settings)}

        @self.router.post(
            "/settings/toggle", operation_id="toggle_setting"
        )
        def toggle_setting(req: ToggleSettingRequest):
            key = str(req.key or "")
            if key not in svc.settings:
                return svc.error_response(
                    code="UNKNOWN_SETTING",
                    message=f"Unknown setting: {key}",
                    status=422,
                )
            old = svc.settings[key]
            new = (
                _setting_value(key, req.value)
                if req.value is not None
                else not bool(old)
            )
            svc.settings[key] = new
            svc.log_action("toggle_setting", params=req.model_dump())
            return {"key": key, "old": old, "new": new}

        @self.router.post(
            "/settings/update", operation_id="api_set_settings"
        )
        def api_set_settings(req: SettingsUpdateRequest):
            changed = {}
            for key, value in req.settings.items():
                if key not in svc.settings:
                    continue
                old = svc.settings[key]
                new = _setting_value(key, value)
                if old != new:
                    svc.settings[key] = new
                    changed[key] = {"old": old, "new": new}
            svc.log_action(
                "api_set_settings",
                params=req.model_dump(),
                effect={"changed": changed},
                no_op=not changed,
            )
            return {"settings": dict(svc.settings), "changed": changed}

        @self.router.post("/end_ringing", include_in_schema=False)
        def ringing_timed_out():
            return svc.inject({"action": "ringing_timed_out"})


def create_service(service_id: str) -> ZoomService:
    return ZoomService(service_id)
