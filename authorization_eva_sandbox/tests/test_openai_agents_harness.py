"""OpenAI Agents harness: scripted tools, prompt, condenser, and the container.

The OpenHands environment does not install ``agents``. Those tests skip.
Oracle scripts are replayed through the real SDK tool loop and the task
services. Shell steps use the sleep-infinity container.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import agents  # noqa: F401
except ImportError:
    agents = None

SANDBOX = Path(__file__).resolve().parents[1]
TASKS = SANDBOX / "tasks"
ORACLES = SANDBOX / ".oracle_runs"
IMAGE = "authorizationbench-openhands:1.49.2"
SENTINEL = "SENTINEL-KEY-do-not-leak"

# Ten oracle tasks, plus the wrapper task and the github_repo task.
ORACLE_TASKS = (
    "T0010-l-b-h",   # zoom and email
    "T0030-h-b-h",   # zoom and email
    "T0018-j-b-h",   # calendar
    "T0002-f-i-h",   # email
    "T0005-i-g-h",   # attachments
    "T0035-n-a-h",   # docs_portal
    "T0047-a-h-h",   # email and docs_portal
    "T0004-b-f-h",   # shell deletion
    "T0026-j-a-h",   # shell deletion
    "T0012-b-c-h",   # shell
)
PROMPT_TASKS = ("T0010-l-b-h", "T0005-i-g-h", "T0035-n-a-h", "T0015-j-c-h", "T0004-b-f-h")
_ACTION_TYPES = {
    "tool_call", "terminal_command", "file_read", "file_edit", "ask_user", "agent_finish",
}


def _docker_image() -> bool:
    proc = subprocess.run(
        ["docker", "inspect", IMAGE],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0


def _needs_workspace(bundle: Path) -> bool:
    config = json.loads((bundle / "task_config.json").read_text())
    ids = {entry.get("id") for entry in config.get("services") or []}
    return bool(ids & {"docs_portal", "email_client", "github_repo"})


def _needs_container(steps: list[dict]) -> bool:
    return any(step.get("tool") in {"shell", "read_file", "write_file"} for step in steps)


def _oracle_turn(step: dict) -> list[dict]:
    tool = step["tool"]
    args = dict(step.get("args") or {})
    if tool == "shell":
        return [{"name": "terminal", "arguments": {"command": args.get("command", "")}}]
    if tool == "read_file":
        path = args.get("path") or args.get("file_path")
        return [{"name": "read_file", "arguments": {"file_path": path}}]
    if tool == "write_file":
        return [{"name": "write_file", "arguments": {
            "file_path": args.get("path") or args.get("file_path"),
            "content": args.get("content", ""),
        }}]
    if tool == "message_user":
        return [{"name": "message_user", "arguments": {
            "message": args.get("message") or args.get("text") or "",
        }}]
    return [{"name": tool.replace(".", "_"), "arguments": args}]


def _file_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


class ToolLimitTests(unittest.TestCase):
    def test_terminal_output_is_clipped_like_openhands(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            MAX_CMD_OUTPUT_SIZE,
            clip_output,
        )

        text = "a" * (MAX_CMD_OUTPUT_SIZE + 100)
        clipped = clip_output(text)
        self.assertLessEqual(len(clipped), MAX_CMD_OUTPUT_SIZE)
        self.assertIn("response clipped", clipped)
        self.assertTrue(clipped.startswith("a"))
        self.assertTrue(clipped.endswith("a"))

    def test_persistent_bash_keeps_state_and_matches_openhands_timeout(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            TIMEOUT_MESSAGE_TEMPLATE,
            WorkspaceTools,
        )

        tools = WorkspaceTools(
            "local",
            user="test",
            cwd=os.getcwd(),
            shell_argv=["bash", "--noprofile", "--norc", "-i"],
            no_change_timeout_seconds=1,
        )
        try:
            tools.terminal({"command": "export X=1; foo() { echo persisted; }"})
            echoed = tools.terminal({"command": "echo $X; foo"})
            self.assertIn("1", echoed)
            self.assertIn("persisted", echoed)
            self.assertIn("Command finished with exit code 0", echoed)
            tools.terminal({"command": "cd /tmp"})
            listed = tools.terminal({"command": "pwd"})
            self.assertIn("/tmp", listed)
            self.assertIn("[Current working directory:", listed)
            started = tools.terminal({"command": "sleep 30 & echo $!"})
            pid = next(line.strip() for line in started.splitlines() if line.strip().isdigit())
            alive = tools.terminal({"command": f"kill -0 {pid}"})
            self.assertIn("Command finished with exit code 0", alive)
            paused = tools.terminal({"command": "sleep 30"})
            self.assertIn("no new output after 1 seconds", paused)
            self.assertIn(TIMEOUT_MESSAGE_TEMPLATE, paused)
            self.assertNotIn("Command finished with exit code", paused)
            blocked = tools.terminal({"command": "echo should-not-run"})
            self.assertIn("is NOT executed", blocked)
            self.assertIn("still running", blocked)
            session = tools._session
            self.assertIsNotNone(session.proc)
            tools.close()
            self.assertIsNotNone(session.proc.poll())
        finally:
            tools.close()

    def test_persistent_bash_ends_when_its_container_is_removed(self):
        if not _docker_image():
            self.skipTest("authorizationbench image is not available")
        import uuid

        from authorization_eva_sandbox.evaluation.harness.agent_container import (
            IMAGE_USER,
            agent_container_spec,
            host_linux_platform,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.container import (
            SleepAgentContainer,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            WorkspaceTools,
        )

        copy = Path(tempfile.mkdtemp(prefix="oa_shell_"))
        container = None
        tools = None
        try:
            spec = agent_container_spec(copy, home="/workspace", platform=host_linux_platform())
            container = SleepAgentContainer(spec, name=f"oa-shell-{uuid.uuid4().hex[:6]}")
            container.start()
            tools = WorkspaceTools(container.name, user=IMAGE_USER, cwd="/workspace")
            text = tools.terminal({"command": "echo ready"})
            self.assertIn("ready", text)
            session = tools._session
            container.remove()
            session.proc.wait(timeout=10)
            self.assertIsNotNone(session.proc.poll())
        finally:
            if tools is not None:
                tools.close()
            if container is not None:
                container.remove()
            shutil.rmtree(copy, ignore_errors=True)


@unittest.skipUnless(agents is not None, "agents SDK is not installed")
class OpenAIAgentsHarnessTests(unittest.TestCase):
    def test_length_cap_becomes_model_behavior_error(self):
        from agents.exceptions import ModelBehaviorError

        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            _raise_if_length_capped,
        )

        empty = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content=None, tool_calls=None),
            )],
        )
        with self.assertRaises(ModelBehaviorError):
            _raise_if_length_capped(empty)
        kept = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content="partial", tool_calls=None),
            )],
        )
        _raise_if_length_capped(kept)

    def test_parallel_tool_calls_is_not_sent(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            model_for_profile,
        )

        _model, settings, info = model_for_profile(
            {"model": "gpt-4.1", "api_key": "not-used"}, chat_backend="litellm",
        )
        self.assertNotEqual(getattr(settings, "parallel_tool_calls", None), True)
        self.assertEqual(info.api_path, "responses")

    def test_output_cap_ends_the_run(self):
        from agents.exceptions import ModelBehaviorError

        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        model = ScriptedModel([
            ModelBehaviorError("output cap: finish_reason=length before any text or tool call"),
        ])
        produced = self._run_runtime(model, "You have these tools:\nfinish\n", "go")
        self.assertEqual(produced["metadata"]["stop_reason"], "output_cap")
        self.assertEqual(produced["state"]["agent_state"], "ERROR")
        self.assertIn("accumulated_cost_usd", produced["cost_summary"]["combined"])

    def test_responses_incomplete_max_output_tokens_ends_the_run(self):
        from agents.exceptions import ModelBehaviorError

        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            _raise_if_responses_output_capped,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        recorded = SimpleNamespace(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output=[],
        )
        with self.assertRaises(ModelBehaviorError) as raised:
            _raise_if_responses_output_capped(recorded)
        self.assertIn("max_output_tokens", str(raised.exception))
        completed = SimpleNamespace(
            status="completed",
            incomplete_details=None,
            output=[],
        )
        _raise_if_responses_output_capped(completed)
        other = SimpleNamespace(
            status="incomplete",
            incomplete_details={"reason": "content_filter"},
        )
        _raise_if_responses_output_capped(other)

        model = ScriptedModel([raised.exception])
        produced = self._run_runtime(model, "You have these tools:\nfinish\n", "go")
        self.assertEqual(produced["metadata"]["stop_reason"], "output_cap")
        self.assertIn("max_output_tokens", produced["metadata"]["output_cap"])
        self.assertIn("max_output_tokens", produced["state"]["last_error"])

    def test_simulated_user_reply_reaches_the_next_segment(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        model = ScriptedModel(["which folder should I use?", "sent"])
        seen: list[str] = []

        def reply(text: str) -> str | None:
            seen.append(text)
            return "use the shared listing" if len(seen) == 1 else None

        self._run_runtime(
            model, "You have these tools:\nfinish\n", "start", user_reply_fn=reply,
        )
        self.assertEqual(seen[0], "which folder should I use?")
        second = model.calls[1]["input"]
        self.assertIn("use the shared listing", json.dumps(second, default=str))
        self.assertEqual(len(model.calls), 2)

    def test_step_limit_resets_for_each_segment(self):
        from agents import function_tool

        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        @function_tool
        def ping() -> str:
            """Return pong."""
            return "pong"

        ping_call = [{"name": "ping", "arguments": {}}]
        model = ScriptedModel([ping_call, "which folder?", ping_call, "all set, anything else?"])
        replies = iter(["use the shared one"])

        produced = self._run_runtime(
            model, "You have these tools:\nping, finish\n", "start",
            local_tools=[ping], max_steps=2,
            user_reply_fn=lambda _text: next(replies, None),
        )
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(produced["metadata"]["stop_reason"], "finished")
        self.assertEqual(produced["metadata"]["max_steps_scope"], "per_segment")

    def test_repeated_actions_are_recorded_not_stopped(self):
        from agents import function_tool

        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        @function_tool
        def ping() -> str:
            """Return pong."""
            return "pong"

        ping_call = [{"name": "ping", "arguments": {}}]
        model = ScriptedModel([ping_call] * 5 + ["done"])

        produced = self._run_runtime(
            model, "You have these tools:\nping, finish\n", "start",
            local_tools=[ping], max_steps=10,
        )
        self.assertEqual(len(model.calls), 6)
        self.assertEqual(produced["metadata"]["stop_reason"], "finished")
        self.assertEqual(produced["metadata"]["repeated_actions"],
                         [{"tool": "ping", "times": 5, "first_pair": 0}])

    def test_wall_clock_timeout_ends_the_run(self):
        import time

        from agents import function_tool

        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        @function_tool
        def ping() -> str:
            """Return pong."""
            time.sleep(0.2)
            return "pong"

        model = ScriptedModel([
            [{"name": "ping", "arguments": {}}],
            "should not be sent",
        ])
        produced = self._run_runtime(
            model, "You have these tools:\nping, finish\n", "start",
            local_tools=[ping], max_steps=4, task_timeout_seconds=0.05,
        )
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(produced["metadata"]["stop_reason"], "timeout")
        self.assertEqual(produced["state"]["agent_state"], "ERROR")
        self.assertIn("wall-clock timeout", produced["state"]["last_error"])

    def test_simulated_user_stops_after_ten_turns(self):
        from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
            MAX_PLAIN_REPLY_TURNS,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        self.assertEqual(MAX_PLAIN_REPLY_TURNS, 10)
        model = ScriptedModel(["which folder?"] * (MAX_PLAIN_REPLY_TURNS + 3))
        replies: list[str] = []

        def reply(text: str) -> str:
            replies.append(text)
            return "use the shared listing"

        produced = self._run_runtime(
            model, "You have these tools:\nfinish\n", "start",
            user_reply_fn=reply, max_steps=2,
        )
        self.assertEqual(len(replies), MAX_PLAIN_REPLY_TURNS)
        self.assertEqual(len(model.calls), MAX_PLAIN_REPLY_TURNS + 1)
        self.assertEqual(produced["metadata"]["stop_reason"], "finished")
        self.assertEqual(produced["state"]["agent_state"], "FINISHED")

    def test_step_limit_ends_the_segment_and_the_run(self):
        from agents import function_tool

        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        @function_tool
        def ping() -> str:
            """Return pong."""
            return "pong"

        ping_call = [{"name": "ping", "arguments": {}}]
        model = ScriptedModel([ping_call, ping_call, ping_call])
        produced = self._run_runtime(
            model, "You have these tools:\nping, finish\n", "start",
            local_tools=[ping], max_steps=2,
        )
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(produced["metadata"]["stop_reason"], "max_steps")
        self.assertEqual(produced["state"]["agent_state"], "ERROR")
        self.assertIn("maximum iterations", produced["state"]["last_error"])
        self.assertIn("(2)", produced["state"]["last_error"])

    def test_empty_final_output_ends_the_run(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.events import (
            MessageEvent,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        model = ScriptedModel([""])
        replies: list[str] = []
        produced = self._run_runtime(
            model, "You have these tools:\nfinish\n", "start",
            user_reply_fn=lambda text: replies.append(text) or "go on",
        )
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(replies, [])
        self.assertEqual(produced["metadata"]["stop_reason"], "finished")
        self.assertEqual(produced["state"]["agent_state"], "FINISHED")
        agent_messages = [
            event for event in produced["events"]
            if isinstance(event, MessageEvent) and event.source == "agent"
        ]
        self.assertEqual([event._text for event in agent_messages], [""])

    def test_tracing_stays_off(self):
        from agents.models.interface import ModelTracing

        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        model = ScriptedModel(["done"])
        self._run_runtime(model, "You have these tools:\nfinish\n", "start")
        self.assertTrue(model.calls)
        for call in model.calls:
            self.assertEqual(call["tracing"], ModelTracing.DISABLED)
        self.assertEqual(os.environ.get("OPENAI_AGENTS_DISABLE_TRACING"), "1")

    def test_condenser_drops_the_system_prompt_and_keeps_tool_pairs(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
            SummarizingCondenser,
        )
        from authorization_eva_sandbox.evaluation.run_eval import context_management_record

        def count(messages: list[dict]) -> int:
            blob = json.dumps(messages)
            if "SYSTEM PROMPT" in blob and "HUGE-TOOL-OUTPUT" in blob:
                return 1000
            return 10

        condenser = SummarizingCondenser(
            max_tokens=100,
            keep_first=0,
            model="gpt-4.1",
            summarize=lambda _prompt: "summary of the forgotten prefix",
            count_tokens=count,
        )
        first = condenser.apply(
            "SYSTEM PROMPT",
            [{"role": "user", "content": "hello"}],
            event_ids={"system": "evt-system"},
        )
        self.assertIsNone(first.record)
        items = [
            {"role": "user", "content": "hello"},
            {"type": "function_call", "call_id": "c1", "name": "terminal", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "HUGE-TOOL-OUTPUT"},
        ]
        second = condenser.apply(
            "SYSTEM PROMPT",
            items,
            event_ids={"system": "evt-system", "function_call:c1": "c1",
                       "function_call_output:c1": "c1-obs"},
        )
        self.assertIsNotNone(second.record)
        self.assertIsNone(second.instructions)
        self.assertEqual(second.record.summary_offset, 0)
        forgotten_ids = {item.call_id for item in second.record.forgotten if item.call_id}
        if "c1" in forgotten_ids:
            kinds = {item.kind for item in second.record.forgotten}
            self.assertIn("function_call", kinds)
            self.assertIn("function_call_output", kinds)
        else:
            rendered = json.dumps(second.input_items)
            self.assertIn("function_call", rendered)
            self.assertIn("function_call_output", rendered)
        self.assertEqual(condenser.unhelpful, 0)

        from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
            OpenAIAgentsRuntime,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            WorkspaceTools,
            function_tools,
        )

        def _exec(command, **kwargs):
            del kwargs
            return subprocess.CompletedProcess(command, 0, "ok\n__AB_CWD__/workspace\n", "")

        workspace = WorkspaceTools("unused", user="openhands", cwd="/workspace", exec_fn=_exec)
        tools = function_tools(["terminal", "finish"], workspace)
        model = ScriptedModel([
            [{"name": "terminal", "arguments": {"command": "echo hi"}}],
            "done",
        ])
        def over_only_with_system(messages: list[dict]) -> int:
            return 1000 if "SYSTEM PROMPT" in json.dumps(messages) else 10

        runtime_condenser = SummarizingCondenser(
            max_tokens=20,
            keep_first=0,
            model="gpt-4.1",
            summarize=lambda _prompt: "summary only",
            count_tokens=over_only_with_system,
        )
        runtime = OpenAIAgentsRuntime(
            instruction="do it",
            system_prompt="SYSTEM PROMPT\nYou have these tools:\nterminal, finish\n",
            model=model,
            task="condense",
            model_name="gpt-4.1",
            local_tools=tools,
            condenser=runtime_condenser,
            workspace=workspace,
            task_timeout_seconds=30,
            max_steps=5,
        )
        produced = runtime.run()
        record = context_management_record(
            {"agent": {"condenser": {"max_tokens": 20, "keep_first": 0}}},
            produced["events"],
        )
        self.assertEqual(record["condensation_event_count"], 1)
        summary = record["condensation_summaries"][0]
        forgotten_kinds = [item["kind"] for item in summary["forgotten"]]
        self.assertIn("SystemPromptEvent", forgotten_kinds)
        self.assertIsNone(model.calls[0]["system"])
        actions = [
            event for event in produced["events"]
            if type(event).__name__ == "ActionEvent" and event.tool_name == "terminal"
        ]
        observations = [
            event for event in produced["events"]
            if type(event).__name__ == "ObservationEvent" and event.tool_name == "terminal"
        ]
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].action_id, actions[0].id)
        self.assertEqual(
            [tool["function"]["name"] for tool in runtime_condenser.tools],
            ["terminal", "finish"],
        )

    def test_condensation_is_recorded_on_the_trajectory_step_where_it_took_effect(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
            SummarizingCondenser,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
            OpenAIAgentsRuntime,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.trajectory import (
            record_condensation_in_trajectory_file,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            WorkspaceTools,
            function_tools,
        )
        from authorization_eva_sandbox.evaluation.run_eval import (
            context_management_record,
            record_openhands_events,
        )
        from authorization_eva_sandbox.evaluation.run_records import _step_lines
        from authorization_eva_sandbox.trajectory_logger import TrajectoryLog

        def count(messages: list[dict]) -> int:
            return 5000 if "HUGE-TOOL-OUTPUT" in json.dumps(messages) else 10

        condenser = SummarizingCondenser(
            max_tokens=100,
            keep_first=0,
            model="gpt-4.1",
            summarize=lambda _prompt: "Kept the task. The system prompt was dropped.",
            count_tokens=count,
        )

        def _exec(command, **kwargs):
            del command, kwargs
            return subprocess.CompletedProcess([], 0, "HUGE-TOOL-OUTPUT\n", "")

        workspace = WorkspaceTools("unused", user="openhands", cwd="/workspace", exec_fn=_exec)
        tools = function_tools(["terminal", "finish"], workspace)
        model = ScriptedModel([
            [{"name": "terminal", "arguments": {"command": "echo hi"}}],
            "done",
        ])
        runtime = OpenAIAgentsRuntime(
            instruction="do it",
            system_prompt="SYSTEM PROMPT\nYou have these tools:\nterminal, finish\n",
            model=model,
            task="condense",
            model_name="gpt-4.1",
            local_tools=tools,
            condenser=condenser,
            workspace=workspace,
            task_timeout_seconds=30,
            max_steps=5,
        )
        produced = runtime.run()
        self.assertIn("SYSTEM PROMPT", model.calls[0]["system"])
        self.assertIsNone(model.calls[1]["system"])
        self.assertNotIn("HUGE-TOOL-OUTPUT", json.dumps(model.calls[1]["input"]))
        self.assertIn("Kept the task. The system prompt was dropped.", json.dumps(model.calls[1]["input"]))
        record = context_management_record(
            {"agent": {"condenser": {"max_tokens": 100, "keep_first": 0}}},
            produced["events"],
        )
        self.assertEqual(record["condensation_event_count"], 1)
        self.assertEqual(
            record["condensation_summaries"][0]["summary"],
            "Kept the task. The system prompt was dropped.",
        )

        log = TrajectoryLog(task_id="condense", model="gpt-4.1")
        record_openhands_events(log, produced["events"])
        folder = Path(tempfile.mkdtemp(prefix="oa_condense_"))
        try:
            log.save(str(folder / "traj_condense.json"))
            record_condensation_in_trajectory_file(folder, produced["events"])
            saved = json.loads((folder / "traj_condense.json").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        steps = saved["trajectory"]
        condensation = next(step for step in steps if step["type"] == "condensation")
        terminal = next(step for step in steps if step["type"] == "terminal_command")
        self.assertEqual(terminal["data"]["command"], "echo hi")
        self.assertEqual(condensation["data"]["after_step"], terminal["step"])
        self.assertEqual(condensation["data"]["forgotten_steps"], [1, terminal["step"]])
        self.assertLess(terminal["step"], condensation["step"])
        self.assertEqual(
            condensation["data"]["summary"],
            "Kept the task. The system prompt was dropped.",
        )
        self.assertTrue(condensation["data"]["system_prompt_forgotten"])
        self.assertEqual(
            condensation["data"]["system_prompt_event"],
            "SystemPromptEvent evt-system",
        )
        self.assertFalse(condensation["data"]["previous_summary_forgotten"])
        self.assertEqual(condensation["data"]["trigger_tokens"], 5000)
        self.assertEqual(condensation["data"]["unmapped_forgotten"], [])
        self.assertEqual(saved["total_steps"], len(steps))
        line = _step_lines(condensation)
        self.assertIn("took effect after step 2", line)
        self.assertIn("Forgot steps 1-2", line)
        self.assertIn("Triggered at 5000 tokens", line)

    def test_tool_schemas_count_only_while_the_system_prompt_is_present(self):
        import litellm

        from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
            SummarizingCondenser,
            _forgotten_range,
            _parse_view,
            _suffix_length,
        )

        tools = [{
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a shell command in the persistent terminal session.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The command to run"},
                        "timeout": {"type": "number", "description": "Hard timeout in seconds"},
                    },
                    "required": ["command"],
                },
            },
        }]
        condenser = SummarizingCondenser(
            max_tokens=100,
            keep_first=0,
            model="gpt-4.1",
            summarize=lambda _prompt: "summary",
            tools=tools,
        )

        def messages_of(items):
            return [item.as_message() for item in items]

        def openhands_count(items):
            schemas = tools if any(item.kind == "system" for item in items) else None
            return int(litellm.token_counter(
                model="gpt-4.1", messages=messages_of(items), tools=schemas,
            ))

        def openhands_suffix(events, base, token_reduction):
            """get_suffix_length_for_token_reduction, including tools only with a system event."""
            if not events or token_reduction <= 0:
                return len(events)
            base_tokens = openhands_count(base) if base else 0

            def region(prefix):
                return openhands_count([*base, *events[:prefix]]) - base_tokens

            if region(len(events)) <= token_reduction:
                return 0
            left, right = 1, len(events)
            while left < right:
                mid = (left + right) // 2
                if region(mid) > token_reduction:
                    right = mid
                else:
                    left = mid + 1
            return len(events) - left

        view = _parse_view(
            "SYSTEM PROMPT " + ("policy " * 40),
            [{"role": "user", "content": f"note {index} " + ("word " * 20)} for index in range(8)],
            {},
        )
        before = condenser._tokens(view)
        without_schemas = int(litellm.token_counter(
            model="gpt-4.1", messages=messages_of(view), tools=None,
        ))
        schema_size = before - without_schemas
        self.assertGreater(schema_size, 0)
        self.assertEqual(before, openhands_count(view))
        forgotten_system = [item for item in view if item.kind != "system"]
        after = condenser._tokens(forgotten_system)
        self.assertEqual(after, openhands_count(forgotten_system))
        self.assertEqual(before - without_schemas, schema_size)
        self.assertEqual(
            before - after,
            schema_size + (without_schemas - after),
        )
        head = view[:1]
        tail_prefix = view[1:4]
        self.assertEqual(head[0].kind, "system")
        cancelled = condenser._tokens([*head, *tail_prefix]) - condenser._tokens(head)
        bare_head = int(litellm.token_counter(
            model="gpt-4.1", messages=messages_of(head), tools=None,
        ))
        bare_both = int(litellm.token_counter(
            model="gpt-4.1", messages=messages_of([*head, *tail_prefix]), tools=None,
        ))
        self.assertEqual(cancelled, bare_both - bare_head)
        total = condenser._tokens(view)
        token_reduction = total - (total // 2)
        suffix = _suffix_length(view, [], token_reduction, condenser._tokens)
        self.assertEqual(suffix, openhands_suffix(view, [], token_reduction))
        forgotten, offset = _forgotten_range(
            view, keep_first=0, max_tokens=total, count=condenser._tokens,
        )
        self.assertEqual(offset, 0)
        self.assertEqual(len(forgotten), len(view) - suffix)
        self.assertEqual(condenser._tokens(forgotten), openhands_count(forgotten))

    def test_condenser_matches_openhands_token_count_and_offsets(self):
        """Same history and cuts as OpenHands 1.49.2, on the schemas we send.

        The cut integers were recorded from ``LLMSummarizingCondenser.condense``
        on this history with gpt-4.1. The view is 1381 tokens on the schemas
        this harness sends (``terminal``, ``read_file``, ``finish``). It was
        1582 when ``think`` was included, after ``security_risk`` and
        ``summary`` were left out (OpenHands counted 1788 with those fields).
        ``max_tokens`` stays 596, the budget of that recorded run. This test
        does not import openhands.
        """
        from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
            SummarizingCondenser,
            _forgotten_range,
            _parse_view,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            load_tool_specs,
        )

        specs = load_tool_specs()
        tools = [{
            "type": "function",
            "function": {
                "name": specs[name]["name"],
                "description": specs[name]["description"],
                "parameters": specs[name]["parameters"],
            },
        } for name in ("terminal", "read_file", "finish")]
        system = "SYSTEM " + ("policy " * 30)
        user = "Please inspect the inbox and draft a note."
        calls = [
            ("terminal", "c1", json.dumps({
                "command": "pwd", "security_risk": "LOW",
                "summary": "Show the working directory",
            })),
            ("read_file", "c2", json.dumps({
                "file_path": "/workspace/report.md",
                "summary": "Read the draft report",
            })),
            ("terminal", "c3", json.dumps({"command": "ls notes", "security_risk": "LOW"})),
            ("read_file", "c4", json.dumps({"file_path": "/workspace/notes/a.md"})),
        ]
        observations = {
            "c1": "/workspace\n",
            "c2": "Quarterly target: $10\n" * 8,
            "c3": "a.md\nb.md\n",
            "c4": "notes body " * 20,
        }
        items = [
            {"role": "user", "content": user},
            {"type": "reasoning", "summary": [{"text": "Look around first."}]},
            {"role": "assistant", "content": "I will start with the shell."},
            {"type": "function_call", "name": "terminal", "call_id": "c1", "arguments": calls[0][2]},
            {"type": "function_call_output", "call_id": "c1", "output": observations["c1"]},
            {"type": "function_call", "name": "read_file", "call_id": "c2", "arguments": calls[1][2]},
            {"type": "function_call_output", "call_id": "c2", "output": observations["c2"]},
            {"type": "reasoning", "summary": [{"text": ""}]},
            {"type": "function_call", "name": "terminal", "call_id": "c3", "arguments": calls[2][2]},
            {"type": "function_call", "name": "read_file", "call_id": "c4", "arguments": calls[3][2]},
            {"type": "function_call_output", "call_id": "c3", "output": observations["c3"]},
            {"type": "function_call_output", "call_id": "c4", "output": observations["c4"]},
            {"role": "assistant", "content": "The draft is still a placeholder."},
        ]
        view = _parse_view(system, items, {})
        self.assertEqual(
            [(item.kind, item.tool_name) for item in view],
            [
                ("system", None),
                ("user", None),
                ("function_call", "terminal"),
                ("function_call_output", "terminal"),
                ("function_call", "read_file"),
                ("function_call_output", "read_file"),
                ("function_call", "terminal"),
                ("function_call", "read_file"),
                ("function_call_output", "terminal"),
                ("function_call_output", "read_file"),
                ("assistant", None),
            ],
        )
        self.assertEqual(view[2].thought, "I will start with the shell.")
        self.assertEqual(view[6].batch_id, view[7].batch_id)
        condenser = SummarizingCondenser(
            max_tokens=596,
            keep_first=0,
            model="gpt-4.1",
            summarize=lambda _prompt: "summary of the forgotten prefix",
            tools=tools,
        )
        self.assertEqual(condenser._tokens(view), 1381)
        # Forgetting index before the 10% rule, then the condensation OpenHands emits.
        expected = {
            0: {"index": 0, "range": 1, "offset": 0, "forgotten": 11},
            4: {"index": 4, "range": 7, "offset": 4, "forgotten": 7},
        }
        for keep, want in expected.items():
            with self.subTest(keep_first=keep):
                forgotten, index = _forgotten_range(
                    view, keep_first=keep, max_tokens=596, count=condenser._tokens,
                )
                self.assertEqual(index, want["index"])
                self.assertEqual(len(forgotten), want["range"])
                applied = SummarizingCondenser(
                    max_tokens=596,
                    keep_first=keep,
                    model="gpt-4.1",
                    summarize=lambda _prompt: "summary of the forgotten prefix",
                    tools=tools,
                ).apply(system, items)
                self.assertIsNotNone(applied.record)
                self.assertEqual(applied.record.summary_offset, want["offset"])
                self.assertEqual(len(applied.record.forgotten), want["forgotten"])

    def test_published_schemas_omit_openhands_prediction_fields(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            load_tool_specs,
        )

        specs = load_tool_specs()
        self.assertEqual(list(specs), ["terminal", "read_file", "write_file", "finish"])
        for name, spec in specs.items():
            properties = spec["parameters"]["properties"]
            required = spec["parameters"].get("required") or []
            self.assertNotIn("security_risk", properties, name)
            self.assertNotIn("summary", properties, name)
            self.assertNotIn("security_risk", required, name)
            self.assertNotIn("summary", required, name)

    def test_think_call_returns_the_sdk_unknown_tool_error(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.prompts import (
            openhands_local_tool_names,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            WorkspaceTools,
            function_tools,
        )

        mounted = openhands_local_tool_names([
            "runtime:shell.read_file",
            "runtime:shell.write_file",
            "runtime:shell.bash",
        ])
        self.assertEqual(mounted, ["read_file", "write_file", "terminal", "finish"])
        model = ScriptedModel([
            [{"name": "think", "arguments": {"thought": "Check the listing."}}],
            "done",
        ])
        tools = function_tools(
            mounted,
            WorkspaceTools("unused", user="openhands", cwd="/workspace"),
        )
        produced = self._run_runtime(
            model,
            "You have these tools:\nread_file, write_file, terminal, finish\n",
            "start",
            local_tools=tools,
        )
        self.assertNotIn("think", model.calls[0]["tools"])
        self.assertIn("Tool 'think' not found.", json.dumps(model.calls[1]["input"]))
        self.assertIn("events", produced)

    def test_preface_reasoning_and_text_reach_judges_and_the_digest(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
            OpenAIAgentsRuntime,
        )
        from authorization_eva_sandbox.evaluation.run_eval import record_openhands_events
        from authorization_eva_sandbox.evaluation.run_records import _step_lines
        from authorization_eva_sandbox.trajectory_logger import TrajectoryLog

        runtime = OpenAIAgentsRuntime(
            instruction="start",
            system_prompt="system",
            model=None,
            task="unit",
            model_name="gpt-4.1",
        )
        runtime._ensure_input([
            {"type": "reasoning", "summary": [{"text": "Look around first."}]},
            {"role": "assistant", "content": "I will start with the shell."},
            {"type": "function_call", "name": "terminal", "call_id": "c1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "/workspace\n"},
            {"role": "assistant", "content": "Done."},
        ])
        log = TrajectoryLog(task_id="unit", model="gpt-4.1")
        record_openhands_events(log, runtime.events)
        kinds = [step.type for step in log.steps]
        self.assertEqual(kinds, ["agent_think", "agent_message", "terminal_command", "agent_message"])
        digest = "\n".join(
            _step_lines({"step": step.step, "type": step.type, "data": step.data})
            for step in log.steps
        )
        self.assertIn("Look around first.", digest)
        self.assertIn("I will start with the shell.", digest)
        self.assertIn("Done.", digest)
        self.assertIn("/workspace", digest)
        # The condenser still does not count the preface as its own event.
        self.assertTrue(any("function_call:c1" == key or key.endswith(":c1") for key in runtime._events_by_key))
        self.assertFalse(any("reasoning:" in key for key in runtime._events_by_key))

    def test_prompt_tools_match_openhands_prompt(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.prompts import (
            prompt_tool_names,
        )
        from authorization_eva_sandbox.evaluation.harness.tool_ir import allowlist_from_actions
        from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import (
            adapter_for_model,
        )
        from authorization_eva_sandbox.evaluation.run_eval import sealed_system_prompt

        for task in PROMPT_TASKS:
            with self.subTest(task=task):
                bundle = TASKS / task
                vignette = json.loads((bundle / "vignette.json").read_text())
                actions = [step["action"] for step in vignette["assembly_blueprint"]["actions"]]
                names, _files = allowlist_from_actions(actions)
                prompt = sealed_system_prompt(bundle)
                prompt = adapter_for_model("gpt-4.1", names).adapt_prompt(prompt)
                produced, model = self._replay_turns(bundle, prompt, ["done"], names)
                self.assertEqual(model.calls[0]["system"], prompt)
                listed = set(prompt_tool_names(prompt))
                sent = set(model.calls[0]["tools"])
                self.assertEqual(sent, listed, f"{task}: sent {sorted(sent)} prompt {sorted(listed)}")
                self.assertEqual(produced["metadata"]["harness"], "openai_agents")

    def test_tool_outside_the_allowlist_is_not_listed(self):
        bundle = TASKS / "T0010-l-b-h"
        _produced, model = self._replay_turns(
            bundle,
            "You have these tools:\nzoom_get_meeting, finish\n",
            ["done"],
            ["zoom.get_meeting"],
            local_names=["finish"],
        )
        sent = set(model.calls[0]["tools"])
        self.assertIn("zoom_get_meeting", sent)
        self.assertIn("finish", sent)
        self.assertNotIn("email_client_send_message", sent)
        self.assertNotIn("github_repo_view_repo_home", sent)

    def test_oracle_scripts(self):
        for task in ORACLE_TASKS:
            oracle = json.loads((ORACLES / task / "oracle.json").read_text())
            for path in ("authorized", "violating"):
                with self.subTest(task=task, path=path):
                    self._replay_oracle(task, oracle[path]["steps"])

    def test_wrapper_and_github_repo(self):
        if not _docker_image():
            self.fail(f"docker image {IMAGE} is required for the wrapper replay")
        self._replay_oracle("T0015-j-c-h", [{
            "tool": "shell",
            "args": {"command": "npm list"},
            "why": "the npm wrapper logs the invocation",
        }])
        self._replay_oracle("T0078-i-c-n", [{
            "tool": "github_repo.view_repo_home",
            "args": {},
            "why": "open the repo home",
        }])

    def test_no_credential_in_the_container_workspace_or_trajectory(self):
        if not _docker_image():
            self.fail(f"docker image {IMAGE} is required")
        from authorization_eva_sandbox.evaluation.harness.agent_container import (
            agent_container_spec,
            host_linux_platform,
            prepare_agent_workspace_copy,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.container import (
            SleepAgentContainer,
        )

        previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "SESSION_API_KEY")}
        os.environ["OPENAI_API_KEY"] = SENTINEL
        os.environ["SESSION_API_KEY"] = SENTINEL
        copy_root = Path(tempfile.mkdtemp(prefix="oa_secret_"))
        container = None
        try:
            host_copy = prepare_agent_workspace_copy(TASKS / "T0010-l-b-h", copy_root / "workspace")
            (host_copy / "note.txt").write_text("no secrets here\n")
            spec = agent_container_spec(
                host_copy, home="/workspace", platform=host_linux_platform(),
            )
            container = SleepAgentContainer(spec, name=f"oa-secret-{os.getpid()}")
            container.start()
            proc = subprocess.run(
                ["docker", "exec", container.name, "printenv"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotIn(SENTINEL, proc.stdout)
            self.assertNotIn(SENTINEL, proc.stderr)
            difference = container.difference()
            self.assertFalse(difference["agent_server"])
            self.assertEqual(difference["sandbox_state_dir"], None)
            self.assertTrue(difference["omitted_openhands_env"])
            blob = (host_copy / "note.txt").read_text()
            self.assertNotIn(SENTINEL, blob)
            produced = self._run_runtime(
                self._text_model("done"),
                "You have these tools:\nfinish\n",
                "start",
            )
            dumped = json.dumps(produced, default=str)
            self.assertNotIn(SENTINEL, dumped)
        finally:
            if container is not None:
                container.remove()
            for key, old in previous.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
            shutil.rmtree(copy_root, ignore_errors=True)

    def test_container_is_removed_when_the_run_raises(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.bundle import (
            _remove_on_exit,
        )

        class _Fake:
            def __init__(self):
                self.removed = 0

            def remove(self):
                self.removed += 1

        fake = _Fake()
        with self.assertRaises(RuntimeError):
            with _remove_on_exit(fake):
                raise RuntimeError("boom")
        self.assertGreaterEqual(fake.removed, 1)

    def test_shared_run_path_writes_the_trajectory(self):
        from authorization_eva_sandbox.evaluation.run_eval import run_single_task

        bundle = TASKS / "T0010-l-b-h"
        oracle = json.loads((ORACLES / "T0010-l-b-h" / "oracle.json").read_text())
        steps = oracle["authorized"]["steps"]
        outputs = Path(tempfile.mkdtemp(prefix="oa_out_"))
        try:
            path = self._replay_oracle(
                "T0010-l-b-h", steps, outputs=outputs, through_run_single_task=True,
            )
            traj = json.loads(next(Path(path).glob("traj_*.json")).read_text())
            self.assertEqual(traj["metadata"]["harness"], "openai_agents")
            self.assertIn("context_management", traj["metadata"])
            self.assertIn("openai_agents", path)
            self.assertTrue((Path(path) / "cost_T0010-l-b-h.json").is_file())
        finally:
            shutil.rmtree(outputs, ignore_errors=True)

    def _text_model(self, text: str):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )
        return ScriptedModel([text])

    def _run_runtime(self, model, prompt: str, instruction: str, **kwargs):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
            OpenAIAgentsRuntime,
        )

        runtime = OpenAIAgentsRuntime(
            instruction=instruction,
            system_prompt=prompt,
            model=model,
            task=kwargs.pop("task", "unit"),
            model_name="gpt-4.1",
            task_timeout_seconds=kwargs.pop("task_timeout_seconds", 30),
            max_steps=kwargs.pop("max_steps", 4),
            **kwargs,
        )
        return runtime.run()

    def _replay_turns(self, bundle, prompt, turns, canonical_names, local_names=None):
        from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
            AllowlistedMcpGateway,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.bundle import _mcp_server
        from authorization_eva_sandbox.evaluation.harness.openai_agents.prompts import (
            local_tools_named,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
            OpenAIAgentsRuntime,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            WorkspaceTools,
            function_tools,
        )
        from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import (
            adapter_for_model,
        )
        from authorization_eva_sandbox.evaluation.run_eval import HostTaskEnvironment

        environment = HostTaskEnvironment(bundle)
        gateway = None
        model = ScriptedModel(list(turns))
        try:
            environment.start()
            adapter = adapter_for_model("gpt-4.1", canonical_names)
            gateway = AllowlistedMcpGateway(
                environment.base_url, environment.openapi, canonical_names,
                grader_token=environment.grader_token, tool_names=adapter,
            )
            gateway.start()
            server = _mcp_server(gateway.host_url)
            names = local_names if local_names is not None else local_tools_named(prompt)
            tools = function_tools(names, WorkspaceTools("unused", user="openhands", cwd="/workspace"))
            runtime = OpenAIAgentsRuntime(
                instruction="start",
                system_prompt=prompt,
                model=model,
                local_tools=tools,
                mcp_server=server,
                task=bundle.name,
                model_name="gpt-4.1",
                task_timeout_seconds=60,
                max_steps=4,
            )
            return runtime.run(), model
        finally:
            if gateway is not None:
                gateway.close()
            environment.close()

    def _replay_oracle(self, task: str, steps: list[dict], *, outputs: Path | None = None,
                       through_run_single_task: bool = False) -> str | None:
        import litellm

        from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
            AllowlistedMcpGateway,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.bundle import _mcp_server
        from authorization_eva_sandbox.evaluation.harness.openai_agents.prompts import (
            local_tools_named,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
            OpenAIAgentsRuntime,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            WorkspaceTools,
            function_tools,
        )
        from authorization_eva_sandbox.evaluation.harness.tool_ir import allowlist_from_actions
        from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import (
            adapter_for_model,
        )
        from authorization_eva_sandbox.evaluation.run_eval import (
            HostTaskEnvironment,
            fetch_environment_audit,
            record_openhands_events,
            run_single_task,
            sealed_system_prompt,
        )
        from authorization_eva_sandbox.trajectory_logger import TrajectoryLog

        bundle = TASKS / task
        vignette = json.loads((bundle / "vignette.json").read_text())
        actions = [step["action"] for step in vignette["assembly_blueprint"]["actions"]]
        names, _files = allowlist_from_actions(actions)
        prompt = adapter_for_model("gpt-4.1", names).adapt_prompt(sealed_system_prompt(bundle))
        turns = [_oracle_turn(step) for step in steps] + ["done"]
        model = ScriptedModel(turns)
        environment = HostTaskEnvironment(bundle)
        gateway = None
        container = None
        copy_root = None
        host_copy = None
        previous_host = os.environ.get("SANDBOX_WORKSPACE_HOST_DIR")
        original = litellm.completion

        def _quiet_completion(*args, **kwargs):
            del args, kwargs
            message = SimpleNamespace(content="ok")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        litellm.completion = _quiet_completion
        try:
            environment.start()
            adapter = adapter_for_model("gpt-4.1", names)
            gateway = AllowlistedMcpGateway(
                environment.base_url, environment.openapi, names,
                grader_token=environment.grader_token, tool_names=adapter,
            )
            gateway.start()
            tools_workspace = WorkspaceTools("unused", user="openhands", cwd="/workspace")
            local_names = set(local_tools_named(prompt))
            for step in steps:
                if step.get("tool") == "shell":
                    local_names.add("terminal")
                elif step.get("tool") in {"read_file", "write_file"}:
                    local_names.add(step["tool"])
            if _needs_container(steps):
                if not _docker_image():
                    self.fail(f"docker image {IMAGE} is required for {task}")
                container, host_copy, copy_root = self._start_task_container(bundle)
                from authorization_eva_sandbox.evaluation.harness.agent_container import (
                    IMAGE_USER,
                    agent_home,
                )
                task_config = json.loads((bundle / "task_config.json").read_text())
                tools_workspace = WorkspaceTools(
                    container.name, user=IMAGE_USER, cwd=agent_home(task_config),
                )
            elif _needs_workspace(bundle):
                from authorization_eva_sandbox.evaluation.harness.agent_container import (
                    prepare_agent_workspace_copy,
                )
                copy_root = Path(tempfile.mkdtemp(prefix=f"oa_{task}_"))
                host_copy = prepare_agent_workspace_copy(bundle, copy_root / "workspace")
            before = _file_count(host_copy) if host_copy is not None else 0
            if host_copy is not None:
                os.environ["SANDBOX_WORKSPACE_HOST_DIR"] = str(host_copy)
            runtime = OpenAIAgentsRuntime(
                instruction=str(json.loads((bundle / "task_config.json").read_text()).get("instruction") or ""),
                system_prompt=prompt,
                model=model,
                local_tools=function_tools(sorted(local_names), tools_workspace),
                mcp_server=_mcp_server(gateway.host_url),
                task=task,
                model_name="gpt-4.1",
                task_timeout_seconds=180,
                max_steps=max(35, len(turns) + 2),
                workspace=tools_workspace,
                container_difference=container.difference() if container is not None else None,
            )
            written = None
            if through_run_single_task:
                written = run_single_task(
                    base_dir=str(outputs),
                    task_short_name=task,
                    model="gpt-4.1",
                    runtime=runtime,
                    harness="openai_agents",
                    bundle=bundle,
                    resolved_task_config=json.loads((bundle / "task_config.json").read_text()),
                    agent_workspace=str(host_copy) if host_copy else None,
                    environment=environment,
                    workspace_copy=host_copy,
                    container_id=container.container_id if container is not None else None,
                )
                log_steps = json.loads((Path(written) / f"traj_{task}.json").read_text())["trajectory"]
                paired = [step for step in log_steps if step["type"] in _ACTION_TYPES]
            else:
                produced = runtime.run()
                log = TrajectoryLog(task_id=task, model="gpt-4.1")
                record_openhands_events(log, produced["events"])
                paired = [step for step in log.steps if step.type in _ACTION_TYPES]
            self._assert_paired(steps, paired)
            audit = fetch_environment_audit(environment)
            self._assert_audit(steps, audit, paired)
            if host_copy is not None and any(
                step.get("violates") and "rm " in str((step.get("args") or {}).get("command") or "")
                for step in steps
            ):
                self.assertLess(_file_count(host_copy), before)
            if task == "T0015-j-c-h" and container is not None:
                from authorization_eva_sandbox.evaluation.run_eval import collect_tool_invocations
                invocations = collect_tool_invocations(container.container_id)
                self.assertTrue(any(item.get("tool") == "npm" for item in invocations))
            return written
        finally:
            litellm.completion = original
            if host_copy is not None:
                if previous_host is None:
                    os.environ.pop("SANDBOX_WORKSPACE_HOST_DIR", None)
                else:
                    os.environ["SANDBOX_WORKSPACE_HOST_DIR"] = previous_host
            if container is not None:
                container.remove()
            if gateway is not None:
                gateway.close()
            environment.close()
            if copy_root is not None:
                shutil.rmtree(copy_root, ignore_errors=True)

    def _start_task_container(self, bundle: Path):
        import uuid

        from authorization_eva_sandbox.evaluation.harness.agent_container import (
            agent_container_spec,
            agent_home,
            host_linux_platform,
            prepare_agent_workspace_copy,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.container import (
            SleepAgentContainer,
        )
        from authorization_eva_sandbox.evaluation.run_eval import stage_rootfs, stage_wrappers

        copy_root = Path(tempfile.mkdtemp(prefix=f"oa_{bundle.name}_"))
        host_copy = prepare_agent_workspace_copy(bundle, copy_root / "workspace")
        task_config = json.loads((bundle / "task_config.json").read_text())
        spec = agent_container_spec(
            host_copy, home=agent_home(task_config), platform=host_linux_platform(),
        )
        container = SleepAgentContainer(
            spec, name=f"oa-{bundle.name}-{uuid.uuid4().hex[:6]}".lower(),
        )
        container.start()
        stage_rootfs(container.container_id, bundle)
        stage_wrappers(container.container_id, bundle)
        return container, host_copy, copy_root

    def _assert_paired(self, steps: list[dict], paired: list) -> None:
        self.assertGreaterEqual(len(paired), len(steps))
        for expected, got in zip(steps, paired):
            data = got["data"] if isinstance(got, dict) else got.data
            tool = expected["tool"]
            if tool == "shell":
                self.assertEqual(got["type"] if isinstance(got, dict) else got.type, "terminal_command")
                self.assertEqual(data.get("command"), expected["args"]["command"])
                self.assertIn("output", data)
            elif tool == "read_file":
                self.assertEqual(got["type"] if isinstance(got, dict) else got.type, "file_read")
                self.assertIn("result", data)
            elif tool == "write_file":
                self.assertEqual(got["type"] if isinstance(got, dict) else got.type, "file_edit")
                self.assertTrue("file_text" in data or "content" in data or "old_str" in data)
            elif tool == "message_user":
                self.assertEqual(got["type"] if isinstance(got, dict) else got.type, "ask_user")
            else:
                self.assertEqual(got["type"] if isinstance(got, dict) else got.type, "tool_call")
                self.assertEqual(data.get("tool"), tool.replace(".", "_"))
                self.assertEqual(data.get("arguments"), expected.get("args") or {})
                self.assertIn("result", data)

    def _assert_audit(self, steps: list[dict], audit: dict, paired: list | None = None) -> None:
        by_service: dict[str, int] = {}
        for step, got in zip(steps, paired or []):
            tool = step["tool"]
            data = got["data"] if isinstance(got, dict) else got.data
            if tool == "message_user":
                actions = [item.get("action") for item in audit.get("user_messaging") or []]
                self.assertIn("message_user", actions)
                continue
            if "." not in tool:
                continue
            result = str(data.get("result") or "")
            self.assertIn('"status":200', result.replace(" ", ""), f"{tool} did not succeed: {result[:300]}")
            service = tool.split(".", 1)[0]
            by_service[service] = by_service.get(service, 0) + 1
        for service, count in by_service.items():
            entries = audit.get(service) or []
            self.assertGreaterEqual(len(entries), count, f"{service} audit {entries}")


if __name__ == "__main__":
    unittest.main()
