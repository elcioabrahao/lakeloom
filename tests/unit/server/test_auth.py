"""Tests for OIDC token validation and the bearer middleware.

NFR-SEC-01 (JWT validation), NFR-SEC-02 (RFC 9728 metadata, 401 challenge),
NFR-SEC-03 (agent vs human), NFR-SEC-06 (scopes and group mapping), FR-AGT-05
(``upstream_unavailable``).
"""

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from lakeloom.config import Settings
from lakeloom.server.auth import (
    PRM_PATH,
    PUBLIC_PATHS,
    SCOPES,
    AuthError,
    BearerAuthMiddleware,
    IdPUnavailable,
    Principal,
    PrincipalType,
    TokenValidator,
    protected_resource_metadata,
)

PRM_URL = "https://lakeloom.test/.well-known/oauth-protected-resource"
CHALLENGE = f'Bearer resource_metadata="{PRM_URL}"'
INVALID_CHALLENGE = f'Bearer error="invalid_token", resource_metadata="{PRM_URL}"'


@pytest.fixture
def make_validator(kit: Any, http: httpx.AsyncClient, clock: Any) -> Any:
    def build(**kwargs: Any) -> TokenValidator:
        params: dict[str, Any] = {
            "issuer": kit.issuer,
            "audience": kit.audience,
            "http": http,
            "time_fn": clock,
        }
        params.update(kwargs)
        return TokenValidator(**params)

    return build


@pytest.fixture
def validator(make_validator: Any) -> TokenValidator:
    return make_validator()


async def _rejected(validator: TokenValidator, token: str) -> AuthError:
    with pytest.raises(AuthError) as excinfo:
        await validator.validate(token)
    assert excinfo.value.code == "invalid_token"
    if token:
        assert token not in str(excinfo.value)
        assert token not in excinfo.value.description
    return excinfo.value


# --- NFR-SEC-01: claim and signature checks ---------------------------------------------


async def test_nfr_sec_01_valid_token_accepted(validator: TokenValidator, kit: Any) -> None:
    token = kit.token(scope="openid lakeloom:read lakeloom:propose")
    principal = await validator.validate(token)
    assert isinstance(principal, Principal)
    assert principal.subject == "user-123"
    assert principal.scopes == frozenset({"lakeloom:read", "lakeloom:propose"})
    assert principal.token.get_secret_value() == token


