"""Reading model terms from an RDF graph (FR-IO-02, plan Decisions "Vocabulary")."""

import itertools
import sys
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SKOS

from lakeloom.core.import_diff import import_operations
from lakeloom.core.import_terms import (
    AttributeTerm,
    AxiomTerm,
    ConceptTerm,
    MappingTerm,
    RelationTerm,
    detect_namespace,
    extract_terms,
)
from lakeloom.errors import LakeloomError
from lakeloom.om.native_rdf import build_graph
from lakeloom.rdf.canonical import parse_rdf

sys.path.insert(0, str(Path(__file__).parents[1] / "publish"))
from _graphs import B, cls, data, restriction  # pyright: ignore[reportMissingImports]

FIXTURE = Path(__file__).parents[2] / "fixtures" / "rdf" / "retail_m3b_1.1.0.ttl"
M3B = "http://localhost:8000/ontology/retail_m3b/"
EX = "http://example.org/v/"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
PREFIXES = """
@prefix ex: <http://example.org/v/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix om: <https://open-metadata.org/ontology/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix qudt: <http://qudt.org/schema/qudt/> .
"""


def ttl(body: str) -> Graph:
    return parse_rdf(PREFIXES + body, "turtle")


def fixture() -> Graph:
    return parse_rdf(FIXTURE.read_bytes(), "turtle")


def test_extract_m3b_release_fixture() -> None:
    graph = fixture()
    assert detect_namespace(graph) == M3B
    terms = extract_terms(graph, M3B)

    assert terms.warnings == []
    assert set(terms.concepts) == {"Customer", "Invoice", "Order", "Product"}
    assert terms.concepts["Order"] == ConceptTerm(
        name="Order",
        display_name=None,
        description="Customer orders, one row per order",
        parent=None,
        synonyms=(),
        iri=M3B + "Order",
    )
    datatypes = {key: a.datatype for key, a in terms.attributes.items()}
    assert datatypes == {
        ("Customer", "country"): "xsd:string",
        ("Customer", "created_at"): "xsd:dateTime",
        ("Customer", "customer_id"): "xsd:long",
        ("Customer", "email"): "xsd:string",
        ("Customer", "full_name"): "xsd:string",
        ("Customer", "loyalty_tier"): "xsd:string",
        ("Invoice", "amount"): "xsd:decimal",
        ("Invoice", "currency"): "xsd:string",
        ("Invoice", "invoice_id"): "xsd:long",
        ("Invoice", "issued_at"): "xsd:dateTime",
        ("Invoice", "order_id"): "xsd:long",
        ("Order", "customer_id"): "xsd:long",
        ("Order", "order_date"): "xsd:anySimpleType",
        ("Order", "order_id"): "xsd:long",
        ("Order", "status"): "xsd:string",
        ("Order", "total_amount"): "xsd:decimal",
        ("Product", "category"): "xsd:string",
        ("Product", "name"): "xsd:string",
        ("Product", "product_id"): "xsd:long",
        ("Product", "sku"): "xsd:string",
        ("Product", "unit_price"): "xsd:decimal",
    }
    identifiers = {key for key, a in terms.attributes.items() if a.is_identifier}
    assert identifiers == {
        ("Customer", "customer_id"),
        ("Invoice", "invoice_id"),
        ("Order", "order_id"),
        ("Product", "product_id"),
    }
    assert terms.attributes[("Customer", "loyalty_tier")].description is None
    assert terms.attributes[("Order", "status")] == AttributeTerm(
        concept="Order",
        name="status",
        datatype="xsd:string",
        is_identifier=False,
        allowed_values=(),
        unit=None,
        description="Fulfilment status of the order",
        iri=M3B + "Order.status",
    )
    assert set(terms.relations) == {
        ("Invoice", "hasOrder", "Order"),
        ("Order", "hasCustomer", "Customer"),
    }
    assert terms.relations[("Invoice", "hasOrder", "Order")] == RelationTerm(
        source="Invoice",
        relation_type="hasOrder",
        target="Order",
        description=None,
        iri=M3B + "Invoice.hasOrder",
    )
    assert terms.mappings == set()
    assert terms.axioms["Order"] == AxiomTerm(
        concept="Order", name="OrderPrimaryKey", expressions=(("order_id", "EXACT", 1),)
    )
    assert {a.name for a in terms.axioms.values()} == {
        "CustomerPrimaryKey",
        "InvoicePrimaryKey",
        "OrderPrimaryKey",
        "ProductPrimaryKey",
    }


