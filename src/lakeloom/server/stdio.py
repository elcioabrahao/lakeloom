"""stdio transport: MCP over stdin/stdout for local use (NFR-SEC-04).

The caller is the configured local principal (``LAKELOOM_STDIO_PRINCIPAL``, scopes
``LAKELOOM_STDIO_SCOPES``), an agent: local tools are run by an agent on the operator's
behalf and the operator vouches for the process (ADR 0010). No bearer token exists, so the
principal's token is empty and token exchange is unavailable. It is placed in a
contextvar that :func:`~lakeloom.server.tools_common.current_principal` falls back to when
there is no HTTP request. stdout carries only the protocol; logs go to stderr.
"""

from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.sources import close_sources, start_sources
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.server.http_app import (
    _CONNECTION_ERRORS,  # pyright: ignore[reportPrivateUsage]
    AppDeps,
)
from lakeloom.server.mcp_app import create_mcp
from lakeloom.server.tools_common import local_principal
from lakeloom.state.db import upgrade
from lakeloom.telemetry import configure_telemetry, is_active

__all__ = ["StdioStartupError", "run_stdio", "stdio_principal"]


class StdioStartupError(Exception):
    """The local server could not start (message is safe to print: never a URL or secret)."""


def stdio_principal(settings: Settings) -> Principal:
    return Principal(
        subject=settings.stdio_principal,
        type=PrincipalType.AGENT,
        client_id=None,
        username=settings.stdio_principal,
        scopes=frozenset(settings.stdio_scopes),
        groups=frozenset(),
        token=SecretStr(""),
    )


async def run_stdio(deps: AppDeps) -> None:
    """Migrate the state store, start the sources and serve MCP on stdin/stdout."""
    local_principal.set(stdio_principal(deps.settings))
    owns_telemetry = not is_active()
    telemetry = None
    try:
        telemetry = configure_telemetry(deps.settings)
        mcp = create_mcp(deps)
        try:
            await upgrade(deps.engine)
        except _CONNECTION_ERRORS as exc:
            # Never the exception text: it can embed the DB URL (NFR-SEC-11).
            raise StdioStartupError(
                f"state store unreachable ({type(exc).__name__}); check LAKELOOM_DB_URL"
            ) from None
        except Exception as exc:
            # Same rule for anything else (corrupt file, alembic error): class name only.
            raise StdioStartupError(
                f"state store migration failed ({type(exc).__name__})"
            ) from None
        await start_sources(deps.sources)
        await mcp.run_stdio_async()
    finally:
        await close_sources(deps.sources)
        if deps.idp_http is not None:
            await deps.idp_http.aclose()
        await deps.om.aclose()
        await deps.engine.dispose()
        if owns_telemetry and telemetry is not None:
            telemetry.shutdown()
