# LogsCrawler

A professional Docker container log aggregation and monitoring solution. Collects logs and metrics from multiple Linux hosts running Docker containers via SSH, stores them in OpenSearch, and provides a modern web dashboard for analysis.

![Dashboard Preview](docs/dashboard-preview.png)

## Features

### Log Management
- **Centralized Log Collection**: Automatically collects logs from all Docker containers across multiple hosts
- **Incremental Fetching**: Only fetches new logs since last collection (optimized bandwidth)
- **Full-Text Search**: Powerful search capabilities using OpenSearch query syntax
- **Log Level Detection**: Automatically detects ERROR, WARN, INFO, DEBUG levels
- **HTTP Status Extraction**: Parses HTTP status codes from web server logs

### Metrics & Monitoring
- **Host Metrics**: CPU, Memory, Disk, and GPU (NVIDIA) usage per host
- **Container Metrics**: CPU, Memory, Network I/O, Block I/O per container
- **Time Series Visualization**: Charts showing resource usage over time
- **Error Tracking**: 4xx/5xx HTTP error counts and trends

### Container Management
- **Grouped View**: Containers grouped by host and Docker Compose project
- **Status Filtering**: Filter by running, exited, paused status
- **Container Actions**: Start, Stop, Restart, Pause/Unpause containers
- **Live Logs**: View real-time container logs
- **Resource Stats**: View current CPU/Memory usage

### Dashboard
- **Summary Statistics**: Running containers, hosts, error counts
- **Error Trends**: Visualize error patterns over 24h
- **Resource Usage**: CPU and Memory usage graphs
- **HTTP Status Distribution**: 4xx/5xx breakdown

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        LogsCrawler                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
│  │  Frontend   │    │   FastAPI   │    │     Collector       │  │
│  │  (HTML/JS)  │◄──►│   Backend   │◄──►│  (Async SSH)        │  │
│  └─────────────┘    └──────┬──────┘    └──────────┬──────────┘  │
│                            │                      │             │
│                     ┌──────▼──────┐               │             │
│                     │  OpenSearch │               │             │
│                     │   Storage   │               │             │
│                     └─────────────┘               │             │
└───────────────────────────────────────────────────┼─────────────┘
                                                    │
                    ┌───────────────────────────────┼───────────────┐
                    │                               │               │
              ┌─────▼─────┐                  ┌──────▼────┐   ┌──────▼────┐
              │  Host 1   │                  │  Host 2   │   │  Host N   │
              │  Docker   │                  │  Docker   │   │  Docker   │
              └───────────┘                  └───────────┘   └───────────┘
```

## Quick Start

### Prerequisites

- Docker & Docker Compose
- For SSH mode: SSH key-based access to target Linux hosts
- Docker installed on target hosts

### 1. Clone and Start

```bash
git clone https://github.com/yourusername/logscrawler.git
cd logscrawler

# Start with Docker Compose (monitors local Docker by default)
docker-compose up -d
```

### 2. Access the Dashboard

Open http://localhost:5000 in your browser.

### 3. Check Configuration

```bash
# View current configuration
curl http://localhost:5000/api/config

# Test connectivity to all hosts
curl http://localhost:5000/api/config/test
```

## Configuration

All configuration is done via **environment variables** in `docker-compose.yml`. No config file needed!

### Local Docker Monitoring (Default)

The default configuration monitors containers on the local Docker host:

```yaml
environment:
  - |
    LOGSCRAWLER_HOSTS=[
      {"name": "local-docker", "mode": "docker", "docker_url": "unix:///var/run/docker.sock"}
    ]
```

### Docker Swarm with Auto-Discovery (Recommended)

For Swarm clusters, configure only the manager - workers are discovered automatically:

```yaml
environment:
  - |
    LOGSCRAWLER_HOSTS=[
      {
        "name": "swarm-manager",
        "mode": "docker",
        "docker_url": "unix:///var/run/docker.sock",
        "swarm_manager": true,
        "swarm_routing": true,
        "swarm_autodiscover": true
      }
    ]
```

### SSH Mode (Multiple Remote Hosts)

For monitoring multiple remote hosts via SSH:

```yaml
environment:
  - |
    LOGSCRAWLER_HOSTS=[
      {"name": "local", "mode": "docker"},
      {"name": "server-1", "mode": "ssh", "hostname": "192.168.1.10", "username": "deploy"},
      {"name": "server-2", "mode": "ssh", "hostname": "192.168.1.11", "username": "deploy"}
    ]
volumes:
  - ~/.ssh:/root/.ssh:ro  # Mount SSH keys
```

## Development Setup

### Without Docker

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Start OpenSearch (required)
docker run -d -p 9200:9200 -e "discovery.type=single-node" \
  -e "DISABLE_SECURITY_PLUGIN=true" \
  opensearchproject/opensearch:2.11.1

# Run the application
python -m backend.main
```

### Project Structure

