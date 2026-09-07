# PulsarCD MCP Servers

PulsarCD exposes two [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) servers that let AI agents interact with the platform.

## Endpoints

| Server | URL | Description |
|--------|-----|-------------|
| **Read** | `/ai/mcp` | Read-only tools: list stacks, containers, hosts, search logs, check action status |
| **Actions** | `/ai/actions/mcp` | Write tools: build, test and deploy stacks, run a shell command. **Admin JWT (or the MCP API key) only** |

## Authentication

The token **must** be sent in the `Authorization: Bearer <token>` header. The
`?token=` query-string fallback was removed: uvicorn writes the full request line
to its access log, and that log is indexed in the log store any `viewer` account
can search, so a token in the URL was a reusable credential sitting in a
searchable index. A streamable-HTTP MCP client always controls its headers.

Both servers accept two token types:

- **MCP API key** — dedicated key printed in the server logs at startup, or set via `PULSARCD_MCP__API_KEY`.
  This is a machine identity provisioned out of band: it carries full privilege on
  **both** servers and is not subject to the role check below. Treat it like a root
  credential, pin it explicitly rather than relying on the per-boot random value,
  and rotate it if it ever reaches a log.
- **JWT token** — the same token used by the web UI, with two conditions:
  - `/ai/actions/mcp` requires `role == "admin"`. A `viewer` JWT gets
    `403 {"error": "Admin role required for this MCP server"}` — its tools reach
    the Swarm manager over SSH. `/ai/mcp` accepts any authenticated role.
  - Revocation is enforced on both mounts: after a password change, a role change
    or an account deletion the token is refused with
    `401 {"error": "Token has been revoked"}`, exactly as on the HTTP API.

## Configuration

| Environment variable | Default | Description |
|---------------------|---------|-------------|
| `PULSARCD_MCP__ENABLED` | `true` | Enable or disable both MCP servers |
| `PULSARCD_MCP__API_KEY` | *(auto-generated)* | Set a fixed MCP API key. If empty, a random key is generated at startup and logged |

## Available Tools

### Read server (`/ai/mcp`)

| Tool | Description |
|------|-------------|
| `list_stacks` | List available stacks (starred GitHub repositories) |
| `list_containers` | List all Docker containers and their states across all hosts. Accepts optional `host` and `status` filters |
| `list_computers` | List all monitored hosts including discovered Swarm nodes. Returns names and the Swarm flag only: hostname/port/username are admin-only infrastructure detail and this server accepts any role |
| `get_log_metadata` | Discover available hosts, services, containers and log levels in the log store. Call this first before searching logs |
| `search_logs` | Search logs with filters (query, project, service, host, level, time range) or raw OpenSearch queries |
| `get_action_status` | Check the status of a background build or deploy action by its `action_id` |

### Actions server (`/ai/actions/mcp`)

| Tool | Description |
|------|-------------|
| `build_stack` | Build a Docker image from a GitHub repository. Accepts `version` to choose which version to build. Returns an `action_id` |
| `test_stack` | Run the test suite for a stack. Accepts `version` to choose which version to test. Returns an `action_id` |
| `deploy_stack` | Deploy a stack to Docker Swarm. Accepts `version` (or `tag`) to choose which version to deploy. Returns an `action_id` |
| `run_command` | Run a shell command on a host (the Swarm manager by default). This is arbitrary code execution on that node — it is why the whole server is admin-only |

All four are on the LLM agent's unconditional denylist
(`backend/config_file.py: DANGEROUS_TOOL_NAMES`): the agent refuses to call them
even when they are listed in `error_handling.allowed_tools`, unless
`error_handling.allow_dangerous_tools` is explicitly enabled. Any tool added to
this server must be added to that list too — a tool the agent can reach is a tool
a prompt injection in a log line can reach.

`build_stack`, `test_stack` and `deploy_stack` run in the background and return an `action_id`. Use `get_action_status` on the read server to track progress.

## Client Configuration Examples

### Claude Desktop / Claude Code

Add both servers in your MCP settings:

```json
{
  "mcpServers": {
    "pulsarcd": {
      "type": "streamable-http",
      "url": "https://your-host:8000/ai/mcp",
      "headers": {
        "Authorization": "Bearer <your-mcp-api-key>"
      }
    },
    "pulsarcd-actions": {
      "type": "streamable-http",
      "url": "https://your-host:8000/ai/actions/mcp",
      "headers": {
        "Authorization": "Bearer <your-mcp-api-key>"
      }
    }
  }
}
```

### Typical Workflow

1. Call `get_log_metadata()` to discover available services and hosts
2. Call `search_logs(github_project="myrepo", last_hours=24)` to browse recent logs
3. Call `build_stack(...)` to build, then `get_action_status(action_id)` to track progress
4. Call `deploy_stack(...)` to deploy once the build completes
