"""Run one sealed bundle on the OpenAI Agents harness."""
from __future__ import annotations

import json
import os
import shutil
import signal
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager

from authorization_eva_sandbox.evaluation.harness.agent_container import (
    IMAGE_USER,
    WORKSPACE_HOST_DIR_ENV,
    agent_container_spec,
    agent_home,
    host_linux_platform,
    prepare_agent_workspace_copy,
)
from authorization_eva_sandbox.evaluation.harness.condenser_budget import condenser_budget
from authorization_eva_sandbox.evaluation.harness.layout import _resolve_outputs_path
from authorization_eva_sandbox.evaluation.harness.openai_agents.container import (
    SleepAgentContainer,
)
from authorization_eva_sandbox.evaluation.harness.openai_agents.prompts import (
    openhands_local_tool_names,
)
from authorization_eva_sandbox.evaluation.harness.openai_agents.trajectory import (
    record_condensation_in_trajectory_file,
)
from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
    WorkspaceTools,
    function_tools,
)
from authorization_eva_sandbox.evaluation.harness.openhands.llm import effort_from_profile
from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
)
from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    DEFAULT_PROMPT_CONDITION,
    compose,
)

HARNESS = "openai_agents"


def run_openai_agents_bundle(
    bundle: Path,
    outputs_base: Path,
    *,
    profile_name: str = "gemini_3_1_pro",
    task_timeout_seconds: float | None = DEFAULT_TASK_TIMEOUT_SECONDS,
    chat_backend: str = "litellm",
    prompt_condition: str = DEFAULT_PROMPT_CONDITION,
) -> str:
    """Run one sealed bundle. The model client stays on the host.

    The container is ``sleep infinity`` on the same image and workspace copy
    OpenHands uses. It is removed when the run ends, including after an error
    or Ctrl-C. Business tools are the host MCP gateway, not a process in the
    container. A plain ``Agent`` has no prompt of its own, so the native
    condition sends our added text alone and the condenser keeps the task budget.
    """
    from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
        AllowlistedMcpGateway,
    )
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
        model_for_profile,
    )
    from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
        OpenAIAgentsRuntime,
    )
    from authorization_eva_sandbox.evaluation.harness.tool_ir import allowlist_from_actions
    from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import (
        adapter_for_model,
    )
    from authorization_eva_sandbox.evaluation.run_eval import (
        HostTaskEnvironment,
        oracle_reply_fn,
        run_single_task,
        stage_rootfs,
        stage_wrappers,
    )
    from authorization_eva_sandbox.evaluation.run_task import load_llm_profile

    bundle = Path(bundle).resolve()
    task_config = json.loads((bundle / "task_config.json").read_text())
    vignette = json.loads((bundle / "vignette.json").read_text())
    actions = [step["action"] for step in vignette["assembly_blueprint"]["actions"]]
    names, _files = allowlist_from_actions(actions)
    profile = load_llm_profile(profile_name)
    model_name = str(profile["model"])
    tool_names = adapter_for_model(model_name, names)
    composition = compose(bundle, model_name, condition=prompt_condition, harness=HARNESS)
    prompt = composition.text
    task_id = str(task_config.get("task_id") or bundle.name)
    home = agent_home(task_config)
    environment = HostTaskEnvironment(bundle)
    gateway = None
    container: SleepAgentContainer | None = None
    copy_root = Path(tempfile.mkdtemp(prefix=f"oa_{task_id}_"))
    previous_host_dir = os.environ.get(WORKSPACE_HOST_DIR_ENV)
    workspace = None
    try:
        environment.start()
        gateway = AllowlistedMcpGateway(
            environment.base_url,
            environment.openapi,
            names,
            grader_token=environment.grader_token,
            tool_names=tool_names,
        )
        gateway.start()
        host_copy = prepare_agent_workspace_copy(bundle, copy_root / "workspace")
        os.environ[WORKSPACE_HOST_DIR_ENV] = str(host_copy)
        spec = agent_container_spec(host_copy, home=home, platform=host_linux_platform())
        container = SleepAgentContainer(
            spec, name=f"oa-{task_id}-{uuid.uuid4().hex[:8]}".lower(),
        )
        with _remove_on_exit(container):
            container.start()
            assert container.container_id
            stage_rootfs(container.container_id, bundle)
            stage_wrappers(container.container_id, bundle)
            workspace = WorkspaceTools(
                container.name, user=IMAGE_USER, cwd=home,
            )
            tools = function_tools(openhands_local_tool_names(actions), workspace)
            model, settings, info = model_for_profile(profile, chat_backend=chat_backend)
            budget = condenser_budget(
                task_config, condition=prompt_condition, native_text="", model=info.model_id,
            )
            outputs_path = _resolve_outputs_path(
                str(outputs_base), task_id, model_name,
                effort_from_profile(profile), harness=HARNESS,
            )
            server = _mcp_server(gateway.host_url)
            runtime = OpenAIAgentsRuntime(
                instruction=str(task_config.get("instruction") or ""),
                system_prompt=prompt,
                model=model,
                model_settings=settings,
                local_tools=tools,
                mcp_server=server,
                task=task_id,
                model_name=model_name,
                model_info=info.as_dict(),
                max_steps=int((task_config.get("agent") or {}).get("max_steps") or 35),
                task_timeout_seconds=task_timeout_seconds,
                user_reply_fn=oracle_reply_fn(environment.base_url, environment.grader_token),
                condenser=_condenser(task_config, profile, info.model_id, budget),
                workspace=workspace,
                container_difference=container.difference(),
                traj_stream_path=Path(outputs_path) / "traj_stream.jsonl",
                prompt_composition=composition,
                condenser_budget=budget,
            )
            try:
                return run_single_task(
                    base_dir=str(outputs_base),
                    task_short_name=task_id,
                    model=model_name,
                    runtime=runtime,
                    harness=HARNESS,
                    bundle=bundle,
                    resolved_task_config=task_config,
                    agent_workspace=str(host_copy),
                    environment=environment,
                    workspace_copy=host_copy,
                    container_id=container.container_id,
                    outputs_path=outputs_path,
                )
            finally:
                record_condensation_in_trajectory_file(outputs_path, runtime.events)
    finally:
        if workspace is not None:
            workspace.close()
        if gateway is not None:
            gateway.close()
        environment.close()
        if previous_host_dir is None:
            os.environ.pop(WORKSPACE_HOST_DIR_ENV, None)
        else:
            os.environ[WORKSPACE_HOST_DIR_ENV] = previous_host_dir
        shutil.rmtree(copy_root, ignore_errors=True)


