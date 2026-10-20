"""Agent container for the OpenAI Agents harness.

Same image, workspace copy, user, working directory, platform, and host
reachability as OpenHands. The process is ``sleep infinity``: there is no
agent server, no ``OH_*`` environment, and no ``/tmp/sandbox-state``.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from authorization_eva_sandbox.evaluation.harness.agent_container import (
    AgentContainerSpec,
    SANDBOX_STATE_DIR,
)


@dataclass
class SleepAgentContainer:
    spec: AgentContainerSpec
    name: str
    container_id: str | None = None

    def start(self) -> str:
        home = self.spec.working_dir
        command = [
            "docker", "run", "-d",
            "--name", self.name,
            "--platform", self.spec.platform,
            "--entrypoint", "sleep",
            "-w", home,
            "-v", self.spec.workspace_mount,
            "-e", f"HOME={home}",
            self.spec.image,
            "infinity",
        ]
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "docker run failed")
        self.container_id = proc.stdout.strip()
        return self.container_id

    def remove(self) -> None:
        if not self.name:
            return
        subprocess.run(
            ["docker", "rm", "-f", self.name],
            capture_output=True, text=True, check=False,
        )
        self.container_id = None

    def difference(self) -> dict:
        """How this container differs from the OpenHands agent-server container."""
        omitted = [
            key for key, value in self.spec.env_dict().items()
            if key.startswith("OH_") or SANDBOX_STATE_DIR in value
        ]
        return {
            "image": self.spec.image,
            "entrypoint": ["sleep", "infinity"],
            "agent_server": False,
            "omitted_openhands_env": omitted,
            "sandbox_state_dir": None,
            "user": self.spec.image_user,
            "user_flag": self.spec.user,
            "working_dir": self.spec.working_dir,
            "platform": self.spec.platform,
            "host_reachability": self.spec.host_reachability,
            "network_mode": self.spec.network_mode,
            "extra_hosts": list(self.spec.extra_hosts),
        }
