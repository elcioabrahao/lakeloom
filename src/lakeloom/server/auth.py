"""OAuth 2.1 resource-server authentication (REQUIREMENTS.md §8.1-8.2).

- NFR-SEC-01: :class:`TokenValidator` validates JWT access tokens from one OIDC issuer:
  JWKS (via discovery or an explicit URL), signature, ``iss``, ``aud``, ``exp``, ``nbf``
  and ``iat`` with clock-skew leeway. Only asymmetric algorithms are accepted, and each
  key is bound to one algorithm, so ``alg: none`` and HS*/RS* confusion are impossible.
- NFR-SEC-02: :func:`protected_resource_metadata` builds the RFC 9728 document;
  :class:`BearerAuthMiddleware` answers ``401`` with ``WWW-Authenticate`` pointing at it.
- NFR-SEC-03: a token carrying the configured agent claim (default ``client_id``) is an
  agent principal; otherwise it is a human principal.
- NFR-SEC-06: scopes come from the ``scope`` claim plus IdP groups mapped to scopes.
- FR-AGT-05: an unreachable IdP yields ``503 upstream_unavailable``, never 500 or 200.

Error descriptions are fixed strings: they never echo the token or IdP responses.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal, cast

import httpx
import jwt
from pydantic import SecretStr
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lakeloom.config import Settings

__all__ = [
    "MAX_TOKEN_LENGTH",
    "PRM_PATH",
    "PUBLIC_PATHS",
    "SCOPES",
    "AuthError",
    "BearerAuthMiddleware",
    "IdPUnavailable",
    "Principal",
    "PrincipalType",
    "TokenValidator",
    "protected_resource_metadata",
]

_log = logging.getLogger(__name__)

SCOPES: Final = (
    "lakeloom:read",
    "lakeloom:propose",
    "lakeloom:review",
    "lakeloom:publish",
    "lakeloom:admin",
)
PRM_PATH: Final = "/.well-known/oauth-protected-resource"
PUBLIC_PATHS: Final = ("/healthz", "/readyz", "/metrics", PRM_PATH)

MAX_TOKEN_LENGTH: Final = 16 * 1024
"""Longer tokens are rejected before any parsing (Keycloak tokens are typically < 4 KiB)."""
JWKS_REFETCH_INTERVAL: Final = 30.0
"""Minimum seconds between JWKS refetches triggered by an unknown ``kid``."""
_MAX_KID_LENGTH: Final = 256

# Asymmetric algorithms only, with the JWK key type each one requires.
_KTY_BY_ALG: Final[Mapping[str, str]] = {
    "RS256": "RSA",
    "RS384": "RSA",
    "RS512": "RSA",
    "PS256": "RSA",
    "PS384": "RSA",
    "PS512": "RSA",
    "ES256": "EC",
    "ES384": "EC",
    "ES512": "EC",
    "EdDSA": "OKP",
}
_DEFAULT_ALGORITHMS: Final = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")
_REQUIRED_CLAIMS: Final = ["exp", "iss", "aud", "sub"]


class PrincipalType(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. ``token`` is kept for on-behalf-of calls, never serialised."""

    subject: str
    type: PrincipalType
    client_id: str | None
    username: str | None
    scopes: frozenset[str]
    groups: frozenset[str]
    token: SecretStr


class AuthError(Exception):
    """The request is not authenticated (401) or lacks a scope (403)."""

    def __init__(
        self,
        description: str,
        code: Literal["invalid_token", "insufficient_scope"] = "invalid_token",
    ) -> None:
        super().__init__(description)
        self.code: Literal["invalid_token", "insufficient_scope"] = code
        self.description = description


