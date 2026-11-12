"""Fixtures for contract tests against the running compose dev stack (NFR-TST-02).

Tokens come from the dev Keycloak realm (ADR 0003). The OM bot token is read from env
``OM_BOT_TOKEN`` or from the ``om-secrets`` volume; it is never printed.
"""

import os
import subprocess
from collections.abc import Callable

import httpx
import pytest
from stack import COMPOSE, KEYCLOAK_URL, LAKELOOM_URL, OM_URL, TOKEN_URL, exchange_for_om

# Lets the contract-marked Postgres ping in tests/unit/state run in `-m contract` too.
os.environ.setdefault(
    "LAKELOOM_TEST_DB_URL", "postgresql://lakeloom:lakeloom@localhost:5432/lakeloom"
)


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


@pytest.fixture(scope="session")
def admin_agent_token() -> str:
    """DEV ONLY client with lakeloom:admin (create_model), besides read and propose."""
    return _token(
        {
            "grant_type": "client_credentials",
            "client_id": "lakeloom-admin-agent",
            "client_secret": "dev-admin-secret",
        }
    )


def _human_token(username: str, scope: str = "openid lakeloom:read") -> str:
    # Deliberately no lakeloom:review / :propose: those must come from the group map.
    # DEV ONLY: realm users have password = username.
    return _token(
        {
            "grant_type": "password",
            "client_id": "lakeloom-dev-cli",
            "username": username,
            "password": username,
            "scope": scope,
        }
    )


def _om_user_token(username: str) -> str:
    """A Keycloak token OpenMetadata accepts as ``username`` (``aud`` has ``openmetadata``
    via the optional scope ``openmetadata-audience``; ADR 0012)."""
    return _human_token(username, "openid openmetadata-audience")


@pytest.fixture(scope="session")
def om_token_for() -> Callable[[str], str]:
    """``om_token_for("alice")``: alice's Lakeloom token exchanged for an OpenMetadata token
    (client ``lakeloom``, ``audience=openmetadata``; ADR 0012). A secret: never print it."""

    def exchange(username: str) -> str:
        r = exchange_for_om(_human_token(username))
        # Only the OAuth error code: a 200 body would hold the token.
        error = None if r.status_code == 200 else r.json().get("error")
        assert r.status_code == 200, f"token exchange failed: {r.status_code} {error}"
        return str(r.json()["access_token"])

    return exchange


@pytest.fixture(scope="session")
def human_token() -> str:
    return _human_token("alice")


@pytest.fixture(scope="session")
def bob_token() -> str:
    return _human_token("bob")


@pytest.fixture(scope="session")
def carol_token() -> str:
    """carol: group ontology-reviewers like alice, but no OM reviewer role (ADR 0012)."""
    return _human_token("carol")


@pytest.fixture(scope="session")
def om_admin_token() -> str:
    """OM's admin principal ``om-admin`` (DEV ONLY), as a token OpenMetadata accepts."""
    return _om_user_token("om-admin")


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
