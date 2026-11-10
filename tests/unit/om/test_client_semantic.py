"""OMClient glossary, term, asset, export and lineage reads (FR-OM-01), mocked transport.

Response shapes were captured from the live 2.1.0-rc1 server.
"""

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakeloom.om.client import OMClient, OMError, OMUnavailable
from lakeloom.om.models import OMEntityRef

BASE = "http://om.test:8585"
API = f"{BASE}/api/v1"
GID = "263277db-bc8a-4f3f-a153-254bfdb4587f"
TID = "b3bf85db-2acf-4312-b739-02668c1d6016"
GLOSSARY = {
    "id": GID,
    "name": "retail",
    "fullyQualifiedName": "retail",
    "displayName": "Retail",
    "description": "Retail model",
    "namespaces": [{"prefix": "ex", "namespace": "https://example.org/retail#"}],
}
TERM = {
    "id": TID,
    "name": "Customer",
    "fullyQualifiedName": "retail.Customer",
    "displayName": "Customer",
    "description": "A buyer",
    "iri": "https://example.org/retail#Customer",
    "synonyms": ["Client"],
    "glossary": {"id": GID, "type": "glossary", "name": "retail", "fullyQualifiedName": "retail"},
    "parent": {"id": "p1", "type": "glossaryTerm", "fullyQualifiedName": "retail.Party"},
    "relatedTerms": [
        {
            "relationType": "broader",
            "status": "Approved",
            "term": {"id": "t2", "type": "glossaryTerm", "fullyQualifiedName": "retail.Party"},
        }
    ],
    "conceptMappings": [
        {
            "conceptIri": "http://xmlns.com/foaf/0.1/Person",
            "mappingType": "CLOSE_MATCH",
            "source": "foaf",
        }
    ],
}


@pytest.fixture
def client() -> OMClient:
    return OMClient(BASE, SecretStr("t"))


@respx.mock
async def test_fr_om_01_list_glossaries_params_and_page(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaries").respond(
        json={"data": [GLOSSARY], "paging": {"total": 7, "after": "abc"}}
    )

    page = await client.list_glossaries(after="x", limit=5)

    params = route.calls.last.request.url.params
    assert params["after"] == "x" and params["limit"] == "5"
    assert page.after == "abc" and page.total == 7
    assert page.data[0].name == "retail"
    assert page.data[0].fully_qualified_name == "retail"
    assert page.data[0].namespaces[0].prefix == "ex"


