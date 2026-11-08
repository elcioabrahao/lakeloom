"""Lakeloom command line interface (NFR-OSS-01)."""

import typer

import lakeloom

app = typer.Typer(name="lakeloom", help="Lakeloom operator CLI.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Lakeloom operator CLI."""


@app.command()
def version() -> None:
    """Print the installed Lakeloom version."""
    typer.echo(lakeloom.__version__)
