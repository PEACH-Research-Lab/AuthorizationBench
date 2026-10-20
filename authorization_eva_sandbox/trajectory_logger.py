from __future__ import annotations

"""
Trajectory Logger: records all agent actions into a structured JSON log.

Three sources feed the log:
1. OpenHands runtime — browser actions, terminal commands, ask_user interactions
2. Oracle User — classification results + responses
3. Web app templates — internal action logs (reads, writes, deletes via web UI)
"""
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class TrajectoryStep:
    step: int
    timestamp: float
    type: str  # browser_action | terminal_command | ask_user | npc_interaction | webapp_action
    data: dict = field(default_factory=dict)


@dataclass
class TrajectoryLog:
    task_id: str
    model: str
    timestamp_start: float = field(default_factory=time.time)
    timestamp_end: Optional[float] = None
    steps: list[TrajectoryStep] = field(default_factory=list)
    oracle_interactions: list[dict] = field(default_factory=list)
    npc_interactions: list[dict] = field(default_factory=list)
    web_app_action_logs: dict[str, list[dict]] = field(default_factory=dict)
    external_integrity: dict = field(default_factory=dict)
    workspace_file_snapshots: dict[str, list[dict]] = field(default_factory=dict)
    # Free-form bag for run-level diagnostics that do not fit the structured
    # fields above (context management, cost, termination, errors).
    metadata: dict[str, Any] = field(default_factory=dict)
    # Producing-harness identity block used by the normalized V4 trajectory.
    # {name, version, tool_tier, capabilities}. Empty for legacy v1 logs.
    harness: dict[str, Any] = field(default_factory=dict)
    # v2: per-service final-state findings written by the service_state detector.
    service_state_diff: dict[str, Any] = field(default_factory=dict)

    def add_step(self, step_type: str, **data) -> TrajectoryStep:
        step = TrajectoryStep(
            step=len(self.steps) + 1,
            timestamp=time.time(),
            type=step_type,
            data=data,
        )
        self.steps.append(step)
        return step

    def add_browser_action(self, action: str, url: str = "", element: str = "",
                           screenshot_path: str = "", **extra):
        return self.add_step(
            "browser_action",
            action=action,
            url=url,
            element=element,
            screenshot_path=screenshot_path,
            **extra,
        )

    def add_terminal_command(self, command: str, output: str = "",
                            exit_code: int = 0, **extra):
        return self.add_step(
            "terminal_command",
            command=command,
            output=output,
            exit_code=exit_code,
            **extra,
        )

    def add_ask_user(
        self,
        agent_message: str,
        oracle_response: Optional[str],
        responded: Optional[bool] = None,
    ):
        if responded is None:
            responded = oracle_response is not None
        step = self.add_step(
            "ask_user",
            agent_message=agent_message,
            oracle_response=oracle_response,
            responded=responded,
        )
        self.oracle_interactions.append(step.data)
        return step

    def add_npc_interaction(self, npc_name: str, platform: str,
                            agent_message: str, npc_response: str, **extra):
        step = self.add_step(
            "npc_interaction",
            npc_name=npc_name,
            platform=platform,
            agent_message=agent_message,
            npc_response=npc_response,
            **extra,
        )
        self.npc_interactions.append(step.data)
        return step

    def add_webapp_action(self, app_name: str, action: str, **details):
        if app_name not in self.web_app_action_logs:
            self.web_app_action_logs[app_name] = []
        entry = {"action": action, "timestamp": time.time(), **details}
        self.web_app_action_logs[app_name].append(entry)
        return self.add_step("webapp_action", app_name=app_name, action=action, **details)

    def finalize(self):
        if self.timestamp_end is None:
            self.timestamp_end = time.time()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "model": self.model,
            "harness": self.harness,
            "service_state_diff": self.service_state_diff,
            "timestamp_start": self.timestamp_start,
            "timestamp_end": self.timestamp_end,
            "duration_seconds": (
                (self.timestamp_end - self.timestamp_start)
                if self.timestamp_end
                else None
            ),
            "total_steps": len(self.steps),
            "trajectory": [asdict(s) for s in self.steps],
            "oracle_interactions": self.oracle_interactions,
            "npc_interactions": self.npc_interactions,
            "web_app_action_logs": self.web_app_action_logs,
            "external_integrity": self.external_integrity,
            "workspace_file_snapshots": self.workspace_file_snapshots,
            "metadata": self.metadata,
        }

    def save(self, path: str):
        self.finalize()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def from_openhands_trajectory(
        cls, openhands_traj_path: str, task_id: str, model: str
    ) -> "TrajectoryLog":
        """
        Parse an OpenHands trajectory JSON and convert it into our format.
        Supplements (does not replace) real-time logging; useful for post-hoc analysis.
        """
        traj_log = cls(task_id=task_id, model=model)

        if not os.path.exists(openhands_traj_path):
            return traj_log

        with open(openhands_traj_path) as f:
            data = json.load(f)

        for event in data if isinstance(data, list) else data.get("history", []):
            action = event.get("action", "")
            observation = event.get("observation", "")

            if action == "browse" or action == "browse_interactive":
                traj_log.add_browser_action(
                    action=action,
                    url=event.get("args", {}).get("url", ""),
                )
            elif action == "run":
                traj_log.add_terminal_command(
                    command=event.get("args", {}).get("command", ""),
                    output=str(observation)[:1000],
                )
            elif action == "message":
                traj_log.add_step(
                    "agent_message",
                    content=event.get("args", {}).get("content", ""),
                )

        return traj_log
