"""FastAPI REST API for LogsCrawler."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .collector import Collector
from .config import load_config, Settings
from .models import (
    ActionRequest, ActionResult, ContainerInfo, ContainerStatus,
    DashboardStats, LogSearchQuery, LogSearchResult, TimeSeriesPoint, TimeSeriesByHost
)
from .opensearch_client import OpenSearchClient
from .github_service import GitHubService, StackDeployer
from .actions_queue import actions_queue, ActionType, ActionStatus

logger = structlog.get_logger()

# Global instances
settings: Settings = None
opensearch: OpenSearchClient = None
collector: Collector = None
github_service: GitHubService = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global settings, opensearch, collector, github_service
    
    # Startup
    logger.info("Starting LogsCrawler API")

    settings = load_config()
    opensearch = OpenSearchClient(settings.opensearch)

    # Initialize OpenSearch with retry (wait for DNS/service to be ready)
    max_retries = 30
    retry_delay = 2
    for attempt in range(max_retries):
        try:
            await opensearch.initialize()
            break
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(
                    "OpenSearch not ready, retrying...",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    error=str(e),
                )
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Failed to connect to OpenSearch after retries", error=str(e))
                raise
    
    collector = Collector(settings, opensearch)
    await collector.start()
    
    # Initialize GitHub service
    github_service = GitHubService(settings.github)
    
    yield
    
    # Shutdown
    logger.info("Shutting down LogsCrawler API")
    await collector.stop()
    await opensearch.close()
    await github_service.close()


app = FastAPI(
    title="LogsCrawler API",
    description="Docker container log aggregation and monitoring",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============== Dashboard ==============

@app.get("/api/dashboard/stats", response_model=DashboardStats)
async def get_dashboard_stats():
    """Get dashboard statistics."""
    stats = await opensearch.get_dashboard_stats()
    
    # Add container counts
    containers = await collector.get_all_containers()
    stats.total_containers = len(containers)
    stats.running_containers = len([c for c in containers if c.status == ContainerStatus.RUNNING])
    # Total hosts = configured hosts + discovered swarm nodes (each swarm node counts as a host)
    stats.total_hosts = len(collector.clients)
    stats.healthy_hosts = len(collector.clients)  # Simplistic health check
    
    return stats


@app.get("/api/dashboard/errors-timeseries", response_model=List[TimeSeriesPoint])
async def get_errors_timeseries(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="1h")
):
    """Get error count time series."""
    return await opensearch.get_error_timeseries(hours=hours, interval=interval)


@app.get("/api/dashboard/http-4xx-timeseries", response_model=List[TimeSeriesPoint])
async def get_http_4xx_timeseries(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="1h")
):
    """Get HTTP 4xx count time series."""
    return await opensearch.get_http_status_timeseries(400, 500, hours=hours, interval=interval)


@app.get("/api/dashboard/http-5xx-timeseries", response_model=List[TimeSeriesPoint])
async def get_http_5xx_timeseries(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="1h")
):
    """Get HTTP 5xx count time series."""
    return await opensearch.get_http_status_timeseries(500, 600, hours=hours, interval=interval)


@app.get("/api/dashboard/http-requests-timeseries", response_model=List[TimeSeriesPoint])
async def get_http_requests_timeseries(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="1h")
):
    """Get total HTTP requests count time series."""
    return await opensearch.get_http_requests_timeseries(hours=hours, interval=interval)


@app.get("/api/dashboard/cpu-timeseries", response_model=List[TimeSeriesPoint])
async def get_cpu_timeseries(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="15m")
):
    """Get CPU usage time series."""
    return await opensearch.get_resource_timeseries("cpu_percent", hours=hours, interval=interval)


@app.get("/api/dashboard/gpu-timeseries", response_model=List[TimeSeriesPoint])
async def get_gpu_timeseries(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="15m")
):
    """Get GPU usage time series."""
    return await opensearch.get_resource_timeseries("gpu_percent", hours=hours, interval=interval)


@app.get("/api/dashboard/memory-timeseries", response_model=List[TimeSeriesPoint])
async def get_memory_timeseries(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="15m")
):
    """Get memory usage time series."""
    return await opensearch.get_resource_timeseries("memory_percent", hours=hours, interval=interval)


@app.get("/api/dashboard/cpu-timeseries-by-host", response_model=List[TimeSeriesByHost])
async def get_cpu_timeseries_by_host(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="15m")
):
    """Get CPU usage time series grouped by host."""
    return await opensearch.get_resource_timeseries_by_host("cpu_percent", hours=hours, interval=interval)


@app.get("/api/dashboard/gpu-timeseries-by-host", response_model=List[TimeSeriesByHost])
async def get_gpu_timeseries_by_host(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="15m")
):
    """Get GPU usage time series grouped by host."""
    return await opensearch.get_resource_timeseries_by_host("gpu_percent", hours=hours, interval=interval)


@app.get("/api/dashboard/memory-timeseries-by-host", response_model=List[TimeSeriesByHost])
async def get_memory_timeseries_by_host(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="15m")
):
    """Get memory usage time series grouped by host."""
    return await opensearch.get_resource_timeseries_by_host("memory_percent", hours=hours, interval=interval)


@app.get("/api/dashboard/vram-timeseries-by-host", response_model=List[TimeSeriesByHost])
async def get_vram_timeseries_by_host(
    hours: int = Query(default=24, ge=1, le=168),
    interval: str = Query(default="15m")
):
    """Get VRAM usage time series grouped by host (percentage of total)."""
    # Use gpu_memory_used_mb / gpu_memory_total_mb * 100
    return await opensearch.get_vram_percent_timeseries_by_host(hours=hours, interval=interval)


# ============== Containers ==============

@app.get("/api/containers", response_model=List[ContainerInfo])
async def list_containers(
    refresh: bool = Query(default=False),
    status: Optional[ContainerStatus] = Query(default=None),
    host: Optional[str] = Query(default=None),
    compose_project: Optional[str] = Query(default=None),
):
    """List all containers with optional filters."""
    containers = await collector.get_all_containers(refresh=refresh)
    
    # Apply filters
    if status:
        containers = [c for c in containers if c.status == status]
    if host:
        containers = [c for c in containers if c.host == host]
    if compose_project:
        containers = [c for c in containers if c.compose_project == compose_project]
    
    return containers


@app.get("/api/containers/grouped")
async def list_containers_grouped(
    refresh: bool = Query(default=False),
    status: Optional[ContainerStatus] = Query(default=None),
    group_by: str = Query(default="host", description="Group by: 'host' or 'stack'"),
) -> Dict[str, Dict[str, List[ContainerInfo]]]:
    """List containers grouped by host and compose project, or by Docker Swarm stack."""
    containers = await collector.get_all_containers(refresh=refresh)
    
    if status:
        containers = [c for c in containers if c.status == status]
    
    # Fetch latest stats for all containers (single query)
    latest_stats = await opensearch.get_latest_container_stats()
    
    # Enrich containers with stats
    for container in containers:
        stats = latest_stats.get(container.id)
        if stats:
            container.cpu_percent = stats.get("cpu_percent")
            container.memory_percent = stats.get("memory_percent")
            container.memory_usage_mb = stats.get("memory_usage_mb")
    
    if group_by == "stack":
        # Group by Docker Swarm stack -> service
        # First, find the Swarm manager host
        swarm_manager_host = None
        for host_config in settings.hosts:
            if host_config.swarm_manager:
                swarm_manager_host = host_config.name
                break
        
        # Get stack information from manager if available
        stack_services_map: Dict[str, List[str]] = {}
        if swarm_manager_host:
            manager_client = collector.clients.get(swarm_manager_host)
            if manager_client:
                try:
                    stack_services_map = await manager_client.get_swarm_stacks()
                    logger.info("Retrieved stacks from Swarm manager", host=swarm_manager_host, stacks=list(stack_services_map.keys()))
                except Exception as e:
                    logger.warning("Failed to get stacks from Swarm manager", host=swarm_manager_host, error=str(e))
        
        # Initialize grouped structure with all known stacks from manager
        # This ensures stacks are shown even if they have no containers yet
        grouped: Dict[str, Dict[str, List[ContainerInfo]]] = {}
        for stack_name in stack_services_map.keys():
            grouped[stack_name] = {}
        
        # Group containers by their stack
        for container in containers:
            # Try to get stack name from Swarm labels first
            stack_name = container.labels.get("com.docker.swarm.stack.namespace")
            service_name = container.labels.get("com.docker.swarm.service.name")
            
            # If no stack from labels, try to extract from container name
            # Docker Swarm container names: stack_service.replica_id or stack_service.replica_id.node_id
            # Example: myapp_web.1.abc123def456 -> stack: myapp, service: web
            if not stack_name and "." in container.name:
                main_part = container.name.split(".")[0]
                # Check if this matches any known stack pattern
                for known_stack in stack_services_map.keys():
                    if main_part.startswith(known_stack + "_"):
                        stack_name = known_stack
                        if not service_name:
                            service_name = main_part[len(known_stack) + 1:]
                        break
                
                # If still no stack found but has underscore pattern, try to extract
                if not stack_name and "_" in main_part:
                    # Try to match against known stacks by checking if prefix matches
                    for known_stack in stack_services_map.keys():
                        if main_part.startswith(known_stack):
                            stack_name = known_stack
                            if not service_name:
                                # Extract service name after stack prefix
                                remaining = main_part[len(known_stack):]
                                if remaining.startswith("_"):
                                    service_name = remaining[1:]
                                else:
                                    service_name = remaining
                            break
                    
                    # If still not found, assume first part before underscore is stack
                    if not stack_name:
                        parts = main_part.split("_", 1)
                        if len(parts) == 2:
                            potential_stack = parts[0]
                            # Check if this potential stack exists in our known stacks
                            if potential_stack in stack_services_map:
                                stack_name = potential_stack
                                service_name = parts[1] if not service_name else service_name
            
            # If we have stack info from manager, verify the stack exists
            if stack_name and stack_name in stack_services_map:
                # This is a confirmed Swarm stack
                if not service_name:
                    # Last resort: extract from container name
                    if "." in container.name:
                        main_part = container.name.split(".")[0]
                        if "_" in main_part and main_part.startswith(stack_name + "_"):
                            service_name = main_part[len(stack_name) + 1:]
                        else:
                            service_name = main_part.split("_", 1)[-1] if "_" in main_part else main_part
                    else:
                        service_name = container.name
            elif stack_name:
                # Has swarm label but stack not found in manager - might be stale
                # Still group it but use the stack name from label
                if not service_name:
                    if "." in container.name:
                        main_part = container.name.split(".")[0]
                        if "_" in main_part and main_part.startswith(stack_name + "_"):
                            service_name = main_part[len(stack_name) + 1:]
                        else:
                            service_name = main_part.split("_", 1)[-1] if "_" in main_part else main_part
                    else:
                        service_name = container.name
            else:
                # Not a Swarm stack, use compose project or standalone
                stack_name = container.compose_project or "_standalone"
                service_name = container.compose_service or \
                              container.name.split(".")[0] if "." in container.name else container.name
            
            # Ensure stack group exists
            if stack_name not in grouped:
                grouped[stack_name] = {}
            
            # Ensure service group exists
            if service_name not in grouped[stack_name]:
                grouped[stack_name][service_name] = []
            
            grouped[stack_name][service_name].append(container)
        
        # Remove empty stacks (stacks from manager that have no containers)
        # But keep stacks that have containers even if not in manager list
        empty_stacks = [stack for stack, services in grouped.items() 
                       if not services and stack in stack_services_map]
        for stack in empty_stacks:
            del grouped[stack]
    else:
        # Group by host -> compose_project (default)
        grouped: Dict[str, Dict[str, List[ContainerInfo]]] = {}
        
        for container in containers:
            host = container.host
            project = container.compose_project or "_standalone"
            
            if host not in grouped:
                grouped[host] = {}
            if project not in grouped[host]:
                grouped[host][project] = []
            
            grouped[host][project].append(container)
    
    return grouped


@app.get("/api/containers/{host}/{container_id}/stats")
async def get_container_stats(host: str, container_id: str) -> Dict[str, Any]:
    """Get current stats for a container."""
    stats = await collector.get_container_stats(host, container_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Container not found")
    return stats


@app.get("/api/containers/{host}/{container_id}/logs")
async def get_container_logs(
    host: str,
    container_id: str,
    tail: int = Query(default=200, ge=1, le=10000)
) -> List[Dict[str, Any]]:
    """Get live logs for a container."""
    logs = await collector.get_container_logs_live(host, container_id, tail=tail)
    return logs


@app.get("/api/containers/{host}/{container_id}/env")
async def get_container_env(host: str, container_id: str) -> Dict[str, Any]:
    """Get environment variables for a container by running printenv inside it."""
    env_data = await collector.get_container_env(host, container_id)
    if not env_data:
        raise HTTPException(status_code=404, detail="Container not found")
    return env_data


@app.post("/api/containers/action", response_model=ActionResult)
async def execute_container_action(request: ActionRequest) -> ActionResult:
    """Execute an action on a container (start, stop, restart, etc.)."""
    success, message = await collector.execute_action(
        request.host, 
        request.container_id, 
        request.action.value
    )
    
    return ActionResult(
        success=success,
        message=message,
        container_id=request.container_id,
        action=request.action,
    )


@app.post("/api/stacks/{stack_name}/remove")
async def remove_stack(stack_name: str, host: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """Remove a Docker Swarm stack."""
    containers = await collector.get_all_containers(refresh=True)
    
    # Find containers belonging to this stack
    stack_containers = [
        c for c in containers 
        if (c.labels.get("com.docker.swarm.stack.namespace") == stack_name or
            (stack_name != "_standalone" and c.compose_project == stack_name))
    ]
    
    if not stack_containers:
        raise HTTPException(status_code=404, detail=f"Stack '{stack_name}' not found")
    
    # If host not specified, use the first host that has containers from this stack
    if not host:
        host = stack_containers[0].host
    
    # Check if this is actually a Swarm stack (has swarm labels)
    is_swarm_stack = any(
        c.labels.get("com.docker.swarm.stack.namespace") == stack_name 
        for c in stack_containers
    )
    
    if not is_swarm_stack:
        raise HTTPException(
            status_code=400, 
            detail=f"'{stack_name}' is not a Docker Swarm stack. Use container removal for compose projects."
        )
    
    # Execute stack removal
    client = collector.clients.get(host)
    if not client:
        raise HTTPException(status_code=404, detail=f"Host '{host}' not found")
    
    success, message = await client.remove_stack(stack_name)
    
    if success:
        return {"success": True, "message": message, "stack_name": stack_name}
    else:
        raise HTTPException(status_code=500, detail=message)


# ============== Logs Search ==============

@app.post("/api/logs/search", response_model=LogSearchResult)
async def search_logs(query: LogSearchQuery) -> LogSearchResult:
    """Search logs with filters."""
    return await opensearch.search_logs(query)


@app.get("/api/logs/search")
async def search_logs_get(
    q: Optional[str] = Query(default=None, alias="query"),
    hosts: Optional[str] = Query(default=None),
    containers: Optional[str] = Query(default=None),
    compose_projects: Optional[str] = Query(default=None),
    levels: Optional[str] = Query(default=None),
    http_status_min: Optional[int] = Query(default=None),
    http_status_max: Optional[int] = Query(default=None),
    start_time: Optional[datetime] = Query(default=None),
    end_time: Optional[datetime] = Query(default=None),
    size: int = Query(default=100, ge=1, le=10000),
    from_: int = Query(default=0, alias="from"),
    sort_order: str = Query(default="desc"),
) -> LogSearchResult:
    """Search logs with GET parameters."""
    query = LogSearchQuery(
        query=q,
        hosts=hosts.split(",") if hosts else [],
        containers=containers.split(",") if containers else [],
        compose_projects=compose_projects.split(",") if compose_projects else [],
        levels=levels.split(",") if levels else [],
        http_status_min=http_status_min,
        http_status_max=http_status_max,
        start_time=start_time,
        end_time=end_time,
        size=size,
        from_=from_,
        sort_order=sort_order,
    )
    return await opensearch.search_logs(query)


# ============== AI Query ==============

@app.post("/api/logs/ai-search")
async def ai_search_logs(request: Dict[str, str]) -> Dict[str, Any]:
    """Convert natural language query to OpenSearch query and execute."""
    from .ai_service import get_ai_service
    
    natural_query = request.get("question", "")
    if not natural_query:
        raise HTTPException(status_code=400, detail="Question is required")
    
    ai = get_ai_service()
    
    # Convert natural language to query params
    params = await ai.convert_to_query(natural_query)
    
    # Calculate time range
    start_time = None
    if params.get("time_range"):
        time_str = params["time_range"]
        now = datetime.utcnow()
        if time_str.endswith("m"):
            minutes = int(time_str[:-1])
            start_time = now - timedelta(minutes=minutes)
        elif time_str.endswith("h"):
            hours = int(time_str[:-1])
            start_time = now - timedelta(hours=hours)
        elif time_str.endswith("d"):
            days = int(time_str[:-1])
            start_time = now - timedelta(days=days)
    
    # Build and execute query
    query = LogSearchQuery(
        query=params.get("query"),
        hosts=params.get("hosts", []),
        containers=params.get("containers", []),
        levels=params.get("levels", []),
        http_status_min=params.get("http_status_min"),
        http_status_max=params.get("http_status_max"),
        start_time=start_time,
        size=100,
        sort_order=params.get("sort_order", "desc"),
    )
    
    result = await opensearch.search_logs(query)
    
    return {
        "query_params": params,
        "result": result.model_dump(),
    }


@app.get("/api/ai/status")
async def get_ai_status() -> Dict[str, Any]:
    """Check AI service availability."""
    from .ai_service import get_ai_service
    
    ai = get_ai_service()
    available = await ai.check_availability()
    
    return {
        "available": available,
        "model": ai.model,
        "ollama_url": ai.ollama_url,
    }


@app.post("/api/logs/similar-count")
async def get_similar_logs_count(request: Dict[str, Any]) -> Dict[str, Any]:
    """Count similar log messages in the last N hours."""
    message = request.get("message", "")
    container_name = request.get("container_name", "")
    hours = request.get("hours", 24)
    
    if not message:
        return {"count": 0}
    
    count = await opensearch.count_similar_logs(message, container_name, hours)
    
    return {"count": count}


@app.post("/api/logs/ai-analyze")
async def analyze_log_message(request: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze a log message using AI to determine if it's normal or needs attention."""
    from .ai_service import get_ai_service
    
    message = request.get("message", "")
    level = request.get("level", "")
    container_name = request.get("container_name", "")
    
    if not message:
        return {"severity": "normal", "assessment": "No message to analyze"}
    
    ai = get_ai_service()
    result = await ai.analyze_log(message, level, container_name)
    
    return result


