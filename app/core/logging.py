"""Structured logging with request-ID correlation context."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict

request_id_ctx_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON with request_id and timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_ctx_var.get("-"),
            "message": record.getMessage(),
        }
        if hasattr(record, "run_id"):
            payload["run_id"] = getattr(record, "run_id")
        if hasattr(record, "stage"):
            payload["stage"] = getattr(record, "stage")
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Configure root and app loggers with structured formatting."""
    root_logger = logging.getLogger()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    if not any(isinstance(h.formatter, StructuredJsonFormatter) for h in root_logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredJsonFormatter())
        root_logger.handlers = [handler]


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)
