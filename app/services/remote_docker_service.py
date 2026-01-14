"""
Remote Docker Service - Executes Docker commands on remote systems via SSH.
Provides the same interface as DockerService but for remote systems.
"""

import json
import logging
import re
from datetime import datetime
from typing import List, Optional, AsyncGenerator, Dict, Tuple
import asyncio

from app.models import ContainerInfo, ContainerLog, RemoteSystem
from app.services.remote_systems_service import remote_systems_service

logger = logging.getLogger(__name__)

# Cache TTL for container metadata (seconds)
CONTAINER_CACHE_TTL = 300  # 5 minutes


class RemoteDockerService:
    """
    Service for interacting with Docker on remote systems via SSH.
    Parses docker CLI output to provide container and log information.
    Uses caching to minimize SSH round-trips.
    """
    
    def __init__(self):
        # Cache: {(system_id, container_id): (container_name, cached_at)}
        self._container_name_cache: Dict[Tuple[str, str], Tuple[str, datetime]] = {}
        # Cache: {system_id: (containers_list, cached_at)}
        self._containers_cache: Dict[str, Tuple[List[ContainerInfo], datetime]] = {}
    
    def _get_cached_container_name(self, system_id: str, container_id: str) -> Optional[str]:
        """Get container name from cache if still valid."""
        cache_key = (system_id, container_id)
        if cache_key in self._container_name_cache:
            name, cached_at = self._container_name_cache[cache_key]
            if (datetime.now() - cached_at).total_seconds() < CONTAINER_CACHE_TTL:
                return name
            # Cache expired
            del self._container_name_cache[cache_key]
        return None
    
    def _cache_container_name(self, system_id: str, container_id: str, name: str):
        """Cache a container name."""
        self._container_name_cache[(system_id, container_id)] = (name, datetime.now())
    
    def _get_cached_containers(self, system_id: str) -> Optional[List[ContainerInfo]]:
        """Get containers list from cache if still valid."""
        if system_id in self._containers_cache:
            containers, cached_at = self._containers_cache[system_id]
            # Use shorter TTL for containers list (30 seconds)
            if (datetime.now() - cached_at).total_seconds() < 30:
                return containers
            del self._containers_cache[system_id]
        return None
    
    def _cache_containers(self, system_id: str, containers: List[ContainerInfo]):
        """Cache containers list and update name cache."""
        self._containers_cache[system_id] = (containers, datetime.now())
        # Also update name cache for all containers
        for c in containers:
            self._cache_container_name(system_id, c.id, c.name)
    
    def clear_cache(self, system_id: Optional[str] = None):
        """Clear cache for a specific system or all systems."""
        if system_id:
            # Clear cache for specific system
            if system_id in self._containers_cache:
                del self._containers_cache[system_id]
            # Clear name cache for this system
            keys_to_remove = [k for k in self._container_name_cache if k[0] == system_id]
            for k in keys_to_remove:
                del self._container_name_cache[k]
        else:
            # Clear all caches
            self._container_name_cache.clear()
            self._containers_cache.clear()
        logger.debug(f"Cleared cache for system: {system_id or 'all'}")
    
    async def get_containers(
        self,
        system_id: str,
        all_containers: bool = True,
        use_cache: bool = True
    ) -> List[ContainerInfo]:
        """Get list of containers from a remote system."""
        # Try cache first for running containers (most common case)
        if use_cache and not all_containers:
            cached = self._get_cached_containers(system_id)
            if cached is not None:
                logger.debug(f"Using cached containers for {system_id}")
                return cached
        
        system = remote_systems_service.get_system(system_id)
        if not system:
            logger.error(f"System not found: {system_id}")
            return []
        
        # Docker command to get container info as JSON
        all_flag = "-a" if all_containers else ""
        cmd = f'docker ps {all_flag} --format "{{{{json .}}}}"'
        
        output = await remote_systems_service.run_command(system_id, cmd, timeout=30)
        if not output:
            return []
        
        containers = []
        for line in output.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
                container = self._parse_container_json(data, system)
                if container:
                    containers.append(container)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse container JSON: {e}")
                continue
        
        # Cache running containers
        if not all_containers:
            self._cache_containers(system_id, containers)
        else:
            # Still update name cache for all containers
            for c in containers:
                self._cache_container_name(system_id, c.id, c.name)
        
        return containers
    
    async def get_container(
        self,
        system_id: str,
        container_id: str
    ) -> Optional[ContainerInfo]:
        """Get a specific container by ID from a remote system."""
        system = remote_systems_service.get_system(system_id)
        if not system:
            return None
        
        cmd = f'docker inspect {container_id} --format "{{{{json .}}}}"'
        output = await remote_systems_service.run_command(system_id, cmd, timeout=15)
        
        if not output:
            return None
        
        try:
            data = json.loads(output.strip())
            return self._parse_container_inspect(data, system)
        except json.JSONDecodeError:
            return None
    
    async def get_logs(
        self,
        system_id: str,
        container_id: str,
        tail: int = 100,
        timestamps: bool = True
    ) -> List[ContainerLog]:
        """Get logs from a container on a remote system."""
        system = remote_systems_service.get_system(system_id)
        if not system:
            return []
        
        # Try to get container name from cache first
        container_name = self._get_cached_container_name(system_id, container_id)
        
        if container_name:
            # Name cached - just get logs (1 SSH command)
            ts_flag = "-t" if timestamps else ""
            cmd = f'docker logs {container_id} --tail {tail} {ts_flag} 2>&1'
            output = await remote_systems_service.run_command(system_id, cmd, timeout=60)
        else:
            # Batch both commands into single SSH call to reduce round-trips
            ts_flag = "-t" if timestamps else ""
            cmd = f'''bash -c 'echo "===NAME_START==="; docker inspect {container_id} --format "{{{{.Name}}}}"; echo "===NAME_END==="; docker logs {container_id} --tail {tail} {ts_flag} 2>&1' '''
            output = await remote_systems_service.run_command(system_id, cmd, timeout=60)
            
            if output:
                # Parse the combined output
                if "===NAME_START===" in output and "===NAME_END===" in output:
                    parts = output.split("===NAME_END===", 1)
                    name_part = parts[0].replace("===NAME_START===", "").strip()
                    container_name = name_part.lstrip('/')
                    output = parts[1].strip() if len(parts) > 1 else ""
                    # Cache the name
                    if container_name:
                        self._cache_container_name(system_id, container_id, container_name)
        
        # Fallback to container_id if name couldn't be determined
        if not container_name:
            container_name = container_id
        
        if not output:
            return []
        
        logs = []
        for line in output.split('\n'):
            if not line:
                continue
            log_entry = self._parse_log_line(
                container_id, container_name, line, "stdout", timestamps, system
            )
            if log_entry:
                logs.append(log_entry)
        
        # Sort by timestamp
        logs.sort(key=lambda x: x.timestamp or datetime.min)
        return logs
    
    async def get_all_logs(
        self,
        system_id: str,
        tail: int = 50
    ) -> List[ContainerLog]:
        """Get logs from all running containers on a remote system."""
        containers = await self.get_containers(system_id, all_containers=False)
        
        all_logs = []
        for container in containers:
            try:
                logs = await self.get_logs(system_id, container.id, tail=tail)
                all_logs.extend(logs)
            except Exception as e:
                logger.warning(f"Failed to get logs from {container.name}: {e}")
                continue
        
        # Sort by timestamp
        all_logs.sort(key=lambda x: x.timestamp or datetime.min)
        return all_logs
    
    async def stream_logs(
        self,
        system_id: str,
        container_id: str,
        tail: int = 50
    ) -> AsyncGenerator[ContainerLog, None]:
        """Stream logs from a container on a remote system."""
        system = remote_systems_service.get_system(system_id)
        if not system:
            return
        
        # Try to get container name from cache first
        container_name = self._get_cached_container_name(system_id, container_id)
        
        if not container_name:
            # Fetch and cache the name
            name_cmd = f'docker inspect {container_id} --format "{{{{.Name}}}}"'
            name_output = await remote_systems_service.run_command(system_id, name_cmd, timeout=10)
            if name_output:
                container_name = name_output.strip().lstrip('/')
                self._cache_container_name(system_id, container_id, container_name)
            else:
                container_name = container_id
        
        connection = await remote_systems_service.get_connection(system_id)
        if not connection:
            return
        
        try:
            # Start streaming logs
            cmd = f'docker logs {container_id} --tail {tail} -f -t 2>&1'
            
            async with connection.create_process(cmd) as process:
                async for line in process.stdout:
                    line = line.strip()
                    if line:
                        log_entry = self._parse_log_line(
                            container_id, container_name, line, "stdout", True, system
                        )
                        if log_entry:
                            yield log_entry
                            
        except Exception as e:
            logger.error(f"Error streaming logs from {system_id}/{container_id}: {e}")
    
    def _parse_container_json(self, data: dict, system: RemoteSystem) -> Optional[ContainerInfo]:
        """Parse container info from docker ps JSON output."""
        try:
            # Parse ports
            ports = []
            ports_str = data.get("Ports", "")
            if ports_str:
                # Parse format like "0.0.0.0:8080->80/tcp, 443/tcp"
                for port_mapping in ports_str.split(", "):
                    if port_mapping:
                        ports.append(port_mapping)
            
            # Parse labels
            labels = {}
            labels_str = data.get("Labels", "")
            if labels_str:
                for label in labels_str.split(","):
                    if "=" in label:
                        k, v = label.split("=", 1)
                        labels[k] = v
            
            # Determine state from Status
            status = data.get("Status", "")
            state = "unknown"
            if "Up" in status:
                state = "running"
            elif "Exited" in status:
                state = "exited"
            elif "Created" in status:
                state = "created"
            
            return ContainerInfo(
                id=data.get("ID", "")[:12],
                name=data.get("Names", "unknown"),
                image=data.get("Image", "unknown"),
                status=data.get("Status", "unknown"),
                state=state,
                created=data.get("CreatedAt", ""),
                ports=ports,
                labels=labels,
                system_id=system.id,
                system_name=system.name,
            )
        except Exception as e:
            logger.error(f"Error parsing container JSON: {e}")
            return None
    
    def _parse_container_inspect(self, data: dict, system: RemoteSystem) -> Optional[ContainerInfo]:
        """Parse container info from docker inspect output."""
        try:
            # Extract port mappings
            ports = []
            port_bindings = data.get("NetworkSettings", {}).get("Ports", {})
            for container_port, bindings in port_bindings.items():
                if bindings:
                    for binding in bindings:
                        ports.append(f"{binding.get('HostPort', '?')}:{container_port}")
                else:
                    ports.append(container_port)
            
            return ContainerInfo(
                id=data.get("Id", "")[:12],
                name=data.get("Name", "unknown").lstrip("/"),
                image=data.get("Config", {}).get("Image", "unknown"),
                status=data.get("State", {}).get("Status", "unknown"),
                state=data.get("State", {}).get("Status", "unknown"),
                created=data.get("Created", ""),
                ports=ports,
                labels=data.get("Config", {}).get("Labels", {}),
                system_id=system.id,
                system_name=system.name,
            )
        except Exception as e:
            logger.error(f"Error parsing container inspect: {e}")
            return None
    
    def _parse_log_line(
        self,
        container_id: str,
        container_name: str,
        line: str,
        stream: str,
        has_timestamp: bool,
        system: RemoteSystem
    ) -> Optional[ContainerLog]:
        """Parse a log line into a ContainerLog model."""
        if not line:
            return None
        
        timestamp = None
        message = line
        
        if has_timestamp:
            # Try to parse timestamp (format: 2024-01-15T10:30:45.123456789Z)
            timestamp_pattern = r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z?)\s*(.*)$'
            match = re.match(timestamp_pattern, line)
            if match:
                try:
                    ts_str = match.group(1)
                    # Truncate nanoseconds to microseconds for Python datetime
                    if '.' in ts_str:
                        base, frac = ts_str.rstrip('Z').split('.')
                        frac = frac[:6]  # Keep only 6 digits
                        ts_str = f"{base}.{frac}"
                    timestamp = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    message = match.group(2)
                except (ValueError, IndexError):
                    pass
        
        return ContainerLog(
            container_id=container_id,
            container_name=container_name,
            timestamp=timestamp,
            message=message,
            stream=stream,
            system_id=system.id,
            system_name=system.name,
        )


# Global instance
remote_docker_service = RemoteDockerService()
