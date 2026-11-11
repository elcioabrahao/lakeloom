"""OMClient change sets, glossary creation and axiom validation (FR-OM-01, FR-OM-02).

Response bodies were captured from the live 2.1.0-rc1 server (ids replaced, ADR 0011).
"""

import json
import re
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakeloom.om.client import OMClient, OMConflict, OMError
from lakeloom.om.models import OMChangeSet

BASE = "http://om.test:8585"
API = f"{BASE}/api/v1"
GID = "e94efb2d-5632-4379-a478-948b04170574"
CSID = "5e2b520e-a1d1-4132-978a-f23bf139a91f"
OPID = "efaa67c0-0ad9-4aaf-bf45-19e38c38aef5"
TERMID = "9902b5ba-a2f4-41f0-a9c2-f3941f73f7db"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

GLOSSARY = {
    "id": GID,
    "name": "llresearch1",
    "fullyQualifiedName": "llresearch1",
    "displayName": "LL research 1",
    "description": "throwaway",
    "version": 0.1,
    "updatedAt": 1794363198360,
    "updatedBy": "ingestion-bot",
    "deleted": False,
    "provider": "user",
    "mutuallyExclusive": False,
    "entityStatus": "Unprocessed",
    "namespaces": [],
}
GLOSSARY_REF = {
    "id": GID,
    "type": "glossary",
    "name": "llresearch1",
    "fullyQualifiedName": "llresearch1",
    "description": "throwaway",
    "displayName": "LL research 1",
    "deleted": False,
}
CREATE_TERM_OP = {
    "id": OPID,
    "operationType": "CREATE_TERM",
    "term": {
        "id": TERMID,
        "name": "Widget",
        "description": "a widget",
        "synonyms": [],
        "glossary": {"id": GID, "type": "glossary"},
        "relatedTerms": [],
        "references": [],
        "conceptMappings": [],
        "version": 0.1,
        "tags": [],
        "deleted": False,
        "provider": "user",
        "mutuallyExclusive": False,
    },
    "sourceMemoryIds": [],
    "state": "ACTIVE",
}
UPDATE_TERM_OP = {
    "id": "b17d9641-44fc-487f-a8e8-33cf6c8322b9",
    "operationType": "UPDATE_TERM",
    "targetId": "a939040b-6a91-42ad-8774-5c20191dda49",
    "baseVersion": 0.1,
    "term": {
        "id": "a939040b-6a91-42ad-8774-5c20191dda49",
        "name": "Base",
        "description": "Base term updated",
        "glossary": {"id": GID, "type": "glossary"},
    },
    "sourceMemoryIds": [],
    "state": "ACTIVE",
}


