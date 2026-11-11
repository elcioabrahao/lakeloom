"""RDF for a native OM ontology's *working* state (ADR 0011, "Reading a native ontology").

rc1 exports RDF only for sealed releases (``GET /v1/ontologyReleases/{id}/export``), and
sealing is a write. The working members (concepts, properties, axioms) are readable, so the
read path rebuilds the asserted triples from them, with the same shapes OM's release export
uses: ``owl:Class``, ``owl:DatatypeProperty`` / ``owl:ObjectProperty`` /
``owl:AnnotationProperty`` with ``rdfs:domain`` and ``rdfs:range``, ``rdfs:label`` (the
display name when set) and ``rdfs:comment``; enum values as ``owl:oneOf``, the unit as
``qudt:unit``. Descriptions and names are untrusted text and only ever become literals.
"""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD

QUDT_UNIT = URIRef("http://qudt.org/schema/qudt/unit")
_CONCEPT_TYPES = {"OWL_CLASS": [OWL.Class], "SKOS_CONCEPT": [], "BOTH": [OWL.Class]}
_KINDS = {
    "DATA": OWL.DatatypeProperty,
    "OBJECT": OWL.ObjectProperty,
    "ANNOTATION": OWL.AnnotationProperty,
}
_CARDINALITY = {
    "EXACT": (OWL.qualifiedCardinality, OWL.cardinality),
    "MIN": (OWL.minCardinality, None),
    "MAX": (OWL.maxCardinality, None),
}


def _iris(value: object) -> list[URIRef]:
    if not isinstance(value, list):
        return []
    return [URIRef(i) for i in cast("list[object]", value) if isinstance(i, str) and i]


def _text(member: Mapping[str, Any], key: str) -> str | None:
    value = member.get(key)
    return value if isinstance(value, str) and value else None


def _annotate(g: Graph, node: URIRef, member: Mapping[str, Any]) -> None:
    name = _text(member, "displayName") or _text(member, "name")
    if name:
        g.add((node, RDFS.label, Literal(name)))
    description = _text(member, "description")
    if description:
        g.add((node, RDFS.comment, Literal(description)))


def _restriction(g: Graph, expr: Mapping[str, Any]) -> BNode | URIRef | None:
    kind = expr.get("kind")
    if kind == "NAMED_CLASS" and isinstance(expr.get("classIri"), str):
        return URIRef(expr["classIri"])
    if kind != "RESTRICTION" or not isinstance(expr.get("propertyIri"), str):
        return None
    node = BNode()
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, OWL.onProperty, URIRef(expr["propertyIri"])))
    restriction = str(expr.get("restrictionKind", ""))
    cardinality = expr.get("cardinality")
    if restriction in _CARDINALITY and isinstance(cardinality, int):
        predicate = _CARDINALITY[restriction][1] or _CARDINALITY[restriction][0]
        g.add((node, predicate, Literal(cardinality, datatype=XSD.nonNegativeInteger)))
        return node
    return None


def build_graph(
    concepts: Sequence[Mapping[str, Any]],
    properties: Sequence[Mapping[str, Any]],
    axioms: Sequence[Mapping[str, Any]],
) -> Graph:
    g = Graph()
    for concept in concepts:
        iri = _text(concept, "iri")
        if iri is None:
            continue
        node = URIRef(iri)
        for rdf_type in _CONCEPT_TYPES.get(str(concept.get("conceptType")), [OWL.Class]):
            g.add((node, RDF.type, rdf_type))
        _annotate(g, node, concept)
    for prop in properties:
        iri = _text(prop, "iri")
        if iri is None:
            continue
        node = URIRef(iri)
        g.add((node, RDF.type, _KINDS.get(str(prop.get("kind")), OWL.DatatypeProperty)))
        _annotate(g, node, prop)
        for domain in _iris(prop.get("domain")):
            g.add((node, RDFS.domain, domain))
        for range_ in _iris(prop.get("range")):
            g.add((node, RDFS.range, range_))
        values = [
            v for v in cast("list[object]", prop.get("enumValues") or []) if isinstance(v, str)
        ]
        if values:
            head = BNode()
            Collection(g, head, [Literal(v) for v in values])
            g.add((node, OWL.oneOf, head))
        unit = _text(prop, "unit")
        if unit:
            g.add((node, QUDT_UNIT, Literal(unit)))
        if prop.get("isIdentifier") is True:
            g.add((node, RDF.type, OWL.FunctionalProperty))
    for axiom in axioms:
        subject = _text(axiom, "subjectIri")
        if subject is None or axiom.get("axiomType") != "SUBCLASS_OF":
            continue
        for expr in cast("Sequence[Mapping[str, Any]]", axiom.get("expressions") or []):
            target = _restriction(g, expr)
            if target is not None:
                g.add((URIRef(subject), RDFS.subClassOf, target))
    return g
