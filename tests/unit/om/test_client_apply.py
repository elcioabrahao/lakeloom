"""OMClient apply, per-identity clients, permissions and asset tags (FR-OM-04, FR-OM-05,
FR-REV-02, NFR-SEC-11).

Response bodies were captured from the live 2.1.0-rc1 server on 2026-11-12 (ADR 0012
§ Apply research; ids and names shortened).
"""

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakeloom.om.client import OMClient, OMConflict, OMError, OMForbidden
from lakeloom.om.models import APPLY_PERMISSIONS, OMChangeSet
from lakeloom.om.tags import glossary_tag, merge_tags, remove_tags

BASE = "http://om.test:8585"
API = f"{BASE}/api/v1"
CSID = "5e2b520e-a1d1-4132-978a-f23bf139a91f"
GID = "e94efb2d-5632-4379-a478-948b04170574"
TID = "0b8b7f4e-56e3-4bb5-9a31-8f4e1d6c2a10"
WIDGET = "2dd4d6e3-0c21-5178-8a77-c82037474bd9"  # the CREATE_TERM draft id, kept by OM
BASE_TERM = "f9095af6-0126-4f69-b594-c1e1fc470f49"
OP_CREATE = "d0ec01a0-31b8-4094-b6fe-9c65d43017a8"
OP_UPDATE = "74a528f9-9485-4cbb-aa67-8bd8615668bc"
USER_TOKEN = "alice-om-token-value"  # noqa: S105 - test value
BOT_TOKEN = "bot-token-value"  # noqa: S105 - test value

APPLICATION_RESULT = {
    "appliedAt": 1794501966989,
    "appliedBy": "alice",
    "operationsProcessed": 2,
    "operationsApplied": 2,
    "operationsFailed": 0,
    "results": [
        {
            "operationId": OP_CREATE,
            "status": "APPLIED",
            "entity": {
                "id": WIDGET,
                "type": "glossaryTerm",
                "name": "Widget",
                "fullyQualifiedName": "ll-r2.Widget",
                "description": "A widget",
                "displayName": "Widget",
                "deleted": False,
                "href": f"http://localhost:8585/api/v1/glossaryTerms/{WIDGET}",
            },
            "entityVersion": 0.1,
        },
        {
            "operationId": OP_UPDATE,
            "status": "APPLIED",
            "entity": {
                "id": BASE_TERM,
                "type": "glossaryTerm",
                "name": "Base",
                "fullyQualifiedName": "ll-r2.Base",
                "description": "Base UPDATED",
                "deleted": False,
            },
            "entityVersion": 0.2,
        },
    ],
}

# GET /v1/permissions/glossary/{id} as carol (no reviewer role; DataConsumer only)
CAROL_GLOSSARY_PERMISSIONS = {
    "resource": "glossary",
    "permissions": [
        {"operation": "Create", "access": "notAllow"},
        {"operation": "Delete", "access": "notAllow"},
        {"operation": "ViewAll", "access": "allow", "policy": "DataConsumerPolicy"},
        {"operation": "EditAll", "access": "notAllow"},
        {"operation": "EditGlossaryTerms", "access": "allow"},
        {"operation": "EditTags", "access": "allow"},
        {"operation": "EditOwners", "access": "conditionalAllow"},
    ],
}

TABLE = {
    "id": TID,
    "name": "orders",
    "fullyQualifiedName": "svc.db.sales.orders",
    "version": 0.3,
    "tags": [
        {
            "tagFQN": "PII.Sensitive",
            "source": "Classification",
            "labelType": "Manual",
            "state": "Confirmed",
        }
    ],
    "columns": [
        {"name": "order_id", "dataType": "INT", "tags": []},
        {
            "name": "email",
            "dataType": "STRING",
            "tags": [
                # OM shows a table's tags on its columns as Derived: not the column's own
                {
                    "tagFQN": "PII.Sensitive",
                    "source": "Classification",
                    "labelType": "Derived",
                    "state": "Confirmed",
                },
                {
                    "tagFQN": "retail.EmailAddress",
                    "source": "Glossary",
                    "labelType": "Manual",
                    "state": "Confirmed",
                },
            ],
        },
    ],
}


def change_set(state: str, application_result: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": CSID,
        "name": "ll-cs",
        "description": "research",
        "glossaries": [{"id": GID, "type": "glossary"}],
        "state": state,
        "operations": [],
        "version": 0.2,
    }
    if application_result is not None:
        out["applicationResult"] = application_result
    return out


