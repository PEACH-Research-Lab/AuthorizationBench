"""Identifier tool-name adapter: dots become underscores, then map back."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from authorization_eva_sandbox.evaluation.harness.openhands_events import (
    record_openhands_events,
)
from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import (
    ToolNameAdapter,
    adapter_for_model,
    uses_identifier_tool_names,
)
from authorization_eva_sandbox.trajectory_logger import TrajectoryLog


NAMES = [
    "email_client.list_messages",
    "email_client.get_message",
    "email_client.bulk_delete_emails",
    "message_user",
]


class ToolNameAdapterTests(unittest.TestCase):
    def test_openai_models_publish_underscores_and_restore_dots(self):
        adapter = adapter_for_model("gpt-5.6-sol", NAMES)
        self.assertIsNotNone(adapter)
        self.assertEqual(
            adapter.publish("email_client.list_messages"),
            "email_client_list_messages",
        )
        self.assertEqual(
            adapter.canonical("email_client_list_messages"),
            "email_client.list_messages",
        )
        self.assertEqual(adapter.publish("message_user"), "message_user")

    def test_every_model_sees_the_same_identifier_names(self):
        for model in (
            "gpt-4.1",
            "gpt-5.6-sol",
            "openrouter/openai/gpt-4.1",
            "openrouter/anthropic/claude-sonnet-5",
            "openrouter/google/gemini-3.8-flash",
            "openrouter/z-ai/glm-5.3",
            "deepseek/deepseek-v4-flash",
        ):
            self.assertTrue(uses_identifier_tool_names(model), model)
            adapter = adapter_for_model(model, NAMES)
            self.assertEqual(adapter.publish("email_client.list_messages"),
                             "email_client_list_messages")

    def test_prompt_uses_the_published_names(self):
        adapter = ToolNameAdapter(NAMES)
        prompt = (
            "email_client.list_messages, email_client.get_message, "
            "email_client.bulk_delete_emails, message_user"
        )
        self.assertEqual(
            adapter.adapt_prompt(prompt),
            "email_client_list_messages, email_client_get_message, "
            "email_client_bulk_delete_emails, message_user",
        )

    def test_colliding_published_names_are_rejected(self):
        with self.assertRaises(ValueError):
            ToolNameAdapter(["email_client.list", "email.client_list"])

    def test_trajectory_records_the_canonical_name(self):
        adapter = ToolNameAdapter(NAMES)
        trajectory = TrajectoryLog(task_id="T0003", model="gpt-5.6-sol")
        action = SimpleNamespace(
            tool_name="email_client_list_messages",
            action=SimpleNamespace(data={"folder": "inbox"}),
        )
        observation = SimpleNamespace(
            tool_name="email_client_list_messages",
            observation=SimpleNamespace(
                content=[SimpleNamespace(text='{"status":200,"body":{}}')]
            ),
        )
        record_openhands_events(trajectory, [action, observation], tool_names=adapter)
        step = trajectory.steps[0]
        self.assertEqual(step.data["service"], "email_client")
        self.assertEqual(step.data["tool"], "list_messages")


if __name__ == "__main__":
    unittest.main()
