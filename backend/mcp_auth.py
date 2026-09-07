"""Authentication middleware for the MCP server.

Validates Bearer tokens on every MCP request before it reaches the tool handlers.
Accepts either a valid JWT (same tokens used by the web UI) or a dedicated MCP API key.

The token MUST be supplied in the ``Authorization: Bearer <token>`` header.
The historical ``?token=`` query-string fallback was removed: a token in the URL
is written verbatim to the uvicorn access log, and those logs are indexed in the
log store that any viewer account can query, which turned a leaked line into a
full credential.  Streamable-HTTP MCP clients always control their headers, so
nothing legitimate needs the fallback.

Authorization model:
- The read-only MCP server (mounted at /ai) is reachable by any authenticated user.
- The actions MCP server (mounted at /ai/actions) exposes privileged tools such as
  run_command, which grants shell access on the Swarm manager. It is mounted with
  ``require_admin=True`` so a JWT must carry ``role == "admin"``.
- Revocation is enforced exactly like on the HTTP API: /ai is exempt from
  auth_middleware, so this middleware has to check the token epoch itself.

Secrets are never written to the logs: failures are traced with a short SHA-256
prefix of the presented token, the request path and the client IP only.
"""

import hashlib
import hmac

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger()


def _token_fingerprint(token: str) -> str:
    """Return a short, non-reversible fingerprint of a token for debugging."""
    if not token:
        return "none"
    return hashlib.sha256(token.encode("utf-8", errors="ignore")).hexdigest()[:8]


class MCPAuthMiddleware:
    """ASGI middleware that validates Bearer tokens for MCP requests.

    Args:
        app: the wrapped ASGI application (an MCP server app).
        require_admin: when True, a JWT is only accepted if its ``role`` claim is
            ``admin``. The dedicated MCP API key is deliberately NOT subject to this
            check: it is a service credential, provisioned out-of-band by the
            operator (PULSARCD_MCP__API_KEY) and never handed out to end users, so it
            is treated as a full-privilege machine identity on both MCP servers.
    """

    def __init__(self, app: ASGIApp, require_admin: bool = False):
        self.app = app
        self.require_admin = require_admin

    @staticmethod
    def _client_ip(scope: Scope) -> str:
        client = scope.get("client")
        if client:
            return str(client[0])
        return "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        client_ip = self._client_ip(scope)

        # Extract Authorization header from raw ASGI headers
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")

        token = auth_header[7:] if auth_header.startswith("Bearer ") else None

        if not token:
            # A token supplied in the URL is refused on purpose: it would be
            # written to the access log, which is indexed in the log store.
            if b"token=" in scope.get("query_string", b""):
                logger.warning(
                    "MCP auth failed: token supplied in the query string",
                    path=path,
                    client_ip=client_ip,
                    detail="?token= is no longer accepted; it leaks into access logs",
                )
            response = JSONResponse(
                status_code=401,
                content={"error": "MCP authentication required. Provide an Authorization: Bearer <token> header."},
            )
            await response(scope, receive, send)
            return

        # Lazy import to get current settings (from api.py, initialized in lifespan)
        from . import api as _api
        settings = _api.settings

        # Check 1: dedicated MCP API key (constant-time comparison, service credential)
        api_key = (settings.mcp.api_key or "").strip()
        if api_key and hmac.compare_digest(token.strip().encode("utf-8"), api_key.encode("utf-8")):
            await self.app(scope, receive, send)
            return

        # Check 2: valid JWT token
        payload = None
        try:
            from .auth import decode_token
            payload = decode_token(token, settings.auth.jwt_secret)
        except Exception:
            payload = None

        if payload is None:
            logger.warning(
                "MCP auth failed: invalid token",
                path=path,
                client_ip=client_ip,
                token_fp=_token_fingerprint(token),
            )
            response = JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired token"},
            )
            await response(scope, receive, send)
            return

        # Same revocation gate as auth_middleware: a password change, a role
        # change or an account deletion bumps the account epoch, and /ai is
        # exempt from the HTTP middleware that would otherwise enforce it.
        if _api._token_is_revoked(payload):
            logger.warning(
                "MCP auth denied: revoked token",
                path=path,
                client_ip=client_ip,
                user=payload.get("sub"),
            )
            response = JSONResponse(
                status_code=401,
                content={"error": "Token has been revoked"},
            )
            await response(scope, receive, send)
            return

        if self.require_admin and payload.get("role") != "admin":
            logger.warning(
                "MCP auth denied: admin role required",
                path=path,
                client_ip=client_ip,
                user=payload.get("sub"),
                role=payload.get("role"),
            )
            response = JSONResponse(
                status_code=403,
                content={"error": "Admin role required for this MCP server"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
