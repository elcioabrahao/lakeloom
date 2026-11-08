"""MCP surface over Streamable HTTP (FR-AGT-01, FR-AGT-02, FR-AGT-05, FR-AGT-06, FR-OM-06).

The full FastAPI app runs in-process: ``httpx2.ASGITransport`` (the HTTP library the MCP
client uses) carries the official MCP client's requests straight into the ASGI app, and
the app lifespan (which runs the MCP session manager) is entered explicitly.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
from conftest import AuthKit, DepsFactory, om_down
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from lakeloom.metrics import REGISTRY
from lakeloom.server.mcp_app import create_mcp, create_server_app

BASE = "https://lakeloom.test"


@asynccontextmanager
async def mcp_session(app: FastAPI, token: str | None) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers
        ) as http,
        streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def call_server_info(app: FastAPI, token: str) -> CallToolResult:
    async with mcp_session(app, token) as session:
        return await session.call_tool("get_server_info", {})


def tool_calls(outcome: str) -> float:
    value = REGISTRY.get_sample_value(
        "lakeloom_tool_calls_total", {"tool": "get_server_info", "outcome": outcome}
    )
    return value or 0.0


async def test_fr_agt_06_get_server_info_annotations(make_deps: DepsFactory) -> None:
    mcp = create_mcp(make_deps())

    tools = await mcp.list_tools()

    assert [t.name for t in tools] == ["get_server_info"]
    tool = tools[0]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.description
    assert "lakeloom:read" in tool.description
    assert tool.input_schema.get("properties", {}) == {}
    assert tool.output_schema is not None
    assert "openmetadata" in tool.output_schema["properties"]


async def test_nfr_sec_01_mcp_requires_auth(make_deps: DepsFactory) -> None:
    app = create_server_app(make_deps())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


async def test_fr_om_06_get_server_info_over_streamable_http(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    app = create_server_app(make_deps())
    before = tool_calls("ok")

    result = await call_server_info(app, kit.token())

    assert result.is_error is not True
    structured: dict[str, Any] = result.structured_content or {}
    assert structured["name"] == "lakeloom"
    assert structured["openmetadata"]["version"] == "2.1.0"
    assert structured["openmetadata"]["rdf_enabled"] is True
    assert structured["caller"]["subject"] == "user-123"
    assert structured["caller"]["scopes"] == ["lakeloom:read"]
    text = result.content[0]
    assert text.type == "text"
    assert "2.1.0" in text.text
    assert tool_calls("ok") == before + 1


async def test_fr_om_06_get_server_info_when_om_down(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(make_deps(om_down))

    result = await call_server_info(app, kit.token())

    assert result.is_error is not True
    structured: dict[str, Any] = result.structured_content or {}
    assert structured["openmetadata"]["reachable"] is False
    assert structured["openmetadata"]["error"]


async def test_fr_agt_05_get_server_info_forbidden_without_read_scope(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    app = create_server_app(make_deps())
    before = tool_calls("forbidden")

    result = await call_server_info(app, kit.token(scope="lakeloom:propose"))

    assert result.is_error is True
    assert result.structured_content == {
        "error": {
            "code": "forbidden",
            "message": "get_server_info requires the lakeloom:read scope",
            "hint": "token lacks lakeloom:read",
        }
    }
    assert tool_calls("forbidden") == before + 1


async def test_nfr_ops_02_mcp_requests_labelled_with_mount(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    app = create_server_app(make_deps())

    await call_server_info(app, kit.token())

    value = REGISTRY.get_sample_value(
        "lakeloom_http_requests_total", {"method": "POST", "route": "/mcp", "status": "200"}
    )
    assert value is not None and value >= 1


async def test_mcp_rejects_foreign_host_header(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(make_deps())
    headers = {
        "Authorization": f"Bearer {kit.token()}",
        "Host": "evil.test",
        "Accept": "application/json, text/event-stream",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c,
    ):
        r = await c.post("/mcp", json=body, headers=headers)
    assert r.status_code == 421
