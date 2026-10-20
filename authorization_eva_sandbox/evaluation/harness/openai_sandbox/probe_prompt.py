"""Final instructions and tool list for real tasks (spike questions 2, 3 and 5). Offline.

For each task, boots the task environment and the allowlisted MCP gateway,
starts the task container, attaches the sandbox session, and sends one
request per API path to the recorder. Saves the exact instructions and tools
that were sent, their token counts per benchmark model, and two variants of
the filesystem section: a manifest entry with a description, and the section
removed by ``call_model_input_filter``.

    .venv-openai-agents/bin/python -m \
        authorization_eva_sandbox.evaluation.harness.openai_sandbox.probe_prompt
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from authorization_eva_sandbox.evaluation.harness.openai_sandbox import spike_common as sc
from authorization_eva_sandbox.evaluation.harness.openai_sandbox.recorder import Recorder

TASKS = ("T0028-j-b-h", "T0015-j-c-h")
PATHS = {"responses": "gpt5_6_sol_high", "litellm_chat": "sonnet5_high"}
TOKEN_MODELS = {
    "gpt5_6_sol": "gpt-5.6-sol",
    "sonnet5": "openrouter/anthropic/claude-sonnet-5",
    "gemini38_flash": "openrouter/google/gemini-3.8-flash",
    "glm53": "openrouter/z-ai/glm-5.3",
}
OUT = sc.CAPTURES / "prompt"


def sent_instructions(body: dict[str, Any]) -> str:
    if body.get("instructions") is not None:
        return str(body["instructions"])
    messages = body.get("messages") or []
    if messages and messages[0].get("role") == "system":
        content = messages[0].get("content")
        if isinstance(content, list):
            return "".join(block.get("text", "") for block in content if isinstance(block, dict))
        return str(content)
    return ""


def sent_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for tool in body.get("tools") or []:
        if "function" in tool:
            fn = tool["function"]
            out.append({"type": "function", "name": fn.get("name"),
                        "description": fn.get("description"), "parameters": fn.get("parameters")})
        else:
            out.append({"type": tool.get("type"), "name": tool.get("name"),
                        "description": tool.get("description"), "parameters": tool.get("parameters"),
                        "format": tool.get("format")})
    return out


def sections(text: str) -> list[dict[str, Any]]:
    """Top-level ``# `` headings of the sent instructions, with their offsets."""
    out = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("# "):
            out.append({"heading": line.strip(), "offset": offset})
        offset += len(line)
    return out


def token_counts(text: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    import litellm

    schemas = [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description") or "",
        "parameters": t.get("parameters") or {"type": "object", "properties": {}},
    }} for t in tools]
    counts = {}
    for key, model in TOKEN_MODELS.items():
        counts[key] = {
            "instructions": litellm.token_counter(model=model, text=text),
            "instructions_plus_tools": litellm.token_counter(
                model=model, messages=[{"role": "system", "content": text}], tools=schemas,
            ),
        }
    return counts


async def _one_request(recorder: Recorder, *, profile_name: str, instructions: str, tools: list[Any],
                       server: Any, session: Any, input_filter: Any = None) -> dict[str, Any]:
    from agents import Runner

    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import model_for_profile

    profile = {**sc.example_profiles()[profile_name], "api_key": "x", "base_url": recorder.base_url}
    chat = sc.is_chat_profile(profile)
    model, settings, _info = model_for_profile(profile, chat_backend="litellm")
    agent = sc.build_agent(
        instructions=instructions, model=model, settings=settings, tools=tools,
        mcp_server=server, chat_path=chat,
    )
    config = sc.sandbox_run_config(session)
    if input_filter is not None:
        config.call_model_input_filter = input_filter
    recorder.load([{"text": "DONE"}])
    await Runner.run(agent, "SPIKE_USER_TASK", max_turns=2, run_config=config)
    return recorder.requests[0]["body"]


def _drop_filesystem(data: Any) -> Any:
    from agents.run_config import ModelInputData

    text = data.model_data.instructions or ""
    head, sep, _tail = text.rpartition("\n\n# Filesystem\n")
    return ModelInputData(input=data.model_data.input, instructions=head if sep else text)


