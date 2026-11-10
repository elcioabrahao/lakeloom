"""MCP server over Streamable HTTP at ``/mcp`` (FR-AGT-01, FR-AGT-02, FR-AGT-05, FR-AGT-06).

Built on ``mcp`` 2.x, where ``FastMCP`` became ``mcp.server.mcpserver.MCPServer``.
Authentication is not the SDK's: :class:`~lakeloom.server.auth.BearerAuthMiddleware`
guards ``/mcp`` and stores the :class:`Principal` in the ASGI scope state, which tools
read through ``ctx.request_context.request``. Tools check their own scope and answer a
missing one with a typed ``forbidden`` error result (FR-AGT-05).

Tools: ``get_server_info`` (FR-OM-06, §7: scope ``lakeloom:read``, no side effects), the
source/scan tools of :mod:`lakeloom.server.tools_sources` (M1a) and the semantic read
tools of :mod:`lakeloom.server.tools_semantic` (M1b). Shared plumbing
(scope checks, typed errors, metrics) lives in :mod:`lakeloom.server.tools_common`.
"""

from contextlib import AbstractAsyncContextManager
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

import lakeloom
from lakeloom.config import Settings
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps, create_app
from lakeloom.server.server_info import ServerInfo, build_server_info, summarize
from lakeloom.server.tools_common import READ_SCOPE, guarded, tool_error
from lakeloom.server.tools_semantic import register_semantic_tools
from lakeloom.server.tools_sources import register_source_tools

__all__ = ["MCP_PATH", "create_mcp", "create_server_app", "tool_error", "transport_security"]

MCP_PATH = "/mcp"

GET_SERVER_INFO_DESCRIPTION = """\
Describe this Lakeloom server: its version and environment, the connected OpenMetadata \
instance (version, whether it is reachable, whether its RDF/knowledge-graph support is \
enabled), which optional features are switched on (triplestore, stdio, token_exchange; \
these are configuration flags, not a guarantee the capability is live in this release), the \
configured lake sources and semantic models, and who you are authenticated as (subject, \
principal type, granted scopes).

Call this first to learn what the server can do before using other tools. Takes no \
arguments. Read-only with no side effects; safe to call repeatedly. If OpenMetadata is \
down the call still succeeds and reports `openmetadata.reachable: false` with an error.

Requires scope `lakeloom:read`; without it the call fails with error code `forbidden`."""


def create_mcp(deps: AppDeps) -> MCPServer:
    """The MCP server with Lakeloom's tools registered."""
    mcp: MCPServer = MCPServer(
        name="lakeloom",
        title="Lakeloom",
        version=lakeloom.__version__,
        instructions=(
            "Lakeloom builds semantic models of a data lake in OpenMetadata. "
            "Start with get_server_info to see versions, enabled features and your scopes; "
            "list_sources, list_tables and describe_table explore the configured lake "
            "sources, and scan_lake_source records a scan of one. The semantic tools list_models, "
            "find_concepts, explain_concept, find_assets_for_concept, get_asset_context and "
            "export_model read the business vocabulary and what each asset means."
        ),
        log_level="WARNING",
    )

    async def get_server_info(ctx: Context) -> Annotated[CallToolResult, ServerInfo]:
        async def body(principal: Principal) -> CallToolResult:
            info = await build_server_info(deps, principal)
            return CallToolResult(
                content=[TextContent(type="text", text=summarize(info))],
                structured_content=info.model_dump(mode="json"),
            )

        return await guarded("get_server_info", ctx, READ_SCOPE, body)

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
    register_source_tools(mcp, deps)
    register_semantic_tools(mcp, deps)
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
