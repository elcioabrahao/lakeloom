"""Lakeloom command line interface (NFR-OSS-01, FR-AGT-07).

``lakeloom serve`` runs the HTTP app (health, metrics and the MCP endpoint at ``/mcp``).
Configuration errors are printed to stderr and exit with code 2, without a traceback.
"""

from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError

import lakeloom
from lakeloom.config import ConfigError, get_settings
from lakeloom.logging import configure_logging

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
