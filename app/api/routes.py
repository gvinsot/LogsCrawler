"""API routes for the LogsCrawler application."""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional

from app.models import (
    ContainerInfo,
    ContainerLog,
    DetectedIssue,
    IssueSeverity,
    AIAnalysisRequest,
    AIAnalysisResponse,
)
from app.services.docker_service import docker_service
from app.services.ai_service import ai_service
from app.config import settings

router = APIRouter(prefix="/api", tags=["API"])


# ==================== Health & Status ====================

@router.get("/health")
async def health_check():
    """Check service health."""
    docker_ok = docker_service.is_connected()
    ai_ok = await ai_service.check_connection()
    
    # Try to get more details about Docker connection
    docker_error = None
    if not docker_ok:
        try:
            docker_service.client.ping()
        except Exception as e:
            docker_error = str(e)
    
    return {
        "status": "healthy" if docker_ok else "degraded",
        "docker_connected": docker_ok,
        "docker_error": docker_error,
        "ai_connected": ai_ok,
        "ai_model": settings.ollama_model,
    }


@router.get("/status")
async def get_status():
    """Get detailed system status."""
    docker_ok = docker_service.is_connected()
    ai_ok = await ai_service.check_connection()
    models = await ai_service.get_available_models() if ai_ok else []
    
    return {
        "app_name": settings.app_name,
        "version": settings.app_version,
        "docker": {
            "connected": docker_ok,
        },
        "ai": {
            "connected": ai_ok,
            "host": settings.ollama_host,
            "model": settings.ollama_model,
            "available_models": models,
        },
    }


# ==================== Containers ====================

@router.get("/containers", response_model=List[ContainerInfo])
async def list_containers(all: bool = Query(True, description="Include stopped containers")):
    """Get list of all Docker containers."""
    try:
        return docker_service.get_containers(all_containers=all)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/containers/{container_id}", response_model=ContainerInfo)
async def get_container(container_id: str):
    """Get a specific container by ID or name."""
    container = docker_service.get_container(container_id)
    if not container:
        raise HTTPException(status_code=404, detail="Container not found")
    return container


# ==================== Logs ====================

@router.get("/logs/{container_id}", response_model=List[ContainerLog])
async def get_container_logs(
    container_id: str,
    tail: int = Query(100, ge=1, le=5000, description="Number of lines to fetch"),
    timestamps: bool = Query(True, description="Include timestamps"),
):
    """Get logs from a specific container."""
    try:
        logs = docker_service.get_logs(
            container_id=container_id,
            tail=tail,
            timestamps=timestamps,
        )
        return logs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs", response_model=List[ContainerLog])
