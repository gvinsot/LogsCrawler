"""API routes for remote systems management."""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional

from app.models import (
    RemoteSystem,
    RemoteSystemCreate,
    RemoteSystemUpdate,
    ContainerInfo,
    ContainerLog,
)
from app.services.remote_systems_service import remote_systems_service
from app.services.remote_docker_service import remote_docker_service

router = APIRouter(prefix="/api/systems", tags=["Remote Systems"])


# ==================== Remote Systems CRUD ====================

@router.get("", response_model=List[RemoteSystem])
async def list_systems():
    """Get all registered remote systems."""
    return remote_systems_service.get_all_systems()


@router.get("/{system_id}", response_model=RemoteSystem)
async def get_system(system_id: str):
    """Get a specific remote system."""
    system = remote_systems_service.get_system(system_id)
    if not system:
        raise HTTPException(status_code=404, detail="System not found")
    return system


@router.post("", response_model=RemoteSystem)
async def create_system(data: RemoteSystemCreate):
    """Create a new remote system configuration."""
    return await remote_systems_service.create_system(data)


@router.put("/{system_id}", response_model=RemoteSystem)
async def update_system(system_id: str, data: RemoteSystemUpdate):
    """Update a remote system configuration."""
    system = await remote_systems_service.update_system(system_id, data)
    if not system:
        raise HTTPException(status_code=404, detail="System not found")
    return system


@router.delete("/{system_id}")
async def delete_system(system_id: str):
    """Delete a remote system."""
    success = await remote_systems_service.delete_system(system_id)
    if not success:
        raise HTTPException(status_code=404, detail="System not found")
    return {"status": "deleted", "system_id": system_id}


@router.post("/{system_id}/test")
async def test_system_connection(system_id: str):
    """Test SSH connection to a remote system."""
    result = await remote_systems_service.test_connection(system_id)
    return result


# ==================== Remote Containers ====================

@router.get("/{system_id}/containers", response_model=List[ContainerInfo])
async def list_remote_containers(
    system_id: str,
    all: bool = Query(True, description="Include stopped containers")
):
    """Get containers from a remote system."""
    system = remote_systems_service.get_system(system_id)
    if not system:
        raise HTTPException(status_code=404, detail="System not found")
    
    try:
        return await remote_docker_service.get_containers(system_id, all_containers=all)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{system_id}/containers/{container_id}", response_model=ContainerInfo)
async def get_remote_container(system_id: str, container_id: str):
    """Get a specific container from a remote system."""
    system = remote_systems_service.get_system(system_id)
    if not system:
        raise HTTPException(status_code=404, detail="System not found")
    
    container = await remote_docker_service.get_container(system_id, container_id)
    if not container:
        raise HTTPException(status_code=404, detail="Container not found")
    return container


# ==================== Remote Logs ====================

@router.get("/{system_id}/logs", response_model=List[ContainerLog])
async def get_all_remote_logs(
    system_id: str,
    tail: int = Query(50, ge=1, le=1000, description="Lines per container")
):
    """Get logs from all running containers on a remote system."""
    system = remote_systems_service.get_system(system_id)
    if not system:
        raise HTTPException(status_code=404, detail="System not found")
    
    try:
        return await remote_docker_service.get_all_logs(system_id, tail=tail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{system_id}/logs/{container_id}", response_model=List[ContainerLog])
async def get_remote_container_logs(
    system_id: str,
    container_id: str,
    tail: int = Query(100, ge=1, le=5000, description="Number of lines to fetch"),
    timestamps: bool = Query(True, description="Include timestamps")
):
    """Get logs from a specific container on a remote system."""
    system = remote_systems_service.get_system(system_id)
    if not system:
        raise HTTPException(status_code=404, detail="System not found")
    
    try:
        return await remote_docker_service.get_logs(
            system_id, container_id, tail=tail, timestamps=timestamps
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
