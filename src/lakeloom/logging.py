"""JSON logging with a per-request id (NFR-OPS-03, NFR-SEC-11).

One JSON object per line on stdout: ``ts``, ``level``, ``logger``, ``message``,
``request_id`` plus any ``extra`` fields. Callers must never pass secrets or request
headers as log fields.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

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
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in data:
                data[key] = value
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging(level: str) -> None:
    """Install a single JSON stdout handler on the root logger (idempotent)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_lakeloom", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._lakeloom = True  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
    root.addHandler(handler)
    root.setLevel(level.upper())
