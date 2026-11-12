"""OpenMetadata on Keycloak SSO in the dev stack (FR-OM-05, NFR-SEC-08, NFR-SEC-09; ADR 0012).

Tokens are secrets: tests assert on status codes and selected claims only, never print them.
"""

import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from stack import TOKEN_URL

pytestmark = pytest.mark.contract

ISSUER = "http://localhost:8180/realms/lakeloom"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - grant URN
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - type URN


def _logged_in_user(om_url: str, token: str) -> httpx.Response:
    return httpx.get(
        f"{om_url}/api/v1/users/loggedInUser",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def _claims(token: str) -> dict[str, object]:
    """Claims without verification (test inspection only)."""
    return dict(jwt.decode(token, options={"verify_signature": False}))


def _audiences(token: str) -> set[str]:
    aud = _claims(token).get("aud")
    if isinstance(aud, str):
        return {aud}
    if isinstance(aud, list):
        return {str(a) for a in aud}  # pyright: ignore[reportUnknownVariableType]
    return set()


def _exchange(subject_token: str) -> httpx.Response:
    """Standard token exchange (Keycloak V2) as the confidential client ``lakeloom``.

    ``audience=openmetadata`` alone is refused ("Requested audience not available"): the
    audience must come from a client scope of the request, here ``openmetadata-audience``.
    """
    return httpx.post(
        TOKEN_URL,
        data={
            "grant_type": TOKEN_EXCHANGE,
            "client_id": "lakeloom",
            "client_secret": "dev-lakeloom-secret",  # DEV ONLY
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": "openmetadata",
            "scope": "openmetadata-audience",
        },
        timeout=10,
    )


def test_bot_token_still_authenticates(om_url: str, om_bot_token: str) -> None:
    r = _logged_in_user(om_url, om_bot_token)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "ingestion-bot"
    assert r.json()["isBot"] is True


def test_om_admin_is_admin_principal(om_url: str, om_admin_token: str) -> None:
    r = _logged_in_user(om_url, om_admin_token)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "om-admin"
    assert r.json()["isAdmin"] is True


def test_keycloak_user_token_is_om_user(om_url: str) -> None:
    r = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "lakeloom-dev-cli",
            "username": "alice",
            "password": "alice",  # DEV ONLY
            "scope": "openid openmetadata-audience",
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    token = str(r.json()["access_token"])
    assert "openmetadata" in _audiences(token)
    me = _logged_in_user(om_url, token)
    assert me.status_code == 200, me.text
    assert me.json()["name"] == "alice"
    assert me.json()["isAdmin"] is False


def test_lakeloom_token_is_not_forwardable_to_om(om_url: str, human_token: str) -> None:
    """NFR-SEC-09: alice's Lakeloom token (aud ``lakeloom``) is not accepted by OM."""
    assert "openmetadata" not in _audiences(human_token)
    r = _logged_in_user(om_url, human_token)
    assert r.status_code == 401, r.text
    assert "audience" in r.json()["message"]


def test_token_exchange_yields_om_token_for_alice(om_url: str, human_token: str) -> None:
    r = _exchange(human_token)
    assert r.status_code == 200, r.text
    exchanged = str(r.json()["access_token"])
    claims = _claims(exchanged)
    assert claims["iss"] == ISSUER
    assert claims["azp"] == "lakeloom"
    assert claims["preferred_username"] == "alice"
    assert _audiences(exchanged) == {"openmetadata"}
    me = _logged_in_user(om_url, exchanged)
    assert me.status_code == 200, me.text
    assert me.json()["name"] == "alice"


def test_token_exchange_needs_lakeloom_in_subject_audience(wrong_audience_token: str) -> None:
    r = _exchange(wrong_audience_token)
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "access_denied"


def test_om_rejects_other_issuer_token(om_url: str) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {
        "iss": "https://issuer.example.invalid",
        "aud": "openmetadata",
        "sub": str(uuid.uuid4()),
        "preferred_username": "alice",
        "email": "alice@example.invalid",
        "iat": now,
        "exp": now + 300,
    }
    forged = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "ll-unknown-key"})
    r = _logged_in_user(om_url, forged)
    assert r.status_code == 401, r.text


def test_om_auth_config_is_custom_oidc(om_url: str) -> None:
    r = httpx.get(f"{om_url}/api/v1/system/config/auth", timeout=30)
    assert r.status_code == 200, r.text
    config = r.json()
    assert config["provider"] == "custom-oidc"
    assert config["providerName"] == "keycloak"
    assert config["authority"] == ISSUER
    assert config["clientId"] == "openmetadata"


def test_only_alice_has_om_reviewer_role(om_url: str, om_admin_token: str) -> None:
    headers = {"Authorization": f"Bearer {om_admin_token}"}
    roles: dict[str, set[str]] = {}
    for name in ("alice", "bob", "carol"):
        r = httpx.get(
            f"{om_url}/api/v1/users/name/{name}",
            params={"fields": "roles"},
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 200, r.text
        roles[name] = {role["name"] for role in r.json().get("roles", [])}
    assert "LakeloomOntologyReviewer" in roles["alice"]
    assert "LakeloomOntologyReviewer" not in roles["carol"]
    assert "LakeloomOntologyReviewer" not in roles["bob"]