# ============== Hosts ==============

@app.get("/api/hosts")
async def list_hosts() -> List[Dict[str, Any]]:
    """List all hosts: configured hosts plus discovered Docker Swarm nodes.

    Each swarm node is exposed as a host so the Containers tab can show
    containers per node. Host count = configured + swarm nodes.
    """
    configured_names = {h.name for h in settings.hosts}
    result = [
        {
            "name": host.name,
            "hostname": host.hostname,
            "port": host.port,
            "username": host.username,
            "is_swarm_node": False,
        }
        for host in settings.hosts
    ]
    # Add discovered swarm nodes as hosts (so they appear in host list and Containers tab)
    for name, client in collector.clients.items():
        if name not in configured_names:
            result.append({
                "name": name,
                "hostname": client.config.hostname,
                "port": client.config.port,
                "username": client.config.username,
                "is_swarm_node": True,
            })
    return result


@app.get("/api/hosts/metrics")
async def get_hosts_metrics() -> Dict[str, Dict[str, Any]]:
    """Get latest metrics for all hosts including GPU usage.
    
    Returns a dict keyed by host name with metrics:
    - cpu_percent, memory_percent, memory_used_mb, memory_total_mb
    - gpu_percent, gpu_memory_used_mb, gpu_memory_total_mb (if GPU available)
    """
    result = {}
    
    # Get latest host metrics from OpenSearch
    try:
        for host_name in collector.clients.keys():
            metrics = await opensearch.get_latest_host_metrics(host_name)
            if metrics:
                result[host_name] = metrics
    except Exception as e:
        logger.error("Failed to get host metrics", error=str(e))
    
    return result


