"""AI Service for natural language to OpenSearch query conversion."""

import json
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import aiohttp
import structlog

logger = structlog.get_logger()

# System prompt for query conversion
SYSTEM_PROMPT = """You are an AI assistant that converts natural language questions about logs into OpenSearch query parameters.

You must respond with a valid JSON object containing these fields:
- query: string or null (full-text search query, use OpenSearch query syntax)
- levels: array of strings (log levels: ERROR, WARN, INFO, DEBUG)
- http_status_min: number or null (minimum HTTP status code)
- http_status_max: number or null (maximum HTTP status code)
- hosts: array of strings (host names to filter)
- containers: array of strings (container names to filter)
- time_range: string (relative time: "5m", "10m", "1h", "6h", "24h", "7d") or null
- sort_order: "desc" or "asc"

Examples:
User: "Find errors from the last 10 minutes"
Response: {"query": null, "levels": ["ERROR"], "http_status_min": null, "http_status_max": null, "hosts": [], "containers": [], "time_range": "10m", "sort_order": "desc"}

User: "Show me all 500 errors in nginx"
Response: {"query": "nginx", "levels": ["ERROR"], "http_status_min": 500, "http_status_max": 599, "hosts": [], "containers": ["nginx"], "time_range": null, "sort_order": "desc"}

User: "What warnings occurred in the api container in the last hour?"
Response: {"query": null, "levels": ["WARN"], "http_status_min": null, "http_status_max": null, "hosts": [], "containers": ["api"], "time_range": "1h", "sort_order": "desc"}

User: "Find timeout errors from server-1"
Response: {"query": "timeout", "levels": ["ERROR"], "http_status_min": null, "http_status_max": null, "hosts": ["server-1"], "containers": [], "time_range": null, "sort_order": "desc"}

User: "Show recent 404 not found errors"
Response: {"query": "not found", "levels": [], "http_status_min": 404, "http_status_max": 404, "hosts": [], "containers": [], "time_range": "1h", "sort_order": "desc"}

User: "List all logs from yesterday sorted oldest first"
Response: {"query": null, "levels": [], "http_status_min": null, "http_status_max": null, "hosts": [], "containers": [], "time_range": "24h", "sort_order": "asc"}

IMPORTANT: Only respond with the JSON object, no explanations or markdown."""


