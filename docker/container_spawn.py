"""
Sibling-container spawn helper with the full aMaze isolation profile.

The Orchestrator calls `spawn_sibling_container` when starting a session
(agent) or registering an MCP server. The resulting container:

  - Is attached ONLY to its isolation network (amaze-agent-net for agents,
    amaze-mcp-net for MCP servers). Both are internal=true → no internet.
  - Has HTTP_PROXY / HTTPS_PROXY pre-set to the in-net proxy URL.
  - Gets the proxy's MITM CA mounted read-only from a shared named volume
    (the proxy wrote it on first boot).
  - Runs with dropped Linux capabilities, read-only rootfs, /tmp as tmpfs.
  - Is memory / pid / cpu capped.

Ported from aMaze's services/orchestrator/src/orchestrator/container_manager.py,
simplified for the single-container + YAML-config model. The aMaze original
used a bind-mount host path for the CA; we use a named Docker volume, which
is the only shape that works when the proxy and the Orchestrator run inside
the same container and the CA is generated at runtime rather than pre-staged
on the host.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

import docker
import docker.errors
from docker.models.containers import Container

logger = logging.getLogger(__name__)

PROXY_URL = os.environ.get("AMAZE_PROXY_URL", "http://proxy:8080")

AGENT_NETWORK = os.environ.get("AMAZE_AGENT_NETWORK", "amaze_amaze-agent-net")
MCP_NETWORK = os.environ.get("AMAZE_MCP_NETWORK", "amaze_amaze-mcp-net")

# Named Docker volume holding mitmproxy-ca-cert.pem. Written by the proxy on
# first boot (mitmproxy auto-generates its CA into $HOME/.mitmproxy). Must be
# mounted into the proxy at /opt/mitmproxy and into every sibling at
# /etc/amaze/ca (read-only).
CA_VOLUME_NAME = os.environ.get("AMAZE_PROXY_CA_VOLUME", "amaze-proxy-ca")
CA_MOUNT_PATH = "/etc/amaze/ca"
CA_CERT_FILENAME = "mitmproxy-ca-cert.pem"


class SiblingKind(str, Enum):
    AGENT = "agent"
    MCP = "mcp"


@dataclass(frozen=True)
class SpawnResult:
    container_id: str
    container_name: str


def spawn_sibling_container(
    *,
    kind: SiblingKind,
    session_id: str,
    identity: str,           # agent_id for agents; server name for MCP
    image: str,
    bearer_token: str | None = None,
    env_vars: dict[str, str] | None = None,
    mem_limit: str = "512m",
    cpu_quota: int = 50000,
) -> SpawnResult:
    """Spawn one isolated sibling container (agent or MCP)."""

    network = AGENT_NETWORK if kind is SiblingKind.AGENT else MCP_NETWORK
    name_prefix = "amaze-agent" if kind is SiblingKind.AGENT else "amaze-mcp"

    env: dict[str, str] = {
        "HTTP_PROXY": PROXY_URL,
        "HTTPS_PROXY": PROXY_URL,
        "NO_PROXY": "",
        "AMAZE_SESSION_ID": session_id,
        "AMAZE_PROXY_URL": PROXY_URL,
        "SSL_CERT_FILE": f"{CA_MOUNT_PATH}/{CA_CERT_FILENAME}",
        "REQUESTS_CA_BUNDLE": f"{CA_MOUNT_PATH}/{CA_CERT_FILENAME}",
        **(env_vars or {}),
    }
    if kind is SiblingKind.AGENT:
        env["AMAZE_AGENT_ID"] = identity
        if bearer_token is not None:
            env["AMAZE_SESSION_TOKEN"] = bearer_token

    volumes = {
        CA_VOLUME_NAME: {"bind": CA_MOUNT_PATH, "mode": "ro"},
    }

    container_name = f"{name_prefix}-{identity}-{session_id[:8]}"

    client = docker.from_env()
    container: Container = client.containers.run(
        image=image,
        name=container_name,
        detach=True,
        network=network,
        environment=env,
        volumes=volumes,
        security_opt=["no-new-privileges:true"],
        cap_drop=["ALL"],
        read_only=True,
        tmpfs={"/tmp": "size=256m,noexec,nosuid"},
        mem_limit=mem_limit,
        pids_limit=512,
        cpu_quota=cpu_quota,
        cpu_period=100000,
        userns_mode="",
    )

    logger.info(
        "spawned %s container name=%s image=%s session=%s identity=%s",
        kind.value, container_name, image, session_id, identity,
    )
    return SpawnResult(container_id=container.id, container_name=container_name)


def stop_sibling_container(container_id: str) -> None:
    """Idempotent stop + remove."""
    client = docker.from_env()
    try:
        container = client.containers.get(container_id)
        container.stop(timeout=10)
        container.remove(force=True)
    except docker.errors.NotFound:
        return


def ca_is_ready() -> bool:
    """Orchestrator gates agent-spawn on this: the CA volume has a cert in it.
    Prevents racing the proxy's first-boot CA generation."""
    local_path = os.path.join(CA_MOUNT_PATH, CA_CERT_FILENAME)
    return os.path.exists(local_path)
