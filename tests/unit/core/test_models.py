"""Model service: list/get models, cached graphs, deterministic export (FR-IO-01, FR-GIT-03)."""

import asyncio
import base64

import pytest
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL, SKOS

from lakeloom.config import Settings
from lakeloom.core.models import ModelService
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMError, OMUnavailable
from lakeloom.om.models import OMGlossary, OMNativeExport, OMPage
from lakeloom.rdf.canonical import ExportFormat, parse_rdf

TURTLE = """\
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://x/ontology/retail/Order> a skos:Concept ;
    skos:prefLabel "Pedido"@pt, "Order"@en ;
    skos:definition "Compra de clientes: café, açaí" ;
    rdfs:subClassOf [ a owl:Restriction ; owl:onProperty <http://x/p> ;
                      owl:someValuesFrom <http://x/ontology/retail/Customer> ] .
<http://x/ontology/retail/Customer> a skos:Concept ; skos:prefLabel "Customer"@en .
"""


def glossary(name: str = "retail", term_count: int | None = 2) -> OMGlossary:
    return OMGlossary.model_validate(
        {
            "id": f"id-{name}",
            "name": name,
            "fullyQualifiedName": name,
            "displayName": name.title(),
            "description": "Retail concepts",
            "termCount": term_count,
        }
    )


class FakeOM:
    def __init__(self) -> None:
        self.glossaries = [glossary("retail"), glossary("hr", None)]
        self.exports = 0
        self.down = False
        self.delay = 0.0
        self.list_calls: list[str | None] = []
        self.native: OMNativeExport | None = None
        self.native_calls: list[str] = []

    async def list_glossaries(self, *, after: str | None = None, limit: int = 50):
        self.list_calls.append(after)
        if self.down:
            raise OMUnavailable("down")
        start = int(after) if after else 0
        chunk = self.glossaries[start : start + limit]
        nxt = str(start + limit) if start + limit < len(self.glossaries) else None
        return OMPage[OMGlossary](data=chunk, after=nxt)

    async def get_glossary_by_name(self, name: str) -> OMGlossary | None:
        if self.down:
            raise OMUnavailable("down")
        return next((g for g in self.glossaries if g.name == name), None)

    async def export_native_ontology(self, name: str):
        self.native_calls.append(name)
        if self.down:
            raise OMUnavailable("down")
        return self.native

    async def export_ontology(self, glossary_id: str, fmt: str, *, include_relations: bool = True):
        self.exports += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.down:
            raise OMUnavailable("down")
        assert fmt == "turtle"
        return TURTLE


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(ttl: int = 60, public: str | None = "http://x") -> tuple[ModelService, FakeOM, Clock]:
    om = FakeOM()
    clock = Clock()
    settings = Settings(model_cache_ttl_seconds=ttl, public_url=public)  # pyright: ignore[reportArgumentType]
    return ModelService(om, settings, clock=clock), om, clock  # pyright: ignore[reportArgumentType]


FORMATS: list[ExportFormat] = ["turtle", "rdfxml", "ntriples", "jsonld", "skos"]


@pytest.mark.parametrize("fmt", FORMATS)
async def test_fr_io_01_export_all_five_formats_parse(fmt: ExportFormat) -> None:
    svc, _, _ = make()
    data = await svc.export("retail", fmt)
    parse_fmt = {"skos": "turtle", "rdfxml": "xml", "ntriples": "nt", "jsonld": "json-ld"}.get(
        fmt, fmt
    )
    g = Graph().parse(data=data, format=parse_fmt)
    assert len(g) > 0


async def test_fr_io_01_export_identical_across_two_runs() -> None:
    svc, _, _ = make()
    for fmt in FORMATS:
        first = await svc.export("retail", fmt)
        svc.clear_cache()
        second = await svc.export("retail", fmt)
        assert first == second


async def test_export_uses_model_prefix() -> None:
    svc, _, _ = make()
    assert b"@prefix retail: <http://x/ontology/retail/>" in await svc.export("retail", "turtle")


async def test_graph_cached_within_ttl() -> None:
    svc, om, clock = make(ttl=60)
    await svc.graph("retail")
    await svc.graph("retail")
    assert om.exports == 1
    clock.now += 61
    await svc.graph("retail")
    assert om.exports == 2


async def test_ttl_zero_disables_cache() -> None:
    svc, om, _ = make(ttl=0)
    await svc.graph("retail")
    await svc.graph("retail")
    assert om.exports == 2


async def test_concurrent_loads_share_one_fetch() -> None:
    svc, om, _ = make()
    om.delay = 0.05
    graphs = await asyncio.gather(*(svc.graph("retail") for _ in range(5)))
    assert om.exports == 1
    assert all(g is graphs[0] for g in graphs)


async def test_unknown_model_not_found() -> None:
    svc, _, _ = make()
    with pytest.raises(LakeloomError) as e:
        await svc.get_model("nope")
    assert e.value.code == "not_found"
    assert "list_models" in e.value.hint
    with pytest.raises(LakeloomError) as e2:
        await svc.graph("nope")
    assert e2.value.code == "not_found"


