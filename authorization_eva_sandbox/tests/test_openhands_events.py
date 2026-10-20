"""SDK events land in the trajectory fields the judge renderer already reads."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from authorization_eva_sandbox.evaluation.harness.openhands_events import (
    record_openhands_events,
)
from authorization_eva_sandbox.trajectory_logger import TrajectoryLog


def _action(tool: str, data: dict):
    return SimpleNamespace(
        tool_name=tool,
        action=SimpleNamespace(data=data),
    )


def _observation(tool: str, text: str):
    return SimpleNamespace(
        tool_name=tool,
        observation=SimpleNamespace(content=[SimpleNamespace(text=text)]),
    )


class OpenHandsEventTests(unittest.TestCase):
    def test_tool_finish_and_message_user_map_to_judge_steps(self):
        trajectory = TrajectoryLog(task_id="step4", model="gemini")
        record_openhands_events(trajectory, [
            _action("calendar.list_events", {"page": 1, "page_size": 5}),
            _observation(
                "calendar.list_events",
                '{"status":200,"body":{"events":[]}}',
            ),
            _action("message_user", {"message": "Sent the minutes."}),
            _observation(
                "message_user",
                '{"status":200,"body":{"delivered":true,"responded":false,"reply":null}}',
            ),
            _action("finish", {"message": "done"}),
        ])
        kinds = [step.type for step in trajectory.steps]
        self.assertEqual(kinds, ["tool_call", "ask_user", "agent_finish"])
        self.assertEqual(trajectory.steps[0].data["tool"], "list_events")
        self.assertEqual(trajectory.steps[0].data["service"], "calendar")
        self.assertEqual(trajectory.steps[0].data["status"], 200)
        self.assertFalse(trajectory.steps[1].data["responded"])
        self.assertEqual(trajectory.steps[2].data["final_thought"], "done")

    def test_june_step_types_are_reused(self):
        trajectory = TrajectoryLog(task_id="T0001-b-e-h", model="gemini")
        record_openhands_events(trajectory, [
            SimpleNamespace(
                source="agent",
                llm_message=SimpleNamespace(content=[SimpleNamespace(text="Looking now.")]),
            ),
            SimpleNamespace(
                source="user",
                llm_message=SimpleNamespace(content=[SimpleNamespace(text="Use the cheaper one.")]),
            ),
            _action("think", {"thought": "The cheaper listing is in scope."}),
            _action("bash", {"command": "ls /workspace"}),
            _observation("bash", "notes.md"),
            _action("read_file", {"path": "/workspace/notes.md"}),
            _observation("read_file", "price: 4"),
            _action("browse_interactive", {"browser_actions": "click", "url": "https://shop.example"}),
            _action("run_ipython", {"code": "print(1)"}),
            _observation("run_ipython", "1"),
        ])
        self.assertEqual(
            [step.type for step in trajectory.steps],
            [
                "agent_message",
                "user_message",
                "agent_think",
                "terminal_command",
                "file_read",
                "browser_action",
                "ipython_cell",
                "ipython_output",
            ],
        )
        self.assertEqual(trajectory.steps[3].data["command"], "ls /workspace")
        self.assertEqual(trajectory.steps[3].data["output"], "notes.md")
        self.assertEqual(trajectory.steps[5].data["action"], "click")
        self.assertEqual(trajectory.steps[7].data["output"], "1")

    def test_think_observation_does_not_replace_the_next_tool_result(self):
        trajectory = TrajectoryLog(task_id="T0001-b-e-h", model="gemini")
        record_openhands_events(trajectory, [
            _action("think", {"thought": "The memory says to use the saved card."}),
            _action("shopping_app_instacart.place_order", {}),
            _observation("think", "Your thought has been logged."),
            _observation(
                "shopping_app_instacart.place_order",
                '{"status":200,"body":{"status":"order_placed","order":{"order_id":"ORDER-1"}}}',
            ),
        ])
        self.assertEqual(
            [step.type for step in trajectory.steps],
            ["agent_think", "tool_call"],
        )
        order = trajectory.steps[1].data
        self.assertEqual(order["tool"], "place_order")
        self.assertEqual(order["status"], 200)
        self.assertIn("ORDER-1", order["result"])
        self.assertNotIn("thought has been logged", order["result"])


class ParallelToolCallTests(unittest.TestCase):
    def test_parallel_reads_keep_their_own_path_and_result(self):
        def read(i, path):
            return SimpleNamespace(tool_name="read_file", id=f"act-{i}", tool_call_id=f"call-{i}",
                                   action=SimpleNamespace(data={"file_path": path}))

        def result(i, text):
            return SimpleNamespace(tool_name="read_file", action_id=f"act-{i}",
                                   tool_call_id=f"call-{i}",
                                   observation=SimpleNamespace(content=[SimpleNamespace(text=text)]))

        trajectory = TrajectoryLog(task_id="T0005-i-g-h", model="gpt-4.1")
        record_openhands_events(trajectory, [
            read(1, "/workspace/a.csv"), read(2, "/workspace/b.csv"), read(3, "/workspace/c.json"),
            result(2, "B rows"), result(1, "A rows"), result(3, "C export"),
            _action("email_client.send_message", {"to": ["x@y.z"]}),
            _observation("email_client.send_message", '{"status":200,"body":{}}'),
        ])
        reads = [s.data for s in trajectory.steps if s.type == "file_read"]
        self.assertEqual([(r["path"], r["result"]) for r in reads],
                         [("/workspace/a.csv", "A rows"), ("/workspace/b.csv", "B rows"),
                          ("/workspace/c.json", "C export")])
        self.assertEqual(trajectory.steps[-1].data["status"], 200)


class FileEditContentTests(unittest.TestCase):
    def test_write_file_records_the_new_file_text(self):
        trajectory = TrajectoryLog(task_id="T0028-j-b-h", model="gpt-5.6-sol")
        record_openhands_events(trajectory, [
            _action("write_file", {
                "file_path": "/workspace/report.md",
                "content": "# Panel\nConfidential finding\n",
            }),
            _observation("write_file", "Created file: /workspace/report.md"),
        ])
        data = trajectory.steps[0].data
        self.assertEqual(trajectory.steps[0].type, "file_edit")
        self.assertEqual(data["path"], "/workspace/report.md")
        self.assertEqual(data["command"], "write")
        self.assertEqual(data["file_text"], "# Panel\nConfidential finding\n")
        self.assertFalse(data["content_truncated"])

    def test_file_editor_str_replace_records_old_and_new(self):
        trajectory = TrajectoryLog(task_id="T0028-j-b-h", model="gpt-5.6-sol")
        record_openhands_events(trajectory, [
            _action("file_editor", {
                "command": "str_replace",
                "path": "/workspace/report.md",
                "old_str": "draft",
                "new_str": "final",
            }),
            _observation("file_editor", "The file has been edited."),
        ])
        data = trajectory.steps[0].data
        self.assertEqual(data["command"], "str_replace")
        self.assertEqual(data["old_str"], "draft")
        self.assertEqual(data["new_str"], "final")
        self.assertNotIn("file_text", data)
        self.assertFalse(data["content_truncated"])

    def test_gemini_edit_create_records_file_text(self):
        trajectory = TrajectoryLog(task_id="T0028-j-b-h", model="gemini")
        record_openhands_events(trajectory, [
            _action("edit", {
                "file_path": "/workspace/notes.md",
                "old_string": "",
                "new_string": "created body",
            }),
            _observation("edit", "Edited."),
        ])
        data = trajectory.steps[0].data
        self.assertEqual(data["command"], "create")
        self.assertEqual(data["file_text"], "created body")

    def test_file_editor_view_stays_a_read(self):
        trajectory = TrajectoryLog(task_id="T0028-j-b-h", model="gpt-5.6-sol")
        record_openhands_events(trajectory, [
            _action("file_editor", {"command": "view", "path": "/workspace/report.md"}),
            _observation("file_editor", "1|# title"),
        ])
        self.assertEqual(trajectory.steps[0].type, "file_read")
        self.assertEqual(trajectory.steps[0].data["result"], "1|# title")

    def test_oversized_write_sets_content_truncated(self):
        from authorization_eva_sandbox.evaluation.harness.openhands_events import (
            FILE_EDIT_CONTENT_CAP,
        )
        trajectory = TrajectoryLog(task_id="T0028-j-b-h", model="gpt-5.6-sol")
        record_openhands_events(trajectory, [
            _action("write_file", {
                "file_path": "/workspace/big.md",
                "content": "x" * (FILE_EDIT_CONTENT_CAP + 25),
            }),
            _observation("write_file", "Created file."),
        ])
        data = trajectory.steps[0].data
        self.assertEqual(len(data["file_text"]), FILE_EDIT_CONTENT_CAP)
        self.assertTrue(data["content_truncated"])


if __name__ == "__main__":
    unittest.main()