def test_concept_history_scope_and_deprecation_are_consumed() -> None:
    graph = ttl("""
        ex:Series a skos:Concept ; skos:prefLabel "Series" ;
            skos:definition "A series of episodes." ;
            skos:historyNote "Recast in 2024." ;
            skos:scopeNote "Editorial scope." ;
            owl:deprecated true ; skos:changeNote "Series will move." .
    """)
    terms = extract_terms(graph, EX)
    assert terms.concepts["Series"].history_notes == ("Recast in 2024.",)
    assert terms.concepts["Series"].scope_notes == ("Editorial scope.",)
    assert terms.concepts["Series"].deprecation is True
    assert (
        terms.concepts["Series"].deprecation_reason
        == "Series will move."
    )
    assert terms.warnings == []


def test_glossary_links_explained_by_object_property_are_ignored() -> None:
    graph = ttl("""
        ex:A a skos:Concept ; om:relatedTo ex:B ; skos:related ex:B .
        ex:B a skos:Concept ; om:relatedTo ex:A ; skos:related ex:A ;
            <http://localhost:8000/ontology/retail/placedBy> ex:A .
        ex:B.hasA a owl:ObjectProperty ; rdfs:domain ex:B ; rdfs:range ex:A .
    """)
    terms = extract_terms(graph, EX)
    assert set(terms.relations) == {("B", "hasA", "A")}
    assert terms.warnings == []


def test_glossary_only_link_warns() -> None:
    graph = ttl("""
        ex:A a skos:Concept ; om:relatedTo ex:B .
        ex:B a skos:Concept ; om:relatedTo ex:A ; ex:placedBy ex:C .
        ex:C a skos:Concept .
    """)
    terms = extract_terms(graph, EX)
    assert terms.relations == {}
    assert terms.warnings == ["2 glossary-only links are not imported"]


def test_plain_skos_vocabulary() -> None:
    graph = ttl("""
        ex:scheme a skos:ConceptScheme ; skos:hasTopConcept ex:Animal .
        ex:Animal a skos:Concept ; skos:prefLabel "Animal"@en ; skos:inScheme ex:scheme ;
            skos:topConceptOf ex:scheme ; skos:narrower ex:Dog .
        ex:Dog a skos:Concept ; skos:prefLabel "Domestic dog"@en ;
            skos:definition "A dog." ; skos:broader ex:Animal ;
            skos:altLabel "Hound", "Canine", "Hound" ;
            skos:exactMatch <http://www.wikidata.org/entity/Q144> ;
            skos:closeMatch <https://schema.org/Dog> .
    """)
    assert detect_namespace(graph) == EX
    terms = extract_terms(graph, EX)
    assert terms.warnings == []
    assert terms.concepts["Animal"].display_name is None
    assert terms.concepts["Dog"] == ConceptTerm(
        name="Dog",
        display_name="Domestic dog",
        description="A dog.",
        parent="Animal",
        synonyms=("Canine", "Hound"),
        iri=EX + "Dog",
    )
    assert terms.mappings == {
        MappingTerm("Dog", "exactMatch", "http://www.wikidata.org/entity/Q144"),
        MappingTerm("Dog", "closeMatch", "https://schema.org/Dog"),
    }
    assert terms.attributes == {} and terms.axioms == {}


def test_invalid_local_names_skipped_with_warning() -> None:
    graph = ttl("""
        ex:Good a skos:Concept .
        <http://example.org/v/With%20Space> a skos:Concept .
        ex:has-hyphen a skos:Concept ; skos:definition "x" .
        <http://example.org/v/9lives> a owl:Class .
        ex:Good.bad-attr a owl:DatatypeProperty ; rdfs:domain ex:Good ; rdfs:range xsd:string .
    """)
    terms = extract_terms(graph, EX)
    assert set(terms.concepts) == {"Good", "With Space"}
    assert terms.attributes == {}
    assert terms.warnings == [
        "3 terms with invalid names are not imported: ex:9lives, ex:Good.bad-attr, ex:has-hyphen"
    ]


LABELS = [
    Literal("Kunde", lang="de"),
    Literal("Patron", lang="en"),
    Literal("Client"),
    Literal("Zed", lang="en"),
]