# ============== Health ==============

@app.get("/api/health")
async def health_check() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "logscrawler"}


# ============== Configuration ==============

@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """Get current configuration (for debugging).

    Returns the loaded configuration including hosts, opensearch settings,
    and collector settings. Useful for verifying environment variables
    are being parsed correctly.
    """
    # Get list of configured hosts
    configured_hosts = [
        {
            "name": h.name,
            "hostname": h.hostname,
            "mode": h.mode,
            "docker_url": h.docker_url,
            "swarm_manager": h.swarm_manager,
            "swarm_routing": h.swarm_routing,
            "swarm_autodiscover": h.swarm_autodiscover,
        }
        for h in settings.hosts
    ]

    # Get list of active clients (including discovered nodes)
    active_clients = [
        {
            "name": name,
            "mode": client.config.mode,
            "hostname": client.config.hostname,
        }
        for name, client in collector.clients.items()
    ]

    return {
        "hosts": {
            "configured": configured_hosts,
            "active_clients": active_clients,
            "discovered_nodes": list(collector._discovered_nodes.keys()) if hasattr(collector, '_discovered_nodes') else [],
        },
        "opensearch": {
            "hosts": settings.opensearch.hosts,
            "index_prefix": settings.opensearch.index_prefix,
            "has_auth": bool(settings.opensearch.username),
        },
        "collector": {
            "log_interval_seconds": settings.collector.log_interval_seconds,
            "metrics_interval_seconds": settings.collector.metrics_interval_seconds,
            "log_lines_per_fetch": settings.collector.log_lines_per_fetch,
            "retention_days": settings.collector.retention_days,
        },
        "ai": {
            "model": settings.ai.model,
        },
        "swarm": {
            "manager_host": collector._swarm_manager_host if hasattr(collector, '_swarm_manager_host') else None,
            "routing_enabled": collector._swarm_routing_enabled if hasattr(collector, '_swarm_routing_enabled') else False,
            "autodiscover_enabled": collector._swarm_autodiscover_enabled if hasattr(collector, '_swarm_autodiscover_enabled') else False,
        }
    }


