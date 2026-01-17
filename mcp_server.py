#!/usr/bin/env python3
"""
MCP Server for LogsCrawler OpenSearch Integration.
Provides tools for AI agents to search and analyze container logs.

Usage:
    python mcp_server.py

Configure in your MCP client (e.g., Claude Desktop, Cursor):
{
    "mcpServers": {
        "logscrawler": {
            "command": "python",
            "args": ["path/to/mcp_server.py"],
            "env": {
                "LOGSCRAWLER_OPENSEARCH_HOST": "http://localhost:9200"
            }
        }
    }
}
"""

import asyncio
import json
import sys
import os
from datetime import datetime, timedelta
from typing import Optional, Any

import httpx

# Configuration from environment
OPENSEARCH_HOST = os.getenv("LOGSCRAWLER_OPENSEARCH_HOST", "http://localhost:9200")
OPENSEARCH_INDEX = os.getenv("LOGSCRAWLER_OPENSEARCH_INDEX", "logscrawler-logs")


class OpenSearchClient:
    """Simple OpenSearch client for MCP server."""
    
    def __init__(self, host: str, index: str):
        self.host = host
        self.index = index
    
    async def search(
        self,
        query: str = "",
        container: Optional[str] = None,
        system: Optional[str] = None,
        level: Optional[str] = None,
        hours: Optional[int] = None,
        limit: int = 50,
    ) -> dict:
        """Search logs with filters."""
        must = []
        filter_clauses = []
        
        if query:
            must.append({
                "match": {
                    "message": {
                        "query": query,
                        "operator": "and"
                    }
                }
            })
        
        if container:
            filter_clauses.append({"term": {"container_name": container}})
        
        if system:
            filter_clauses.append({"term": {"system_name": system}})
        
        if level:
            filter_clauses.append({"term": {"level": level.lower()}})
        
        if hours:
            time_from = (datetime.now() - timedelta(hours=hours)).isoformat()
            filter_clauses.append({"range": {"timestamp": {"gte": time_from}}})
        
        search_body = {
            "size": limit,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {
                "bool": {
                    "must": must if must else [{"match_all": {}}],
                    "filter": filter_clauses
                }
            },
            "highlight": {
                "fields": {
                    "message": {
                        "pre_tags": ["**"],
                        "post_tags": ["**"],
                        "fragment_size": 200
                    }
                }
            }
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.host}/{self.index}/_search",
                json=search_body
            )
            
            if response.status_code != 200:
                return {"error": f"Search failed: {response.status_code}"}
            
            result = response.json()
            hits = result.get("hits", {}).get("hits", [])
            
            logs = []
            for hit in hits:
                source = hit.get("_source", {})
                highlight = hit.get("highlight", {}).get("message", [])
                logs.append({
                    "container": source.get("container_name", ""),
                    "system": source.get("system_name", "local"),
                    "level": source.get("level", "info"),
                    "timestamp": source.get("timestamp", ""),
                    "message": source.get("message", ""),
                    "highlight": highlight[0] if highlight else None,
                })
            
            return {
                "total": result.get("hits", {}).get("total", {}).get("value", 0),
                "returned": len(logs),
                "logs": logs
            }
    
    async def get_stats(self) -> dict:
        """Get index statistics."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get index stats
            stats_response = await client.get(f"{self.host}/{self.index}/_stats")
            
            # Get aggregations for overview
            aggs_body = {
                "size": 0,
                "aggs": {
                    "containers": {"terms": {"field": "container_name", "size": 20}},
                    "systems": {"terms": {"field": "system_name", "size": 10}},
                    "levels": {"terms": {"field": "level"}},
                    "patterns": {"terms": {"field": "pattern", "size": 10}}
                }
            }
            aggs_response = await client.post(
                f"{self.host}/{self.index}/_search",
                json=aggs_body
            )
            
            stats = {}
            if stats_response.status_code == 200:
                result = stats_response.json()
                primaries = result.get("indices", {}).get(self.index, {}).get("primaries", {})
                stats["doc_count"] = primaries.get("docs", {}).get("count", 0)
                stats["size_mb"] = round(primaries.get("store", {}).get("size_in_bytes", 0) / 1024 / 1024, 2)
            
            if aggs_response.status_code == 200:
                result = aggs_response.json()
                aggs = result.get("aggregations", {})
                stats["containers"] = [
                    {"name": b["key"], "count": b["doc_count"]}
                    for b in aggs.get("containers", {}).get("buckets", [])
                ]
                stats["systems"] = [
                    {"name": b["key"], "count": b["doc_count"]}
                    for b in aggs.get("systems", {}).get("buckets", [])
                ]
                stats["levels"] = {
                    b["key"]: b["doc_count"]
                    for b in aggs.get("levels", {}).get("buckets", [])
                }
                stats["top_patterns"] = [
                    {"pattern": b["key"], "count": b["doc_count"]}
                    for b in aggs.get("patterns", {}).get("buckets", [])
                ]
            
            return stats
    
    async def get_errors(self, hours: int = 24, limit: int = 50) -> dict:
        """Get recent error logs."""
        return await self.search(level="error", hours=hours, limit=limit)
    
    async def get_container_logs(self, container: str, limit: int = 100) -> dict:
        """Get logs for a specific container."""
        return await self.search(container=container, limit=limit)


# Initialize client
client = OpenSearchClient(OPENSEARCH_HOST, OPENSEARCH_INDEX)


# MCP Protocol Implementation
class MCPServer:
    """Model Context Protocol server for log search."""
    
    def __init__(self):
        self.tools = {
            "search_logs": {
                "description": "Search container logs with full-text query and filters. Returns matching log entries from all monitored Docker containers.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Full-text search query (e.g., 'connection refused', 'error', 'timeout')"
                        },
                        "container": {
                            "type": "string",
                            "description": "Filter by container name (e.g., 'nginx', 'postgres')"
                        },
                        "system": {
                            "type": "string",
                            "description": "Filter by system name ('local' or remote system name)"
                        },
                        "level": {
                            "type": "string",
                            "description": "Filter by log level: 'debug', 'info', 'warning', 'error', 'critical'",
                            "enum": ["debug", "info", "warning", "error", "critical"]
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Only include logs from the last N hours"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 50)",
                            "default": 50
                        }
                    }
                }
            },
            "get_errors": {
                "description": "Get recent error-level logs from all containers. Use this to quickly identify problems.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "Look back N hours (default: 24)",
                            "default": 24
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 50)",
                            "default": 50
                        }
                    }
                }
            },
            "get_container_logs": {
                "description": "Get recent logs for a specific container.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "container": {
                            "type": "string",
                            "description": "Container name to get logs for"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 100)",
                            "default": 100
                        }
                    },
                    "required": ["container"]
                }
            },
            "get_log_stats": {
                "description": "Get statistics about indexed logs including container counts, log levels, and detected patterns.",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    
    async def handle_request(self, request: dict) -> dict:
        """Handle an MCP request."""
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")
        
        if method == "initialize":
            return self._response(request_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "logscrawler-mcp",
                    "version": "1.0.0"
                }
            })
        
        elif method == "tools/list":
            tools_list = [
                {"name": name, **tool}
                for name, tool in self.tools.items()
            ]
            return self._response(request_id, {"tools": tools_list})
        
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            
            try:
                result = await self._call_tool(tool_name, arguments)
                return self._response(request_id, {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2, default=str)
                        }
                    ]
                })
            except Exception as e:
                return self._error(request_id, -32000, str(e))
        
        elif method == "notifications/initialized":
            # No response needed for notifications
            return None
        
        else:
            return self._error(request_id, -32601, f"Unknown method: {method}")
    
    async def _call_tool(self, name: str, arguments: dict) -> Any:
        """Execute a tool call."""
        if name == "search_logs":
            return await client.search(
                query=arguments.get("query", ""),
                container=arguments.get("container"),
                system=arguments.get("system"),
                level=arguments.get("level"),
                hours=arguments.get("hours"),
                limit=arguments.get("limit", 50)
            )
        
        elif name == "get_errors":
            return await client.get_errors(
                hours=arguments.get("hours", 24),
                limit=arguments.get("limit", 50)
            )
        
        elif name == "get_container_logs":
            if "container" not in arguments:
                raise ValueError("container parameter is required")
            return await client.get_container_logs(
                container=arguments["container"],
                limit=arguments.get("limit", 100)
            )
        
        elif name == "get_log_stats":
            return await client.get_stats()
        
        else:
            raise ValueError(f"Unknown tool: {name}")
    
    def _response(self, request_id: Any, result: dict) -> dict:
        """Create a JSON-RPC response."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result
        }
    
    def _error(self, request_id: Any, code: int, message: str) -> dict:
        """Create a JSON-RPC error response."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message
            }
        }


async def main():
    """Run the MCP server."""
    server = MCPServer()
    
    # Read from stdin, write to stdout (JSON-RPC over stdio)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    
    writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())
    
    buffer = ""
    
    while True:
        try:
            # Read a line
            line = await reader.readline()
            if not line:
                break
            
            line = line.decode('utf-8').strip()
            if not line:
                continue
            
            # Parse JSON-RPC request
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            # Handle request
            response = await server.handle_request(request)
            
            # Send response (if any)
            if response:
                response_str = json.dumps(response) + "\n"
                writer.write(response_str.encode('utf-8'))
                await writer.drain()
                
        except Exception as e:
            # Log errors to stderr
            print(f"Error: {e}", file=sys.stderr)
            continue


if __name__ == "__main__":
    asyncio.run(main())
