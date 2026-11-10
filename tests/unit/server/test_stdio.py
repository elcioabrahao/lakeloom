"""stdio transport: the configured local principal (NFR-SEC-04)."""

import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from typer.testing import CliRunner

from lakeloom.cli import app
from lakeloom.config import ConfigError, Settings, get_settings


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "LAKELOOM_STDIO_ENABLED": "true",
        "LAKELOOM_ENV": "development",
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_OM_URL": "http://127.0.0.1:1",
        "PYTHONPATH": str(Path(__file__).parents[3] / "src"),
        **extra,
    }


def test_nfr_sec_04_stdio_refused_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LAKELOOM_ENV", "production")
    monkeypatch.delenv("LAKELOOM_STDIO_ENABLED", raising=False)
    result = CliRunner().invoke(app, ["serve", "--transport", "stdio"])
    assert result.exit_code == 2
    assert "stdio transport is disabled (set LAKELOOM_STDIO_ENABLED=true)" in result.output


def test_stdio_principal_scopes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().stdio_principal == "local"
    assert Settings().stdio_scopes == ["lakeloom:read"]
    monkeypatch.setenv("LAKELOOM_STDIO_SCOPES", "lakeloom:read, lakeloom:propose")
    assert get_settings().stdio_scopes == ["lakeloom:read", "lakeloom:propose"]
    monkeypatch.setenv("LAKELOOM_STDIO_SCOPES", '["lakeloom:read"]')
    assert get_settings().stdio_scopes == ["lakeloom:read"]
    monkeypatch.setenv("LAKELOOM_STDIO_SCOPES", "lakeloom:read,root")
    with pytest.raises(ConfigError):
        get_settings()


async def _session(tmp_path: Path, **extra: str):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "lakeloom.cli", "serve", "--transport", "stdio"],
        env=_env(tmp_path, **extra),
    )
    return params


async def test_stdio_session_end_to_end(tmp_path: Path) -> None:
    params = await _session(tmp_path)
    with (tmp_path / "err.log").open("w+") as err:
        async with (
            stdio_client(params, errlog=err) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert "get_server_info" in tools
            info = await session.call_tool("get_server_info", {})
            assert info.structured_content is not None
            assert info.structured_content["caller"]["subject"] == "local"
            assert info.structured_content["caller"]["scopes"] == ["lakeloom:read"]
            denied = await session.call_tool("scan_lake_source", {"source": "x"})
            assert denied.is_error
            assert denied.structured_content is not None
            assert denied.structured_content["error"]["code"] == "forbidden"
        err.seek(0)
        for line in err.read().splitlines():
            if line.startswith("{"):
                json.loads(line)  # logs on stderr are JSON; stdout only carried the protocol


async def test_stdio_scopes_from_env_grant_propose(tmp_path: Path) -> None:
    params = await _session(
        tmp_path,
        LAKELOOM_STDIO_SCOPES="lakeloom:read,lakeloom:propose",
        LAKELOOM_STDIO_PRINCIPAL="me",
    )
    with (tmp_path / "err.log").open("w+") as err:
        async with (
            stdio_client(params, errlog=err) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            info = await session.call_tool("get_server_info", {})
            assert info.structured_content is not None
            assert info.structured_content["caller"]["subject"] == "me"
            assert "lakeloom:propose" in info.structured_content["caller"]["scopes"]


def test_no_stdout_logging_on_stdio(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    streams: list[object] = []
    monkeypatch.setattr(
        "lakeloom.cli.configure_logging", lambda level, stream=None: streams.append(stream)
    )
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)

    async def fake_run(deps: object) -> None:
        return None

    monkeypatch.setattr("lakeloom.server.stdio.run_stdio", fake_run)
    result = CliRunner().invoke(app, ["serve", "--transport", "stdio"])
    assert result.exit_code == 0, result.output
    assert len(streams) == 1
    assert streams[0] is not None
    assert streams[0] is not sys.stdout
