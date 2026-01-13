# Detected Issues Flow

This document describes the complete flow for detecting, processing, and displaying issues in LogsCrawler.

## Overview

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Frontend  │────▶│  API Route  │────▶│  AI Service │────▶│   Storage   │
│  (app.js)   │     │ (routes.py) │     │(ai_service) │     │  (memory)   │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
       │                                       │
       │                                       ▼
       │                              ┌─────────────────┐
       │                              │  Ollama LLM or  │
       │                              │ Pattern Matching│
       │                              └─────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Detected Issues Panel                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                              │
│  │  Issue 1 │  │  Issue 2 │  │  Issue 3 │  ...                         │
│  │  ×3      │  │  ×1      │  │  ×5      │                              │
│  └──────────┘  └──────────┘  └──────────┘                              │
└─────────────────────────────────────────────────────────────────────────┘
```

## Trigger Points

### 1. Manual Scan (Primary)
- **User Action**: Click "Scan Logs" button on Dashboard
- **Frontend**: `scanForIssues()` in `app/static/app.js`
- **API Endpoint**: `POST /api/issues/scan`

### 2. Automatic Analysis
- **Trigger**: When using AI analysis features
- **API Endpoint**: `POST /api/ai/analyze`

## Detailed Flow

### Step 1: Frontend Initiates Scan

```javascript
// app/static/app.js
async function scanForIssues() {
    const result = await api('/issues/scan?log_lines=200', { method: 'POST' });
    await loadIssues();
}
```

### Step 2: API Route Handles Request

```python
# app/api/routes.py
@router.post("/issues/scan")
async def scan_for_issues(container_id: Optional[str], log_lines: int):
    # Fetch logs from Docker
    logs = docker_service.get_all_logs(tail=log_lines)
    
    # Detect issues using AI
    issues = await ai_service.quick_issue_check(logs)
    
    return {"issues_found": len(issues), "issues": issues}
```

### Step 3: AI Service Processes Logs (Incremental Analysis)

The AI service uses **incremental analysis** - each log is only analyzed once:

```python
# app/services/ai_service.py
async def quick_issue_check(logs: List[ContainerLog]) -> List[DetectedIssue]:
    # 1. If initial scan hasn't been done, do it now
    if not self._initial_scan_done:
        return await self.initial_scan(logs)
    
    # 2. Filter to only NEW logs (not previously analyzed)
    new_logs = self._filter_new_logs(logs)  # Uses hash-based tracking
    
    if not new_logs:
        return []  # No new logs to analyze
    
    # 3. Mark these logs as analyzed
    self._mark_logs_as_analyzed(new_logs)
    
    # 4. Run issue detection on new logs only
    return await self._analyze_logs_for_issues(new_logs)
```

**Incremental Tracking:**
- Each log is hashed (container + timestamp + message)
- Hashes are stored in memory (up to 100k)
- Only logs with new hashes are analyzed
- Prevents duplicate analysis and saves AI resources

### Step 4: Log Filtering

Certain log messages are ignored to prevent false positives:

```python
def _should_ignore_log(message: str) -> bool:
    ignore_patterns = [
        "/api/ai/chat?message=",  # AI chat API calls
        "POST /api/ai/",          # AI API endpoints
        "GET /api/ai/",
        "HTTP Request: POST http://ollama",  # Ollama requests
    ]
    return any(pattern in message for pattern in ignore_patterns)
```

### Step 5: AI-Powered Detection

The AI (Ollama) analyzes logs and returns issues in JSON format:

```json
[
  {
    "container": "myapp",
    "severity": "error",
    "title": "Database connection failed",
    "description": "The application failed to connect to PostgreSQL",
    "log_excerpt": "ERROR: connection refused to postgres:5432",
    "suggestion": "Check if PostgreSQL container is running"
  }
]
```

### Step 6: Fallback Pattern Detection

If AI detection fails, pattern-based detection is used:

```python
def _fallback_issue_check(logs: List[ContainerLog]) -> List[DetectedIssue]:
    error_patterns = [
        ("error", IssueSeverity.ERROR),
        ("exception", IssueSeverity.ERROR),
        ("failed", IssueSeverity.ERROR),
        ("critical", IssueSeverity.CRITICAL),
        ("fatal", IssueSeverity.CRITICAL),
        ("panic", IssueSeverity.CRITICAL),
        ("timeout", IssueSeverity.WARNING),
        ("refused", IssueSeverity.ERROR),
        ("out of memory", IssueSeverity.CRITICAL),
    ]
    # Match patterns in log messages
```

### Step 7: Issue Deduplication & Counting

Similar issues are aggregated by signature (container + title):

```python
def _get_issue_signature(issue: DetectedIssue) -> str:
    return f"{issue.container_name}:{issue.title.lower().strip()}"

