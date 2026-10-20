"""Native shell and file tools become the steps post_hoc_eval and the digest read."""
from __future__ import annotations

import unittest

from authorization_eva_sandbox.evaluation.harness.native_tools import (
    NATIVE_TOOL_NAMES,
    apply_hunks,
    native_steps,
    parse_patch,
)
from authorization_eva_sandbox.evaluation.harness.openai_agents.events import (
    ActionEvent,
    ObservationEvent,
)
from authorization_eva_sandbox.evaluation.harness.openhands_events import (
    FILE_EDIT_CONTENT_CAP,
    record_openhands_events,
)
from authorization_eva_sandbox.trajectory_logger import TrajectoryLog

SANDBOX_OUTPUT = (
    "Chunk ID: 1a2b3c\nWall time: 0.0100 seconds\nProcess exited with code {code}\n"
    "Output:\n{body}"
)
PATCH = """*** Begin Patch
*** Add File: notes/summary.md
+# Summary
+All three invoices are paid.
*** Update File: config/app.ini
@@ [server]
 host = localhost
-port = 8080
+port = 9090
 debug = false
*** Delete File: tmp/old.log
*** End Patch
"""


def _steps(tool, arguments, output="", **kwargs):
    log = TrajectoryLog(task_id="t", model="m")
    action = ActionEvent(tool_name=tool, arguments=arguments, id="c1")
    observation = ObservationEvent(tool_name=tool, text=output, action_id="c1")
    for key, value in kwargs.items():
        setattr(action if key == "files_before" else observation, key, value)
    record_openhands_events(log, [action, observation])
    return [(step.type, step.data) for step in log.steps]


class TerminalMappingTests(unittest.TestCase):
    def test_exec_command_is_a_terminal_step_with_the_line_the_sdk_runs(self):
        output = SANDBOX_OUTPUT.format(code=0, body="a.txt\n")
        [(kind, data)] = _steps("exec_command", {"cmd": "ls", "workdir": "/workspace/docs"}, output)
        self.assertEqual(kind, "terminal_command")
        self.assertEqual(data["command"], "cd /workspace/docs && ls")
        self.assertEqual(data["cmd"], "ls")
        self.assertEqual(data["workdir"], "/workspace/docs")
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["output"], output)
        self.assertEqual(data["native_tool"], "exec_command")

    def test_exec_command_without_workdir_and_with_a_failure(self):
        output = SANDBOX_OUTPUT.format(code=2, body="rm: cannot remove")
        [(_kind, data)] = native_steps("exec_command", '{"cmd": "rm -rf /workspace/x"}', output)
        self.assertEqual(data["command"], "rm -rf /workspace/x")
        self.assertEqual(data["exit_code"], 2)
        self.assertNotIn("workdir", data)

    def test_running_session_and_write_stdin(self):
        running = "Chunk ID: x\nWall time: 1.0 seconds\nProcess running with session ID 7\nOutput:\n>"
        [(_kind, started)] = native_steps("exec_command", {"cmd": "python3", "tty": True}, running)
        self.assertIsNone(started["exit_code"])
        self.assertEqual(started["session_id"], 7)
        self.assertTrue(started["tty"])
        [(kind, typed)] = native_steps(
            "write_stdin", {"session_id": 7, "chars": "print(1)\n"},
            SANDBOX_OUTPUT.format(code=0, body="1\n"),
        )
        self.assertEqual(kind, "terminal_command")
        self.assertEqual(typed["command"], "print(1)\n")
        self.assertEqual(typed["session_id"], 7)
        self.assertTrue(typed["stdin"])
        self.assertEqual(typed["exit_code"], 0)

    def test_claude_bash_exit_codes(self):
        [(kind, ok)] = _steps("Bash", {"command": "ls /workspace", "description": "list"}, "a\nb")
        self.assertEqual(kind, "terminal_command")
        self.assertEqual(ok["command"], "ls /workspace")
        self.assertIsNone(ok["exit_code"])
        [(_kind, known_ok)] = native_steps("Bash", {"command": "true"}, "", is_error=False)
        self.assertEqual(known_ok["exit_code"], 0)
        [(_kind, failed)] = native_steps(
            "Bash", {"command": "false"}, "Exit code 1\nboom", is_error=True,
        )
        self.assertEqual(failed["exit_code"], 1)
        self.assertTrue(failed["is_error"])


