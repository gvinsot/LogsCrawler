"""Configuration management for LogsCrawler.

All configuration is done via environment variables. No config file required!

Environment variables:
- LOGSCRAWLER_HOSTS: JSON array of host configs
- LOGSCRAWLER_OPENSEARCH__HOSTS: JSON array of OpenSearch URLs
- LOGSCRAWLER_OPENSEARCH__INDEX_PREFIX: Index prefix string
- LOGSCRAWLER_COLLECTOR__LOG_INTERVAL_SECONDS: Log collection interval
- LOGSCRAWLER_COLLECTOR__METRICS_INTERVAL_SECONDS: Metrics collection interval
- LOGSCRAWLER_AI__MODEL: AI model name
"""

import json
import os
from typing import List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings


class HostConfig(BaseModel):
    """Configuration for a single host."""
    name: str
    hostname: str = "localhost"
    port: int = 22
    username: str = "root"
    ssh_key_path: Optional[str] = None
    
    # Connection mode (choose one):
    # - "ssh": Connect via SSH (default for remote hosts)
    # - "docker": Connect via Docker API socket or TCP
    # - "local": Run commands locally (for development without Docker)
    mode: str = "ssh"
    
    # Docker API URL (only used when mode="docker")
    # Examples:
    # - "unix:///var/run/docker.sock" (local socket, default)
    # - "tcp://192.168.1.10:2375" (remote TCP)
    # - "tcp://host.docker.internal:2375" (host from container)
    docker_url: Optional[str] = None
    
    # Swarm manager flag: set to true if this host is a Docker Swarm manager
    # Used for stack operations and grouping
    swarm_manager: bool = False

    # Swarm routing: when True, commands for containers on other Swarm nodes
    # will be routed through this manager instead of direct SSH connections.
    # This eliminates the need for SSH access to worker nodes.
    # Only applicable when swarm_manager=True and mode="docker" or "ssh"
    swarm_routing: bool = False

    # Swarm auto-discovery: when True, automatically discovers all nodes in the
    # Swarm cluster and monitors their containers. No need to configure worker
    # nodes manually - they are discovered from the manager.
    # Requires swarm_manager=True and mode="docker"
    swarm_autodiscover: bool = False
    
    
class OpenSearchConfig(BaseModel):
    """OpenSearch configuration."""
    hosts: List[str] = ["http://localhost:9200"]
    index_prefix: str = "logscrawler"
    username: Optional[str] = None
    password: Optional[str] = None


class CollectorConfig(BaseModel):
    """Collector configuration."""
    log_interval_seconds: int = 30
    metrics_interval_seconds: int = 15
    log_lines_per_fetch: int = 500
    retention_days: int = 7


class AIConfig(BaseModel):
    """AI/Ollama configuration."""
    model: str = "qwen2.5:1.5b"


class GitHubConfig(BaseModel):
    """GitHub integration configuration."""
    token: Optional[str] = None
    username: Optional[str] = None
    # Path where repos are cloned on the host
    repos_path: str = "~/repos"
    # Path to deployment scripts
    scripts_path: str = "~/PrivateNetwork"
    # SSH configuration for executing commands on the host
    # Required when LogsCrawler runs in a container and needs to run git/build on the host
    ssh_host: Optional[str] = None
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_path: Optional[str] = None


class Settings(BaseSettings):
    """Application settings."""
    app_name: str = "LogsCrawler"
    debug: bool = False

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # OpenSearch
    opensearch: OpenSearchConfig = OpenSearchConfig()

    # Collector
    collector: CollectorConfig = CollectorConfig()

    # AI
    ai: AIConfig = AIConfig()

    # GitHub
    github: GitHubConfig = GitHubConfig()

    # Hosts (configured via LOGSCRAWLER_HOSTS env var)
    hosts: List[HostConfig] = []

    class Config:
        env_prefix = "LOGSCRAWLER_"
        env_nested_delimiter = "__"


