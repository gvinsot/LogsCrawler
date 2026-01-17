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
- SSH key-based access to target Linux hosts
- Docker installed on target hosts

### 1. Clone and Configure

```bash
git clone https://github.com/yourusername/logscrawler.git
cd logscrawler
```

Edit `config.yaml` to add your hosts:

```yaml
hosts:
  - name: "production-1"
    hostname: "192.168.1.10"
    port: 22
    username: "root"
    # ssh_key_path: "~/.ssh/id_rsa"  # Optional

  - name: "production-2"
    hostname: "192.168.1.11"
    port: 22
    username: "deploy"
    ssh_key_path: "~/.ssh/deploy_key"

opensearch:
  hosts:
    - "http://localhost:9200"
  index_prefix: "logscrawler"

collector:
  log_interval_seconds: 30      # How often to fetch logs
  metrics_interval_seconds: 15  # How often to collect metrics
  log_lines_per_fetch: 500      # Max lines per container per cycle
  retention_days: 7             # Data retention period
```

### 2. Start with Docker Compose

```bash
# Production
docker-compose up -d

# Development (with hot reload)
docker-compose -f docker-compose.dev.yml up -d
```

### 3. Access the Dashboard

Open http://localhost:8000 in your browser.

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
│   ├── config.py           # Configuration management
│   ├── main.py             # Application entry point
│   ├── models.py           # Pydantic data models
│   ├── opensearch_client.py # OpenSearch operations
│   └── ssh_client.py       # SSH/Docker operations
├── frontend/
│   ├── index.html          # Main HTML page
│   └── static/
│       ├── css/
│       │   └── style.css   # Styles (Deep Ocean theme)
│       └── js/
│           └── app.js      # Frontend JavaScript
├── config.yaml             # Configuration file
├── docker-compose.yml      # Production deployment
├── docker-compose.dev.yml  # Development deployment
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

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LOGSCRAWLER_DEBUG` | Enable debug mode | `false` |
| `LOGSCRAWLER_HOST` | Server bind host | `0.0.0.0` |
| `LOGSCRAWLER_PORT` | Server bind port | `8000` |
| `LOGSCRAWLER_CONFIG_PATH` | Config file path | `config.yaml` |

### OpenSearch with Authentication

```yaml
opensearch:
  hosts:
    - "https://opensearch.example.com:9200"
  index_prefix: "logscrawler"
  username: "admin"
  password: "your-secure-password"
```

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