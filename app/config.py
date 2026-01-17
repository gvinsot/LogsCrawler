"""Application configuration settings."""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Application
    app_name: str = "LogsCrawler"
    app_version: str = "1.0.0"
    debug: bool = False
    
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    
    # Docker
    docker_socket: str = "unix:///var/run/docker.sock"  # Linux/Mac
    docker_host: Optional[str] = None  # For Windows: tcp://localhost:2375
    
    # Ollama LLM Settings
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"  # Default model for chat/analysis
    
    # Embedding Settings (for RAG)
    embedding_model: str = "nomic-embed-text"  # Model for generating embeddings
    
    # MongoDB Settings
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "logscrawler"
    
    # OpenSearch Settings
    opensearch_host: str = "http://localhost:9200"
    opensearch_index: str = "logscrawler-logs"
    opensearch_enabled: bool = True
    
    # Log Settings
    log_tail_lines: int = 500  # Default number of lines to fetch
    log_stream_interval: float = 1.0  # Seconds between log checks
    log_retention_days: int = 30  # Days to keep logs in MongoDB
    
    # Data Storage
    data_dir: str = "/app/data"  # Directory for vector index and other data
    
    # AI Analysis Settings
    analysis_interval: int = 60  # Seconds between automatic analysis
    max_log_context: int = 4000  # Max characters to send to LLM
    
    # RAG Settings
    rag_enabled: bool = True  # Enable RAG-based analysis
    rag_search_limit: int = 20  # Number of similar logs to retrieve
    rag_context_window: int = 10000  # Max context for RAG queries
    
    class Config:
        env_file = ".env"
        env_prefix = "LOGSCRAWLER_"


settings = Settings()