async def get_all_logs(
    tail: int = Query(50, ge=1, le=1000, description="Lines per container"),
):
    """Get recent logs from all running containers."""
    try:
        return docker_service.get_all_logs(tail=tail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/{container_id}/search")
async def search_logs_context(
    container_id: str,
    search: str = Query(..., description="Text to search for in logs"),
    context_before: int = Query(50, ge=1, le=200, description="Lines before match"),
    context_after: int = Query(50, ge=1, le=200, description="Lines after match"),
    max_logs: int = Query(10000, ge=100, le=50000, description="Maximum logs to search through"),
):
    """
    Search for a specific text in container logs and return context around it.
    Loads up to max_logs lines and finds the matching line with context.
    """
    try:
        # Load a large amount of logs to search through
        logs = docker_service.get_logs(
            container_id=container_id,
            tail=max_logs,
            timestamps=True,
        )
        
        if not logs:
            return {
                "found": False,
                "search": search,
                "logs": [],
                "match_index": -1,
                "total_searched": 0
            }
        
        # Search for the matching log line
        # Try to match the beginning of the search string (first 50 chars)
        search_text = search[:50] if len(search) > 50 else search
        match_index = -1
        
        for i, log in enumerate(logs):
            if search_text in log.message:
                match_index = i
                break
        
        if match_index == -1:
            # Try a more flexible search with just the first 30 chars
            search_text = search[:30] if len(search) > 30 else search
            for i, log in enumerate(logs):
                if search_text in log.message:
                    match_index = i
                    break
        
        if match_index == -1:
            # Still not found, return last logs as fallback
            return {
                "found": False,
                "search": search,
                "logs": [log.model_dump() for log in logs[-100:]],
                "match_index": -1,
                "total_searched": len(logs)
            }
        
        # Extract context around the match
        start_index = max(0, match_index - context_before)
        end_index = min(len(logs), match_index + context_after + 1)
        context_logs = logs[start_index:end_index]
        
        # Calculate the relative match index in the returned logs
        relative_match_index = match_index - start_index
        
        return {
            "found": True,
            "search": search,
            "logs": [log.model_dump() for log in context_logs],
            "match_index": relative_match_index,
            "total_searched": len(logs),
            "absolute_match_index": match_index
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/{container_id}/by-time")
async def get_logs_by_time(
    container_id: str,
    timestamp: str = Query(..., description="Target timestamp (ISO format) to search around"),
    search: Optional[str] = Query(None, description="Optional text to search for in logs"),
    context_before: int = Query(50, ge=1, le=200, description="Lines before target"),
    context_after: int = Query(50, ge=1, le=200, description="Lines after target"),
    max_logs: int = Query(20000, ge=100, le=50000, description="Maximum logs to search through"),
):
    """
    Get logs around a specific timestamp.
    Searches through logs to find entries closest to the target time.
    """
    from datetime import datetime, timedelta
    
    try:
        # Parse target timestamp
        try:
            target_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except:
            # Try parsing without timezone
            target_time = datetime.fromisoformat(timestamp.replace('Z', ''))
        
        # Load logs
        logs = docker_service.get_logs(
            container_id=container_id,
            tail=max_logs,
            timestamps=True,
        )
        
        if not logs:
            return {
                "found": False,
                "logs": [],
                "match_index": -1,
                "target_time": timestamp,
                "total_searched": 0
            }
        
        # PRIORITY 1: Text-based search (most reliable for finding the actual issue)
        match_index = -1
        
        if search:
            import re
            # Clean up search text - extract meaningful content
            search_text = search.strip()
            
            # Try multiple search strategies with decreasing specificity
            search_attempts = []
            
            # HIGHEST PRIORITY: Extract UUIDs - these are unique identifiers
            uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
            uuids = re.findall(uuid_pattern, search_text, re.IGNORECASE)
            search_attempts.extend(uuids)
            
            # Extract connection IDs (e.g., conn8520, connectionId:8520)
            conn_ids = re.findall(r'conn(\d+)', search_text, re.IGNORECASE)
            for conn_id in conn_ids:
                search_attempts.append(f'conn{conn_id}')
                search_attempts.append(f'"connectionId":{conn_id}')
            
            # Extract IP:port combinations
            ip_ports = re.findall(r'(\d+\.\d+\.\d+\.\d+:\d+)', search_text)
            search_attempts.extend(ip_ports)
            
            # Extract client IPs with quotes
            client_ips = re.findall(r'"client":"([^"]+)"', search_text)
            search_attempts.extend(client_ips)
            
            # Extract key error/warning messages from the excerpt
            key_patterns = [
                r'"msg":\s*"([^"]+)"',  # MongoDB JSON logs
                r'ERROR[:\s]+([^\n]+)',  # Error messages
                r'WARNING[:\s]+([^\n]+)',  # Warning messages
                r'\[error\][:\s]*([^\n]+)',  # [error] prefix
                r'\[warning\][:\s]*([^\n]+)',  # [warning] prefix
                r'INFO\s*[-:]\s*(.{15,})',  # INFO messages
                r'DEBUG\s*[-:]\s*(.{15,})',  # DEBUG messages
                r'CRITICAL\s*[-:]\s*(.{15,})',  # CRITICAL messages
            ]
            for pattern in key_patterns:
                match = re.search(pattern, search_text, re.IGNORECASE)
                if match:
                    extracted = match.group(1).strip()
                    if extracted and len(extracted) > 3:
                        search_attempts.append(extracted[:80])
            
            # Split by "..." and try each part (for truncated excerpts)
            parts = search_text.split('...')
            for part in parts:
                part = part.strip()
                # Skip parts that are mostly punctuation or very short
                if len(part) > 15 and not part.startswith('['):
                    search_attempts.append(part[:80])
            
            # Fallback: Try full excerpt without "..." placeholder (up to 100 chars)
            clean_text = search_text.replace('...', '').strip()
            if len(clean_text) > 20:
                search_attempts.append(clean_text[:100])
            
            # Search through logs for any of our search attempts
            for attempt in search_attempts:
                if not attempt or len(attempt) < 3:
                    continue
                for i, log in enumerate(logs):
                    if attempt in log.message:
                        match_index = i
                        break
                if match_index >= 0:
                    break
        
        # PRIORITY 2: Timestamp-based search (fallback)
        if match_index == -1:
            min_diff = timedelta(days=365)
            for i, log in enumerate(logs):
                if log.timestamp:
                    log_time = log.timestamp
                    # Make both timezone-naive for comparison
                    if log_time.tzinfo:
                        log_time = log_time.replace(tzinfo=None)
                    if target_time.tzinfo:
                        target_time_naive = target_time.replace(tzinfo=None)
                    else:
                        target_time_naive = target_time
                        
                    diff = abs(log_time - target_time_naive)
                    if diff < min_diff:
                        min_diff = diff
                        match_index = i
        
        if match_index == -1:
            # Fallback to last 100 logs
            return {
                "found": False,
                "logs": [log.model_dump() for log in logs[-100:]],
                "match_index": -1,
                "target_time": timestamp,
                "total_searched": len(logs)
            }
        
        # Extract context around the match
        start_index = max(0, match_index - context_before)
        end_index = min(len(logs), match_index + context_after + 1)
        context_logs = logs[start_index:end_index]
        
        # Calculate the relative match index in the returned logs
        relative_match_index = match_index - start_index
        
        return {
            "found": True,
            "logs": [log.model_dump() for log in context_logs],
            "match_index": relative_match_index,
            "target_time": timestamp,
            "total_searched": len(logs),
            "absolute_match_index": match_index
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== AI Analysis ====================

@router.post("/ai/analyze", response_model=AIAnalysisResponse)
async def analyze_logs(request: AIAnalysisRequest):
    """Analyze logs using AI."""
    try:
        # Fetch logs
        if request.container_id:
            logs = docker_service.get_logs(request.container_id, tail=request.log_lines)
        elif request.include_all_containers:
            logs = docker_service.get_all_logs(tail=request.log_lines)
        else:
            logs = docker_service.get_all_logs(tail=request.log_lines)
        
        if not logs:
            raise HTTPException(status_code=404, detail="No logs found")
        
        # Analyze with AI
        result = await ai_service.analyze_logs(
            logs=logs,
            query=request.query if request.query else None,
            detect_issues=True
        )
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ai/chat")
async def chat_with_ai(
    message: str = Query(..., description="Message to send to AI"),
    container_id: Optional[str] = Query(None, description="Container to include logs from"),
    include_logs: bool = Query(True, description="Include recent logs in context"),
    log_lines: int = Query(50, ge=1, le=500, description="Number of log lines to include"),
):
    """Chat with AI about container logs."""
    try:
        logs = None
        if include_logs:
            if container_id:
                logs = docker_service.get_logs(container_id, tail=log_lines)
            else:
                logs = docker_service.get_all_logs(tail=log_lines)
        
        response = await ai_service.chat(message=message, logs=logs, stream=False)
        
        return {
            "message": message,
            "response": response,
            "logs_included": len(logs) if logs else 0,
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ai/models")
async def get_ai_models():
    """Get available AI models."""
    models = await ai_service.get_available_models()
    return {
        "current_model": settings.ollama_model,
        "available_models": models,
    }


# ==================== Issues ====================

@router.get("/issues", response_model=List[DetectedIssue])
async def get_issues(
    limit: int = Query(50, ge=1, le=200),
    container_id: Optional[str] = None,
    severity: Optional[IssueSeverity] = None,
    include_resolved: bool = False,
    min_occurrences: int = Query(1, ge=1, le=100, description="Minimum occurrences to show (2 = recurring only)"),
):
    """Get detected issues with occurrence count."""
    return ai_service.get_detected_issues(
        limit=limit,
        container_id=container_id,
        severity=severity,
        include_resolved=include_resolved,
        min_occurrences=min_occurrences,
    )


@router.get("/issues/status")
async def get_analysis_status():
    """Get the current incremental analysis status."""
    return ai_service.get_analysis_status()


@router.post("/issues/reset")
async def reset_analysis():
    """Reset the incremental analysis state. Next scan will re-analyze all logs."""
    ai_service._initial_scan_done = False
    ai_service._analyzed_log_hashes.clear()
    ai_service._last_analyzed_timestamp = None
    ai_service._total_logs_analyzed = 0
    return {
        "status": "reset",
        "message": "Analysis state reset. Next scan will analyze all logs."
    }


@router.post("/issues/scan")
async def scan_for_issues(
    container_id: Optional[str] = None,
    log_lines: int = Query(100, ge=10, le=1000),
):
    """
    Scan logs for issues using incremental AI detection.
    Only new logs (not previously analyzed) will be processed.
    """
    try:
        if container_id:
            logs = docker_service.get_logs(container_id, tail=log_lines)
        else:
            logs = docker_service.get_all_logs(tail=log_lines)
        
        issues = await ai_service.quick_issue_check(logs)
        status = ai_service.get_analysis_status()
        
        return {
            "logs_scanned": len(logs),
            "issues_found": len(issues),
            "issues": issues,
            "analysis_status": status,
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/issues/{issue_id}/resolve")
async def resolve_issue(issue_id: str):
    """Mark an issue as resolved."""
    success = ai_service.resolve_issue(issue_id)
    if not success:
        raise HTTPException(status_code=404, detail="Issue not found")
    return {"status": "resolved", "issue_id": issue_id}


@router.delete("/issues")
async def clear_issues():
    """Clear all detected issues."""
    count = ai_service.clear_issues()
    return {"status": "cleared", "count": count}


# ==================== RAG / Advanced Analytics ====================

@router.get("/rag/status")
async def get_rag_status():
    """Get RAG system status."""
    status = ai_service.get_rag_status()
    
    # Add storage stats
    if settings.rag_enabled:
        try:
            from app.services.storage_service import storage_service
            storage_stats = await storage_service.get_stats()
            status["storage"] = storage_stats
        except Exception as e:
            status["storage"] = {"error": str(e)}
    
    return status


@router.post("/rag/ingest")
async def ingest_historical_logs(
    container_id: Optional[str] = Query(None, description="Container to ingest (None for all)"),
    tail: int = Query(1000, ge=100, le=10000, description="Number of log lines to ingest"),
):
    """Ingest historical logs into the RAG system."""
    if not settings.rag_enabled:
        raise HTTPException(status_code=400, detail="RAG is not enabled")
    
    try:
        from app.services.log_processor import log_processor
        
        count = await log_processor.ingest_historical_logs(
            container_id=container_id,
            tail=tail,
        )
        
        return {
            "status": "success",
            "logs_ingested": count,
            "container": container_id or "all",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rag/search")
async def search_logs(
    query: str = Query(..., description="Search query"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    container: Optional[str] = Query(None, description="Filter by container"),
    hours: Optional[int] = Query(None, ge=1, le=720, description="Filter by hours"),
):
    """Semantic search for similar log entries."""
    if not settings.rag_enabled:
        raise HTTPException(status_code=400, detail="RAG is not enabled")
    
    try:
        from app.services.vector_service import vector_service
        
        results = await vector_service.search(
            query=query,
            limit=limit,
            container_filter=container,
            time_filter_hours=hours,
        )
        
        return {
            "query": query,
            "results": [
                {
                    "container_name": r.document.container_name,
                    "message": r.document.message,
                    "timestamp": r.document.timestamp.isoformat() if r.document.timestamp else None,
                    "level": r.document.level,
                    "score": r.score,
                }
                for r in results
            ],
            "count": len(results),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rag/stats")
async def get_log_statistics(
    container: Optional[str] = Query(None, description="Filter by container"),
    days: int = Query(7, ge=1, le=90, description="Time range in days"),
):
    """Get log statistics from stored events."""
    if not settings.rag_enabled:
        raise HTTPException(status_code=400, detail="RAG is not enabled")
    
    try:
        from app.services.storage_service import storage_service, LogLevel
        from datetime import datetime, timedelta
        
        start_time = datetime.now() - timedelta(days=days)
        
        # Get various statistics
        error_counts = await storage_service.get_error_count_by_container(
            start_time=start_time
        )
        
        patterns = await storage_service.get_pattern_frequency(
            container_name=container,
            start_time=start_time,
        )
        
        daily = await storage_service.get_daily_counts(
            container_name=container,
            days=days,
        )
        
        hourly = await storage_service.get_hourly_distribution(
            container_name=container,
            days=days,
        )
        
        total_events = await storage_service.count_events(
            container_name=container,
            start_time=start_time,
        )
        
        total_errors = await storage_service.count_events(
            container_name=container,
            level=LogLevel.ERROR,
            start_time=start_time,
        )
        
        return {
            "time_range_days": days,
            "container": container or "all",
            "total_events": total_events,
            "total_errors": total_errors,
            "error_counts_by_container": error_counts,
            "top_patterns": patterns,
            "daily_distribution": daily,
            "hourly_distribution": hourly,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/ask")
async def ask_with_rag(
    question: str = Query(..., description="Question to ask about logs"),
    container: Optional[str] = Query(None, description="Focus on specific container"),
    include_recent: bool = Query(True, description="Include recent logs"),
):
    """Ask a question with RAG-enhanced context (historical + semantic search)."""
    try:
        logs = None
        if include_recent:
            if container:
                logs = docker_service.get_logs(container, tail=50)
            else:
                logs = docker_service.get_all_logs(tail=50)
        
        response = await ai_service.chat(message=question, logs=logs, stream=False)
        
        return {
            "question": question,
            "answer": response,
            "rag_enabled": ai_service.rag_enabled,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rag/issue-frequency")
async def get_issue_frequency(
    pattern: str = Query(..., description="Error pattern to search"),
    container: Optional[str] = Query(None, description="Filter by container"),
    days: int = Query(30, ge=1, le=90, description="Time range in days"),
):
    """Get frequency statistics for a specific issue pattern."""
    if not settings.rag_enabled:
        raise HTTPException(status_code=400, detail="RAG is not enabled")
    
    try:
        result = await ai_service.get_issue_frequency(
            pattern=pattern,
            container_name=container,
            days=days,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