def change_set(
    state: str = "DRAFT", operations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    ops = [CREATE_TERM_OP] if operations is None else operations
    return {
        "id": CSID,
        "name": "ll-cs-2",
        "displayName": "ll cs 2",
        "fullyQualifiedName": "ll-cs-2",
        "description": "research",
        "glossaries": [GLOSSARY_REF],
        "state": state,
        "operations": ops,
        "undoCursor": len(ops),
        "version": 0.1,
        "updatedAt": 1794363226007,
        "updatedBy": "ingestion-bot",
        "deleted": False,
        "provider": "user",
    }


def lock(version: int = 2) -> dict[str, Any]:
    return {
        "resourceType": "ontologyChangeSet",
        "resourceId": CSID,
        "holder": {"id": "1d3127ed-b578-4f3f-8cb4-a3609f0ba728", "type": "user"},
        "sessionId": "set-by-test",
        "version": version,
        "acquiredAt": 1794363365997,
        "renewedAt": 1794363365997,
        "expiresAt": 1794363425997,
    }


def body(route: respx.Route, call: int = -1) -> Any:
    return json.loads(route.calls[call].request.content)


@pytest.fixture
def client() -> OMClient:
    return OMClient(BASE, SecretStr("t"))


def mock_lease() -> tuple[respx.Route, respx.Route]:
    def acquire(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        return httpx.Response(200, json={**lock(), "sessionId": sent["sessionId"]})

    acquire_route = respx.post(f"{API}/ontologyEditLocks/acquire").mock(side_effect=acquire)
    release_route = respx.delete(f"{API}/ontologyEditLocks/ontologyChangeSet/{CSID}").respond(204)
    return acquire_route, release_route


def test_fr_om_02_change_set_paths_registered() -> None:
    for name in (
        "create_change_set",
        "get_change_set",
        "replace_operations",
        "submit_change_set",
        "discard_change_set",
        "acquire_edit_lease",
        "release_edit_lease",
        "validate_axiom",
        "create_glossary",
        "get_glossary_by_id",
    ):
        assert name in OMClient.PATHS


@respx.mock
async def test_fr_om_02_create_change_set_resolves_glossary_and_fills_op_defaults(
    client: OMClient,
) -> None:
    respx.get(f"{API}/glossaries/{GID}").respond(200, json=GLOSSARY)
    route = respx.post(f"{API}/ontologyChangeSets").respond(201, json=change_set())

    op = {
        "operationType": "CREATE_TERM",
        "term": {"id": TERMID, "name": "Widget", "description": "a widget"},
    }
    cs = await client.create_change_set(
        name="ll-cs-2", glossary_id=GID, description="research", operations=[op]
    )

    sent = body(route)
    assert sent["name"] == "ll-cs-2"
    assert sent["displayName"] == "ll-cs-2"
    assert sent["description"] == "research"
    assert sent["glossaries"] == ["llresearch1"]  # OM takes names, not ids
    assert "ontology" not in sent
    (sent_op,) = sent["operations"]
    assert UUID.match(sent_op["id"])
    assert sent_op["state"] == "ACTIVE"
    assert sent_op["term"]["name"] == "Widget"
    assert op.keys() == {"operationType", "term"}  # caller's mapping is not mutated

    assert isinstance(cs, OMChangeSet)
    assert cs.id == CSID
    assert cs.state == "DRAFT"
    assert cs.glossaries[0].name == "llresearch1"
    (got,) = cs.operations
    assert got.operation_type == "CREATE_TERM"
    assert got.term is not None and got.term["id"] == TERMID


@respx.mock
async def test_fr_om_02_create_change_set_by_glossary_name_skips_lookup(client: OMClient) -> None:
    route = respx.post(f"{API}/ontologyChangeSets").respond(201, json=change_set(operations=[]))
    await client.create_change_set(
        name="ll-cs-2",
        glossary_id="llresearch1",
        description="research",
        operations=[],
        display_name="LL cs",
    )
    sent = body(route)
    assert sent["glossaries"] == ["llresearch1"]
    assert sent["displayName"] == "LL cs"
    assert sent["operations"] == []


@respx.mock
async def test_fr_om_02_create_native_ontology_change_set(client: OMClient) -> None:
    native = {
        **change_set(operations=[]),
        "glossaries": [],
        "ontology": {"id": "fce2a934-0be5-4523-b401-f8174ca44de2", "type": "ontology"},
        "baseWorkingRevision": 4,
    }
    route = respx.post(f"{API}/ontologyChangeSets").respond(201, json=native)
    cs = await client.create_change_set(
        name="ll-cs-2",
        glossary_id=None,
        description="research",
        operations=[],
        ontology="llresearch1",
        base_working_revision=4,
    )
    sent = body(route)
    assert sent["ontology"] == "llresearch1"
    assert sent["baseWorkingRevision"] == 4
    assert "glossaries" not in sent
    assert cs.base_working_revision == 4
    assert cs.ontology is not None and cs.ontology.type == "ontology"


async def test_fr_om_02_create_change_set_needs_exactly_one_target(client: OMClient) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await client.create_change_set(name="x", glossary_id=None, description="d", operations=[])
    with pytest.raises(ValueError, match="exactly one"):
        await client.create_change_set(
            name="x",
            glossary_id="g",
            description="d",
            operations=[],
            ontology="o",
            base_working_revision=1,
        )
    with pytest.raises(ValueError, match="base_working_revision"):
        await client.create_change_set(
            name="x", glossary_id=None, description="d", operations=[], ontology="o"
        )


@respx.mock
async def test_fr_om_02_add_operations_appends_under_a_lease(client: OMClient) -> None:
    acquire, release = mock_lease()
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(200, json=change_set())
    put = respx.put(f"{API}/ontologyChangeSets/{CSID}/operations").respond(
        200, json=change_set(operations=[CREATE_TERM_OP, UPDATE_TERM_OP])
    )

    new = {k: v for k, v in UPDATE_TERM_OP.items() if k not in ("id", "state", "sourceMemoryIds")}
    cs = await client.add_operations(CSID, [new])

    lease_req = body(acquire)
    assert lease_req["resourceType"] == "ontologyChangeSet"
    assert lease_req["resourceId"] == CSID
    session = lease_req["sessionId"]
    assert session.startswith("lakeloom-")

    sent = body(put)
    assert sent["lease"] == {"sessionId": session, "version": 2}
    assert [o["operationType"] for o in sent["operations"]] == ["CREATE_TERM", "UPDATE_TERM"]
    assert sent["operations"][0]["id"] == OPID  # existing operation kept verbatim
    assert UUID.match(sent["operations"][1]["id"])
    assert sent["operations"][1]["baseVersion"] == 0.1
    assert sent["undoCursor"] == 2

    assert release.called
    assert release.calls[0].request.url.params["sessionId"] == session
    assert [o.operation_type for o in cs.operations] == ["CREATE_TERM", "UPDATE_TERM"]
    assert cs.operations[1].base_version == 0.1


@respx.mock
async def test_fr_om_02_add_operations_drops_undone_tail(client: OMClient) -> None:
    mock_lease()
    undone = {**UPDATE_TERM_OP, "state": "UNDONE"}
    current = {**change_set(operations=[CREATE_TERM_OP, undone]), "undoCursor": 1}
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(200, json=current)
    put = respx.put(f"{API}/ontologyChangeSets/{CSID}/operations").respond(200, json=change_set())
    await client.add_operations(CSID, [{"operationType": "DELETE_TERM", "targetId": TERMID}])
    sent = body(put)
    assert [o["operationType"] for o in sent["operations"]] == ["CREATE_TERM", "DELETE_TERM"]
    assert sent["undoCursor"] == 2


@respx.mock
async def test_fr_om_02_add_operations_missing_change_set(client: OMClient) -> None:
    _, release = mock_lease()
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(
        404, json={"code": 404, "message": "ontologyChangeSet instance for x not found"}
    )
    with pytest.raises(OMError) as info:
        await client.add_operations(CSID, [])
    assert info.value.status == 404
    assert release.called


@respx.mock
async def test_fr_om_02_get_change_set(client: OMClient) -> None:
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(200, json=change_set("SUBMITTED"))
    cs = await client.get_change_set(CSID)
    assert cs is not None and cs.state == "SUBMITTED"
    assert cs.undo_cursor == 1


@respx.mock
async def test_fr_om_02_get_change_set_missing_is_none(client: OMClient) -> None:
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(
        404, json={"code": 404, "message": "not found"}
    )
    assert await client.get_change_set(CSID) is None


@respx.mock
async def test_fr_om_02_submit_change_set_under_a_lease(client: OMClient) -> None:
    acquire, release = mock_lease()
    route = respx.post(f"{API}/ontologyChangeSets/{CSID}/submit").respond(
        200, json=change_set("SUBMITTED")
    )
    cs = await client.submit_change_set(CSID)
    session = body(acquire)["sessionId"]
    assert body(route) == {"lease": {"sessionId": session, "version": 2}}
    assert cs.state == "SUBMITTED"
    assert release.called


@respx.mock
async def test_fr_om_02_discard_change_set_under_a_lease(client: OMClient) -> None:
    acquire, release = mock_lease()
    route = respx.post(f"{API}/ontologyChangeSets/{CSID}/discard").respond(
        200, json=change_set("DISCARDED")
    )
    cs = await client.discard_change_set(CSID)
    assert body(route)["lease"]["sessionId"] == body(acquire)["sessionId"]
    assert cs.state == "DISCARDED"
    assert release.called


@respx.mock
async def test_fr_om_02_lease_released_when_command_fails(client: OMClient) -> None:
    _, release = mock_lease()
    respx.post(f"{API}/ontologyChangeSets/{CSID}/submit").respond(
        400,
        json={
            "code": 400,
            "message": "Ontology change set transition from DISCARDED to SUBMITTED is not allowed",
        },
    )
    with pytest.raises(OMError) as info:
        await client.submit_change_set(CSID)
    assert not isinstance(info.value, OMConflict)
    assert info.value.status == 400
    assert info.value.detail is not None and "not allowed" in info.value.detail
    assert release.called


@respx.mock
async def test_fr_om_02_lease_release_failure_does_not_mask_result(client: OMClient) -> None:
    respx.post(f"{API}/ontologyEditLocks/acquire").respond(200, json=lock())
    respx.delete(f"{API}/ontologyEditLocks/ontologyChangeSet/{CSID}").respond(500)
    respx.post(f"{API}/ontologyChangeSets/{CSID}/submit").respond(200, json=change_set("SUBMITTED"))
    cs = await client.submit_change_set(CSID)
    assert cs.state == "SUBMITTED"


@pytest.mark.parametrize(
    ("status", "message", "entity_ids"),
    [
        (  # glossary op with a stale baseVersion, at apply time
            400,
            "Ontology operation '66ec8ec9-7138-4077-adc2-5a042db2885a' expected version 0.05 "
            "but found 0.1",
            ("66ec8ec9-7138-4077-adc2-5a042db2885a",),
        ),
        (412, "Ontology changed since this draft was created", ()),  # native draft, stale rev
        (
            409,
            f"Ontology resource 'ontologyChangeSet:{CSID}' is being edited by another session",
            (CSID,),
        ),
        (409, "Entity already exists", ()),
    ],
)
@respx.mock
async def test_conflict_maps_to_om_conflict(
    client: OMClient, status: int, message: str, entity_ids: tuple[str, ...]
) -> None:
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(
        status, json={"code": status, "message": message}
    )
    with pytest.raises(OMConflict) as info:
        await client.get_change_set(CSID)
    exc = info.value
    assert isinstance(exc, OMError)
    assert exc.status == status
    assert exc.detail == message
    assert exc.entity_ids == entity_ids
    assert "Bearer" not in str(exc)


@respx.mock
async def test_fr_om_02_lease_held_elsewhere_is_conflict(client: OMClient) -> None:
    respx.post(f"{API}/ontologyEditLocks/acquire").respond(
        409,
        json={
            "code": 409,
            "message": f"Ontology resource 'ontologyChangeSet:{CSID}' is being edited by "
            "another session",
        },
    )
    submit = respx.post(f"{API}/ontologyChangeSets/{CSID}/submit")
    with pytest.raises(OMConflict) as info:
        await client.submit_change_set(CSID)
    assert info.value.entity_ids == (CSID,)
    assert not submit.called


@respx.mock
async def test_fr_om_02_validate_axiom(client: OMClient) -> None:
    route = respx.post(f"{API}/ontologyAxioms/validate").respond(
        200,
        json={
            "valid": False,
            "violations": [
                {
                    "severity": "ERROR",
                    "code": "UNDECLARED_CLASS",
                    "path": "/subjectIri",
                    "message": "Declare or import the referenced class: http://x/Widget",
                },
                {
                    "severity": "WARNING",
                    "code": "ILLEGAL_PUNNING",
                    "path": "/subjectIri",
                    "message": "punning",
                },
            ],
        },
    )
    axiom = {
        "name": "widgetHasOneWeight",
        "displayName": "Widget has exactly one weight",
        "description": "card",
        "axiomType": "SUBCLASS_OF",
        "subjectIri": "http://x/Widget",
        "provenance": "Manual",
        "ontology": "llresearch1",
        "expressions": [
            {
                "kind": "RESTRICTION",
                "restrictionKind": "EXACT",
                "propertyIri": "http://x/widgetWeight",
                "cardinality": 1,
            }
        ],
    }
    result = await client.validate_axiom(axiom)
    assert body(route) == axiom
    assert result.valid is False
    assert [v.code for v in result.violations] == ["UNDECLARED_CLASS", "ILLEGAL_PUNNING"]
    assert result.errors == ["Declare or import the referenced class: http://x/Widget"]


@respx.mock
async def test_fr_om_02_validate_axiom_valid(client: OMClient) -> None:
    respx.post(f"{API}/ontologyAxioms/validate").respond(
        200, json={"valid": True, "violations": []}
    )
    result = await client.validate_axiom({"name": "a"})
    assert result.valid and result.errors == []


@respx.mock
async def test_fr_om_01_create_glossary(client: OMClient) -> None:
    route = respx.post(f"{API}/glossaries").respond(201, json=GLOSSARY)
    glossary = await client.create_glossary(
        name="llresearch1", display_name="LL research 1", description="throwaway"
    )
    assert body(route) == {
        "name": "llresearch1",
        "displayName": "LL research 1",
        "description": "throwaway",
    }
    assert glossary.id == GID and glossary.fully_qualified_name == "llresearch1"


@respx.mock
async def test_fr_om_01_create_glossary_without_display_name(client: OMClient) -> None:
    route = respx.post(f"{API}/glossaries").respond(201, json=GLOSSARY)
    await client.create_glossary(name="llresearch1", display_name=None, description="d")
    assert "displayName" not in body(route)


@respx.mock
async def test_fr_om_01_create_glossary_duplicate_is_conflict(client: OMClient) -> None:
    respx.post(f"{API}/glossaries").respond(
        409, json={"code": 409, "message": "Entity already exists"}
    )
    with pytest.raises(OMConflict):
        await client.create_glossary(name="retail", display_name=None, description="d")


@respx.mock
async def test_fr_om_02_writes_send_json_and_bearer(client: OMClient) -> None:
    route = respx.post(f"{API}/glossaries").respond(201, json=GLOSSARY)
    await client.create_glossary(name="g", display_name=None, description="d")
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer t"
    assert request.headers["Content-Type"] == "application/json"


@respx.mock
async def test_fr_om_02_remove_operations_drops_ids_under_a_lease(client: OMClient) -> None:
    _, release = mock_lease()
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(
        200, json=change_set(operations=[CREATE_TERM_OP, UPDATE_TERM_OP])
    )
    put = respx.put(f"{API}/ontologyChangeSets/{CSID}/operations").respond(200, json=change_set())
    await client.remove_operations(CSID, [str(UPDATE_TERM_OP["id"]), "unknown"])
    sent = body(put)
    assert [o["id"] for o in sent["operations"]] == [OPID]
    assert sent["undoCursor"] == 1
    assert "lease" in sent and release.called


def test_relationship_types_path_registered() -> None:
    assert OMClient.PATHS["list_relationship_types"] == ("GET", "/v1/relationshipTypes")


@respx.mock
async def test_list_relationship_types_pages(client: OMClient) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after") == "c1":
            return httpx.Response(200, json={"data": [{"id": "r2", "name": "billsFor"}]})
        return httpx.Response(
            200, json={"data": [{"id": "r1", "name": "placedBy"}], "paging": {"after": "c1"}}
        )

    respx.get(f"{API}/relationshipTypes").mock(side_effect=page)
    assert await client.list_relationship_types() == {"placedBy": "r1", "billsFor": "r2"}