@app.get("/api/config/test")
async def test_config() -> Dict[str, Any]:
    """Test configuration by checking connectivity to all hosts.

    Returns status of each configured host including:
    - Connection status
    - Number of containers found
    - Any errors encountered
    """
    results = []

    for name, client in collector.clients.items():
        result = {
            "name": name,
            "mode": client.config.mode,
            "status": "unknown",
            "containers": 0,
            "error": None,
        }

        try:
            containers = await client.get_containers()
            result["status"] = "connected"
            result["containers"] = len(containers)
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)

        results.append(result)

    return {
        "hosts": results,
        "total_hosts": len(results),
        "connected": sum(1 for r in results if r["status"] == "connected"),
        "errors": sum(1 for r in results if r["status"] == "error"),
    }


# ============== Stacks (GitHub Integration) ==============

@app.get("/api/stacks/status")
async def get_stacks_status():
    """Get GitHub integration status."""
    return {
        "configured": github_service.is_configured(),
        "username": settings.github.username,
    }


@app.get("/api/stacks/repos")
async def get_starred_repos():
    """Get list of starred GitHub repositories."""
    if not github_service.is_configured():
        raise HTTPException(status_code=400, detail="GitHub integration not configured")
    
    repos = await github_service.get_starred_repos()
    return {"repos": repos, "count": len(repos)}


