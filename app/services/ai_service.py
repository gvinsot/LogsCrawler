"""AI Service for log analysis using local LLM (Ollama) with RAG support."""

import httpx
import json
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional, AsyncGenerator, Dict, Any
import uuid

from app.config import settings
from app.models import (
    DetectedIssue, 
    IssueSeverity, 
    AIAnalysisResponse, 
    ContainerLog,
    ChatMessage
)

logger = logging.getLogger(__name__)


class AIService:
    """Service for AI-powered log analysis using Ollama with RAG capabilities."""
    
    def __init__(self):
        """Initialize AI service."""
        self.ollama_url = settings.ollama_host
        self.model = settings.ollama_model
        self._detected_issues: List[DetectedIssue] = []
        self._chat_history: List[ChatMessage] = []
        self._rag_available = False
        
        # Incremental analysis tracking
        self._last_analyzed_timestamp: Optional[datetime] = None
        self._analyzed_log_hashes: set = set()  # Track analyzed logs by hash
        self._initial_scan_done: bool = False
        self._total_logs_analyzed: int = 0
        
    async def initialize_rag(self) -> bool:
        """Initialize RAG components (call after storage/vector services are ready)."""
        if not settings.rag_enabled:
            logger.info("RAG is disabled in settings")
            return False
        
        try:
            # Import here to avoid circular imports
            from app.services.storage_service import storage_service
            from app.services.vector_service import vector_service
            
            self._storage = storage_service
            self._vector = vector_service
            self._rag_available = True
            logger.info("RAG components initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize RAG: {e}")
            self._rag_available = False
            return False
    
    @property
    def rag_enabled(self) -> bool:
        return self._rag_available and settings.rag_enabled
        
    async def check_connection(self) -> bool:
        """Check if Ollama is running and accessible."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.ollama_url}/api/tags", timeout=5.0)
                return response.status_code == 200
        except Exception:
            return False
    
    async def get_available_models(self) -> List[str]:
        """Get list of available models in Ollama."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.ollama_url}/api/tags", timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    return [m["name"] for m in data.get("models", [])]
        except Exception:
            pass
        return []
    
    async def _plan_query(self, query: str) -> Dict[str, Any]:
        """
        Analyze the user's query to determine which data sources to use.
        Returns a plan with flags for: vector_search, mongodb_query, recent_logs
        """
        plan = {
            "use_vector_search": False,
            "use_mongodb": False,
            "use_recent_logs": True,  # Always include recent logs
            "vector_query": query,
            "time_range_days": None,
            "container_filter": None,
            "level_filter": None,
        }
        
        query_lower = query.lower()
        
        # Detect time-based queries
        time_patterns = {
            "last month": 30,
            "past month": 30,
            "last week": 7,
            "past week": 7,
            "last day": 1,
            "yesterday": 1,
            "today": 1,
            "last hour": 0.04,  # ~1 hour
            "recently": 1,
        }
        
        for pattern, days in time_patterns.items():
            if pattern in query_lower:
                plan["time_range_days"] = days
                plan["use_mongodb"] = True
                break
        
        # Detect frequency/count queries
        frequency_keywords = ["how many", "how often", "count", "frequency", "times", "occurrences"]
        if any(kw in query_lower for kw in frequency_keywords):
            plan["use_mongodb"] = True
        
        # Detect similarity queries
        similarity_keywords = ["similar", "like this", "same error", "related", "pattern"]
        if any(kw in query_lower for kw in similarity_keywords):
            plan["use_vector_search"] = True
        
        # Detect analysis queries (benefit from historical context)
        analysis_keywords = ["trend", "pattern", "analysis", "investigate", "root cause", "why"]
        if any(kw in query_lower for kw in analysis_keywords):
            plan["use_vector_search"] = True
            plan["use_mongodb"] = True
        
        # If asking about errors specifically
        if "error" in query_lower:
            plan["level_filter"] = "error"
            plan["use_mongodb"] = True
        
        return plan
    
    async def _gather_rag_context(
        self,
        query: str,
        plan: Dict[str, Any],
        logs: Optional[List[ContainerLog]] = None,
    ) -> Dict[str, Any]:
        """Gather context from various sources based on query plan."""
        context = {
            "recent_logs": "",
            "similar_logs": "",
            "statistics": "",
            "historical_events": "",
        }
        
        # Recent logs (always included)
        if logs and plan.get("use_recent_logs", True):
            context["recent_logs"] = self._format_logs_for_analysis(logs)
        
        if not self.rag_enabled:
            return context
        
        try:
            # Vector search for similar logs
            if plan.get("use_vector_search"):
                similar = await self._vector.search(
                    query=plan.get("vector_query", query),
                    limit=settings.rag_search_limit,
                    time_filter_hours=int(plan.get("time_range_days", 7) * 24) if plan.get("time_range_days") else None,
                )
                
                if similar:
                    similar_text = []
                    for result in similar:
                        ts = result.document.timestamp.strftime("%Y-%m-%d %H:%M") if result.document.timestamp else "unknown"
                        similar_text.append(
                            f"[{ts}] [{result.document.container_name}] {result.document.message}"
                        )
                    context["similar_logs"] = "\n".join(similar_text)
            
            # MongoDB queries for statistics
            if plan.get("use_mongodb") and self._storage.is_connected:
                stats_parts = []
                
                # Time range
                start_time = None
                if plan.get("time_range_days"):
                    start_time = datetime.now() - timedelta(days=plan["time_range_days"])
                
                # Get error counts
                error_counts = await self._storage.get_error_count_by_container(
                    start_time=start_time
                )
                if error_counts:
                    stats_parts.append("Error counts by container:")
                    for container, count in list(error_counts.items())[:10]:
                        stats_parts.append(f"  - {container}: {count} errors")
                
                # Get pattern frequency
                patterns = await self._storage.get_pattern_frequency(
                    start_time=start_time,
                    limit=10
                )
                if patterns:
                    stats_parts.append("\nMost common error patterns:")
                    for pattern, count in list(patterns.items())[:10]:
                        stats_parts.append(f"  - {pattern}: {count} occurrences")
                
                # Get total counts
                total_errors = await self._storage.count_events(
                    level=plan.get("level_filter"),
                    start_time=start_time,
                )
                if total_errors:
                    time_desc = f"last {plan.get('time_range_days', 7)} days" if plan.get('time_range_days') else "all time"
                    stats_parts.append(f"\nTotal events ({time_desc}): {total_errors}")
                
                context["statistics"] = "\n".join(stats_parts)
                
                # Get historical events matching the query
                from app.services.storage_service import LogLevel
                events = await self._storage.get_events(
                    level=LogLevel(plan["level_filter"]) if plan.get("level_filter") else None,
                    start_time=start_time,
                    limit=20,
                )
                if events:
                    historical = []
                    for event in events:
                        ts = event.timestamp.strftime("%Y-%m-%d %H:%M") if event.timestamp else "unknown"
                        historical.append(
                            f"[{ts}] [{event.container_name}] [{event.level.value}] {event.message[:200]}"
                        )
                    context["historical_events"] = "\n".join(historical)
                    
        except Exception as e:
            logger.error(f"Error gathering RAG context: {e}")
        
        return context
    
    async def analyze_logs(
        self,
        logs: List[ContainerLog],
        query: Optional[str] = None,
        detect_issues: bool = True
    ) -> AIAnalysisResponse:
        """Analyze logs using the LLM with RAG support."""
        
        containers = list(set(log.container_name for log in logs))
        
        if query and self.rag_enabled:
            # Use RAG for user queries
            plan = await self._plan_query(query)
            context = await self._gather_rag_context(query, plan, logs)
            prompt = self._build_rag_prompt(query, context)
        elif query:
            # Regular query without RAG
            log_text = self._format_logs_for_analysis(logs)
            prompt = self._build_query_prompt(log_text, query)
        else:
            # Automatic analysis
            log_text = self._format_logs_for_analysis(logs)
            prompt = self._build_analysis_prompt(log_text)
        
        # Get LLM response
        response_text = await self._query_llm(prompt)
        
        # Extract issues if analyzing for problems
        issues = []
        if detect_issues and not query:
            issues = self._extract_issues_from_response(response_text, logs)
            for issue in issues:
                self._add_or_increment_issue(issue)
        
        return AIAnalysisResponse(
            query=query or "Automatic log analysis",
            response=response_text,
            containers_analyzed=containers,
            issues_found=issues
        )
    
    async def chat(
        self,
        message: str,
        logs: Optional[List[ContainerLog]] = None,
        stream: bool = False
    ) -> str | AsyncGenerator[str, None]:
        """Chat with the AI about logs with RAG support."""
        
        # Add user message to history
        self._chat_history.append(ChatMessage(role="user", content=message))
        
        # Use RAG if enabled
        if self.rag_enabled:
            plan = await self._plan_query(message)
            context = await self._gather_rag_context(message, plan, logs)
            prompt = self._build_rag_chat_prompt(message, context)
        else:
            # Fallback to simple chat
            context = ""
            if logs:
                context = f"\n\nCurrent Docker Logs:\n{self._format_logs_for_analysis(logs)}\n\n"
            prompt = self._build_chat_prompt(message, context)
        
        if stream:
            return self._stream_llm_response(prompt)
        else:
            response = await self._query_llm(prompt)
            self._chat_history.append(ChatMessage(role="assistant", content=response))
            return response
    
    async def ask_about_issue(
        self,
        issue: DetectedIssue,
        question: str,
    ) -> str:
        """Ask a specific question about a detected issue using RAG."""
        
        combined_query = f"""
Issue: {issue.title}
Container: {issue.container_name}
Severity: {issue.severity.value}
Log excerpt: {issue.log_excerpt}

Question: {question}
"""
        
        if self.rag_enabled:
            plan = await self._plan_query(combined_query)
            plan["use_vector_search"] = True  # Always search for similar issues
            plan["use_mongodb"] = True  # Always check frequency
            
            context = await self._gather_rag_context(combined_query, plan)
            prompt = self._build_rag_prompt(combined_query, context)
        else:
            prompt = f"""You are a DevOps expert. Analyze this issue and answer the question.

{combined_query}

Provide a detailed, helpful response."""
        
        return await self._query_llm(prompt)
    
    async def get_issue_frequency(
        self,
        pattern: str,
        container_name: Optional[str] = None,
        days: int = 30,
    ) -> Dict[str, Any]:
        """Get frequency statistics for a specific issue pattern."""
        if not self.rag_enabled or not self._storage.is_connected:
            return {"error": "RAG/Storage not available"}
        
        start_time = datetime.now() - timedelta(days=days)
        
        # Get total count
        total = await self._storage.count_events(
            container_name=container_name,
            start_time=start_time,
        )
        
        # Search for similar messages
        from app.services.storage_service import LogLevel
        events = await self._storage.search_messages(
            search_text=pattern,
            container_name=container_name,
            limit=100,
        )
        
        # Get daily distribution
        daily = await self._storage.get_daily_counts(
            container_name=container_name,
            level=LogLevel.ERROR,
            days=days,
        )
        
        return {
            "pattern": pattern,
            "container": container_name or "all",
            "time_range_days": days,
            "matching_events": len(events),
            "total_events": total,
            "daily_distribution": daily,
        }
    
    def _get_log_hash(self, log: ContainerLog) -> str:
        """Generate a unique hash for a log entry."""
        import hashlib
        # Use container, timestamp and first 100 chars of message for hash
        ts_str = log.timestamp.isoformat() if log.timestamp else ""
        content = f"{log.container_id}:{ts_str}:{log.message[:100]}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def _filter_new_logs(self, logs: List[ContainerLog]) -> List[ContainerLog]:
        """Filter logs to only include ones that haven't been analyzed yet."""
        new_logs = []
        for log in logs:
            log_hash = self._get_log_hash(log)
            if log_hash not in self._analyzed_log_hashes:
                new_logs.append(log)
        return new_logs
    
    def _mark_logs_as_analyzed(self, logs: List[ContainerLog]) -> None:
        """Mark logs as analyzed to avoid re-processing."""
        for log in logs:
            log_hash = self._get_log_hash(log)
            self._analyzed_log_hashes.add(log_hash)
            self._total_logs_analyzed += 1
        
        # Update last analyzed timestamp
        if logs:
            latest = max((log.timestamp for log in logs if log.timestamp), default=None)
            if latest and (self._last_analyzed_timestamp is None or latest > self._last_analyzed_timestamp):
                self._last_analyzed_timestamp = latest
        
        # Limit hash set size to prevent memory issues (keep last 100k hashes)
        if len(self._analyzed_log_hashes) > 100000:
            # Clear oldest half
            self._analyzed_log_hashes = set(list(self._analyzed_log_hashes)[-50000:])
    
    def get_analysis_status(self) -> Dict[str, Any]:
        """Get the current incremental analysis status."""
        return {
            "initial_scan_done": self._initial_scan_done,
            "total_logs_analyzed": self._total_logs_analyzed,
            "last_analyzed_timestamp": self._last_analyzed_timestamp.isoformat() if self._last_analyzed_timestamp else None,
            "tracked_log_hashes": len(self._analyzed_log_hashes),
            "detected_issues_count": len(self._detected_issues),
        }
    
    async def initial_scan(self, logs: List[ContainerLog]) -> List[DetectedIssue]:
        """
        Perform initial scan of all historical logs.
        Should be called once when the application starts.
        """
        if self._initial_scan_done:
            logger.info("Initial scan already done, skipping")
            return []
        
        logger.info(f"Starting initial log scan with {len(logs)} logs")
        
        # Mark all logs as analyzed (we'll process them now)
        self._mark_logs_as_analyzed(logs)
        self._initial_scan_done = True
        
        # Run the actual issue detection
        issues = await self._analyze_logs_for_issues(logs)
        
        logger.info(f"Initial scan complete: {len(issues)} issues found, {self._total_logs_analyzed} logs analyzed")
        return issues
    
    async def quick_issue_check(self, logs: List[ContainerLog]) -> List[DetectedIssue]:
        """
        AI-powered incremental issue detection from logs.
        Only analyzes logs that haven't been processed before.
        """
        if not logs:
            return []
        
        # If initial scan hasn't been done, do it now
        if not self._initial_scan_done:
            return await self.initial_scan(logs)
        
        # Filter to only new logs
        new_logs = self._filter_new_logs(logs)
        
        if not new_logs:
            logger.debug("No new logs to analyze")
            return []
        
        logger.info(f"Analyzing {len(new_logs)} new logs (out of {len(logs)} total)")
        
        # Mark these logs as analyzed
        self._mark_logs_as_analyzed(new_logs)
        
        # Run the actual issue detection on new logs only
        return await self._analyze_logs_for_issues(new_logs)
    
    async def _analyze_logs_for_issues(self, logs: List[ContainerLog]) -> List[DetectedIssue]:
        """Internal method to analyze logs for issues."""
        if not logs:
            return []
        
        # Filter out logs that should be ignored (e.g., AI API calls)
        filtered_logs = [log for log in logs if not self._should_ignore_log(log.message)]
        
        if not filtered_logs:
            return []
        
        # Format logs for AI analysis
        log_text = self._format_logs_for_analysis(filtered_logs)
        
        # Build prompt for AI issue detection
        prompt = self._build_issue_detection_prompt(log_text)
        
        # Query the LLM
        try:
            response = await self._query_llm(prompt)
            issues = self._parse_ai_issues(response, filtered_logs)
            
            # Filter out issues that reference ignored log patterns
            issues = [i for i in issues if not self._should_ignore_log(i.log_excerpt)]
            
            # Track issues and count occurrences
            for issue in issues:
                self._add_or_increment_issue(issue)
            
            return issues
            
        except Exception as e:
            logger.error(f"AI issue detection failed: {e}")
            # Fallback to basic pattern detection
            return self._fallback_issue_check(logs)
    
    def _build_issue_detection_prompt(self, log_text: str) -> str:
        """Build prompt for AI-powered issue detection."""
        return f"""You are an expert DevOps engineer analyzing Docker container logs for issues.

Analyze the following logs and identify any problems, errors, warnings, or anomalies.

Log format: [TIMESTAMP] [CONTAINER_NAME] message

LOGS:
{log_text}

For each issue found, respond in this EXACT JSON format (array of issues):
```json
[
  {{
    "container": "container_name_from_brackets",
    "severity": "critical|error|warning|info",
    "title": "Brief title describing the issue",
    "description": "Detailed explanation of what the issue is and why it matters",
    "log_excerpt": "The specific log line(s) showing the issue",
    "suggestion": "Recommended action to fix or investigate"
  }}
]
```

Rules:
- IMPORTANT: The "container" field must be the EXACT container name from the [CONTAINER_NAME] part of the log line, NOT a name extracted from the log message content
- Only include REAL issues (not normal operation logs like HTTP 200 OK responses)
- Severity levels: critical (service down, data loss risk), error (failures, exceptions), warning (potential problems), info (informational issues)
- Be specific in descriptions - explain the actual problem
- Focus on actionable issues that need attention
- If no issues found, return an empty array: []
- Limit to maximum 10 most important issues
- DO NOT include normal HTTP request logs or routine operations
- IGNORE any logs related to API calls like "/api/ai/chat" - these contain user messages that may have error keywords but are not actual errors
- IGNORE HTTP requests to ollama or AI services

Respond ONLY with the JSON array, no other text."""

    def _parse_ai_issues(self, response: str, logs: List[ContainerLog]) -> List[DetectedIssue]:
        """Parse AI response into DetectedIssue objects."""
        issues = []
        
        try:
            # Extract JSON from response (handle markdown code blocks)
            json_str = response.strip()
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
            
            # Try to find JSON array in the response using regex
            if not json_str.startswith('['):
                # Look for JSON array pattern
                match = re.search(r'\[[\s\S]*\]', json_str)
                if match:
                    json_str = match.group(0)
            
            # Fix common JSON issues from LLMs
            # Remove trailing commas before ] or }
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
            
            # Parse JSON
            parsed = json.loads(json_str)
            
            if not isinstance(parsed, list):
                # If it's a single object, wrap in list
                if isinstance(parsed, dict):
                    parsed = [parsed]
                else:
                    return []
            
            # Build maps for container lookup
            container_map = {log.container_name: log.container_id for log in logs}
            # Get unique container names from actual logs
            valid_container_names = set(container_map.keys())
            
            for item in parsed[:10]:  # Limit to 10 issues
                if not isinstance(item, dict):
                    continue
                
                ai_container_name = item.get("container", "unknown")
                log_excerpt = item.get("log_excerpt", "")
                
                # Try to find the actual container from the log excerpt
                # The AI might return wrong container names (e.g., "mongosh" from log content)
                actual_container_name = None
                actual_container_id = None
                
                # First, check if AI's container name is valid
                if ai_container_name in valid_container_names:
                    actual_container_name = ai_container_name
                    actual_container_id = container_map.get(ai_container_name, "unknown")
                else:
                    # Search for the log excerpt in actual logs to find the real container
                    excerpt_search = log_excerpt[:50] if log_excerpt else ""
                    for log in logs:
                        if excerpt_search and excerpt_search in log.message:
                            actual_container_name = log.container_name
                            actual_container_id = log.container_id
                            break
                    
                    # If still not found, use the first container or "unknown"
                    if not actual_container_name and logs:
                        actual_container_name = logs[0].container_name
                        actual_container_id = logs[0].container_id
                    elif not actual_container_name:
                        actual_container_name = ai_container_name
                        actual_container_id = "unknown"
                
                container_name = actual_container_name
                container_id = actual_container_id
                
                # Map severity string to enum
                severity_str = item.get("severity", "info").lower()
                severity_map = {
                    "critical": IssueSeverity.CRITICAL,
                    "error": IssueSeverity.ERROR,
                    "warning": IssueSeverity.WARNING,
                    "info": IssueSeverity.INFO,
                }
                severity = severity_map.get(severity_str, IssueSeverity.INFO)
                
                issue = DetectedIssue(
                    id=str(uuid.uuid4()),
                    container_id=container_id,
                    container_name=container_name,
                    severity=severity,
                    title=item.get("title", "Issue detected")[:100],
                    description=item.get("description", "")[:500],
                    log_excerpt=item.get("log_excerpt", "")[:300],
                    suggestion=item.get("suggestion", "Review logs for more details")[:300],
                )
                issues.append(issue)
                
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse AI issue response as JSON: {e}")
        except Exception as e:
            logger.error(f"Error parsing AI issues: {e}")
        
        return issues
    
    def _should_ignore_log(self, message: str) -> bool:
        """Check if a log message should be ignored for issue detection."""
        # Ignore patterns - these are not real errors
        ignore_patterns = [
            "/api/ai/chat?message=",  # AI chat API calls contain user messages with error keywords
            "POST /api/ai/",          # Any AI API calls
            "GET /api/ai/",
            "HTTP Request: POST http://ollama",  # Ollama HTTP requests
        ]
        
        for pattern in ignore_patterns:
            if pattern in message:
                return True
        
        return False
    
    def _fallback_issue_check(self, logs: List[ContainerLog]) -> List[DetectedIssue]:
        """Fallback pattern-based issue detection when AI fails."""
        issues = []
        
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
        
        for log in logs:
            # Skip logs that should be ignored
            if self._should_ignore_log(log.message):
                continue
                
            message_lower = log.message.lower()
            for pattern, severity in error_patterns:
                if pattern in message_lower:
                    issue = DetectedIssue(
                        id=str(uuid.uuid4()),
                        container_id=log.container_id,
                        container_name=log.container_name,
                        severity=severity,
                        title=f"{severity.value.upper()} in {log.container_name}",
                        description=f"Pattern '{pattern}' detected in log message",
                        log_excerpt=log.message[:200],
                        suggestion="Review the log message for more context"
                    )
                    self._add_or_increment_issue(issue)
                    issues.append(issue)
                    break
        
        return issues
    
    def _get_issue_signature(self, issue: DetectedIssue) -> str:
        """Generate a signature for issue deduplication based on title and container."""
        # Normalize title to catch similar issues
        title_normalized = issue.title.lower().strip()
        return f"{issue.container_name}:{title_normalized}"
    
    def _add_or_increment_issue(self, issue: DetectedIssue) -> None:
        """Add a new issue or increment occurrence count if similar issue exists."""
        signature = self._get_issue_signature(issue)
        
        # Look for existing similar issue
        for existing in self._detected_issues:
            if not existing.resolved and self._get_issue_signature(existing) == signature:
                # Increment occurrence count and update timestamp
                existing.occurrence_count += 1
                existing.detected_at = issue.detected_at
                # Update log excerpt with the latest one
                existing.log_excerpt = issue.log_excerpt
                return
        
        # No existing issue found, add as new
        self._detected_issues.append(issue)
    
    def get_detected_issues(
        self,
        limit: int = 50,
        container_id: Optional[str] = None,
        severity: Optional[IssueSeverity] = None,
        include_resolved: bool = False,
        min_occurrences: int = 1
    ) -> List[DetectedIssue]:
        """Get detected issues with optional filters."""
        issues = self._detected_issues.copy()
        
        if container_id:
            issues = [i for i in issues if i.container_id == container_id]
        
        if severity:
            issues = [i for i in issues if i.severity == severity]
        
        if not include_resolved:
            issues = [i for i in issues if not i.resolved]
        
        # Filter by minimum occurrences (for recurring issues)
        if min_occurrences > 1:
            issues = [i for i in issues if i.occurrence_count >= min_occurrences]
        
        # Sort by occurrence count (most frequent first), then by time
        issues.sort(key=lambda x: (x.occurrence_count, x.detected_at), reverse=True)
        
        return issues[:limit]
    
    def resolve_issue(self, issue_id: str) -> bool:
        """Mark an issue as resolved."""
        for issue in self._detected_issues:
            if issue.id == issue_id:
                issue.resolved = True
                return True
        return False
    
    def clear_issues(self) -> int:
        """Clear all detected issues. Returns count of cleared issues."""
        count = len(self._detected_issues)
        self._detected_issues.clear()
        return count
    
    def _format_logs_for_analysis(self, logs: List[ContainerLog]) -> str:
        """Format logs for LLM analysis."""
        formatted = []
        for log in logs[-100:]:  # Limit to last 100 logs
            ts = log.timestamp.strftime("%H:%M:%S") if log.timestamp else "??:??:??"
            formatted.append(f"[{ts}] [{log.container_name}] {log.message}")
        
        text = "\n".join(formatted)
        
        # Truncate if too long
        if len(text) > settings.max_log_context:
            text = text[-settings.max_log_context:]
        
        return text
    
    def _build_rag_prompt(self, query: str, context: Dict[str, Any]) -> str:
        """Build prompt with RAG context."""
        sections = []
        
        if context.get("statistics"):
            sections.append(f"=== STATISTICS ===\n{context['statistics']}")
        
        if context.get("historical_events"):
            sections.append(f"=== HISTORICAL LOGS ===\n{context['historical_events']}")
        
        if context.get("similar_logs"):
            sections.append(f"=== SIMILAR LOGS (semantic search) ===\n{context['similar_logs']}")
        
        if context.get("recent_logs"):
            sections.append(f"=== RECENT LOGS ===\n{context['recent_logs']}")
        
        context_text = "\n\n".join(sections)
        
        # Truncate context if needed
        max_context = settings.rag_context_window
        if len(context_text) > max_context:
            context_text = context_text[:max_context] + "\n... (truncated)"
        
        return f"""You are an expert DevOps engineer analyzing Docker container logs.
You have access to historical log data, statistics, and recent logs.

{context_text}

USER QUESTION: {query}

Based on all the available data above, provide a comprehensive and accurate answer.
If asking about frequency or counts, use the statistics provided.
If asking about patterns or similar issues, reference the similar logs found.
Be specific with numbers and timestamps when available.
If you cannot find enough information to answer, say so clearly."""
    
    def _build_rag_chat_prompt(self, message: str, context: Dict[str, Any]) -> str:
        """Build chat prompt with RAG context."""
        history = ""
        for msg in self._chat_history[-10:]:  # Last 10 messages
            history += f"{msg.role.upper()}: {msg.content}\n"
        
        sections = []
        
        if context.get("statistics"):
            sections.append(f"Statistics:\n{context['statistics']}")
        
        if context.get("similar_logs"):
            sections.append(f"Similar historical logs:\n{context['similar_logs'][:2000]}")
        
        if context.get("recent_logs"):
            sections.append(f"Recent logs:\n{context['recent_logs'][:2000]}")
        
        context_text = "\n\n".join(sections)
        
        return f"""You are a helpful DevOps assistant specializing in Docker and container management.
You have access to historical log data and statistics to provide accurate answers.

{context_text}

CONVERSATION:
{history}
USER: {message}

ASSISTANT:"""
    
    def _build_analysis_prompt(self, log_text: str) -> str:
        """Build prompt for automatic log analysis."""
        return f"""You are a DevOps expert analyzing Docker container logs. 
Analyze the following logs and identify any issues, errors, or potential problems.

LOGS:
{log_text}

Provide a structured analysis:
1. Summary of what's happening
2. Any errors or warnings found (list each with severity: CRITICAL, ERROR, WARNING, INFO)
3. Potential root causes
4. Recommended actions

Be concise and focus on actionable insights."""

    def _build_query_prompt(self, log_text: str, query: str) -> str:
        """Build prompt for user query about logs."""
        return f"""You are a DevOps expert helping analyze Docker container logs.

LOGS:
{log_text}

USER QUESTION: {query}

Provide a helpful, accurate response based on the logs above. If the answer isn't 
clear from the logs, say so. Be concise and practical."""

    def _build_chat_prompt(self, message: str, context: str) -> str:
        """Build prompt for chat interaction."""
        history = ""
        for msg in self._chat_history[-10:]:  # Last 10 messages
            history += f"{msg.role.upper()}: {msg.content}\n"
        
        return f"""You are a helpful DevOps assistant specializing in Docker and container management.
You help users understand their container logs and troubleshoot issues.
{context}

CONVERSATION:
{history}
USER: {message}

ASSISTANT:"""

    async def _query_llm(self, prompt: str) -> str:
        """Query the Ollama LLM."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.7,
                            "num_predict": 2000,  # Increased for detailed responses
                        }
                    },
                    timeout=180.0  # Increased timeout for complex queries
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return data.get("response", "No response generated")
                else:
                    return f"Error: LLM returned status {response.status_code}"
                    
        except httpx.TimeoutException:
            return "Error: Request to LLM timed out. The model might be loading."
        except Exception as e:
            return f"Error communicating with LLM: {str(e)}"
    
    async def _stream_llm_response(self, prompt: str) -> AsyncGenerator[str, None]:
        """Stream response from Ollama LLM."""
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{self.ollama_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": True,
                        "options": {
                            "temperature": 0.7,
                            "num_predict": 2000,
                        }
                    },
                    timeout=180.0
                ) as response:
                    full_response = ""
                    async for line in response.aiter_lines():
                        if line:
                            try:
                                data = json.loads(line)
                                token = data.get("response", "")
                                full_response += token
                                yield token
                                if data.get("done"):
                                    break
                            except json.JSONDecodeError:
                                continue
                    
                    # Save to history
                    self._chat_history.append(
                        ChatMessage(role="assistant", content=full_response)
                    )
                    
        except Exception as e:
            yield f"Error: {str(e)}"
    
    def _extract_issues_from_response(
        self,
        response: str,
        logs: List[ContainerLog]
    ) -> List[DetectedIssue]:
        """Extract structured issues from LLM response."""
        issues = []
        
        # Simple extraction based on severity keywords in response
        response_lower = response.lower()
        
        severity_markers = {
            IssueSeverity.CRITICAL: ["critical", "fatal", "urgent", "immediately"],
            IssueSeverity.ERROR: ["error", "failed", "exception", "problem"],
            IssueSeverity.WARNING: ["warning", "caution", "potential", "might"],
        }
        
        # Find the highest severity mentioned
        detected_severity = None
        for severity, markers in severity_markers.items():
            if any(marker in response_lower for marker in markers):
                detected_severity = severity
                break
        
        if detected_severity:
            # Create a summary issue
            containers = list(set(log.container_name for log in logs))
            issue = DetectedIssue(
                id=str(uuid.uuid4()),
                container_id=logs[0].container_id if logs else "unknown",
                container_name=", ".join(containers[:3]),
                severity=detected_severity,
                title=f"AI Analysis: Issues detected in logs",
                description=response[:500],
                log_excerpt=self._format_logs_for_analysis(logs[-5:]),
                suggestion="Review the full AI analysis for details"
            )
            issues.append(issue)
        
        return issues
    
    def get_rag_status(self) -> Dict[str, Any]:
        """Get status of RAG components."""
        status = {
            "rag_enabled": settings.rag_enabled,
            "rag_available": self._rag_available,
            "vector_service": None,
            "storage_service": None,
        }
        
        if self._rag_available:
            try:
                status["vector_service"] = self._vector.get_stats()
            except:
                pass
            
            try:
                status["storage_service"] = {"connected": self._storage.is_connected}
            except:
                pass
        
        return status


# Global service instance
ai_service = AIService()
