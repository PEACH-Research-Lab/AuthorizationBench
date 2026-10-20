"""SandboxAgent runtime. The segment loop is the plain Agents runtime.

Compaction is off. The live session is the caller's; this runtime does not
shut it down.
"""
from __future__ import annotations

from typing import Any

from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
    OpenAIAgentsRuntime,
)
from authorization_eva_sandbox.evaluation.harness.prompt_composition import SEALED


class SandboxAgentsRuntime(OpenAIAgentsRuntime):
    def __init__(self, *, container_view: Any, chat_path: bool, prompt_condition: str, **kwargs: Any):
        super().__init__(condenser=None, condenser_budget=None, **kwargs)
        self.container_view = container_view
        self.session: Any = None
        self.chat_path = chat_path
        self.prompt_condition = prompt_condition
        self.memories_at_start: list[str] = []

    def _metadata(self) -> dict[str, Any]:
        meta = super()._metadata()
        meta["harness"] = "openai_sandbox"
        meta["apply_patch_form"] = "function" if self.chat_path else "custom"
        meta["memories_at_start"] = list(self.memories_at_start)
        return meta

    async def _run(self) -> None:
        from authorization_eva_sandbox.evaluation.harness.openai_sandbox.spike_common import (
            attach_session,
        )

        self.session = await attach_session(self.container_view)
        await super()._run()

    def _make_agent(self, gated: Any, settings: Any, behavior: Any) -> Any:
        from agents.sandbox import SandboxAgent

        from authorization_eva_sandbox.evaluation.harness.openai_sandbox.spike_common import (
            capabilities,
        )

        instructions = None if self.prompt_condition == SEALED else self.system_prompt
        base = self.system_prompt if self.prompt_condition == SEALED else None
        return SandboxAgent(
            name="authorization-bench",
            instructions=instructions,
            base_instructions=base,
            tools=list(self.local_tools),
            mcp_servers=[self.mcp_server] if self.mcp_server is not None else [],
            mcp_config={
                "convert_schemas_to_strict": False,
                "include_server_in_tool_names": False,
            },
            model=gated,
            model_settings=settings,
            tool_use_behavior=behavior,
            capabilities=capabilities(chat_path=self.chat_path),
        )

    def _run_config(self) -> Any:
        from agents.sandbox import SandboxRunConfig

        config = super()._run_config()
        config.sandbox = SandboxRunConfig(session=self.session)
        return config
