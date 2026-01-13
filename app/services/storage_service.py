"""
Storage Service using MongoDB for event storage and analytics.
Handles structured log events, aggregations, and time-series queries.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict
from enum import Enum

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure

from app.config import settings

logger = logging.getLogger(__name__)


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class LogEvent:
    """A structured log event stored in MongoDB."""
    container_id: str
    container_name: str
    message: str
    timestamp: datetime
    level: LogLevel
    pattern: Optional[str] = None  # Detected pattern (e.g., "connection_refused")
    category: Optional[str] = None  # Category (e.g., "network", "database")
    source: Optional[str] = None  # Source file/component
    extra: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "container_id": self.container_id,
            "container_name": self.container_name,
            "message": self.message,
            "timestamp": self.timestamp,
            "level": self.level.value if isinstance(self.level, LogLevel) else self.level,
            "pattern": self.pattern,
            "category": self.category,
            "source": self.source,
            "extra": self.extra or {},
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LogEvent":
        return cls(
            container_id=data.get("container_id", ""),
            container_name=data.get("container_name", ""),
            message=data.get("message", ""),
            timestamp=data.get("timestamp", datetime.now()),
            level=LogLevel(data.get("level", "info")),
            pattern=data.get("pattern"),
            category=data.get("category"),
            source=data.get("source"),
            extra=data.get("extra"),
        )


@dataclass
class EventStats:
    """Statistics for log events."""
    total_count: int
    error_count: int
    warning_count: int
    containers: Dict[str, int]
    patterns: Dict[str, int]
    hourly_distribution: Dict[int, int]


class StorageService:
    """
    MongoDB storage service for log events and analytics.
    """
    
    def __init__(self):
        self._client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AsyncIOMotorDatabase] = None
        self._connected = False
    
    async def connect(self) -> bool:
        """Connect to MongoDB."""
        try:
            self._client = AsyncIOMotorClient(
                settings.mongodb_uri,
                serverSelectionTimeoutMS=5000,
            )
            
            # Test connection
            await self._client.admin.command("ping")
            
            self._db = self._client[settings.mongodb_database]
            self._connected = True
            
            # Create indexes
            await self._create_indexes()
            
            logger.info(f"Connected to MongoDB at {settings.mongodb_uri}")
            return True
            
        except ConnectionFailure as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to MongoDB: {e}")
            self._connected = False
            return False
    
    async def _create_indexes(self):
        """Create indexes for efficient queries."""
        if self._db is None:
            return
        
        events_collection = self._db.events
        
        # Index for time-based queries
        await events_collection.create_index([("timestamp", DESCENDING)])
        
        # Index for container queries
        await events_collection.create_index([("container_name", ASCENDING)])
        
        # Index for level queries
        await events_collection.create_index([("level", ASCENDING)])
        
        # Compound index for common queries
        await events_collection.create_index([
            ("container_name", ASCENDING),
            ("level", ASCENDING),
            ("timestamp", DESCENDING),
        ])
        
        # Index for pattern queries
        await events_collection.create_index([("pattern", ASCENDING)])
        
        # TTL index to auto-expire old events (30 days by default)
        await events_collection.create_index(
            [("timestamp", ASCENDING)],
            expireAfterSeconds=settings.log_retention_days * 24 * 3600,
            name="ttl_index"
        )
        
        logger.info("MongoDB indexes created")
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._db is not None
    
    async def store_event(self, event: LogEvent) -> bool:
        """Store a single log event."""
        if not self.is_connected:
            return False
        
        try:
            await self._db.events.insert_one(event.to_dict())
            return True
        except Exception as e:
            logger.error(f"Error storing event: {e}")
            return False
    
    async def store_events(self, events: List[LogEvent]) -> int:
        """Store multiple log events."""
        if not self.is_connected or not events:
            return 0
        
        try:
            result = await self._db.events.insert_many(
                [e.to_dict() for e in events]
            )
            return len(result.inserted_ids)
        except Exception as e:
            logger.error(f"Error storing events: {e}")
            return 0
    
    async def get_events(
        self,
        container_name: Optional[str] = None,
        level: Optional[LogLevel] = None,
        pattern: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        skip: int = 0,
    ) -> List[LogEvent]:
        """Query log events with filters."""
        if not self.is_connected:
            return []
        
        try:
            query = {}
            
            if container_name:
                query["container_name"] = container_name
            
            if level:
                query["level"] = level.value if isinstance(level, LogLevel) else level
            
            if pattern:
                query["pattern"] = pattern
            
            if start_time or end_time:
                query["timestamp"] = {}
                if start_time:
                    query["timestamp"]["$gte"] = start_time
                if end_time:
                    query["timestamp"]["$lte"] = end_time
            
            cursor = self._db.events.find(query).sort(
                "timestamp", DESCENDING
            ).skip(skip).limit(limit)
            
            events = []
            async for doc in cursor:
                events.append(LogEvent.from_dict(doc))
            
            return events
            
        except Exception as e:
            logger.error(f"Error querying events: {e}")
            return []
    
    async def count_events(
        self,
        container_name: Optional[str] = None,
        level: Optional[LogLevel] = None,
        pattern: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int:
        """Count events matching filters."""
        if not self.is_connected:
            return 0
        
        try:
            query = {}
            
            if container_name:
                query["container_name"] = container_name
            
            if level:
                query["level"] = level.value if isinstance(level, LogLevel) else level
            
            if pattern:
                query["pattern"] = pattern
            
            if start_time or end_time:
                query["timestamp"] = {}
                if start_time:
                    query["timestamp"]["$gte"] = start_time
                if end_time:
                    query["timestamp"]["$lte"] = end_time
            
            return await self._db.events.count_documents(query)
            
        except Exception as e:
            logger.error(f"Error counting events: {e}")
            return 0
    
    async def get_error_count_by_container(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> Dict[str, int]:
        """Get error counts grouped by container."""
        if not self.is_connected:
            return {}
        
        try:
            match_stage = {"level": {"$in": ["error", "critical"]}}
            
            if start_time or end_time:
                match_stage["timestamp"] = {}
                if start_time:
                    match_stage["timestamp"]["$gte"] = start_time
                if end_time:
                    match_stage["timestamp"]["$lte"] = end_time
            
            pipeline = [
                {"$match": match_stage},
                {"$group": {"_id": "$container_name", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ]
            
            result = {}
            async for doc in self._db.events.aggregate(pipeline):
                result[doc["_id"]] = doc["count"]
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting error counts: {e}")
            return {}
    
    async def get_pattern_frequency(
        self,
        container_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 20,
    ) -> Dict[str, int]:
        """Get frequency of detected patterns."""
        if not self.is_connected:
            return {}
        
        try:
            match_stage = {"pattern": {"$ne": None}}
            
            if container_name:
                match_stage["container_name"] = container_name
            
            if start_time or end_time:
                match_stage["timestamp"] = {}
                if start_time:
                    match_stage["timestamp"]["$gte"] = start_time
                if end_time:
                    match_stage["timestamp"]["$lte"] = end_time
            
            pipeline = [
                {"$match": match_stage},
                {"$group": {"_id": "$pattern", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": limit},
            ]
            
            result = {}
            async for doc in self._db.events.aggregate(pipeline):
                if doc["_id"]:
                    result[doc["_id"]] = doc["count"]
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting pattern frequency: {e}")
            return {}
    
    async def get_hourly_distribution(
        self,
        container_name: Optional[str] = None,
        level: Optional[LogLevel] = None,
        days: int = 7,
    ) -> Dict[int, int]:
        """Get event distribution by hour of day."""
        if not self.is_connected:
            return {}
        
        try:
            start_time = datetime.now() - timedelta(days=days)
            
            match_stage = {"timestamp": {"$gte": start_time}}
            
            if container_name:
                match_stage["container_name"] = container_name
            
            if level:
                match_stage["level"] = level.value if isinstance(level, LogLevel) else level
            
            pipeline = [
                {"$match": match_stage},
                {"$group": {
                    "_id": {"$hour": "$timestamp"},
                    "count": {"$sum": 1}
                }},
                {"$sort": {"_id": 1}},
            ]
            
            result = {i: 0 for i in range(24)}
            async for doc in self._db.events.aggregate(pipeline):
                result[doc["_id"]] = doc["count"]
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting hourly distribution: {e}")
            return {}
    
    async def get_daily_counts(
        self,
        container_name: Optional[str] = None,
        level: Optional[LogLevel] = None,
        days: int = 30,
    ) -> Dict[str, int]:
        """Get event counts by day."""
        if not self.is_connected:
            return {}
        
        try:
            start_time = datetime.now() - timedelta(days=days)
            
            match_stage = {"timestamp": {"$gte": start_time}}
            
            if container_name:
                match_stage["container_name"] = container_name
            
            if level:
                match_stage["level"] = level.value if isinstance(level, LogLevel) else level
            
            pipeline = [
                {"$match": match_stage},
                {"$group": {
                    "_id": {
                        "$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}
                    },
                    "count": {"$sum": 1}
                }},
                {"$sort": {"_id": 1}},
            ]
            
            result = {}
            async for doc in self._db.events.aggregate(pipeline):
                result[doc["_id"]] = doc["count"]
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting daily counts: {e}")
            return {}
    
    async def search_messages(
        self,
        search_text: str,
        container_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[LogEvent]:
        """Full-text search on log messages."""
        if not self.is_connected:
            return []
        
        try:
            query = {"message": {"$regex": search_text, "$options": "i"}}
            
            if container_name:
                query["container_name"] = container_name
            
            cursor = self._db.events.find(query).sort(
                "timestamp", DESCENDING
            ).limit(limit)
            
            events = []
            async for doc in cursor:
                events.append(LogEvent.from_dict(doc))
            
            return events
            
        except Exception as e:
            logger.error(f"Error searching messages: {e}")
            return []
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get overall statistics."""
        if not self.is_connected:
            return {"connected": False}
        
        try:
            total = await self._db.events.count_documents({})
            
            # Get level counts
            level_pipeline = [
                {"$group": {"_id": "$level", "count": {"$sum": 1}}},
            ]
            level_counts = {}
            async for doc in self._db.events.aggregate(level_pipeline):
                level_counts[doc["_id"]] = doc["count"]
            
            # Get container counts
            container_pipeline = [
                {"$group": {"_id": "$container_name", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": 10},
            ]
            container_counts = {}
            async for doc in self._db.events.aggregate(container_pipeline):
                container_counts[doc["_id"]] = doc["count"]
            
            return {
                "connected": True,
                "total_events": total,
                "level_counts": level_counts,
                "top_containers": container_counts,
            }
            
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {"connected": True, "error": str(e)}
    
    async def close(self):
        """Close MongoDB connection."""
        if self._client:
            self._client.close()
            self._connected = False
            logger.info("MongoDB connection closed")


# Global instance
storage_service = StorageService()