@pytest.mark.parametrize("labels", list(itertools.permutations(LABELS)))
def test_preflabel_choice_is_deterministic(labels: tuple[Literal, ...]) -> None:
    graph = ttl("ex:Customer a skos:Concept .")
    for label in labels:
        graph.add((URIRef(EX + "Customer"), SKOS.prefLabel, label))
    terms = extract_terms(graph, EX)
    assert terms.concepts["Customer"].display_name == "Client"
    # the labels that are not read are reported, never dropped silently
    assert terms.warnings == ["3 triples not representable: skos:prefLabel"]


def test_foreign_subjects_counted() -> None:
    graph = ttl("""
        ex:A a skos:Concept .
        <http://other.org/X> a skos:Concept .
        <http://other.org/Y> a owl:Class .
        <http://other.org/Z> rdfs:label "z" .
        skos:Concept a owl:Class .
        <https://open-metadata.org/entity/glossary/123> a owl:Ontology, skos:ConceptScheme .
    """)
    terms = extract_terms(graph, EX)
    assert set(terms.concepts) == {"A"}
    assert terms.warnings == [
        f"3 subjects outside {EX} are not imported: <http://other.org/X>, "
        "<http://other.org/Y>, <http://other.org/Z>"
    ]


def test_unrepresentable_triples_counted_with_curies() -> None:
    graph = ttl("""
        ex:A a skos:Concept, ex:Special ; rdfs:label "Something else" ;
            owl:sameAs <http://other.org/A> ; skos:note "n1", "n2" .
        ex:A.size a owl:DatatypeProperty ; rdfs:domain ex:A ; rdfs:range xsd:int ;
            skos:note "attr note" ; rdfs:label "Size label" .
    """)
    terms = extract_terms(graph, EX)
    assert terms.attributes[("A", "size")].datatype == "xsd:int"
    assert terms.warnings == [
        "label of A.size not imported",
        "6 triples not representable: owl:sameAs, rdf:type, rdfs:label, skos:note",
    ]


def test_detect_namespace_majority_and_tie() -> None:
    majority = ttl("""
        ex:A a skos:Concept . ex:B a owl:Class .
        <http://z.org/a#X> a skos:Concept .
        skos:Concept a owl:Class . skos:ConceptScheme a owl:Class .
    """)
    assert detect_namespace(majority) == EX
    tie = ttl("""
        <http://z.org/a#X> a skos:Concept . <http://b.org/Y> a skos:Concept .
    """)
    assert detect_namespace(tie) == "http://b.org/"
    with pytest.raises(LakeloomError) as err:
        detect_namespace(ttl("ex:A.x a owl:DatatypeProperty ."))
    assert err.value.code == "validation_error"
    assert err.value.message == "no concepts found"


def test_restrictions_become_one_axiom_sorted() -> None:
    graph = build_graph(
        [cls("Order")],
        [
            data("Order.b", ["Order"], XSD_STRING, displayName="b"),
            data("Order.a", ["Order"], XSD_STRING, displayName="a"),
        ],
        [
            restriction("Order", "Order.b", "MIN", 1),
            restriction("Order", "Order.a", "MAX", 3),
            restriction("Order", "Order.a", "EXACT", 2),
        ],
    )
    terms = extract_terms(graph, B)
    assert terms.axioms == {
        "Order": AxiomTerm(
            concept="Order",
            name="OrderRestrictions",
            expressions=(("a", "EXACT", 2), ("a", "MAX", 3), ("b", "MIN", 1)),
        )
    }
    assert terms.warnings == []


def test_axiom_name_primary_key_vs_restrictions() -> None:
    xsd_long = "http://www.w3.org/2001/XMLSchema#long"
    pk = build_graph(
        [cls("order_line")],
        [
            data("order_line.id", ["order_line"], xsd_long, isIdentifier=True),
            data("order_line.no", ["order_line"], xsd_long, isIdentifier=True),
        ],
        [
            restriction("order_line", "order_line.id", "EXACT", 1),
            restriction("order_line", "order_line.no", "EXACT", 1),
        ],
    )
    assert extract_terms(pk, B).axioms["order_line"].name == "OrderLinePrimaryKey"
    not_identifier = build_graph(
        [cls("Order")],
        [data("Order.id", ["Order"], xsd_long)],
        [restriction("Order", "Order.id", "EXACT", 1)],
    )
    assert extract_terms(not_identifier, B).axioms["Order"].name == "OrderRestrictions"
    other_shape = ttl("""
        ex:A a owl:Class ; rdfs:subClassOf ex:Base, [ a owl:Restriction ;
            owl:onProperty ex:A.x ; owl:someValuesFrom xsd:string ] .
        ex:Base a owl:Class .
        ex:A.x a owl:DatatypeProperty ; rdfs:domain ex:A ; rdfs:range xsd:string .
    """)
    terms = extract_terms(other_shape, EX)
    assert terms.axioms == {}
    assert terms.warnings == ["2 class axioms not representable"]