def _mcp_server(url: str) -> Any:
    from agents.mcp import MCPServerStreamableHttp

    return MCPServerStreamableHttp(
        params={"url": url},
        cache_tools_list=True,
        name="authorization-bench",
        client_session_timeout_seconds=30,
    )


def _condenser(
    task_config: dict[str, Any],
    profile: dict[str, Any],
    model_id: str,
    budget: dict[str, Any] | None = None,
) -> Any | None:
    agent = task_config.get("agent") or {}
    raw = agent.get("condenser") if isinstance(agent, dict) else None
    if not isinstance(raw, dict) or not raw.get("max_tokens"):
        return None
    from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
        SummarizingCondenser,
    )

    keep = raw.get("keep_first")
    max_tokens = budget["effective_max_tokens"] if budget else raw["max_tokens"]
    return SummarizingCondenser(
        max_tokens=int(max_tokens),
        keep_first=0 if keep is None else int(keep),
        model=model_id,
        summarize=_litellm_summarize(profile, model_id),
    )


def _litellm_summarize(profile: dict[str, Any], model_id: str):
    """One summary call on the agent model. The cost callback records it."""

    def summarize(prompt: str) -> str:
        import litellm

        response = litellm.completion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            api_key=str(profile.get("api_key") or "") or None,
            base_url=profile.get("base_url") or None,
            timeout=150,
            num_retries=0,
            max_retries=0,
        )
        message = response.choices[0].message
        return str(getattr(message, "content", "") or "")

    return summarize


@contextmanager
def _remove_on_exit(container: SleepAgentContainer) -> Iterator[None]:
    """Remove the container on the way out, and on Ctrl-C before the stack unwinds."""
    previous = signal.getsignal(signal.SIGINT)

    def _handler(signum: int, frame: Any) -> None:
        container.remove()
        raise KeyboardInterrupt

    installed = False
    try:
        signal.signal(signal.SIGINT, _handler)
        installed = True
    except ValueError:
        installed = False
    try:
        yield
    finally:
        container.remove()
        if installed:
            signal.signal(signal.SIGINT, previous)
