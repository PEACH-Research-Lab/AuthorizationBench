"""OpenHands DockerWorkspace over one workspace copy."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.evaluation.harness.agent_container import (
    AGENT_SERVER_IMAGE,
    CONTAINER_WORKSPACE,
    agent_container_spec,
    agent_home,
    docker_workspace_kwargs,
    host_linux_platform,
    prepare_agent_workspace_copy,
)

@contextlib.contextmanager
def _forwarded_container_env(values: dict[str, str]):
    """Expose ``values`` to DockerWorkspace, then restore this process."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class OpenHandsDockerWorkspace:
    """One fresh agent-server container over a copy of ``bundle/workspace``.

    ``DockerWorkspace()`` starts the container. This object does not build a
    Conversation and does not call a model.
    """

    def __init__(
        self,
        bundle: Path,
        copy_destination: Path,
        *,
        server_image: str = AGENT_SERVER_IMAGE,
        platform_name: str | None = None,
    ):
        self.bundle = Path(bundle)
        self.host_copy = prepare_agent_workspace_copy(self.bundle, Path(copy_destination))
        self.server_image = server_image
        self.platform_name = platform_name or host_linux_platform()
        self.home = CONTAINER_WORKSPACE
        self.workspace: Any = None

    def start(self) -> Any:
        from openhands.workspace.docker.workspace import DockerWorkspace

        config_path = self.bundle / "task_config.json"
        task_config = (
            json.loads(config_path.read_text()) if config_path.is_file() else {}
        )
        self.home = agent_home(task_config)
        spec = agent_container_spec(
            self.host_copy,
            home=self.home,
            platform=self.platform_name,
            image=self.server_image,
        )
        with _forwarded_container_env(spec.env_dict()):
            self.workspace = DockerWorkspace(**docker_workspace_kwargs(spec))
        return self.workspace

    def close(self) -> None:
        if self.workspace is not None:
            self.workspace.cleanup()
            self.workspace = None