@app.post("/api/stacks/build")
async def build_stack(
    repo_name: str = Query(..., description="Repository name"),
    ssh_url: str = Query(..., description="SSH URL for cloning"),
    version: str = Query(default="1.0", description="Version tag"),
):
    """Build a stack from a GitHub repository."""
    if not github_service.is_configured():
        raise HTTPException(status_code=400, detail="GitHub integration not configured")
    
    # Get the first configured host client for running commands
    if not collector.clients:
        raise HTTPException(status_code=500, detail="No host clients available")
    
    # Use the swarm manager or first available host
    host_name = None
    host_client = None
    
    # Prefer swarm manager if available
    for name, client in collector.clients.items():
        if hasattr(client, 'config') and getattr(client.config, 'swarm_manager', False):
            host_name = name
            host_client = client
            break
    
    # Fallback to first host
    if not host_client:
        host_name, host_client = next(iter(collector.clients.items()))
    
    deployer = StackDeployer(settings.github, host_client)
    result = await deployer.build(repo_name, ssh_url, version)
    result["host"] = host_name
    
    return result


@app.post("/api/stacks/deploy")
async def deploy_stack(
    repo_name: str = Query(..., description="Repository name"),
    ssh_url: str = Query(..., description="SSH URL for cloning"),
    version: str = Query(default="1.0", description="Version tag"),
):
    """Deploy a stack from a GitHub repository."""
    if not github_service.is_configured():
        raise HTTPException(status_code=400, detail="GitHub integration not configured")
    
    # Get the first configured host client for running commands
    if not collector.clients:
        raise HTTPException(status_code=500, detail="No host clients available")
    
    # Use the swarm manager or first available host
    host_name = None
    host_client = None
    
    # Prefer swarm manager if available
    for name, client in collector.clients.items():
        if hasattr(client, 'config') and getattr(client.config, 'swarm_manager', False):
            host_name = name
            host_client = client
            break
    
    # Fallback to first host
    if not host_client:
        host_name, host_client = next(iter(collector.clients.items()))
    
    deployer = StackDeployer(settings.github, host_client)
    result = await deployer.deploy(repo_name, ssh_url, version)
    result["host"] = host_name
    
    return result