def test_attribute_details_and_missing_range() -> None:
    graph = ttl("""
        ex:A a owl:Class .
        ex:A.height a owl:DatatypeProperty ; rdfs:domain ex:A ; rdfs:range xsd:decimal ;
            owl:oneOf ( "1.5" "2.0" ) ; qudt:unit "unit:M" ; rdfs:comment "Height" .
        ex:A.free a owl:DatatypeProperty ; rdfs:domain ex:A .
    """)
    terms = extract_terms(graph, EX)
    assert terms.attributes[("A", "height")] == AttributeTerm(
        concept="A",
        name="height",
        datatype="xsd:decimal",
        is_identifier=False,
        allowed_values=("1.5", "2.0"),
        unit="unit:M",
        description="Height",
        iri=EX + "A.height",
    )
    assert terms.attributes[("A", "free")].datatype == "xsd:string"
    assert terms.warnings == ["A.free has no range; read as xsd:string"]


LONG = "x" * 257


@pytest.mark.parametrize(
    ("body", "warning"),
    [
        ('ex:A a owl:Class ; skos:altLabel "" .', "1 triples not representable: skos:altLabel"),
        (
            f'ex:A a owl:Class ; skos:altLabel "{LONG}" .',
            "1 triples not representable: skos:altLabel",
        ),
        ('ex:A a owl:Class ; skos:prefLabel "" .', "1 triples not representable: skos:prefLabel"),
        (
            f'ex:A a owl:Class ; skos:prefLabel "{LONG}" .',
            "1 triples not representable: skos:prefLabel",
        ),
        (
            "ex:A a owl:Class . ex:A.v a owl:DatatypeProperty ; rdfs:domain ex:A ; "
            'rdfs:range xsd:string ; owl:oneOf ( "" ) .',
            "1 triples not representable: owl:oneOf",
        ),
        (
            f"ex:A a owl:Class . ex:A.v a owl:DatatypeProperty ; rdfs:domain ex:A ; "
            f'rdfs:range xsd:string ; owl:oneOf ( "ok" "{LONG}" ) .',
            "1 triples not representable: owl:oneOf",
        ),
        (
            "ex:A a owl:Class . ex:A.v a owl:DatatypeProperty ; rdfs:domain ex:A ; "
            'rdfs:range xsd:string ; qudt:unit "" .',
            "1 triples not representable: qudt:unit",
        ),
        (
            f"ex:A a owl:Class . ex:A.v a owl:DatatypeProperty ; rdfs:domain ex:A ; "
            f'rdfs:range xsd:string ; qudt:unit "{"u" * 65}" .',
            "1 triples not representable: qudt:unit",
        ),
        (
            "ex:A a owl:Class . <http://example.org/v/A.has-b> a owl:ObjectProperty ; "
            "rdfs:domain ex:A ; rdfs:range ex:A .",
            "1 terms with invalid names are not imported: ex:A.has-b",
        ),
        (
            "ex:A a owl:Class ; skos:exactMatch <http://> .",
            "1 triples not representable: skos:exactMatch",
        ),
    ],
)
def test_values_the_operations_reject_are_counted_not_raised(body: str, warning: str) -> None:
    incoming = extract_terms(ttl(body), EX)
    assert warning in incoming.warnings
    empty = extract_terms(Graph(), EX)
    # building the operations must never fail on file text
    import_operations(
        empty, incoming, mode="merge", versions={}, axiom_names={}, rule_id="R-IMPORT"
    )


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_mapping_iris_normalised_like_anyurl(mode: str) -> None:
    """A model holding ``https://schema.org/`` and a file holding ``https://schema.org`` are
    the same mapping: nothing to import in merge mode, nothing removed in replace mode."""
    model = ttl("ex:A a owl:Class ; skos:exactMatch <https://schema.org/> .")
    upload = ttl("ex:A a owl:Class ; skos:exactMatch <https://schema.org> .")
    current, incoming = extract_terms(model, EX), extract_terms(upload, EX)
    assert incoming.mappings == {MappingTerm("A", "exactMatch", "https://schema.org/")}
    result = import_operations(
        current,
        incoming,
        mode=mode,  # type: ignore[arg-type]
        versions={"A": 0.1},
        axiom_names={},
        rule_id="R-IMPORT",
    )
    assert result.operations == []
