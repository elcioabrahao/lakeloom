"""OMClient native-ontology endpoints, conflict kinds and lease shielding (FR-OM-02, ADR 0011).

Bodies follow the live 2.1.0-rc1 server (ADR 0011, "Reading a native ontology").
"""

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, XSD

from lakeloom.om.client import OMClient, OMConflict
from lakeloom.om.native_rdf import QUDT_UNIT
from lakeloom.rdf.canonical import parse_rdf

BASE = "http://om.test:8585"
API = f"{BASE}/api/v1"
OID = "f390dbf9-9645-4bda-8f33-45c8cb4d0180"
CSID = "5e2b520e-a1d1-4132-978a-f23bf139a91f"
IRI = "http://localhost:8000/ontology/pair/"

ONTOLOGY = {
    "id": OID,
    "name": "pair",
    "fullyQualifiedName": "pair",
    "description": "paired",
    "version": 0.1,
    "iri": IRI,
    "namespace": IRI,
}
CONCEPT = {
    "id": "e2ed746b-1d9d-4e99-9cba-24c583a184c6",
    "name": "Widget",
    "fullyQualifiedName": "pair.Widget",
    "description": "A widget",
    "iri": f"{IRI}Widget",
    "conceptType": "OWL_CLASS",
}
PROPERTY = {
    "id": "44585526-76d4-4999-ad2b-bc493b732794",
    "name": "Widget.weight",
    "fullyQualifiedName": 'pair."Widget.weight"',
    "description": "weight",
    "iri": f"{IRI}Widget.weight",
    "kind": "DATA",
    "domain": [f"{IRI}Widget"],
    "range": ["http://www.w3.org/2001/XMLSchema#decimal"],
    "enumValues": ["light", "heavy"],
    "isIdentifier": True,
    "displayName": "weight",
    "unit": "kg",
}
AXIOM = {
    "id": "0c0a5f6e-86a2-4d20-8d8b-5ef1b0c6a111",
    "name": "widgetHasOneWeight",
    "fullyQualifiedName": "pair.widgetHasOneWeight",
    "axiomType": "SUBCLASS_OF",
    "subjectIri": f"{IRI}Widget",
    "expressions": [
        {
            "kind": "RESTRICTION",
            "restrictionKind": "EXACT",
            "propertyIri": f"{IRI}Widget.weight",
            "cardinality": 1,
        }
    ],
}


def state(rev: int) -> dict[str, Any]:
    return {
        "ontologyId": OID,
        "workingRevision": rev,
        "lastReleaseRevision": 0,
        "activationRevision": 0,
    }


@pytest.fixture
def client() -> OMClient:
    return OMClient(BASE, SecretStr("t"))


def test_ontology_paths_registered() -> None:
    for name in (
        "create_ontology",
        "get_ontology_by_name",
        "get_ontology_state",
        "list_ontology_concepts",
        "list_ontology_properties",
        "list_ontology_axioms",
    ):
        assert name in OMClient.PATHS


@respx.mock
async def test_create_ontology_sends_iri_as_namespace(client: OMClient) -> None:
    route = respx.post(f"{API}/ontologies").respond(201, json=ONTOLOGY)
    onto = await client.create_ontology(name="pair", iri=IRI, description="paired")
    assert onto.id == OID and onto.name == "pair"
    assert json.loads(route.calls[0].request.content) == {
        "name": "pair",
        "iri": IRI,
        "namespace": IRI,
        "description": "paired",
    }


@respx.mock
async def test_create_ontology_duplicate_is_conflict(client: OMClient) -> None:
    respx.post(f"{API}/ontologies").respond(
        409, json={"code": 409, "message": "Entity already exists"}
    )
    with pytest.raises(OMConflict) as info:
        await client.create_ontology(name="pair", iri=IRI, description="x")
    assert info.value.kind == "exists"


@respx.mock
async def test_get_ontology_by_name_found_and_missing(client: OMClient) -> None:
    respx.get(f"{API}/ontologies/name/pair").respond(200, json=ONTOLOGY)
    respx.get(f"{API}/ontologies/name/nope").respond(404, json={"code": 404, "message": "x"})
    found = await client.get_ontology_by_name("pair")
    assert found is not None and found.id == OID
    assert await client.get_ontology_by_name("nope") is None


@respx.mock
async def test_get_ontology_state(client: OMClient) -> None:
    respx.get(f"{API}/ontologies/{OID}/state").respond(200, json=state(3))
    assert (await client.get_ontology_state(OID)).working_revision == 3


def mock_members(
    concepts: list[dict[str, Any]], props: list[dict[str, Any]], axioms: list[dict[str, Any]]
) -> list[respx.Route]:
    def page(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"data": items, "paging": {"total": len(items)}}

    return [
        respx.get(f"{API}/ontologyConcepts").respond(200, json=page(concepts)),
        respx.get(f"{API}/ontologyProperties").respond(200, json=page(props)),
        respx.get(f"{API}/ontologyAxioms").respond(200, json=page(axioms)),
    ]


