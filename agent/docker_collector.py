"""Docker collector for the agent - collects containers, stats, and logs locally."""

import asyncio
import json
import re
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import structlog

logger = structlog.get_logger()


class DockerCollector:
    """Local Docker collector using Docker API."""

    def __init__(self, docker_url: str, host_name: str):
        self.docker_url = docker_url
        self.host_name = host_name
        self._session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.BaseConnector] = None
        self._closing = False
        self._last_log_timestamp: Dict[str, datetime] = {}

        # Determine connection type
        if docker_url.startswith("unix://"):
            socket_path = docker_url.replace("unix://", "")
            self._base_url = "http://localhost"
            self._connector = aiohttp.UnixConnector(path=socket_path)
            logger.info("Docker collector (socket)", socket=socket_path)
        else:
            self._base_url = docker_url.replace("tcp://", "http://")
            self._connector = None
            logger.info("Docker collector (TCP)", url=self._base_url)

    async def _get_session(self) -> Optional[aiohttp.ClientSession]:
        """Get or create aiohttp session."""
        if self._closing:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=self._connector)
        return self._session

    async def close(self):
        """Close the client session."""
        self._closing = True
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, endpoint: str, **kwargs) -> Tuple[Any, int]:
        """Make HTTP request to Docker API."""
        if self._closing:
            return None, 503

        session = await self._get_session()
        if session is None:
            return None, 503

        url = f"{self._base_url}{endpoint}"

        try:
            async with session.request(method, url, **kwargs) as response:
                if response.content_type == "application/json":
                    data = await response.json()
                else:
                    data = await response.text()
                return data, response.status
        except aiohttp.ClientError as e:
            if not self._closing:
                logger.error("Docker API request failed", endpoint=endpoint, error=str(e))
            return None, 500
        except Exception as e:
            if not self._closing:
                logger.error("Docker API request failed", endpoint=endpoint, error=str(e))
            return None, 500

    async def get_containers(self) -> List[Dict[str, Any]]:
        """Get list of all Docker containers."""
        data, status = await self._request("GET", "/containers/json?all=true")

        if status != 200 or not data:
            return []

        containers = []
        for c in data:
            try:
                container_id = c["Id"][:12]
                state = c.get("State", "").lower()
                labels = c.get("Labels", {}) or {}
                created_ts = c.get("Created", 0)
                created = datetime.fromtimestamp(created_ts) if created_ts else datetime.now()

                names = c.get("Names", ["/unknown"])
                name = names[0].lstrip("/") if names else "unknown"

                ports = {}
                for port in c.get("Ports", []):
                    private = f"{port.get('PrivatePort', '')}/{port.get('Type', 'tcp')}"
                    public = f"{port.get('IP', '')}:{port.get('PublicPort', '')}" if port.get('PublicPort') else None
                    if public:
                        ports[private] = public

                compose_project = (labels.get("com.docker.compose.project") or
                                   labels.get("com.docker.stack.namespace"))
                compose_service = (labels.get("com.docker.compose.service") or
                                   labels.get("com.docker.swarm.service.name"))

                containers.append({
                    "id": container_id,
                    "name": name,
                    "image": c.get("Image", "unknown"),
                    "status": state,
                    "created": created.isoformat(),
                    "host": self.host_name,
                    "compose_project": compose_project,
                    "compose_service": compose_service,
                    "ports": ports,
                    "labels": labels,
                })

            except Exception as e:
                logger.error("Failed to parse container", error=str(e))

        return containers

    async def get_container_stats(self, container_id: str, container_name: str) -> Optional[Dict[str, Any]]:
        """Get container resource statistics."""
        data, status = await self._request("GET", f"/containers/{container_id}/stats?stream=false")

        if status != 200 or not data:
            return None

        try:
            cpu_stats = data.get("cpu_stats", {})
            precpu_stats = data.get("precpu_stats", {})

            cpu_percent = 0.0
            num_cpus = cpu_stats.get("online_cpus") or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1

            cpu_usage = cpu_stats.get("cpu_usage", {})
            precpu_usage = precpu_stats.get("cpu_usage", {})

            cpu_delta = cpu_usage.get("total_usage", 0) - precpu_usage.get("total_usage", 0)

            if "system_cpu_usage" in cpu_stats and "system_cpu_usage" in precpu_stats:
                system_delta = cpu_stats["system_cpu_usage"] - precpu_stats["system_cpu_usage"]
                if system_delta > 0 and cpu_delta > 0:
                    cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0
            else:
                if cpu_delta > 0:
                    cpu_percent = (cpu_delta / 1e9) * 100.0 / num_cpus
                    cpu_percent = min(cpu_percent, 100.0 * num_cpus)

            memory_stats = data.get("memory_stats", {})
            memory_usage = memory_stats.get("usage", 0) / (1024 * 1024)
            memory_limit = memory_stats.get("limit", 0) / (1024 * 1024)

            if memory_limit > 1e12:
                memory_limit = memory_usage * 2 if memory_usage > 0 else 1024

            memory_percent = (memory_usage / memory_limit * 100) if memory_limit > 0 else 0

            networks = data.get("networks", {})
            net_rx = sum(n.get("rx_bytes", 0) for n in networks.values())
            net_tx = sum(n.get("tx_bytes", 0) for n in networks.values())

            blkio = data.get("blkio_stats", {}).get("io_service_bytes_recursive", []) or []
            block_read = sum(s.get("value", 0) for s in blkio if s.get("op", "").lower() == "read")
            block_write = sum(s.get("value", 0) for s in blkio if s.get("op", "").lower() == "write")

            return {
                "container_id": container_id,
                "container_name": container_name,
                "host": self.host_name,
                "timestamp": datetime.utcnow(),
                "cpu_percent": round(cpu_percent, 2),
                "memory_usage_mb": round(memory_usage, 2),
                "memory_limit_mb": round(memory_limit, 2),
                "memory_percent": round(memory_percent, 2),
                "network_rx_bytes": net_rx,
                "network_tx_bytes": net_tx,
                "block_read_bytes": block_read,
                "block_write_bytes": block_write,
            }

        except Exception as e:
            logger.error("Failed to parse container stats", container=container_id, error=str(e))
            return None

    async def get_host_metrics(self) -> Dict[str, Any]:
        """Get host-level metrics."""
        data, status = await self._request("GET", "/info")

        cpu_percent = 0.0
        memory_total_mb = 0.0
        memory_used_mb = 0.0

        if status == 200 and data:
            memory_total_mb = data.get("MemTotal", 0) / (1024 * 1024)
            containers = await self.get_containers()
            running = [c for c in containers if c.get("status") == "running"]

            for container in running[:10]:
                stats = await self.get_container_stats(container["id"], container["name"])
                if stats:
                    memory_used_mb += stats["memory_usage_mb"]
                    cpu_percent += stats["cpu_percent"]

        memory_percent = (memory_used_mb / memory_total_mb * 100) if memory_total_mb > 0 else 0

        gpu_percent, gpu_mem_used, gpu_mem_total = self._get_gpu_metrics()

        return {
            "host": self.host_name,
            "timestamp": datetime.utcnow(),
            "cpu_percent": round(cpu_percent, 2),
            "memory_total_mb": round(memory_total_mb, 2),
            "memory_used_mb": round(memory_used_mb, 2),
            "memory_percent": round(memory_percent, 2),
            "disk_total_gb": 0,
            "disk_used_gb": 0,
            "disk_percent": 0,
            "gpu_percent": gpu_percent,
            "gpu_memory_used_mb": gpu_mem_used,
            "gpu_memory_total_mb": gpu_mem_total,
        }

    def _get_gpu_metrics(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Try to get GPU metrics using nvidia-smi or rocm-smi."""
        # Try AMD GPU first
        try:
            result = subprocess.run(
                ["rocm-smi", "--showuse", "--showmeminfo", "vram", "--csv"],
                capture_output=True,
                text=True,
                timeout=5
            )
            logger.debug("rocm-smi output", returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                # Expected CSV format:
                # device,GPU use (%),VRAM Total Memory (B),VRAM Total Used Memory (B)
                # card0,0,1073741824,81498112
                for line in lines:
                    # Skip header line containing "device" or empty lines
                    line_lower = line.lower()
                    if "device" in line_lower or "gpu use" in line_lower or not line.strip():
                        continue
                    # Data lines start with "card0", "card1", etc.
                    if line_lower.startswith("card"):
                        parts = [p.strip() for p in line.split(",")]
                        logger.debug("rocm-smi CSV parts", parts=parts)
                        # parts[0]=device, parts[1]=GPU use (%), parts[2]=VRAM Total (B), parts[3]=VRAM Used (B)
                        if len(parts) >= 4:
                            try:
                                gpu_use = float(parts[1].replace('%', '').strip())
                                vram_total_bytes = float(parts[2].strip())
                                vram_used_bytes = float(parts[3].strip())
                                mem_total = vram_total_bytes / (1024 * 1024)  # Convert to MB
                                mem_used = vram_used_bytes / (1024 * 1024)    # Convert to MB
                                logger.info("AMD GPU metrics collected", gpu_percent=gpu_use, mem_used_mb=mem_used, mem_total_mb=mem_total)
                                return gpu_use, mem_used, mem_total
                            except (ValueError, IndexError) as e:
                                logger.warning("Failed to parse rocm-smi CSV line", line=line, error=str(e))
        except FileNotFoundError:
            logger.debug("rocm-smi not found, trying nvidia-smi")
        except subprocess.TimeoutExpired:
            logger.warning("rocm-smi command timed out")
        except Exception as e:
            logger.warning("rocm-smi failed", error=str(e))

        # Fallback to NVIDIA GPU
        try:
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
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            pass

        return None, None, None

    def _get_rocm_memory(self) -> Tuple[Optional[float], Optional[float]]:
        """Get AMD GPU memory usage via rocm-smi."""
        try:
            result = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True,
                text=True,
                timeout=5
            )
            logger.debug("rocm-smi memory output", returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
            if result.returncode == 0:
                total_mb = None
                used_mb = None
                for line in result.stdout.split("\n"):
                    line_upper = line.upper()
                    # Handle various output formats
                    if "TOTAL" in line_upper and "VRAM" in line_upper:
                        try:
                            # Extract numeric value (could be in bytes, KB, MB, GB)
                            value_str = line.split(":")[-1].strip()
                            total_mb = self._parse_memory_value(value_str)
                            logger.debug("Parsed VRAM Total", raw=value_str, mb=total_mb)
                        except Exception as e:
                            logger.warning("Failed to parse VRAM Total", line=line, error=str(e))
                    elif "USED" in line_upper and "VRAM" in line_upper:
                        try:
                            value_str = line.split(":")[-1].strip()
                            used_mb = self._parse_memory_value(value_str)
                            logger.debug("Parsed VRAM Used", raw=value_str, mb=used_mb)
                        except Exception as e:
                            logger.warning("Failed to parse VRAM Used", line=line, error=str(e))
                
                if total_mb is not None or used_mb is not None:
                    return used_mb, total_mb
        except FileNotFoundError:
            logger.debug("rocm-smi not found for memory query")
        except subprocess.TimeoutExpired:
            logger.warning("rocm-smi memory command timed out")
        except Exception as e:
            logger.warning("rocm-smi memory query failed", error=str(e))
        return None, None

    def _parse_memory_value(self, value_str: str) -> Optional[float]:
        """Parse memory value from rocm-smi, handling different units."""
        value_str = value_str.strip().upper()
        
        # Remove any units and get numeric value
        import re
        match = re.match(r'([\d.]+)\s*(B|KB|MB|GB|TB|BYTES)?', value_str, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            unit = (match.group(2) or 'B').upper()
            
            # Convert to MB
            if unit in ('B', 'BYTES'):
                return value / (1024 * 1024)
            elif unit == 'KB':
                return value / 1024
            elif unit == 'MB':
                return value
            elif unit == 'GB':
                return value * 1024
            elif unit == 'TB':
                return value * 1024 * 1024
        
        # Try as raw bytes if no unit found
        try:
            return float(value_str) / (1024 * 1024)
        except ValueError:
            return None

    async def get_container_logs(
        self,
        container_id: str,
        container_name: str,
        since: Optional[datetime] = None,
        tail: Optional[int] = 500,
        compose_project: Optional[str] = None,
        compose_service: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get container logs via Docker API."""
        params = ["timestamps=true", "stdout=true", "stderr=true"]

        if since:
            params.append(f"since={int(since.timestamp())}")
        elif tail:
            params.append(f"tail={tail}")

        endpoint = f"/containers/{container_id}/logs?{'&'.join(params)}"

        session = await self._get_session()
        if not session:
            return []

        url = f"{self._base_url}{endpoint}"

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    return []

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
    ) -> List[Dict[str, Any]]:
        """Parse Docker log stream format."""
        entries = []
        offset = 0

        while offset < len(raw_data):
            if offset + 8 > len(raw_data):
                break

            header = raw_data[offset:offset + 8]
            stream_type = header[0]
            size = int.from_bytes(header[4:8], byteorder='big')

            if offset + 8 + size > len(raw_data):
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
    ) -> Optional[Dict[str, Any]]:
        """Parse a log line with timestamp."""
        # Filter out known non-critical warnings
        if ("failed to parse CPU allowed micro secs" in line and
            ("parsing \"max\"" in line or "parsing \\\"max\\\"" in line)):
            return None

        timestamp_pattern = r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.?\d*Z?)\s+'
        match = re.match(timestamp_pattern, line)

        if match:
            timestamp_str = match.group(1)
            message = line[match.end():]
            try:
                ts = timestamp_str.rstrip('Z')
                if '.' in ts:
                    ts = ts[:26]
                timestamp = datetime.fromisoformat(ts)
            except:
                timestamp = datetime.utcnow()
        else:
            timestamp = datetime.utcnow()
            message = line

        level = self._detect_log_level(message)
        http_status = self._detect_http_status(message)

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

        return {
            "timestamp": timestamp,
            "host": self.host_name,
            "container_id": container_id,
            "container_name": container_name,
            "compose_project": compose_project,
            "compose_service": compose_service,
            "stream": stream,
            "message": message,
            "level": level,
            "http_status": http_status,
            "parsed_fields": parsed_fields,
        }

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
            r'status[=:\s]+(\d{3})',
            r'\s(\d{3})\s+\d+\s*$',
            r'"status":\s*(\d{3})',
        ]

        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                status = int(match.group(1))
                if 100 <= status <= 599:
                    return status
        return None

    async def collect_all_logs(self, tail: int = 500) -> List[Dict[str, Any]]:
        """Collect logs from all running containers."""
        containers = await self.get_containers()
        running = [c for c in containers if c.get("status") == "running"]

        all_logs = []
        for container in running:
            container_key = container["id"]
            last_timestamp = self._last_log_timestamp.get(container_key)

            logs = await self.get_container_logs(
                container_id=container["id"],
                container_name=container["name"],
                since=last_timestamp,
                tail=tail if last_timestamp is None else None,
                compose_project=container.get("compose_project"),
                compose_service=container.get("compose_service"),
            )

            if logs:
                all_logs.extend(logs)
                newest_log = max(logs, key=lambda x: x["timestamp"])
                self._last_log_timestamp[container_key] = newest_log["timestamp"] + timedelta(milliseconds=1)

        return all_logs

    async def collect_all_stats(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Collect host metrics and all container stats."""
        host_metrics = await self.get_host_metrics()

        containers = await self.get_containers()
        running = [c for c in containers if c.get("status") == "running"]

        container_stats = []
        for container in running:
            stats = await self.get_container_stats(container["id"], container["name"])
            if stats:
                container_stats.append(stats)

        return host_metrics, container_stats