async def probe_task(task_id: str, recorder: Recorder) -> dict[str, Any]:
    from agents.mcp import MCPServerStreamableHttp

    from authorization_eva_sandbox.evaluation.harness.agent_container import IMAGE_USER
    from authorization_eva_sandbox.evaluation.harness.mcp_gateway import AllowlistedMcpGateway
    from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
        WorkspaceTools,
        function_tools,
    )
    from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import adapter_for_model
    from authorization_eva_sandbox.evaluation.run_eval import HostTaskEnvironment

    bundle = sc.task_bundle(task_id)
    result: dict[str, Any] = {"task": task_id}
    environment = HostTaskEnvironment(bundle)
    environment.start()
    tc = sc.TaskContainer(task_id).start()
    gateway = None
    try:
        session = await sc.attach_session(tc)
        for label, profile_name in PATHS.items():
            model_name = str(sc.example_profiles()[profile_name]["model"])
            ours, names, local = sc.compose_instructions(bundle, model_name)
            gateway = AllowlistedMcpGateway(
                environment.base_url, environment.openapi, names,
                grader_token=environment.grader_token,
                tool_names=adapter_for_model(model_name, names),
            )
            gateway.start()
            server = MCPServerStreamableHttp(
                params={"url": gateway.host_url}, cache_tools_list=True,
                name="authorization-bench", client_session_timeout_seconds=30,
            )
            await server.connect()
            workspace = WorkspaceTools(tc.container.name, user=IMAGE_USER, cwd="/workspace")
            tools = function_tools(local, workspace)
            try:
                body = await _one_request(
                    recorder, profile_name=profile_name, instructions=ours, tools=tools,
                    server=server, session=session,
                )
                stripped = await _one_request(
                    recorder, profile_name=profile_name, instructions=ours, tools=tools,
                    server=server, session=session, input_filter=_drop_filesystem,
                )
            finally:
                workspace.close()
                await server.cleanup()
                gateway.close()
                gateway = None
            text = sent_instructions(body)
            tools_sent = sent_tools(body)
            names_sent = [t["name"] for t in tools_sent]
            stem = f"{task_id}_{label}"
            (OUT / f"{stem}_instructions.txt").write_text(text)
            sc.write_json(OUT / f"{stem}_tools.json", tools_sent)
            if label == "responses":
                sc.EVIDENCE.mkdir(parents=True, exist_ok=True)
                (sc.EVIDENCE / f"{task_id}_instructions.txt").write_text(text)
            our_at = text.find(ours)
            result[label] = {
                "profile": profile_name,
                "model": model_name,
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode()).hexdigest()[:16],
                "sections": sections(text),
                "ours_verbatim_once": text.count(ours) == 1,
                "ours_offset": our_at,
                "base_is_prompt_md_prefix": text.startswith(_prompt_md()),
                "filesystem_section": text[text.rfind("# Filesystem"):],
                "tools": names_sent,
                "duplicate_tool_names": sorted({n for n in names_sent if names_sent.count(n) > 1}),
                "prompt_tool_line": _prompt_tool_line(ours),
                "tool_types": {t["name"]: t["type"] for t in tools_sent},
                "tokens": token_counts(text, tools_sent),
                "tokens_ours_only": token_counts(ours, []),
                "filter_removed_filesystem": "# Filesystem" not in sent_instructions(stripped),
                "filter_removed_chars": len(text) - len(sent_instructions(stripped)),
                "body_keys": sorted(body.keys()),
            }
    finally:
        tc.close()
        if gateway is not None:
            gateway.close()
        environment.close()
    return result


def _prompt_md() -> str:
    from agents.sandbox.runtime_agent_preparation import get_default_sandbox_instructions

    return get_default_sandbox_instructions() or ""


def _prompt_tool_line(prompt: str) -> list[str]:
    from authorization_eva_sandbox.evaluation.harness.openai_agents.prompts import prompt_tool_names

    return prompt_tool_names(prompt)


def tree_with_entry() -> str:
    """The filesystem section when the manifest carries an entry with a description."""
    from agents.sandbox import Dir, Manifest
    from agents.sandbox.runtime_agent_preparation import _filesystem_instructions

    manifest = Manifest(root="/workspace", entries={
        "fixtures": Dir(description="task fixtures"),
    })
    return _filesystem_instructions(manifest)


async def main() -> None:
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import disable_tracing

    disable_tracing()
    OUT.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "prompt_md_chars": len(_prompt_md()),
        "tree_with_manifest_entry": tree_with_entry(),
        "tasks": [],
    }
    with Recorder(OUT / "requests.jsonl") as recorder:
        for task_id in TASKS:
            summary["tasks"].append(await probe_task(task_id, recorder))
    sc.write_json(OUT / "summary.json", summary)
    print(json.dumps(summary, indent=1, default=str)[:15000])


if __name__ == "__main__":
    asyncio.run(main())