class FileMappingTests(unittest.TestCase):
    def test_claude_write_records_the_whole_file(self):
        [(kind, data)] = _steps(
            "Write", {"file_path": "/workspace/report.md", "content": "# Report\nDone.\n"},
            "File created successfully",
        )
        self.assertEqual(kind, "file_edit")
        self.assertEqual(data["path"], "/workspace/report.md")
        self.assertEqual(data["file_text"], "# Report\nDone.\n")
        self.assertEqual(data["command"], "write")
        self.assertFalse(data["content_truncated"])
        self.assertEqual(data["native_tool"], "Write")

    def test_claude_edit_and_multiedit_record_old_and_new(self):
        [(kind, data)] = _steps("Edit", {
            "file_path": "/workspace/a.py", "old_string": "x = 1", "new_string": "x = 2",
            "replace_all": True,
        })
        self.assertEqual(kind, "file_edit")
        self.assertEqual((data["old_str"], data["new_str"]), ("x = 1", "x = 2"))
        self.assertEqual(data["command"], "str_replace")
        self.assertTrue(data["replace_all"])
        created = native_steps("Edit", {"file_path": "/workspace/n.txt", "old_string": "",
                                        "new_string": "new file"})
        self.assertEqual(created[0][1]["command"], "create")
        self.assertEqual(created[0][1]["file_text"], "new file")
        multi = native_steps("MultiEdit", {"file_path": "/workspace/a.py", "edits": [
            {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"},
        ]})
        self.assertEqual([step[1]["new_str"] for step in multi], ["b", "d"])

    def test_edit_keeps_the_file_after_when_the_adapter_read_it(self):
        [(_kind, data)] = _steps(
            "Edit", {"file_path": "/workspace/a.py", "old_string": "1", "new_string": "2"},
            files_after={"/workspace/a.py": "x = 2\n"},
        )
        self.assertEqual(data["file_text"], "x = 2\n")
        self.assertEqual(data["old_str"], "1")

    def test_claude_read_is_a_file_read(self):
        [(kind, data)] = _steps("Read", {"file_path": "/workspace/a.txt", "limit": 5}, "hello")
        self.assertEqual(kind, "file_read")
        self.assertEqual(data, {"path": "/workspace/a.txt", "result": "hello",
                                "native_tool": "Read", "limit": 5})

    def test_large_write_is_capped(self):
        body = "a" * (FILE_EDIT_CONTENT_CAP + 10)
        [(_kind, data)] = native_steps("Write", {"file_path": "/workspace/big", "content": body})
        self.assertEqual(len(data["file_text"]), FILE_EDIT_CONTENT_CAP)
        self.assertTrue(data["content_truncated"])


class ApplyPatchTests(unittest.TestCase):
    def test_parse_patch_sections(self):
        sections = parse_patch(PATCH)
        self.assertEqual([(s["op"], s["path"]) for s in sections], [
            ("add", "notes/summary.md"), ("update", "config/app.ini"), ("delete", "tmp/old.log"),
        ])

    def test_freeform_patch_becomes_one_file_edit_per_file(self):
        steps = _steps("apply_patch", {"input": PATCH}, "Done!")
        self.assertEqual([kind for kind, _ in steps], ["file_edit"] * 3)
        added, updated, deleted = (data for _kind, data in steps)
        self.assertEqual(added["path"], "/workspace/notes/summary.md")
        self.assertEqual(added["command"], "create")
        self.assertEqual(added["file_text"], "# Summary\nAll three invoices are paid.")
        self.assertEqual(updated["path"], "/workspace/config/app.ini")
        self.assertEqual(updated["command"], "apply_patch")
        self.assertEqual(updated["old_str"], "host = localhost\nport = 8080\ndebug = false")
        self.assertEqual(updated["new_str"], "host = localhost\nport = 9090\ndebug = false")
        self.assertIn("-port = 8080", updated["patch"])
        self.assertNotIn("file_text", updated)
        self.assertEqual(deleted["command"], "delete")
        self.assertEqual(deleted["path"], "/workspace/tmp/old.log")
        self.assertTrue(all(data["native_tool"] == "apply_patch" for _kind, data in steps))
        self.assertTrue(all(data["result"] == "Done!" for _kind, data in steps))

    def test_update_content_is_derived_from_the_file_before(self):
        before = "[server]\nhost = localhost\nport = 8080\ndebug = false\n"
        steps = _steps(
            "apply_patch", {"input": PATCH}, "Done!",
            files_before={"config/app.ini": before},
        )
        updated = steps[1][1]
        self.assertEqual(updated["file_text"], "[server]\nhost = localhost\nport = 9090\ndebug = false\n")

    def test_update_content_prefers_the_file_after(self):
        steps = native_steps(
            "apply_patch", PATCH, "Done!",
            files_before={"/workspace/config/app.ini": "unrelated"},
            files_after={"/workspace/config/app.ini": "as written\n"},
        )
        self.assertEqual(steps[1][1]["file_text"], "as written\n")

    def test_responses_operation_and_move(self):
        [(kind, data)] = native_steps("apply_patch", {"operation": {
            "type": "update_file", "path": "/workspace/a.txt",
            "diff": "@@\n-old line\n+new line\n", "move_to": "b.txt",
        }})
        self.assertEqual(kind, "file_edit")
        self.assertEqual((data["old_str"], data["new_str"]), ("old line", "new line"))
        self.assertEqual(data["move_to"], "/workspace/b.txt")
        [(_kind, created)] = native_steps("apply_patch", {"type": "create_file",
                                                           "path": "c.txt", "diff": "+hi\n"})
        self.assertEqual(created["file_text"], "hi")

    def test_hunks_that_do_not_match_leave_the_content_unknown(self):
        self.assertIsNone(apply_hunks("a\nb\n", [(["zzz"], ["y"])]))
        self.assertEqual(apply_hunks("a\nb\nc", [(["b"], ["B"]), (["c"], ["C"])]), "a\nB\nC")

    def test_paths_outside_the_workspace_stay_absolute(self):
        [(_kind, data)] = native_steps("Write", {"file_path": "/home/openhands/.claude/memory/x.md",
                                                 "content": "m"})
        self.assertEqual(data["path"], "/home/openhands/.claude/memory/x.md")


class RecorderTests(unittest.TestCase):
    def test_openhands_and_our_tool_names_are_not_native(self):
        for name in ("terminal", "read_file", "write_file", "file_editor", "edit", "finish", "think",
                     "message_user", "email_client.send_message"):
            self.assertNotIn(name, NATIVE_TOOL_NAMES)

    def test_our_terminal_step_is_unchanged(self):
        [(kind, data)] = _steps("terminal", {"command": "ls"}, "a")
        self.assertEqual(kind, "terminal_command")
        self.assertEqual(data, {"command": "ls", "output": "a", "exit_code": 0})

    def test_unknown_tool_is_refused_by_the_mapper(self):
        with self.assertRaises(ValueError):
            native_steps("terminal", {"command": "ls"})


if __name__ == "__main__":
    unittest.main()
