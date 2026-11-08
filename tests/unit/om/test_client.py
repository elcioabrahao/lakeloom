"""OMClient behaviour with a mocked transport (FR-OM-01, FR-OM-06)."""

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakeloom.om.client import OMClient, OMError, OMUnavailable
from lakeloom.om.models import OMVersion

BASE = "http://om.test:8585"
RDF = {"enabled": True, "storageType": "FUSEKI", "projectionState": "READY"}


@pytest.fixture
def client() -> OMClient:
    return OMClient(BASE, SecretStr("t"))


@respx.mock
async def test_fr_om_01_get_version_parses_response(client: OMClient) -> None:
    route = respx.get(f"{BASE}/api/v1/system/version").respond(
        json={"version": "2.1.0", "revision": "abc", "timestamp": 1}
    )

    got = await client.get_version()

    assert got == OMVersion(version="2.1.0", revision="abc", timestamp=1)
    assert route.calls.last.request.headers["Authorization"] == "Bearer t"
    await client.aclose()


@respx.mock
async def test_fr_om_06_capabilities_rdf_enabled(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/system/version").respond(json={"version": "2.1.0"})
    respx.get(f"{BASE}/api/v1/rdf/status").respond(json=RDF)

    caps = await client.get_capabilities()

    assert caps.reachable is True
    assert caps.version == "2.1.0"
    assert caps.rdf_enabled is True
    assert caps.rdf is not None and caps.rdf.enabled is True
    assert caps.error is None


@respx.mock
async def test_fr_om_06_capabilities_rdf_endpoint_404_degrades(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/system/version").respond(json={"version": "2.0.7"})
    respx.get(f"{BASE}/api/v1/rdf/status").respond(404)

    caps = await client.get_capabilities()

    assert caps.reachable is True
    assert caps.rdf_enabled is None
    assert caps.rdf is None


@respx.mock
async def test_fr_om_06_capabilities_when_om_unreachable(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/system/version").mock(side_effect=httpx.ConnectError("refused"))

    caps = await client.get_capabilities()

    assert caps.reachable is False
    assert caps.error is not None and "connect" in caps.error.lower()
    assert caps.rdf_enabled is None


@respx.mock
async def test_fr_om_06_capabilities_other_failure_not_raised(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/system/version").respond(401)

    caps = await client.get_capabilities()

    assert caps.reachable is False
    assert caps.error is not None and "401" in caps.error


@respx.mock
async def test_om_5xx_raises_om_unavailable(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/system/version").respond(503)

    with pytest.raises(OMUnavailable) as info:
        await client.get_version()
    assert info.value.status == 503


@respx.mock
async def test_om_timeout_raises_om_unavailable(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/system/version").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(OMUnavailable):
        await client.get_version()


@respx.mock
async def test_om_4xx_raises_om_error_with_status(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/system/version").respond(403)

    with pytest.raises(OMError) as info:
        await client.get_version()
    assert not isinstance(info.value, OMUnavailable)
    assert info.value.status == 403


@respx.mock
async def test_nfr_sec_11_token_not_in_errors_or_repr() -> None:
    c = OMClient(BASE, SecretStr("sekret-token"))
    respx.get(f"{BASE}/api/v1/system/version").respond(500)

    with pytest.raises(OMUnavailable) as info:
        await c.get_version()

    assert "sekret-token" not in str(info.value)
    assert "sekret-token" not in repr(c)


@respx.mock
async def test_no_token_sends_no_authorization_header() -> None:
    c = OMClient(BASE, None)
    route = respx.get(f"{BASE}/api/v1/system/version").respond(json={"version": "2.1.0"})

    await c.get_version()

    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
async def test_injected_http_client_is_not_closed() -> None:
    http = httpx.AsyncClient()
    c = OMClient(BASE, SecretStr("t"), http=http)
    respx.get(f"{BASE}/api/v1/system/version").respond(json={"version": "2.1.0"})

    await c.get_version()
    await c.aclose()

    assert not http.is_closed
    await http.aclose()


async def test_owned_http_client_is_closed_on_aclose(client: OMClient) -> None:
    await client.aclose()
    assert client._http.is_closed  # pyright: ignore[reportPrivateUsage]
