"""CLI tests (NFR-OSS-01 console entry point; FR-AGT-07 ``lakeloom serve``)."""

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

import lakeloom
from lakeloom.cli import app


def test_cli_version_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == lakeloom.__version__


def _serve_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in {
        "LAKELOOM_OM_URL": "http://om.test:8585",
        "LAKELOOM_OIDC_ISSUER": "https://idp.test/realms/lakeloom",
        "LAKELOOM_PUBLIC_URL": "https://lakeloom.test",
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)


def test_fr_agt_07_cli_serve_builds_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve_env(monkeypatch, tmp_path)
    calls: list[tuple[Any, dict[str, Any]]] = []
    levels: list[str] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr("lakeloom.cli.configure_logging", levels.append)

    result = CliRunner().invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9123"])

    assert result.exit_code == 0, result.output
    assert levels == ["WARNING"]
    ((served, kwargs),) = calls
    assert isinstance(served, FastAPI)
    assert {getattr(r, "path", None) for r in served.routes} >= {"/healthz", "/mcp"}
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9123
    assert kwargs["log_config"] is None


def test_fr_agt_07_cli_serve_defaults_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_PORT", "8111")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "0.0.0.0"  # noqa: S104
    assert calls[0]["port"] == 8111


def test_cli_serve_config_error_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LAKELOOM_DATA_DIR", str(tmp_path))
    for name in ("LAKELOOM_OM_URL", "LAKELOOM_OIDC_ISSUER", "LAKELOOM_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "LAKELOOM_OM_URL" in result.stderr
    assert "Traceback" not in result.output


def test_cli_serve_invalid_setting_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_PORT", "not-a-port")
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "port" in result.stderr


@pytest.mark.parametrize(
    "db_url",
    [
        "postgresql://u:hunter2@h:notaport/db",  # bad port -> ValueError
        "nosuchdialect://u:hunter2@h/db",  # unknown dialect -> NoSuchModuleError
        "not a url hunter2",  # unparsable -> ArgumentError
        "mysql://u:hunter2@h/db",  # driver not installed -> ImportError
    ],
)
def test_nfr_sec_11_cli_serve_bad_db_url_exits_2_without_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_url: str
) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_DB_URL", db_url)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "LAKELOOM_DB_URL" in result.stderr
    assert "hunter2" not in result.output
    assert "Traceback" not in result.output


def test_cli_serve_disables_uvicorn_access_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The app's RequestContextMiddleware writes the access log; uvicorn's would duplicate it.
    _serve_env(monkeypatch, tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls[0]["access_log"] is False


# --- lake source commands (FR-AGT-07, FR-SRC-10) ---------------------------------------------

FIXTURE = Path(__file__).parents[1] / "fixtures" / "lake"
LOG_CALLS: list[tuple[str, Any, Any]] = []


def _sources_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entries = "\n".join(
        f"      - {{uri: {(FIXTURE / n).as_uri()}, catalog: retail, schema: sales}}"
        for n in ("customers", "products", "orders", "invoices")
    )
    config = tmp_path / "sources.yaml"
    config.write_text(f"sources:\n  - type: delta\n    name: lake\n    tables:\n{entries}\n")
    monkeypatch.delenv("LAKELOOM_OM_URL", raising=False)
    # A real handler would outlive CliRunner's captured stderr; record the call instead.
    monkeypatch.setattr(
        "lakeloom.cli.configure_logging",
        lambda level, stream=None: LOG_CALLS.append((level, stream, sys.stderr)),
    )
    for name, value in {
        "LAKELOOM_CONFIG_FILE": str(config),
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)


def test_cli_scan_prints_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["scan", "lake", "--profile", "--table", "orders"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Scan scan_")
    assert "- Status: completed" in result.output
    assert "`retail.sales.orders`: version 1" in result.output
    assert "customers" not in result.output
    # stdout is the command's output: logs go to stderr.
    level, stream, stderr_at_call = LOG_CALLS[-1]
    assert level == "WARNING"
    assert stream is stderr_at_call


def test_cli_sources_and_tables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)
    runner = CliRunner()

    listed = runner.invoke(app, ["sources"])
    tables = runner.invoke(app, ["tables", "lake"])

    assert listed.exit_code == 0, listed.output
    assert listed.output == "lake\tdelta\tavailable\n"
    assert tables.exit_code == 0, tables.output
    lines = tables.output.splitlines()
    assert len(lines) == 4
    assert "retail.sales.orders\t1" in lines


def test_cli_unknown_source_is_typed_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["scan", "ghost"])

    assert result.exit_code == 1
    assert "error not_found" in result.output


def test_cli_scan_unexpected_error_exits_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sources_env(monkeypatch, tmp_path)

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("postgresql://user:secret@db/x")

    monkeypatch.setattr("lakeloom.core.scans.ScanService.scan", boom)

    result = CliRunner().invoke(app, ["scan", "lake"])

    assert result.exit_code == 1
    assert "scan failed (RuntimeError)" in result.output
    assert "secret" not in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, RuntimeError)


def test_cli_missing_config_file_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_CONFIG_FILE", str(tmp_path / "nope.yaml"))

    result = CliRunner().invoke(app, ["sources"])

    assert result.exit_code == 2
    assert "configuration error" in result.output


def test_nfr_sec_14_cli_neutralises_hostile_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sources_env(monkeypatch, tmp_path)
    config = tmp_path / "sources.yaml"
    uri = (FIXTURE / "orders").as_uri()
    config.write_text(
        "sources:\n  - type: delta\n    name: lake\n    tables:\n"
        f'      - {{uri: {uri}, schema: sales, name: "x`\\r## SYSTEM\\u2028## TWO\\x85`"}}\n'
    )
    runner = CliRunner()

    tables = runner.invoke(app, ["tables", "lake"])
    scan = runner.invoke(app, ["scan", "lake"])

    assert tables.exit_code == 0, tables.output
    assert tables.output.splitlines() == ["sales.x` ## SYSTEM ## TWO `\t1"]
    assert scan.exit_code == 0, scan.output
    assert "- `sales.x' ## SYSTEM ## TWO '`: version 1" in scan.output.splitlines()
    assert not any(line.startswith("## SYSTEM") for line in scan.output.splitlines())
