"""Plumbing shared by every MCP tool: typed errors, the caller, scope checks, metrics.

Each tool body runs through :func:`guarded`, which checks the scope (``forbidden``), turns
a :class:`~lakeloom.errors.LakeloomError` into a typed error result (FR-AGT-05), hides
any other failure behind ``upstream_unavailable`` (its text can carry upstream detail),
and counts the call in ``lakeloom_tool_calls_total`` with outcome ok/error/forbidden.
"""

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from starlette.requests import Request

from lakeloom.errors import ErrorCode, LakeloomError
from lakeloom.metrics import TOOL_CALLS
from lakeloom.server.auth import Principal

__all__ = [
    "PROPOSE_SCOPE",
    "READ_SCOPE",
    "client_name",
    "current_principal",
    "guarded",
    "local_principal",
    "principal_of",
    "tool_error",
]

READ_SCOPE = "lakeloom:read"
PROPOSE_SCOPE = "lakeloom:propose"

_log = logging.getLogger("lakeloom.mcp")

# The stdio transport has no HTTP request: its configured local principal (NFR-SEC-04) lives
# here. Set only by :func:`lakeloom.server.stdio.run_stdio`; HTTP never reads or sets it.
local_principal: ContextVar[Principal | None] = ContextVar("lakeloom_local_principal", default=None)


def tool_error(code: ErrorCode, message: str, hint: str) -> CallToolResult:
    """A typed tool error (FR-AGT-05): ``{"error": {"code", "message", "hint"}}``."""
    error = LakeloomError(code, message, hint)
    return CallToolResult(
        content=[TextContent(type="text", text=f"Error `{code}`: {message}. Hint: {hint}.")],
        structured_content=error.to_dict(),
        is_error=True,
    )


def current_principal(ctx: Context) -> Principal | None:
    """The caller: the HTTP principal :class:`BearerAuthMiddleware` stored in the ASGI state.

    Without an HTTP request (stdio) it is the configured local principal, if any.
    """
    request = ctx.request_context.request
    if not isinstance(request, Request):
        return local_principal.get()
    state: dict[str, Any] = request.scope.get("state") or {}
    principal = state.get("principal")
    return principal if isinstance(principal, Principal) else None


principal_of = current_principal  # the name tools used before the shared helper


def client_name(ctx: Context) -> str | None:
    """The MCP client's name from ``initialize`` (``clientInfo.name``), if it sent one."""
    try:
        params = ctx.session.client_params
    except (AttributeError, LookupError, ValueError):
        return None
    if params is None:
        return None
    name = params.client_info.name
    return name or None


async def guarded(
    tool: str,
    ctx: Context,
    scope: str,
    body: Callable[[Principal], Awaitable[CallToolResult]],
) -> CallToolResult:
    """Run ``body`` for a caller holding ``scope``; typed errors and metrics around it."""
    principal = current_principal(ctx)
    if principal is None or scope not in principal.scopes:
        TOOL_CALLS.labels(tool=tool, outcome="forbidden").inc()
        return tool_error("forbidden", f"{tool} requires the {scope} scope", f"token lacks {scope}")
    try:
        result = await body(principal)
    except LakeloomError as exc:
        TOOL_CALLS.labels(tool=tool, outcome="error").inc()
        return tool_error(exc.code, exc.message, exc.hint)
    except Exception as exc:
        TOOL_CALLS.labels(tool=tool, outcome="error").inc()
        # Only the type: the text may carry upstream detail or a connection URL (NFR-SEC-11).
        _log.error("tool %s failed unexpectedly (%s)", tool, type(exc).__name__)
        return tool_error(
            "upstream_unavailable",
            f"{tool} failed unexpectedly ({type(exc).__name__})",
            "retry; if it keeps failing, check the Lakeloom server logs",
        )
    TOOL_CALLS.labels(tool=tool, outcome="ok").inc()
    return result