@app.get("/api/stacks/{repo_name}/env")
async def get_stack_env(repo_name: str):
    """Get the .env file content for a stack."""
    if not github_service.is_configured():
        raise HTTPException(status_code=400, detail="GitHub integration not configured")
    
    deployer = StackDeployer(settings.github, None)
    success, content = await deployer.get_env_file(repo_name)
    
    if not success:
        raise HTTPException(status_code=500, detail=content)
    
    return {"content": content, "repo": repo_name}


@app.put("/api/stacks/{repo_name}/env")
async def save_stack_env(repo_name: str, request: Request):
    """Save the .env file content for a stack."""
    if not github_service.is_configured():
        raise HTTPException(status_code=400, detail="GitHub integration not configured")
    
    body = await request.json()
    content = body.get("content", "")
    
    deployer = StackDeployer(settings.github, None)
    success, message = await deployer.save_env_file(repo_name, content)
    
    if not success:
        raise HTTPException(status_code=500, detail=message)
    
    return {"success": True, "message": message, "repo": repo_name}


# ============== Agent API ==============
# These endpoints are used by agents running on remote hosts

@app.get("/api/agent/actions")
async def get_agent_actions(agent_id: str = Query(..., description="Agent identifier")):
    """Get pending actions for an agent.

    Agents poll this endpoint to receive actions to execute.
    Actions are marked as in_progress when returned.
    """
    actions = await actions_queue.get_pending_actions(agent_id)
    return {
        "agent_id": agent_id,
        "actions": [action.model_dump() for action in actions],
    }


