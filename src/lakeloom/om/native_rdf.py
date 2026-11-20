"""RDF for a native OM ontology's *working* state (ADR 0011, native ontology appendix).

rc1 exports RDF only for sealed releases (``GET /v1/ontologyReleases/{id}/export``), and
sealing is a write. The working members (concepts, properties, axioms) are readable, so the
read path rebuilds the asserted triples from them, with the same shapes OM's release export
uses: ``owl:Class``, ``owl:DatatypeProperty`` / ``owl:ObjectProperty`` /
``owl:AnnotationProperty`` with ``rdfs:domain`` and ``rdfs:range``, ``rdfs:label`` (the
display name when set) and ``rdfs:comment``; enum values (strings or ``OntologyLiteral``
objects) as ``owl:oneOf``, the unit as ``qudt:unit``. Axioms render class-level:
``SUBCLASS_OF`` restrictions as ``rdfs:subClassOf`` blank-node restrictions,
``DISJOINT_WITH`` / ``EQUIVALENT_CLASS`` as ``owl:disjointWith`` / ``owl:equivalentClass``
links to each named class in the axiom's expressions. Descriptions and names are
untrusted text and only ever become literals.

A member that cannot be expressed (no absolute IRI, wrong shape) is skipped with a WARNING
naming only its kind and OM id; the raw value is never logged.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlparse

from rdflib import BNode, Graph, Literal, Node, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD

_log = logging.getLogger(__name__)
_BAD_IRI_CHARS = re.compile(r'[\x00-\x20<>"{}|\\^`\x7f]')
QUDT_UNIT = URIRef("http://qudt.org/schema/qudt/unit")
_CONCEPT_TYPES = {"OWL_CLASS": [OWL.Class], "SKOS_CONCEPT": [], "BOTH": [OWL.Class]}
_KINDS = {
    "DATA": OWL.DatatypeProperty,
    "OBJECT": OWL.ObjectProperty,
    "ANNOTATION": OWL.AnnotationProperty,
}
# Class-level axiom renderings. SUBCLASS_OF takes restrictions (below) or named classes;
# DISJOINT_WITH / EQUIVALENT_CLASS link the subject to each named class in the expressions.
# Assertion axiom types (CLASS_ASSERTION, *_PROPERTY_ASSERTION) are about individuals and
# stay unrendered. DISJOINT_WITH was measured live on rc1 (cigars, 2026-11-20): before this
# table the disjoint axiom applied in OM never reached the merged graph or any export.
_AXIOM_PREDICATES = {
    "SUBCLASS_OF": RDFS.subClassOf,
    "DISJOINT_WITH": OWL.disjointWith,
    "EQUIVALENT_CLASS": OWL.equivalentClass,
}
_CARDINALITY = {
    "EXACT": (OWL.qualifiedCardinality, OWL.cardinality),
    "MIN": (OWL.minCardinality, None),
    "MAX": (OWL.maxCardinality, None),
}


def _valid_iri(value: object) -> str | None:
    """``value`` when it is an absolute IRI that turtle can carry, else ``None``."""
    if not isinstance(value, str) or not value or _BAD_IRI_CHARS.search(value):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme and (parsed.netloc or parsed.path) else None


def _iris(value: object) -> list[URIRef] | None:
    """IRIs of a list field; ``None`` when the field holds anything invalid."""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    out: list[URIRef] = []
    for item in cast("list[object]", value):
        iri = _valid_iri(item)
        if iri is None:
            return None
        out.append(URIRef(iri))
    return out


def _enum_values(value: object) -> list[str] | None:
    """Enum values in OM's order: plain strings or ``OntologyLiteral`` objects with a string
    ``value`` (what rc1 stores, ADR 0016 R1b); ``None`` when the list holds anything else."""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in cast("list[object]", value):
        if isinstance(item, dict):
            item = cast("dict[str, object]", item).get("value")
        if not isinstance(item, str):
            return None
        out.append(item)
    return out


def _text(member: Mapping[str, Any], key: str) -> str | None:
    value = member.get(key)
    return value if isinstance(value, str) and value else None


def _skip(kind: str, member: Mapping[str, Any]) -> None:
    ident = member.get("id")
    _log.warning(
        "skipping malformed native %s (OM id %s)", kind, ident if isinstance(ident, str) else "?"
    )


def _annotate(g: Graph, node: URIRef, member: Mapping[str, Any]) -> None:
    name = _text(member, "displayName") or _text(member, "name")
    if name:
        g.add((node, RDFS.label, Literal(name)))
    description = _text(member, "description")
    if description:
        g.add((node, RDFS.comment, Literal(description)))


def _restriction(g: Graph, expr: object) -> BNode | URIRef | None:
    if not isinstance(expr, dict):
        return None
    e = cast("dict[str, Any]", expr)
    kind = e.get("kind")
    if kind == "NAMED_CLASS":
        iri = _valid_iri(e.get("classIri"))
        return None if iri is None else URIRef(iri)
    prop = _valid_iri(e.get("propertyIri"))
    restriction = str(e.get("restrictionKind", ""))
    cardinality = e.get("cardinality")
    if (
        kind != "RESTRICTION"
        or prop is None
        or restriction not in _CARDINALITY
        or not isinstance(cardinality, int)
        or isinstance(cardinality, bool)
        or cardinality < 0
    ):
        return None
    node = BNode()
    predicate = _CARDINALITY[restriction][1] or _CARDINALITY[restriction][0]
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, OWL.onProperty, URIRef(prop)))
    g.add((node, predicate, Literal(cardinality, datatype=XSD.nonNegativeInteger)))
    return node


def build_graph(
    concepts: Sequence[Mapping[str, Any]],
    properties: Sequence[Mapping[str, Any]],
    axioms: Sequence[Mapping[str, Any]],
) -> Graph:
    g = Graph()
    for concept in concepts:
        iri = _valid_iri(concept.get("iri"))
        if iri is None:
            _skip("concept", concept)
            continue
        node = URIRef(iri)
        for rdf_type in _CONCEPT_TYPES.get(str(concept.get("conceptType")), [OWL.Class]):
            g.add((node, RDF.type, rdf_type))
        _annotate(g, node, concept)
    for prop in properties:
        iri = _valid_iri(prop.get("iri"))
        domains = _iris(prop.get("domain"))
        ranges = _iris(prop.get("range"))
        values = _enum_values(prop.get("enumValues"))
        if iri is None or domains is None or ranges is None or values is None:
            _skip("property", prop)
            continue
        node = URIRef(iri)
        g.add((node, RDF.type, _KINDS.get(str(prop.get("kind")), OWL.DatatypeProperty)))
        _annotate(g, node, prop)
        for domain in domains:
            g.add((node, RDFS.domain, domain))
        for range_ in ranges:
            g.add((node, RDFS.range, range_))
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
        subject = _valid_iri(axiom.get("subjectIri"))
        if subject is None:
            _skip("axiom", axiom)
            continue
        predicate = _AXIOM_PREDICATES.get(str(axiom.get("axiomType", "")))
        if predicate is None:
            continue  # assertions and unknown types have no class-level rendering yet
        expressions = axiom.get("expressions")
        exprs = cast("list[object]", expressions) if isinstance(expressions, list) else []
        for expr in exprs:
            if predicate is RDFS.subClassOf:
                target: Node | None = _restriction(g, expr)
            else:
                # class-level links (disjoint / equivalent) take named classes only
                target = None
                if isinstance(expr, dict) and expr.get("kind") == "NAMED_CLASS":
                    iri = _valid_iri(expr.get("classIri"))
                    target = None if iri is None else URIRef(iri)
            if target is None:
                _skip("axiom expression", axiom)
            else:
                g.add((URIRef(subject), predicate, target))
    return g