```
logscrawler/
├── backend/
│   ├── __init__.py
│   ├── api.py              # FastAPI REST endpoints
│   ├── collector.py        # Log/metrics collection service
│   ├── config.py           # Configuration management (env vars)
│   ├── main.py             # Application entry point
│   ├── models.py           # Pydantic data models
│   ├── docker_client.py    # Docker API client
│   ├── ssh_client.py       # SSH client for remote hosts
│   ├── host_client.py      # Unified host client interface
│   └── opensearch_client.py # OpenSearch operations
├── frontend/
│   ├── index.html          # Main HTML page
│   └── static/
│       ├── css/
│       │   └── style.css   # Styles (Deep Ocean theme)
│       └── js/
│           └── app.js      # Frontend JavaScript
├── devops/
│   └── docker-compose.swarm.yml  # Docker Swarm deployment
├── docker-compose.yml      # Local/single-host deployment
├── Dockerfile
├── requirements.txt
└── README.md
```

## API Reference

### Dashboard

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dashboard/stats` | GET | Get summary statistics |
| `/api/dashboard/errors-timeseries` | GET | Error count over time |
| `/api/dashboard/http-4xx-timeseries` | GET | HTTP 4xx count over time |
| `/api/dashboard/http-5xx-timeseries` | GET | HTTP 5xx count over time |
| `/api/dashboard/cpu-timeseries` | GET | CPU usage over time |
| `/api/dashboard/memory-timeseries` | GET | Memory usage over time |

### Containers

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/containers` | GET | List all containers |
| `/api/containers/grouped` | GET | List containers grouped by host/project |
| `/api/containers/{host}/{id}/stats` | GET | Get container stats |
| `/api/containers/{host}/{id}/logs` | GET | Get container logs |
| `/api/containers/action` | POST | Execute container action |

### Logs Search

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/logs/search` | POST | Search logs with filters |
| `/api/logs/search` | GET | Search logs (query params) |

#### Search Query Parameters

```json
{
  "query": "error AND timeout",
  "hosts": ["server-1"],
  "containers": ["nginx", "api"],
  "compose_projects": ["webapp"],
  "levels": ["ERROR", "WARN"],
  "http_status_min": 400,
  "http_status_max": 599,
  "start_time": "2024-01-15T00:00:00Z",
  "end_time": "2024-01-16T00:00:00Z",
  "size": 100,
  "from": 0,
  "sort_order": "desc"
}
```

### Hosts

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/hosts` | GET | List configured hosts |
| `/api/health` | GET | Health check |

## Environment Variables Reference

All configuration is done via environment variables prefixed with `LOGSCRAWLER_`.

### Core Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `LOGSCRAWLER_DEBUG` | Enable debug mode | `false` |
| `LOGSCRAWLER_HOSTS` | JSON array of host configs | `[]` |

### OpenSearch Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `LOGSCRAWLER_OPENSEARCH__HOSTS` | JSON array of OpenSearch URLs | `["http://localhost:9200"]` |
| `LOGSCRAWLER_OPENSEARCH__INDEX_PREFIX` | Index prefix | `logscrawler` |
| `LOGSCRAWLER_OPENSEARCH__USERNAME` | Username (optional) | - |
| `LOGSCRAWLER_OPENSEARCH__PASSWORD` | Password (optional) | - |

### Collector Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `LOGSCRAWLER_COLLECTOR__LOG_INTERVAL_SECONDS` | Log collection interval | `30` |
| `LOGSCRAWLER_COLLECTOR__METRICS_INTERVAL_SECONDS` | Metrics collection interval | `15` |
| `LOGSCRAWLER_COLLECTOR__LOG_LINES_PER_FETCH` | Lines per container per fetch | `500` |
| `LOGSCRAWLER_COLLECTOR__RETENTION_DAYS` | Data retention period | `7` |

### AI Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `LOGSCRAWLER_AI__MODEL` | Ollama model name | `llama3.2:latest` |
| `LOGSCRAWLER_OLLAMA_URL` | Ollama API URL | - |

### Host Configuration Options

Each host in `LOGSCRAWLER_HOSTS` supports these fields:

| Field | Description | Required |
|-------|-------------|----------|
| `name` | Display name for the host | Yes |
| `mode` | Connection mode: `docker`, `ssh`, or `local` | Yes |
| `hostname` | IP or hostname (for SSH mode) | For SSH |
| `port` | SSH port | No (default: 22) |
| `username` | SSH username | For SSH |
| `docker_url` | Docker API URL (for docker mode) | No |
| `swarm_manager` | Is this a Swarm manager? | No |
| `swarm_routing` | Route commands through manager | No |
| `swarm_autodiscover` | Auto-discover Swarm nodes | No |

## Troubleshooting

### SSH Connection Issues

1. Ensure SSH key-based authentication is configured:
   ```bash
   ssh-copy-id user@hostname
   ```

2. Test SSH connection manually:
   ```bash
   ssh -i ~/.ssh/your_key user@hostname "docker ps"
   ```

3. Check SSH key permissions:
   ```bash
   chmod 600 ~/.ssh/your_key
   ```

### OpenSearch Connection Issues

1. Verify OpenSearch is running:
   ```bash
   curl http://localhost:9200
   ```

2. Check logs:
   ```bash
   docker-compose logs opensearch
   ```

### No Logs Appearing

1. Check collector logs:
   ```bash
   docker-compose logs logscrawler
   ```

2. Verify containers are running on target hosts:
   ```bash
   ssh user@host "docker ps"
   ```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.