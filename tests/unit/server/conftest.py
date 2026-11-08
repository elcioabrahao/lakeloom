"""Fixtures for the server unit tests: signing keys, a JWKS, a fake IdP and app deps.

Helpers (token builders, ``FakeIdP``, fake OM handlers) live in ``kit.py``.
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from kit import (
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    OM_BOT_TOKEN,
    AuthKit,
    Clock,
    DepsFactory,
    FakeIdP,
    ec_jwk,
    om_up,
    rsa_jwk,
)
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.om.client import OMClient
from lakeloom.server.auth import TokenValidator
from lakeloom.server.http_app import AppDeps
from lakeloom.state.db import create_engine


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def kit(
    rsa_key: rsa.RSAPrivateKey,
    other_rsa_key: rsa.RSAPrivateKey,
    ec_key: ec.EllipticCurvePrivateKey,
) -> AuthKit:
    return AuthKit(rsa_key=rsa_key, other_rsa_key=other_rsa_key, ec_key=ec_key)


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


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def make_deps(tmp_path: Path, http: httpx.AsyncClient) -> AsyncIterator[DepsFactory]:
    """Build ``AppDeps`` with a fake OM and a validator backed by the fake IdP."""
    created: list[AppDeps] = []

    def factory(
        om_handler: Callable[[httpx.Request], httpx.Response] = om_up, **settings: Any
    ) -> AppDeps:
        values: dict[str, Any] = {
            "om_url": "http://om.test:8585",
            "om_bot_token": SecretStr(OM_BOT_TOKEN),
            "oidc_issuer": ISSUER,
            "public_url": "https://lakeloom.test",
            "db_url": f"sqlite:///{tmp_path}/l.db",
            "data_dir": tmp_path,
            **settings,
        }
        s = Settings(**values)
        om_http = httpx.AsyncClient(transport=httpx.MockTransport(om_handler))
        validator = TokenValidator(issuer=ISSUER, audience=AUDIENCE, http=http, jwks_url=JWKS_URL)
        deps = AppDeps(
            settings=s,
            om=OMClient(str(s.om_url), s.om_bot_token, http=om_http),
            engine=create_engine(s.db_url),
            validator=validator,
        )
        created.append(deps)
        return deps

    yield factory
    for deps in created:
        await deps.engine.dispose()