async def test_nfr_sec_01_discovery_used_when_no_jwks_url(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    await validator.validate(kit.token())
    await validator.validate(kit.token())
    assert idp.discovery_hits == 1
    assert idp.jwks_hits == 1


async def test_nfr_sec_01_explicit_jwks_url_skips_discovery(
    make_validator: Any, kit: Any, idp: Any
) -> None:
    validator = make_validator(jwks_url=kit.jwks_url)
    await validator.validate(kit.token())
    assert idp.discovery_hits == 0
    assert idp.jwks_hits == 1


async def test_nfr_sec_01_audience_list_accepted(validator: TokenValidator, kit: Any) -> None:
    principal = await validator.validate(kit.token(aud=["account", "lakeloom"]))
    assert principal.subject == "user-123"


async def test_nfr_sec_01_es256_token_accepted(validator: TokenValidator, kit: Any) -> None:
    token = kit.token(key=kit.ec_key, alg="ES256", kid=kit.ec_kid)
    assert (await validator.validate(token)).subject == "user-123"


async def test_nfr_sec_01_wrong_audience_rejected(validator: TokenValidator, kit: Any) -> None:
    await _rejected(validator, kit.token(aud="account"))
    await _rejected(validator, kit.token(aud=None))


async def test_nfr_sec_01_wrong_issuer_rejected(validator: TokenValidator, kit: Any) -> None:
    await _rejected(validator, kit.token(iss="https://evil.test/realms/lakeloom"))
    # A prefix/substring of the issuer must not match.
    await _rejected(validator, kit.token(iss=kit.issuer[:-1]))
    await _rejected(validator, kit.token(iss=None))


async def test_nfr_sec_01_expired_rejected(validator: TokenValidator, kit: Any) -> None:
    now = int(time.time())
    await _rejected(validator, kit.token(iat=now - 600, exp=now - 120))
    await _rejected(validator, kit.token(exp=None))


async def test_nfr_sec_01_nbf_in_future_rejected(validator: TokenValidator, kit: Any) -> None:
    now = int(time.time())
    await _rejected(validator, kit.token(nbf=now + 120))
    await _rejected(validator, kit.token(iat=now + 120))


async def test_nfr_sec_01_clock_skew_tolerated(validator: TokenValidator, kit: Any) -> None:
    now = int(time.time())
    principal = await validator.validate(kit.token(iat=now - 600, exp=now - 30, nbf=now + 30))
    assert principal.subject == "user-123"


async def test_nfr_sec_01_missing_subject_rejected(validator: TokenValidator, kit: Any) -> None:
    await _rejected(validator, kit.token(sub=None))
    await _rejected(validator, kit.token(sub=""))


async def test_nfr_sec_01_alg_none_rejected(validator: TokenValidator, kit: Any) -> None:
    for alg in ("none", "None", "NONE"):
        await _rejected(
            validator, kit.unsigned({"alg": alg, "typ": "JWT", "kid": kit.kid}, kit.claims())
        )
    await _rejected(validator, kit.unsigned({"typ": "JWT", "kid": kit.kid}, kit.claims()))


async def test_nfr_sec_01_hs256_with_public_key_rejected(
    validator: TokenValidator, kit: Any
) -> None:
    await _rejected(validator, kit.hs256_forgery(kit.claims(), kit.public_pem()))


async def test_nfr_sec_01_bad_signature_rejected(validator: TokenValidator, kit: Any) -> None:
    await _rejected(validator, kit.token(key=kit.other_rsa_key))
    good = kit.token()
    header, _payload, sig = good.split(".")
    tampered_payload = kit.unsigned({}, kit.claims(sub="admin")).split(".")[1]
    await _rejected(validator, f"{header}.{tampered_payload}.{sig}")


async def test_nfr_sec_01_alg_must_match_key(validator: TokenValidator, kit: Any) -> None:
    # RS384 is allowed globally, but the JWK pins RS256.
    await _rejected(validator, kit.token(alg="RS384"))
    # EC algorithm against the RSA key id.
    await _rejected(validator, kit.token(key=kit.ec_key, alg="ES256", kid=kit.kid))


async def test_nfr_sec_01_malformed_tokens_rejected(validator: TokenValidator, kit: Any) -> None:
    for token in ("", "abc", "a.b.c", "....", kit.token() + "x", "e30.e30.", "W10.e30.sig"):
        await _rejected(validator, token)


async def test_nfr_sec_01_oversized_token_rejected(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    await _rejected(validator, kit.token(padding="x" * 20_000))
    assert idp.jwks_hits == 0


async def test_nfr_sec_01_bad_kid_header_rejected(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    await _rejected(validator, kit.token(kid=None))
    await _rejected(validator, kit.unsigned({"alg": "RS256", "kid": 7}, kit.claims()))
    await _rejected(validator, kit.token(kid="k" * 1000))
    # The encryption key is never usable for signatures.
    await _rejected(validator, kit.token(kid="enc-1"))


async def test_nfr_sec_01_jku_and_embedded_jwk_ignored(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    attacker_jwk = kit.rsa_jwk(kit.other_rsa_key, kid=kit.kid)
    token = kit.token(
        key=kit.other_rsa_key, headers={"jku": "https://evil.test/jwks", "jwk": attacker_jwk}
    )
    await _rejected(validator, token)
    assert not any("evil" in url for url in idp.requests)


def test_nfr_sec_01_symmetric_algorithms_refused_in_config(kit: Any) -> None:
    http = httpx.AsyncClient()
    for algs in (("HS256",), ("RS256", "none")):
        with pytest.raises(ValueError, match="algorithm"):
            TokenValidator(issuer=kit.issuer, audience=kit.audience, http=http, algorithms=algs)


def test_nfr_sec_06_unknown_scope_in_group_map_refused(kit: Any) -> None:
    with pytest.raises(ValueError, match="lakeloom:superuser"):
        TokenValidator(
            issuer=kit.issuer,
            audience=kit.audience,
            http=httpx.AsyncClient(),
            scope_group_map={"g": ["lakeloom:superuser"]},
        )


# --- JWKS caching, rotation and IdP outages ------------------------------------------------


async def test_nfr_sec_01_unknown_kid_refetches_jwks_once(
    make_validator: Any, kit: Any, idp: Any, jwks: dict[str, Any], clock: Any
) -> None:
    rotated_away = {"keys": [k for k in jwks["keys"] if k["kid"] != kit.kid]}

    # Key appears on the second fetch (rotation) -> accepted after one refetch.
    idp.jwks_responses = [rotated_away, jwks]
    validator = make_validator(jwks_url=kit.jwks_url)
    assert (await validator.validate(kit.token())).subject == "user-123"
    assert idp.jwks_hits == 2

    # Key never present -> 401, still exactly one refetch.
    idp.jwks_hits = 0
    idp.jwks_responses = [rotated_away]
    validator = make_validator(jwks_url=kit.jwks_url)
    error = await _rejected(validator, kit.token())
    assert "key" in error.description
    assert idp.jwks_hits == 2


async def test_nfr_sec_01_unknown_kid_refetch_rate_limited(
    validator: TokenValidator, kit: Any, idp: Any, clock: Any
) -> None:
    await _rejected(validator, kit.token(kid="nope-1"))
    assert idp.jwks_hits == 2
    clock.advance(10)
    await _rejected(validator, kit.token(kid="nope-2"))
    assert idp.jwks_hits == 2  # within 30 s: no refetch
    clock.advance(21)
    await _rejected(validator, kit.token(kid="nope-3"))
    assert idp.jwks_hits == 3
    # Known keys keep working without any fetch.
    await validator.validate(kit.token())
    assert idp.jwks_hits == 3


async def test_nfr_sec_01_concurrent_cold_cache_fetches_once(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    tokens = [kit.token(sub=f"user-{i}") for i in range(20)]
    principals = await asyncio.gather(*(validator.validate(t) for t in tokens))
    assert {p.subject for p in principals} == {f"user-{i}" for i in range(20)}
    assert idp.discovery_hits == 1
    assert idp.jwks_hits == 1


async def test_nfr_sec_01_concurrent_unknown_kids_refetch_once(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    await validator.validate(kit.token())
    results = await asyncio.gather(
        *(validator.validate(kit.token(kid=f"nope-{i}")) for i in range(10)),
        return_exceptions=True,
    )
    assert all(isinstance(r, AuthError) for r in results)
    assert idp.jwks_hits == 2


async def test_nfr_sec_01_jwks_cache_expires_and_drops_revoked_keys(
    make_validator: Any, kit: Any, idp: Any, jwks: dict[str, Any], clock: Any
) -> None:
    revoked = {"keys": [k for k in jwks["keys"] if k["kid"] != kit.kid]}
    idp.jwks_responses = [jwks, revoked]
    validator = make_validator(jwks_max_age=600)
    await validator.validate(kit.token())
    clock.advance(601)
    await _rejected(validator, kit.token())
    assert idp.jwks_hits == 3  # max-age refresh + one unknown-kid refetch


async def test_nfr_sec_01_stale_jwks_kept_when_idp_down(
    make_validator: Any, kit: Any, idp: Any, clock: Any
) -> None:
    validator = make_validator(jwks_max_age=600)
    await validator.validate(kit.token())
    idp.down = True
    clock.advance(601)
    # Refresh fails, cached keys keep serving; the next retry waits 30 s.
    assert (await validator.validate(kit.token())).subject == "user-123"
    attempts = len(idp.requests)
    clock.advance(5)
    await validator.validate(kit.token())
    assert len(idp.requests) == attempts
    clock.advance(30)
    idp.down = False
    await validator.validate(kit.token())
    assert idp.jwks_hits == 2


async def test_idp_unreachable_raises_idp_unavailable(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    idp.down = True
    token = kit.token()
    with pytest.raises(IdPUnavailable) as excinfo:
        await validator.validate(token)
    assert token not in str(excinfo.value)
    # Recovers once the IdP is back.
    idp.down = False
    assert (await validator.validate(token)).subject == "user-123"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, None),
        (200, {"no": "keys"}),
        (200, {"keys": "nope"}),
        (200, ["not", "an", "object"]),
    ],
)
async def test_idp_bad_jwks_response_raises_idp_unavailable(
    make_validator: Any, kit: Any, idp: Any, status: int, body: Any
) -> None:
    idp.jwks_status = status
    if body is not None:
        idp.jwks_responses = [body]
    validator = make_validator(jwks_url=kit.jwks_url)
    with pytest.raises(IdPUnavailable):
        await validator.validate(kit.token())


@pytest.mark.parametrize(
    "discovery",
    [
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"issuer": "x"}),
        httpx.Response(200, json={"jwks_uri": "file:///etc/passwd"}),
    ],
)
async def test_idp_bad_discovery_raises_idp_unavailable(kit: Any, discovery: Any) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: discovery))
    validator = TokenValidator(issuer=kit.issuer, audience=kit.audience, http=http)
    with pytest.raises(IdPUnavailable):
        await validator.validate(kit.token())


