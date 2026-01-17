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
    
    # Hosts (loaded from config file)
    hosts: List[HostConfig] = []
    
    class Config:
        env_prefix = "LOGSCRAWLER_"
        env_nested_delimiter = "__"


def load_config(config_path: str = "config.yaml") -> Settings:
    """Load configuration from YAML file and environment variables."""
    settings = Settings()
    
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file, "r") as f:
            yaml_config = yaml.safe_load(f)
            
        if yaml_config:
            if "hosts" in yaml_config:
                settings.hosts = [HostConfig(**h) for h in yaml_config["hosts"]]
            if "opensearch" in yaml_config:
                settings.opensearch = OpenSearchConfig(**yaml_config["opensearch"])
            if "collector" in yaml_config:
                settings.collector = CollectorConfig(**yaml_config["collector"])
    
    # Override with environment variables
    opensearch_hosts_env = os.environ.get("LOGSCRAWLER_OPENSEARCH__HOSTS")
    if opensearch_hosts_env:
        try:
            hosts_list = json.loads(opensearch_hosts_env)
            if isinstance(hosts_list, list):
                settings.opensearch.hosts = hosts_list
        except json.JSONDecodeError:
            # If it's not JSON, treat as single host
            settings.opensearch.hosts = [opensearch_hosts_env]
                
    return settings


# Global settings instance
settings = load_config()
