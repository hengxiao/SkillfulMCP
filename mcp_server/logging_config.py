"""
Structured JSON logging configuration.

Every log record emitted through `logging.getLogger(...)` includes:
  - request_id (when inside a request; "-" otherwise)
  - any `extra={}` kwargs the caller passed

Call `configure_logging()` once at startup (done by the app factory).
Use `get_logger(__name__)` everywhere else.

No external dependencies — stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any


# -- Request-scoped context --------------------------------------------------

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(request_id: str | None) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> str | None:
    return _request_id_var.get()


# -- Formatter ---------------------------------------------------------------

_STANDARD_LOGRECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class JSONFormatter(logging.Formatter):
    """One JSON object per line. Stable field order for grep-friendliness."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": _request_id_var.get() or "-",
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge any caller-supplied extras.
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


# -- Setup -------------------------------------------------------------------

_CONFIGURED = False


def configure_logging(
    level: str | int | None = None, *, force: bool = False
) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent by default: calling it a second time after successful
    configuration is a no-op. `force=True` bypasses the idempotency
    guard and reinstalls the handlers — used by `migrations/env.py`
    to recover after alembic's ``fileConfig()`` replaces root
    handlers with its own text-format console logger.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    lvl = level or os.environ.get("MCP_LOG_LEVEL", "INFO")
    if isinstance(lvl, str):
        lvl = lvl.upper()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)

    # Uvicorn has its own loggers; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper that also makes sure logging is configured."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
