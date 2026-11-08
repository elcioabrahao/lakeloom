"""CLI smoke tests (NFR-OSS-01: package is installable and exposes a console entry point)."""

from typer.testing import CliRunner

import lakeloom
from lakeloom.cli import app


def test_cli_version_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == lakeloom.__version__