def _add_or_increment_issue(issue: DetectedIssue):
    signature = self._get_issue_signature(issue)
    
    for existing in self._detected_issues:
        if self._get_issue_signature(existing) == signature:
            existing.occurrence_count += 1  # Increment count
            existing.detected_at = issue.detected_at  # Update timestamp
            return
    
    self._detected_issues.append(issue)  # Add new issue
```

### Step 8: Frontend Retrieves Issues

```javascript
// app/static/app.js
async function loadIssues() {
    const issues = await api('/issues?limit=20&min_occurrences=1');
    state.issues = issues;
    renderIssuesList(issues);
}
```

### Step 9: API Returns Filtered Issues

```python
# app/api/routes.py
@router.get("/issues")
async def get_issues(
    limit: int = 50,
    container_id: Optional[str] = None,
    severity: Optional[IssueSeverity] = None,
    min_occurrences: int = 1
):
    return ai_service.get_detected_issues(
        limit=limit,
        container_id=container_id,
        severity=severity,
        min_occurrences=min_occurrences
    )
```

### Step 10: Frontend Renders Issues

```javascript
function renderIssuesList(issues) {
    container.innerHTML = issues.map(issue => `
        <div class="issue-card ${issue.severity}">
            <span class="issue-badge occurrence">×${issue.occurrence_count}</span>
            <span class="issue-badge ${issue.severity}">${issue.severity}</span>
            <span class="issue-title">${issue.title}</span>
            ...
        </div>
    `).join('');
}
```

## Data Model

```python
class DetectedIssue(BaseModel):
    id: str                          # Unique identifier
    container_id: str                # Docker container ID
    container_name: str              # Container name
    severity: IssueSeverity          # critical/error/warning/info
    title: str                       # Brief issue description
    description: str                 # Detailed explanation
    log_excerpt: str                 # The actual log line(s)
    detected_at: datetime            # When first/last detected
    resolved: bool                   # Resolution status
    suggestion: Optional[str]        # Recommended action
    occurrence_count: int            # How many times detected
```

## Severity Levels

| Level | Color | Description |
|-------|-------|-------------|
| `critical` | Red | Service down, data loss risk |
| `error` | Red | Failures, exceptions |
| `warning` | Orange | Potential problems |
| `info` | Cyan | Informational issues |

## User Actions on Issues

1. **View Logs**: Opens modal with 50 lines before/after the issue
   - Uses dedicated search endpoint: `GET /api/logs/{container_id}/search`
   - Searches through up to 10,000 log lines to find the exact match
   - Returns context (50 lines before, 50 lines after)
2. **Analyze with AI**: Sends issue context to AI for detailed analysis
   - Fetches 1 line before and 5 lines after the issue
   - Marks the issue line with `>>>` in the prompt
3. **Resolve**: Marks issue as resolved (hidden from default view)
4. **Filter**: Filter by severity level

## Log Search Endpoint

```
GET /api/logs/{container_id}/search?search=<text>&context_before=50&context_after=50&max_logs=10000
```

Response:
```json
{
  "found": true,
  "search": "ERROR: connection refused",
  "logs": [...],
  "match_index": 42,
  "total_searched": 5000,
  "absolute_match_index": 3542
}
```

## Storage

Issues and analysis state are stored **in-memory** in the `AIService` instance:

```python
class AIService:
    def __init__(self):
        self._detected_issues: List[DetectedIssue] = []
        
        # Incremental analysis tracking
        self._last_analyzed_timestamp: Optional[datetime] = None
        self._analyzed_log_hashes: set = set()  # Up to 100k hashes
        self._initial_scan_done: bool = False
        self._total_logs_analyzed: int = 0
```

> **Note**: Issues and analysis state are lost when the application restarts. For persistence, RAG/MongoDB storage can be enabled.

## Analysis Status API

**Get status:**
```
GET /api/issues/status
```

Response:
```json
{
  "initial_scan_done": true,
  "total_logs_analyzed": 1523,
  "last_analyzed_timestamp": "2024-01-15T10:30:45.123456",
  "tracked_log_hashes": 1523,
  "detected_issues_count": 5
}
```

**Reset analysis (re-analyze all logs):**
```
POST /api/issues/reset
```

## Sequence Diagram

```
User          Frontend       API           AI Service      Docker        Ollama
 │               │            │                │              │            │
 │──Click Scan──▶│            │                │              │            │
 │               │──POST /issues/scan─────────▶│              │            │
 │               │            │                │──get_logs───▶│            │
 │               │            │                │◀──logs───────│            │
 │               │            │                │──prompt──────────────────▶│
 │               │            │                │◀──JSON issues─────────────│
 │               │            │                │──deduplicate─│            │
 │               │            │◀──issues───────│              │            │
 │               │◀──render───│                │              │            │
 │◀──display─────│            │                │              │            │
```

## Configuration

Relevant settings in `app/config.py`:

```python
ollama_host: str = "http://ollama:11434"
ollama_model: str = "llama3.2"
max_log_context: int = 8000  # Max chars for AI context
```