@respx.mock
async def test_fr_om_01_list_glossaries_without_after(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaries").respond(json={"data": [], "paging": {"total": 0}})

    page = await client.list_glossaries()

    assert "after" not in route.calls.last.request.url.params
    assert page.after is None and page.data == []


@respx.mock
async def test_fr_om_01_get_glossary_by_name(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaries/name/retail").respond(json=GLOSSARY)

    got = await client.get_glossary_by_name("retail")

    assert got is not None and got.id == GID
    assert route.calls.last.request.url.params["fields"] == "owners,tags,domains,termCount"


@respx.mock
async def test_fr_om_01_get_glossary_by_name_404_is_none(client: OMClient) -> None:
    respx.get(f"{API}/glossaries/name/nope").respond(404, json={"code": 404})

    assert await client.get_glossary_by_name("nope") is None


@respx.mock
async def test_fr_om_01_list_terms_by_glossary_id(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaryTerms").respond(json={"data": [TERM], "paging": {"total": 1}})

    page = await client.list_terms(GID, after="a", limit=10)

    params = route.calls.last.request.url.params
    assert params["glossary"] == GID
    assert params["limit"] == "10" and params["after"] == "a"
    assert "conceptMappings" in params["fields"]
    term = page.data[0]
    assert term.iri == "https://example.org/retail#Customer"
    assert term.synonyms == ["Client"]
    assert term.parent is not None and term.parent.fully_qualified_name == "retail.Party"
    assert term.related_terms[0].relation_type == "broader"
    assert term.related_terms[0].status == "Approved"
    assert term.related_terms[0].term.fully_qualified_name == "retail.Party"
    assert term.concept_mappings[0].mapping_type == "CLOSE_MATCH"
    assert term.concept_mappings[0].concept_iri == "http://xmlns.com/foaf/0.1/Person"
    assert term.glossary.name == "retail"


@respx.mock
async def test_fr_om_01_list_terms_resolves_glossary_name_to_id(client: OMClient) -> None:
    # OM filters terms by glossary *id*; a name must be resolved first.
    respx.get(f"{API}/glossaries/name/retail").respond(json=GLOSSARY)
    route = respx.get(f"{API}/glossaryTerms").respond(json={"data": [], "paging": {"total": 0}})

    await client.list_terms("retail")

    assert route.calls.last.request.url.params["glossary"] == GID


@respx.mock
async def test_fr_om_01_list_terms_unknown_glossary_raises(client: OMClient) -> None:
    respx.get(f"{API}/glossaries/name/nope").respond(404, json={"code": 404})

    with pytest.raises(OMError) as err:
        await client.list_terms("nope")
    assert err.value.status == 404


@respx.mock
async def test_fr_om_01_get_term_by_uuid(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaryTerms/{TID}").respond(json=TERM)

    got = await client.get_term(TID)

    assert got is not None and got.name == "Customer"
    assert "relatedTerms" in route.calls.last.request.url.params["fields"]


@respx.mock
async def test_fr_om_01_get_term_by_fqn_is_quoted(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaryTerms/name/retail.%22A%2FB%22").respond(json=TERM)

    got = await client.get_term('retail."A/B"')

    assert got is not None
    assert route.called


@respx.mock
async def test_fr_om_01_get_term_404_is_none(client: OMClient) -> None:
    respx.get(f"{API}/glossaryTerms/{TID}").respond(404, json={"code": 404})
    respx.get(f"{API}/glossaryTerms/name/retail.Nope").respond(404, json={"code": 404})

    assert await client.get_term(TID) is None
    assert await client.get_term("retail.Nope") is None


@respx.mock
async def test_fr_om_01_search_terms_params(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaryTerms/search").respond(
        json={"data": [TERM], "paging": {"total": 1}}
    )

    got = await client.search_terms("cust", glossary_fqn="retail", limit=3)

    params = route.calls.last.request.url.params
    assert params["q"] == "cust" and params["glossaryFqn"] == "retail"
    assert params["limit"] == "3"
    assert [t.name for t in got] == ["Customer"]


@respx.mock
async def test_fr_om_01_search_terms_without_glossary(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaryTerms/search").respond(json={"data": [], "paging": {}})

    assert await client.search_terms("x") == []
    assert "glossaryFqn" not in route.calls.last.request.url.params


@respx.mock
async def test_fr_om_01_get_term_assets(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaryTerms/{TID}/assets").respond(
        json={
            "data": [
                {
                    "id": "x1",
                    "type": "table",
                    "fullyQualifiedName": "svc.db.sch.customers",
                }
            ],
            "paging": {"total": 1},
        }
    )

    page = await client.get_term_assets(TID, offset=20, limit=5)

    params = route.calls.last.request.url.params
    assert params["offset"] == "20" and params["limit"] == "5"
    assert page.total == 1
    assert isinstance(page.data[0], OMEntityRef)
    assert page.data[0].type == "table"


@respx.mock
async def test_fr_om_01_get_term_assets_404_raises(client: OMClient) -> None:
    respx.get(f"{API}/glossaryTerms/{TID}/assets").respond(404, json={"code": 404})

    with pytest.raises(OMError):
        await client.get_term_assets(TID)


TURTLE = "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n\nskos:Concept a <x:C> .\n"


@respx.mock
async def test_fr_om_01_export_ontology_returns_text(client: OMClient) -> None:
    route = respx.get(f"{API}/glossaries/{GID}/exportOntology").respond(
        text=TURTLE, headers={"Content-Type": "text/turtle"}
    )

    got = await client.export_ontology(GID)

    assert got == TURTLE
    params = route.calls.last.request.url.params
    assert params["format"] == "turtle"
    assert params["includeRelations"] == "true"
    assert route.calls.last.request.headers["Accept"] == "text/turtle"


@respx.mock
@pytest.mark.parametrize(
    ("fmt", "mime"),
    [
        ("turtle", "text/turtle"),
        ("rdfxml", "application/rdf+xml"),
        ("ntriples", "application/n-triples"),
        ("jsonld", "application/ld+json"),
    ],
)
async def test_fr_om_01_export_ontology_formats(client: OMClient, fmt: str, mime: str) -> None:
    route = respx.get(f"{API}/glossaries/{GID}/exportOntology").respond(
        text="body", headers={"Content-Type": mime}
    )

    got = await client.export_ontology(GID, fmt, include_relations=False)  # type: ignore[arg-type]

    assert got == "body"
    params = route.calls.last.request.url.params
    assert params["format"] == fmt and params["includeRelations"] == "false"


@respx.mock
async def test_fr_om_01_export_ontology_error_raises(client: OMClient) -> None:
    respx.get(f"{API}/glossaries/{GID}/exportOntology").respond(404, json={"code": 404})
    with pytest.raises(OMError) as err:
        await client.export_ontology(GID)
    assert err.value.status == 404

    respx.get(f"{API}/glossaries/{GID}/exportOntology").respond(503)
    with pytest.raises(OMUnavailable):
        await client.export_ontology(GID)


@respx.mock
async def test_fr_om_01_get_lineage_flattens_nodes(client: OMClient) -> None:
    route = respx.get(f"{API}/lineage/getLineage").respond(
        json={
            "nodes": {
                "svc.db.s.a": {
                    "entity": {"id": "1", "type": "table", "fullyQualifiedName": "svc.db.s.a"},
                    "nodeDepth": 0,
                },
                "svc.db.s.b": {
                    "entity": {"id": "2", "type": "table", "fullyQualifiedName": "svc.db.s.b"}
                },
            },
            "upstreamEdges": {"e1": {"fromEntity": {"id": "2"}, "toEntity": {"id": "1"}}},
            "downstreamEdges": {},
        }
    )

    got = await client.get_lineage("table", "svc.db.s.a", upstream_depth=2, downstream_depth=3)

    params = route.calls.last.request.url.params
    assert params["fqn"] == "svc.db.s.a" and params["type"] == "table"
    assert params["upstreamDepth"] == "2" and params["downstreamDepth"] == "3"
    assert sorted(n.fully_qualified_name or "" for n in got.nodes) == ["svc.db.s.a", "svc.db.s.b"]
    assert len(got.upstream_edges) == 1 and got.downstream_edges == []


@respx.mock
async def test_fr_om_01_get_lineage_accepts_list_shapes(client: OMClient) -> None:
    respx.get(f"{API}/lineage/getLineage").respond(
        json={
            "nodes": [{"id": "1", "type": "table"}],
            "upstreamEdges": [{"fromEntity": "2", "toEntity": "1"}],
            "downstreamEdges": [],
        }
    )

    got = await client.get_lineage("table", "svc.db.s.a")

    assert got.nodes[0].id == "1" and len(got.upstream_edges) == 1


@respx.mock
async def test_fr_om_01_get_table_requests_extended_fields(client: OMClient) -> None:
    route = respx.get(f"{API}/tables/name/svc.db.s.t").respond(
        json={"id": "1", "fullyQualifiedName": "svc.db.s.t"}
    )

    await client.get_table_by_fqn("svc.db.s.t")

    fields = route.calls.last.request.url.params["fields"].split(",")
    for f in ("columns", "tags", "owners", "domains", "dataProducts", "tableConstraints"):
        assert f in fields


@respx.mock
async def test_fr_om_01_semantic_reads_map_unavailable(client: OMClient) -> None:
    respx.get(f"{API}/glossaries").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(OMUnavailable):
        await client.list_glossaries()
