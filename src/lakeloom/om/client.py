"""Async OpenMetadata client for the M0 surface (FR-OM-01, FR-OM-06, NFR-SEC-11).

Paths are keys of the committed spec (``servers[0].url`` is ``/api``), verified by
``tests/unit/om/test_spec_paths.py``. The bot token is only ever placed in the request
header; it is never part of an exception message, log line or repr.
"""

from typing import ClassVar

import httpx
from pydantic import SecretStr

from lakeloom.metrics import OM_REQUEST_SECONDS
from lakeloom.om.models import OMCapabilities, OMVersion, RdfStatus

API_PREFIX = "/api"


class OMError(Exception):
    """OpenMetadata answered with an error (or could not be used)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OMUnavailable(OMError):
    """OpenMetadata is down, too slow, or answered 5xx."""


class OMClient:
    PATHS: ClassVar[dict[str, tuple[str, str]]] = {
        "get_version": ("GET", "/v1/system/version"),
        "get_rdf_status": ("GET", "/v1/rdf/status"),
    }

    def __init__(
        self,
        base_url: str,
        token: SecretStr | None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=timeout)

    def __repr__(self) -> str:
        return f"OMClient(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _request(self, name: str) -> httpx.Response:
        method, path = self.PATHS[name]
        headers = {"Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        url = f"{self._base_url}{API_PREFIX}{path}"
        try:
            with OM_REQUEST_SECONDS.labels(operation=name).time():
                response = await self._http.request(method, url, headers=headers)
        except httpx.TimeoutException as exc:
            raise OMUnavailable(f"OpenMetadata timed out ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise OMUnavailable(f"cannot connect to OpenMetadata ({type(exc).__name__})") from None
        status = response.status_code
        if status >= 500:
            raise OMUnavailable(f"OpenMetadata returned HTTP {status}", status)
        if status >= 400:
            raise OMError(f"OpenMetadata returned HTTP {status}", status)
        return response

    async def get_version(self) -> OMVersion:
        return OMVersion.model_validate(_json(await self._request("get_version")))

    async def get_rdf_status(self) -> RdfStatus:
        return RdfStatus.model_validate(_json(await self._request("get_rdf_status")))

    async def get_capabilities(self) -> OMCapabilities:
        """Probe version and RDF status; never raises (FR-OM-06)."""
        try:
            version = await self.get_version()
        except OMError as exc:
            return OMCapabilities(reachable=False, error=str(exc))
        except Exception as exc:
            return OMCapabilities(reachable=False, error=f"unexpected error: {type(exc).__name__}")
        try:
            rdf = await self.get_rdf_status()
        except OMError as exc:
            if exc.status == 404:
                return OMCapabilities(reachable=True, version=version.version)
            return OMCapabilities(reachable=True, version=version.version, error=str(exc))
        except Exception as exc:
            error = f"unexpected error: {type(exc).__name__}"
            return OMCapabilities(reachable=True, version=version.version, error=error)
        return OMCapabilities(
            reachable=True, version=version.version, rdf_enabled=rdf.enabled, rdf=rdf
        )


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        raise OMError("OpenMetadata returned a non-JSON body", response.status_code) from None
