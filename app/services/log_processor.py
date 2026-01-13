"""
Log Processor Service - Ingests, parses, and stores logs for RAG.
Handles the pipeline from raw Docker logs to structured events and embeddings.
"""

import re
import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any, Set
from dataclasses import dataclass
import hashlib

from app.config import settings
from app.services.docker_service import docker_service
from app.services.storage_service import storage_service, LogEvent, LogLevel
from app.services.vector_service import vector_service, LogDocument

logger = logging.getLogger(__name__)


# Pattern detection rules
PATTERNS = {
    # Error patterns
    "connection_refused": re.compile(r"connection\s+refused|ECONNREFUSED", re.I),
    "timeout": re.compile(r"timeout|timed?\s*out|deadline\s+exceeded", re.I),
    "out_of_memory": re.compile(r"out\s+of\s+memory|OOM|memory\s+exhausted", re.I),
    "disk_full": re.compile(r"no\s+space\s+left|disk\s+full|ENOSPC", re.I),
    "permission_denied": re.compile(r"permission\s+denied|access\s+denied|EACCES", re.I),
    "file_not_found": re.compile(r"file\s+not\s+found|no\s+such\s+file|ENOENT", re.I),
    "authentication_failed": re.compile(r"auth(entication)?\s+(failed|error)|invalid\s+(credentials|token)", re.I),
    "database_error": re.compile(r"database\s+error|sql\s+error|query\s+failed|deadlock", re.I),
    "network_error": re.compile(r"network\s+(error|unreachable)|DNS\s+resolution|ENETUNREACH", re.I),
    "crash": re.compile(r"crash(ed)?|segfault|core\s+dump|fatal\s+error|panic", re.I),
    "ssl_error": re.compile(r"ssl\s+error|certificate\s+(error|expired|invalid)|TLS\s+handshake", re.I),
    "rate_limit": re.compile(r"rate\s+limit|too\s+many\s+requests|429", re.I),
    "dependency_error": re.compile(r"dependency\s+(error|failed)|import\s+error|module\s+not\s+found", re.I),
    
    # Warning patterns
    "deprecation": re.compile(r"deprecat(ed|ion)|will\s+be\s+removed", re.I),
    "high_load": re.compile(r"high\s+(load|cpu|memory)|resource\s+pressure", re.I),
    "retry": re.compile(r"retry(ing)?|retried|attempt\s+\d+", re.I),
    "slow_query": re.compile(r"slow\s+query|long\s+running|taking\s+too\s+long", re.I),
}

# Log level detection
LEVEL_PATTERNS = {
    LogLevel.CRITICAL: re.compile(r"\b(CRITICAL|FATAL|EMERGENCY|CRIT)\b", re.I),
    LogLevel.ERROR: re.compile(r"\b(ERROR|ERR|SEVERE|FAIL(ED)?)\b", re.I),
    LogLevel.WARNING: re.compile(r"\b(WARN(ING)?|ALERT)\b", re.I),
    LogLevel.INFO: re.compile(r"\b(INFO|NOTICE)\b", re.I),
    LogLevel.DEBUG: re.compile(r"\b(DEBUG|TRACE|VERBOSE)\b", re.I),
}


@dataclass
class ProcessedLog:
    """A processed log entry ready for storage."""
    container_id: str
    container_name: str
    message: str
    timestamp: datetime
    level: LogLevel
    pattern: Optional[str]
    category: Optional[str]
    hash: str  # For deduplication


