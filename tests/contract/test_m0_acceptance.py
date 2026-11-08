"""M0 acceptance against the running stack (NFR-TST-02, NFR-OPS-01, NFR-SEC-01..03, FR-OM-06).

Needs the compose dev stack (``docker compose -f deploy/compose/docker-compose.yml up``).
"""

from typing import Any

import httpx
import pytest
from conftest import KEYCLOAK_URL, server_info, token_scopes

pytestmark = pytest.mark.contract


async def test_nfr_ops_01_readyz_green(lakeloom_url: str) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{lakeloom_url}/readyz")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    checks = {c["name"]: c for c in body["checks"]}
    assert body["status"] == "ok"
    assert {"openmetadata", "state_store"} <= checks.keys(), body
    assert all(c["ok"] is True for c in checks.values()), body


async def test_nfr_sec_01_keycloak_agent_token_accepted(
    lakeloom_url: str, agent_token: str
) -> None:
    info = await server_info(lakeloom_url, agent_token)
    assert info["caller"]["type"] == "agent"  # type: ignore[index]


async def test_nfr_sec_03_keycloak_human_token_is_human(
    lakeloom_url: str, human_token: str
) -> None:
    # The token's own scope claim lacks review: it can only come from the group map.
    assert "lakeloom:review" not in token_scopes(human_token)
    info = await server_info(lakeloom_url, human_token)
    caller: Any = info["caller"]
    assert caller["type"] == "human"
    assert "lakeloom:review" in caller["scopes"]


async def test_nfr_sec_06_group_map_grants_propose_to_data_engineers(
    lakeloom_url: str, bob_token: str
) -> None:
    assert "lakeloom:propose" not in token_scopes(bob_token)
    info = await server_info(lakeloom_url, bob_token)
    caller: Any = info["caller"]
    assert "lakeloom:propose" in caller["scopes"]
    assert "lakeloom:review" not in caller["scopes"]


async def test_nfr_sec_01_wrong_audience_rejected(
    lakeloom_url: str, wrong_audience_token: str
) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{lakeloom_url}/mcp",
            headers={"Authorization": f"Bearer {wrong_audience_token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


async def test_nfr_sec_02_prm_published(lakeloom_url: str) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{lakeloom_url}/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    assert r.json()["authorization_servers"] == [f"{KEYCLOAK_URL}/realms/lakeloom"]


async def test_fr_om_06_server_info_reports_om_version_and_rdf(
    lakeloom_url: str, agent_token: str
) -> None:
    info = await server_info(lakeloom_url, agent_token)
    om: Any = info["openmetadata"]
    assert om["reachable"] is True
    assert om["version"].startswith("2.1")
    # RDF profile is off in the default dev stack (ADR 0003); the field is a bool, not None.
    assert isinstance(om["rdf_enabled"], bool)
