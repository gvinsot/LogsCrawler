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
            cpu_stats = data.get("cpu_stats", {})
            precpu_stats = data.get("precpu_stats", {})

            # Calculate CPU percentage
            # Handle different Docker versions and platforms (Linux, Windows, macOS)
            cpu_percent = 0.0
            num_cpus = cpu_stats.get("online_cpus") or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1

            cpu_usage = cpu_stats.get("cpu_usage", {})
            precpu_usage = precpu_stats.get("cpu_usage", {})

            cpu_delta = cpu_usage.get("total_usage", 0) - precpu_usage.get("total_usage", 0)

            # Try system_cpu_usage first (Linux with cgroup v1)
            if "system_cpu_usage" in cpu_stats and "system_cpu_usage" in precpu_stats:
                system_delta = cpu_stats["system_cpu_usage"] - precpu_stats["system_cpu_usage"]
                if system_delta > 0 and cpu_delta > 0:
                    cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0
            else:
                # Fallback for cgroup v2, Windows, macOS - use usage_in_kernelmode + usage_in_usermode
                # Or calculate based on time period if available
                if cpu_delta > 0:
                    # Estimate based on 1 second polling interval (stats are roughly 1s apart)
                    # CPU time is in nanoseconds, so divide by 1e9 to get seconds
                    cpu_percent = (cpu_delta / 1e9) * 100.0 / num_cpus
                    # Cap at reasonable value
                    cpu_percent = min(cpu_percent, 100.0 * num_cpus)

            # Memory stats
            memory_stats = data.get("memory_stats", {})
            memory_usage = memory_stats.get("usage", 0) / (1024 * 1024)  # MB
            memory_limit = memory_stats.get("limit", 0) / (1024 * 1024)  # MB

            # Handle unlimited memory (very large limit value)
            if memory_limit > 1e12:  # > 1 PB, essentially unlimited
                memory_limit = memory_usage * 2 if memory_usage > 0 else 1024  # Show relative usage

            memory_percent = (memory_usage / memory_limit * 100) if memory_limit > 0 else 0

            # Network stats
            networks = data.get("networks", {})
            net_rx = sum(n.get("rx_bytes", 0) for n in networks.values())
            net_tx = sum(n.get("tx_bytes", 0) for n in networks.values())

            # Block I/O stats
            blkio = data.get("blkio_stats", {}).get("io_service_bytes_recursive", []) or []
            block_read = sum(s.get("value", 0) for s in blkio if s.get("op", "").lower() == "read")
            block_write = sum(s.get("value", 0) for s in blkio if s.get("op", "").lower() == "write")

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
        # Filter out known non-critical warnings from external libraries
        # This warning comes from Go libraries parsing cgroup v2 "max" values
        # Check both in raw line and in structured log format (msg=...)
        if ("failed to parse CPU allowed micro secs" in line and 
            ("parsing \"max\"" in line or "parsing \\\"max\\\"" in line)):
            return None
        
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
            ContainerAction.REMOVE: ("DELETE", f"/containers/{container_id}?force=true"),
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

    async def get_swarm_stacks(self) -> Dict[str, List[str]]:
        """Get list of Docker Swarm stacks and their services.
        
        Returns:
            Dict mapping stack_name -> list of service names
        """
        # Docker API doesn't have stack endpoints, use subprocess
        import subprocess
        try:
            result = subprocess.run(
                ["docker", "stack", "ls", "--format", "{{.Name}}"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {}
            
            stacks = {}
            for stack_name in result.stdout.strip().split('\n'):
                stack_name = stack_name.strip()
                if not stack_name:
                    continue
                
                # Get services for this stack
                services_result = subprocess.run(
                    ["docker", "stack", "services", stack_name, "--format", "{{.Name}}"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if services_result.returncode == 0:
                    services = [s.strip() for s in services_result.stdout.strip().split('\n') if s.strip()]
                    stacks[stack_name] = services
                else:
                    stacks[stack_name] = []
            
            return stacks
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return {}

    async def exec_command(self, container_id: str, command: List[str]) -> Tuple[bool, str]:
        """Execute a command inside a container using Docker exec API.

        Args:
            container_id: The container ID
            command: Command as list of strings (e.g., ["printenv"])

        Returns:
            Tuple of (success, output/error)
        """
        # Step 1: Create exec instance
        exec_config = {
            "AttachStdout": True,
            "AttachStderr": True,
            "Cmd": command
        }

        data, status = await self._request(
            "POST",
            f"/containers/{container_id}/exec",
            json=exec_config
        )

        if status != 201 or not data:
            error_msg = data if isinstance(data, str) else json.dumps(data) if data else "Failed to create exec"
            return False, error_msg

        exec_id = data.get("Id")
        if not exec_id:
            return False, "No exec ID returned"

        # Step 2: Start exec and get output
        start_config = {
            "Detach": False,
            "Tty": False
        }

        session = await self._get_session()
        url = f"{self._base_url}/exec/{exec_id}/start"

        try:
            async with session.post(url, json=start_config) as response:
                if response.status != 200:
                    return False, f"Exec start failed with status {response.status}"

                # Read the output - Docker sends multiplexed stream
                output = await response.read()

                # Parse multiplexed stream (header: 8 bytes, then payload)
                result = []
                pos = 0
                while pos < len(output):
                    if pos + 8 > len(output):
                        break
                    # Header: 1 byte type, 3 bytes padding, 4 bytes size (big endian)
                    size = int.from_bytes(output[pos+4:pos+8], 'big')
                    pos += 8
                    if pos + size > len(output):
                        break
                    chunk = output[pos:pos+size].decode('utf-8', errors='replace')
                    result.append(chunk)
                    pos += size

                return True, ''.join(result)
        except Exception as e:
            logger.error("Exec command failed", container_id=container_id, error=str(e))
            return False, str(e)

    # ============== Swarm-specific methods ==============

    async def get_swarm_nodes(self) -> List[Dict[str, Any]]:
        """Get all nodes in the Docker Swarm.

        Returns:
            List of node info dicts with id, hostname, role, status, availability
        """
        data, status = await self._request("GET", "/nodes")

        if status != 200 or not data:
            return []

        nodes = []
        for node in data:
            node_info = {
                "id": node.get("ID", "")[:12],
                "hostname": node.get("Description", {}).get("Hostname", "unknown"),
                "role": node.get("Spec", {}).get("Role", "worker"),
                "status": node.get("Status", {}).get("State", "unknown"),
                "availability": node.get("Spec", {}).get("Availability", "unknown"),
                "addr": node.get("Status", {}).get("Addr", ""),
                "engine_version": node.get("Description", {}).get("Engine", {}).get("EngineVersion", ""),
            }
            nodes.append(node_info)

        return nodes

    async def get_swarm_services(self) -> List[Dict[str, Any]]:
        """Get all services in the Docker Swarm.

        Returns:
            List of service info dicts
        """
        data, status = await self._request("GET", "/services")

        if status != 200 or not data:
            return []

        services = []
        for svc in data:
            spec = svc.get("Spec", {})
            service_info = {
                "id": svc.get("ID", "")[:12],
                "name": spec.get("Name", "unknown"),
                "image": spec.get("TaskTemplate", {}).get("ContainerSpec", {}).get("Image", "unknown"),
                "replicas": spec.get("Mode", {}).get("Replicated", {}).get("Replicas", 0),
                "stack": spec.get("Labels", {}).get("com.docker.stack.namespace", ""),
            }
            services.append(service_info)

        return services

    async def get_swarm_tasks(self) -> List[Dict[str, Any]]:
        """Get all tasks (container instances) across the Swarm.

        This returns information about where each container is running,
        enabling routing commands to the correct node.

        Returns:
            List of task info dicts with container_id, node_id, service, status
        """
        data, status = await self._request("GET", "/tasks")

        if status != 200 or not data:
            return []

        tasks = []
        for task in data:
            task_status = task.get("Status", {})
            container_status = task_status.get("ContainerStatus", {})

            task_info = {
                "id": task.get("ID", "")[:12],
                "node_id": task.get("NodeID", ""),
                "service_id": task.get("ServiceID", ""),
                "container_id": container_status.get("ContainerID", "")[:12] if container_status.get("ContainerID") else None,
                "state": task_status.get("State", "unknown"),
                "desired_state": task.get("DesiredState", "unknown"),
                "slot": task.get("Slot", 0),
            }

            # Only include running tasks with a container
            if task_info["container_id"] and task_info["state"] == "running":
                tasks.append(task_info)

        return tasks

    async def get_node_containers(self, node_id: str) -> List[ContainerInfo]:
        """Get containers running on a specific Swarm node.

        This uses tasks API to find containers on a node, then gets their details.
        Useful for Swarm routing when you want to query containers on worker nodes.
        """
        tasks = await self.get_swarm_tasks()
        node_tasks = [t for t in tasks if t["node_id"].startswith(node_id)]

        containers = []
        for task in node_tasks:
            if task["container_id"]:
                # Get container details
                data, status = await self._request("GET", f"/containers/{task['container_id']}/json")
                if status == 200 and data:
                    try:
                        labels = data.get("Config", {}).get("Labels", {}) or {}
                        name = data.get("Name", "unknown").lstrip("/")

                        container = ContainerInfo(
                            id=task["container_id"],
                            name=name,
                            image=data.get("Config", {}).get("Image", "unknown"),
                            status=ContainerStatus.RUNNING,
                            created=datetime.fromisoformat(data.get("Created", "").replace("Z", "+00:00")),
                            host=self.config.name,
                            compose_project=labels.get("com.docker.stack.namespace"),
                            compose_service=labels.get("com.docker.swarm.service.name"),
                            ports={},
                            labels=labels,
                        )
                        containers.append(container)
                    except Exception as e:
                        logger.error("Failed to parse swarm container", task_id=task["id"], error=str(e))

        return containers

    async def get_all_swarm_containers(self) -> Dict[str, List[ContainerInfo]]:
        """Get all containers across all Swarm nodes, grouped by node hostname.

        This is the main method for Swarm routing - it discovers all containers
        in the swarm and their locations, so commands can be routed through
        the manager instead of requiring direct access to worker nodes.

        Returns:
            Dict mapping node hostname to list of containers on that node
        """
        nodes = await self.get_swarm_nodes()
        tasks = await self.get_swarm_tasks()

        # Build node_id -> hostname mapping
        node_hostnames = {n["id"]: n["hostname"] for n in nodes}

        # Group tasks by node
        containers_by_node: Dict[str, List[ContainerInfo]] = {}
        for hostname in node_hostnames.values():
            containers_by_node[hostname] = []

        for task in tasks:
            if not task["container_id"]:
                continue

            # Find node hostname
            node_hostname = None
            for node_id, hostname in node_hostnames.items():
                if task["node_id"].startswith(node_id) or node_id.startswith(task["node_id"]):
                    node_hostname = hostname
                    break

            if not node_hostname:
                continue

            # Get container details
            data, status = await self._request("GET", f"/containers/{task['container_id']}/json")
            if status == 200 and data:
                try:
                    labels = data.get("Config", {}).get("Labels", {}) or {}
                    name = data.get("Name", "unknown").lstrip("/")

                    container = ContainerInfo(
                        id=task["container_id"],
                        name=name,
                        image=data.get("Config", {}).get("Image", "unknown"),
                        status=ContainerStatus.RUNNING,
                        created=datetime.fromisoformat(data.get("Created", "").replace("Z", "+00:00")),
                        host=node_hostname,  # Use the actual node hostname
                        compose_project=labels.get("com.docker.stack.namespace"),
                        compose_service=labels.get("com.docker.swarm.service.name"),
                        ports={},
                        labels=labels,
                    )
                    containers_by_node[node_hostname].append(container)
                except Exception as e:
                    logger.error("Failed to parse swarm container", task_id=task["id"], error=str(e))

        return containers_by_node
