"""Fixtures for contract tests against the running compose dev stack (NFR-TST-02).

Tokens come from the dev Keycloak realm (ADR 0003). The OM bot token is read from env
``OM_BOT_TOKEN`` or from the ``om-secrets`` volume; it is never printed.
"""

import base64
import json
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# Lets the contract-marked Postgres ping in tests/unit/state run in `-m contract` too.
os.environ.setdefault(
    "LAKELOOM_TEST_DB_URL", "postgresql://lakeloom:lakeloom@localhost:5432/lakeloom"
)

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(REPO / "deploy/compose/docker-compose.yml")]

LAKELOOM_URL = os.environ.get("LAKELOOM_TEST_URL", "http://localhost:8000").rstrip("/")
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://localhost:8180").rstrip("/")
OM_URL = os.environ.get("OM_URL", "http://localhost:8585").rstrip("/")
TOKEN_URL = f"{KEYCLOAK_URL}/realms/lakeloom/protocol/openid-connect/token"


@pytest.fixture(scope="session")
def lakeloom_url() -> str:
    try:
        httpx.get(f"{LAKELOOM_URL}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Lakeloom stack not reachable at {LAKELOOM_URL}: {exc!r}")
    return LAKELOOM_URL


def _token(data: dict[str, str]) -> str:
    try:
        r = httpx.post(TOKEN_URL, data=data, timeout=10)
    except httpx.HTTPError as exc:
        pytest.skip(f"Keycloak not reachable at {KEYCLOAK_URL}: {exc!r}")
    assert r.status_code == 200, f"token request failed: {r.status_code} {r.text}"
    return str(r.json()["access_token"])


@pytest.fixture(scope="session")
def agent_token() -> str:
    return _token(
        {
            "grant_type": "client_credentials",
            "client_id": "lakeloom-agent",
            "client_secret": "dev-agent-secret",
        }
    )


def _human_token(username: str) -> str:
    # Deliberately no lakeloom:review / :propose: those must come from the group map.
    return _token(
        {
            "grant_type": "password",
            "client_id": "lakeloom-dev-cli",
            "username": username,
            "password": username,
            "scope": "openid lakeloom:read",
        }
    )


@pytest.fixture(scope="session")
def human_token() -> str:
    return _human_token("alice")


@pytest.fixture(scope="session")
def bob_token() -> str:
    return _human_token("bob")


def token_scopes(token: str) -> set[str]:
    """Scope claim of a JWT, decoded without verification (test inspection only)."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return set(str(claims.get("scope", "")).split())


@pytest.fixture(scope="session")
def wrong_audience_token() -> str:
    return _token(
        {
            "grant_type": "client_credentials",
            "client_id": "other-app",
            "client_secret": "dev-other-secret",
        }
    )


@pytest.fixture(scope="session")
def om_url() -> str:
    return OM_URL


@pytest.fixture(scope="session")
def om_bot_token() -> str:
    token = os.environ.get("OM_BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no untrusted input
            [*COMPOSE, "exec", "-T", "lakeloom", "cat", "/secrets/om_bot_token"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    pytest.skip(
        "OM bot token unavailable: set OM_BOT_TOKEN or start the compose stack "
        "(read via `docker compose exec lakeloom cat /secrets/om_bot_token`)"
    )


@asynccontextmanager
async def mcp_session(base_url: str, token: str) -> AsyncIterator[ClientSession]:
    """Official MCP client over real Streamable HTTP with a bearer token."""
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(f"{base_url}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def server_info(base_url: str, token: str) -> dict[str, object]:
    async with mcp_session(base_url, token) as session:
        result = await session.call_tool("get_server_info", {})
    assert result.is_error is not True, result
    return dict(result.structured_content or {})
