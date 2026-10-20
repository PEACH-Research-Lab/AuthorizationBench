"""The MCP allowlist is the business surface the agent is allowed to see."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from authorization_eva_sandbox.services.multi_server import create_app
from authorization_eva_sandbox.services.zoom.service import TranscriptQueryRequest
from authorization_eva_sandbox.evaluation.harness.tool_ir import (
    allowlist_from_actions,
    render_tool_notice,
    select_allowlisted_tools,
)


_INFRA = ("/state", "/audit", "/reset", "/health", "/inject")


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        app = create_app([{"id": "calendar", "template": "calendar"}])
        self.openapi = app.openapi()

    def test_selection_is_exactly_the_requested_names(self):
        names = ["calendar.list_events", "message_user"]
        selected = select_allowlisted_tools(self.openapi, names)
        self.assertEqual([tool["name"] for tool in selected], names)
        for tool in selected:
            self.assertFalse(tool["endpoint"].startswith("/_env"))
            self.assertFalse(any(tool["endpoint"].endswith(suffix) for suffix in _INFRA))

    def test_event_id_description_names_its_lookup(self):
        selected = select_allowlisted_tools(self.openapi, ["calendar.get_event"])
        text = selected[0]["parameters"]["properties"]["event_id"]["description"]
        self.assertIn("list_events", text)

    def test_private_route_cannot_be_named(self):
        with self.assertRaises(ValueError):
            select_allowlisted_tools(self.openapi, ["calendar.state"])
        with self.assertRaises(ValueError):
            select_allowlisted_tools(self.openapi, ["_env.reset"])


class BlueprintAllowlistTests(unittest.TestCase):
    def test_s0010_actions_gain_lookups_and_a_tool_notice(self):
        actions = [
            "injected:context",
            "zoom.get_transcript",
            "runtime:shell.read_file",
            "runtime:shell.write_file",
            "calendar.get_event",
            "email_client.send_message",
            "zoom.create_summary_email_draft",
            "email_client.send_draft",
        ]
        names, files = allowlist_from_actions(actions)
        self.assertEqual(names, [
            "zoom.get_meeting",
            "zoom.get_transcript",
            "calendar.list_events",
            "calendar.get_event",
            "email_client.send_message",
            "zoom.create_summary_email_draft",
            "email_client.list_messages",
            "email_client.send_draft",
            "message_user",
        ])
        self.assertTrue(files)
        notice = render_tool_notice(names, files=files)
        self.assertIn("zoom.get_transcript", notice)
        self.assertIn("read_file", notice)
        self.assertIn("/workspace", notice)
        self.assertNotIn("localhost", notice)
        self.assertNotIn("191", notice)
        self.assertIn("1 to 500", TranscriptQueryRequest.model_fields["limit"].description)

    def test_a_step_can_list_several_actions(self):
        names, files = allowlist_from_actions([
            ["runtime:shell.bash", "runtime:shell.read_file"],
            ["email_client.search_contacts_by_name", "email_client.send_message"],
        ])
        self.assertEqual(names, [
            "email_client.search_contacts_by_name",
            "email_client.send_message",
            "message_user",
        ])
        self.assertTrue(files)


if __name__ == "__main__":
    unittest.main()
