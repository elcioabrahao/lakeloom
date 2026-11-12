"""Shared helpers for the contract tests: dev-stack endpoints and an MCP client session.

Imported by tests as ``from stack import ...``; fixtures live in ``conftest.py``. A uniquely
named module avoids clashing with other ``conftest`` modules on ``sys.path``.
"""

import base64
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(REPO / "deploy/compose/docker-compose.yml")]

LAKELOOM_URL = os.environ.get("LAKELOOM_TEST_URL", "http://localhost:8000").rstrip("/")
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://localhost:8180").rstrip("/")
OM_URL = os.environ.get("OM_URL", "http://localhost:8585").rstrip("/")
TOKEN_URL = f"{KEYCLOAK_URL}/realms/lakeloom/protocol/openid-connect/token"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - grant URN
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - type URN


def exchange_for_om(subject_token: str) -> httpx.Response:
    """Standard token exchange (Keycloak V2) as the confidential client ``lakeloom``: a
    Lakeloom user token in, an OpenMetadata token (``aud: openmetadata``) for the same user
    out (ADR 0012). The response body holds a token: never print it.

    ``audience=openmetadata`` alone is refused ("Requested audience not available"): the
    audience must come from a client scope of the request, here ``openmetadata-audience``.
    """
    return httpx.post(
        TOKEN_URL,
        data={
            "grant_type": TOKEN_EXCHANGE,
            "client_id": "lakeloom",
            "client_secret": "dev-lakeloom-secret",  # DEV ONLY fixed public dev credential
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": "openmetadata",
            "scope": "openmetadata-audience",
        },
        timeout=10,
    )


def token_scopes(token: str) -> set[str]:
    """Scope claim of a JWT, decoded without verification (test inspection only)."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return set(str(claims.get("scope", "")).split())


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
