"""Fixtures for the auth tests: signing keys, a JWKS, a token factory and a fake IdP.

The fake IdP is an ``httpx.MockTransport`` serving OIDC discovery and the JWKS, with
hit counters, so tests can assert exactly how often Lakeloom talks to the IdP.
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

ISSUER = "https://idp.test/realms/lakeloom"
AUDIENCE = "lakeloom"
KID = "test-rsa-1"
EC_KID = "test-ec-1"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


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


@pytest.fixture
def kit(
    rsa_key: rsa.RSAPrivateKey,
    other_rsa_key: rsa.RSAPrivateKey,
    ec_key: ec.EllipticCurvePrivateKey,
) -> AuthKit:
    return AuthKit(rsa_key=rsa_key, other_rsa_key=other_rsa_key, ec_key=ec_key)


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


@pytest.fixture
def jwks(rsa_key: rsa.RSAPrivateKey, ec_key: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    enc_key = {**rsa_jwk(rsa_key, kid="enc-1"), "use": "enc", "alg": "RSA-OAEP"}
    return {"keys": [enc_key, rsa_jwk(rsa_key), ec_jwk(ec_key)]}


@pytest.fixture
def idp(jwks: dict[str, Any]) -> FakeIdP:
    return FakeIdP(jwks_responses=[jwks])


@pytest.fixture
def http(idp: FakeIdP) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))


class Clock:
    """Injectable monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()