async def test_unknown_kid_refetch_with_idp_down_raises_idp_unavailable(
    validator: TokenValidator, kit: Any, idp: Any
) -> None:
    await validator.validate(kit.token())
    idp.down = True
    with pytest.raises(IdPUnavailable):
        await validator.validate(kit.token(kid="rotated"))


async def test_malformed_jwks_entries_skipped(
    make_validator: Any, kit: Any, idp: Any, jwks: dict[str, Any]
) -> None:
    junk: list[Any] = [
        "not-a-dict",
        {"kty": "RSA"},  # no kid
        {"kid": "sym", "kty": "oct", "k": "c2VjcmV0"},
        {"kid": "broken", "kty": "RSA", "n": "!!", "e": "AQAB", "use": "sig"},
    ]
    idp.jwks_responses = [{"keys": [*junk, *jwks["keys"]]}]
    validator = make_validator()
    assert (await validator.validate(kit.token())).subject == "user-123"
    await _rejected(validator, kit.token(kid="broken"))
    await _rejected(validator, kit.hs256_forgery(kit.claims(), b"secret", kid="sym"))


async def test_jwk_without_alg_accepts_family_algorithms(
    make_validator: Any, kit: Any, idp: Any
) -> None:
    jwk = {k: v for k, v in kit.rsa_jwk(kit.rsa_key, kid="noalg").items() if k != "alg"}
    idp.jwks_responses = [{"keys": [jwk]}]
    validator = make_validator()
    for alg in ("RS256", "PS256"):
        assert (await validator.validate(kit.token(alg=alg, kid="noalg"))).subject == "user-123"
    await _rejected(validator, kit.token(key=kit.ec_key, alg="ES256", kid="noalg"))
    # Alg confusion against a key whose JWK does not pin an algorithm.
    await _rejected(validator, kit.hs256_forgery(kit.claims(), kit.public_pem(), kid="noalg"))


