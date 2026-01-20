"""Unified host client interface."""

from typing import Dict, List, Optional, Tuple, Protocol
from datetime import datetime

import structlog

from .config import HostConfig
from .models import (
    ContainerInfo, ContainerStats, ContainerAction,
    HostMetrics, LogEntry
)

logger = structlog.get_logger()


class HostClientProtocol(Protocol):
    """Protocol for host clients (SSH or Docker API)."""
    
    config: HostConfig
    
    async def get_containers(self) -> List[ContainerInfo]: ...
    async def get_container_stats(self, container_id: str, container_name: str) -> Optional[ContainerStats]: ...
    async def get_host_metrics(self) -> HostMetrics: ...
    async def get_container_logs(
        self,
        container_id: str,
        container_name: str,
        since: Optional[datetime] = None,
        tail: Optional[int] = 500,
        compose_project: Optional[str] = None,
        compose_service: Optional[str] = None,
    ) -> List[LogEntry]: ...
    async def execute_container_action(self, container_id: str, action: ContainerAction) -> Tuple[bool, str]: ...
    async def remove_stack(self, stack_name: str) -> Tuple[bool, str]: ...
    async def get_swarm_stacks(self) -> Dict[str, List[str]]: ...
    async def close(self) -> None: ...


def create_host_client(host_config: HostConfig) -> HostClientProtocol:
    """Factory function to create the appropriate client based on config."""
    
    mode = host_config.mode.lower()
    
    if mode == "docker":
        from .docker_client import DockerAPIClient
        logger.info("Creating Docker API client", host=host_config.name)
        return DockerAPIClient(host_config)
    
    elif mode == "local":
        from .ssh_client import SSHClient
        # Force local mode in SSH client
        host_config_copy = host_config.model_copy()
        host_config_copy.hostname = "localhost"
        logger.info("Creating local client", host=host_config.name)
        return SSHClient(host_config_copy)
    
    else:  # mode == "ssh" or default
        from .ssh_client import SSHClient
        logger.info("Creating SSH client", host=host_config.name, hostname=host_config.hostname)
        return SSHClient(host_config)
