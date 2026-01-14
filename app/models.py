"""Pydantic models for the application."""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class IssueSeverity(str, Enum):
    """Severity levels for detected issues."""
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class SystemStatus(str, Enum):
    """Connection status for remote systems."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    UNKNOWN = "unknown"


class RemoteSystem(BaseModel):
    """Remote system configuration for SSH-based Docker monitoring."""
    id: str
    name: str
    hostname: str  # IP or hostname
    username: str
    port: int = Field(default=22, description="SSH port")
    ssh_key: Optional[str] = Field(default=None, description="Private SSH key for authentication")
    status: SystemStatus = SystemStatus.UNKNOWN
    last_connected: Optional[datetime] = None
    last_error: Optional[str] = None
    container_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)


class RemoteSystemCreate(BaseModel):
    """Model for creating a new remote system."""
    name: str
    hostname: str
    username: str
    port: int = 22
    ssh_key: Optional[str] = None


class RemoteSystemUpdate(BaseModel):
    """Model for updating a remote system."""
    name: Optional[str] = None
    hostname: Optional[str] = None
    username: Optional[str] = None
    port: Optional[int] = None
    ssh_key: Optional[str] = None


class ContainerInfo(BaseModel):
    """Container information model."""
    id: str
    name: str
    image: str
    status: str
    state: str
    created: str
    ports: List[str] = []
    labels: dict = {}
    system_id: Optional[str] = Field(default="local", description="System ID (local or remote system ID)")
    system_name: Optional[str] = Field(default="Local", description="System display name")


class ContainerLog(BaseModel):
    """Container log entry model."""
    container_id: str
    container_name: str
    timestamp: Optional[datetime] = None
    message: str
    stream: str = "stdout"  # stdout or stderr
    system_id: str = Field(default="local", description="System ID (local or remote system ID)")
    system_name: str = Field(default="Local", description="System display name")


class DetectedIssue(BaseModel):
    """Model for AI-detected issues in logs."""
    id: str
    container_id: str
    container_name: str
    severity: IssueSeverity
    title: str
    description: str
    log_excerpt: str
    detected_at: datetime = Field(default_factory=datetime.now)
    resolved: bool = False
    suggestion: Optional[str] = None
    occurrence_count: int = Field(default=1, description="Number of times this issue has been detected")
    system_id: str = Field(default="local", description="System ID (local or remote system ID)")
    system_name: str = Field(default="Local", description="System display name")


class AIAnalysisRequest(BaseModel):
    """Request model for AI analysis."""
    query: str
    container_id: Optional[str] = None
    include_all_containers: bool = False
    log_lines: int = 100


class AIAnalysisResponse(BaseModel):
    """Response model for AI analysis."""
    query: str
    response: str
    containers_analyzed: List[str]
    issues_found: List[DetectedIssue] = []
    analyzed_at: datetime = Field(default_factory=datetime.now)


class ChatMessage(BaseModel):
    """Chat message model."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)


class LogStreamConfig(BaseModel):
    """Configuration for log streaming."""
    container_id: str
    follow: bool = True
    tail: int = 100
    timestamps: bool = True
