"""Docker service for container management and log retrieval."""

import docker
from docker import APIClient
from docker.errors import DockerException, NotFound
from typing import List, Optional, AsyncGenerator
import asyncio
from datetime import datetime
import re
import logging
import os
from pathlib import Path

from app.models import ContainerInfo, ContainerLog
from app.config import settings

logger = logging.getLogger(__name__)


class DockerService:
    """Service for interacting with Docker daemon."""
    
    def __init__(self):
        """Initialize Docker client."""
        self._client: Optional[docker.DockerClient] = None
        
    @property
    def client(self) -> docker.DockerClient:
        """Get or create Docker client."""
        if self._client is None:
            # Clear any problematic environment variables that might interfere
            # Save original values
            original_docker_host = os.environ.get('DOCKER_HOST')
            
            # Determine which Docker endpoint to use
            socket_path = Path("/var/run/docker.sock")
            base_url = None
            
            if settings.docker_host:
                base_url = settings.docker_host
                logger.info(f"Using configured Docker host: {base_url}")
                # Temporarily set DOCKER_HOST to our explicit value
                os.environ['DOCKER_HOST'] = base_url
            elif socket_path.exists():
                # Use the socket path directly - docker library handles unix:// automatically
                base_url = "unix:///var/run/docker.sock"
                logger.info("Using Unix socket: /var/run/docker.sock")
                # Don't set DOCKER_HOST - let the library handle it via base_url parameter
            else:
                # Socket not found, try docker.from_env() but clear DOCKER_HOST first
                if 'DOCKER_HOST' in os.environ:
                    del os.environ['DOCKER_HOST']
                logger.info("Socket not found, trying docker.from_env()")
                try:
                    self._client = docker.from_env()
                    self._client.ping()
                    logger.info("Docker client connected via docker.from_env()")
                    # Restore original DOCKER_HOST if we had one
                    if original_docker_host:
                        os.environ['DOCKER_HOST'] = original_docker_host
                    return self._client
                except Exception as e:
                    logger.error(f"docker.from_env() failed: {e}")
                    if original_docker_host:
                        os.environ['DOCKER_HOST'] = original_docker_host
                    raise Exception(
                        f"Docker socket not found at {socket_path} and docker.from_env() failed. "
                        "Make sure Docker is running and the socket is mounted correctly. "
                        f"Error: {e}"
                    )
            
            # Create client with explicit base_url
            try:
                # Try using APIClient first to validate connection
                # This helps avoid the "http+docker" scheme issue
                try:
                    api_client = APIClient(base_url=base_url)
                    api_client.ping()
                    api_client.close()
                except Exception as api_err:
                    logger.warning(f"APIClient test failed: {api_err}, trying DockerClient directly")
                
                # Create the high-level client
                # Clear DOCKER_HOST to avoid conflicts
                if 'DOCKER_HOST' in os.environ:
                    del os.environ['DOCKER_HOST']
                
                self._client = docker.DockerClient(base_url=base_url)
                # Test connection
                self._client.ping()
                logger.info(f"Docker client connected successfully via {base_url}")
            except DockerException as e:
                logger.error(f"Failed to connect to Docker daemon at {base_url}: {e}")
                # Restore original DOCKER_HOST
                if original_docker_host:
                    os.environ['DOCKER_HOST'] = original_docker_host
                elif 'DOCKER_HOST' in os.environ and base_url:
                    # Remove the one we set
                    del os.environ['DOCKER_HOST']
                raise Exception(
                    f"Cannot connect to Docker daemon at {base_url}. "
                    "Make sure Docker is running and the socket is accessible. "
                    f"Error: {e}"
                )
            except Exception as e:
                logger.error(f"Unexpected error connecting to Docker: {e}")
                # Restore original DOCKER_HOST
                if original_docker_host:
                    os.environ['DOCKER_HOST'] = original_docker_host
                elif 'DOCKER_HOST' in os.environ and base_url:
                    del os.environ['DOCKER_HOST']
                raise Exception(f"Cannot connect to Docker daemon: {e}")
            
            # Restore original DOCKER_HOST after successful connection
            if original_docker_host and base_url:
                os.environ['DOCKER_HOST'] = original_docker_host
            elif 'DOCKER_HOST' in os.environ and base_url and not original_docker_host:
                # We set it, but there was no original, so we can leave it or clear it
                # Leave it for now as it might be needed
                pass
                
        return self._client
    
    def is_connected(self) -> bool:
        """Check if Docker daemon is accessible."""
        try:
            self.client.ping()
            return True
        except Exception as e:
            logger.debug(f"Docker connection check failed: {e}")
            return False
    
    def get_containers(self, all_containers: bool = True) -> List[ContainerInfo]:
        """Get list of all containers."""
        try:
            if not self.is_connected():
                raise Exception("Docker daemon is not connected. Please check Docker is running and socket is accessible.")
            containers = self.client.containers.list(all=all_containers)
            return [self._container_to_info(c) for c in containers]
        except DockerException as e:
            logger.error(f"Failed to list containers: {e}")
            raise Exception(f"Failed to list containers: {e}")
        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            raise
    
    def get_container(self, container_id: str) -> Optional[ContainerInfo]:
        """Get a specific container by ID or name."""
        try:
            container = self.client.containers.get(container_id)
            return self._container_to_info(container)
        except NotFound:
            return None
        except DockerException as e:
            raise Exception(f"Failed to get container: {e}")
    
    def get_logs(
        self,
        container_id: str,
        tail: int = 100,
        since: Optional[datetime] = None,
        timestamps: bool = True
    ) -> List[ContainerLog]:
        """Get logs from a container."""
        try:
            container = self.client.containers.get(container_id)
            
            # Fetch logs
            logs_stdout = container.logs(
                stdout=True,
                stderr=False,
                tail=tail,
                since=since,
                timestamps=timestamps
            ).decode("utf-8", errors="replace")
            
            logs_stderr = container.logs(
                stdout=False,
                stderr=True,
                tail=tail,
                since=since,
                timestamps=timestamps
            ).decode("utf-8", errors="replace")
            
            result = []
            container_name = container.name
            
            # Parse stdout logs
            for line in logs_stdout.strip().split("\n"):
                if line:
                    log_entry = self._parse_log_line(
                        container_id, container_name, line, "stdout", timestamps
                    )
                    if log_entry:
                        result.append(log_entry)
            
            # Parse stderr logs
            for line in logs_stderr.strip().split("\n"):
                if line:
                    log_entry = self._parse_log_line(
                        container_id, container_name, line, "stderr", timestamps
                    )
                    if log_entry:
                        result.append(log_entry)
            
            # Sort by timestamp if available
            result.sort(key=lambda x: x.timestamp or datetime.min)
            
            return result
            
        except NotFound:
            return []
        except DockerException as e:
            raise Exception(f"Failed to get logs: {e}")
    
    async def stream_logs(
        self,
        container_id: str,
        tail: int = 50
    ) -> AsyncGenerator[ContainerLog, None]:
        """Stream logs from a container in real-time."""
        try:
            container = self.client.containers.get(container_id)
            container_name = container.name
            
            # Use a queue to bridge blocking Docker API with async
            # Use a larger queue size to handle bursts
            log_queue = asyncio.Queue(maxsize=1000)
            loop = asyncio.get_event_loop()
            stop_event = asyncio.Event()
            
            def read_logs():
                """Blocking function to read logs from Docker."""
                try:
                    logger.debug(f"Starting log reader thread for {container_id}")
                    log_stream = container.logs(
                        stdout=True,
                        stderr=True,
                        follow=True,
                        tail=tail,
                        timestamps=True,
                        stream=True
                    )
                    log_count = 0
                    for log_bytes in log_stream:
                        if stop_event.is_set():
                            break
                        log_count += 1
                        # Put log bytes in queue (use put_nowait, but handle full queue)
                        try:
                            # Try non-blocking first
                            loop.call_soon_threadsafe(log_queue.put_nowait, log_bytes)
                        except:
                            # Queue full, try blocking put via a future
                            try:
                                future = asyncio.run_coroutine_threadsafe(log_queue.put(log_bytes), loop)
                                future.result(timeout=0.1)  # Wait max 100ms
                            except Exception as e:
                                logger.warning(f"Queue full for {container_id}, dropping log")
                                # Skip this log if queue is full
                                continue
                        
                        if log_count % 50 == 0:
                            logger.debug(f"Read {log_count} log chunks from {container_id}")
                    
                    logger.info(f"Log reader finished for {container_id} (read {log_count} chunks)")
                    # Signal end
                    try:
                        loop.call_soon_threadsafe(log_queue.put_nowait, None)
                    except:
                        pass
                except Exception as e:
                    logger.error(f"Error in log stream reader for {container_id}: {e}", exc_info=True)
                    try:
                        loop.call_soon_threadsafe(log_queue.put_nowait, None)  # Signal end
                    except:
                        pass
            
            # Start reading logs in executor
            executor_task = loop.run_in_executor(None, read_logs)
            
            try:
                # Yield logs from queue
                while True:
                    try:
                        # Wait for log data with timeout
                        log_bytes = await asyncio.wait_for(log_queue.get(), timeout=2.0)
                        
                        if log_bytes is None:
                            # End signal
                            logger.debug(f"Received end signal for {container_id}")
                            break
                        
                        line = log_bytes.decode("utf-8", errors="replace").strip()
                        if line:
                            log_entry = self._parse_log_line(
                                container_id, container_name, line, "stdout", True
                            )
                            if log_entry:
                                yield log_entry
                                
                    except asyncio.TimeoutError:
                        # Check if container still exists and is running
                        try:
                            container.reload()
                            if container.status != 'running':
                                logger.info(f"Container {container_id} is not running ({container.status})")
                                break
                        except NotFound:
                            logger.warning(f"Container {container_id} no longer exists")
                            break
                        except Exception:
                            # Container might be stopped, continue waiting
                            pass
                        continue
                    except Exception as e:
                        logger.warning(f"Error processing log for {container_id}: {e}")
                        await asyncio.sleep(0.1)
            finally:
                # Signal the reader to stop
                stop_event.set()
                # Cancel executor task if still running
                if not executor_task.done():
                    executor_task.cancel()
                
        except NotFound:
            logger.warning(f"Container {container_id} not found for log streaming")
            return
        except DockerException as e:
            logger.error(f"Failed to stream logs from {container_id}: {e}")
            raise Exception(f"Failed to stream logs: {e}")
        except Exception as e:
            logger.error(f"Unexpected error streaming logs from {container_id}: {e}")
            return
    
    def get_all_logs(self, tail: int = 50) -> List[ContainerLog]:
        """Get recent logs from all running containers."""
        if not self.is_connected():
            raise Exception("Docker daemon is not connected. Please check Docker is running and socket is accessible.")
        
        all_logs = []
        try:
            containers = self.get_containers(all_containers=False)  # Only running
        except Exception as e:
            logger.error(f"Failed to get containers for logs: {e}")
            raise
        
        for container in containers:
            try:
                logs = self.get_logs(container.id, tail=tail)
                all_logs.extend(logs)
            except Exception as e:
                logger.warning(f"Failed to get logs from container {container.id}: {e}")
                continue
        
        # Sort by timestamp
        all_logs.sort(key=lambda x: x.timestamp or datetime.min)
        return all_logs
    
    def _container_to_info(self, container) -> ContainerInfo:
        """Convert Docker container object to ContainerInfo model."""
        # Extract port mappings
        ports = []
        if container.attrs.get("NetworkSettings", {}).get("Ports"):
            for container_port, host_bindings in container.attrs["NetworkSettings"]["Ports"].items():
                if host_bindings:
                    for binding in host_bindings:
                        ports.append(f"{binding.get('HostPort', '?')}:{container_port}")
                else:
                    ports.append(container_port)
        
        # Get image information - handle case where image might be deleted
        image_name = "unknown"
        try:
            # Try to get image from container object
            if container.image and container.image.tags:
                image_name = container.image.tags[0]
            elif container.image:
                image_name = container.image.short_id
            else:
                # Fallback to image ID from container attributes
                image_name = container.attrs.get("Image", "unknown")
        except (DockerException, AttributeError, IndexError) as e:
            # Image might have been deleted, use image ID from container attrs
            logger.debug(f"Could not access image for container {container.name}: {e}")
            image_name = container.attrs.get("Image", "unknown")
            # If it's a full SHA, shorten it
            if image_name.startswith("sha256:"):
                image_name = image_name[7:19]  # Take first 12 chars after sha256:
        
        return ContainerInfo(
            id=container.short_id,
            name=container.name,
            image=image_name,
            status=container.status,
            state=container.attrs.get("State", {}).get("Status", "unknown"),
            created=container.attrs.get("Created", ""),
            ports=ports,
            labels=container.labels,
            system_id="local",
            system_name="Local",
        )
    
    def _parse_log_line(
        self,
        container_id: str,
        container_name: str,
        line: str,
        stream: str,
        has_timestamp: bool
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
            system_id="local",
            system_name="Local",
        )


# Global service instance
docker_service = DockerService()
