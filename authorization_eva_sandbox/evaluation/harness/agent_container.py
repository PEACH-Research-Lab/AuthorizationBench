"""Parameters of the agent workspace container.

OpenHands starts this container through DockerWorkspace. The fields below are
the effective create-time parameters: what that call passes, plus the image
user, network, extra hosts, and labels DockerWorkspace leaves unset.
"""
from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The image built for SDK 1.49.2. DockerWorkspace's own default is the
# unpinned ghcr.io/openhands/agent-server:latest-python tag.
AGENT_SERVER_IMAGE = "authorizationbench-openhands:1.49.2"
CONTAINER_WORKSPACE = "/workspace"
# Host path of the copy mounted at CONTAINER_WORKSPACE, for host-side services.
WORKSPACE_HOST_DIR_ENV = "SANDBOX_WORKSPACE_HOST_DIR"
# Agent-server state and tool caches. This is outside task_config.home, so a
# cleanup of ~ does not delete the conversation, and it is not /home/openhands.
SANDBOX_STATE_DIR = "/tmp/sandbox-state"
# Names DockerWorkspace forwards only when they are already in the process environment.
_DOCKER_FORWARD_ENV = ("DEBUG", "SESSION_API_KEY", "OH_SESSION_API_KEYS_0")
# Dockerfile USER. DockerWorkspace does not pass --user, so this is who runs.
IMAGE_USER = "openhands"
# Docker Desktop resolves this name. The run does not add --add-host.
HOST_REACHABILITY = "host.docker.internal"

def host_linux_platform() -> str:
    """Docker platform for this machine. SDK 1.49.2 defaults to linux/amd64."""
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        return "linux/arm64"
    return "linux/amd64"


def _resolved_new_path(path: Path) -> Path:
    path = path.expanduser()
    if path.exists():
        return path.resolve()
    return path.parent.resolve() / path.name


def prepare_agent_workspace_copy(bundle: Path, destination: Path) -> Path:
    """Copy ``bundle/workspace`` to a new directory outside the sealed bundle.

    June reset a reused runtime's ``/workspace`` before ``copy_to``. SDK
    1.49.2 ``DockerWorkspace`` starts a new ``--rm`` container on each
    construction, so the reset is this copy: the container mounts
    ``destination``, never ``bundle`` or ``bundle/workspace``. A bundle with
    no ``workspace/`` still gets an empty directory.
    """
    bundle = Path(bundle).resolve()
    destination = _resolved_new_path(Path(destination))
    sealed = bundle / "workspace"
    if destination == bundle or bundle in destination.parents:
        raise ValueError(
            f"run workspace {destination} is inside the sealed bundle {bundle}"
        )
    if destination == sealed or sealed in destination.parents or destination in sealed.parents:
        raise ValueError(
            f"run workspace {destination} overlaps the sealed workspace {sealed}"
        )
    if destination.exists():
        raise FileExistsError(f"run workspace already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sealed.is_dir():
        shutil.copytree(sealed, destination, symlinks=False)
    else:
        destination.mkdir()
    return destination


def agent_home(task_config: dict | None) -> str:
    """``task_config.home``: the directory ``~`` expands to.

    The field already exists on every sealed bundle. A missing or relative
    value uses ``/workspace``, which is also the schema default.
    """
    raw = ""
    if isinstance(task_config, dict):
        raw = str(task_config.get("home") or "").strip()
    if raw.startswith("/") and ".." not in raw.split("/"):
        return raw
    return CONTAINER_WORKSPACE


def sandbox_process_env(home: str) -> dict[str, str]:
    """Container environment for one run.

    ``HOME`` is the sandbox home. Conversation files, bash events, and tool
    caches go under ``/tmp/sandbox-state`` so they are not written into that
    home and not into ``/home/openhands``.
    """
    state = SANDBOX_STATE_DIR
    return {
        "HOME": home,
        "OH_CONVERSATIONS_PATH": f"{state}/conversations",
        "OH_BASH_EVENTS_DIR": f"{state}/bash_events",
        "OH_PERSISTENCE_DIR": f"{state}/persistence",
        "XDG_CONFIG_HOME": f"{state}/config",
        "XDG_CACHE_HOME": f"{state}/cache",
        "XDG_DATA_HOME": f"{state}/data",
        "NPM_CONFIG_USERCONFIG": f"{state}/npmrc",
        "NPM_CONFIG_PREFIX": f"{state}/npm",
        "PIP_CACHE_DIR": f"{state}/pip",
        "GIT_CONFIG_GLOBAL": f"{state}/gitconfig",
        "HISTFILE": "/dev/null",
    }


def docker_workspace_volume(host_copy: Path) -> str:
    """Host path mounted at ``/workspace``. The path must already be a copy."""
    host = Path(host_copy).resolve()
    if not host.is_dir():
        raise ValueError(f"agent workspace copy does not exist: {host}")
    return f"{host}:{CONTAINER_WORKSPACE}"


@dataclass(frozen=True)
class AgentContainerSpec:
    """One agent container, as OpenHandsDockerWorkspace creates it.

    ``user`` is None because no ``--user`` is passed. ``image_user`` is the
    image USER that then applies. ``network_mode`` None leaves Docker's
    default bridge. ``extra_hosts`` and ``labels`` are empty: the run sets
    neither ``--add-host`` nor ``--label``.
    """

    image: str
    workspace_mount: str
    user: str | None
    image_user: str
    network_mode: str | None
    extra_hosts: tuple[str, ...]
    labels: tuple[tuple[str, str], ...]
    env: tuple[tuple[str, str], ...]
    platform: str
    working_dir: str
    forward_env: tuple[str, ...]
    detach_logs: bool
    host_reachability: str

    def env_dict(self) -> dict[str, str]:
        return dict(self.env)


def agent_container_spec(
    host_copy: Path,
    *,
    home: str,
    platform: str,
    image: str = AGENT_SERVER_IMAGE,
) -> AgentContainerSpec:
    """The container OpenHandsDockerWorkspace starts for this workspace copy."""
    env = sandbox_process_env(home)
    return AgentContainerSpec(
        image=image,
        workspace_mount=docker_workspace_volume(host_copy),
        user=None,
        image_user=IMAGE_USER,
        network_mode=None,
        extra_hosts=(),
        labels=(),
        env=tuple(env.items()),
        platform=platform,
        working_dir=home,
        forward_env=(*_DOCKER_FORWARD_ENV, *env),
        detach_logs=False,
        host_reachability=HOST_REACHABILITY,
    )


def docker_workspace_kwargs(spec: AgentContainerSpec) -> dict[str, Any]:
    """Keyword arguments OpenHands passes to DockerWorkspace for ``spec``."""
    return {
        "server_image": spec.image,
        "working_dir": spec.working_dir,
        "platform": spec.platform,
        "volumes": [spec.workspace_mount],
        "forward_env": list(spec.forward_env),
        "detach_logs": spec.detach_logs,
    }
