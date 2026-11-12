"""Who calls a service: an MCP principal or an operator outside MCP.

:class:`Caller` is what the services need of :class:`~lakeloom.server.auth.Principal`;
:class:`Actor` is a caller outside MCP (the CLI operator, tests).
"""

from dataclasses import dataclass
from typing import Protocol

__all__ = ["Actor", "Caller"]


class Caller(Protocol):
    """Who calls: an authenticated :class:`~lakeloom.server.auth.Principal` or an :class:`Actor`."""

    @property
    def subject(self) -> str: ...

    @property
    def type(self) -> str: ...


@dataclass(frozen=True)
class Actor:
    """A caller outside MCP (CLI, tests); ``scopes`` as a token would grant them. It has no
    groups and is exempt from per-model access groups (NFR-SEC-07)."""

    subject: str
    type: str = "agent"
    scopes: frozenset[str] = frozenset()
