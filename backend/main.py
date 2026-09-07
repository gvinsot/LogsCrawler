"""Main entry point for PulsarCD."""

import logging
import os
import re
import uvicorn
import structlog

from .config import load_config


# A JWT still travels in the query string of the two browser clients that cannot
# set an Authorization header (the SSE action-log stream and the terminal
# WebSocket).  uvicorn writes the request line -- query string included -- to its
# access log, and this container's stdout is indexed in the log store that any
# viewer account can search, so an unredacted line hands out a live credential.
# Redact before the record is ever formatted.
_SECRET_QUERY_RE = re.compile(
    r"((?:token|api_key|password|secret)=)[^&\s\"']+", re.IGNORECASE
)


def _redact(value: str) -> str:
    """Replace secret-bearing query parameters with a placeholder."""
    return _SECRET_QUERY_RE.sub(lambda m: m.group(1) + "REDACTED", value)


class EndpointFilter(logging.Filter):
    """Drop noisy endpoints and redact secrets from uvicorn log records."""

    # Endpoints to exclude from logging (high-frequency polling)
    EXCLUDED_PATHS = [
        "/api/agent/actions",
        "/api/health",
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact in place: the record is formatted later by the handler.
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact(val) if isinstance(val, str) else val
                for key, val in record.args.items()
            }
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        message = record.getMessage()
        return not any(path in message for path in self.EXCLUDED_PATHS)


# Configure log level from environment
_log_level = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)


def main():
    """Run the PulsarCD server."""
    settings = load_config()

    # Exclude noisy endpoints and strip secrets from both uvicorn loggers:
    # the request line lands in uvicorn.access, and the WebSocket "[accepted]"
    # line -- which also carries the query string -- lands in uvicorn.error.
    for _logger_name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(_logger_name).addFilter(EndpointFilter())

    uvicorn.run(
        "backend.api:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=_log_level.lower(),
    )


if __name__ == "__main__":
    main()