def load_config() -> Settings:
    """Load configuration from environment variables.

    All configuration is done via environment variables prefixed with LOGSCRAWLER_.

    Required:
    - LOGSCRAWLER_HOSTS: JSON array of host configs

    Optional:
    - LOGSCRAWLER_OPENSEARCH__HOSTS: JSON array of OpenSearch URLs
    - LOGSCRAWLER_OPENSEARCH__INDEX_PREFIX: Index prefix
    - LOGSCRAWLER_COLLECTOR__LOG_INTERVAL_SECONDS: integer
    - LOGSCRAWLER_COLLECTOR__METRICS_INTERVAL_SECONDS: integer
    - LOGSCRAWLER_AI__MODEL: string

    Example LOGSCRAWLER_HOSTS:
    [{"name": "local", "mode": "docker", "docker_url": "unix:///var/run/docker.sock"}]
    """
    settings = Settings()

    # Load hosts from environment variable (JSON array)
    hosts_env = os.environ.get("LOGSCRAWLER_HOSTS")
    if hosts_env:
        try:
            hosts_list = json.loads(hosts_env)
            if isinstance(hosts_list, list):
                settings.hosts = [HostConfig(**h) for h in hosts_list]
        except json.JSONDecodeError as e:
            print(f"Warning: Failed to parse LOGSCRAWLER_HOSTS: {e}")

    # OpenSearch
    opensearch_hosts_env = os.environ.get("LOGSCRAWLER_OPENSEARCH__HOSTS")
    if opensearch_hosts_env:
        try:
            hosts_list = json.loads(opensearch_hosts_env)
            if isinstance(hosts_list, list):
                settings.opensearch.hosts = hosts_list
        except json.JSONDecodeError:
            settings.opensearch.hosts = [opensearch_hosts_env]

    if os.environ.get("LOGSCRAWLER_OPENSEARCH__INDEX_PREFIX"):
        settings.opensearch.index_prefix = os.environ["LOGSCRAWLER_OPENSEARCH__INDEX_PREFIX"]
    if os.environ.get("LOGSCRAWLER_OPENSEARCH__USERNAME"):
        settings.opensearch.username = os.environ["LOGSCRAWLER_OPENSEARCH__USERNAME"]
    if os.environ.get("LOGSCRAWLER_OPENSEARCH__PASSWORD"):
        settings.opensearch.password = os.environ["LOGSCRAWLER_OPENSEARCH__PASSWORD"]

    # Collector
    if os.environ.get("LOGSCRAWLER_COLLECTOR__LOG_INTERVAL_SECONDS"):
        settings.collector.log_interval_seconds = int(os.environ["LOGSCRAWLER_COLLECTOR__LOG_INTERVAL_SECONDS"])
    if os.environ.get("LOGSCRAWLER_COLLECTOR__METRICS_INTERVAL_SECONDS"):
        settings.collector.metrics_interval_seconds = int(os.environ["LOGSCRAWLER_COLLECTOR__METRICS_INTERVAL_SECONDS"])
    if os.environ.get("LOGSCRAWLER_COLLECTOR__LOG_LINES_PER_FETCH"):
        settings.collector.log_lines_per_fetch = int(os.environ["LOGSCRAWLER_COLLECTOR__LOG_LINES_PER_FETCH"])
    if os.environ.get("LOGSCRAWLER_COLLECTOR__RETENTION_DAYS"):
        settings.collector.retention_days = int(os.environ["LOGSCRAWLER_COLLECTOR__RETENTION_DAYS"])

    # AI
    if os.environ.get("LOGSCRAWLER_AI__MODEL"):
        settings.ai.model = os.environ["LOGSCRAWLER_AI__MODEL"]

    # GitHub
    if os.environ.get("LOGSCRAWLER_GITHUB__TOKEN"):
        settings.github.token = os.environ["LOGSCRAWLER_GITHUB__TOKEN"]
    if os.environ.get("LOGSCRAWLER_GITHUB__USERNAME"):
        settings.github.username = os.environ["LOGSCRAWLER_GITHUB__USERNAME"]
    if os.environ.get("LOGSCRAWLER_GITHUB__REPOS_PATH"):
        settings.github.repos_path = os.environ["LOGSCRAWLER_GITHUB__REPOS_PATH"]
    if os.environ.get("LOGSCRAWLER_GITHUB__SCRIPTS_PATH"):
        settings.github.scripts_path = os.environ["LOGSCRAWLER_GITHUB__SCRIPTS_PATH"]
    if os.environ.get("LOGSCRAWLER_GITHUB__SSH_HOST"):
        settings.github.ssh_host = os.environ["LOGSCRAWLER_GITHUB__SSH_HOST"]
    if os.environ.get("LOGSCRAWLER_GITHUB__SSH_USER"):
        settings.github.ssh_user = os.environ["LOGSCRAWLER_GITHUB__SSH_USER"]
    if os.environ.get("LOGSCRAWLER_GITHUB__SSH_PORT"):
        settings.github.ssh_port = int(os.environ["LOGSCRAWLER_GITHUB__SSH_PORT"])
    if os.environ.get("LOGSCRAWLER_GITHUB__SSH_KEY_PATH"):
        settings.github.ssh_key_path = os.environ["LOGSCRAWLER_GITHUB__SSH_KEY_PATH"]

    return settings


# Global settings instance
settings = load_config()
