"""Log and metrics collector service."""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import structlog

from .config import Settings
from .models import ContainerInfo, ContainerStatus
from .opensearch_client import OpenSearchClient
from .host_client import create_host_client, HostClientProtocol, SwarmProxyClient

logger = structlog.get_logger()


class Collector:
    """Collects logs and metrics from all configured hosts."""

    def __init__(self, settings: Settings, opensearch: OpenSearchClient):
        self.settings = settings
        self.opensearch = opensearch
        self.clients: Dict[str, HostClientProtocol] = {}
        self._running = False
        # Store the timestamp of the LAST LOG received (not the fetch time)
        # This ensures we don't miss any logs between collections
        self._last_log_timestamp: Dict[str, datetime] = {}
        self._containers_cache: Dict[str, List[ContainerInfo]] = {}
        self._containers_cache_time: Optional[datetime] = None

        # Track Swarm manager for routing (if swarm_routing is enabled)
        self._swarm_manager_host: Optional[str] = None
        self._swarm_routing_enabled: bool = False
        self._swarm_autodiscover_enabled: bool = False
        self._discovered_nodes: Dict[str, str] = {}  # node_hostname -> node_id

        # Initialize clients based on host configuration
        for host in settings.hosts:
            self.clients[host.name] = create_host_client(host)

            # Track Swarm manager with routing/autodiscover enabled
            if host.swarm_manager:
                if host.swarm_routing:
                    self._swarm_manager_host = host.name
                    self._swarm_routing_enabled = True
                    logger.info("Swarm routing enabled via manager", manager=host.name)

                if host.swarm_autodiscover:
                    self._swarm_manager_host = host.name
                    self._swarm_autodiscover_enabled = True
                    logger.info("Swarm auto-discovery enabled via manager", manager=host.name)
    
    async def start(self):
        """Start the collector background tasks."""
        if self._running:
            return

        self._running = True
        logger.info("Starting collector")

        # Discover Swarm nodes if auto-discovery is enabled
        if self._swarm_autodiscover_enabled:
            await self._discover_swarm_nodes()

        # Start collection tasks
        asyncio.create_task(self._log_collection_loop())
        asyncio.create_task(self._metrics_collection_loop())
        asyncio.create_task(self._cleanup_loop())

        # Start node discovery refresh loop if auto-discovery is enabled
        if self._swarm_autodiscover_enabled:
            asyncio.create_task(self._node_discovery_loop())
    
    async def stop(self):
        """Stop the collector."""
        self._running = False
        
        # Close all client connections
        for client in self.clients.values():
            await client.close()
            
        logger.info("Collector stopped")
    
    async def _log_collection_loop(self):
        """Periodically collect logs from all containers."""
        while self._running:
            try:
                await self._collect_all_logs()
            except Exception as e:
                logger.error("Log collection error", error=str(e))
            
            await asyncio.sleep(self.settings.collector.log_interval_seconds)
    
    async def _metrics_collection_loop(self):
        """Periodically collect metrics from all hosts and containers."""
        while self._running:
            try:
                await self._collect_all_metrics()
            except Exception as e:
                logger.error("Metrics collection error", error=str(e))
            
            await asyncio.sleep(self.settings.collector.metrics_interval_seconds)
    
    async def _cleanup_loop(self):
        """Periodically cleanup old data."""
        while self._running:
            try:
                await self.opensearch.cleanup_old_data(self.settings.collector.retention_days)
            except Exception as e:
                logger.error("Cleanup error", error=str(e))

            # Run cleanup once per hour
            await asyncio.sleep(3600)

    async def _node_discovery_loop(self):
        """Periodically refresh discovered Swarm nodes."""
        while self._running:
            # Refresh every 5 minutes
            await asyncio.sleep(300)

            try:
                await self._discover_swarm_nodes()
            except Exception as e:
                logger.error("Node discovery error", error=str(e))

    async def _discover_swarm_nodes(self):
        """Discover Swarm nodes and create proxy clients for each.

        This method queries the Swarm manager for all nodes in the cluster
        and creates SwarmProxyClient instances for each worker node. These
        proxy clients route all Docker commands through the manager.
        """
        if not self._swarm_manager_host:
            return

        manager_client = self.clients.get(self._swarm_manager_host)
        if not manager_client:
            return

        # Check if manager client supports Swarm operations
        if not hasattr(manager_client, 'get_swarm_nodes'):
            logger.warning("Manager client does not support Swarm node discovery",
                         manager=self._swarm_manager_host)
            return

        try:
            nodes = await manager_client.get_swarm_nodes()
            logger.info("Discovered Swarm nodes", count=len(nodes), manager=self._swarm_manager_host)

            # Get the local node ID to identify the manager node
            local_node_id = None
            if hasattr(manager_client, '_get_local_node_id'):
                local_node_id = await manager_client._get_local_node_id()

            # Track which nodes we've seen
            current_nodes = set()

            for node in nodes:
                node_id = node["id"]
                node_hostname = node["hostname"]
                node_status = node["status"]
                node_role = node["role"]

                current_nodes.add(node_hostname)

                # Skip nodes that are not ready
                if node_status != "ready":
                    logger.debug("Skipping non-ready node", node=node_hostname, status=node_status)
                    continue

                # Skip the local/manager node (identified by node ID, not hostname)
                # This handles the case where config name differs from actual hostname
                if local_node_id and (node_id.startswith(local_node_id[:12]) or
                                      local_node_id.startswith(node_id[:12])):
                    logger.debug("Skipping local manager node", node=node_hostname,
                               config_name=self._swarm_manager_host)
                    continue

                # Skip if we already have a client for this host (explicitly configured)
                if node_hostname in self.clients and node_hostname not in self._discovered_nodes:
                    logger.debug("Skipping explicitly configured host", node=node_hostname)
                    continue

                # Create or update proxy client for this node
                if node_hostname not in self._discovered_nodes:
                    proxy_client = SwarmProxyClient(manager_client, node_id, node_hostname)
                    self.clients[node_hostname] = proxy_client
                    self._discovered_nodes[node_hostname] = node_id
                    logger.info("Discovered new Swarm node",
                              node=node_hostname, role=node_role, node_id=node_id[:12])

            # Remove clients for nodes that no longer exist
            for hostname in list(self._discovered_nodes.keys()):
                if hostname not in current_nodes:
                    logger.info("Removing departed Swarm node", node=hostname)
                    if hostname in self.clients:
                        await self.clients[hostname].close()
                        del self.clients[hostname]
                    del self._discovered_nodes[hostname]

        except Exception as e:
            logger.error("Failed to discover Swarm nodes", error=str(e))
    
    async def _collect_all_logs(self):
        """Collect logs from all hosts in parallel."""
        tasks = []
        for host_name, client in self.clients.items():
            tasks.append(self._collect_host_logs(host_name, client))
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _collect_host_logs(self, host_name: str, client: HostClientProtocol):
        """Collect logs from a single host."""
        try:
            containers = await client.get_containers()
            
            # Cache containers
            self._containers_cache[host_name] = containers
            self._containers_cache_time = datetime.utcnow()
            
            # Only collect logs from running containers
            running = [c for c in containers if c.status == ContainerStatus.RUNNING]
            
            for container in running:
                container_key = f"{host_name}:{container.id}"
                
                # Get the timestamp of the last log we received for this container
                last_timestamp = self._last_log_timestamp.get(container_key)
                
                # Fetch logs:
                # - If we have a last timestamp: get ALL logs since that timestamp (no tail limit)
                # - If first fetch: use tail to limit initial load
                logs = await client.get_container_logs(
                    container_id=container.id,
                    container_name=container.name,
                    since=last_timestamp,
                    tail=self.settings.collector.log_lines_per_fetch if last_timestamp is None else None,
                    compose_project=container.compose_project,
                    compose_service=container.compose_service,
                )
                
                if logs:
                    await self.opensearch.index_logs(logs)
                    
                    # Update with the timestamp of the MOST RECENT log
                    # Add a tiny offset to avoid duplicates on next fetch
                    newest_log = max(logs, key=lambda x: x.timestamp)
                    self._last_log_timestamp[container_key] = newest_log.timestamp + timedelta(milliseconds=1)
                    
                    logger.debug(
                        "Collected logs", 
                        host=host_name, 
                        container=container.name, 
                        count=len(logs),
                        since=last_timestamp.isoformat() if last_timestamp else "initial"
                    )
                    
        except Exception as e:
            logger.error("Failed to collect logs from host", host=host_name, error=str(e))
    
    async def _collect_all_metrics(self):
        """Collect metrics from all hosts in parallel."""
        tasks = []
        for host_name, client in self.clients.items():
            tasks.append(self._collect_host_metrics(host_name, client))
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _collect_host_metrics(self, host_name: str, client: HostClientProtocol):
        """Collect metrics from a single host."""
        try:
            # Host-level metrics
            host_metrics = await client.get_host_metrics()
            await self.opensearch.index_host_metrics(host_metrics)
            
            # Container-level metrics - only for containers we can actually access
            containers = self._containers_cache.get(host_name, [])
            running = [c for c in containers if c.status == ContainerStatus.RUNNING]
            
            # Check if this is an autodiscovered node (not in original clients list)
            is_autodiscovered = host_name in self._discovered_nodes
            
            for container in running:
                # Skip stats for containers on autodiscovered Swarm nodes
                # because we can't access /containers/{id}/stats through the manager
                if is_autodiscovered:
                    continue
                
                stats = await client.get_container_stats(container.id, container.name)
                if stats:
                    await self.opensearch.index_container_stats(stats)
            
            logger.debug("Collected metrics", host=host_name, containers=len(running), 
                        autodiscovered=is_autodiscovered)
            
        except Exception as e:
            logger.error("Failed to collect metrics from host", host=host_name, error=str(e))
    
    async def get_all_containers(self, refresh: bool = False) -> List[ContainerInfo]:
        """Get all containers from all hosts (including every Docker Swarm node)."""
        # When refreshing, ensure swarm node list is up to date so Containers tab shows all nodes
        if refresh and self._swarm_autodiscover_enabled:
            await self._discover_swarm_nodes()
            # Invalidate cache after discovering nodes to force fetching from all nodes
            self._containers_cache_time = None

        # Use cache if available and not stale (30 seconds)
        if (
            not refresh
            and self._containers_cache_time
            and (datetime.utcnow() - self._containers_cache_time) < timedelta(seconds=30)
        ):
            containers = []
            for host_containers in self._containers_cache.values():
                containers.extend(host_containers)
            return containers

        # When swarm autodiscover is enabled, fetch all swarm containers in one go from the
        # manager (Tasks API). This builds ContainerInfo from task/service data instead of
        # /containers/{id}/json which only works for local containers.
        filled_from_swarm = False
        if refresh and self._swarm_autodiscover_enabled and self._swarm_manager_host:
            manager_client = self.clients.get(self._swarm_manager_host)
            if manager_client and hasattr(manager_client, "get_all_swarm_containers"):
                try:
                    containers_by_node = await manager_client.get_all_swarm_containers()
                    logger.info("Fetched swarm containers", 
                               nodes=list(containers_by_node.keys()),
                               counts={k: len(v) for k, v in containers_by_node.items()})
                    
                    # Resolve manager node hostname so we can map to configured name
                    local_node_id = None
                    if hasattr(manager_client, "_get_local_node_id"):
                        local_node_id = await manager_client._get_local_node_id()
                    nodes = await manager_client.get_swarm_nodes()
                    manager_node_hostname = None
                    if local_node_id:
                        for node in nodes:
                            nid = node.get("id", "")
                            if nid and (
                                local_node_id.startswith(nid) or nid.startswith(local_node_id[:12])
                            ):
                                manager_node_hostname = node.get("hostname")
                                break
                    
                    # Clear cache for swarm nodes before filling with new data
                    swarm_hostnames = set(containers_by_node.keys())
                    if manager_node_hostname:
                        swarm_hostnames.add(manager_node_hostname)
                    for hostname in list(self._containers_cache.keys()):
                        if hostname in swarm_hostnames or hostname == self._swarm_manager_host:
                            del self._containers_cache[hostname]
                    
                    for node_hostname, host_containers in containers_by_node.items():
                        if manager_node_hostname and node_hostname == manager_node_hostname:
                            self._containers_cache[self._swarm_manager_host] = host_containers
                        else:
                            self._containers_cache[node_hostname] = host_containers
                    filled_from_swarm = True
                except Exception as e:
                    logger.warning("get_all_swarm_containers failed, falling back to per-host fetch",
                                  error=str(e))

        if filled_from_swarm:
            # Fetch only from hosts not in cache (e.g. other configured non-swarm hosts)
            for host_name, client in self.clients.items():
                if host_name in self._containers_cache:
                    continue
                await self._fetch_and_cache_containers(host_name, client)
        else:
            # Fetch from all clients (normal path or swarm path failed)
            tasks = []
            for host_name, client in self.clients.items():
                tasks.append(self._fetch_and_cache_containers(host_name, client))
            await asyncio.gather(*tasks, return_exceptions=True)

        self._containers_cache_time = datetime.utcnow()

        containers = []
        for host_containers in self._containers_cache.values():
            containers.extend(host_containers)
        return containers
    
    async def _fetch_and_cache_containers(self, host_name: str, client: HostClientProtocol):
        """Fetch and cache containers from a host."""
        try:
            containers = await client.get_containers()
            self._containers_cache[host_name] = containers
        except Exception as e:
            logger.error("Failed to fetch containers", host=host_name, error=str(e))
    
    def _get_exec_client(self, host: str) -> Optional[HostClientProtocol]:
        """Get the client to use for exec operations on a container.

        If Swarm routing is enabled and the target host is in the Swarm,
        uses the Swarm manager client instead of direct connection.
        This eliminates the need for SSH access to worker nodes.
        """
        # If swarm routing is enabled and we have a manager
        if self._swarm_routing_enabled and self._swarm_manager_host:
            manager_client = self.clients.get(self._swarm_manager_host)
            if manager_client:
                # Check if target host is different from manager
                # (manager can handle its own containers directly)
                if host != self._swarm_manager_host:
                    logger.debug("Routing exec through Swarm manager",
                                target_host=host, manager=self._swarm_manager_host)
                    return manager_client

        # Fall back to direct client
        return self.clients.get(host)

    async def get_container_stats(self, host: str, container_id: str) -> Optional[dict]:
        """Get current stats for a specific container.

        For Swarm containers on worker nodes, stats are fetched by connecting
        to the worker node (if SSH is configured) or returned as None.
        """
        containers = self._containers_cache.get(host, [])
        container = next((c for c in containers if c.id == container_id), None)
        if not container:
            return None

        # Check if this is a Swarm container on a different node
        task_id = container.labels.get("com.docker.swarm.task.id") if container.labels else None
        
        # If we have a direct client for this host, use it
        direct_client = self.clients.get(host)
        if direct_client:
            stats = await direct_client.get_container_stats(container_id, container.name)
            return stats.model_dump() if stats else None
        
        # For Swarm worker nodes without direct client, try manager with task info
        if self._swarm_routing_enabled and self._swarm_manager_host and task_id:
            manager_client = self.clients.get(self._swarm_manager_host)
            if manager_client:
                # Try to get stats - will work if container happens to be on manager
                stats = await manager_client.get_container_stats(container_id, container.name)
                if stats:
                    return stats.model_dump()
                # Stats not available for remote Swarm containers without SSH
                logger.debug("Stats not available for remote Swarm container", 
                           container=container_id, host=host)
        
        return None
    
    async def execute_action(self, host: str, container_id: str, action: str) -> tuple:
        """Execute an action on a container.

        If Swarm routing is enabled, actions are executed through the
        Swarm manager, eliminating the need for direct SSH access.
        """
        from .models import ContainerAction

        # Use routing client (may route through Swarm manager)
        client = self._get_exec_client(host)
        if not client:
            return False, f"Unknown host: {host}"

        try:
            container_action = ContainerAction(action)
        except ValueError:
            return False, f"Invalid action: {action}"

        return await client.execute_container_action(container_id, container_action)
    
    async def get_container_logs_live(
        self,
        host: str,
        container_id: str,
        tail: int = 200
    ) -> List[dict]:
        """Get live logs for a specific container.

        If Swarm routing is enabled, logs are fetched through the
        Swarm manager using the tasks API, eliminating the need for 
        direct SSH access to worker nodes.
        """
        # Use routing client (may route through Swarm manager)
        client = self._get_exec_client(host)
        if not client:
            return []

        containers = self._containers_cache.get(host, [])
        container = next((c for c in containers if c.id == container_id), None)
        
        # Extract task_id from labels if this is a Swarm container
        task_id = None
        if container and container.labels:
            task_id = container.labels.get("com.docker.swarm.task.id")

        logs = await client.get_container_logs(
            container_id=container_id,
            container_name=container.name if container else container_id,
            tail=tail,
            compose_project=container.compose_project if container else None,
            compose_service=container.compose_service if container else None,
            task_id=task_id,
        )

        return [log.model_dump() for log in logs]

    async def get_container_env(self, host: str, container_id: str) -> Optional[dict]:
        """Get environment variables for a specific container.

        For local containers: runs printenv inside the container.
        For Swarm containers on worker nodes: retrieves env from service spec.
        """
        containers = self._containers_cache.get(host, [])
        container = next((c for c in containers if c.id == container_id), None)
        
        # Check if this is a Swarm container
        service_id = container.labels.get("com.docker.swarm.service.id") if container and container.labels else None
        
        # If we have a direct client for this host, use exec
        direct_client = self.clients.get(host)
        if direct_client:
            success, output = await direct_client.exec_command(container_id, ["printenv"])
            if success:
                env_vars = {}
                for line in output.strip().split('\n'):
                    if '=' in line:
                        key, _, value = line.partition('=')
                        env_vars[key] = value
                return {"variables": env_vars}
            return {"error": output}
        
        # For Swarm containers without direct access, get env from service spec
        if self._swarm_routing_enabled and self._swarm_manager_host and service_id:
            manager_client = self.clients.get(self._swarm_manager_host)
            if manager_client and hasattr(manager_client, 'get_service_env'):
                env_vars = await manager_client.get_service_env(service_id)
                if env_vars:
                    return {"variables": env_vars, "source": "service_spec"}
        
        return {"error": "Cannot access container environment (remote Swarm node)"}
