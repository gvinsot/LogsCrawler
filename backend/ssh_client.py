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
from . import utils

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
    
    async def run_shell_command(self, command: str) -> Tuple[bool, str]:
        """Execute a shell command and return (success, output).
        
        This is a convenience wrapper around run_command for stack operations.
        """
        stdout, stderr, exit_code = await self.run_command(command)
        output = stdout + stderr if stderr else stdout
        return exit_code == 0, output.strip()
    
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
        """Get list of all Docker containers.

        Optimized to use a single docker inspect command for all containers
        instead of N separate commands per container.
        """
        # Get all container IDs first
        id_cmd = "docker ps -aq"
        id_stdout, _, id_code = await self.run_command(id_cmd)

        if id_code != 0 or not id_stdout.strip():
            return []

        container_ids = id_stdout.strip().split("\n")
        if not container_ids or container_ids == ['']:
            return []

        # Batch inspect all containers in one command (much faster than N commands)
        # Using JSON array output for all containers at once
        inspect_cmd = f"docker inspect {' '.join(container_ids)}"
        inspect_stdout, inspect_stderr, inspect_code = await self.run_command(inspect_cmd)

        if inspect_code != 0:
            logger.error("Failed to inspect containers", host=self.config.name, error=inspect_stderr)
            return []

        containers = []
        try:
            all_data = json.loads(inspect_stdout)

            for data in all_data:
                try:
                    # Parse status from State
                    state = data.get("State", {})
                    status_str = state.get("Status", "unknown").lower()
                    try:
                        status = ContainerStatus(status_str)
                    except ValueError:
                        status = ContainerStatus.EXITED

                    # Get labels from Config
                    labels = data.get("Config", {}).get("Labels", {}) or {}

                    # Parse created time
                    created_str = data.get("Created", "")
                    try:
                        created = datetime.fromisoformat(created_str.replace("Z", "+00:00")[:26])
                    except:
                        created = datetime.now()

                    # Parse name (remove leading /)
                    name = data.get("Name", "/unknown").lstrip("/")

                    # Parse ports from NetworkSettings
                    ports = {}
                    port_bindings = data.get("HostConfig", {}).get("PortBindings", {}) or {}
                    for container_port, host_bindings in port_bindings.items():
                        if host_bindings:
                            for binding in host_bindings:
                                host_port = f"{binding.get('HostIp', '')}:{binding.get('HostPort', '')}"
                                ports[container_port] = host_port

                    container = ContainerInfo(
                        id=data["Id"][:12],
                        name=name,
                        image=data.get("Config", {}).get("Image", "unknown"),
                        status=status,
                        created=created,
                        host=self.config.name,
                        compose_project=labels.get("com.docker.compose.project"),
                        compose_service=labels.get("com.docker.compose.service"),
                        ports=ports,
                        labels=labels,
                    )
                    containers.append(container)

                except Exception as e:
                    logger.error("Failed to parse container", host=self.config.name, error=str(e))

        except json.JSONDecodeError as e:
            logger.error("Failed to parse docker inspect output", host=self.config.name, error=str(e))

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
        return utils.parse_memory_string(mem_str)
    
    def _parse_io(self, io_str: str) -> Tuple[int, int]:
        """Parse I/O string like '100MB / 50MB'."""
        return utils.parse_io_string(io_str)
    
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
        
        # GPU (AMD rocm-smi first, then NVIDIA nvidia-smi)
        gpu_percent, gpu_mem_used, gpu_mem_total = await self._get_gpu_metrics()
        
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
    
    async def _get_gpu_metrics(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Get GPU metrics using rocm-smi (AMD) or nvidia-smi (NVIDIA)."""
        # Try AMD GPU first (rocm-smi with CSV format - includes all info in one call)
        rocm_cmd = "rocm-smi --showuse --showmeminfo vram --csv 2>/dev/null"
        rocm_out, rocm_err, rocm_code = await self.run_command(rocm_cmd)
        logger.debug("rocm-smi output", returncode=rocm_code, stdout=rocm_out, stderr=rocm_err)
        
        if rocm_code == 0 and rocm_out.strip():
            gpu_percent, gpu_mem_used, gpu_mem_total = utils.parse_rocm_smi_csv(rocm_out)
            if gpu_percent is not None or gpu_mem_used is not None:
                return gpu_percent, gpu_mem_used, gpu_mem_total
        
        # Fallback to NVIDIA GPU
        nvidia_cmd = "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null"
        nvidia_out, _, nvidia_code = await self.run_command(nvidia_cmd)
        logger.debug("nvidia-smi output", returncode=nvidia_code, stdout=nvidia_out)
        
        if nvidia_code == 0 and nvidia_out.strip():
            return utils.parse_nvidia_smi_csv(nvidia_out)
        
        return None, None, None
    
    async def get_container_logs(
        self, 
        container_id: str,
        container_name: str,
        since: Optional[datetime] = None,
        tail: Optional[int] = 500,
        compose_project: Optional[str] = None,
        compose_service: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> List[LogEntry]:
        """Get container logs.
        
        Args:
            container_id: Docker container ID
            container_name: Container name for logging
            since: If set, fetch all logs since this timestamp (ignores tail)
            tail: If since is None, limit to last N lines (initial fetch)
            compose_project: Optional compose project name
            compose_service: Optional compose service name
            task_id: Optional Swarm task ID (unused for SSH, included for API compatibility)
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
        # Filter out known noise
        if utils.should_filter_log_line(line):
            return None
        
        # Extract timestamp and message
        timestamp, message = utils.extract_timestamp_and_message(line)
        
        # Parse log level, HTTP status, and structured fields
        level, http_status, parsed_fields = utils.parse_log_message(message)
        
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

    async def exec_command(self, container_id: str, command: List[str]) -> Tuple[bool, str]:
        """Execute a command inside a container using docker exec.

        Args:
            container_id: The container ID
            command: Command as list of strings (e.g., ["printenv"])

        Returns:
            Tuple of (success, output/error)
        """
        # Build command - properly quote each argument
        cmd_args = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in command)
        cmd = f"docker exec {container_id} {cmd_args}"

        stdout, stderr, code = await self.run_command(cmd)

        if code == 0:
            return True, stdout
        else:
            return False, stderr or "Command failed"

    async def remove_stack(self, stack_name: str) -> Tuple[bool, str]:
        """Remove a Docker Swarm stack."""
        cmd = f"docker stack rm {stack_name}"
        stdout, stderr, code = await self.run_command(cmd)
        
        if code == 0:
            return True, f"Stack '{stack_name}' removed successfully"
        else:
            return False, stderr or "Failed to remove stack"

    async def remove_service(self, service_name: str) -> Tuple[bool, str]:
        """Remove a Docker Swarm service."""
        cmd = f"docker service rm {service_name}"
        stdout, stderr, code = await self.run_command(cmd)
        
        if code == 0:
            return True, f"Service '{service_name}' removed successfully"
        else:
            return False, stderr or "Failed to remove service"

    async def update_service_image(self, service_name: str, new_tag: str) -> Tuple[bool, str]:
        """Update a Docker Swarm service's image tag."""
        # Get current image
        get_image_cmd = f"docker service inspect {service_name} --format '{{{{.Spec.TaskTemplate.ContainerSpec.Image}}}}'"
        stdout, stderr, code = await self.run_command(get_image_cmd)
        
        if code != 0:
            return False, f"Service '{service_name}' not found"
        
        current_image = stdout.strip()
        if not current_image:
            return False, f"Service '{service_name}' has no image configured"
        
        # Remove digest if present
        if "@sha256:" in current_image:
            current_image = current_image.split("@sha256:")[0]
        
        # Get base image name without tag
        if ":" in current_image:
            image_base = current_image.rsplit(":", 1)[0]
        else:
            image_base = current_image
        
        new_image = f"{image_base}:{new_tag}"
        
        # Update service with new image
        update_cmd = f"docker service update --image {new_image} --force {service_name}"
        stdout, stderr, code = await self.run_command(update_cmd)
        
        if code == 0:
            return True, f"Service '{service_name}' updated to image '{new_image}'"
        else:
            return False, f"Failed to update service: {stderr or stdout}"

    async def get_service_logs(self, service_name: str, tail: int = 200) -> List[dict]:
        """Get logs for a Docker Swarm service."""
        cmd = f"docker service logs --tail {tail} --timestamps {service_name}"
        stdout, stderr, code = await self.run_command(cmd)
        
        if code != 0:
            logger.error("Failed to get service logs", service=service_name, error=stderr)
            return []
        
        logs = []
        for line in stdout.split('\n'):
            if not line.strip():
                continue
            # Parse timestamp and message
            # Format: service_name.1.xxx@node | 2024-01-01T00:00:00.123456789Z message
            try:
                if '|' in line:
                    prefix, rest = line.split('|', 1)
                    rest = rest.strip()
                    if rest and rest[0].isdigit():
                        ts_end = rest.find(' ')
                        if ts_end > 0:
                            timestamp = rest[:ts_end]
                            message = rest[ts_end+1:]
                        else:
                            timestamp = None
                            message = rest
                    else:
                        timestamp = None
                        message = rest
                else:
                    timestamp = None
                    message = line
                
                logs.append({
                    "timestamp": timestamp,
                    "message": message,
                    "service": service_name,
                    "stream": "stdout",
                })
            except Exception:
                logs.append({
                    "timestamp": None,
                    "message": line,
                    "service": service_name,
                    "stream": "stdout",
                })
        
        return logs
