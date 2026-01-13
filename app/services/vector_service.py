"""
Vector Search Service using usearch for efficient similarity search.
Handles embeddings generation via Ollama and vector storage/retrieval.
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict
import numpy as np
import httpx
from usearch.index import Index, MetricKind

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LogDocument:
    """A log entry stored in the vector database."""
    id: str
    container_id: str
    container_name: str
    message: str
    timestamp: datetime
    level: str  # info, warning, error, critical
    embedding: Optional[List[float]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "message": self.message,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "level": self.level,
        }


@dataclass
class SearchResult:
    """A search result from vector similarity search."""
    document: LogDocument
    score: float  # Similarity score (higher is better)


class VectorService:
    """
    Vector search service using usearch for efficient similarity search.
    Uses Ollama for generating embeddings.
    """
    
    def __init__(self):
        self.data_dir = Path(settings.data_dir) / "vectors"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.index_path = self.data_dir / "logs.usearch"
        self.metadata_path = self.data_dir / "logs_metadata.json"
        
        # Embedding dimensions (nomic-embed-text uses 768)
        self.embedding_dim = 768
        self.embedding_model = settings.embedding_model
        
        # Initialize usearch index
        self._index: Optional[Index] = None
        self._metadata: Dict[int, Dict[str, Any]] = {}
        self._next_id: int = 0
        self._lock = asyncio.Lock()
        
        # Load existing index if available
        self._load_index()
    
    def _load_index(self):
        """Load existing index and metadata from disk."""
        try:
            if self.index_path.exists():
                self._index = Index.restore(str(self.index_path))
                logger.info(f"Loaded vector index with {len(self._index)} vectors")
            else:
                self._index = Index(
                    ndim=self.embedding_dim,
                    metric=MetricKind.Cos,  # Cosine similarity
                    dtype="f32",
                )
                logger.info("Created new vector index")
            
            if self.metadata_path.exists():
                with open(self.metadata_path, "r") as f:
                    data = json.load(f)
                    self._metadata = {int(k): v for k, v in data.get("metadata", {}).items()}
                    self._next_id = data.get("next_id", 0)
                logger.info(f"Loaded {len(self._metadata)} metadata entries")
            
        except Exception as e:
            logger.error(f"Error loading vector index: {e}")
            self._index = Index(
                ndim=self.embedding_dim,
                metric=MetricKind.Cos,
                dtype="f32",
            )
            self._metadata = {}
            self._next_id = 0
    
    def _save_index(self):
        """Save index and metadata to disk."""
        try:
            if self._index and len(self._index) > 0:
                self._index.save(str(self.index_path))
            
            with open(self.metadata_path, "w") as f:
                json.dump({
                    "metadata": {str(k): v for k, v in self._metadata.items()},
                    "next_id": self._next_id,
                }, f)
                
        except Exception as e:
            logger.error(f"Error saving vector index: {e}")
    
    async def generate_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding for text using Ollama."""
        try:
            # Truncate long texts to avoid context length errors
            # nomic-embed-text has ~8k token context, ~4 chars per token
            max_chars = 8000
            if len(text) > max_chars:
                text = text[:max_chars]
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{settings.ollama_host}/api/embeddings",
                    json={
                        "model": self.embedding_model,
                        "prompt": text,
                    },
                    timeout=30.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    embedding = data.get("embedding")
                    if embedding:
                        return embedding
                else:
                    logger.warning(f"Embedding API returned status {response.status_code}")
                    
        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
        
        return None
    
    async def add_document(self, doc: LogDocument) -> bool:
        """Add a document to the vector index."""
        async with self._lock:
            try:
                # Generate embedding if not provided
                if doc.embedding is None:
                    doc.embedding = await self.generate_embedding(doc.message)
                
                if doc.embedding is None:
                    logger.warning(f"Could not generate embedding for document {doc.id}")
                    return False
                
                # Ensure correct dimension
                if len(doc.embedding) != self.embedding_dim:
                    logger.warning(f"Embedding dimension mismatch: {len(doc.embedding)} vs {self.embedding_dim}")
                    return False
                
                # Add to index
                vector_id = self._next_id
                self._next_id += 1
                
                embedding_array = np.array(doc.embedding, dtype=np.float32)
                self._index.add(vector_id, embedding_array)
                
                # Store metadata
                self._metadata[vector_id] = doc.to_dict()
                
                # Periodically save (every 100 documents)
                if self._next_id % 100 == 0:
                    self._save_index()
                
                return True
                
            except Exception as e:
                logger.error(f"Error adding document to vector index: {e}")
                return False
    
    async def add_documents(self, docs: List[LogDocument]) -> int:
        """Add multiple documents to the vector index."""
        added = 0
        for doc in docs:
            if await self.add_document(doc):
                added += 1
        
        # Save after batch
        self._save_index()
        return added
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        container_filter: Optional[str] = None,
        level_filter: Optional[str] = None,
        time_filter_hours: Optional[int] = None,
    ) -> List[SearchResult]:
        """
        Search for similar log entries.
        
        Args:
            query: Search query text
            limit: Maximum number of results
            container_filter: Filter by container name (optional)
            level_filter: Filter by log level (optional)
            time_filter_hours: Only include logs from last N hours (optional)
        """
        try:
            # Generate query embedding
            query_embedding = await self.generate_embedding(query)
            if query_embedding is None:
                logger.warning("Could not generate query embedding")
                return []
            
            query_array = np.array(query_embedding, dtype=np.float32)
            
            # Search with more results to allow for filtering
            search_limit = limit * 5 if (container_filter or level_filter or time_filter_hours) else limit
            
            if len(self._index) == 0:
                return []
            
            # Perform search
            matches = self._index.search(query_array, min(search_limit, len(self._index)))
            
            results = []
            now = datetime.now()
            
            for match in matches:
                vector_id = int(match.key)
                score = float(1 - match.distance)  # Convert distance to similarity
                
                if vector_id not in self._metadata:
                    continue
                
                meta = self._metadata[vector_id]
                
                # Apply filters
                if container_filter and meta.get("container_name") != container_filter:
                    continue
                
                if level_filter and meta.get("level") != level_filter:
                    continue
                
                if time_filter_hours:
                    timestamp = datetime.fromisoformat(meta.get("timestamp", ""))
                    if (now - timestamp).total_seconds() > time_filter_hours * 3600:
                        continue
                
                # Create result
                doc = LogDocument(
                    id=meta.get("id", ""),
                    container_id=meta.get("container_id", ""),
                    container_name=meta.get("container_name", ""),
                    message=meta.get("message", ""),
                    timestamp=datetime.fromisoformat(meta["timestamp"]) if meta.get("timestamp") else None,
                    level=meta.get("level", "info"),
                )
                
                results.append(SearchResult(document=doc, score=score))
                
                if len(results) >= limit:
                    break
            
            return results
            
        except Exception as e:
            logger.error(f"Error searching vector index: {e}")
            return []
    
    async def get_similar_to_log(
        self,
        log_message: str,
        limit: int = 10,
        time_filter_hours: Optional[int] = None,
    ) -> List[SearchResult]:
        """Find logs similar to a given log message."""
        return await self.search(
            query=log_message,
            limit=limit,
            time_filter_hours=time_filter_hours,
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the vector index."""
        return {
            "total_vectors": len(self._index) if self._index else 0,
            "embedding_dim": self.embedding_dim,
            "embedding_model": self.embedding_model,
            "index_path": str(self.index_path),
        }
    
    async def clear(self):
        """Clear all vectors from the index."""
        async with self._lock:
            self._index = Index(
                ndim=self.embedding_dim,
                metric=MetricKind.Cos,
                dtype="f32",
            )
            self._metadata = {}
            self._next_id = 0
            self._save_index()
            logger.info("Vector index cleared")


# Global instance
vector_service = VectorService()
