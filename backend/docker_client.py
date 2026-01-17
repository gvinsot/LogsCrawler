"""Docker API client for direct Docker daemon communication."""

import asyncio
import aiohttp
import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import quote

import structlog

from .config import HostConfig
from .models import (
    ContainerInfo, ContainerStats, ContainerStatus,
    HostMetrics, LogEntry, ContainerAction
)

logger = structlog.get_logger()


class DockerAPIClient:
    """Direct Docker API client (via socket or TCP)."""
    
    def __init__(self, host_config: HostConfig):
        self.config = host_config
        self._session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.BaseConnector] = None
        
        docker_url = host_config.docker_url or "unix:///var/run/docker.sock"
        
        # Determine connection type
        if docker_url.startswith("unix://"):
            # Unix socket connection
            socket_path = docker_url.replace("unix://", "")
            self._base_url = "http://localhost"
            self._connector = aiohttp.UnixConnector(path=socket_path)
            logger.info("Docker API client (socket)", host=self.config.name, socket=socket_path)
        else:
            # TCP connection (http:// or tcp://)
            self._base_url = docker_url.replace("tcp://", "http://")
            self._connector = None
            logger.info("Docker API client (TCP)", host=self.config.name, url=self._base_url)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=self._connector)
        return self._session
    
    async def close(self):
        """Close the client session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def _request(self, method: str, endpoint: str, **kwargs) -> Tuple[Any, int]:
        """Make HTTP request to Docker API."""
        session = await self._get_session()
        url = f"{self._base_url}{endpoint}"
        
        try:
            async with session.request(method, url, **kwargs) as response:
                if response.content_type == "application/json":
                    data = await response.json()
                else:
                    data = await response.text()
                return data, response.status
        except Exception as e:
            logger.error("Docker API request failed", endpoint=endpoint, error=str(e))
            return None, 500
    
    async def get_containers(self) -> List[ContainerInfo]:
        """Get list of all Docker containers."""
        data, status = await self._request("GET", "/containers/json?all=true")
        
        if status != 200 or not data:
            return []
        
        containers = []
        for c in data:
            try:
                # Parse status
                state = c.get("State", "").lower()
                try:
                    container_status = ContainerStatus(state)
                except ValueError:
                    container_status = ContainerStatus.EXITED
                
                # Parse labels
                labels = c.get("Labels", {}) or {}
                
                # Parse created timestamp
                created_ts = c.get("Created", 0)
                created = datetime.fromtimestamp(created_ts) if created_ts else datetime.now()
                
                # Parse name (remove leading /)
                names = c.get("Names", ["/unknown"])
                name = names[0].lstrip("/") if names else "unknown"
                
                # Parse ports
                ports = {}
                for port in c.get("Ports", []):
                    private = f"{port.get('PrivatePort', '')}/{port.get('Type', 'tcp')}"
                    public = f"{port.get('IP', '')}:{port.get('PublicPort', '')}" if port.get('PublicPort') else None
                    if public:
                        ports[private] = public
                
                container = ContainerInfo(
                    id=c["Id"][:12],
                    name=name,
                    image=c.get("Image", "unknown"),
                    status=container_status,
                    created=created,
                    host=self.config.name,
                    compose_project=labels.get("com.docker.compose.project"),
                    compose_service=labels.get("com.docker.compose.service"),
                    ports=ports,
                    labels=labels,
                )
                containers.append(container)
                
            except Exception as e:
                logger.error("Failed to parse container", error=str(e))
        
        return containers
    
    async def get_container_stats(self, container_id: str, container_name: str) -> Optional[ContainerStats]:
        """Get container resource statistics."""
        # stream=false for one-shot stats
        data, status = await self._request("GET", f"/containers/{container_id}/stats?stream=false")
        
        if status != 200 or not data:
            return None
        
        try:
            # Calculate CPU percentage
            cpu_delta = data["cpu_stats"]["cpu_usage"]["total_usage"] - \
                       data["precpu_stats"]["cpu_usage"]["total_usage"]
            system_delta = data["cpu_stats"]["system_cpu_usage"] - \
                          data["precpu_stats"]["system_cpu_usage"]
            num_cpus = data["cpu_stats"].get("online_cpus", 1)
            
            cpu_percent = 0.0
            if system_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0
            
            # Memory stats
            memory_stats = data.get("memory_stats", {})
            memory_usage = memory_stats.get("usage", 0) / (1024 * 1024)  # MB
            memory_limit = memory_stats.get("limit", 1) / (1024 * 1024)  # MB
            memory_percent = (memory_usage / memory_limit * 100) if memory_limit > 0 else 0
            
            # Network stats
            networks = data.get("networks", {})
            net_rx = sum(n.get("rx_bytes", 0) for n in networks.values())
            net_tx = sum(n.get("tx_bytes", 0) for n in networks.values())
            
            # Block I/O stats
            blkio = data.get("blkio_stats", {}).get("io_service_bytes_recursive", []) or []
            block_read = sum(s.get("value", 0) for s in blkio if s.get("op") == "read")
            block_write = sum(s.get("value", 0) for s in blkio if s.get("op") == "write")
            
            return ContainerStats(
                container_id=container_id,
                container_name=container_name,
                host=self.config.name,
                timestamp=datetime.utcnow(),
                cpu_percent=round(cpu_percent, 2),
                memory_usage_mb=round(memory_usage, 2),
                memory_limit_mb=round(memory_limit, 2),
                memory_percent=round(memory_percent, 2),
                network_rx_bytes=net_rx,
                network_tx_bytes=net_tx,
                block_read_bytes=block_read,
                block_write_bytes=block_write,
            )
            
        except Exception as e:
            logger.error("Failed to parse container stats", container=container_id, error=str(e))
            return None
    
    async def get_host_metrics(self) -> HostMetrics:
        """Get host-level metrics (limited via Docker API)."""
        # Docker API doesn't provide host metrics directly
        # We'll get aggregated container stats
        data, status = await self._request("GET", "/info")
        
        cpu_percent = 0.0
        memory_total_mb = 0.0
        memory_used_mb = 0.0
        
        if status == 200 and data:
            memory_total_mb = data.get("MemTotal", 0) / (1024 * 1024)
            # Memory used requires summing container usage
            containers = await self.get_containers()
            running = [c for c in containers if c.status == ContainerStatus.RUNNING]
            
            for container in running[:10]:  # Limit to avoid too many API calls
                stats = await self.get_container_stats(container.id, container.name)
                if stats:
                    memory_used_mb += stats.memory_usage_mb
                    cpu_percent += stats.cpu_percent
        
        memory_percent = (memory_used_mb / memory_total_mb * 100) if memory_total_mb > 0 else 0
        
        # Try to get GPU metrics via nvidia-smi
        gpu_percent, gpu_mem_used, gpu_mem_total = await self._get_gpu_metrics()
        
        return HostMetrics(
            host=self.config.name,
            timestamp=datetime.utcnow(),
            cpu_percent=round(cpu_percent, 2),
            memory_total_mb=round(memory_total_mb, 2),
            memory_used_mb=round(memory_used_mb, 2),
            memory_percent=round(memory_percent, 2),
            disk_total_gb=0,
            disk_used_gb=0,
            disk_percent=0,
            gpu_percent=gpu_percent,
            gpu_memory_used_mb=gpu_mem_used,
            gpu_memory_total_mb=gpu_mem_total,
        )
    
    async def _get_gpu_metrics(self) -> tuple:
        """Try to get GPU metrics using nvidia-smi."""
        import subprocess
        try:
            # Run nvidia-smi to get GPU utilization
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(", ")
                if len(parts) >= 3:
                    return float(parts[0]), float(parts[1]), float(parts[2])
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError) as e:
            # nvidia-smi not available or failed
            pass
        return None, None, None
    
    async def get_container_logs(
        self,
        container_id: str,
        container_name: str,
        since: Optional[datetime] = None,
        tail: Optional[int] = 500,
        compose_project: Optional[str] = None,
        compose_service: Optional[str] = None,
    ) -> List[LogEntry]:
        """Get container logs via Docker API."""
        params = ["timestamps=true", "stdout=true", "stderr=true"]
        
        if since:
            # Docker API uses Unix timestamp
            params.append(f"since={int(since.timestamp())}")
        elif tail:
            params.append(f"tail={tail}")
        
        endpoint = f"/containers/{container_id}/logs?{'&'.join(params)}"
        
        session = await self._get_session()
        url = f"{self._base_url}{endpoint}"
        
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    return []
                
                # Docker logs come as a stream with header bytes
                raw_data = await response.read()
                entries = self._parse_docker_logs(
                    raw_data, container_id, container_name,
                    compose_project, compose_service
                )
                return entries
                
        except Exception as e:
            logger.error("Failed to get container logs", container=container_id, error=str(e))
            return []
    
    def _parse_docker_logs(
        self,
        raw_data: bytes,
        container_id: str,
        container_name: str,
        compose_project: Optional[str],
        compose_service: Optional[str],
    ) -> List[LogEntry]:
        """Parse Docker log stream format."""
        entries = []
        offset = 0
        
        while offset < len(raw_data):
            # Docker log format: [8 bytes header][payload]
            # Header: [stream_type(1), 0, 0, 0, size(4)]
            if offset + 8 > len(raw_data):
                break
                
            header = raw_data[offset:offset + 8]
            stream_type = header[0]  # 1=stdout, 2=stderr
            size = int.from_bytes(header[4:8], byteorder='big')
            
            if offset + 8 + size > len(raw_data):
                # Fallback: try parsing as plain text
                break
            
            payload = raw_data[offset + 8:offset + 8 + size]
            offset += 8 + size
            
            try:
                line = payload.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                
                entry = self._parse_log_line(
                    line, container_id, container_name,
                    compose_project, compose_service,
                    "stderr" if stream_type == 2 else "stdout"
                )
                if entry:
                    entries.append(entry)
                    
            except Exception:
                continue
        
        # Fallback: if no entries parsed, try plain text parsing
        if not entries and raw_data:
            try:
                text = raw_data.decode('utf-8', errors='replace')
                for line in text.strip().split('\n'):
                    if line.strip():
                        entry = self._parse_log_line(
                            line.strip(), container_id, container_name,
                            compose_project, compose_service, "stdout"
                        )
                        if entry:
                            entries.append(entry)
            except Exception:
                pass
        
        return entries
    
    def _parse_log_line(
        self,
        line: str,
        container_id: str,
        container_name: str,
        compose_project: Optional[str],
        compose_service: Optional[str],
        stream: str,
    ) -> Optional[LogEntry]:
        """Parse a log line with timestamp."""
        # Docker timestamp format: 2024-01-15T10:30:00.123456789Z
        timestamp_pattern = r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.?\d*Z?)\s+'
        match = re.match(timestamp_pattern, line)
        
        if match:
            timestamp_str = match.group(1)
            message = line[match.end():]
            try:
                # Handle various timestamp formats
                ts = timestamp_str.rstrip('Z')
                if '.' in ts:
                    ts = ts[:26]  # Truncate nanoseconds
                timestamp = datetime.fromisoformat(ts)
            except:
                timestamp = datetime.utcnow()
        else:
            timestamp = datetime.utcnow()
            message = line
        
        # Detect log level
        level = self._detect_log_level(message)
        
        # Detect HTTP status
        http_status = self._detect_http_status(message)
        
        # Try to parse JSON
        parsed_fields = {}
        if message.strip().startswith("{"):
            try:
                parsed_fields = json.loads(message.strip())
                if "level" in parsed_fields:
                    level = str(parsed_fields["level"]).upper()
                if "status" in parsed_fields and isinstance(parsed_fields["status"], int):
                    http_status = parsed_fields["status"]
            except:
                pass
        
        return LogEntry(
            timestamp=timestamp,
            host=self.config.name,
            container_id=container_id,
            container_name=container_name,
            compose_project=compose_project,
            compose_service=compose_service,
            stream=stream,
            message=message,
            level=level,
            http_status=http_status,
            parsed_fields=parsed_fields,
        )
    
    def _detect_log_level(self, message: str) -> Optional[str]:
        """Detect log level from message."""
        msg_upper = message.upper()
        levels = ["ERROR", "WARN", "WARNING", "INFO", "DEBUG", "CRITICAL", "FATAL"]
        for level in levels:
            if level in msg_upper:
                return level.replace("WARNING", "WARN")
        return None
    
    def _detect_http_status(self, message: str) -> Optional[int]:
        """Detect HTTP status code from message."""
        patterns = [
            r'HTTP/\d\.\d["\s]+(\d{3})',
            r'status[_\s]*(?:code)?[=:\s]+(\d{3})',
            r'\[(\d{3})\]',
            r'\s(\d{3})\s',
        ]
        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                status = int(match.group(1))
                if 100 <= status < 600:
                    return status
        return None
    
    async def execute_container_action(self, container_id: str, action: ContainerAction) -> Tuple[bool, str]:
        """Execute an action on a container."""
        action_map = {
            ContainerAction.START: ("POST", f"/containers/{container_id}/start"),
            ContainerAction.STOP: ("POST", f"/containers/{container_id}/stop"),
            ContainerAction.RESTART: ("POST", f"/containers/{container_id}/restart"),
            ContainerAction.PAUSE: ("POST", f"/containers/{container_id}/pause"),
            ContainerAction.UNPAUSE: ("POST", f"/containers/{container_id}/unpause"),
        }
        
        if action not in action_map:
            return False, f"Unknown action: {action}"
        
        method, endpoint = action_map[action]
        data, status = await self._request(method, endpoint)
        
        if status in (200, 204):
            return True, f"Action {action.value} completed successfully"
        else:
            error_msg = data if isinstance(data, str) else json.dumps(data) if data else "Unknown error"
            return False, error_msg