class AIService:
    """Service for AI-powered query conversion using Ollama."""
    
    def __init__(self, ollama_url: str = "http://localhost:11434", model: str = "phi3:mini"):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self._session: Optional[aiohttp.ClientSession] = None
        self._available = False
        
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def check_availability(self) -> bool:
        """Check if Ollama is available and model is loaded."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.ollama_url}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [m.get("name", "") for m in data.get("models", [])]
                    # Check if our model or a variant is available
                    model_base = self.model.split(":")[0]
                    self._available = any(model_base in m for m in models)
                    if not self._available:
                        logger.warning("AI model not found", model=self.model, available=models)
                    return self._available
        except Exception as e:
            logger.debug("Ollama not available", error=str(e))
            self._available = False
        return False
    
    async def convert_to_query(self, natural_query: str) -> Dict[str, Any]:
        """Convert natural language question to OpenSearch query parameters."""
        if not self._available:
            await self.check_availability()
            
        if not self._available:
            # Fallback: return basic query
            return self._fallback_parse(natural_query)
        
        try:
            session = await self._get_session()
            
            payload = {
                "model": self.model,
                "prompt": natural_query,
                "system": SYSTEM_PROMPT,
                "stream": False,
                "options": {
                    "temperature": 0.1,  # Low temperature for consistent output
                    "num_predict": 256,
                }
            }
            
            async with session.post(
                f"{self.ollama_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response_text = data.get("response", "")
                    return self._parse_ai_response(response_text, natural_query)
                else:
                    logger.error("Ollama request failed", status=resp.status)
                    return self._fallback_parse(natural_query)
                    
        except Exception as e:
            logger.error("AI conversion failed", error=str(e))
            return self._fallback_parse(natural_query)
    
    def _parse_ai_response(self, response: str, original_query: str) -> Dict[str, Any]:
        """Parse AI response JSON."""
        try:
            # Try to extract JSON from response
            response = response.strip()
            
            # Handle markdown code blocks
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                response = response.split("```")[1].split("```")[0]
            
            # Parse JSON
            result = json.loads(response.strip())
            
            # Validate and normalize
            return {
                "query": result.get("query"),
                "levels": result.get("levels", []),
                "http_status_min": result.get("http_status_min"),
                "http_status_max": result.get("http_status_max"),
                "hosts": result.get("hosts", []),
                "containers": result.get("containers", []),
                "time_range": result.get("time_range"),
                "sort_order": result.get("sort_order", "desc"),
            }
            
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse AI response", response=response[:200], error=str(e))
            return self._fallback_parse(original_query)
    
    def _fallback_parse(self, query: str) -> Dict[str, Any]:
        """Simple fallback parser for when AI is unavailable."""
        query_lower = query.lower()
        
        result = {
            "query": None,
            "levels": [],
            "http_status_min": None,
            "http_status_max": None,
            "hosts": [],
            "containers": [],
            "time_range": None,
            "sort_order": "desc",
        }
        
        # Detect error levels
        if any(w in query_lower for w in ["error", "erreur", "fail", "fatal"]):
            result["levels"].append("ERROR")
        if any(w in query_lower for w in ["warning", "warn", "avertissement"]):
            result["levels"].append("WARN")
        
        # Detect time ranges
        time_patterns = [
            (r"(\d+)\s*minutes?", lambda m: f"{m.group(1)}m"),
            (r"(\d+)\s*hours?", lambda m: f"{m.group(1)}h"),
            (r"(\d+)\s*heures?", lambda m: f"{m.group(1)}h"),
            (r"last\s*hour", lambda m: "1h"),
            (r"dernière\s*heure", lambda m: "1h"),
            (r"today|aujourd", lambda m: "24h"),
            (r"yesterday|hier", lambda m: "24h"),
        ]
        
        for pattern, converter in time_patterns:
            match = re.search(pattern, query_lower)
            if match:
                result["time_range"] = converter(match)
                break
        
        # Detect HTTP status codes
        status_match = re.search(r"\b([45]\d{2})\b", query)
        if status_match:
            status = int(status_match.group(1))
            result["http_status_min"] = status
            result["http_status_max"] = status
        elif "5xx" in query_lower or "500" in query_lower:
            result["http_status_min"] = 500
            result["http_status_max"] = 599
        elif "4xx" in query_lower or "400" in query_lower:
            result["http_status_min"] = 400
            result["http_status_max"] = 499
        
        # Extract search terms (simple approach)
        # Remove common words and use remaining as query
        stop_words = {"find", "show", "get", "list", "search", "logs", "log", "from", "in", "the", 
                     "last", "recent", "all", "me", "trouve", "affiche", "cherche", "les", "des",
                     "dernières", "derniers", "minutes", "heures", "hours", "errors", "warnings"}
        words = re.findall(r'\b\w+\b', query_lower)
        search_words = [w for w in words if w not in stop_words and len(w) > 2 and not w.isdigit()]
        
        if search_words and not result["levels"] and result["http_status_min"] is None:
            result["query"] = " ".join(search_words[:3])  # Limit to 3 words
        
        return result
    
    async def analyze_log(self, message: str, level: str = "", container_name: str = "") -> Dict[str, Any]:
        """Analyze a log message to determine if it needs attention."""
        
        # Quick heuristic checks first (avoid AI call for obvious cases)
        message_lower = message.lower()
        
        # Clear error indicators
        critical_patterns = [
            "exception", "fatal", "critical", "panic", "crash", "out of memory",
            "connection refused", "permission denied", "access denied", "segmentation fault",
            "stack trace", "traceback", "killed", "oom", "deadlock"
        ]
        
        error_patterns = [
            "error", "failed", "failure", "unable to", "cannot", "could not",
            "timeout", "timed out", "refused", "rejected", "invalid", "corrupt"
        ]
        
        warning_patterns = [
            "warning", "warn", "deprecated", "slow", "retry", "retrying",
            "high", "low memory", "disk space", "rate limit"
        ]
        
        # Check for critical issues
        if level in ["FATAL", "CRITICAL"] or any(p in message_lower for p in critical_patterns):
            return {
                "severity": "critical",
                "assessment": "This log indicates a critical issue that requires immediate attention."
            }
        
        # Check for errors (but exclude HTTP 4xx in URL paths)
        is_http_log = "http" in message_lower and ('" 2' in message or '" 3' in message or '" 4' in message or '" 5' in message)
        has_error_in_path = "/error" in message_lower or "/errors" in message_lower
        
        if level == "ERROR" or (any(p in message_lower for p in error_patterns) and not has_error_in_path):
            # Check if it's just an HTTP error response
            if is_http_log and ('" 4' in message or '" 5' in message):
                http_status = re.search(r'" (\d{3})', message)
                if http_status:
                    status = int(http_status.group(1))
                    if 400 <= status < 500:
                        return {
                            "severity": "attention",
                            "assessment": f"HTTP {status} client error. May indicate bad requests or missing resources."
                        }
                    elif status >= 500:
                        return {
                            "severity": "critical",
                            "assessment": f"HTTP {status} server error. Indicates a backend issue that needs investigation."
                        }
            
            return {
                "severity": "attention",
                "assessment": "This log contains error indicators that should be reviewed."
            }
        
        # Check for warnings
        if level in ["WARN", "WARNING"] or any(p in message_lower for p in warning_patterns):
            return {
                "severity": "attention",
                "assessment": "This log contains warnings that may need monitoring."
            }
        
        # For INFO/DEBUG or no clear issues - try AI if available, else normal
        if not self._available:
            await self.check_availability()
        
        if self._available:
            try:
                return await self._ai_analyze_log(message, level, container_name)
            except Exception as e:
                logger.debug("AI analysis failed, using heuristics", error=str(e))
        
        # Default: appears normal
        return {
            "severity": "normal",
            "assessment": "This log appears to be a standard operational message."
        }
    
    async def _ai_analyze_log(self, message: str, level: str, container_name: str) -> Dict[str, Any]:
        """Use AI to analyze a log message."""
        session = await self._get_session()
        
        analysis_prompt = f"""Analyze this log message and determine if it indicates a problem.

