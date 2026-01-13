# LogsCrawler Advanced Architecture

## Overview

This document describes the enhanced architecture for long-term log analysis with AI using **RAG (Retrieval Augmented Generation)**.

## Quick Start

```bash
# Pull required models
docker exec logscrawler-ollama ollama pull llama3.2
docker exec logscrawler-ollama ollama pull nomic-embed-text

# Ingest historical logs
curl -X POST "http://localhost:8000/api/rag/ingest?tail=5000"

# Ask questions with RAG context
curl "http://localhost:8000/api/rag/ask?question=How%20many%20errors%20occurred%20last%20week"

# Search for similar logs
curl "http://localhost:8000/api/rag/search?query=connection%20refused"

# Get statistics
curl "http://localhost:8000/api/rag/stats?days=7"
```

## Components

### 1. Log Ingestion Pipeline

```
Docker Containers
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│                    Log Processor                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │
│  │ Parse &     │  │ Event       │  │ Generate    │       │
│  │ Normalize   │──▶│ Extraction  │──▶│ Embeddings  │       │
│  └─────────────┘  └─────────────┘  └─────────────┘       │
└──────────────────────────────────────────────────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  Raw Logs   │    │   Events    │    │  Embeddings │
│  (Recent)   │    │  Database   │    │  (USearch) │
│  In-Memory  │    │ (Mongo/PG)  │    │             │
└─────────────┘    └─────────────┘    └─────────────┘
```

### 2. Storage Layer

| Store | Purpose | Retention |
|-------|---------|-----------|
| In-Memory Buffer | Real-time streaming | Last 1000 logs |
| Mongo/PostgreSQL | Structured events, counts | Unlimited |
| USearch | Semantic search | Last 30 days |
| Compressed Archives | Long-term backup | 1+ year |

### 3. Event Extraction

The log processor extracts structured events:

```json
{
  "timestamp": "2024-01-12T17:22:33Z",
  "container": "traefik-local",
  "level": "error",
  "category": "network",
  "pattern": "connection_refused",
  "message": "Provider error, retrying...",
  "count": 1
}
```

### 4. AI Query Flow

```
User: "Did this error happen many times last month?"
                    │
                    ▼
            ┌───────────────┐
            │ Query Planner │
            └───────┬───────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ Mongo   │ │ Vector  │ │ Recent  │
   │ Query   │ │ Search  │ │ Logs    │
   │(counts) │ │(similar)│ │(context)│
   └────┬────┘ └────┬────┘ └────┬────┘
        │           │           │
        └───────────┼───────────┘
                    ▼
            ┌───────────────┐
            │  LLM Analysis │
            │  + RAG Context│
            └───────────────┘
                    │
                    ▼
            "This error occurred 47 times
             in the last month, mostly
             between 2-4 AM..."
```

## Implementation Plan

### Phase 1: Event Database (Mongo)
- Store parsed log events with timestamps
- Track error/warning counts per container
- Enable SQL queries for aggregations

### Phase 2: Vector Store (USearch)
- Generate embeddings for log messages
- Enable semantic search ("find similar errors")
- Retrieve relevant context for AI

### Phase 3: AI Agent Enhancement
- Query planner to decide which stores to query
- Combine SQL results + RAG context
- Generate comprehensive answers

## Technology Stack

| Component | Technology | Why |
|-----------|------------|-----|
| Vector DB | USearch | Lightweight, embedded, Python-native |
| SQL DB | Mongo | Zero-config, embedded, fast |
| Embeddings | Ollama (nomic-embed-text) | Local, free, fast |
| LLM | Ollama (llama3.2) | Local, GPU-accelerated |

## Model Recommendations

### For Embeddings (semantic search):
- `nomic-embed-text` - Fast, good quality, 768 dimensions
- `mxbai-embed-large` - Better quality, 1024 dimensions

### For Analysis (LLM):
- `llama3.2` (3B) - Fast, good for simple queries
- `llama3.2:70b` - Better reasoning, needs more VRAM
- `qwen2.5:14b` - Good balance of speed and quality
- `deepseek-coder` - Better for technical log analysis

### Context Window Comparison:
| Model | Context | Use Case |
|-------|---------|----------|
| llama3.2 (3B) | 128K | Quick queries |
| qwen2.5 (14B) | 128K | Complex analysis |
| gemma2 (27B) | 8K | Limited context |

## Docker Compose Addition

```yaml
services:
  # ... existing services ...
  
  USearch:
    image: USearch/chroma:latest
    container_name: logscrawler-USearch
    ports:
      - "8001:8000"
    volumes:
      - USearch-data:/chroma/chroma
    networks:
      - logscrawler-net
    restart: unless-stopped

volumes:
  USearch-data:
    driver: local
```

## Example Queries This Enables

1. **Frequency Analysis**
   - "How many times did this error occur last week?"
   - "What's the error rate trend for traefik?"

2. **Pattern Detection**
   - "Find all logs similar to this error"
   - "Are there any recurring issues at specific times?"

3. **Root Cause Analysis**
   - "What happened before this crash?"
   - "Are these errors correlated with other containers?"

4. **Anomaly Detection**
   - "Are there any unusual patterns today?"
   - "Did anything change compared to last week?"
