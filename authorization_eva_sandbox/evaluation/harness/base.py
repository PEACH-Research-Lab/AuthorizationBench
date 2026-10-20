"""HarnessAdapter contract (see docs/v4/HARNESS-CAPABILITY-MATRIX.md).

Hard requirements for every adapter:
  - consume a RunSpec (task bundle already staged; environment API live);
  - expose the bundle's tools[] to the agent (native / MCP / SKILL+curl);
  - place the task context block in its best available prompt slot;
  - produce a final TrajectoryLog (v2 schema) and append per-step lines to
    <run_dir>/traj_stream.jsonl when capable (event_stream == "live");

Everything else (condensation, reasoning capture, cost reporting) is a
declared capability, recorded in trajectory.harness.capabilities.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import copy
from typing import Any, Optional


@dataclass(frozen=True)
class AgentTaskView:
    """The only task configuration an adapter may expose to an agent.

    Benchmark-only fields (criteria, violated actions, detection policy,
    Oracle script, trigger labels) deliberately have no representation here.
    """

    instruction: str = ""
    agent_context: dict = field(default_factory=dict)
    services: tuple[dict, ...] = ()
    tools: tuple[dict, ...] = ()
    workspace_boundary_root: str = "/workspace"
    enforce_workspace_boundary: bool = True
    agent: dict = field(default_factory=dict)
    prior_memory: str = ""

    @classmethod
    def from_task_config(cls, config: dict) -> "AgentTaskView":
        return cls(
            instruction=str(config.get("instruction") or ""),
            agent_context=copy.deepcopy(config.get("agent_context") or {}),
            services=tuple(copy.deepcopy(config.get("services") or [])),
            tools=tuple(copy.deepcopy(config.get("tools") or [])),
            workspace_boundary_root=str(
                config.get("workspace_boundary_root") or "/workspace"
            ),
            enforce_workspace_boundary=bool(
                config.get("enforce_workspace_boundary", True)
            ),
            agent=copy.deepcopy(config.get("agent") or {}),
            prior_memory=str(config.get("prior_memory") or ""),
        )

    def tool_config(self) -> dict:
        """Minimal mapping consumed by Tool IR generation."""
        return {"tools": [copy.deepcopy(t) for t in self.tools]}


@dataclass(frozen=True)
class GraderEnvironmentClient:
    """Authenticated private environment control-plane client."""

    base_url: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, *, timeout: float = 15):
        import requests

        return requests.get(
            f"{self.base_url}{path}", headers=self.headers, timeout=timeout
        )

    def post(self, path: str, *, json: Optional[dict] = None,
             timeout: float = 15):
        import requests

        return requests.post(
            f"{self.base_url}{path}", headers=self.headers,
            json=json or {}, timeout=timeout
        )


@dataclass
class RunSpec:
    """Everything an adapter needs to run one task."""

    task_id: str
    task_view: AgentTaskView          # model-safe projection only
    run_dir: str                      # where traj_stream.jsonl / outputs go
    env_base_url: str                 # environment API, e.g. http://127.0.0.1:19100
    container_name: str               # sandbox container (for shell tools)
    grader: GraderEnvironmentClient
    container_user: str = "agent"
    # Agent model (adapter-specific meaning; builtin loop uses litellm):
    model: str = ""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_steps: int = 35
    condenser: Optional[dict] = None  # {"max_size": 28, "keep_first": 0} | None
    native_tools: bool = True         # False -> text-fallback tool parsing
    extra: dict = field(default_factory=dict)


@dataclass
class RunResult:
    trajectory_log: Any               # TrajectoryLog instance
    stop_reason: str                  # finished | max_steps | error
    final_message: str = ""
    error: Optional[str] = None
    cost: dict = field(default_factory=dict)


class HarnessAdapter(ABC):
    """One agent harness. Capabilities are declared, not inferred."""

    name: str = "base"
    tool_tier: str = "native"            # native | mcp | skill_curl
    event_stream: str = "live"           # live | backfill
    condensation: str = "none"           # free-form label

    def capabilities(self) -> dict:
        return {"event_stream": self.event_stream,
                "condensation": self.condensation}

    def harness_block(self, version: str = "") -> dict:
        """The trajectory.harness block (trajectory-log-v2.md)."""
        return {"name": self.name, "version": version,
                "tool_tier": self.tool_tier,
                "capabilities": self.capabilities()}

    @abstractmethod
    def run(self, spec: RunSpec) -> RunResult:
        ...
