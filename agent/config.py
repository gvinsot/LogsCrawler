"""Agent configuration."""

import json
import os
from typing import List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings


class OpenSearchConfig(BaseModel):
    """OpenSearch configuration for direct writes."""
    hosts: List[str] = ["http://localhost:9200"]
    index_prefix: str = "logscrawler"
    username: Optional[str] = None
    password: Optional[str] = None


class AgentSettings(BaseSettings):
    """Agent settings loaded from environment variables."""

    # Agent identification
    agent_id: str = "agent-default"

    # Backend URL for polling actions
    backend_url: str = "http://localhost:8000"

    # OpenSearch for direct data writes
    opensearch: OpenSearchConfig = OpenSearchConfig()

    # Docker connection
    docker_url: str = "unix:///var/run/docker.sock"

    # Collection intervals (seconds)
    log_interval: int = 30
    metrics_interval: int = 15
    action_poll_interval: int = 2

    # Collection settings
    log_lines_per_fetch: int = 500

    class Config:
        env_prefix = "AGENT_"
        env_nested_delimiter = "__"


def load_agent_config() -> AgentSettings:
    """Load agent configuration from environment variables.

    Environment variables:
    - AGENT_AGENT_ID: Unique agent identifier (hostname recommended)
    - AGENT_BACKEND_URL: URL of the LogsCrawler backend
    - AGENT_OPENSEARCH__HOSTS: JSON array of OpenSearch URLs
    - AGENT_OPENSEARCH__USERNAME: OpenSearch username
    - AGENT_OPENSEARCH__PASSWORD: OpenSearch password
    - AGENT_DOCKER_URL: Docker socket or TCP URL
    - AGENT_LOG_INTERVAL: Log collection interval in seconds
    - AGENT_METRICS_INTERVAL: Metrics collection interval in seconds
    - AGENT_ACTION_POLL_INTERVAL: Action polling interval in seconds
    """
    settings = AgentSettings()

    # Agent ID
    if os.environ.get("AGENT_AGENT_ID"):
        settings.agent_id = os.environ["AGENT_AGENT_ID"]

    # Backend URL
    if os.environ.get("AGENT_BACKEND_URL"):
        settings.backend_url = os.environ["AGENT_BACKEND_URL"]

    # Docker URL
    if os.environ.get("AGENT_DOCKER_URL"):
        settings.docker_url = os.environ["AGENT_DOCKER_URL"]

    # OpenSearch
    opensearch_hosts = os.environ.get("AGENT_OPENSEARCH__HOSTS")
    if opensearch_hosts:
        try:
            hosts_list = json.loads(opensearch_hosts)
            if isinstance(hosts_list, list):
                settings.opensearch.hosts = hosts_list
        except json.JSONDecodeError:
            settings.opensearch.hosts = [opensearch_hosts]

    if os.environ.get("AGENT_OPENSEARCH__INDEX_PREFIX"):
        settings.opensearch.index_prefix = os.environ["AGENT_OPENSEARCH__INDEX_PREFIX"]
    if os.environ.get("AGENT_OPENSEARCH__USERNAME"):
        settings.opensearch.username = os.environ["AGENT_OPENSEARCH__USERNAME"]
    if os.environ.get("AGENT_OPENSEARCH__PASSWORD"):
        settings.opensearch.password = os.environ["AGENT_OPENSEARCH__PASSWORD"]

    # Intervals
    if os.environ.get("AGENT_LOG_INTERVAL"):
        settings.log_interval = int(os.environ["AGENT_LOG_INTERVAL"])
    if os.environ.get("AGENT_METRICS_INTERVAL"):
        settings.metrics_interval = int(os.environ["AGENT_METRICS_INTERVAL"])
    if os.environ.get("AGENT_ACTION_POLL_INTERVAL"):
        settings.action_poll_interval = int(os.environ["AGENT_ACTION_POLL_INTERVAL"])

    return settings
