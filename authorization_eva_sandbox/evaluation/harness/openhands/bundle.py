"""Run one sealed bundle on the OpenHands harness."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from authorization_eva_sandbox.evaluation.harness.agent_container import (
    WORKSPACE_HOST_DIR_ENV,
)
from authorization_eva_sandbox.evaluation.harness.layout import _resolve_outputs_path
from authorization_eva_sandbox.evaluation.harness.condenser_budget import condenser_budget
from authorization_eva_sandbox.evaluation.harness.openhands.conversation import (
    native_system_text,
    open_task_conversation,
    shell_tools_for_actions,
)
from authorization_eva_sandbox.evaluation.harness.openhands.llm import (
    effort_from_profile,
    llm_from_profile,
)
from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    OpenHandsLocalRuntime,
)
from authorization_eva_sandbox.evaluation.harness.openhands.workspace import (
    OpenHandsDockerWorkspace,
)
from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    DEFAULT_PROMPT_CONDITION,
    SEALED,
    check_condition,
    compose,
)

HARNESS = "openhands"


def run_openhands_bundle(
    bundle: Path,
    outputs_base: Path,
    *,
    profile_name: str = "gemini_3_1_pro",
    task_timeout_seconds: float | None = DEFAULT_TASK_TIMEOUT_SECONDS,
    prompt_condition: str = DEFAULT_PROMPT_CONDITION,
) -> str:
    """Run one sealed bundle in DockerWorkspace and write the June artifacts."""
    check_condition(prompt_condition)
    from authorization_eva_sandbox.evaluation.run_eval import (
        HostNativeTrajectory,
        HostTaskEnvironment,
        agent_mcp_config,
        oracle_reply_fn,
        run_single_task,
        stage_rootfs,
        stage_wrappers,
    )
    import tempfile

    from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
        AllowlistedMcpGateway,
    )
    from authorization_eva_sandbox.evaluation.harness.tool_ir import (
        allowlist_from_actions,
    )
    from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import (
        adapter_for_model,
    )
    from authorization_eva_sandbox.evaluation.run_task import load_llm_profile

    bundle = Path(bundle).resolve()
    task_config = json.loads((bundle / "task_config.json").read_text())
    vignette = json.loads((bundle / "vignette.json").read_text())
    actions = [
        step["action"] for step in vignette["assembly_blueprint"]["actions"]
    ]
    names, _files = allowlist_from_actions(actions)
    shell_tools = shell_tools_for_actions(actions)
    profile = load_llm_profile(profile_name)
    model_name = str(profile["model"])
    tool_names = adapter_for_model(model_name, names)
    composition = compose(bundle, model_name, condition=prompt_condition, harness=HARNESS)
    llm = llm_from_profile(profile)
    native_text = None
    budget = None
    if composition.condition == SEALED:
        prompt_kwargs = {"system_prompt": composition.text}
    else:
        native_text = native_system_text(llm, shell_tools)
        budget = condenser_budget(
            task_config, condition=composition.condition,
            native_text=native_text, model=model_name,
        )
        prompt_kwargs = {"native_suffix": composition.text}
    task_id = str(task_config.get("task_id") or bundle.name)
    environment = HostTaskEnvironment(bundle)
    gateway = None
    docker = None
    copy_root = Path(tempfile.mkdtemp(prefix=f"oh_{task_id}_"))
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
        docker = OpenHandsDockerWorkspace(bundle, copy_root / "workspace")
        # Host-side services that read the agent's /workspace (docs_portal)
        # resolve it to the copy mounted into the container.
        previous_host_dir = os.environ.get(WORKSPACE_HOST_DIR_ENV)
        os.environ[WORKSPACE_HOST_DIR_ENV] = str(docker.host_copy)
        workspace = docker.start()
        stage_rootfs(workspace._container_id, bundle)
        stage_wrappers(workspace._container_id, bundle)
        outputs_path = _resolve_outputs_path(str(outputs_base), task_id, model_name,
                                             effort_from_profile(profile))
        native_trajectory = HostNativeTrajectory(
            outputs_path, task_id, str(docker.host_copy),
        )
        conversation = open_task_conversation(
            workspace,
            bundle,
            agent_mcp_config(gateway.port),
            llm=llm,
            tools=shell_tools,
            callbacks=[native_trajectory.append],
            condenser_max_tokens=budget["effective_max_tokens"] if budget else None,
            **prompt_kwargs,
        )
        runtime = OpenHandsLocalRuntime(
            conversation,
            str(task_config.get("instruction") or ""),
            task=task_id,
            model=model_name,
            task_timeout_seconds=task_timeout_seconds,
            user_reply_fn=oracle_reply_fn(environment.base_url, environment.grader_token),
            prompt_composition=composition,
            native_text=native_text,
            condenser_budget=budget,
        )
        return run_single_task(
            base_dir=str(outputs_base),
            task_short_name=task_id,
            model=model_name,
            runtime=runtime,
            bundle=bundle,
            resolved_task_config=task_config,
            agent_workspace=str(docker.host_copy),
            environment=environment,
            workspace_copy=docker.host_copy,
            container_id=workspace._container_id,
            outputs_path=outputs_path,
        )
    finally:
        if docker is not None:
            docker.close()
            if previous_host_dir is None:
                os.environ.pop(WORKSPACE_HOST_DIR_ENV, None)
            else:
                os.environ[WORKSPACE_HOST_DIR_ENV] = previous_host_dir
        if gateway is not None:
            gateway.close()
        environment.close()
        shutil.rmtree(copy_root, ignore_errors=True)

