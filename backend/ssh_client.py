"""SSH client for remote host operations."""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import asyncssh
import structlog

from .config import HostConfig
from .models import (
    ContainerInfo, ContainerStats, ContainerStatus, 
    HostMetrics, LogEntry, ContainerAction
)

logger = structlog.get_logger()


def is_localhost(hostname: str) -> bool:
    """Check if hostname refers to localhost."""
    return hostname.lower() in ("localhost", "127.0.0.1", "::1")


class SSHClient:
    """Async SSH client for Docker operations."""
    
    def __init__(self, host_config: HostConfig):
        self.config = host_config
        self._connection: Optional[asyncssh.SSHClientConnection] = None
        self._lock = asyncio.Lock()
        
        # Determine if we should run locally or via SSH:
        # - mode="local" forces local execution
        # - mode="ssh" forces SSH
        # - Otherwise, auto-detect based on hostname
        if host_config.mode == "local":
            self._is_local = True
        elif host_config.mode == "ssh":
            self._is_local = False
        else:
            self._is_local = is_localhost(host_config.hostname)
        
        if self._is_local:
            logger.info("Host configured as local (no SSH)", host=self.config.name)
        else:
            logger.info("Host configured for SSH", host=self.config.name, hostname=host_config.hostname)
        
    def _is_connection_open(self) -> bool:
        """Check if SSH connection is still open."""
        if self._connection is None:
            return False
        try:
            # asyncssh connections have a _transport attribute that is None when closed
            return self._connection._transport is not None and not self._connection._transport.is_closing()
        except Exception:
            return False
    
    async def connect(self) -> Optional[asyncssh.SSHClientConnection]:
        """Establish SSH connection (skipped for localhost)."""
        if self._is_local:
            return None
            
        async with self._lock:
            if not self._is_connection_open():
                options = {
                    "host": self.config.hostname,
                    "port": self.config.port,
                    "username": self.config.username,
                    "known_hosts": None,  # Disable host key checking
                }
                
                if self.config.ssh_key_path:
                    key_path = Path(self.config.ssh_key_path).expanduser()
                    options["client_keys"] = [str(key_path)]
                    
                self._connection = await asyncssh.connect(**options)
                logger.info("SSH connected", host=self.config.name)
                
            return self._connection
    
    async def disconnect(self):
        """Close SSH connection."""
        if self._is_local:
            return
            
        async with self._lock:
            if self._connection:
                try:
                    self._connection.close()
                    await self._connection.wait_closed()
                except Exception:
                    pass
                self._connection = None
                logger.info("SSH disconnected", host=self.config.name)
    
    async def close(self):
        """Alias for disconnect() to match HostClientProtocol."""
        await self.disconnect()
    
    async def run_command(self, command: str) -> Tuple[str, str, int]:
        """Execute command and return stdout, stderr, exit code."""
        if self._is_local:
            return await self._run_local_command(command)
        
        conn = await self.connect()
        result = await conn.run(command, check=False)
        return result.stdout or "", result.stderr or "", result.exit_status
    
    async def _run_local_command(self, command: str) -> Tuple[str, str, int]:
        """Execute command locally using asyncio subprocess."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                proc.returncode or 0,
            )
        except Exception as e:
            logger.error("Local command failed", command=command[:50], error=str(e))
            return "", str(e), 1
    
    async def get_containers(self) -> List[ContainerInfo]:
        """Get list of all Docker containers."""
        cmd = """docker ps -a --format '{{json .}}'"""
        stdout, stderr, code = await self.run_command(cmd)
        
        if code != 0:
            logger.error("Failed to list containers", host=self.config.name, error=stderr)
            return []
        
        containers = []
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                
                # Get compose info from labels
                label_cmd = f"docker inspect --format '{{{{json .Config.Labels}}}}' {data['ID']}"
                label_out, _, _ = await self.run_command(label_cmd)
                labels = json.loads(label_out.strip()) if label_out.strip() else {}
                
                # Parse status
                status_str = data.get("State", "unknown").lower()
                try:
                    status = ContainerStatus(status_str)
                except ValueError:
                    status = ContainerStatus.EXITED
                
                # Parse created time
                created_cmd = f"docker inspect --format '{{{{.Created}}}}' {data['ID']}"
                created_out, _, _ = await self.run_command(created_cmd)
                created_str = created_out.strip()
                try:
                    # Handle Docker's ISO format with nanoseconds
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00")[:26])
                except:
                    created = datetime.now()
                
                container = ContainerInfo(
                    id=data["ID"],
                    name=data["Names"],
                    image=data["Image"],
                    status=status,
                    created=created,
                    host=self.config.name,
                    compose_project=labels.get("com.docker.compose.project"),
                    compose_service=labels.get("com.docker.compose.service"),
                    ports=self._parse_ports(data.get("Ports", "")),
                    labels=labels,
                )
                containers.append(container)
                
            except Exception as e:
                logger.error("Failed to parse container", host=self.config.name, error=str(e))
                
        return containers
    
    def _parse_ports(self, ports_str: str) -> Dict[str, str]:
        """Parse Docker ports string."""
        ports = {}
        if not ports_str:
            return ports
        for mapping in ports_str.split(", "):
            if "->" in mapping:
                parts = mapping.split("->")
                ports[parts[1]] = parts[0]
        return ports
    
    async def get_container_stats(self, container_id: str, container_name: str) -> Optional[ContainerStats]:
        """Get container resource statistics."""
        cmd = f"docker stats {container_id} --no-stream --format '{{{{json .}}}}'"
        stdout, stderr, code = await self.run_command(cmd)
        
        if code != 0:
            return None
            
        try:
            data = json.loads(stdout.strip())
            
            # Parse CPU percentage
            cpu_str = data.get("CPUPerc", "0%").replace("%", "")
            cpu_percent = float(cpu_str) if cpu_str else 0.0
            
            # Parse memory
            mem_usage, mem_limit = self._parse_memory(data.get("MemUsage", "0B / 0B"))
            mem_perc_str = data.get("MemPerc", "0%").replace("%", "")
            mem_percent = float(mem_perc_str) if mem_perc_str else 0.0
            
            # Parse network I/O
            net_rx, net_tx = self._parse_io(data.get("NetIO", "0B / 0B"))
            
            # Parse block I/O
            block_r, block_w = self._parse_io(data.get("BlockIO", "0B / 0B"))
            
            return ContainerStats(
                container_id=container_id,
                container_name=container_name,
                host=self.config.name,
                timestamp=datetime.utcnow(),
                cpu_percent=cpu_percent,
                memory_usage_mb=mem_usage,
                memory_limit_mb=mem_limit,
                memory_percent=mem_percent,
                network_rx_bytes=net_rx,
                network_tx_bytes=net_tx,
                block_read_bytes=block_r,
                block_write_bytes=block_w,
            )
        except Exception as e:
            logger.error("Failed to parse stats", container=container_id, error=str(e))
            return None
    
    def _parse_memory(self, mem_str: str) -> Tuple[float, float]:
        """Parse memory usage string like '100MiB / 1GiB'."""
        parts = mem_str.split(" / ")
        if len(parts) != 2:
            return 0.0, 0.0
        return self._parse_size_mb(parts[0]), self._parse_size_mb(parts[1])
    
    def _parse_size_mb(self, size_str: str) -> float:
        """Convert size string to MB."""
        size_str = size_str.strip().upper()
        multipliers = {
            "B": 1 / (1024 * 1024),
            "KB": 1 / 1024,
            "KIB": 1 / 1024,
            "MB": 1,
            "MIB": 1,
            "GB": 1024,
            "GIB": 1024,
            "TB": 1024 * 1024,
            "TIB": 1024 * 1024,
        }
        for suffix, mult in multipliers.items():
            if size_str.endswith(suffix):
                try:
                    return float(size_str[:-len(suffix)].strip()) * mult
                except:
                    return 0.0
        return 0.0
    
    def _parse_io(self, io_str: str) -> Tuple[int, int]:
        """Parse I/O string like '100MB / 50MB'."""
        parts = io_str.split(" / ")
        if len(parts) != 2:
            return 0, 0
        return int(self._parse_size_mb(parts[0]) * 1024 * 1024), int(self._parse_size_mb(parts[1]) * 1024 * 1024)
    
    async def get_host_metrics(self) -> HostMetrics:
        """Get host-level resource metrics."""
        # CPU usage
        cpu_cmd = "grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {print usage}'"
        cpu_out, _, _ = await self.run_command(cpu_cmd)
        cpu_percent = float(cpu_out.strip()) if cpu_out.strip() else 0.0
        
        # Memory
        mem_cmd = "free -m | grep Mem"
        mem_out, _, _ = await self.run_command(mem_cmd)
        mem_parts = mem_out.split()
        mem_total = float(mem_parts[1]) if len(mem_parts) > 1 else 0.0
        mem_used = float(mem_parts[2]) if len(mem_parts) > 2 else 0.0
        mem_percent = (mem_used / mem_total * 100) if mem_total > 0 else 0.0
        
        # Disk
        disk_cmd = "df -BG / | tail -1"
        disk_out, _, _ = await self.run_command(disk_cmd)
        disk_parts = disk_out.split()
        disk_total = float(disk_parts[1].replace("G", "")) if len(disk_parts) > 1 else 0.0
        disk_used = float(disk_parts[2].replace("G", "")) if len(disk_parts) > 2 else 0.0
        disk_percent = float(disk_parts[4].replace("%", "")) if len(disk_parts) > 4 else 0.0
        
        # GPU (NVIDIA)
        gpu_percent = None
        gpu_mem_used = None
        gpu_mem_total = None
        gpu_cmd = "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null || echo ''"
        gpu_out, _, code = await self.run_command(gpu_cmd)
        if gpu_out.strip():
            gpu_parts = gpu_out.strip().split(", ")
            if len(gpu_parts) >= 3:
                gpu_percent = float(gpu_parts[0])
                gpu_mem_used = float(gpu_parts[1])
                gpu_mem_total = float(gpu_parts[2])
        
        return HostMetrics(
            host=self.config.name,
            timestamp=datetime.utcnow(),
            cpu_percent=cpu_percent,
            memory_total_mb=mem_total,
            memory_used_mb=mem_used,
            memory_percent=mem_percent,
            disk_total_gb=disk_total,
            disk_used_gb=disk_used,
            disk_percent=disk_percent,
            gpu_percent=gpu_percent,
            gpu_memory_used_mb=gpu_mem_used,
            gpu_memory_total_mb=gpu_mem_total,
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
        """Get container logs.
        
        Args:
            container_id: Docker container ID
            container_name: Container name for logging
            since: If set, fetch all logs since this timestamp (ignores tail)
            tail: If since is None, limit to last N lines (initial fetch)
            compose_project: Optional compose project name
            compose_service: Optional compose service name
        """
        cmd = f"docker logs {container_id} --timestamps"
        if since:
            # Fetch ALL logs since timestamp - don't use tail to avoid missing logs
            cmd += f" --since {since.isoformat()}"
        elif tail:
            # First fetch - limit to recent logs
            cmd += f" --tail {tail}"
        # else: fetch all logs (no limit) - rare case
        cmd += " 2>&1"
        
        stdout, _, code = await self.run_command(cmd)
        
        if code != 0:
            return []
        
        entries = []
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            entry = self._parse_log_line(
                line, container_id, container_name,
                compose_project, compose_service
            )
            if entry:
                entries.append(entry)
                
        return entries
    
    def _parse_log_line(
        self, 
        line: str, 
        container_id: str, 
        container_name: str,
        compose_project: Optional[str],
        compose_service: Optional[str],
    ) -> Optional[LogEntry]:
        """Parse a log line with timestamp."""
        # Filter out known non-critical warnings from external libraries
        # This warning comes from Go libraries parsing cgroup v2 "max" values
        # Check both in raw line and in structured log format (msg=...)
        if ("failed to parse CPU allowed micro secs" in line and 
            ("parsing \"max\"" in line or "parsing \\\"max\\\"" in line)):
            return None
        
        # Docker log format: 2024-01-15T10:30:00.123456789Z message
        timestamp_pattern = r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z?)\s+'
        match = re.match(timestamp_pattern, line)
        
        if match:
            timestamp_str = match.group(1)
            message = line[match.end():]
            try:
                timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")[:26])
            except:
                timestamp = datetime.utcnow()
        else:
            timestamp = datetime.utcnow()
            message = line
        
        # Try to detect log level
        level = self._detect_log_level(message)
        
        # Try to detect HTTP status
        http_status = self._detect_http_status(message)
        
        # Try to parse JSON
        parsed_fields = {}
        if message.strip().startswith("{"):
            try:
                parsed_fields = json.loads(message.strip())
                if "level" in parsed_fields:
                    level = parsed_fields["level"].upper()
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
            stream="stdout",
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
        # Common patterns: "HTTP/1.1" 200, status=200, status_code=200, [200], " 200 "
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
        cmd_map = {
            ContainerAction.START: f"docker start {container_id}",
            ContainerAction.STOP: f"docker stop {container_id}",
            ContainerAction.RESTART: f"docker restart {container_id}",
            ContainerAction.PAUSE: f"docker pause {container_id}",
            ContainerAction.UNPAUSE: f"docker unpause {container_id}",
            ContainerAction.REMOVE: f"docker rm -f {container_id}",
        }
        
        cmd = cmd_map.get(action)
        if not cmd:
            return False, f"Unknown action: {action}"
        
        stdout, stderr, code = await self.run_command(cmd)
        
        if code == 0:
            return True, f"Action {action.value} completed successfully"
        else:
            return False, stderr or "Command failed"

    async def get_swarm_stacks(self) -> Dict[str, List[str]]:
        """Get list of Docker Swarm stacks and their services.
        
        Returns:
            Dict mapping stack_name -> list of service names
        """
        cmd = "docker stack ls --format '{{.Name}}'"
        stdout, stderr, code = await self.run_command(cmd)
        
        if code != 0:
            return {}
        
        stacks = {}
        for stack_name in stdout.strip().split('\n'):
            stack_name = stack_name.strip()
            if not stack_name:
                continue
            
            # Get services for this stack
            services_cmd = f"docker stack services {stack_name} --format '{{.Name}}'"
            services_out, _, services_code = await self.run_command(services_cmd)
            
            if services_code == 0:
                services = [s.strip() for s in services_out.strip().split('\n') if s.strip()]
                stacks[stack_name] = services
            else:
                stacks[stack_name] = []
        
        return stacks