@respx.mock
async def test_export_native_ontology_builds_rdf_from_working_members(client: OMClient) -> None:
    respx.get(f"{API}/ontologies/name/pair").respond(200, json=ONTOLOGY)
    respx.get(f"{API}/ontologies/{OID}/state").respond(200, json=state(3))
    routes = mock_members([CONCEPT], [PROPERTY], [AXIOM])
    export = await client.export_native_ontology("pair")
    assert export is not None and export.working_revision == 3
    assert routes[0].calls[0].request.url.params["ontology"] == "pair"
    g: Graph = parse_rdf(export.turtle, "turtle")
    widget, weight = URIRef(f"{IRI}Widget"), URIRef(f"{IRI}Widget.weight")
    assert (widget, RDF.type, OWL.Class) in g
    assert (widget, RDFS.label, Literal("Widget")) in g
    assert (weight, RDF.type, OWL.DatatypeProperty) in g
    assert (weight, RDFS.domain, widget) in g
    assert (weight, RDFS.range, XSD.decimal) in g
    assert (weight, RDFS.label, Literal("weight")) in g  # displayName wins
    assert (weight, QUDT_UNIT, Literal("kg")) in g
    (head,) = g.objects(weight, OWL.oneOf)
    assert [str(v) for v in Collection(g, head)] == ["light", "heavy"]
    assert (weight, RDFS.comment, Literal("weight")) in g
    assert (weight, RDF.type, OWL.FunctionalProperty) in g  # identifier => functional
    # The cardinality axiom becomes a subclass restriction on the subject.
    restrictions = [o for o in g.objects(widget, RDFS.subClassOf)]
    assert len(restrictions) == 1
    assert (restrictions[0], OWL.onProperty, weight) in g


@respx.mock
async def test_export_native_ontology_unpaired_is_none(client: OMClient) -> None:
    respx.get(f"{API}/ontologies/name/pair").respond(404, json={"code": 404, "message": "x"})
    assert await client.export_native_ontology("pair") is None


@respx.mock
async def test_export_native_ontology_retries_when_revision_moves(client: OMClient) -> None:
    respx.get(f"{API}/ontologies/name/pair").respond(200, json=ONTOLOGY)
    states = iter([state(1), state(2), state(2), state(2)])
    respx.get(f"{API}/ontologies/{OID}/state").mock(
        side_effect=lambda _r: httpx.Response(200, json=next(states))
    )
    mock_members([CONCEPT], [], [])
    export = await client.export_native_ontology("pair")
    assert export is not None and export.working_revision == 2


@respx.mock
async def test_export_native_ontology_pages_members(client: OMClient) -> None:
    respx.get(f"{API}/ontologies/name/pair").respond(200, json=ONTOLOGY)
    respx.get(f"{API}/ontologies/{OID}/state").respond(200, json=state(2))
    second = {**CONCEPT, "name": "Gadget", "iri": f"{IRI}Gadget"}

    def concepts(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after") == "c1":
            return httpx.Response(200, json={"data": [second], "paging": {"total": 2}})
        return httpx.Response(200, json={"data": [CONCEPT], "paging": {"after": "c1", "total": 2}})

    respx.get(f"{API}/ontologyConcepts").mock(side_effect=concepts)
    respx.get(f"{API}/ontologyProperties").respond(200, json={"data": [], "paging": {"total": 0}})
    respx.get(f"{API}/ontologyAxioms").respond(200, json={"data": [], "paging": {"total": 0}})
    export = await client.export_native_ontology("pair")
    assert export is not None
    g = parse_rdf(export.turtle, "turtle")
    assert {str(s) for s in g.subjects(RDF.type, OWL.Class)} == {f"{IRI}Widget", f"{IRI}Gadget"}


# --- conflict kinds ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "kind"),
    [
        (400, "Ontology operation 'x' expected version 0.05 but found 0.1", "stale_base"),
        (412, "Ontology changed since this draft was created", "stale_revision"),
        (
            409,
            "Ontology resource 'ontologyChangeSet:x' is being edited by another session",
            "lease",
        ),
        (409, "The ontology edit lease is missing, expired, or no longer owned", "lease"),
        (409, "Entity already exists", "exists"),
    ],
)
@respx.mock
async def test_conflict_kind(client: OMClient, status: int, message: str, kind: str) -> None:
    respx.get(f"{API}/ontologyChangeSets/{CSID}").respond(
        status, json={"code": status, "message": message}
    )
    with pytest.raises(OMConflict) as info:
        await client.get_change_set(CSID)
    assert info.value.kind == kind


# --- cancellation ------------------------------------------------------------------------


@respx.mock
async def test_cancelled_command_still_releases_the_lease(client: OMClient) -> None:
    lock = {
        "resourceType": "ontologyChangeSet",
        "resourceId": CSID,
        "sessionId": "s",
        "version": 1,
    }
    respx.post(f"{API}/ontologyEditLocks/acquire").respond(200, json=lock)
    release = respx.delete(f"{API}/ontologyEditLocks/ontologyChangeSet/{CSID}").respond(204)
    started = asyncio.Event()

    async def slow(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200)

    respx.post(f"{API}/ontologyChangeSets/{CSID}/submit").mock(side_effect=slow)
    task = asyncio.create_task(client.submit_change_set(CSID))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert release.called
