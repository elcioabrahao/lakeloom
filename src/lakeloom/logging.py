"""JSON logging with a per-request id (NFR-OPS-03, NFR-SEC-11).

One JSON object per line on stdout: ``ts``, ``level``, ``logger``, ``message``,
``request_id`` (plus ``trace_id``/``span_id`` inside an active span) and any ``extra``
fields. Callers must never pass secrets or request headers as log fields.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

from opentelemetry import trace

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            data["trace_id"] = format(span_context.trace_id, "032x")
            data["span_id"] = format(span_context.span_id, "016x")
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in data:
                data[key] = value
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """Install a single JSON handler (stdout by default) on the root logger (idempotent)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_lakeloom", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(JsonFormatter())
    handler._lakeloom = True  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
    root.addHandler(handler)
    root.setLevel(level.upper())