class LogProcessor:
    """
    Processes Docker container logs for storage and RAG.
    - Parses log messages
    - Detects patterns and levels
    - Stores in MongoDB and vector index
    - Handles deduplication
    """
    
    def __init__(self):
        self._processed_hashes: Set[str] = set()
        self._max_cache_size = 10000
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    def _compute_hash(self, container_id: str, message: str, timestamp: datetime) -> str:
        """Compute a hash for deduplication."""
        # Use container + message + minute-level timestamp for dedup
        ts_key = timestamp.strftime("%Y%m%d%H%M") if timestamp else ""
        key = f"{container_id}:{message}:{ts_key}"
        return hashlib.md5(key.encode()).hexdigest()[:16]
    
    def _detect_level(self, message: str) -> LogLevel:
        """Detect log level from message content."""
        for level, pattern in LEVEL_PATTERNS.items():
            if pattern.search(message):
                return level
        return LogLevel.INFO
    
    def _detect_pattern(self, message: str) -> Optional[str]:
        """Detect known patterns in log message."""
        for pattern_name, pattern_re in PATTERNS.items():
            if pattern_re.search(message):
                return pattern_name
        return None
    
    def _get_category(self, pattern: Optional[str]) -> Optional[str]:
        """Get category based on detected pattern."""
        categories = {
            "connection_refused": "network",
            "timeout": "network",
            "network_error": "network",
            "ssl_error": "network",
            "out_of_memory": "resource",
            "disk_full": "resource",
            "high_load": "resource",
            "permission_denied": "security",
            "authentication_failed": "security",
            "file_not_found": "filesystem",
            "database_error": "database",
            "slow_query": "database",
            "crash": "system",
            "rate_limit": "api",
            "dependency_error": "application",
            "deprecation": "maintenance",
            "retry": "resilience",
        }
        return categories.get(pattern)
    
    def process_log(
        self,
        container_id: str,
        container_name: str,
        message: str,
        timestamp: Optional[datetime] = None,
    ) -> Optional[ProcessedLog]:
        """Process a single log entry."""
        if not message or not message.strip():
            return None
        
        timestamp = timestamp or datetime.now()
        message = message.strip()
        
        # Compute hash for deduplication
        log_hash = self._compute_hash(container_id, message, timestamp)
        
        # Check for duplicates
        if log_hash in self._processed_hashes:
            return None
        
        # Add to cache
        self._processed_hashes.add(log_hash)
        
        # Prune cache if needed
        if len(self._processed_hashes) > self._max_cache_size:
            # Remove oldest half
            to_remove = list(self._processed_hashes)[:self._max_cache_size // 2]
            for h in to_remove:
                self._processed_hashes.discard(h)
        
        # Detect level and pattern
        level = self._detect_level(message)
        pattern = self._detect_pattern(message)
        category = self._get_category(pattern)
        
        return ProcessedLog(
            container_id=container_id,
            container_name=container_name,
            message=message,
            timestamp=timestamp,
            level=level,
            pattern=pattern,
            category=category,
            hash=log_hash,
        )
    
    async def ingest_log(
        self,
        container_id: str,
        container_name: str,
        message: str,
        timestamp: Optional[datetime] = None,
        store_all: bool = False,
    ) -> bool:
        """
        Ingest a single log entry.
        
        Args:
            container_id: Docker container ID
            container_name: Docker container name
            message: Log message
            timestamp: Log timestamp (defaults to now)
            store_all: If False, only store warnings/errors in MongoDB
        """
        processed = self.process_log(container_id, container_name, message, timestamp)
        if not processed:
            return False
        
        try:
            # Store in MongoDB (only errors/warnings by default)
            if store_all or processed.level in [LogLevel.WARNING, LogLevel.ERROR, LogLevel.CRITICAL]:
                event = LogEvent(
                    container_id=processed.container_id,
                    container_name=processed.container_name,
                    message=processed.message,
                    timestamp=processed.timestamp,
                    level=processed.level,
                    pattern=processed.pattern,
                    category=processed.category,
                )
                await storage_service.store_event(event)
            
            # Store in vector index (for semantic search)
            # Only store if RAG is enabled and it's a significant log
            if settings.rag_enabled and (
                processed.pattern or 
                processed.level in [LogLevel.WARNING, LogLevel.ERROR, LogLevel.CRITICAL]
            ):
                doc = LogDocument(
                    id=processed.hash,
                    container_id=processed.container_id,
                    container_name=processed.container_name,
                    message=processed.message,
                    timestamp=processed.timestamp,
                    level=processed.level.value,
                )
                await vector_service.add_document(doc)
            
            return True
            
        except Exception as e:
            logger.error(f"Error ingesting log: {e}")
            return False
    
    async def ingest_batch(
        self,
        logs: List[Dict[str, Any]],
        store_all: bool = False,
    ) -> int:
        """
        Ingest a batch of logs.
        
        Args:
            logs: List of log dicts with container_id, container_name, message, timestamp
            store_all: If False, only store warnings/errors
        
        Returns:
            Number of logs successfully ingested
        """
        ingested = 0
        
        for log in logs:
            success = await self.ingest_log(
                container_id=log.get("container_id", ""),
                container_name=log.get("container_name", ""),
                message=log.get("message", ""),
                timestamp=log.get("timestamp"),
                store_all=store_all,
            )
            if success:
                ingested += 1
        
        return ingested
    
    async def start_background_ingestion(self, interval: float = 30.0):
        """Start background task to continuously ingest logs."""
        if self._running:
            return
        
        self._running = True
        
        async def ingest_loop():
            while self._running:
                try:
                    await self._ingest_all_container_logs()
                except Exception as e:
                    logger.error(f"Error in background ingestion: {e}")
                
                await asyncio.sleep(interval)
        
        self._task = asyncio.create_task(ingest_loop())
        logger.info(f"Started background log ingestion (interval: {interval}s)")
    
    async def stop_background_ingestion(self):
        """Stop background ingestion task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped background log ingestion")
    
    async def _ingest_all_container_logs(self):
        """Ingest recent logs from all running containers."""
        try:
            containers = docker_service.get_containers(all_containers=False)
            
            for container in containers:
                try:
                    logs = docker_service.get_logs(
                        container_id=container.id,
                        tail=100,  # Last 100 logs per container
                    )
                    
                    for log in logs:
                        await self.ingest_log(
                            container_id=log.container_id,
                            container_name=log.container_name,
                            message=log.message,
                            timestamp=log.timestamp,
                        )
                        
                except Exception as e:
                    logger.warning(f"Error ingesting logs from {container.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Error getting containers for ingestion: {e}")
    
    async def ingest_historical_logs(
        self,
        container_id: Optional[str] = None,
        tail: int = 1000,
    ) -> int:
        """
        Ingest historical logs from containers.
        
        Args:
            container_id: Specific container to ingest (None for all)
            tail: Number of log lines to fetch
        
        Returns:
            Number of logs ingested
        """
        total_ingested = 0
        
        try:
            if container_id:
                containers = [docker_service.get_container(container_id)]
            else:
                containers = docker_service.get_containers(all_containers=True)
            
            for container in containers:
                if not container:
                    continue
                    
                try:
                    logs = docker_service.get_logs(container_id=container.id, tail=tail)
                    
                    for log in logs:
                        success = await self.ingest_log(
                            container_id=log.container_id,
                            container_name=log.container_name,
                            message=log.message,
                            timestamp=log.timestamp,
                            store_all=True,  # Store all for historical
                        )
                        if success:
                            total_ingested += 1
                            
                    logger.info(f"Ingested logs from {container.name}")
                    
                except Exception as e:
                    logger.warning(f"Error ingesting historical logs from {container.name}: {e}")
            
        except Exception as e:
            logger.error(f"Error ingesting historical logs: {e}")
        
        return total_ingested
    
    def get_stats(self) -> Dict[str, Any]:
        """Get processor statistics."""
        return {
            "processed_hashes_count": len(self._processed_hashes),
            "running": self._running,
        }


# Global instance
log_processor = LogProcessor()
