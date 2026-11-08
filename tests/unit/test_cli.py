"""CLI tests (NFR-OSS-01 console entry point; FR-AGT-07 ``lakeloom serve``)."""

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
