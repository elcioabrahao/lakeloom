"""Shared helpers for the server unit tests: keys/JWK helpers, token builders, a fake IdP.

Imported by tests as ``from kit import ...``; fixtures live in ``conftest.py``. A uniquely
named module avoids clashing with other ``conftest`` modules on ``sys.path``.

The fake IdP is an ``httpx.MockTransport`` handler serving OIDC discovery and the JWKS, with
hit counters, so tests can assert exactly how often Lakeloom talks to the IdP.
"""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from lakeloom.server.http_app import AppDeps
from lakeloom.sources.base import LakeSource

ISSUER = "https://idp.test/realms/lakeloom"
AUDIENCE = "lakeloom"
KID = "test-rsa-1"
EC_KID = "test-ec-1"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


def rsa_jwk(key: rsa.RSAPrivateKey, kid: str = KID, **extra: str) -> dict[str, Any]:
    jwk: dict[str, Any] = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256", **extra}


def ec_jwk(key: ec.EllipticCurvePrivateKey, kid: str = EC_KID) -> dict[str, Any]:
    jwk: dict[str, Any] = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "ES256"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned_token(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    """Assemble a JWS by hand (for alg=none and other forgeries PyJWT refuses to make)."""
    h = b64url(json.dumps(header).encode())
    p = b64url(json.dumps(claims).encode())
    return f"{h}.{p}.{b64url(signature)}"


def hs256_forgery(claims: dict[str, Any], secret: bytes, kid: str = KID) -> str:
    """Alg-confusion token: HS256 'signed' with the RSA public key as the HMAC secret."""
    h = b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    p = b64url(json.dumps(claims).encode())
    sig = hmac.new(secret, f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{b64url(sig)}"


def public_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def base_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "iat": now,
        "exp": now + 300,
        "scope": "lakeloom:read",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


@dataclass
class AuthKit:
    """Constants and token builders, exposed as the ``kit`` fixture."""

    rsa_key: rsa.RSAPrivateKey
    other_rsa_key: rsa.RSAPrivateKey
    ec_key: ec.EllipticCurvePrivateKey
    issuer: str = ISSUER
    audience: str = AUDIENCE
    kid: str = KID
    ec_kid: str = EC_KID
    jwks_url: str = JWKS_URL

    def token(
        self,
        *,
        key: Any = None,
        alg: str = "RS256",
        kid: str | None = KID,
        headers: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> str:
        """Signed token with ``base_claims(**overrides)``; ``None`` drops a claim."""
        hdrs: dict[str, Any] = dict(headers or {})
        if kid is not None:
            hdrs["kid"] = kid
        return jwt.encode(
            base_claims(**overrides), key or self.rsa_key, algorithm=alg, headers=hdrs
        )

    claims = staticmethod(base_claims)
    unsigned = staticmethod(unsigned_token)
    hs256_forgery = staticmethod(hs256_forgery)
    rsa_jwk = staticmethod(rsa_jwk)

    def public_pem(self) -> bytes:
        return public_pem(self.rsa_key)


@dataclass
class FakeIdP:
    """Serves discovery and a JWKS. ``jwks_responses`` is consumed in order; the last repeats."""

    jwks_responses: list[dict[str, Any]]
    down: bool = False
    jwks_status: int = 200
    discovery_hits: int = 0
    jwks_hits: int = 0
    requests: list[str] = field(default_factory=list[str])

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if url == DISCOVERY_URL:
            self.discovery_hits += 1
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        if url == JWKS_URL:
            index = min(self.jwks_hits, len(self.jwks_responses) - 1)
            self.jwks_hits += 1
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status, text="boom")
            return httpx.Response(200, json=self.jwks_responses[index])
        return httpx.Response(404)


class Clock:
    """Injectable monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- App dependencies with a fake OpenMetadata (Task 7: get_server_info / MCP) ---

OM_BOT_TOKEN = "om-bot-token-should-never-leak"  # noqa: S105


def om_up(request: httpx.Request) -> httpx.Response:
    """Fake OM 2.1.0 with RDF enabled."""
    if request.url.path == "/api/v1/system/version":
        return httpx.Response(200, json={"version": "2.1.0", "revision": "abc"})
    if request.url.path == "/api/v1/rdf/status":
        return httpx.Response(200, json={"enabled": True, "storageType": "FUSEKI"})
    return httpx.Response(404)


def om_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class DepsFactory(Protocol):
    def __call__(
        self,
        om_handler: Callable[[httpx.Request], httpx.Response] = ...,
        sources: dict[str, LakeSource] | None = ...,
        **settings: Any,
    ) -> AppDeps: ...


# --- In-process MCP client over the full ASGI app (Task 7 / M1a Task 9) ---

BASE = "https://lakeloom.test"


@asynccontextmanager
async def mcp_session(
    app: FastAPI, token: str | None, *, client_name: str = "mcp"
) -> AsyncIterator[ClientSession]:
    """The official MCP client against ``app`` via ``httpx2.ASGITransport``; runs the lifespan."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers
        ) as http,
        streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write),
        ClientSession(
            read, write, client_info=Implementation(name=client_name, version="0")
        ) as session,
    ):
        await session.initialize()
        yield session
