"""Model paths and condenser under SandboxAgent (spike questions 4 and 5). Offline.

A. For all 8 benchmark profiles (``config.toml.example`` wired to the
   recorder), one request from the plain adapter's ``Agent`` and one from
   ``SandboxAgent`` with Shell + Filesystem. Everything but instructions,
   input and tools must be equal; effort, provider lock and the 40000 cap are
   checked as in ``test_openai_agents_models``.
B. What breaks: the stock Filesystem capability on the chat path, and the
   SDK default capabilities (which include Compaction) on both paths.
C. Our ``SummarizingCondenser`` through ``call_model_input_filter`` on a
   SandboxAgent run, with an ``apply_patch`` call in the forgotten range, on
   both paths and with ``keep_first`` 1 and 0.
D. Whether that condenser can separate a Responses ``custom_tool_call`` from
   its output, over a range of budgets.

    .venv-openai-agents/bin/python -m \
        authorization_eva_sandbox.evaluation.harness.openai_sandbox.probe_models
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from authorization_eva_sandbox.evaluation.harness.openai_sandbox import spike_common as sc
from authorization_eva_sandbox.evaluation.harness.openai_sandbox.recorder import Recorder

OUT = sc.CAPTURES / "models"
LOCKS = {"sonnet5": "anthropic", "gemini38_flash": "google-ai-studio", "glm53": "z-ai"}
CAP_FIELDS = ("max_tokens", "max_output_tokens", "max_completion_tokens")
PROMPT_SHAPED = {"instructions", "input", "messages", "tools"}


def _effort_fields(body: dict[str, Any]) -> list[tuple[str, Any]]:
    found = []
    if "reasoning_effort" in body:
        found.append(("reasoning_effort", body["reasoning_effort"]))
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        found.append(("reasoning.effort", reasoning["effort"]))
    return found


def _wired(recorder: Recorder, name: str) -> dict[str, Any]:
    return {**sc.example_profiles()[name], "api_key": "x", "base_url": recorder.base_url}


def _our_tools(tc: sc.TaskContainer) -> tuple[Any, list[Any]]:
    from authorization_eva_sandbox.evaluation.harness.agent_container import IMAGE_USER
    from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
        WorkspaceTools,
        function_tools,
    )

    workspace = WorkspaceTools(tc.container.name, user=IMAGE_USER, cwd="/workspace")
    return workspace, function_tools(["terminal", "finish"], workspace)


async def _single(recorder: Recorder, agent: Any, config: Any) -> dict[str, Any]:
    from agents import Runner

    recorder.load([{"text": "DONE"}])
    await Runner.run(agent, "SPIKE_USER_TASK", max_turns=2, run_config=config)
    return recorder.requests[0]


async def part_a(recorder: Recorder, tc: sc.TaskContainer, session: Any) -> dict[str, Any]:
    from agents import Agent

    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
        model_for_profile,
        run_config,
    )

    out: dict[str, Any] = {}
    workspace, tools = _our_tools(tc)
    try:
        for name in sc.BENCHMARK_PROFILES:
            profile = _wired(recorder, name)
            chat = sc.is_chat_profile(profile)
            model, settings, info = model_for_profile(profile, chat_backend="litellm")
            plain = Agent(
                name="authorization-bench", instructions="SPIKE", tools=list(tools),
                model=model, model_settings=settings,
                tool_use_behavior={"stop_at_tool_names": ["finish"]},
            )
            plain_req = await _single(recorder, plain, run_config())
            model2, settings2, _ = model_for_profile(profile, chat_backend="litellm")
            sandbox = sc.build_agent(
                instructions="SPIKE", model=model2, settings=settings2, tools=list(tools),
                mcp_server=None, chat_path=chat,
            )
            sandbox_req = await _single(recorder, sandbox, sc.sandbox_run_config(session))
            pb, sb = plain_req["body"], sandbox_req["body"]
            rest_plain = {k: v for k, v in pb.items() if k not in PROMPT_SHAPED}
            rest_sandbox = {k: v for k, v in sb.items() if k not in PROMPT_SHAPED}
            base = name.rsplit("_", 1)[0]
            effort = name.rsplit("_", 1)[1]
            fields = _effort_fields(sb)
            caps = {k: sb[k] for k in CAP_FIELDS if k in sb}
            out[name] = {
                "api_path": info.api_path,
                "path": sandbox_req["path"],
                "non_prompt_fields_equal": rest_plain == rest_sandbox,
                "non_prompt_fields": rest_sandbox,
                "diff_keys": sorted(
                    k for k in set(rest_plain) | set(rest_sandbox)
                    if rest_plain.get(k) != rest_sandbox.get(k)
                ),
                "one_effort_field": len(fields) == 1 and fields[0][1] == effort,
                "effort_fields": fields,
                "cap": caps,
                "cap_is_40000": list(caps.values()) == [40000],
                "provider_lock_ok": (
                    sb.get("provider") == {"only": [LOCKS[base]], "allow_fallbacks": False,
                                           "require_parameters": True}
                    if base in LOCKS else "provider" not in sb
                ),
                "tools_plain": [_tool_name(t) for t in pb.get("tools") or []],
                "tools_sandbox": [_tool_name(t) for t in sb.get("tools") or []],
            }
    finally:
        workspace.close()
    return out


def _tool_name(tool: dict[str, Any]) -> str:
    if "function" in tool:
        return f"{tool.get('type')}:{tool['function'].get('name')}"
    return f"{tool.get('type')}:{tool.get('name')}"


async def part_b(recorder: Recorder, session: Any) -> dict[str, Any]:
    from agents.sandbox.capabilities import Capabilities, Filesystem, Shell

    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import model_for_profile

    out: dict[str, Any] = {}
    for label, name, caps in (
        ("chat_stock_filesystem", "sonnet5_high", [Shell(), Filesystem()]),
        ("responses_default_capabilities", "gpt5_6_sol_high", Capabilities.default()),
        ("chat_default_capabilities", "glm53_high", Capabilities.default()),
    ):
        profile = _wired(recorder, name)
        model, settings, _ = model_for_profile(profile, chat_backend="litellm")
        agent = sc.build_agent(
            instructions="SPIKE", model=model, settings=settings, tools=[],
            mcp_server=None, chat_path=True, caps=caps,
        )
        try:
            req = await _single(recorder, agent, sc.sandbox_run_config(session))
            body = req["body"]
            out[label] = {
                "result": "request sent",
                "context_management": body.get("context_management"),
                "tools": [_tool_name(t) for t in body.get("tools") or []],
            }
        except Exception as exc:  # noqa: BLE001
            out[label] = {"result": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return out


def _pairing_problems(body: dict[str, Any]) -> list[str]:
    """Calls without outputs and outputs without calls, in one request."""
    problems = []
    if "input" in body:
        calls = {i.get("call_id"): i.get("type") for i in body["input"]
                 if isinstance(i, dict) and i.get("type") in ("function_call", "custom_tool_call")}
        outs = {i.get("call_id"): i.get("type") for i in body["input"]
                if isinstance(i, dict) and i.get("type") in ("function_call_output", "custom_tool_call_output")}
    else:
        calls, outs = {}, {}
        for m in body.get("messages") or []:
            for call in m.get("tool_calls") or []:
                calls[call.get("id")] = "tool_call"
            if m.get("role") == "tool":
                outs[m.get("tool_call_id")] = "tool"
    for cid, kind in calls.items():
        if cid not in outs:
            problems.append(f"{kind} {cid} has no output")
    for cid, kind in outs.items():
        if cid not in calls:
            problems.append(f"{kind} {cid} has no call")
    return problems


async def part_c(recorder: Recorder, tc: sc.TaskContainer, session: Any) -> dict[str, Any]:
    from agents import Runner
    from agents.run_config import ModelInputData

    from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
        SummarizingCondenser,
    )
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import model_for_profile
    from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
        _openai_tool_schemas,
        _plain_item,
    )

    out: dict[str, Any] = {}
    big = "python3 -c \"print('lorem ipsum dolor ' * 700)\""
    for name in ("gpt5_6_sol_high", "sonnet5_high"):
        for keep_first in (1, 0):
            profile = _wired(recorder, name)
            chat = sc.is_chat_profile(profile)
            model, settings, info = model_for_profile(profile, chat_backend="litellm")
            workspace, tools = _our_tools(tc)
            patch = f"*** Begin Patch\n*** Add File: cond_{name}_{keep_first}.txt\n+X\n*** End Patch\n"
            patch_call = (
                {"name": "apply_patch", "arguments": {"input": patch}}
                if chat else {"name": "apply_patch", "arguments": patch, "custom": True}
            )
            script = [
                {"calls": [{"name": "exec_command", "arguments": {"cmd": big}}]},
                {"calls": [patch_call]},
                {"calls": [{"name": "exec_command", "arguments": {"cmd": big}}]},
                {"calls": [{"name": "exec_command", "arguments": {"cmd": big}}]},
                {"calls": [{"name": "exec_command", "arguments": {"cmd": big}}]},
                {"text": "DONE"},
            ]
            summaries: list[str] = []

            def summarize(prompt: str) -> str:
                summaries.append(prompt)
                return "SPIKE_SUMMARY"

            condenser = SummarizingCondenser(
                max_tokens=9000, keep_first=keep_first, model=info.model_id, summarize=summarize,
            )
            seen: list[dict[str, Any]] = []

            def input_filter(data: Any) -> Any:
                if not condenser.tools:
                    condenser.tools = _openai_tool_schemas(list(data.agent.tools))
                md = data.model_data
                items = [_plain_item(i) for i in md.input]
                result = condenser.apply(md.instructions, items)
                seen.append({
                    "instructions_chars_in": len(md.instructions or ""),
                    "instructions_chars_out": len(result.instructions or ""),
                    "items_in": len(items),
                    "items_out": len(result.input_items),
                    "item_types_in": [i.get("type") or i.get("role") for i in items if isinstance(i, dict)],
                    "condensed": result.record is not None,
                    "trigger_tokens": result.record.trigger_tokens if result.record else None,
                    "forgotten_kinds": [f.kind for f in result.record.forgotten] if result.record else None,
                    "agent_tool_names": [getattr(t, "name", None) for t in data.agent.tools],
                })
                return ModelInputData(input=result.input_items, instructions=result.instructions)

            agent = sc.build_agent(
                instructions="SPIKE", model=model, settings=settings, tools=tools,
                mcp_server=None, chat_path=chat,
            )
            config = sc.sandbox_run_config(session)
            config.call_model_input_filter = input_filter
            recorder.load(script)
            error = None
            try:
                await Runner.run(agent, "SPIKE_USER_TASK", max_turns=10, run_config=config)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {str(exc)[:400]}"
            finally:
                workspace.close()
            requests = []
            for req in recorder.requests:
                body = req["body"]
                instr = body.get("instructions")
                if instr is None and body.get("messages") and body["messages"][0].get("role") == "system":
                    instr = body["messages"][0].get("content")
                requests.append({
                    "instructions_chars": len(json.dumps(instr)) if instr else 0,
                    "has_summary": "SPIKE_SUMMARY" in json.dumps(body),
                    "pairing_problems": _pairing_problems(body),
                })
            out[f"{name}_keep{keep_first}"] = {
                "api_path": info.api_path,
                "error": error,
                "filter_calls": seen,
                "requests": requests,
                "summaries_requested": len(summaries),
                "patch_in_summary_prompt": any("Begin Patch" in s for s in summaries),
            }
    return out


def part_d() -> dict[str, Any]:
    """Can the condenser cut between a Responses ``custom_tool_call`` and its output?

    Synthetic Responses input with one apply_patch call between large shell
    results, condensed at every budget from 1500 to 9000 tokens.
    """
    from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
        SummarizingCondenser,
    )

    big = "lorem ipsum dolor " * 350

    def fc(i: int) -> list[dict[str, Any]]:
        return [
            {"type": "function_call", "call_id": f"f{i}", "name": "exec_command",
             "arguments": json.dumps({"cmd": f"step {i}"})},
            {"type": "function_call_output", "call_id": f"f{i}", "output": big},
        ]

    items: list[dict[str, Any]] = [{"role": "user", "content": "task"}, *fc(1)]
    items += [
        {"type": "custom_tool_call", "call_id": "c2", "name": "apply_patch",
         "input": "*** Begin Patch\n*** Add File: a.txt\n+A\n*** End Patch\n"},
        {"type": "custom_tool_call_output", "call_id": "c2", "output": "Created a.txt"},
    ]
    items += [*fc(3), *fc(4), *fc(5)]
    out: dict[str, Any] = {}
    for keep_first in (1, 0):
        splits = []
        for budget in range(1500, 9001, 100):
            condenser = SummarizingCondenser(
                max_tokens=budget, keep_first=keep_first, model="gpt-5.6-sol",
                summarize=lambda _prompt: "S",
            )
            result = condenser.apply("SYSTEM " * 300, list(items))
            kept = {i.get("type"): i for i in result.input_items if isinstance(i, dict)}
            if ("custom_tool_call" in kept) != ("custom_tool_call_output" in kept):
                splits.append(budget)
        out[f"keep{keep_first}"] = {"budgets_that_split_the_pair": splits}
    return out


async def main() -> None:
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import disable_tracing

    disable_tracing()
    OUT.mkdir(parents=True, exist_ok=True)
    tc = sc.TaskContainer(None).start()
    summary: dict[str, Any] = {}
    try:
        session = await sc.attach_session(tc)
        with Recorder(OUT / "requests.jsonl") as recorder:
            summary["a_wire_parity"] = await part_a(recorder, tc, session)
            summary["b_breakage"] = await part_b(recorder, session)
            summary["c_condenser"] = await part_c(recorder, tc, session)
        summary["d_custom_call_split"] = part_d()
    finally:
        tc.close()
    sc.write_json(OUT / "summary.json", summary)
    print(json.dumps(summary, indent=1, default=str)[:30000])


if __name__ == "__main__":
    asyncio.run(main())