Log message: {message[:500]}
Log level: {level or 'UNKNOWN'}
Container: {container_name or 'UNKNOWN'}

Respond with a JSON object containing:
- severity: "normal", "attention", or "critical"
- assessment: A brief one-sentence explanation (max 100 chars)

Examples:
{{"severity": "normal", "assessment": "Standard HTTP request log, no issues detected."}}
{{"severity": "attention", "assessment": "Connection timeout may indicate network issues."}}
{{"severity": "critical", "assessment": "Out of memory error requires immediate action."}}

Respond only with JSON, no markdown or explanations."""

        payload = {
            "model": self.model,
            "prompt": analysis_prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 100,
            }
        }
        
        async with session.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                response_text = data.get("response", "").strip()
                
                # Parse JSON response
                try:
                    if "```" in response_text:
                        response_text = response_text.split("```")[1] if "```json" in response_text else response_text.split("```")[0]
                        response_text = response_text.replace("json", "").strip()
                    
                    result = json.loads(response_text)
                    severity = result.get("severity", "normal")
                    if severity not in ["normal", "attention", "critical"]:
                        severity = "normal"
                    
                    return {
                        "severity": severity,
                        "assessment": result.get("assessment", "Analysis complete.")[:150]
                    }
                except:
                    pass
        
        # Fallback
        return {
            "severity": "normal",
            "assessment": "Unable to analyze. Log appears standard."
        }


# Global instance
ai_service: Optional[AIService] = None


def get_ai_service() -> AIService:
    """Get or create AI service instance."""
    import os
    global ai_service
    if ai_service is None:
        ollama_url = os.environ.get("LOGSCRAWLER_OLLAMA_URL", "http://ollama:11434")
        model = os.environ.get("LOGSCRAWLER_AI_MODEL", "phi3:mini")
        ai_service = AIService(ollama_url, model)
    return ai_service