async def test_om_down_after_ttl_raises_upstream_unavailable() -> None:
    svc, om, clock = make(ttl=60)
    await svc.graph("retail")
    om.down = True
    await svc.graph("retail")  # still within TTL: served from cache
    clock.now += 61
    with pytest.raises(LakeloomError) as e:
        await svc.graph("retail")
    assert e.value.code == "upstream_unavailable"
    om.down = False
    assert len(await svc.graph("retail")) > 0  # lock and cache are not poisoned


async def test_export_error_from_om_maps_to_not_found_on_404() -> None:
    svc, om, _ = make()

    async def boom(*a: object, **k: object) -> str:
        raise OMError("gone", status=404)

    om.export_ontology = boom  # type: ignore[method-assign]
    with pytest.raises(LakeloomError) as e:
        await svc.graph("retail")
    assert e.value.code == "not_found"


async def test_get_model_info() -> None:
    svc, _, _ = make()
    info = await svc.get_model("retail")
    assert info.slug == "retail"
    assert info.glossary_id == "id-retail"
    assert info.base_iri == "http://x/ontology/retail/"
    assert info.prefixes == {"retail": "http://x/ontology/retail/"}
    assert info.term_count == 2
    assert info.description is not None
    assert info.description.untrusted is True


async def test_no_public_url_means_no_base_iri() -> None:
    svc, _, _ = make(public=None)
    info = await svc.get_model("retail")
    assert info.base_iri is None
    assert info.prefixes == {}


async def test_om_namespaces_preferred_when_present() -> None:
    svc, om, _ = make()
    om.glossaries[0] = OMGlossary.model_validate(
        {
            "id": "id-retail",
            "name": "retail",
            "fullyQualifiedName": "retail",
            "namespaces": [{"prefix": "rt", "namespace": "http://om/retail#"}],
        }
    )
    info = await svc.get_model("retail")
    assert info.base_iri == "http://om/retail#"
    assert info.prefixes == {"rt": "http://om/retail#"}
    assert info.term_count is None


async def test_list_models_paginates_with_opaque_cursor() -> None:
    svc, om, _ = make()
    page = await svc.list_models(None, 1)
    assert [m.slug for m in page.items] == ["retail"]
    assert page.next_cursor is not None
    assert page.next_cursor != "1"
    assert base64.urlsafe_b64decode(page.next_cursor.encode()) == b"1"
    page2 = await svc.list_models(page.next_cursor, 1)
    assert [m.slug for m in page2.items] == ["hr"]
    assert page2.next_cursor is None
    assert om.list_calls == [None, "1"]


async def test_invalid_cursor_is_validation_error() -> None:
    svc, _, _ = make()
    with pytest.raises(LakeloomError) as e:
        await svc.list_models("!!not-base64!!", 10)
    assert e.value.code == "validation_error"


async def test_parse_failure_is_not_cached() -> None:
    svc, om, _ = make()

    async def bad(*a: object, **k: object) -> str:
        om.exports += 1
        return "this is <not turtle"

    good = om.export_ontology
    om.export_ontology = bad  # type: ignore[method-assign]
    with pytest.raises(LakeloomError):
        await svc.graph("retail")
    om.export_ontology = good  # type: ignore[method-assign]
    assert len(await svc.graph("retail")) > 0
    assert parse_rdf(TURTLE, "turtle")


async def test_unknown_slug_allocates_no_lock_or_cache() -> None:
    svc, _, _ = make()
    for slug in ("nope", "nope2"):
        with pytest.raises(LakeloomError):
            await svc.graph(slug)
    assert svc._locks == {}  # pyright: ignore[reportPrivateUsage]
    assert svc._cache == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("cursor", ["abcd", "aGVsbG8", "a b", "MQ"])
async def test_garbage_cursor_is_validation_error(cursor: str) -> None:
    svc, om, _ = make()
    with pytest.raises(LakeloomError) as e:
        await svc.list_models(cursor, 10)
    assert e.value.code == "validation_error"
    assert om.list_calls == []


async def test_invalidate_one_slug_or_all() -> None:
    svc, om, _ = make()
    await svc.graph("retail")
    await svc.graph("hr")
    seen: list[str | None] = []
    svc.on_invalidate(seen.append)
    svc.invalidate("retail")
    await svc.graph("hr")
    assert om.exports == 2  # hr still cached
    await svc.graph("retail")
    assert om.exports == 3
    svc.clear_cache()
    await svc.graph("hr")
    assert om.exports == 4
    assert seen == ["retail", None]


async def test_derived_value_built_once_per_loaded_graph() -> None:
    svc, _, clock = make(ttl=60)
    builds: list[Graph] = []

    def build(g: Graph) -> int:
        builds.append(g)
        return len(g)

    first = await svc.derived("retail", "k", build)
    assert await svc.derived("retail", "k", build) == first
    assert len(builds) == 1
    svc.invalidate("retail")
    await svc.derived("retail", "k", build)
    clock.now += 61  # TTL expired: reloaded graph, rebuilt value
    await svc.derived("retail", "k", build)
    assert len(builds) == 3 and len({id(g) for g in builds}) == 3