def mock_lease() -> tuple[respx.Route, respx.Route]:
    def acquire(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "resourceType": "ontologyChangeSet",
                "resourceId": CSID,
                "sessionId": sent["sessionId"],
                "version": 3,
            },
        )

    acquire_route = respx.post(f"{API}/ontologyEditLocks/acquire").mock(side_effect=acquire)
    release_route = respx.delete(f"{API}/ontologyEditLocks/ontologyChangeSet/{CSID}").respond(404)
    return acquire_route, release_route


@pytest.fixture
def client() -> OMClient:
    return OMClient(BASE, SecretStr(BOT_TOKEN))


def _auth(route: respx.Route, call: int = -1) -> str:
    return route.calls[call].request.headers["Authorization"]


# --- with_token -----------------------------------------------------------------------------


@respx.mock
async def test_with_token_sends_that_token_and_shares_transport() -> None:
    http = httpx.AsyncClient()
    bot = OMClient(BASE, SecretStr(BOT_TOKEN), http=http)
    user = bot.with_token(SecretStr(USER_TOKEN))
    route = respx.get(f"{API}/users/loggedInUser").respond(
        200, json={"id": GID, "name": "alice", "email": "alice@example.invalid", "isBot": False}
    )
    me = await user.get_current_user()
    await bot.get_current_user()
    assert (me.id, me.name, me.email) == (GID, "alice", "alice@example.invalid")
    assert _auth(route, 0) == f"Bearer {USER_TOKEN}"
    assert _auth(route, 1) == f"Bearer {BOT_TOKEN}"
    assert user._http is bot._http  # pyright: ignore[reportPrivateUsage]
    await user.aclose()  # not the owner: the shared transport stays open
    assert not http.is_closed
    await http.aclose()


@respx.mock
async def test_identities_sharing_a_transport_share_no_cookies() -> None:
    """A Set-Cookie OM sends to one identity is never sent by another (nor kept at all)."""
    http = httpx.AsyncClient()
    bot = OMClient(BASE, SecretStr(BOT_TOKEN), http=http)
    alice = bot.with_token(SecretStr(USER_TOKEN))
    bob = bot.with_token(SecretStr("bob-om-token-value"))
    user = {"id": GID, "name": "alice", "email": "alice@example.invalid", "isBot": False}
    route = respx.get(f"{API}/users/loggedInUser").mock(
        side_effect=[
            httpx.Response(200, json=user, headers={"Set-Cookie": "JSESSIONID=alice; Path=/"}),
            httpx.Response(200, json=user),
            httpx.Response(200, json=user),
        ]
    )
    await alice.get_current_user()
    await bob.get_current_user()
    await bot.get_current_user()
    assert all("cookie" not in call.request.headers for call in route.calls)
    assert len(http.cookies.jar) == 0
    await http.aclose()


@respx.mock
async def test_http_401_marks_the_session_rejected() -> None:
    http = httpx.AsyncClient()
    alice = OMClient(BASE, None, http=http).with_token(SecretStr(USER_TOKEN))
    respx.get(f"{API}/users/loggedInUser").respond(401, json={"message": "expired"})
    assert alice.session_rejected is False
    with pytest.raises(OMError) as info:
        await alice.get_current_user()
    assert info.value.status == 401 and alice.session_rejected is True
    assert USER_TOKEN not in str(info.value)
    await http.aclose()


async def test_with_token_of_an_owning_client_does_not_close_it() -> None:
    bot = OMClient(BASE, SecretStr(BOT_TOKEN))
    user = bot.with_token(SecretStr(USER_TOKEN))
    await user.aclose()
    assert not bot._http.is_closed  # pyright: ignore[reportPrivateUsage]
    await bot.aclose()
    assert bot._http.is_closed  # pyright: ignore[reportPrivateUsage]


def test_repr_hides_token(client: OMClient) -> None:
    user = client.with_token(SecretStr(USER_TOKEN))
    for c in (client, user):
        text = f"{c!r} {c}"
        assert USER_TOKEN not in text and BOT_TOKEN not in text
        assert "om.test" in repr(c)


# --- apply ----------------------------------------------------------------------------------


