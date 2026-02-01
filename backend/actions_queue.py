"""In-memory actions queue for agent communication.

This module manages pending actions that agents poll for and execute.
Actions are stored in memory (not persisted) for simplicity.
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class ActionStatus(str, Enum):
    """Action status enum."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class ActionType(str, Enum):
    """Action type enum."""
    CONTAINER_ACTION = "container_action"
    EXEC = "exec"
    GET_LOGS = "get_logs"
    GET_ENV = "get_env"


class Action(BaseModel):
    """Action model."""
    id: str
    agent_id: str
    type: ActionType
    payload: Dict[str, Any]
    status: ActionStatus = ActionStatus.PENDING
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[str] = None
    success: Optional[bool] = None


class AgentInfo(BaseModel):
    """Agent information from heartbeat."""
    agent_id: str
    last_seen: datetime
    status: str = "healthy"


class ActionsQueue:
    """In-memory queue for agent actions."""

    def __init__(self, action_timeout_seconds: int = 60):
        self._actions: Dict[str, Action] = {}
        self._agents: Dict[str, AgentInfo] = {}
        self._lock = asyncio.Lock()
        self._action_timeout = timedelta(seconds=action_timeout_seconds)
        self._waiters: Dict[str, asyncio.Event] = {}

    async def create_action(
        self,
        agent_id: str,
        action_type: ActionType,
        payload: Dict[str, Any],
    ) -> Action:
        """Create a new action for an agent."""
        async with self._lock:
            action = Action(
                id=str(uuid.uuid4()),
                agent_id=agent_id,
                type=action_type,
                payload=payload,
                status=ActionStatus.PENDING,
                created_at=datetime.utcnow(),
            )
            self._actions[action.id] = action
            self._waiters[action.id] = asyncio.Event()

            logger.info(
                "Created action",
                action_id=action.id,
                agent_id=agent_id,
                type=action_type,
            )

            return action

    async def get_pending_actions(self, agent_id: str) -> List[Action]:
        """Get pending actions for an agent and mark them as in_progress."""
        async with self._lock:
            pending = []
            now = datetime.utcnow()

            for action in list(self._actions.values()):
                if action.agent_id != agent_id:
                    continue

                # Check for expired actions
                if action.status == ActionStatus.PENDING:
                    if now - action.created_at > self._action_timeout:
                        action.status = ActionStatus.EXPIRED
                        continue

                    # Mark as in_progress and return
                    action.status = ActionStatus.IN_PROGRESS
                    action.started_at = now
                    pending.append(action)

                # Also check in_progress actions for timeout
                elif action.status == ActionStatus.IN_PROGRESS:
                    if action.started_at and now - action.started_at > self._action_timeout:
                        action.status = ActionStatus.EXPIRED

            return pending

    async def complete_action(
        self,
        action_id: str,
        success: bool,
        output: str,
    ) -> Optional[Action]:
        """Mark an action as completed."""
        async with self._lock:
            action = self._actions.get(action_id)
            if not action:
                logger.warning("Action not found", action_id=action_id)
                return None

            action.status = ActionStatus.COMPLETED if success else ActionStatus.FAILED
            action.completed_at = datetime.utcnow()
            action.success = success
            action.result = output

            # Notify waiters
            if action_id in self._waiters:
                self._waiters[action_id].set()

            logger.info(
                "Action completed",
                action_id=action_id,
                success=success,
            )

            return action

    async def get_action(self, action_id: str) -> Optional[Action]:
        """Get an action by ID."""
        return self._actions.get(action_id)

    async def wait_for_action(
        self,
        action_id: str,
        timeout: float = 30.0,
    ) -> Optional[Action]:
        """Wait for an action to complete."""
        if action_id not in self._waiters:
            return None

        try:
            await asyncio.wait_for(
                self._waiters[action_id].wait(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            pass

        return self._actions.get(action_id)

    async def update_agent_heartbeat(self, agent_id: str, status: str = "healthy"):
        """Update agent heartbeat."""
        async with self._lock:
            self._agents[agent_id] = AgentInfo(
                agent_id=agent_id,
                last_seen=datetime.utcnow(),
                status=status,
            )

    async def get_agents(self) -> List[AgentInfo]:
        """Get all known agents."""
        return list(self._agents.values())

    async def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Get agent info."""
        return self._agents.get(agent_id)

    async def is_agent_online(self, agent_id: str, timeout_seconds: int = 30) -> bool:
        """Check if an agent is online (recent heartbeat)."""
        agent = self._agents.get(agent_id)
        if not agent:
            return False

        return datetime.utcnow() - agent.last_seen < timedelta(seconds=timeout_seconds)

    async def cleanup_old_actions(self, max_age_seconds: int = 300):
        """Remove old completed/failed/expired actions."""
        async with self._lock:
            now = datetime.utcnow()
            max_age = timedelta(seconds=max_age_seconds)

            to_remove = []
            for action_id, action in self._actions.items():
                if action.status in [ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.EXPIRED]:
                    age = now - (action.completed_at or action.created_at)
                    if age > max_age:
                        to_remove.append(action_id)

            for action_id in to_remove:
                del self._actions[action_id]
                if action_id in self._waiters:
                    del self._waiters[action_id]

            if to_remove:
                logger.debug("Cleaned up old actions", count=len(to_remove))


# Global queue instance
actions_queue = ActionsQueue()