async def test_parse_and_serialise_run_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import lakeloom.core.models as mod

    threads: dict[str, int] = {}
    real_parse, real_serialize = mod.parse_rdf, mod.serialize

    def parse(*a: object, **k: object) -> Graph:
        threads["parse"] = threading.get_ident()
        return real_parse(*a, **k)  # pyright: ignore[reportArgumentType]

    def serialize(*a: object, **k: object) -> bytes:
        threads["serialize"] = threading.get_ident()
        return real_serialize(*a, **k)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(mod, "parse_rdf", parse)
    monkeypatch.setattr(mod, "serialize", serialize)
    svc, _, _ = make()
    await svc.export("retail", "turtle")
    loop_thread = threading.get_ident()
    assert threads["parse"] != loop_thread and threads["serialize"] != loop_thread


NATIVE = """\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://x/ontology/retail/Order> a owl:Class ; rdfs:label "Order" .
<http://x/ontology/retail/Order.total> a owl:DatatypeProperty ;
    rdfs:label "total" ; rdfs:domain <http://x/ontology/retail/Order> ; rdfs:range xsd:decimal .
"""


async def test_graph_merges_paired_native_ontology() -> None:
    svc, om, _ = make()
    om.native = OMNativeExport(working_revision=3, turtle=NATIVE)
    g = await svc.graph("retail")
    prop = URIRef("http://x/ontology/retail/Order.total")
    assert (prop, RDF.type, OWL.DatatypeProperty) in g
    assert (prop, RDFS.domain, URIRef("http://x/ontology/retail/Order")) in g
    # glossary side intact
    assert (URIRef("http://x/ontology/retail/Customer"), RDF.type, SKOS.Concept) in g
    assert om.native_calls == ["retail"]


async def test_unpaired_model_is_glossary_only_without_error() -> None:
    svc, om, _ = make()
    om.native = None
    g = await svc.graph("retail")
    assert (URIRef("http://x/ontology/retail/Customer"), RDF.type, SKOS.Concept) in g
    assert not list(g.subjects(RDF.type, OWL.DatatypeProperty))


async def test_native_export_failure_is_upstream_unavailable() -> None:
    svc, om, _ = make()
    om.native = OMNativeExport(working_revision=1, turtle="not turtle {{{")
    with pytest.raises(LakeloomError) as info:
        await svc.graph("retail")
    assert info.value.code == "upstream_unavailable"


async def test_export_includes_native_and_stays_deterministic() -> None:
    svc, om, _ = make()
    om.native = OMNativeExport(working_revision=3, turtle=NATIVE)
    first = await svc.export("retail", "turtle")
    svc.clear_cache()
    assert await svc.export("retail", "turtle") == first
    assert b"Order.total" in first


async def test_version_changes_with_native_revision() -> None:
    svc, om, _ = make()
    om.native = OMNativeExport(working_revision=3, turtle=NATIVE)
    v3 = await svc.version("retail")
    assert v3 == await svc.version("retail")
    svc.invalidate("retail")
    om.native = OMNativeExport(working_revision=4, turtle=NATIVE)
    assert await svc.version("retail") != v3
    svc.invalidate("retail")
    om.native = None
    assert await svc.version("retail") not in (v3,)


async def test_invalidate_reloads_native_side() -> None:
    svc, om, _ = make()
    om.native = None
    await svc.graph("retail")
    om.native = OMNativeExport(working_revision=2, turtle=NATIVE)
    assert not list((await svc.graph("retail")).subjects(RDF.type, OWL.DatatypeProperty))  # cached
    svc.invalidate("retail")
    assert list((await svc.graph("retail")).subjects(RDF.type, OWL.DatatypeProperty))


async def test_native_build_failure_is_upstream_unavailable() -> None:
    svc, om, _ = make()

    async def boom(name: str):
        raise OMError("cannot express native ontology")

    om.export_native_ontology = boom  # type: ignore[method-assign]
    with pytest.raises(LakeloomError) as info:
        await svc.graph("retail")
    assert info.value.code == "upstream_unavailable"


async def test_native_class_of_a_glossary_term_adds_no_label_or_comment() -> None:
    svc, om, _ = make()
    turtle = NATIVE.replace(
        'rdfs:label "Order" .', 'rdfs:label "Native Order" ; rdfs:comment "native text" .'
    )
    om.native = OMNativeExport(working_revision=1, turtle=turtle)
    g = await svc.graph("retail")
    order = URIRef("http://x/ontology/retail/Order")
    assert (order, RDFS.label, None) not in g
    assert (order, RDFS.comment, None) not in g
    assert (order, RDF.type, OWL.Class) in g
    prop = URIRef("http://x/ontology/retail/Order.total")
    assert (prop, RDFS.label, None) in g  # properties keep theirs
