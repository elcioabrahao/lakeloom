"""Typed errors shared by every tool (FR-AGT-05).

Every failure an agent can see has a stable ``code``, a human ``message`` and a ``hint``
telling the caller what to do next.
"""

from typing import Literal

__all__ = ["ErrorCode", "LakeloomError"]

ErrorCode = Literal[
    "not_found",
    "forbidden",
    "conflict",
    "validation_error",
    "upstream_unavailable",
    "rate_limited",
]


class LakeloomError(Exception):
    """A typed, agent-readable error: ``{"error": {"code", "message", "hint"}}``."""

    def __init__(self, code: ErrorCode, message: str, hint: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, dict[str, str]]:
        """The structured-content shape returned to MCP clients."""
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}