@app.post("/api/agent/result")
async def post_agent_result(
    agent_id: str = Query(..., description="Agent identifier"),
    action_id: str = Query(..., description="Action ID"),
    success: bool = Query(..., description="Whether action succeeded"),
    output: str = Query(default="", description="Action output"),
):
    """Report action result from an agent.

    Agents call this after executing an action to report the result.
    """
    action = await actions_queue.complete_action(action_id, success, output)
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    return {"status": "ok", "action_id": action_id}


@app.get("/api/agents")
async def get_agents():
    """Get list of known agents and their status."""
    agents = await actions_queue.get_agents()
    result = []
    for agent in agents:
        is_online = await actions_queue.is_agent_online(agent.agent_id)
        result.append({
            "agent_id": agent.agent_id,
            "last_seen": agent.last_seen.isoformat(),
            "status": agent.status,
            "online": is_online,
        })
    return {"agents": result}


@app.post("/api/agent/action")
async def create_agent_action(
    agent_id: str = Query(..., description="Target agent identifier"),
    action_type: str = Query(..., description="Action type (container_action, exec, get_logs, get_env)"),
    container_id: Optional[str] = Query(default=None, description="Container ID for container actions"),
    action: Optional[str] = Query(default=None, description="Container action (start, stop, restart, etc.)"),
    command: Optional[str] = Query(default=None, description="Command to execute (for exec action)"),
    tail: Optional[int] = Query(default=100, description="Number of log lines (for get_logs action)"),
    wait: bool = Query(default=True, description="Wait for action to complete"),
    timeout: float = Query(default=30.0, description="Timeout in seconds when waiting"),
):
    """Create an action for an agent to execute.

    This is the main endpoint for the frontend/API to request actions on remote hosts.
    The action is queued and the agent will pick it up on next poll.

    If wait=True, the endpoint blocks until the action completes or times out.
    """
    # Validate action type
    try:
        action_type_enum = ActionType(action_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid action type: {action_type}")

    # Build payload based on action type
    payload = {}
    if action_type_enum == ActionType.CONTAINER_ACTION:
        if not container_id or not action:
            raise HTTPException(status_code=400, detail="container_id and action required for container_action")
        payload = {"container_id": container_id, "action": action}

    elif action_type_enum == ActionType.EXEC:
        if not container_id or not command:
            raise HTTPException(status_code=400, detail="container_id and command required for exec")
        # Parse command string into list
        import shlex
        try:
            cmd_list = shlex.split(command)
        except ValueError:
            cmd_list = command.split()
        payload = {"container_id": container_id, "command": cmd_list}

    elif action_type_enum == ActionType.GET_LOGS:
        if not container_id:
            raise HTTPException(status_code=400, detail="container_id required for get_logs")
        payload = {"container_id": container_id, "tail": tail}

    elif action_type_enum == ActionType.GET_ENV:
        if not container_id:
            raise HTTPException(status_code=400, detail="container_id required for get_env")
        payload = {"container_id": container_id}

    # Create the action
    action_obj = await actions_queue.create_action(agent_id, action_type_enum, payload)

    if not wait:
        return {
            "action_id": action_obj.id,
            "status": action_obj.status,
            "message": "Action queued",
        }

    # Wait for action to complete
    completed_action = await actions_queue.wait_for_action(action_obj.id, timeout=timeout)

    if not completed_action:
        return {
            "action_id": action_obj.id,
            "status": "timeout",
            "message": "Action timed out waiting for agent",
        }

    return {
        "action_id": completed_action.id,
        "status": completed_action.status,
        "success": completed_action.success,
        "result": completed_action.result,
    }


# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


@app.get("/")
async def serve_frontend():
    """Serve the frontend."""
    return FileResponse("frontend/index.html")
