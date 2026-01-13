"""WebSocket handlers for real-time log streaming."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict, Set
import asyncio
import json

from app.services.docker_service import docker_service
from app.services.ai_service import ai_service

router = APIRouter(tags=["WebSocket"])


class ConnectionManager:
    """Manage WebSocket connections."""
    
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.all_connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket, container_id: str = "all"):
        """Accept and register a new connection."""
        await websocket.accept()
        self.all_connections.add(websocket)
        
        if container_id not in self.active_connections:
            self.active_connections[container_id] = set()
        self.active_connections[container_id].add(websocket)
    
    def disconnect(self, websocket: WebSocket, container_id: str = "all"):
        """Remove a connection."""
        self.all_connections.discard(websocket)
        if container_id in self.active_connections:
            self.active_connections[container_id].discard(websocket)
    
    async def send_to_container_subscribers(self, container_id: str, message: dict):
        """Send message to all subscribers of a container."""
        subscribers = self.active_connections.get(container_id, set()).copy()
        subscribers.update(self.active_connections.get("all", set()))
        
        dead_connections = set()
        for connection in subscribers:
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.add(connection)
        
        # Clean up dead connections
        for conn in dead_connections:
            self.all_connections.discard(conn)
            for conns in self.active_connections.values():
                conns.discard(conn)
    
    async def broadcast(self, message: dict):
        """Broadcast message to all connections."""
        dead_connections = set()
        for connection in self.all_connections.copy():
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.add(connection)
        
        # Clean up
        for conn in dead_connections:
            self.all_connections.discard(conn)
            for conns in self.active_connections.values():
                conns.discard(conn)


manager = ConnectionManager()


@router.websocket("/ws/logs/{container_id}")
async def stream_container_logs(websocket: WebSocket, container_id: str):
    """Stream logs from a specific container in real-time."""
    await manager.connect(websocket, container_id)
    
    try:
        # Send initial batch of logs
        logs = docker_service.get_logs(container_id, tail=50)
        for log in logs:
            await websocket.send_json({
                "type": "log",
                "data": {
                    "container_id": log.container_id,
                    "container_name": log.container_name,
                    "message": log.message,
                    "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                    "stream": log.stream,
                }
            })
        
        # Stream new logs
        async for log in docker_service.stream_logs(container_id, tail=0):
            await websocket.send_json({
                "type": "log",
                "data": {
                    "container_id": log.container_id,
                    "container_name": log.container_name,
                    "message": log.message,
                    "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                    "stream": log.stream,
                }
            })
            
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        manager.disconnect(websocket, container_id)


@router.websocket("/ws/logs")
async def stream_all_logs(websocket: WebSocket):
    """Stream logs from all running containers."""
    import logging
    logger = logging.getLogger(__name__)
    
    await manager.connect(websocket, "all")
    logger.info("WebSocket /ws/logs connected")
    
    tasks = []
    
    try:
        # Send initial logs
        try:
            logs = docker_service.get_all_logs(tail=30)
            logger.info(f"Sending {len(logs)} initial logs")
            for log in logs:
                await websocket.send_json({
                    "type": "log",
                    "data": {
                        "container_id": log.container_id,
                        "container_name": log.container_name,
                        "message": log.message,
                        "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                        "stream": log.stream,
                    }
                })
        except Exception as e:
            logger.error(f"Error sending initial logs: {e}")
            await websocket.send_json({"type": "error", "message": f"Failed to get initial logs: {e}"})
        
        # Get running containers and stream their logs
        try:
            containers = docker_service.get_containers(all_containers=False)
            logger.info(f"Starting log streaming for {len(containers)} containers")
        except Exception as e:
            logger.error(f"Error getting containers: {e}")
            await websocket.send_json({"type": "error", "message": f"Failed to get containers: {e}"})
            containers = []
        
        async def stream_container(container):
            """Stream logs from a single container."""
            try:
                logger.info(f"Starting stream for container {container.id} ({container.name})")
                log_count = 0
                async for log in docker_service.stream_logs(container.id, tail=0):
                    log_count += 1
                    if log_count % 10 == 0:
                        logger.debug(f"Streamed {log_count} logs from {container.name}")
                    await manager.send_to_container_subscribers("all", {
                        "type": "log",
                        "data": {
                            "container_id": log.container_id,
                            "container_name": log.container_name,
                            "message": log.message,
                            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                            "stream": log.stream,
                        }
                    })
                logger.info(f"Finished streaming from {container.name} (total: {log_count} logs)")
            except Exception as e:
                logger.error(f"Error streaming logs from container {container.id} ({container.name}): {e}", exc_info=True)
        
        # Start streaming tasks for all containers
        tasks = [asyncio.create_task(stream_container(c)) for c in containers]
        logger.info(f"Started {len(tasks)} streaming tasks")
        
        # Keep connection alive and handle incoming messages
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                msg = json.loads(data)
                
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                    
            except asyncio.TimeoutError:
                # Send heartbeat
                await websocket.send_json({"type": "heartbeat"})
            except WebSocketDisconnect:
                logger.info("WebSocket /ws/logs disconnected")
                break
        
        # Cancel streaming tasks
        logger.info(f"Cancelling {len(tasks)} streaming tasks")
        for task in tasks:
            task.cancel()
        # Wait for tasks to finish cancelling
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
    except WebSocketDisconnect:
        logger.info("WebSocket /ws/logs disconnected (exception)")
    except Exception as e:
        logger.error(f"Error in stream_all_logs: {e}", exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        # Cancel any remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()
        manager.disconnect(websocket, "all")
        logger.info("WebSocket /ws/logs cleanup complete")


@router.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket):
    """WebSocket endpoint for streaming chat responses."""
    await websocket.accept()
    
    try:
        while True:
            # Receive message
            data = await websocket.receive_text()
            msg = json.loads(data)
            
            if msg.get("type") == "chat":
                user_message = msg.get("message", "")
                include_logs = msg.get("include_logs", True)
                container_id = msg.get("container_id")
                
                # Get logs if requested
                logs = None
                if include_logs:
                    if container_id:
                        logs = docker_service.get_logs(container_id, tail=50)
                    else:
                        logs = docker_service.get_all_logs(tail=50)
                
                # Stream response
                await websocket.send_json({
                    "type": "chat_start",
                    "message": user_message,
                })
                
                async for token in await ai_service.chat(
                    message=user_message,
                    logs=logs,
                    stream=True
                ):
                    await websocket.send_json({
                        "type": "chat_token",
                        "token": token,
                    })
                
                await websocket.send_json({
                    "type": "chat_end",
                })
                
            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass


@router.websocket("/ws/issues")
async def stream_issues(websocket: WebSocket):
    """Stream detected issues in real-time."""
    await websocket.accept()
    
    last_issue_count = 0
    
    try:
        while True:
            # Check for new issues
            issues = ai_service.get_detected_issues(limit=10)
            current_count = len(ai_service._detected_issues)
            
            if current_count > last_issue_count:
                # Send new issues
                new_issues = issues[:current_count - last_issue_count]
                for issue in new_issues:
                    await websocket.send_json({
                        "type": "new_issue",
                        "data": {
                            "id": issue.id,
                            "container_id": issue.container_id,
                            "container_name": issue.container_name,
                            "severity": issue.severity.value,
                            "title": issue.title,
                            "description": issue.description,
                            "log_excerpt": issue.log_excerpt,
                            "detected_at": issue.detected_at.isoformat(),
                            "suggestion": issue.suggestion,
                        }
                    })
                last_issue_count = current_count
            
            # Wait before checking again
            await asyncio.sleep(2)
            
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
