"""Audit records for writes and review decisions (NFR-SEC-12).

One JSON line on the ``lakeloom.audit`` logger and one ``audit_log`` row per record. A record
never carries tool arguments or free text: only the fields below. Tool bodies enrich it
(model, proposal, change set) through :func:`current_audit`; the values come from the tool's
own validated state, never from raw arguments.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationInfo, field_validator

from lakeloom.state.audit import AuditRepository

__all__ = ["AuditContext", "AuditRecord", "audit_context", "current_audit", "record"]

_log = logging.getLogger("lakeloom.audit")
_errors = logging.getLogger("lakeloom.audit.errors")


# Column widths of ``audit_log``: client-controlled values are cut to fit, so PostgreSQL never
# rejects (and the record is never lost on) an oversized client name.
MAX_LENGTHS = {
    "principal_subject": 255,
    "principal_type": 64,
    "client": 255,
    "tool": 128,
    "model": 255,
    "proposal_id": 64,
    "change_set_id": 255,
    "error_code": 64,
}


class AuditRecord(BaseModel):
    at: datetime
    principal_subject: str
    principal_type: str
    client: str | None = None
    tool: str
    model: str | None = None
    proposal_id: str | None = None
    change_set_id: str | None = None
    result: Literal["ok", "error", "forbidden"]
    error_code: str | None = None

    @field_validator(*MAX_LENGTHS, mode="after")
    @classmethod
    def _fit_column(cls, value: str | None, info: ValidationInfo) -> str | None:
        """NUL removed (PostgreSQL text rejects it), then cut to the column width."""
        if value is None or info.field_name is None:
            return value
        return value.replace("\x00", "")[: MAX_LENGTHS[info.field_name]]


@dataclass
class AuditContext:
    """Set by a tool body; read by ``guarded`` when it writes the record.

    ``error_code`` marks a partial success: the call returns a result, but the record says
    ``error`` with this code (e.g. a proposal part that could not be created)."""

    model: str | None = None
    proposal_id: str | None = None
    change_set_id: str | None = None
    error_code: str | None = None


audit_context: ContextVar[AuditContext | None] = ContextVar("lakeloom_audit_context", default=None)


def current_audit() -> AuditContext:
    """The audit context of the running guarded call (a detached one outside a call)."""
    return audit_context.get() or AuditContext()


async def record(repo: AuditRepository | None, rec: AuditRecord) -> None:
    """Log the JSON line and store the row. Never raises (audit must not fail the call)."""
    try:
        _log.info("audit", extra=rec.model_dump(mode="json"))
    except Exception as exc:
        _errors.error("audit log line failed (%s)", type(exc).__name__)
    if repo is None:
        return
    try:
        await repo.add(rec)
    except Exception as exc:
        # Only the type: the text can carry a connection URL (NFR-SEC-11).
        _errors.error("audit store write failed (%s)", type(exc).__name__)
