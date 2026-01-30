"""Configuration management for LogsCrawler."""

import json
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings
import yaml


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


class Settings(BaseSettings):
    """Application settings."""
    app_name: str = "LogsCrawler"
    debug: bool = False
    
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    
    # Paths
    config_path: str = "config.yaml"
    
    # OpenSearch
    opensearch: OpenSearchConfig = OpenSearchConfig()
    
    # Collector
    collector: CollectorConfig = CollectorConfig()
    
    # AI
    ai: AIConfig = AIConfig()
    
    # Hosts (loaded from config file)
    hosts: List[HostConfig] = []
    
    class Config:
        env_prefix = "LOGSCRAWLER_"
        env_nested_delimiter = "__"


def load_config(config_path: str = "config.yaml") -> Settings:
    """Load configuration from YAML file and environment variables.

    Priority (highest to lowest):
    1. Environment variables (LOGSCRAWLER_*)
    2. Config file (config.yaml)
    3. Default values

    Environment variable format:
    - LOGSCRAWLER_HOSTS: JSON array of host configs
    - LOGSCRAWLER_OPENSEARCH__HOSTS: JSON array of OpenSearch URLs
    - LOGSCRAWLER_COLLECTOR__LOG_INTERVAL_SECONDS: integer
    - LOGSCRAWLER_AI__MODEL: string
    """
    settings = Settings()

    # Load from config file if it exists (and no LOGSCRAWLER_HOSTS env var)
    hosts_env = os.environ.get("LOGSCRAWLER_HOSTS")
    config_file = Path(config_path)

    if not hosts_env and config_file.exists():
        with open(config_file, "r") as f:
            yaml_config = yaml.safe_load(f)

        if yaml_config:
            if "hosts" in yaml_config:
                settings.hosts = [HostConfig(**h) for h in yaml_config["hosts"]]
            if "opensearch" in yaml_config:
                settings.opensearch = OpenSearchConfig(**yaml_config["opensearch"])
            if "collector" in yaml_config:
                settings.collector = CollectorConfig(**yaml_config["collector"])
            if "ai" in yaml_config:
                settings.ai = AIConfig(**yaml_config["ai"])

    # Override with environment variables
    # Hosts (JSON array)
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

    return settings


# Global settings instance
settings = load_config()
