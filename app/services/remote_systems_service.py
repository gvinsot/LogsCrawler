"""
Remote Systems Service - Manages remote system configurations and SSH connections.
Stores remote system configurations in MongoDB and manages SSH connections for Docker access.
"""

import asyncio
import logging
import uuid
import asyncssh
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from app.models import RemoteSystem, RemoteSystemCreate, RemoteSystemUpdate, SystemStatus

logger = logging.getLogger(__name__)


@dataclass
class SSHConnection:
    """Wrapper for SSH connection with metadata."""
    connection: asyncssh.SSHClientConnection
    system_id: str
    last_used: datetime
    last_checked: datetime  # When we last verified the connection is alive


# Connection liveness check interval (seconds)
CONNECTION_CHECK_INTERVAL = 60


class RemoteSystemsService:
    """
    Service for managing remote systems and SSH connections.
    Uses MongoDB for persistence and maintains a pool of SSH connections.
    """
    
    def __init__(self):
        self._systems: Dict[str, RemoteSystem] = {}  # In-memory cache
        self._connections: Dict[str, SSHConnection] = {}  # SSH connection pool
        self._db = None
        self._initialized = False
    
    async def initialize(self, db=None):
        """Initialize the service with MongoDB connection."""
        if db is not None:
            self._db = db
            # Load systems from MongoDB
            try:
                async for doc in db.remote_systems.find():
                    system = RemoteSystem(
                        id=doc["id"],
                        name=doc["name"],
                        hostname=doc["hostname"],
                        username=doc["username"],
                        port=doc.get("port", 22),
                        ssh_key=doc.get("ssh_key"),  # Load SSH key from DB
                        status=SystemStatus(doc.get("status", "unknown")),
                        last_connected=doc.get("last_connected"),
                        last_error=doc.get("last_error"),
                        container_count=doc.get("container_count", 0),
                        created_at=doc.get("created_at", datetime.now()),
                    )
                    self._systems[system.id] = system
                logger.info(f"Loaded {len(self._systems)} remote systems from database")
            except Exception as e:
                logger.error(f"Failed to load remote systems: {e}")
        
        self._initialized = True
    
    async def _save_system(self, system: RemoteSystem):
        """Save a system to MongoDB."""
        if self._db is not None:
            try:
                await self._db.remote_systems.update_one(
                    {"id": system.id},
                    {"$set": system.model_dump()},
                    upsert=True
                )
            except Exception as e:
                logger.error(f"Failed to save system {system.id}: {e}")
    
    async def _delete_system_from_db(self, system_id: str):
        """Delete a system from MongoDB."""
        if self._db is not None:
            try:
                await self._db.remote_systems.delete_one({"id": system_id})
            except Exception as e:
                logger.error(f"Failed to delete system {system_id}: {e}")
    
    def _sanitize_system(self, system: RemoteSystem) -> RemoteSystem:
        """Return a copy of the system with sensitive data masked (for API responses)."""
        # Create a copy with ssh_key masked (indicate if key exists without exposing it)
        return RemoteSystem(
            id=system.id,
            name=system.name,
            hostname=system.hostname,
            username=system.username,
            port=system.port,
            ssh_key="***configured***" if system.ssh_key else None,  # Mask the actual key
            status=system.status,
            last_connected=system.last_connected,
            last_error=system.last_error,
            container_count=system.container_count,
            created_at=system.created_at,
        )
    
    def get_all_systems(self) -> List[RemoteSystem]:
        """Get all registered remote systems (sanitized for API)."""
        return [self._sanitize_system(s) for s in self._systems.values()]
    
    def get_system(self, system_id: str) -> Optional[RemoteSystem]:
        """Get a specific remote system by ID (sanitized for API)."""
        system = self._systems.get(system_id)
        return self._sanitize_system(system) if system else None
    
    def get_system_internal(self, system_id: str) -> Optional[RemoteSystem]:
        """Get a specific remote system by ID (with full data for internal use)."""
        return self._systems.get(system_id)
    
    async def create_system(self, data: RemoteSystemCreate) -> RemoteSystem:
        """Create a new remote system configuration."""
        system = RemoteSystem(
            id=str(uuid.uuid4())[:8],
            name=data.name,
            hostname=data.hostname,
            username=data.username,
            port=data.port,
            ssh_key=data.ssh_key,  # Store SSH key if provided
            status=SystemStatus.UNKNOWN,
            created_at=datetime.now(),
        )
        
        self._systems[system.id] = system
        await self._save_system(system)
        
        logger.info(f"Created remote system: {system.name} ({system.hostname})")
        return self._sanitize_system(system)
    
    async def update_system(self, system_id: str, data: RemoteSystemUpdate) -> Optional[RemoteSystem]:
        """Update an existing remote system."""
        system = self._systems.get(system_id)
        if not system:
            return None
        
        # Update fields if provided
        if data.name is not None:
            system.name = data.name
        if data.hostname is not None:
            system.hostname = data.hostname
            # Close existing connection if hostname changed
            await self._close_connection(system_id)
        if data.username is not None:
            system.username = data.username
            await self._close_connection(system_id)
        if data.port is not None:
            system.port = data.port
            await self._close_connection(system_id)
        if data.ssh_key is not None:
            system.ssh_key = data.ssh_key
            # Close existing connection if SSH key changed
            await self._close_connection(system_id)
        
        self._systems[system_id] = system
        await self._save_system(system)
        
        logger.info(f"Updated remote system: {system.name}")
        return self._sanitize_system(system)
    
    async def delete_system(self, system_id: str) -> bool:
        """Delete a remote system."""
        if system_id not in self._systems:
            return False
        
        # Close SSH connection if exists
        await self._close_connection(system_id)
        
        # Remove from memory and database
        del self._systems[system_id]
        await self._delete_system_from_db(system_id)
        
        logger.info(f"Deleted remote system: {system_id}")
        return True
    
    async def _close_connection(self, system_id: str):
        """Close SSH connection for a system."""
        if system_id in self._connections:
            try:
                self._connections[system_id].connection.close()
            except:
                pass
            del self._connections[system_id]
    
    async def get_connection(self, system_id: str) -> Optional[asyncssh.SSHClientConnection]:
        """Get or create an SSH connection for a system."""
        system = self._systems.get(system_id)
        if not system:
            return None
        
        # Check if we have an existing connection
        if system_id in self._connections:
            conn = self._connections[system_id]
            now = datetime.now()
            
            # Only verify connection liveness if it hasn't been checked recently
            time_since_check = (now - conn.last_checked).total_seconds()
            
            if time_since_check < CONNECTION_CHECK_INTERVAL:
                # Connection was recently verified, assume it's still good
                conn.last_used = now
                return conn.connection
            
            # Time to verify the connection is still alive
            try:
                result = await asyncio.wait_for(
                    conn.connection.run('echo ok', check=True),
                    timeout=5
                )
                if result.stdout.strip() == 'ok':
                    conn.last_used = now
                    conn.last_checked = now
                    logger.debug(f"SSH connection to {system.name} verified alive")
                    return conn.connection
            except Exception as e:
                # Connection is dead, close it
                logger.info(f"SSH connection to {system.name} is stale, reconnecting: {e}")
                await self._close_connection(system_id)
        
        # Create new connection
        try:
            logger.info(f"Connecting to {system.name} ({system.username}@{system.hostname}:{system.port})")
            
            # Prepare connection options with keepalive
            connect_options = {
                "host": system.hostname,
                "port": system.port,
                "username": system.username,
                "known_hosts": None,  # Accept any host key (consider security implications)
                "keepalive_interval": 30,  # Send keepalive every 30 seconds
                "keepalive_count_max": 3,  # Allow 3 missed keepalives before disconnect
            }
            
            # Use SSH key if provided, otherwise fall back to SSH agent/default keys
            if system.ssh_key:
                logger.info(f"Using provided SSH key for {system.name}")
                connect_options["client_keys"] = [asyncssh.import_private_key(system.ssh_key)]
            
            connection = await asyncio.wait_for(
                asyncssh.connect(**connect_options),
                timeout=30
            )
            
            now = datetime.now()
            self._connections[system_id] = SSHConnection(
                connection=connection,
                system_id=system_id,
                last_used=now,
                last_checked=now
            )
            
            # Update system status
            system.status = SystemStatus.CONNECTED
            system.last_connected = now
            system.last_error = None
            await self._save_system(system)
            
            logger.info(f"Successfully connected to {system.name}")
            return connection
            
        except asyncio.TimeoutError:
            system.status = SystemStatus.ERROR
            system.last_error = "Connection timeout"
            await self._save_system(system)
            logger.error(f"Connection timeout for {system.name}")
            return None
            
        except asyncssh.PermissionDenied as e:
            system.status = SystemStatus.ERROR
            system.last_error = f"Permission denied: {e}"
            await self._save_system(system)
            logger.error(f"Permission denied for {system.name}: {e}")
            return None
        
        except asyncssh.KeyImportError as e:
            system.status = SystemStatus.ERROR
            system.last_error = f"Invalid SSH key format: {e}"
            await self._save_system(system)
            logger.error(f"Invalid SSH key for {system.name}: {e}")
            return None
            
        except Exception as e:
            system.status = SystemStatus.ERROR
            system.last_error = str(e)
            await self._save_system(system)
            logger.error(f"Failed to connect to {system.name}: {e}")
            return None
    
    async def test_connection(self, system_id: str) -> Dict[str, Any]:
        """Test SSH connection to a system."""
        system = self._systems.get(system_id)
        if not system:
            return {"success": False, "error": "System not found"}
        
        try:
            connection = await self.get_connection(system_id)
            if not connection:
                return {
                    "success": False,
                    "error": system.last_error or "Failed to connect"
                }
            
            # Test docker command
            result = await asyncio.wait_for(
                connection.run('docker ps -q | wc -l', check=False),
                timeout=10
            )
            
            container_count = 0
            if result.exit_status == 0:
                try:
                    container_count = int(result.stdout.strip())
                except:
                    pass
            
            # Update container count
            system.container_count = container_count
            await self._save_system(system)
            
            return {
                "success": True,
                "docker_available": result.exit_status == 0,
                "container_count": container_count,
                "message": f"Connected successfully. Docker available with {container_count} running containers."
            }
            
        except asyncio.TimeoutError:
            return {"success": False, "error": "Command timeout"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def run_command(self, system_id: str, command: str, timeout: int = 30) -> Optional[str]:
        """Run a command on a remote system via SSH."""
        connection = await self.get_connection(system_id)
        if not connection:
            return None
        
        try:
            result = await asyncio.wait_for(
                connection.run(command, check=False),
                timeout=timeout
            )
            
            if result.exit_status == 0:
                return result.stdout
            else:
                logger.warning(f"Command failed on {system_id}: {result.stderr}")
                return None
                
        except asyncio.TimeoutError:
            logger.error(f"Command timeout on {system_id}: {command}")
            return None
        except Exception as e:
            logger.error(f"Command error on {system_id}: {e}")
            return None
    
    async def close_all_connections(self):
        """Close all SSH connections."""
        for system_id in list(self._connections.keys()):
            await self._close_connection(system_id)
        logger.info("Closed all SSH connections")


# Global instance
remote_systems_service = RemoteSystemsService()
