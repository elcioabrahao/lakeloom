from rdflib import RDF, RDFS, Graph, Literal, Namespace
from rdflib.namespace import SKOS

from lakeloom.rdf.skos import skos_projection

EX = Namespace("https://example.org/m/")


def test_skos_projection_keeps_only_skos() -> None:
    g = Graph()
    g.add((EX.scheme, RDF.type, SKOS.ConceptScheme))
    g.add((EX.a, RDF.type, SKOS.Concept))
    g.add((EX.a, SKOS.prefLabel, Literal("A", lang="en")))
    g.add((EX.a, SKOS.inScheme, EX.scheme))
    g.add((EX.a, RDFS.comment, Literal("not skos")))
    g.add((EX.a, EX.custom, Literal("x")))
    g.add((EX.a, RDF.type, EX.Other))
    g.add((EX.b, RDF.type, SKOS.Concept))
    g.add((EX.b, RDFS.subClassOf, EX.a))
    g.add((EX.c, RDF.type, SKOS.Concept))
    g.add((EX.c, SKOS.broader, EX.a))
    g.add((EX.c, RDFS.subClassOf, EX.b))
    out = skos_projection(g)
    assert (EX.a, RDFS.comment, None) not in out
    assert (EX.a, EX.custom, None) not in out
    assert (EX.a, RDF.type, EX.Other) not in out
    assert (EX.a, SKOS.prefLabel, Literal("A", lang="en")) in out
    assert (EX.scheme, RDF.type, SKOS.ConceptScheme) in out
    assert (EX.b, SKOS.broader, EX.a) in out  # derived from subClassOf
    assert (EX.c, SKOS.broader, EX.b) not in out  # already has a broader
    assert (EX.c, SKOS.broader, EX.a) in out