@respx.mock
async def test_apply_parses_application_result(client: OMClient) -> None:
    acquire, release = mock_lease()
    route = respx.post(f"{API}/ontologyChangeSets/{CSID}/apply").respond(
        200, json=change_set("APPLIED", APPLICATION_RESULT)
    )
    cs = await client.with_token(SecretStr(USER_TOKEN)).apply_change_set(CSID)
    assert isinstance(cs, OMChangeSet)
    assert cs.state == "APPLIED"
    result = cs.application_result
    assert result is not None
    assert result.applied_by == "alice"
    assert result.operations_failed == 0
    assert [
        (r.operation_id, r.status, r.entity_id, r.entity_fqn, r.entity_version)
        for r in result.results
    ] == [
        (OP_CREATE, "APPLIED", WIDGET, "ll-r2.Widget", 0.1),
        (OP_UPDATE, "APPLIED", BASE_TERM, "ll-r2.Base", 0.2),
    ]
    sent = json.loads(route.calls[0].request.content)
    lease_session = json.loads(acquire.calls[0].request.content)["sessionId"]
    assert sent == {"lease": {"sessionId": lease_session, "version": 3}}
    assert _auth(acquire) == _auth(route) == f"Bearer {USER_TOKEN}"
    assert release.called  # apply already released it (404): best effort, ignored


@respx.mock
async def test_apply_stale_base_maps_to_conflict_with_entities(client: OMClient) -> None:
    mock_lease()
    message = (
        "Ontology operation '12201b95-917b-4e81-b682-e8fc7fdf0c8b' expected version 0.1 "
        "but found 0.2"
    )
    respx.post(f"{API}/ontologyChangeSets/{CSID}/apply").respond(
        400, json={"code": 400, "message": message}
    )
    with pytest.raises(OMConflict) as info:
        await client.apply_change_set(CSID)
    exc = info.value
    assert exc.kind == "stale_base"
    assert exc.status == 400
    assert exc.entities == ("12201b95-917b-4e81-b682-e8fc7fdf0c8b",)
    assert exc.hint


@respx.mock
async def test_apply_412_is_stale_revision(client: OMClient) -> None:
    mock_lease()
    respx.post(f"{API}/ontologyChangeSets/{CSID}/apply").respond(
        412, json={"code": 412, "message": "Ontology changed since this draft was created"}
    )
    with pytest.raises(OMConflict) as info:
        await client.apply_change_set(CSID)
    assert info.value.kind == "stale_revision"
    assert info.value.entities == ()
    assert info.value.hint


@respx.mock
async def test_403_maps_to_om_forbidden(client: OMClient) -> None:
    acquire = respx.post(f"{API}/ontologyEditLocks/acquire").respond(
        403,
        json={
            "code": 403,
            "message": "Principal: CatalogPrincipal{name='carol'} operations [EditAll] not allowed",
        },
    )
    apply = respx.post(f"{API}/ontologyChangeSets/{CSID}/apply")
    with pytest.raises(OMForbidden) as info:
        await client.with_token(SecretStr(USER_TOKEN)).apply_change_set(CSID)
    exc = info.value
    assert isinstance(exc, OMError)
    assert exc.status == 403
    assert exc.detail is not None and "[EditAll]" in exc.detail
    assert exc.hint
    assert USER_TOKEN not in f"{exc} {exc.detail} {exc.hint} {exc!r}"
    assert "Bearer" not in str(exc)
    assert acquire.called and not apply.called


# --- permissions ----------------------------------------------------------------------------


@respx.mock
async def test_permissions_parsed(client: OMClient) -> None:
    route = respx.get(f"{API}/permissions/glossary/{GID}").respond(
        200, json=CAROL_GLOSSARY_PERMISSIONS
    )
    perms = await client.with_token(SecretStr(USER_TOKEN)).get_permissions("glossary", GID)
    assert perms.resource == "glossary"
    assert perms.operations["EditAll"] == "notAllow"
    assert perms.operations["ViewAll"] == "allow"
    assert perms.operations["EditOwners"] == "conditionalAllow"
    assert not perms.allows(*APPLY_PERMISSIONS["glossary"])
    assert perms.allows("ViewAll", "EditTags")
    assert not perms.allows("EditOwners")  # conditional is not a yes
    assert not perms.allows("NoSuchOperation")
    # the caller's own permissions: no ?user= parameter
    assert "user" not in route.calls[0].request.url.params


@respx.mock
async def test_permissions_on_native_ontology_use_its_entity_type(client: OMClient) -> None:
    route = respx.get(f"{API}/permissions/ontology/{GID}").respond(
        200,
        json={"resource": "ontology", "permissions": [{"operation": "EditAll", "access": "allow"}]},
    )
    perms = await client.get_permissions("ontology", GID)
    assert route.called
    assert perms.allows(*APPLY_PERMISSIONS["ontology"])


def test_apply_permissions_constants() -> None:
    assert APPLY_PERMISSIONS == {"glossary": ("EditAll",), "ontology": ("EditAll",)}


