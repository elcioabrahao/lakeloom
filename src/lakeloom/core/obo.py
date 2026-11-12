"""On-behalf-of OpenMetadata access for human reviewers (NFR-SEC-08..11, ADR 0012).

A human's review/approve runs against OpenMetadata as that human, never as the bot:

* ``exchange``: RFC 8693 token exchange at the IdP yields an OM-audience token (preferred);
* ``forward``: the caller's own token is sent to OM, for deployments where OM accepts
  Lakeloom's audience;
* ``unavailable``: neither is configured, so human-only operations fail closed.

An exchange error never falls back to forwarding or to the bot. Subject and exchanged
tokens and the client secret never appear in logs, errors or exception chains; every
failure is raised ``from None``. Exchanged tokens are cached in memory keyed by the
SHA-256 of the subject token until 30 s before they expire (at most 256 entries, oldest
evicted); :meth:`OnBehalfOf.forget` drops one when OM rejected it (HTTP 401). Concurrent first
calls for the same subject token may each exchange once; the cache is a latency
optimisation, not a correctness mechanism, so no lock is held. Forward mode needs
``om_forward_user_token`` with ``oidc_token_exchange`` off.
"""

import base64
import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final, Literal, cast

import httpx
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient
from lakeloom.server.auth import Principal, PrincipalType

__all__ = ["OboMode", "OnBehalfOf"]

OboMode = Literal["exchange", "forward", "unavailable"]

_GRANT: Final = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN: Final = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105
_EXPIRY_MARGIN: Final = 30
_CACHE_MAX: Final = 256
_TIMEOUT: Final = 5.0


class OnBehalfOf:
    """Builds per-human :class:`OMClient` instances (see module docstring)."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        om: OMClient,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http
        self._om = om
        self._time = time_fn
        self._token_endpoint = settings.oidc_token_endpoint
        self._cache: OrderedDict[str, tuple[SecretStr, float]] = OrderedDict()
        if (
            settings.oidc_token_exchange
            and settings.oidc_client_id
            and settings.oidc_client_secret is not None
        ):
            self.mode: OboMode = "exchange"
        elif not settings.oidc_token_exchange and settings.om_forward_user_token:
            self.mode = "forward"
        else:
            self.mode = "unavailable"

    async def om_for(self, principal: Principal) -> OMClient:
        """An OM client acting as ``principal``; raises a typed error, never uses the bot."""
        if principal.type is not PrincipalType.HUMAN:
            raise LakeloomError(
                "forbidden",
                "on-behalf-of OM access is for human principals",
                hint="review and approval need a signed-in human; agents cannot do them",
            )
        if self.mode == "unavailable":
            raise LakeloomError(
                "forbidden",
                "human review is not configured",
                hint=(
                    "operator: set LAKELOOM_OIDC_CLIENT_ID/_SECRET for token exchange, or "
                    "LAKELOOM_OM_FORWARD_USER_TOKEN=true with LAKELOOM_OIDC_TOKEN_EXCHANGE=false "
                    "when OpenMetadata accepts Lakeloom's tokens (NFR-SEC-10)"
                ),
            )
        if self.mode == "forward":
            return self._om.with_token(principal.token)
        return self._om.with_token(await self._exchange(principal.token))

    def forget(self, principal: Principal) -> None:
        """Drop the cached exchanged token for ``principal``'s subject token (OpenMetadata
        rejected it, HTTP 401), so the next call exchanges again. A no-op when none is cached."""
        self._cache.pop(_key(principal.token.get_secret_value()), None)

    async def _exchange(self, subject: SecretStr) -> SecretStr:
        raw = subject.get_secret_value()
        key = _key(raw)
        now = self._time()
        hit = self._cache.get(key)
        if hit is not None:
            if now < hit[1]:
                return hit[0]
            del self._cache[key]

        settings = self._settings
        client_id, client_secret = settings.oidc_client_id, settings.oidc_client_secret
        if not client_id or client_secret is None:  # unreachable: mode == "exchange"
            raise _unavailable("token exchange is not configured")
        endpoint = await self._endpoint()
        form = {
            "grant_type": _GRANT,
            "subject_token": raw,
            "subject_token_type": _ACCESS_TOKEN,
            "requested_token_type": _ACCESS_TOKEN,
            "audience": settings.om_audience,
        }
        if settings.om_exchange_scope is not None:
            form["scope"] = settings.om_exchange_scope
        credentials = f"{client_id}:{client_secret.get_secret_value()}"
        basic = base64.b64encode(credentials.encode()).decode()
        try:
            response = await self._http.post(
                endpoint,
                data=form,
                headers={"Authorization": f"Basic {basic}"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError:
            raise _unavailable("the identity provider did not answer the token exchange") from None
        if response.status_code in (400, 401, 403):
            raise LakeloomError(
                "forbidden",
                "token exchange refused",
                hint=(
                    "the identity provider refused to issue an OpenMetadata token for this "
                    "user; sign in again or ask an operator to check the exchange policy"
                ),
            ) from None
        if response.status_code >= 400:
            raise _unavailable("the identity provider failed the token exchange") from None
        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = None
        body: dict[str, Any] = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise _unavailable("the token exchange returned no access token") from None
        exchanged = SecretStr(access)
        expires_in = body.get("expires_in")
        if isinstance(expires_in, int | float) and expires_in > _EXPIRY_MARGIN:
            self._cache[key] = (exchanged, now + expires_in - _EXPIRY_MARGIN)
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)
        return exchanged

    async def _endpoint(self) -> str:
        if self._token_endpoint is not None:
            return self._token_endpoint
        issuer = (self._settings.oidc_issuer or "").rstrip("/")
        try:
            response = await self._http.get(
                f"{issuer}/.well-known/openid-configuration", timeout=_TIMEOUT
            )
            response.raise_for_status()
            endpoint = response.json().get("token_endpoint")
        except (httpx.HTTPError, ValueError, AttributeError):
            raise _unavailable(
                "the identity provider's discovery document is unavailable"
            ) from None
        if not isinstance(endpoint, str) or not endpoint:
            raise _unavailable("the identity provider's discovery document has no token endpoint")
        self._token_endpoint = endpoint
        return endpoint


def _key(raw: str) -> str:
    """The cache key of a subject token: its SHA-256 (the token itself is never kept)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _unavailable(message: str) -> LakeloomError:
    return LakeloomError(
        "upstream_unavailable", message, hint="retry shortly; if it persists, tell an operator"
    )
