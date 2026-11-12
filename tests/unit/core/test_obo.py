"""On-behalf-of OM tokens (NFR-SEC-08..11, ADR 0012)."""

import base64
import logging
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.obo import OnBehalfOf
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient
from lakeloom.server.auth import Principal, PrincipalType

ISSUER = "http://idp.test/realms/lakeloom"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
SUBJECT = "subject-token-AAA"
EXCHANGED = "exchanged-token-BBB"
CLIENT_SECRET = "client-secret-CCC"  # noqa: S105
BOT = "bot-token-DDD"


def _secret(client: OMClient) -> str:
    token = client._token  # pyright: ignore[reportPrivateUsage]
    assert token is not None
    return token.get_secret_value()


def principal(kind: PrincipalType = PrincipalType.HUMAN, token: str = SUBJECT) -> Principal:
    return Principal(
        subject="alice",
        type=kind,
        client_id=None,
        username="alice",
        scopes=frozenset(),
        groups=frozenset(),
        token=SecretStr(token),
    )


def settings(**kw: object) -> Settings:
    values: dict[str, object] = {
        "om_url": "http://om.test:8585",
        "oidc_issuer": ISSUER,
        "oidc_client_id": "lakeloom",
        "oidc_client_secret": SecretStr(CLIENT_SECRET),
        "oidc_token_endpoint": TOKEN_URL,
        **kw,
    }
    return Settings(**values)  # pyright: ignore[reportArgumentType]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


def make(http: httpx.AsyncClient, s: Settings, clock: Clock | None = None) -> OnBehalfOf:
    om = OMClient("http://om.test:8585", SecretStr(BOT), http=http)
    return OnBehalfOf(s, http, om, clock or Clock())


def ok_response(expires_in: int | None = 300) -> httpx.Response:
    body: dict[str, object] = {"access_token": EXCHANGED, "token_type": "Bearer"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    return httpx.Response(200, json=body)


@respx.mock
async def test_exchange_posts_rfc8693_form_and_returns_client_with_exchanged_token(
    http: httpx.AsyncClient,
) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=ok_response())
    obo = make(http, settings())
    assert obo.mode == "exchange"

    client = await obo.om_for(principal())

    req = route.calls.last.request
    form = parse_qs(req.content.decode())
    assert form == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
        "subject_token": [SUBJECT],
        "subject_token_type": ["urn:ietf:params:oauth:token-type:access_token"],
        "requested_token_type": ["urn:ietf:params:oauth:token-type:access_token"],
        "audience": ["openmetadata"],
        "scope": ["openmetadata-audience"],
    }
    basic = base64.b64encode(f"lakeloom:{CLIENT_SECRET}".encode()).decode()
    assert req.headers["authorization"] == f"Basic {basic}"
    assert isinstance(client, OMClient)
    assert _secret(client) == EXCHANGED  # pyright: ignore[reportPrivateUsage]


@respx.mock
async def test_exchange_scope_none_omits_scope_field(http: httpx.AsyncClient) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=ok_response())
    obo = make(http, settings(om_exchange_scope=None))

    await obo.om_for(principal())

    assert "scope" not in parse_qs(route.calls.last.request.content.decode())


@respx.mock
async def test_exchange_custom_scope_is_sent(http: httpx.AsyncClient) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=ok_response())
    obo = make(http, settings(om_exchange_scope="other-scope"))

    await obo.om_for(principal())

    assert parse_qs(route.calls.last.request.content.decode())["scope"] == ["other-scope"]


@respx.mock
async def test_exchange_cached_until_expiry(http: httpx.AsyncClient) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=ok_response(300))
    clock = Clock()
    obo = make(http, settings(), clock)

    await obo.om_for(principal())
    await obo.om_for(principal())
    assert route.call_count == 1

    clock.now += 269
    await obo.om_for(principal())
    assert route.call_count == 1

    clock.now += 2  # past expires_in - 30
    await obo.om_for(principal())
    assert route.call_count == 2


@respx.mock
async def test_exchange_not_cached_without_useful_expiry(http: httpx.AsyncClient) -> None:
    route = respx.post(TOKEN_URL).mock(side_effect=[ok_response(None), ok_response(30)])
    obo = make(http, settings())

    await obo.om_for(principal())
    await obo.om_for(principal())

    assert route.call_count == 2


@respx.mock
async def test_cache_is_bounded_and_evicts_oldest(http: httpx.AsyncClient) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=ok_response(300))
    obo = make(http, settings())

    for i in range(257):
        await obo.om_for(principal(token=f"t{i}"))
    assert route.call_count == 257

    await obo.om_for(principal(token="t256"))  # noqa: S106  # newest still cached
    assert route.call_count == 257
    await obo.om_for(principal(token="t0"))  # noqa: S106  # oldest was evicted
    assert route.call_count == 258


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_exchange_refused_is_forbidden(http: httpx.AsyncClient, status: int) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(status, json={"error": "access_denied"}))
    obo = make(http, settings())

    with pytest.raises(LakeloomError) as exc:
        await obo.om_for(principal())

    assert exc.value.code == "forbidden"
    assert exc.value.message == "token exchange refused"


@respx.mock
async def test_idp_down_is_upstream_unavailable(http: httpx.AsyncClient) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
    obo = make(http, settings())

    with pytest.raises(LakeloomError) as exc:
        await obo.om_for(principal())

    assert exc.value.code == "upstream_unavailable"
    assert exc.value.__cause__ is None


