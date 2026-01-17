"""Log and metrics collector service."""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import structlog

from .config import Settings
from .models import ContainerInfo, ContainerStatus
from .opensearch_client import OpenSearchClient
from .host_client import create_host_client, HostClientProtocol

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
        
        # Initialize clients based on host configuration
        for host in settings.hosts:
            self.clients[host.name] = create_host_client(host)
    
    async def start(self):
        """Start the collector background tasks."""
        if self._running:
            return
            
        self._running = True
        logger.info("Starting collector")
        
        # Start collection tasks
        asyncio.create_task(self._log_collection_loop())
        asyncio.create_task(self._metrics_collection_loop())
        asyncio.create_task(self._cleanup_loop())
    
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
            
            # Container-level metrics
            containers = self._containers_cache.get(host_name, [])
            running = [c for c in containers if c.status == ContainerStatus.RUNNING]
            
            for container in running:
                stats = await client.get_container_stats(container.id, container.name)
                if stats:
                    await self.opensearch.index_container_stats(stats)
            
            logger.debug("Collected metrics", host=host_name)
            
        except Exception as e:
            logger.error("Failed to collect metrics from host", host=host_name, error=str(e))
    
    async def get_all_containers(self, refresh: bool = False) -> List[ContainerInfo]:
        """Get all containers from all hosts."""
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
        
        # Refresh from all hosts
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
    
    async def get_container_stats(self, host: str, container_id: str) -> Optional[dict]:
        """Get current stats for a specific container."""
        client = self.clients.get(host)
        if not client:
            return None
        
        containers = self._containers_cache.get(host, [])
        container = next((c for c in containers if c.id == container_id), None)
        if not container:
            return None
        
        stats = await client.get_container_stats(container_id, container.name)
        return stats.model_dump() if stats else None
    
    async def execute_action(self, host: str, container_id: str, action: str) -> tuple:
        """Execute an action on a container."""
        from .models import ContainerAction
        
        client = self.clients.get(host)
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
        """Get live logs for a specific container."""
        client = self.clients.get(host)
        if not client:
            return []
        
        containers = self._containers_cache.get(host, [])
        container = next((c for c in containers if c.id == container_id), None)
        
        logs = await client.get_container_logs(
            container_id=container_id,
            container_name=container.name if container else container_id,
            tail=tail,
            compose_project=container.compose_project if container else None,
            compose_service=container.compose_service if container else None,
        )
        
        return [log.model_dump() for log in logs]