# --- asset tags -----------------------------------------------------------------------------


def _patch_ops(route: respx.Route) -> list[dict[str, Any]]:
    request = route.calls[-1].request
    assert request.headers["Content-Type"] == "application/json-patch+json"
    return json.loads(request.content)


@respx.mock
async def test_add_asset_tags_merges_and_is_idempotent(client: OMClient) -> None:
    get = respx.get(f"{API}/tables/name/svc.db.sales.orders").respond(200, json=TABLE)
    patch = respx.patch(f"{API}/tables/{TID}").respond(200, json=TABLE)

    added = await client.add_asset_tags(
        "table", "svc.db.sales.orders", column="email", term_fqn="retail.Customer"
    )
    assert added is True
    assert get.calls[-1].request.url.params["fields"] == "tags,columns"
    assert _patch_ops(patch) == [
        {
            "op": "add",
            "path": "/columns/1/tags",
            "value": [  # Derived dropped, own tags kept, the glossary tag appended
                TABLE["columns"][1]["tags"][1],
                glossary_tag("retail.Customer"),
            ],
        }
    ]

    added = await client.add_asset_tags(
        "table", "svc.db.sales.orders", column=None, term_fqn="retail.Order"
    )
    assert added is True
    assert _patch_ops(patch) == [
        {"op": "add", "path": "/tags", "value": [*TABLE["tags"], glossary_tag("retail.Order")]}
    ]

    calls = len(patch.calls)
    again = await client.add_asset_tags(
        "table", "svc.db.sales.orders", column="email", term_fqn="retail.EmailAddress"
    )
    assert again is False
    assert len(patch.calls) == calls  # already there: nothing written


@respx.mock
async def test_remove_asset_tags_keeps_other_tags_and_is_idempotent(client: OMClient) -> None:
    respx.get(f"{API}/tables/name/svc.db.sales.orders").respond(200, json=TABLE)
    patch = respx.patch(f"{API}/tables/{TID}").respond(200, json=TABLE)

    removed = await client.remove_asset_tags(
        "table", "svc.db.sales.orders", column="email", term_fqn="retail.EmailAddress"
    )
    assert removed is True
    assert _patch_ops(patch) == [{"op": "add", "path": "/columns/1/tags", "value": []}]

    calls = len(patch.calls)
    for column, fqn in (("email", "retail.Customer"), ("email", "PII.Sensitive"), (None, "x.Y")):
        # absent, or only Derived on the column: nothing of the column's own to remove
        assert not await client.remove_asset_tags(
            "table", "svc.db.sales.orders", column=column, term_fqn=fqn
        )
    assert len(patch.calls) == calls


@respx.mock
async def test_asset_tags_unknown_table_or_column_is_404(client: OMClient) -> None:
    respx.get(f"{API}/tables/name/svc.db.sales.nope").respond(
        404, json={"code": 404, "message": "table instance for svc.db.sales.nope not found"}
    )
    respx.get(f"{API}/tables/name/svc.db.sales.orders").respond(200, json=TABLE)
    with pytest.raises(OMError) as info:
        await client.add_asset_tags("table", "svc.db.sales.nope", column=None, term_fqn="r.A")
    assert info.value.status == 404
    with pytest.raises(OMError) as info:
        await client.add_asset_tags("table", "svc.db.sales.orders", column="nope", term_fqn="r.A")
    assert info.value.status == 404


def test_tag_helpers() -> None:
    derived = {"tagFQN": "PII.Sensitive", "labelType": "Derived"}
    own = {"tagFQN": "retail.Order", "source": "Glossary", "labelType": "Manual"}
    assert merge_tags([derived, own], "retail.Order") is None
    assert merge_tags([derived], "retail.Order") == [glossary_tag("retail.Order")]
    assert remove_tags([derived, own], "retail.Order") == []
    assert remove_tags([derived], "PII.Sensitive") is None
    assert glossary_tag("retail.Order") == {
        "tagFQN": "retail.Order",
        "source": "Glossary",
        "labelType": "Manual",
        "state": "Confirmed",
    }


def test_new_paths_registered() -> None:
    paths = OMClient.PATHS
    assert paths["apply_change_set"] == ("POST", "/v1/ontologyChangeSets/{id}/apply")
    assert paths["get_permissions"] == ("GET", "/v1/permissions/{resource}/{id}")
    assert paths["get_current_user"] == ("GET", "/v1/users/loggedInUser")
    assert paths["patch_table"] == ("PATCH", "/v1/tables/{id}")
