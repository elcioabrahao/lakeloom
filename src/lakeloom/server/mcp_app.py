"""MCP server over Streamable HTTP at ``/mcp`` (FR-AGT-01, FR-AGT-02, FR-AGT-05, FR-AGT-06).

Built on ``mcp`` 2.x, where ``FastMCP`` became ``mcp.server.mcpserver.MCPServer``.
Authentication is not the SDK's: :class:`~lakeloom.server.auth.BearerAuthMiddleware`
guards ``/mcp`` and stores the :class:`Principal` in the ASGI scope state, which tools
read through ``ctx.request_context.request``. Tools check their own scope and answer a
missing one with a typed ``forbidden`` error result (FR-AGT-05).

M0 registers one tool, ``get_server_info`` (FR-OM-06, §7: scope ``lakeloom:read``,
no side effects).
"""

from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.requests import Request

import lakeloom
from lakeloom.config import Settings
from lakeloom.metrics import TOOL_CALLS
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps, create_app
from lakeloom.server.server_info import ServerInfo, build_server_info, summarize

__all__ = ["MCP_PATH", "create_mcp", "create_server_app", "transport_security"]

MCP_PATH = "/mcp"
READ_SCOPE = "lakeloom:read"

ErrorCode = Literal[
    "not_found",
    "forbidden",
    "conflict",
    "validation_error",
    "upstream_unavailable",
    "rate_limited",
]

GET_SERVER_INFO_DESCRIPTION = """\
Describe this Lakeloom server: its version and environment, the connected OpenMetadata \
instance (version, whether it is reachable, whether its RDF/knowledge-graph support is \
enabled), which optional features are on (triplestore, stdio, token_exchange), the \
configured lake sources and semantic models, and who you are authenticated as (subject, \
principal type, granted scopes).

Call this first to learn what the server can do before using other tools. Takes no \
arguments. Read-only with no side effects; safe to call repeatedly. If OpenMetadata is \
down the call still succeeds and reports `openmetadata.reachable: false` with an error.

Requires scope `lakeloom:read`; without it the call fails with error code `forbidden`."""


def tool_error(code: ErrorCode, message: str, hint: str) -> CallToolResult:
    """A typed tool error (FR-AGT-05): ``{"error": {"code", "message", "hint"}}``."""
    return CallToolResult(
        content=[TextContent(type="text", text=f"Error `{code}`: {message}. Hint: {hint}.")],
        structured_content={"error": {"code": code, "message": message, "hint": hint}},
        is_error=True,
    )


def _principal(ctx: Context) -> Principal | None:
    request = ctx.request_context.request
    if not isinstance(request, Request):
        return None
    state: dict[str, Any] = request.scope.get("state") or {}
    principal = state.get("principal")
    return principal if isinstance(principal, Principal) else None


def create_mcp(deps: AppDeps) -> MCPServer:
    """The MCP server with Lakeloom's tools registered."""
    mcp: MCPServer = MCPServer(
        name="lakeloom",
        title="Lakeloom",
        version=lakeloom.__version__,
        instructions=(
            "Lakeloom builds semantic models of a data lake in OpenMetadata. "
            "Start with get_server_info to see versions, enabled features and your scopes."
        ),
        log_level="WARNING",
    )

    async def get_server_info(ctx: Context) -> Annotated[CallToolResult, ServerInfo]:
        principal = _principal(ctx)
        if principal is None or READ_SCOPE not in principal.scopes:
            TOOL_CALLS.labels(tool="get_server_info", outcome="forbidden").inc()
            return tool_error(
                "forbidden",
                f"get_server_info requires the {READ_SCOPE} scope",
                f"token lacks {READ_SCOPE}",
            )
        try:
            info = await build_server_info(deps, principal)
        except Exception:
            TOOL_CALLS.labels(tool="get_server_info", outcome="error").inc()
            raise
        TOOL_CALLS.labels(tool="get_server_info", outcome="ok").inc()
        return CallToolResult(
            content=[TextContent(type="text", text=summarize(info))],
            structured_content=info.model_dump(mode="json"),
        )

    mcp.add_tool(
        get_server_info,
        name="get_server_info",
        title="Get server info",
        description=GET_SERVER_INFO_DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    return mcp


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection: accept the public host plus loopback names (any port)."""
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    origins: list[str] = []
    for loopback in hosts:
        origins += [f"http://{loopback}:*", f"http://{loopback}", f"https://{loopback}:*"]
    allowed_hosts = [h for name in hosts for h in (name, f"{name}:*")]
    if settings.public_base_url:
        public = urlsplit(settings.public_base_url)
        if public.netloc:
            allowed_hosts += [public.netloc, f"{public.hostname}:*"]
            origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=origins,
    )


def create_server_app(deps: AppDeps) -> FastAPI:
    """The complete Lakeloom HTTP app: health, metrics, auth and the MCP endpoint."""
    mcp = create_mcp(deps)
    # The Starlette app routes on the full path, so it is served as an exact-path
    # endpoint at /mcp (never /mcp/mcp). Its own lifespan is not used by a Route, so
    # the session manager runs in the FastAPI lifespan instead.
    mcp_http = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=transport_security(deps.settings),
    )

    def session_manager() -> AbstractAsyncContextManager[None]:
        return mcp.session_manager.run()

    return create_app(deps, endpoints=[(MCP_PATH, mcp_http)], lifespans=[session_manager])