@respx.mock
async def test_idp_5xx_is_upstream_unavailable(http: httpx.AsyncClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    obo = make(http, settings())

    with pytest.raises(LakeloomError) as exc:
        await obo.om_for(principal())

    assert exc.value.code == "upstream_unavailable"


@respx.mock
async def test_response_without_access_token_is_upstream_unavailable(
    http: httpx.AsyncClient,
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"token_type": "Bearer"}))
    obo = make(http, settings())

    with pytest.raises(LakeloomError) as exc:
        await obo.om_for(principal())

    assert exc.value.code == "upstream_unavailable"


@respx.mock
async def test_exchange_error_never_falls_back_to_forwarding(http: httpx.AsyncClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
    obo = make(http, settings(om_forward_user_token=True))
    assert obo.mode == "exchange"

    with pytest.raises(LakeloomError):
        await obo.om_for(principal())


async def test_forward_mode_uses_caller_token(http: httpx.AsyncClient) -> None:
    obo = make(http, settings(oidc_token_exchange=False, om_forward_user_token=True))
    assert obo.mode == "forward"

    client = await obo.om_for(principal())

    assert _secret(client) == SUBJECT  # pyright: ignore[reportPrivateUsage]


async def test_unconfigured_fails_closed_with_operator_hint(http: httpx.AsyncClient) -> None:
    obo = make(http, settings(oidc_client_secret=None))
    assert obo.mode == "unavailable"

    with pytest.raises(LakeloomError) as exc:
        await obo.om_for(principal())

    assert exc.value.code == "forbidden"
    assert exc.value.message == "human review is not configured"
    assert "LAKELOOM_OIDC_CLIENT_ID/_SECRET" in exc.value.hint
    assert "LAKELOOM_OM_FORWARD_USER_TOKEN=true" in exc.value.hint


async def test_exchange_off_without_forward_is_unavailable(http: httpx.AsyncClient) -> None:
    assert make(http, settings(oidc_token_exchange=False)).mode == "unavailable"


async def test_agent_principal_refused_never_bot(http: httpx.AsyncClient) -> None:
    for s in (settings(), settings(oidc_client_secret=None)):
        obo = make(http, s)
        with pytest.raises(LakeloomError) as exc:
            await obo.om_for(principal(PrincipalType.AGENT))
        assert exc.value.code == "forbidden"
        assert exc.value.message == "on-behalf-of OM access is for human principals"


@respx.mock
async def test_no_token_in_logs_or_errors(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, text=f"bad {SUBJECT} {CLIENT_SECRET} {EXCHANGED}")
    )
    obo = make(http, settings())

    with pytest.raises(LakeloomError) as exc:
        await obo.om_for(principal())
    respx.post(TOKEN_URL).mock(return_value=ok_response())
    await obo.om_for(principal())

    texts = [str(exc.value), exc.value.hint, repr(exc.value), caplog.text]
    for secret in (SUBJECT, EXCHANGED, CLIENT_SECRET):
        assert all(secret not in t for t in texts)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


@respx.mock
async def test_token_endpoint_from_discovery_is_fetched_once(http: httpx.AsyncClient) -> None:
    disc = respx.get(DISCOVERY).mock(
        return_value=httpx.Response(200, json={"token_endpoint": TOKEN_URL})
    )
    route = respx.post(TOKEN_URL).mock(return_value=ok_response(None))
    obo = make(http, settings(oidc_token_endpoint=None))

    await obo.om_for(principal())
    await obo.om_for(principal())

    assert disc.call_count == 1
    assert route.call_count == 2


@respx.mock
async def test_discovery_failure_is_upstream_unavailable(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY).mock(return_value=httpx.Response(500))
    obo = make(http, settings(oidc_token_endpoint=None))

    with pytest.raises(LakeloomError) as exc:
        await obo.om_for(principal())

    assert exc.value.code == "upstream_unavailable"


def test_client_secret_file(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from pathlib import Path

    f = Path(str(tmp_path)) / "secret"
    f.write_text(f"{CLIENT_SECRET}\n")
    monkeypatch.setenv("LAKELOOM_OIDC_CLIENT_SECRET_FILE", str(f))
    monkeypatch.setenv("LAKELOOM_OIDC_CLIENT_ID", "lakeloom")

    s = Settings()

    assert s.oidc_client_secret is not None
    assert s.oidc_client_secret.get_secret_value() == CLIENT_SECRET


async def test_empty_client_secret_or_id_is_unavailable(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKELOOM_OIDC_CLIENT_SECRET", "")
    assert make(http, Settings(oidc_client_id="lakeloom")).mode == "unavailable"
    monkeypatch.delenv("LAKELOOM_OIDC_CLIENT_SECRET")
    monkeypatch.setenv("LAKELOOM_OIDC_CLIENT_ID", "")
    s = Settings(oidc_client_secret=SecretStr(CLIENT_SECRET))
    assert make(http, s).mode == "unavailable"


@respx.mock
async def test_empty_scope_env_omits_scope_field(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKELOOM_OM_EXCHANGE_SCOPE", "")
    route = respx.post(TOKEN_URL).mock(return_value=ok_response())

    await make(http, settings()).om_for(principal())

    assert "scope" not in parse_qs(route.calls.last.request.content.decode())