class IdPUnavailable(Exception):
    """The identity provider (discovery or JWKS) could not be reached or answered badly."""


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_object(value: object) -> dict[str, Any]:
    """``value`` if it is a JSON object, else an empty dict."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _json_list(value: object) -> list[object]:
    """``value`` if it is a JSON array, else an empty list."""
    return cast(list[object], value) if isinstance(value, list) else []


class TokenValidator:
    """Validates bearer JWTs against one issuer's JWKS (NFR-SEC-01).

    Keys are cached by ``kid`` for ``jwks_max_age`` seconds. An unknown ``kid`` triggers
    one refetch (key rotation), at most once per :data:`JWKS_REFETCH_INTERVAL` seconds.
    Fetches are serialised by a lock, so concurrent requests on a cold cache fetch once.
    If a periodic refresh fails, the cached keys keep serving and the refresh is retried
    after the refetch interval. ``time_fn`` (monotonic seconds) is injectable for tests.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        http: httpx.AsyncClient,
        jwks_url: str | None = None,
        leeway: int = 60,
        agent_claim: str = "client_id",
        scope_group_map: Mapping[str, Sequence[str]] | None = None,
        algorithms: Sequence[str] = _DEFAULT_ALGORITHMS,
        jwks_max_age: float = 3600.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        unsupported = [alg for alg in algorithms if alg not in _KTY_BY_ALG]
        if unsupported or not algorithms:
            raise ValueError(
                f"unsupported token algorithm(s) {unsupported}; "
                f"allowed: {sorted(_KTY_BY_ALG)} (symmetric algorithms and 'none' are refused)"
            )
        group_map = dict(scope_group_map or {})
        unknown = sorted({s for scopes in group_map.values() for s in scopes} - set(SCOPES))
        if unknown:
            raise ValueError(f"scope_group_map names unknown scopes {unknown}; known: {SCOPES}")

        self._issuer = issuer
        self._audience = audience
        self._http = http
        self._jwks_uri = jwks_url
        self._leeway = leeway
        self._agent_claim = agent_claim
        self._group_map = {group: frozenset(scopes) for group, scopes in group_map.items()}
        self._algorithms = frozenset(algorithms)
        self._max_age = jwks_max_age
        self._time = time_fn

        self._keys: dict[str, dict[str, Any]] = {}
        self._pyjwks: dict[tuple[str, str], jwt.PyJWK] = {}
        self._loaded_at: float | None = None
        self._last_refetch: float | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    async def validate(self, token: str) -> Principal:
        """Return the principal for ``token``.

        Raises :class:`AuthError` for any token problem and :class:`IdPUnavailable` when
        the keys cannot be fetched.
        """
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise AuthError("malformed token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("malformed token") from None
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._algorithms:
            raise AuthError("token algorithm not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > _MAX_KID_LENGTH:
            raise AuthError("token has no valid key id")

        key = await self._signing_key(kid, alg)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("token expired") from None
        except jwt.ImmatureSignatureError:
            raise AuthError("token not yet valid") from None
        except jwt.InvalidAudienceError:
            raise AuthError("token audience not accepted") from None
        except jwt.InvalidIssuerError:
            raise AuthError("token issuer not accepted") from None
        except jwt.MissingRequiredClaimError:
            raise AuthError("token lacks a required claim") from None
        except jwt.PyJWTError:
            raise AuthError("invalid token") from None
        return self._principal(claims, token)

    def _principal(self, claims: dict[str, Any], token: str) -> Principal:
        subject = _str_or_none(claims.get("sub"))
        if subject is None:
            raise AuthError("token lacks a required claim")
        is_agent = claims.get(self._agent_claim) not in (None, "")
        scope_claim = claims.get("scope")
        granted = (
            frozenset(scope_claim.split()) if isinstance(scope_claim, str) else frozenset[str]()
        )
        groups = frozenset(g for g in _json_list(claims.get("groups")) if isinstance(g, str))
        mapped = frozenset(s for g in groups for s in self._group_map.get(g, ()))
        return Principal(
            subject=subject,
            type=PrincipalType.AGENT if is_agent else PrincipalType.HUMAN,
            client_id=_str_or_none(claims.get("client_id")) or _str_or_none(claims.get("azp")),
            username=None if is_agent else _str_or_none(claims.get("preferred_username")),
            scopes=(granted & frozenset(SCOPES)) | mapped,
            groups=groups,
            token=SecretStr(token),
        )

    # --- keys -----------------------------------------------------------------------

    async def _signing_key(self, kid: str, alg: str) -> jwt.PyJWK:
        jwk = await self._jwk(kid)  # always: enforces the cache max-age (key revocation)
        if jwk is None:
            raise AuthError("token signed by an unknown key")
        cached = self._pyjwks.get((kid, alg))
        if cached is not None:
            return cached
        if jwk.get("alg", alg) != alg or jwk.get("kty") != _KTY_BY_ALG[alg]:
            raise AuthError("token algorithm does not match its key")
        try:
            # Binding the algorithm here makes PyJWT verify with exactly this one.
            key = jwt.PyJWK(jwk, algorithm=alg)
        except jwt.PyJWTError:
            raise AuthError("token signed by an unusable key") from None
        self._pyjwks[(kid, alg)] = key
        return key

    async def _jwk(self, kid: str) -> dict[str, Any] | None:
        if self._loaded_at is None or self._time() - self._loaded_at >= self._max_age:
            await self._load(self._generation, unknown_kid=False)
            if self._loaded_at is None:
                raise IdPUnavailable("JWKS could not be loaded")
        jwk = self._keys.get(kid)
        if jwk is None and (
            self._last_refetch is None or self._time() - self._last_refetch >= JWKS_REFETCH_INTERVAL
        ):
            await self._load(self._generation, unknown_kid=True)
            jwk = self._keys.get(kid)
        return jwk

    async def _load(self, seen_generation: int, *, unknown_kid: bool) -> None:
        async with self._lock:
            if self._generation != seen_generation:
                return  # another request (re)loaded the keys while we waited
            self._generation += 1
            now = self._time()
            if unknown_kid:
                self._last_refetch = now
            try:
                keys = await self._fetch_jwks()
            except IdPUnavailable:
                if self._loaded_at is None or unknown_kid:
                    raise
                _log.warning("JWKS refresh failed; keeping cached keys", exc_info=True)
                self._loaded_at = now - self._max_age + JWKS_REFETCH_INTERVAL
                return
            self._keys = keys
            self._pyjwks.clear()
            self._loaded_at = now

    async def _fetch_jwks(self) -> dict[str, dict[str, Any]]:
        if self._jwks_uri is None:
            discovery = await self._get_json(
                f"{self._issuer.rstrip('/')}/.well-known/openid-configuration"
            )
            uri = _json_object(discovery).get("jwks_uri")
            if not isinstance(uri, str) or not uri.startswith(("https://", "http://")):
                raise IdPUnavailable("OIDC discovery document has no usable jwks_uri")
            self._jwks_uri = uri
        document = _json_object(await self._get_json(self._jwks_uri))
        if not isinstance(document.get("keys"), list):
            raise IdPUnavailable("JWKS document has no keys array")
        keys: dict[str, dict[str, Any]] = {}
        for item in _json_list(document["keys"]):
            entry = _json_object(item)
            kid = entry.get("kid")
            usable = entry.get("use", "sig") == "sig" and entry.get("kty") in {"RSA", "EC", "OKP"}
            if isinstance(kid, str) and kid and usable:
                keys[kid] = entry
        return keys

    async def _get_json(self, url: str) -> object:
        try:
            response = await self._http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise IdPUnavailable(f"GET {url} failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise IdPUnavailable(f"GET {url} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise IdPUnavailable(f"GET {url} returned invalid JSON") from None


def protected_resource_metadata(settings: Settings) -> dict[str, object]:
    """RFC 9728 OAuth Protected Resource Metadata (NFR-SEC-02)."""
    resource = settings.public_base_url
    if resource is None:
        raise ValueError("LAKELOOM_PUBLIC_URL must be set to publish resource metadata")
    if not settings.oidc_issuer:
        raise ValueError("LAKELOOM_OIDC_ISSUER must be set to publish resource metadata")
    return {
        "resource": resource,
        "authorization_servers": [settings.oidc_issuer],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
    }


class BearerAuthMiddleware:
    """Pure ASGI middleware guarding every HTTP and WebSocket path except ``public_paths``.

    On success the :class:`Principal` is stored at ``scope["state"]["principal"]``
    (``request.state.principal``). Failures: 401 ``invalid_token`` with an RFC 9728
    challenge, or 503 ``upstream_unavailable`` when the IdP is unreachable (FR-AGT-05).
    WebSocket failures close the handshake with code 1008 (policy violation).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        validator: TokenValidator,
        resource_metadata_url: str,
        public_paths: Sequence[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self._validator = validator
        self._public = frozenset(public_paths)
        self._metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope["path"] in self._public:
            await self.app(scope, receive, send)
            return
        values = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
        try:
            principal = await self._validator.validate(self._bearer_token(values))
        except AuthError as exc:
            _log.info("authentication failed: %s", exc.description)
            await self._reject(scope, receive, send, exc.description, token_given=bool(values))
            return
        except IdPUnavailable:
            _log.warning("identity provider unavailable", exc_info=True)
            await self._unavailable(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)

    @staticmethod
    def _bearer_token(values: list[bytes]) -> str:
        if not values:
            raise AuthError("missing bearer token")
        if len(values) > 1:
            raise AuthError("multiple Authorization headers")
        scheme, _, token = values[0].decode("latin-1").strip().partition(" ")
        if scheme.lower() != "bearer":
            raise AuthError("authorization scheme must be Bearer")
        token = token.strip()
        if not token:
            raise AuthError("empty bearer token")
        return token

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, description: str, *, token_given: bool
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        error = 'error="invalid_token", ' if token_given else ""
        response = JSONResponse(
            {"error": "invalid_token", "error_description": description},
            status_code=401,
            headers={"WWW-Authenticate": f'Bearer {error}resource_metadata="{self._metadata_url}"'},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _unavailable(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1011})
            return
        response = JSONResponse(
            {"error": "upstream_unavailable", "hint": "identity provider unreachable"},
            status_code=503,
        )
        await response(scope, receive, send)