# --- NFR-SEC-03 / NFR-SEC-06: principals ------------------------------------------------


async def test_nfr_sec_03_agent_vs_human(validator: TokenValidator, kit: Any) -> None:
    agent = await validator.validate(
        kit.token(client_id="lakeloom-agent", azp="lakeloom-agent", sub="sa-1")
    )
    assert agent.type is PrincipalType.AGENT
    assert agent.client_id == "lakeloom-agent"
    assert agent.username is None

    human = await validator.validate(
        kit.token(azp="lakeloom-dev-cli", preferred_username="alice", sub="u-alice")
    )
    assert human.type is PrincipalType.HUMAN
    assert human.username == "alice"
    assert human.client_id == "lakeloom-dev-cli"


async def test_nfr_sec_03_agent_claim_configurable(make_validator: Any, kit: Any) -> None:
    validator = make_validator(agent_claim="clientId")
    assert (await validator.validate(kit.token(clientId="a"))).type is PrincipalType.AGENT
    assert (await validator.validate(kit.token(client_id="a"))).type is PrincipalType.HUMAN


async def test_nfr_sec_06_scopes_from_scope_claim_and_groups(make_validator: Any, kit: Any) -> None:
    validator = make_validator(scope_group_map={"ontology-reviewers": ["lakeloom:review"]})
    principal = await validator.validate(
        kit.token(scope="openid lakeloom:read", groups=["ontology-reviewers"])
    )
    assert principal.scopes == frozenset({"lakeloom:read", "lakeloom:review"})
    assert principal.groups == frozenset({"ontology-reviewers"})


