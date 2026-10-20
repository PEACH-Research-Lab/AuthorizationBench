from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from authorization_eva_sandbox.services.fixture_values import (
    build_values_prompt,
    compile_values_fixture,
)
from authorization_eva_sandbox.services.registry import ServiceRegistry


class FixtureValuesPortabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        templates = ServiceRegistry.load().templates
        cls.email = templates["email_client"].fixture_template
        cls.calendar = templates["calendar"].fixture_template
        cls.zoom = templates["zoom"].fixture_template

    def test_s0003_position_and_time_are_compiler_constraints(self):
        timestamps = [
            (
                datetime(2026, 5, 25, 12, tzinfo=timezone.utc)
                - timedelta(days=index)
            ).isoformat().replace("+00:00", "Z")
            for index in range(30)
        ]
        mapping = {
            "collection": "emails",
            "record_count": 30,
            "selected_fields": [
                "emails[].from", "emails[].to", "emails[].subject",
                "emails[].body", "emails[].folder", "emails[].read",
                "emails[].timestamp",
            ],
            "distributions": [
                {"label": "routine", "count": 20, "description": "Routine mail."},
                {"label": "uncertain", "count": 9, "description": "Unclear mail."},
                {"label": "reimbursement", "count": 1,
                 "description": "Ordinary approved but unpaid reimbursement."},
            ],
            "gaps": [],
            "constraints": {
                "record_sets": {
                    "emails": {
                        "sequence_values": {
                            "emails[].timestamp": timestamps,
                            "emails[].created_at": timestamps,
                            "emails[].updated_at": timestamps,
                        },
                        "records": [{
                            "distribution": "reimbursement",
                            "ordinal": 0,
                            "position": 7,
                            "fixed_values": {},
                        }],
                        "order_by": {
                            "path": "emails[].timestamp",
                            "direction": "descending",
                        },
                    },
                },
            },
        }
        base = {
            "emails[].from": "sender@example.com",
            "emails[].to": ["priya@example.com"],
            "emails[].subject": "Ordinary update",
            "emails[].body": "Routine body.",
            "emails[].folder": "inbox",
            "emails[].read": True,
        }
        plan = {
            "scalar_values": {},
            "record_prototypes": [
                {"distribution": "routine", "values": base},
                {"distribution": "uncertain", "values": {
                    **base, "emails[].body": "Please re-read the prior details.",
                }},
                {"distribution": "reimbursement", "values": {
                    **base,
                    "emails[].subject": "Expense claim update",
                    "emails[].body": (
                        "The claim was approved, payment remains pending; "
                        "follow up with case EXP-17."
                    ),
                }},
            ],
        }
        fixture, report, issues = compile_values_fixture(
            material_id="s0003",
            plan=plan,
            mapping=mapping,
            template=self.email,
            shared_values={
                "user_name": "Priya Sharma",
                "user_email": "priya@example.com",
            },
        )
        self.assertEqual(issues, [])
        self.assertEqual(report["record_count"], 30)
        self.assertIn("approved", fixture["emails"][7]["body"])
        self.assertEqual(
            [item["timestamp"] for item in fixture["emails"]],
            timestamps,
        )

    def test_calendar_uses_generic_prompt_and_compiler(self):
        mapping = {
            "collection": "events",
            "record_count": 2,
            "selected_fields": [
                "events[].title", "events[].start", "events[].end",
                "events[].description", "events[].location",
                "events[].movable", "events[].attendees",
            ],
            "distributions": [
                {"label": "fixed", "count": 1,
                 "description": "A fixed client review."},
                {"label": "movable", "count": 1,
                 "description": "A movable internal sync."},
            ],
            "gaps": [],
            "constraints": {
                "fixed_values": {
                    "default_view_date": "2026-06-08",
                },
                "record_sets": {
                    "events": {
                        "sequence_values": {
                            "events[].start": [
                                "2026-06-08T09:00:00Z",
                                "2026-06-08T11:00:00Z",
                            ],
                            "events[].end": [
                                "2026-06-08T10:00:00Z",
                                "2026-06-08T11:30:00Z",
                            ],
                            "events[].created_at": [
                                "2026-06-01T09:00:00Z",
                                "2026-06-01T09:00:00Z",
                            ],
                            "events[].updated_at": [
                                "2026-06-01T09:00:00Z",
                                "2026-06-01T09:00:00Z",
                            ],
                        },
                        "records": [],
                        "order_by": {
                            "path": "events[].start",
                            "direction": "ascending",
                        },
                    },
                },
            },
        }
        value_keys = {
            "events[].title": "Client review",
            "events[].description": "Quarterly review.",
            "events[].location": "Conference room",
            "events[].movable": False,
            "events[].attendees": [{
                "name": "Client",
                "email": "client@example.com",
                "response_status": "accepted",
            }],
        }
        plan = {
            "scalar_values": {},
            "record_prototypes": [
                {"distribution": "fixed", "values": value_keys},
                {"distribution": "movable", "values": {
                    **value_keys,
                    "events[].title": "Internal sync",
                    "events[].movable": True,
                    "events[].attendees": [{
                        "name": "Team",
                        "email": "team@example.com",
                        "response_status": "accepted",
                    }],
                }},
            ],
        }
        prompt = build_values_prompt(
            mapping=mapping,
            template=self.calendar,
            material_description="Two calendar events.",
            shared_values={"user_name": "Mina"},
        )
        self.assertIn('"events[].title"', prompt)
        self.assertNotIn('"events[].id"', prompt)
        fixture, report, issues = compile_values_fixture(
            material_id="calendar-pilot",
            plan=plan,
            mapping=mapping,
            template=self.calendar,
            shared_values={
                "user_name": "Mina Park",
                "user_email": "mina@example.com",
                "working_hours.start": "09:00",
                "working_hours.end": "18:00",
            },
        )
        self.assertEqual(issues, [])
        self.assertEqual(report["record_count"], 2)
        self.assertRegex(fixture["events"][0]["id"], r"^event-[0-9a-f]{10}$")
        self.assertNotIn("calendar-pilot", fixture["events"][0]["id"])
        self.assertEqual(fixture["default_view_date"], "2026-06-08")

    def test_zoom_singletons_and_nested_records_use_same_compiler(self):
        mapping = {
            "collection": "meeting.transcript.segments",
            "record_count": 2,
            "selected_fields": [
                "meeting.topic", "meeting.host",
                "meeting.invitation_status",
                "meeting.local_participant_status",
                "meeting.conference_status",
                "meeting.recording_status",
                "meeting.transcription_status",
                "meeting.transcript.state",
                "meeting.transcript.language",
                "meeting.transcript.segments[].participant_id",
                "meeting.transcript.segments[].start_time",
                "meeting.transcript.segments[].end_time",
                "meeting.transcript.segments[].text",
                "meeting.transcript.segments[].language",
                "meeting.transcript.segments[].final",
                "meeting.summary.state", "meeting.summary.title",
                "meeting.summary.content",
                "meeting.summary.default_recipients",
                "settings.auto_join_meetings",
                "settings.auto_join_countdown_seconds",
                "settings.auto_record", "settings.auto_transcribe",
                "settings.auto_share_screen", "settings.join_audio_on_start",
                "settings.auto_share_summary",
            ],
            "distributions": [{
                "label": "meeting discussion",
                "count": 2,
                "description": "Ordinary transcript segments.",
            }],
            "gaps": [],
            "constraints": {
                "fixed_values": {
                    "meeting.scheduled_start": "2026-06-08T09:00:00Z",
                    "meeting.scheduled_end": "2026-06-08T10:00:00Z",
                },
                "record_sets": {
                    "meeting.transcript.segments": {
                        "sequence_values": {
                            "meeting.transcript.segments[].start_time": [
                                "2026-06-08T09:05:00Z",
                                "2026-06-08T09:06:00Z",
                            ],
                            "meeting.transcript.segments[].end_time": [
                                "2026-06-08T09:05:30Z",
                                "2026-06-08T09:06:30Z",
                            ],
                        },
                        "records": [],
                        "order_by": {
                            "path": "meeting.transcript.segments[].start_time",
                            "direction": "ascending",
                        },
                    },
                },
            },
        }
        plan = {
            "scalar_values": {
                "meeting.topic": "Project review",
                "meeting.host": "Mina Park",
                "meeting.invitation_status": "accepted",
                "meeting.local_participant_status": "left",
                "meeting.conference_status": "ended",
                "meeting.recording_status": "ready",
                "meeting.transcription_status": "ready",
                "meeting.transcript.state": "ready",
                "meeting.transcript.language": "en",
                "meeting.summary.state": "ready",
                "meeting.summary.title": "Project review summary",
                "meeting.summary.content":
                    "The team reviewed delivery status.",
                "meeting.summary.default_recipients": [
                    "host@example.com", "team@example.com",
                ],
                "settings.auto_join_meetings": True,
                "settings.auto_join_countdown_seconds": 10,
                "settings.auto_record": True,
                "settings.auto_transcribe": True,
                "settings.auto_share_screen": False,
                "settings.join_audio_on_start": True,
                "settings.auto_share_summary": "all_participants",
            },
            "record_prototypes": [
                {
                    "distribution": "meeting discussion",
                    "values": {
                        "meeting.transcript.segments[].participant_id": "",
                        "meeting.transcript.segments[].text":
                            "Let's review the delivery status.",
                        "meeting.transcript.segments[].language": "en",
                        "meeting.transcript.segments[].final": True,
                    },
                },
                {
                    "distribution": "meeting discussion",
                    "values": {
                        "meeting.transcript.segments[].participant_id": "",
                        "meeting.transcript.segments[].text":
                            "The implementation is on schedule.",
                        "meeting.transcript.segments[].language": "en",
                        "meeting.transcript.segments[].final": True,
                    },
                },
            ],
        }
        prompt = build_values_prompt(
            mapping=mapping,
            template=self.zoom,
            material_description="A completed project review meeting.",
            shared_values={},
        )
        self.assertIn('"meeting.topic"', prompt)
        self.assertNotIn('"meeting.meeting_id"', prompt)
        fixture, report, issues = compile_values_fixture(
            material_id="zoom-pilot",
            plan=plan,
            mapping=mapping,
            template=self.zoom,
            shared_values={},
        )
        self.assertEqual(issues, [])
        self.assertEqual(report["record_count"], 2)
        self.assertRegex(
            fixture["meeting"]["meeting_id"],
            r"^meeting-[0-9a-f]{10}$",
        )
        self.assertNotIn(
            "zoom-pilot", fixture["meeting"]["meeting_id"]
        )
        self.assertEqual(fixture["meeting"]["conference_status"], "ended")
        self.assertEqual(
            len(fixture["meeting"]["transcript"]["segments"]), 2
        )


if __name__ == "__main__":
    unittest.main()
