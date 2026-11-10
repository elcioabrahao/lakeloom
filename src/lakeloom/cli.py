"""Lakeloom command line interface (NFR-OSS-01, FR-AGT-07).

``lakeloom serve`` runs the HTTP app (health, metrics and the MCP endpoint at ``/mcp``).
``lakeloom sources``, ``lakeloom tables SOURCE`` and ``lakeloom scan SOURCE`` use the
sources in ``LAKELOOM_CONFIG_FILE`` directly, as the operator (FR-AGT-07, FR-SRC-10).
Configuration errors are printed to stderr and exit with code 2, without a traceback;
``lakeloom models``, ``concept``, ``context`` and ``export`` read the semantic models in
OpenMetadata (FR-CTX-01..04, FR-IO-01). Typed errors (``not_found``,
``upstream_unavailable``, ...) exit with code 1.
"""

import asyncio
import getpass
import sys
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import typer
import uvicorn
from pydantic import ValidationError

import lakeloom
from lakeloom.config import ConfigError, Settings, get_settings
from lakeloom.errors import LakeloomError
from lakeloom.logging import configure_logging
from lakeloom.untrusted import single_line

if TYPE_CHECKING:
    from lakeloom.core.models import ModelService
    from lakeloom.om.client import OMClient
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
    transport: Annotated[
        Literal["http", "stdio"],
        typer.Option(help="http: /mcp, health and metrics; stdio: local MCP over stdin/stdout."),
    ] = "http",
) -> None:
    """Serve the MCP endpoint (/mcp), health checks and metrics over HTTP, or MCP over stdio."""
    # Imported here so `lakeloom version` stays fast and needs no server dependencies.
    from lakeloom.server.http_app import build_deps
    from lakeloom.server.mcp_app import create_server_app

    if transport == "stdio":
        _serve_stdio()
        return
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


def _serve_stdio() -> None:
    """MCP over stdio as the configured local principal (NFR-SEC-04); stdout is protocol only."""
    from lakeloom.server.http_app import build_deps
    from lakeloom.server.stdio import StdioStartupError, run_stdio

    try:
        settings = get_settings()
        configure_logging(settings.log_level, stream=sys.stderr)
        if not settings.stdio_enabled:
            typer.echo("stdio transport is disabled (set LAKELOOM_STDIO_ENABLED=true)", err=True)
            raise typer.Exit(code=2)
        deps = build_deps(settings, http_auth=False)
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    try:
        asyncio.run(run_stdio(deps))
    except StdioStartupError as exc:
        typer.echo(f"stdio startup failed: {exc}", err=True)
        raise typer.Exit(code=1) from None


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
    from lakeloom.core.scans import ScanService, summarize
    from lakeloom.om.client import OMClient
    from lakeloom.server.auth import PrincipalType
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.scans import ScanRepository

    settings = _cli_settings()
    operator = _operator()

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
                    principal_subject=operator,
                    principal_type=PrincipalType.HUMAN.value,
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


# --- semantic read path (operator side; the same operations as the MCP semantic tools) -----


@asynccontextmanager
async def _open_semantic(
    settings: Settings, *, with_sources: bool = False
) -> AsyncGenerator[tuple["OMClient", "ModelService", dict[str, "LakeSource"]]]:
    from lakeloom.core.models import ModelService
    from lakeloom.om.client import OMClient

    if settings.om_url is None:
        raise ConfigError("LAKELOOM_OM_URL is required")
    om = OMClient(str(settings.om_url), settings.om_bot_token)
    try:
        if with_sources:
            async with _open_sources(settings) as sources:
                yield om, ModelService(om, settings), sources
        else:
            yield om, ModelService(om, settings), {}
    finally:
        await om.aclose()


@app.command("models")
def models_cmd() -> None:
    """List the semantic models (OpenMetadata glossaries)."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings) as (_, models, _s):
            cursor: str | None = None
            while True:
                page = await models.list_models(cursor, 100)
                for m in page.items:
                    terms = "?" if m.term_count is None else str(m.term_count)
                    typer.echo(f"{single_line(m.slug)}\t{terms}\t{single_line(m.base_iri or '')}")
                cursor = page.next_cursor
                if cursor is None:
                    break

    _run(_go())


@app.command("concept")
def concept_cmd(
    query: Annotated[str, typer.Argument(help="Concept name, FQN or IRI, or a search text.")],
    max_tokens: Annotated[int | None, typer.Option(help="Shorten the output.")] = None,
) -> None:
    """Explain a concept; when QUERY is not one concept, list the matching concepts."""
    from lakeloom.core.concepts import concept_markdown, explain_concept, find_concepts

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings) as (om, models, _s):
            try:
                detail = await explain_concept(models, om, query)
            except LakeloomError as exc:
                # not_found or ambiguous: fall back to a search; other errors are real.
                if exc.code not in ("not_found", "validation_error"):
                    raise
                page = await find_concepts(models, om, query)
                if not page.items:
                    raise
                for m in page.items:
                    c = m.concept
                    typer.echo(
                        f"{single_line(c.fqn)}\t{single_line(c.label)}\t{m.matched_on}"
                        f"\t{m.score:.2f}"
                    )
                return
            text, _ = concept_markdown(detail, max_tokens=max_tokens)
            typer.echo(text, nl=False)

    _run(_go())


@app.command("context")
def context_cmd(
    asset: Annotated[str, typer.Argument(help="OM FQN, Unity Catalog name or storage URI.")],
    format: Annotated[
        Literal["md", "json", "jsonld"], typer.Option("--format", help="Output format.")
    ] = "md",
    max_tokens: Annotated[int | None, typer.Option(help="Cut the output to ~N tokens.")] = None,
) -> None:
    """Print the semantic context of an asset (Markdown by default)."""
    import json

    from lakeloom.core.context import (
        get_asset_context,
        render_json,
        render_jsonld,
        render_markdown,
    )

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings, with_sources=True) as (om, models, sources):
            context = await get_asset_context(asset, models=models, om=om, sources=sources)
            if format == "md":
                typer.echo(render_markdown(context, max_tokens=max_tokens).payload, nl=False)
                return
            if format == "json":
                payload = render_json(context, max_tokens=max_tokens).payload
            else:
                info = await models.get_model(context.model) if context.model else None
                payload = render_jsonld(context, info, max_tokens=max_tokens).payload
            typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))

    _run(_go())


@app.command("export")
def export_cmd(
    model: Annotated[str, typer.Argument(help="Model slug (see `lakeloom models`).")],
    format: Annotated[
        Literal["turtle", "rdfxml", "ntriples", "jsonld", "skos"],
        typer.Option("--format", help="RDF serialisation."),
    ] = "turtle",
    out: Annotated[Path | None, typer.Option("--out", help="Write here instead of stdout.")] = None,
) -> None:
    """Export a model as deterministic RDF; the bytes are written exactly."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings) as (_, models, _s):
            data = await models.export(model, format)
        if out is None:
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            try:
                out.write_bytes(data)
            except OSError as exc:
                typer.echo(f"cannot write {out} ({type(exc).__name__})", err=True)
                raise typer.Exit(code=1) from None

    _run(_go())


if __name__ == "__main__":
    app()