async def test_nfr_sec_06_odd_scope_and_group_claims(make_validator: Any, kit: Any) -> None:
    validator = make_validator(scope_group_map={"admins": ["lakeloom:admin"]})
    principal = await validator.validate(kit.token(scope=["lakeloom:read"], groups="admins"))
    assert principal.scopes == frozenset()
    assert principal.groups == frozenset()
    # Scopes are case-sensitive (RFC 6749 §3.3): LAKELOOM:ADMIN is dropped; admin comes
    # from the group mapping. Non-string group entries are ignored.
    principal = await validator.validate(
        kit.token(scope="  lakeloom:read\tLAKELOOM:ADMIN  ", groups=["admins", 3])
    )
    assert principal.scopes == frozenset({"lakeloom:read", "lakeloom:admin"})
    assert principal.groups == frozenset({"admins"})


async def test_principal_repr_hides_token(validator: TokenValidator, kit: Any) -> None:
    token = kit.token()
    principal = await validator.validate(token)
    assert token not in repr(principal)
    assert token not in str(principal)


# --- NFR-SEC-02: protected resource metadata -------------------------------------------


def test_nfr_sec_02_prm_document() -> None:
    settings = Settings(
        public_url="https://lakeloom.test/", oidc_issuer="https://idp.test/realms/lakeloom"
    )
    assert protected_resource_metadata(settings) == {
        "resource": "https://lakeloom.test",
        "authorization_servers": ["https://idp.test/realms/lakeloom"],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
    }
    assert SCOPES == (
        "lakeloom:read",
        "lakeloom:propose",
        "lakeloom:review",
        "lakeloom:publish",
        "lakeloom:admin",
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"oidc_issuer": "https://idp.test"}, {"public_url": "https://lakeloom.test"}],
)
def test_nfr_sec_02_prm_requires_configuration(
    kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LAKELOOM_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LAKELOOM_OIDC_ISSUER", raising=False)
    with pytest.raises(ValueError, match="LAKELOOM_"):
        protected_resource_metadata(Settings(**kwargs))


# --- Middleware ------------------------------------------------------------------------


async def _whoami(request: Request) -> JSONResponse:
    principal: Principal | None = getattr(request.state, "principal", None)
    return JSONResponse({"subject": principal.subject if principal else None})


async def _ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text(websocket.state.principal.subject)
    await websocket.close()


@pytest.fixture
def client(validator: TokenValidator) -> TestClient:
    routes = [Route(path, _whoami) for path in ("/api", "/mcp", *PUBLIC_PATHS, "/healthz/")]
    app = Starlette(routes=[*routes, WebSocketRoute("/ws", _ws)])
    wrapped = BearerAuthMiddleware(
        app, validator=validator, resource_metadata_url=PRM_URL, public_paths=PUBLIC_PATHS
    )
    return TestClient(wrapped)


def _assert_401(response: Any, challenge: str, *, token: str | None = None) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == challenge
    body = response.json()
    assert body["error"] == "invalid_token"
    assert isinstance(body["error_description"], str)
    if token:
        assert token not in response.text
        assert token not in json.dumps(dict(response.headers))


def test_nfr_sec_02_missing_token_401_with_resource_metadata(client: TestClient) -> None:
    for path in ("/api", "/mcp", "/healthz/"):
        _assert_401(client.get(path), CHALLENGE)
    assert (
        client.get("/mcp").headers["www-authenticate"]
        == 'Bearer resource_metadata="https://lakeloom.test/.well-known/oauth-protected-resource"'
    )


def test_empty_bearer_401(client: TestClient) -> None:
    for value in ("Bearer", "Bearer ", "Bearer    ", "bearer"):
        _assert_401(client.get("/api", headers={"Authorization": value}), INVALID_CHALLENGE)


def test_nfr_sec_02_hostile_tokens_401(client: TestClient, kit: Any) -> None:
    now = int(time.time())
    hostile = [
        kit.unsigned({"alg": "none", "kid": kit.kid}, kit.claims()),
        kit.hs256_forgery(kit.claims(), kit.public_pem()),
        kit.token(iat=now - 900, exp=now - 600),
        kit.token(nbf=now + 600),
        kit.token(iss="https://evil.test"),
        kit.token(aud="other"),
        kit.token(padding="x" * 20_000),
        "garbage",
    ]
    for token in hostile:
        response = client.get("/api", headers={"Authorization": f"Bearer {token}"})
        _assert_401(response, INVALID_CHALLENGE, token=token)


def test_non_bearer_scheme_and_duplicate_headers_401(client: TestClient, kit: Any) -> None:
    token = kit.token()
    _assert_401(client.get("/api", headers={"Authorization": "Basic dTpw"}), INVALID_CHALLENGE)
    _assert_401(
        client.get("/api", headers={"Authorization": token}), INVALID_CHALLENGE, token=token
    )
    duplicated = [("authorization", f"Bearer {token}"), ("authorization", "Bearer other")]
    _assert_401(client.get("/api", headers=duplicated), INVALID_CHALLENGE, token=token)


def test_valid_token_sets_principal(client: TestClient, kit: Any) -> None:
    for scheme in ("Bearer", "bearer", "BEARER"):
        response = client.get("/mcp", headers={"Authorization": f"{scheme}  {kit.token()} "})
        assert response.status_code == 200
        assert response.json() == {"subject": "user-123"}


def test_public_paths_skip_auth(client: TestClient, idp: Any) -> None:
    for path in PUBLIC_PATHS:
        response = client.get(path, headers={"Authorization": "Bearer garbage"})
        assert response.status_code == 200, path
        assert response.json() == {"subject": None}
    assert PUBLIC_PATHS == ("/healthz", "/readyz", "/metrics", PRM_PATH)
    assert PRM_PATH == "/.well-known/oauth-protected-resource"
    assert idp.requests == []


def test_idp_down_503_upstream_unavailable(client: TestClient, kit: Any, idp: Any) -> None:
    idp.down = True
    token = kit.token()
    response = client.get("/api", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json() == {
        "error": "upstream_unavailable",
        "hint": "identity provider unreachable",
    }
    assert token not in response.text


def test_websocket_requires_token(client: TestClient, kit: Any) -> None:
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/ws"):
        pass
    assert excinfo.value.code == 1008
    headers = {"Authorization": f"Bearer {kit.token()}"}
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert ws.receive_text() == "user-123"


def test_lifespan_passes_through(validator: TokenValidator) -> None:
    started: list[bool] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "lifespan"
        await receive()
        started.append(True)
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})

    wrapped = BearerAuthMiddleware(
        app, validator=validator, resource_metadata_url=PRM_URL, public_paths=PUBLIC_PATHS
    )
    with TestClient(wrapped):
        assert started == [True]
