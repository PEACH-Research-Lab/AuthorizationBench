"""Run one bundle on SandboxAgent, in the task's own container."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from authorization_eva_sandbox.evaluation.harness.agent_container import (
    IMAGE_USER,
    WORKSPACE_HOST_DIR_ENV,
    agent_container_spec,
    agent_home,
    host_linux_platform,
    prepare_agent_workspace_copy,
)
from authorization_eva_sandbox.evaluation.harness.layout import _resolve_outputs_path
from authorization_eva_sandbox.evaluation.harness.openai_agents.bundle import (
    _mcp_server,
    _remove_on_exit,
)
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
from authorization_eva_sandbox.evaluation.harness.openai_sandbox.runtime import (
    SandboxAgentsRuntime,
)
from authorization_eva_sandbox.evaluation.harness.openai_sandbox.spike_common import (
    is_chat_profile,
)
from authorization_eva_sandbox.evaluation.harness.openhands.llm import effort_from_profile
from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
)
from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    DEFAULT_PROMPT_CONDITION,
    compose,
)

HARNESS = "openai_sandbox"


def run_openai_sandbox_bundle(
    bundle: Path,
    outputs_base: Path,
    *,
    profile_name: str = "gemini_3_1_pro",
    task_timeout_seconds: float | None = DEFAULT_TASK_TIMEOUT_SECONDS,
    chat_backend: str = "litellm",
    prompt_condition: str = DEFAULT_PROMPT_CONDITION,
) -> str:
    """Same loop as the plain Agents harness, on a SandboxAgent.

    The session is attached to the running task container and is not shut
    down here. Compaction is not enabled.
    """
    from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
        AllowlistedMcpGateway,
    )
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
        model_for_profile,
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
    task_id = str(task_config.get("task_id") or bundle.name)
    home = agent_home(task_config)
    environment = HostTaskEnvironment(bundle)
    gateway = None
    container: SleepAgentContainer | None = None
    copy_root = Path(tempfile.mkdtemp(prefix=f"osb_{task_id}_"))
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
            spec, name=f"osb-{task_id}-{uuid.uuid4().hex[:8]}".lower(),
        )
        with _remove_on_exit(container):
            container.start()
            assert container.container_id
            stage_rootfs(container.container_id, bundle)
            stage_wrappers(container.container_id, bundle)
            workspace = WorkspaceTools(container.name, user=IMAGE_USER, cwd=home)
            tools = function_tools(openhands_local_tool_names(actions), workspace)
            model, settings, info = model_for_profile(profile, chat_backend=chat_backend)
            outputs_path = _resolve_outputs_path(
                str(outputs_base), task_id, model_name,
                effort_from_profile(profile), harness=HARNESS,
            )
            memories = host_copy / "memories"
            runtime = SandboxAgentsRuntime(
                container_view=_AttachedContainer(container, host_copy),
                chat_path=is_chat_profile(profile),
                prompt_condition=prompt_condition,
                instruction=str(task_config.get("instruction") or ""),
                system_prompt=composition.text,
                model=model,
                model_settings=settings,
                local_tools=tools,
                mcp_server=_mcp_server(gateway.host_url),
                task=task_id,
                model_name=model_name,
                model_info=info.as_dict(),
                max_steps=int((task_config.get("agent") or {}).get("max_steps") or 35),
                task_timeout_seconds=task_timeout_seconds,
                user_reply_fn=oracle_reply_fn(environment.base_url, environment.grader_token),
                workspace=workspace,
                container_difference=container.difference(),
                traj_stream_path=Path(outputs_path) / "traj_stream.jsonl",
                prompt_composition=composition,
            )
            if memories.is_dir():
                runtime.memories_at_start = sorted(
                    path.name for path in memories.iterdir() if not path.name.startswith(".")
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


class _AttachedContainer:
    """The fields ``attach_session`` reads off the spike's TaskContainer."""

    def __init__(self, container: SleepAgentContainer, host_copy: Path):
        self.container_id = str(container.container_id)
        self.image = str(container.spec.image)
        self.host_copy = host_copy
