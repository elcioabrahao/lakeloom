"""OMClient table lookup by FQN with a mocked transport (FR-SRC-07)."""

import pytest
import respx
from pydantic import SecretStr

from lakeloom.om.client import OMClient, OMError

BASE = "http://om.test:8585"
FQN = "lakeloom_fixture.retail.sales.customers"


@pytest.fixture
def client() -> OMClient:
    return OMClient(BASE, SecretStr("t"))


@respx.mock
async def test_fr_src_07_get_table_by_fqn(client: OMClient) -> None:
    route = respx.get(f"{BASE}/api/v1/tables/name/{FQN}").respond(
        json={
            "id": "11111111-1111-1111-1111-111111111111",
            "fullyQualifiedName": FQN,
            "description": "Customers",
            "columns": [
                {
                    "name": "email",
                    "dataType": "VARCHAR",
                    "tags": [{"tagFQN": "PII.Sensitive", "source": "Classification"}],
                }
            ],
            "tags": [],
            "owners": [{"id": "x", "type": "user", "name": "a"}],
        }
    )

    table = await client.get_table_by_fqn(FQN)

    assert table is not None
    assert table.fully_qualified_name == FQN
    assert table.columns[0].tags[0].tag_fqn == "PII.Sensitive"
    assert table.owners[0].name == "a"
    request = route.calls.last.request
    assert request.url.params["fields"] == "columns,tags,owners,tableConstraints"
    assert request.headers["Authorization"] == "Bearer t"
    await client.aclose()


@respx.mock
async def test_fr_src_07_get_table_by_fqn_encodes_path_segment(client: OMClient) -> None:
    route = respx.get(url__regex=rf"{BASE}/api/v1/tables/name/.*").respond(
        json={"id": "i", "fullyQualifiedName": 'svc."a/b".c.d'}
    )

    await client.get_table_by_fqn('svc."a/b".c.d', fields=())

    raw = route.calls.last.request.url.raw_path.decode()
    assert raw == "/api/v1/tables/name/svc.%22a%2Fb%22.c.d"
    await client.aclose()


@respx.mock
async def test_fr_src_07_get_table_by_fqn_404_returns_none(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/tables/name/{FQN}").respond(404, json={"code": 404})

    assert await client.get_table_by_fqn(FQN) is None
    await client.aclose()


@respx.mock
async def test_fr_src_07_get_table_by_fqn_other_errors_raise(client: OMClient) -> None:
    respx.get(f"{BASE}/api/v1/tables/name/{FQN}").respond(403, json={})

    with pytest.raises(OMError) as info:
        await client.get_table_by_fqn(FQN)
    assert info.value.status == 403
    await client.aclose()
