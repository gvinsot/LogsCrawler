"""Unified host client interface."""

from typing import Dict, List, Optional, Tuple, Protocol, Any, TYPE_CHECKING
from datetime import datetime

import structlog

from .config import HostConfig
from .models import (
    ContainerInfo, ContainerStats, ContainerAction,
    HostMetrics, LogEntry
)

if TYPE_CHECKING:
    from .docker_client import DockerAPIClient

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
    async def exec_command(self, container_id: str, command: List[str]) -> Tuple[bool, str]: ...
    async def remove_stack(self, stack_name: str) -> Tuple[bool, str]: ...
    async def get_swarm_stacks(self) -> Dict[str, List[str]]: ...
    async def close(self) -> None: ...


class SwarmProxyClient:
    """Proxy client for Swarm worker nodes discovered via manager.

    This client wraps a Swarm manager's DockerAPIClient and filters/routes
    requests for a specific worker node. All Docker commands are executed
    through the manager's API, which routes them to the correct node.

    This eliminates the need for direct SSH access to worker nodes.
    """

    def __init__(self, manager_client: "DockerAPIClient", node_id: str, node_hostname: str):
        """Initialize proxy client for a Swarm worker node.

        Args:
            manager_client: The DockerAPIClient connected to the Swarm manager
            node_id: The Swarm node ID for this worker
            node_hostname: The hostname of the worker node
        """
        self._manager = manager_client
        self._node_id = node_id
        self._node_hostname = node_hostname

        # Create a virtual config for this node
        self.config = HostConfig(
            name=node_hostname,
            hostname=node_hostname,
            mode="swarm-proxy",
            swarm_manager=False,
        )
        logger.info("Created Swarm proxy client",
                   node=node_hostname, node_id=node_id[:12],
                   manager=manager_client.config.name)

    async def get_containers(self) -> List[ContainerInfo]:
        """Get containers running on this specific node."""
        # Get all tasks and filter by node
        tasks = await self._manager.get_swarm_tasks()
        node_container_ids = [
            t["container_id"] for t in tasks
            if t["node_id"].startswith(self._node_id[:12]) or self._node_id.startswith(t["node_id"][:12])
        ]

        if not node_container_ids:
            return []

        # Get container details for each
        containers = []
        for container_id in node_container_ids:
            data, status = await self._manager._request("GET", f"/containers/{container_id}/json")
            if status == 200 and data:
                try:
                    labels = data.get("Config", {}).get("Labels", {}) or {}
                    name = data.get("Name", "/unknown").lstrip("/")

                    # Parse status
                    state = data.get("State", {})
                    from .models import ContainerStatus
                    status_str = state.get("Status", "unknown").lower()
                    try:
                        container_status = ContainerStatus(status_str)
                    except ValueError:
                        container_status = ContainerStatus.RUNNING

                    # Parse created time
                    created_str = data.get("Created", "")
                    try:
                        created = datetime.fromisoformat(created_str.replace("Z", "+00:00")[:26])
                    except:
                        created = datetime.now()

                    # Get compose/stack project and service
                    # Try Compose labels first, then Swarm stack labels
                    compose_project = (labels.get("com.docker.compose.project") or
                                       labels.get("com.docker.stack.namespace"))
                    compose_service = (labels.get("com.docker.compose.service") or
                                       labels.get("com.docker.swarm.service.name"))

                    container = ContainerInfo(
                        id=container_id[:12],
                        name=name,
                        image=data.get("Config", {}).get("Image", "unknown"),
                        status=container_status,
                        created=created,
                        host=self._node_hostname,  # Use the node hostname
                        compose_project=compose_project,
                        compose_service=compose_service,
                        ports={},
                        labels=labels,
                    )
                    containers.append(container)
                except Exception as e:
                    logger.error("Failed to parse container", container_id=container_id, error=str(e))

        return containers

    async def get_container_stats(self, container_id: str, container_name: str) -> Optional[ContainerStats]:
        """Get container stats via manager."""
        return await self._manager.get_container_stats(container_id, container_name)

    async def get_host_metrics(self) -> HostMetrics:
        """Get host metrics (limited for proxy nodes)."""
        # We can't get full host metrics via Swarm API, return aggregated container stats
        containers = await self.get_containers()
        from .models import ContainerStatus

        running = [c for c in containers if c.status == ContainerStatus.RUNNING]

        cpu_percent = 0.0
        memory_used_mb = 0.0

        for container in running[:10]:
            stats = await self.get_container_stats(container.id, container.name)
            if stats:
                cpu_percent += stats.cpu_percent
                memory_used_mb += stats.memory_usage_mb

        return HostMetrics(
            host=self._node_hostname,
            timestamp=datetime.utcnow(),
            cpu_percent=round(cpu_percent, 2),
            memory_total_mb=0,  # Unknown via Swarm API
            memory_used_mb=round(memory_used_mb, 2),
            memory_percent=0,
            disk_total_gb=0,
            disk_used_gb=0,
            disk_percent=0,
        )

    async def get_container_logs(
        self,
        container_id: str,
        container_name: str,
        since: Optional[datetime] = None,
        tail: Optional[int] = 500,
        compose_project: Optional[str] = None,
        compose_service: Optional[str] = None,
    ) -> List[LogEntry]:
        """Get container logs via manager."""
        return await self._manager.get_container_logs(
            container_id, container_name, since, tail,
            compose_project, compose_service
        )

    async def execute_container_action(self, container_id: str, action: ContainerAction) -> Tuple[bool, str]:
        """Execute action on container via manager."""
        return await self._manager.execute_container_action(container_id, action)

    async def exec_command(self, container_id: str, command: List[str]) -> Tuple[bool, str]:
        """Execute command in container via manager."""
        return await self._manager.exec_command(container_id, command)

    async def remove_stack(self, stack_name: str) -> Tuple[bool, str]:
        """Remove stack via manager."""
        return await self._manager.remove_stack(stack_name)

    async def get_swarm_stacks(self) -> Dict[str, List[str]]:
        """Get swarm stacks via manager."""
        return await self._manager.get_swarm_stacks()

    async def close(self) -> None:
        """No-op for proxy client (manager handles connection)."""
        pass


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
