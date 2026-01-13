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


@router.post("/issues/scan")
async def scan_for_issues(
    container_id: Optional[str] = None,
    log_lines: int = Query(100, ge=10, le=1000),
):
    """Scan logs for issues using pattern detection."""
    try:
        if container_id:
            logs = docker_service.get_logs(container_id, tail=log_lines)
        else:
            logs = docker_service.get_all_logs(tail=log_lines)
        
        issues = await ai_service.quick_issue_check(logs)
        
        return {
            "logs_scanned": len(logs),
            "issues_found": len(issues),
            "issues": issues,
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
