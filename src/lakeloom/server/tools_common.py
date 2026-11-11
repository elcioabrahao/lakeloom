"""Plumbing shared by every MCP tool: typed errors, the caller, scope checks, metrics.

Each tool body runs through :func:`guarded`, which checks the scope (``forbidden``), turns
a :class:`~lakeloom.errors.LakeloomError` into a typed error result (FR-AGT-05), hides
any other failure behind ``upstream_unavailable`` (its text can carry upstream detail),
and counts the call in ``lakeloom_tool_calls_total`` with outcome ok/error/forbidden.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, cast

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from opentelemetry.trace import Status, StatusCode
from starlette.requests import Request

from lakeloom.core.audit import AuditContext, AuditRecord, audit_context, record
from lakeloom.errors import ErrorCode, LakeloomError
from lakeloom.metrics import TOOL_CALLS
from lakeloom.server.auth import Principal
from lakeloom.state.audit import AuditRepository
from lakeloom.telemetry import record_tool_call, start_span

__all__ = [
    "PROPOSE_SCOPE",
    "READ_SCOPE",
    "client_name",
    "current_principal",
    "guarded",
    "local_principal",
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
    *,
    audited: bool = False,
    audit: AuditRepository | None = None,
) -> CallToolResult:
    """Run ``body`` for a caller holding ``scope`` inside a span; typed errors and metrics.

    ``audited=True`` (writes and review decisions, NFR-SEC-12) also writes an audit record
    with the outcome, to the JSON log and the state store (``audit`` is then required: the
    call fails closed with ``RuntimeError`` otherwise). The body can set the model / proposal
    / change set through ``current_audit()``; arguments are never recorded. An audit failure
    never fails the call, and a cancelled call is still audited (error_code ``cancelled``).
    """
    if audited and audit is None:
        raise RuntimeError(f"tool {tool} is audited but no audit repository was passed")
    principal = current_principal(ctx)
    audit_ctx = AuditContext()
    token = audit_context.set(audit_ctx)
    # Only the tool name, outcome and principal type: never arguments (NFR-SEC-11).
    with start_span(
        f"mcp.tool/{tool}",
        attributes={
            "lakeloom.tool": tool,
            "lakeloom.principal_type": "unknown" if principal is None else principal.type.value,
        },
    ) as span:
        result, outcome = CallToolResult(content=[]), "error"
        try:
            result, outcome = await _run(tool, scope, principal, body)
        except BaseException:
            # Cancelled (or otherwise aborted) writes are audited too.
            if audited:
                await asyncio.shield(
                    _audit(tool, ctx, principal, audit_ctx, "error", "cancelled", audit)
                )
            raise
        finally:
            audit_context.reset(token)
        span.set_attribute("lakeloom.outcome", outcome)
        if outcome != "ok":
            span.set_status(Status(StatusCode.ERROR, outcome))
    if audited:
        code = _error_code(result)
        status = "ok" if outcome == "ok" else "forbidden" if code == "forbidden" else "error"
        await asyncio.shield(_audit(tool, ctx, principal, audit_ctx, status, code, audit))
    TOOL_CALLS.labels(tool=tool, outcome=outcome).inc()
    record_tool_call(tool, outcome)
    return result


async def _run(
    tool: str,
    scope: str,
    principal: Principal | None,
    body: Callable[[Principal], Awaitable[CallToolResult]],
) -> tuple[CallToolResult, str]:
    if principal is None or scope not in principal.scopes:
        return (
            tool_error("forbidden", f"{tool} requires the {scope} scope", f"token lacks {scope}"),
            "forbidden",
        )
    try:
        return await body(principal), "ok"
    except LakeloomError as exc:
        return tool_error(exc.code, exc.message, exc.hint), "error"
    except Exception as exc:
        # Only the type: the text may carry upstream detail or a connection URL (NFR-SEC-11).
        _log.error("tool %s failed unexpectedly (%s)", tool, type(exc).__name__)
        return (
            tool_error(
                "upstream_unavailable",
                f"{tool} failed unexpectedly ({type(exc).__name__})",
                "retry; if it keeps failing, check the Lakeloom server logs",
            ),
            "error",
        )


def _error_code(result: CallToolResult) -> str | None:
    structured: dict[str, Any] = result.structured_content or {}
    error = cast(dict[str, object], structured.get("error") or {})
    code = error.get("code")
    return code if isinstance(code, str) else None


async def _audit(
    tool: str,
    ctx: Context,
    principal: Principal | None,
    audit_ctx: AuditContext,
    status: Literal["ok", "error", "forbidden"],
    code: str | None,
    audit: AuditRepository | None,
) -> None:
    try:
        rec = AuditRecord(
            at=datetime.now(UTC),
            principal_subject="anonymous" if principal is None else principal.subject,
            principal_type="unknown" if principal is None else principal.type.value,
            client=client_name(ctx),
            tool=tool,
            model=audit_ctx.model,
            proposal_id=audit_ctx.proposal_id,
            change_set_id=audit_ctx.change_set_id,
            result=status,
            error_code=code,
        )
    except Exception as exc:
        _log.error("audit record for %s failed (%s)", tool, type(exc).__name__)
        return
    await record(audit, rec)
