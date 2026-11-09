"""Lakeloom command line interface (NFR-OSS-01, FR-AGT-07).

``lakeloom serve`` runs the HTTP app (health, metrics and the MCP endpoint at ``/mcp``).
``lakeloom sources``, ``lakeloom tables SOURCE`` and ``lakeloom scan SOURCE`` use the
sources in ``LAKELOOM_CONFIG_FILE`` directly, as the operator (FR-AGT-07, FR-SRC-10).
Configuration errors are printed to stderr and exit with code 2, without a traceback;
typed errors (``not_found``, ``upstream_unavailable``, ...) exit with code 1.
"""

import asyncio
import getpass
import sys
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any

import typer
import uvicorn
from pydantic import ValidationError

import lakeloom
from lakeloom.config import ConfigError, Settings, get_settings
from lakeloom.errors import LakeloomError
from lakeloom.logging import configure_logging
from lakeloom.untrusted import single_line

if TYPE_CHECKING:
    from lakeloom.sources.base import LakeSource

CLI_CLIENT = "lakeloom-cli"

app = typer.Typer(name="lakeloom", help="Lakeloom operator CLI.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Lakeloom operator CLI."""


@app.command()
def version() -> None:
    """Print the installed Lakeloom version."""
    typer.echo(lakeloom.__version__)


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind address [default: LAKELOOM_HOST or 0.0.0.0].")
    ] = None,
    port: Annotated[
        int | None, typer.Option(help="Bind port [default: LAKELOOM_PORT or 8000].")
    ] = None,
) -> None:
    """Serve the MCP endpoint (/mcp), health checks and metrics over HTTP."""
    # Imported here so `lakeloom version` stays fast and needs no server dependencies.
    from lakeloom.server.http_app import build_deps
    from lakeloom.server.mcp_app import create_server_app

    try:
        settings = get_settings()
        configure_logging(settings.log_level)
        server_app = create_server_app(build_deps(settings))
    except (ConfigError, ValidationError) as exc:
        # Settings hide input values in validation errors, so no secret is echoed.
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    uvicorn.run(
        server_app,
        host=settings.host if host is None else host,
        port=settings.port if port is None else port,
        log_config=None,
        # RequestContextMiddleware writes the access log; uvicorn's would duplicate it.
        access_log=False,
    )


db_app = typer.Typer(name="db", help="State store commands.", no_args_is_help=True)
app.add_typer(db_app)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply state store migrations (Alembic upgrade head)."""
    from lakeloom.state.db import create_engine, upgrade

    try:
        settings = get_settings()
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    async def _run() -> None:
        engine = create_engine(settings.db_url)
        try:
            await upgrade(engine)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:
        # The DB URL may hold a password, so only the error type is printed.
        typer.echo(f"state store upgrade failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("state store at head")


# --- lake sources (operator side; the same operations as the MCP source tools) -------------


def _cli_settings() -> Settings:
    try:
        settings = get_settings()
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    # stdout carries the command's output; logs go to stderr, WARNING and up unless DEBUG.
    debug = settings.log_level.upper() == "DEBUG"
    configure_logging("DEBUG" if debug else "WARNING", stream=sys.stderr)
    return settings


@asynccontextmanager
async def _open_sources(settings: Settings) -> AsyncGenerator[dict[str, "LakeSource"]]:
    from lakeloom.core.sources import close_sources, start_sources
    from lakeloom.server.http_app import load_lake_sources

    sources = load_lake_sources(settings)
    try:
        await start_sources(sources)
        yield sources
    finally:
        await close_sources(sources)


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Run ``coro``; typed errors exit 1, configuration errors exit 2."""
    try:
        asyncio.run(coro)
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except LakeloomError as exc:
        typer.echo(f"error {exc.code}: {exc.message} (hint: {exc.hint})", err=True)
        raise typer.Exit(code=1) from None


def _operator() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # no passwd entry (e.g. an arbitrary container uid)
        return "operator"


@app.command("sources")
def sources_cmd() -> None:
    """List the configured lake sources with kind and availability."""
    from lakeloom.core.sources import source_status

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_sources(settings) as sources:
            if not sources:
                typer.echo("no sources configured (set LAKELOOM_CONFIG_FILE)")
            for name, src in sources.items():
                status = source_status(name, src)
                state = "available"
                if status.error is not None:
                    state = f"unavailable ({status.error.code}: {status.error.hint})"
                typer.echo(f"{single_line(name)}\t{single_line(status.kind)}\t{state}")

    _run(_go())


@app.command("tables")
def tables_cmd(
    source: Annotated[str, typer.Argument(help="Configured source name.")],
    catalog: Annotated[str | None, typer.Option(help="Only this catalog.")] = None,
    schema: Annotated[str | None, typer.Option(help="Only this schema.")] = None,
) -> None:
    """List a source's tables with their current versions."""
    from lakeloom.core.sources import get_source, list_all_tables

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_sources(settings) as sources:
            src = get_source(sources, source)
            for ref in await list_all_tables(src, catalog, schema):
                try:
                    v = (await src.get_table_version(ref)).version
                    version = "unknown" if v is None else str(v)
                except LakeloomError as exc:
                    version = f"error {exc.code}"
                typer.echo(f"{single_line(ref.full_name)}\t{version}")

    _run(_go())


@app.command("scan")
def scan_cmd(
    source: Annotated[str, typer.Argument(help="Configured source name.")],
    profile: Annotated[bool, typer.Option("--profile", help="Also profile columns.")] = False,
    table: Annotated[
        list[str] | None, typer.Option("--table", help="Only this table (repeatable).")
    ] = None,
    catalog: Annotated[
        list[str] | None, typer.Option("--catalog", help="Only this catalog (repeatable).")
    ] = None,
    schema: Annotated[
        list[str] | None, typer.Option("--schema", help="Only this schema (repeatable).")
    ] = None,
) -> None:
    """Scan a source, store the scan record and print its summary."""
    from pydantic import SecretStr

    from lakeloom.core.scans import ScanService, summarize
    from lakeloom.om.client import OMClient
    from lakeloom.server.auth import Principal, PrincipalType
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.scans import ScanRepository

    settings = _cli_settings()
    principal = Principal(
        subject=_operator(),
        type=PrincipalType.HUMAN,
        client_id=CLI_CLIENT,
        username=None,
        scopes=frozenset(),
        groups=frozenset(),
        token=SecretStr(""),
    )

    async def _go() -> None:
        async with _open_sources(settings) as sources:
            engine = create_engine(settings.db_url)
            om = (
                None
                if settings.om_url is None
                else OMClient(str(settings.om_url), settings.om_bot_token)
            )
            try:
                await upgrade(engine)
                service = ScanService(sources, ScanRepository(engine), om, settings)
                record = await service.scan(
                    source,
                    catalogs=catalog,
                    schemas=schema,
                    tables=table,
                    profile=profile,
                    principal=principal,
                    client=CLI_CLIENT,
                )
            finally:
                if om is not None:
                    await om.aclose()
                await engine.dispose()
            typer.echo(summarize(record), nl=False)
            if record.status == "failed":
                raise typer.Exit(code=1)

    try:
        _run(_go())
    except typer.Exit:
        raise
    except Exception as exc:
        # Like `db upgrade`: no traceback, and only the error type (the message may hold
        # a DB URL or upstream text).
        typer.echo(f"scan failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from None
