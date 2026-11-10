"""SKOS-only projection of a model graph (ADR 0009).

Keeps ``skos:ConceptScheme`` / ``skos:Concept`` typing and the SKOS predicates listed in
``SKOS_PREDICATES``. Where two ``skos:Concept`` classes are linked by ``rdfs:subClassOf`` and the
child has no ``skos:broader`` at all, a ``skos:broader`` is derived from it.
"""

from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import SKOS

__all__ = ["SKOS_PREDICATES", "skos_projection"]

SKOS_PREDICATES: frozenset[URIRef] = frozenset(
    {
        SKOS.prefLabel,
        SKOS.altLabel,
        SKOS.definition,
        SKOS.broader,
        SKOS.narrower,
        SKOS.related,
        SKOS.exactMatch,
        SKOS.closeMatch,
        SKOS.broadMatch,
        SKOS.narrowMatch,
        SKOS.relatedMatch,
        SKOS.inScheme,
        SKOS.topConceptOf,
    }
)
_SKOS_TYPES: frozenset[URIRef] = frozenset({SKOS.Concept, SKOS.ConceptScheme})


def skos_projection(graph: Graph) -> Graph:
    """Return a new graph holding only the SKOS view of ``graph``."""
    out = Graph(bind_namespaces="none")
    for prefix, ns in graph.namespaces():
        out.bind(prefix, ns, override=True)
    for s, p, o in graph:
        if p in SKOS_PREDICATES or (p == RDF.type and o in _SKOS_TYPES):
            out.add((s, p, o))
    concepts = set(graph.subjects(RDF.type, SKOS.Concept))
    has_broader = {s for s in concepts if (s, SKOS.broader, None) in graph}
    for child, _, parent in graph.triples((None, RDFS.subClassOf, None)):
        if child in concepts and parent in concepts and child not in has_broader:
            out.add((child, SKOS.broader, parent))
    return out
