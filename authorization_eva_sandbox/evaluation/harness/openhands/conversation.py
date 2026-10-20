"""OpenHands agent, conversation, and shell tools. SDK imports stay inside functions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.evaluation.harness.agent_container import (
    CONTAINER_WORKSPACE,
)

# Present so the agent object is valid. It is not a provider key, and this
# step does not send a message or call the model.
PLACEHOLDER_MODEL_KEY = "not-a-model-call"

def open_task_conversation(
    workspace: Any,
    bundle: Path,
    mcp_config: dict[str, dict[str, str]],
    *,
    model: str = "openai/gpt-4.1-mini",
    llm: Any | None = None,
    system_prompt: str | None = None,
    native_suffix: str | None = None,
    tools: list[Any] | None = None,
    callbacks: list[Any] | None = None,
    condenser_max_tokens: int | None = None,
) -> Any:
    """Give the agent server the prompt and the MCP gateway.

    Sealed: ``system_prompt`` (default: the bundle's sealed prompt) replaces
    the SDK template. Native: pass ``native_suffix`` instead; the SDK template
    stays and the suffix follows it (``native_agent_context``).

    ``Conversation`` stores that agent on the server. This does not call
    ``send_message`` or ``run``. Pass ``llm`` only when a later ``run()``
    should call that model. The default LLM is a placeholder and is not called.
    """
    from openhands.sdk import Conversation, LLM
    from pydantic import SecretStr

    from authorization_eva_sandbox.evaluation.harness.openhands_condenser import (
        build_openhands_agent,
    )

    task_config = json.loads((Path(bundle) / "task_config.json").read_text())
    agent_kwargs: dict[str, Any] = {}
    if native_suffix is not None:
        if system_prompt is not None:
            raise ValueError("pass system_prompt (sealed) or native_suffix (native), not both")
        prompt = None
        agent_kwargs["agent_context"] = native_agent_context(native_suffix)
    else:
        if system_prompt is None:
            from authorization_eva_sandbox.evaluation.run_eval import sealed_system_prompt
            system_prompt = sealed_system_prompt(bundle)
        prompt = system_prompt
    if llm is None:
        llm = LLM(model=model, api_key=SecretStr(PLACEHOLDER_MODEL_KEY))
    max_steps = int((task_config.get("agent") or {}).get("max_steps") or 35)
    if tools:
        agent_kwargs["tools"] = tools
    agent = build_openhands_agent(
        task_config,
        llm,
        system_prompt=prompt,
        mcp_config=mcp_config,
        condenser_max_tokens=condenser_max_tokens,
        **agent_kwargs,
    )
    return Conversation(
        agent=agent,
        workspace=workspace,
        max_iteration_per_run=max_steps,
        callbacks=callbacks,
    )


def native_agent_context(suffix: str) -> Any:
    """The SDK's own way to add text after ``system_prompt.j2``: the dynamic block.

    ``current_datetime=None``: an ``AgentContext`` otherwise adds today's host
    date, which the SDK does not send without one and which contradicts a
    task's ``reference_time``. No skills, memory or secrets are loaded.
    """
    from openhands.sdk import AgentContext

    return AgentContext(system_message_suffix=suffix, current_datetime=None)


def native_system_text(llm: Any, tools: list[Any] | None) -> str:
    """The SDK template this agent sends as its first system block, before any suffix."""
    from openhands.sdk import Agent

    return Agent(llm=llm, tools=list(tools or [])).static_system_message


def shell_tools_for_actions(actions: list) -> list[Any]:
    """OpenHands file and terminal tools named by one blueprint.

    These are not MCP tools. ``read_file`` and ``write_file`` match the
    sealed prompt. ``bash`` is the SDK terminal tool.
    """
    from authorization_eva_sandbox.evaluation.harness.tool_ir import (
        _flatten_actions,
    )

    flat = _flatten_actions(actions)
    needed: list[str] = []
    if any(item == "runtime:shell.read_file" for item in flat):
        needed.append("read_file")
    if any(item == "runtime:shell.write_file" for item in flat):
        needed.append("write_file")
    if any(item == "runtime:shell.bash" for item in flat):
        needed.append("terminal")
    if not needed:
        return []
    import openhands.tools.gemini.read_file.definition  # noqa: F401
    import openhands.tools.gemini.write_file.definition  # noqa: F401
    import openhands.tools.terminal.definition  # noqa: F401
    from openhands.sdk.tool import Tool

    return [Tool(name=name) for name in needed]

def stored_remote_agent(workspace: Any, conversation: Any) -> dict[str, Any]:
    """The agent record the server stored for this conversation."""
    response = workspace.client.get(f"/api/conversations/{conversation.id}")
    response.raise_for_status()
    agent = response.json().get("agent")
    if not isinstance(agent, dict):
        raise RuntimeError("agent server did not store an agent")
    return agent


def agent_container_text(workspace: Any, command: str) -> str:
    """Run a command in the agent container and return its combined output."""
    result = workspace.execute_command(command, cwd=CONTAINER_WORKSPACE)
    return f"{result.stdout or ''}{result.stderr or ''}"

