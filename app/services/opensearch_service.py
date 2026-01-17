"""
OpenSearch Service for full-text log indexing and search.
Provides automatic indexing of all container logs and powerful search capabilities.
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """A log entry to be indexed in OpenSearch."""
    container_id: str
    container_name: str
    message: str
    timestamp: datetime
    level: str
    system_id: Optional[str] = None  # For remote systems
    system_name: Optional[str] = None
    pattern: Optional[str] = None
    category: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "container_id": self.container_id,
            "container_name": self.container_name,
            "message": self.message,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "level": self.level,
            "system_id": self.system_id,
            "system_name": self.system_name or "local",
            "pattern": self.pattern,
            "category": self.category,
        }


@dataclass
class SearchResult:
    """A search result from OpenSearch."""
    container_id: str
    container_name: str
    message: str
    timestamp: str
    level: str
    system_name: str
    score: float
    highlight: Optional[str] = None


class OpenSearchService:
    """
    OpenSearch service for full-text log indexing and search.
    Provides automatic indexing and powerful query capabilities.
    """
    
    def __init__(self):
        self.host = settings.opensearch_host
        self.index = settings.opensearch_index
        self._connected = False
        self._client: Optional[httpx.AsyncClient] = None
        self._bulk_buffer: List[Dict[str, Any]] = []
        self._bulk_size = 100  # Flush after this many documents
        self._lock = asyncio.Lock()
    
    async def connect(self) -> bool:
        """Connect to OpenSearch and create index if needed."""
        try:
            self._client = httpx.AsyncClient(timeout=30.0)
            
            # Check connection
            response = await self._client.get(f"{self.host}/_cluster/health")
            if response.status_code != 200:
                logger.warning(f"OpenSearch health check failed: {response.status_code}")
                return False
            
            # Create index if it doesn't exist
            await self._create_index()
            
            self._connected = True
            logger.info(f"Connected to OpenSearch at {self.host}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to OpenSearch: {e}")
            return False
    
    async def close(self):
        """Close the OpenSearch connection."""
        # Flush any remaining documents
        await self._flush_bulk()
        
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False
    
    async def _create_index(self):
        """Create the logs index with appropriate mappings."""
        index_settings = {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "index": {
                    "refresh_interval": "5s"
                }
            },
            "mappings": {
                "properties": {
                    "container_id": {"type": "keyword"},
                    "container_name": {"type": "keyword"},
                    "message": {
                        "type": "text",
                        "analyzer": "standard",
                        "fields": {
                            "keyword": {"type": "keyword", "ignore_above": 512}
                        }
                    },
                    "timestamp": {"type": "date"},
                    "level": {"type": "keyword"},
                    "system_id": {"type": "keyword"},
                    "system_name": {"type": "keyword"},
                    "pattern": {"type": "keyword"},
                    "category": {"type": "keyword"}
                }
            }
        }
        
        try:
            # Check if index exists
            response = await self._client.head(f"{self.host}/{self.index}")
            if response.status_code == 404:
                # Create index
                response = await self._client.put(
                    f"{self.host}/{self.index}",
                    json=index_settings
                )
                if response.status_code in [200, 201]:
                    logger.info(f"Created OpenSearch index: {self.index}")
                else:
                    logger.warning(f"Failed to create index: {response.text}")
            else:
                logger.info(f"OpenSearch index {self.index} already exists")
                
        except Exception as e:
            logger.error(f"Error creating index: {e}")
    
    def is_connected(self) -> bool:
        """Check if connected to OpenSearch."""
        return self._connected and self._client is not None
    
    async def index_log(self, entry: LogEntry) -> bool:
        """Index a single log entry (buffered for bulk indexing)."""
        if not self.is_connected():
            return False
        
        async with self._lock:
            self._bulk_buffer.append(entry.to_dict())
            
            if len(self._bulk_buffer) >= self._bulk_size:
                await self._flush_bulk()
        
        return True
    
    async def index_logs(self, entries: List[LogEntry]) -> int:
        """Index multiple log entries."""
        if not self.is_connected():
            return 0
        
        indexed = 0
        for entry in entries:
            if await self.index_log(entry):
                indexed += 1
        
        # Flush remaining
        await self._flush_bulk()
        return indexed
    
    async def _flush_bulk(self):
        """Flush the bulk buffer to OpenSearch."""
        if not self._bulk_buffer or not self._client:
            return
        
        try:
            # Build bulk request body
            bulk_body = ""
            for doc in self._bulk_buffer:
                bulk_body += '{"index": {}}\n'
                bulk_body += f"{__import__('json').dumps(doc)}\n"
            
            response = await self._client.post(
                f"{self.host}/{self.index}/_bulk",
                content=bulk_body,
                headers={"Content-Type": "application/x-ndjson"}
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("errors"):
                    logger.warning(f"Some bulk index operations failed")
            else:
                logger.warning(f"Bulk index failed: {response.status_code}")
            
            self._bulk_buffer.clear()
            
        except Exception as e:
            logger.error(f"Error flushing bulk buffer: {e}")
    
    async def search(
        self,
        query: str,
        container: Optional[str] = None,
        system: Optional[str] = None,
        level: Optional[str] = None,
        time_from: Optional[datetime] = None,
        time_to: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[SearchResult]:
        """
        Search logs with full-text query and filters.
        
        Args:
            query: Full-text search query
            container: Filter by container name
            system: Filter by system name (local or remote system name)
            level: Filter by log level (info, warning, error, critical)
            time_from: Start time filter
            time_to: End time filter
            limit: Maximum results to return
        
        Returns:
            List of matching log entries
        """
        if not self.is_connected():
            return []
        
        try:
            # Build query
            must = []
            filter_clauses = []
            
            # Full-text query on message
            if query:
                must.append({
                    "match": {
                        "message": {
                            "query": query,
                            "operator": "and"
                        }
                    }
                })
            
            # Filters
            if container:
                filter_clauses.append({"term": {"container_name": container}})
            
            if system:
                filter_clauses.append({"term": {"system_name": system}})
            
            if level:
                filter_clauses.append({"term": {"level": level.lower()}})
            
            # Time range
            if time_from or time_to:
                time_range = {}
                if time_from:
                    time_range["gte"] = time_from.isoformat()
                if time_to:
                    time_range["lte"] = time_to.isoformat()
                filter_clauses.append({"range": {"timestamp": time_range}})
            
            # Build final query
            search_body = {
                "size": limit,
                "sort": [{"timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "must": must if must else [{"match_all": {}}],
                        "filter": filter_clauses
                    }
                },
                "highlight": {
                    "fields": {
                        "message": {
                            "pre_tags": ["<mark>"],
                            "post_tags": ["</mark>"],
                            "fragment_size": 200
                        }
                    }
                }
            }
            
            response = await self._client.post(
                f"{self.host}/{self.index}/_search",
                json=search_body
            )
            
            if response.status_code != 200:
                logger.warning(f"Search failed: {response.status_code}")
                return []
            
            result = response.json()
            hits = result.get("hits", {}).get("hits", [])
            
            results = []
            for hit in hits:
                source = hit.get("_source", {})
                highlight = hit.get("highlight", {}).get("message", [])
                
                results.append(SearchResult(
                    container_id=source.get("container_id", ""),
                    container_name=source.get("container_name", ""),
                    message=source.get("message", ""),
                    timestamp=source.get("timestamp", ""),
                    level=source.get("level", "info"),
                    system_name=source.get("system_name", "local"),
                    score=hit.get("_score", 0),
                    highlight=highlight[0] if highlight else None
                ))
            
            return results
            
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []
    
    async def search_errors(
        self,
        container: Optional[str] = None,
        system: Optional[str] = None,
        hours: int = 24,
        limit: int = 100,
    ) -> List[SearchResult]:
        """Search for error-level logs in the last N hours."""
        time_from = datetime.now().replace(
            hour=datetime.now().hour - hours if datetime.now().hour >= hours else 0
        )
        
        return await self.search(
            query="",
            container=container,
            system=system,
            level="error",
            time_from=time_from,
            limit=limit
        )
    
    async def get_container_stats(self, container: str) -> Dict[str, Any]:
        """Get log statistics for a container."""
        if not self.is_connected():
            return {}
        
        try:
            aggs_body = {
                "size": 0,
                "query": {
                    "term": {"container_name": container}
                },
                "aggs": {
                    "level_counts": {
                        "terms": {"field": "level"}
                    },
                    "pattern_counts": {
                        "terms": {"field": "pattern", "size": 10}
                    },
                    "logs_over_time": {
                        "date_histogram": {
                            "field": "timestamp",
                            "calendar_interval": "hour"
                        }
                    }
                }
            }
            
            response = await self._client.post(
                f"{self.host}/{self.index}/_search",
                json=aggs_body
            )
            
            if response.status_code != 200:
                return {}
            
            result = response.json()
            aggs = result.get("aggregations", {})
            
            return {
                "total_logs": result.get("hits", {}).get("total", {}).get("value", 0),
                "level_counts": {
                    b["key"]: b["doc_count"]
                    for b in aggs.get("level_counts", {}).get("buckets", [])
                },
                "top_patterns": {
                    b["key"]: b["doc_count"]
                    for b in aggs.get("pattern_counts", {}).get("buckets", [])
                },
                "logs_over_time": [
                    {"time": b["key_as_string"], "count": b["doc_count"]}
                    for b in aggs.get("logs_over_time", {}).get("buckets", [])
                ]
            }
            
        except Exception as e:
            logger.error(f"Error getting container stats: {e}")
            return {}
    
    async def get_recent_logs(
        self,
        container: Optional[str] = None,
        system: Optional[str] = None,
        limit: int = 100
    ) -> List[SearchResult]:
        """Get most recent logs."""
        return await self.search(
            query="",
            container=container,
            system=system,
            limit=limit
        )
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get OpenSearch index statistics."""
        if not self.is_connected():
            return {"connected": False}
        
        try:
            response = await self._client.get(f"{self.host}/{self.index}/_stats")
            if response.status_code != 200:
                return {"connected": True, "error": "Could not get stats"}
            
            result = response.json()
            indices = result.get("indices", {})
            index_stats = indices.get(self.index, {})
            primaries = index_stats.get("primaries", {})
            
            return {
                "connected": True,
                "index": self.index,
                "doc_count": primaries.get("docs", {}).get("count", 0),
                "size_bytes": primaries.get("store", {}).get("size_in_bytes", 0),
                "indexing_total": primaries.get("indexing", {}).get("index_total", 0),
            }
            
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {"connected": True, "error": str(e)}


# Global instance
opensearch_service = OpenSearchService()
