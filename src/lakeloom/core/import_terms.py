"""Model terms read from an RDF graph (FR-IO-02).

:func:`extract_terms` reads exactly the vocabulary Lakeloom exports (glossary export plus the
native members, ADR 0011 native appendix and ADR 0016 R1) from the subjects of one namespace,
mapping them to the target model by local name. Everything it cannot represent is counted in
:attr:`TermSet.warnings`; nothing is dropped silently except OpenMetadata's bookkeeping. The
graph is untrusted: its text only ever becomes term fields and capped warning CURIEs.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import unquote

from pydantic import AnyUrl, TypeAdapter, ValidationError
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD
from rdflib.term import Node

from lakeloom.core.mapper import pascal_name
from lakeloom.core.model_policy import XSD_DATATYPE_PATTERN
from lakeloom.core.operations import MappingKind
from lakeloom.errors import LakeloomError
from lakeloom.om.native_rdf import QUDT_UNIT

__all__ = [
    "AttributeTerm",
    "AxiomTerm",
    "ConceptTerm",
    "MappingTerm",
    "RelationTerm",
    "TermSet",
    "detect_namespace",
    "extract_terms",
]

OM = Namespace("https://open-metadata.org/ontology/")
_OM_ROOT = "https://open-metadata.org/"
_VOCABULARIES = (str(RDF), str(RDFS), str(OWL), str(SKOS), str(XSD), _OM_ROOT)
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,127}$")
_RELATION = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*$")
_XSD_LOCAL = re.compile(f"^{XSD_DATATYPE_PATTERN}$")
_CURIE_LOCAL = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_MATCHES: dict[Node, MappingKind] = {
    SKOS.exactMatch: "exactMatch",
    SKOS.closeMatch: "closeMatch",
    SKOS.broadMatch: "broadMatch",
    SKOS.narrowMatch: "narrowMatch",
    SKOS.relatedMatch: "relatedMatch",
}
_CARDINALITIES: dict[Node, str] = {
    OWL.cardinality: "EXACT",
    OWL.qualifiedCardinality: "EXACT",
    OWL.minCardinality: "MIN",
    OWL.maxCardinality: "MAX",
}
_CONCEPT_TYPES = {SKOS.Concept, OWL.Class}
_SCHEME_TYPES = {OWL.Ontology, SKOS.ConceptScheme}
_BOOKKEEPING = {SKOS.inScheme, SKOS.topConceptOf, SKOS.narrower, OM.fullyQualifiedName}
_ANNOTATIONS = frozenset({SKOS.historyNote, SKOS.scopeNote, SKOS.changeNote, OWL.deprecated})
_MAX_SHORT = 256  # operations.Short
_MAX_LIST = 100  # operations.ShortList
_MAX_UNIT = 64
_MAX_LISTED = 10
_MAX_CURIE = 200
_MAX_LIST_NODES = 10_000
_URL: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)

type Triple = tuple[Node, Node, Node]


@dataclass(frozen=True)
class ConceptTerm:
    name: str
    display_name: str | None
    description: str | None
    parent: str | None
    synonyms: tuple[str, ...]
    iri: str
    history_notes: tuple[str, ...] = ()
    scope_notes: tuple[str, ...] = ()
    deprecation: bool = False
    deprecation_reason: str | None = None


@dataclass(frozen=True)
class AttributeTerm:
    concept: str
    name: str
    datatype: str
    is_identifier: bool
    allowed_values: tuple[str, ...]
    unit: str | None
    description: str | None
    iri: str


@dataclass(frozen=True)
class RelationTerm:
    source: str
    relation_type: str
    target: str
    description: str | None
    iri: str


@dataclass(frozen=True)
class MappingTerm:
    concept: str
    kind: MappingKind
    target_iri: str


@dataclass(frozen=True)
class AxiomTerm:
    """All cardinality restrictions of one concept; expressions are (property, kind, n)."""

    concept: str
    name: str
    expressions: tuple[tuple[str, str, int], ...]


@dataclass
class TermSet:
    """The terms of one namespace, keyed by local name; ``namespace`` is where they were read."""

    concepts: dict[str, ConceptTerm]
    attributes: dict[tuple[str, str], AttributeTerm]
    relations: dict[tuple[str, str, str], RelationTerm]
    mappings: set[MappingTerm]
    axioms: dict[str, AxiomTerm]
    warnings: list[str] = field(default_factory=list[str])
    namespace: str = ""


def _vocabulary(iri: str) -> bool:
    return iri.startswith(_VOCABULARIES)


def _namespace_of(iri: str) -> str | None:
    cut = max(iri.rfind("/"), iri.rfind("#"))
    return iri[: cut + 1] if cut > 0 and cut < len(iri) - 1 else None


def detect_namespace(graph: Graph) -> str:
    """The namespace most ``owl:Class``/``skos:Concept`` subjects live in (ties: the smaller).

    Vocabulary declarations (RDF, RDFS, OWL, SKOS, XSD and OpenMetadata's own) do not count.
    """
    subjects = {
        s
        for t in _CONCEPT_TYPES
        for s in graph.subjects(RDF.type, t)
        if isinstance(s, URIRef) and not _vocabulary(str(s))
    }
    counts = Counter(ns for s in subjects if (ns := _namespace_of(str(s))) is not None)
    if not counts:
        raise LakeloomError(
            "validation_error",
            "no concepts found",
            hint="the file needs owl:Class or skos:Concept subjects with absolute IRIs",
        )
    return min(counts, key=lambda ns: (-counts[ns], ns))


def _listed(items: set[str]) -> str:
    return ", ".join(sorted(items)[:_MAX_LISTED])


def _english(value: Node) -> bool:
    if not isinstance(value, Literal):
        return False
    lang = (value.language or "").lower()
    return lang in ("", "en") or lang.startswith("en-")


def _fits(text: str, max_len: int | None) -> bool:
    """Non-empty and within ``max_len``: what the operation models accept (``Short``...)."""
    return len(text) >= 1 and (max_len is None or len(text) <= max_len)


def _pick(values: list[Node], max_len: int | None = None) -> Literal | None:
    """The lexicographically first non-empty literal without a language or in English."""
    candidates = sorted(
        (v for v in values if isinstance(v, Literal) and _english(v)), key=lambda v: str(v)
    )
    for value in candidates:
        if _fits(str(value), max_len):
            return value
    return None


class _Reader:
    def __init__(self, graph: Graph, namespace: str) -> None:
        self.g = graph
        self.ns = namespace
        self.used: set[Triple] = set()
        self.skipped: set[Node] = set()  # subjects reported as a whole
        self.prefixes = sorted(
            ((p, str(n)) for p, n in graph.namespaces() if p), key=lambda pn: -len(pn[1])
        )
        self.invalid: set[str] = set()
        self.foreign: set[str] = set()
        self.unknown: set[str] = set()
        self.labels: set[str] = set()
        self.no_range: set[str] = set()
        self.class_axioms = 0
        self.links: set[frozenset[str]] = set()
        self.concept_nodes: dict[str, URIRef] = {}
        self.concepts: dict[str, ConceptTerm] = {}
        self.attributes: dict[tuple[str, str], AttributeTerm] = {}
        self.attribute_nodes: dict[Node, tuple[str, str]] = {}
        self.relations: dict[tuple[str, str, str], RelationTerm] = {}
        self.mappings: set[MappingTerm] = set()
        self.restrictions: dict[str, set[tuple[str, str, int]]] = {}
        self.history: dict[str, list[str]] = {}
        self.scope: dict[str, list[str]] = {}
        self.deprecated: dict[str, bool] = {}
        self.deprecation_reason: dict[str, str] = {}

    # helpers -------------------------------------------------------------------------------

    def curie(self, node: Node) -> str:
        iri = str(node)
        for prefix, ns in self.prefixes:
            local = iri[len(ns) :]
            if iri.startswith(ns) and _CURIE_LOCAL.match(local) and not local.endswith("."):
                return f"{prefix}:{local}"[:_MAX_CURIE]
        return f"<{iri[: _MAX_CURIE - 2]}>"

    def local(self, node: URIRef) -> str:
        return unquote(str(node)[len(self.ns) :])

    def objects(self, s: Node, p: URIRef) -> list[Node]:
        return sorted(self.g.objects(s, p), key=str)

    def use(self, s: Node, p: Node, o: Node) -> None:
        self.used.add((s, p, o))

    def concept_of(self, node: Node) -> str | None:
        if isinstance(node, URIRef) and str(node).startswith(self.ns):
            name = self.local(node)
            if self.concept_nodes.get(name) == node:
                return name
        return None

    def text(self, s: Node, p: URIRef, max_len: int | None = None) -> str | None:
        chosen = _pick(self.objects(s, p), max_len)
        if chosen is None:
            return None
        self.use(s, p, chosen)
        return str(chosen)

    def types(self, s: Node, wanted: set[URIRef]) -> set[Node]:
        found = set(self.g.objects(s, RDF.type)) & wanted
        for t in found:
            self.use(s, RDF.type, t)
        return found

    # passes --------------------------------------------------------------------------------

    def classify(self) -> tuple[list[URIRef], list[URIRef], list[URIRef]]:
        concepts: list[URIRef] = []
        attributes: list[URIRef] = []
        relations: list[URIRef] = []
        subjects = sorted({s for s in self.g.subjects() if isinstance(s, URIRef)}, key=str)
        for s in subjects:
            types = set(self.g.objects(s, RDF.type))
            iri = str(s)
            if types & _SCHEME_TYPES:
                self.skipped.add(s)  # the ontology / concept scheme node
            elif not iri.startswith(self.ns) or iri == self.ns:
                self.skipped.add(s)
                if not _vocabulary(iri):
                    self.foreign.add(self.curie(s))
            elif "." not in self.local(s) and types & _CONCEPT_TYPES:
                concepts.append(s)
            elif "." in self.local(s) and OWL.DatatypeProperty in types:
                attributes.append(s)
            elif "." in self.local(s) and OWL.ObjectProperty in types:
                relations.append(s)
        for s in concepts:
            name = self.local(s)
            if _NAME.match(name):
                self.concept_nodes[name] = s
            else:
                self.invalid.add(self.curie(s))
                self.skipped.add(s)
        return concepts, attributes, relations

    def member(self, s: URIRef, member_pattern: re.Pattern[str]) -> tuple[str, str] | None:
        """(concept, member) of ``ns:C.m`` declared on ``ns:C``; ``None`` when skipped."""
        concept, member = self.local(s).split(".", 1)
        if not _NAME.match(concept) or not member_pattern.match(member):
            self.invalid.add(self.curie(s))
            self.skipped.add(s)
            return None
        node = self.concept_nodes.get(concept)
        if node is None:
            self.unknown.add(self.curie(s))
            self.skipped.add(s)
            return None
        if (s, RDFS.domain, node) not in self.g:
            return None  # not Lakeloom's shape: its triples are counted
        self.use(s, RDFS.domain, node)
        return concept, member

    def attribute(self, s: URIRef) -> None:
        names = self.member(s, _NAME)
        if names is None:
            return
        concept, name = names
        identifier = OWL.FunctionalProperty in self.types(
            s, {OWL.DatatypeProperty, OWL.FunctionalProperty}
        )
        datatype: str | None = None
        for range_ in self.objects(s, RDFS.range):
            if range_ == RDFS.Literal:
                datatype = "xsd:anySimpleType"
            elif isinstance(range_, URIRef) and str(range_).startswith(str(XSD)):
                candidate = "xsd:" + str(range_)[len(str(XSD)) :]
                datatype = candidate if _XSD_LOCAL.match(candidate) else None
            if datatype is not None:
                self.use(s, RDFS.range, range_)
                break
        if datatype is None:
            datatype = "xsd:string"
            self.no_range.add(f"{concept}.{name}")
        unit: str | None = None
        for value in self.objects(s, QUDT_UNIT):
            if _fits(str(value), _MAX_UNIT):
                unit = str(value)
                self.use(s, QUDT_UNIT, value)
                break
        for label in self.objects(s, RDFS.label):
            self.use(s, RDFS.label, label)
            if str(label) != name:
                self.labels.add(f"{concept}.{name}")
        self.attributes[(concept, name)] = AttributeTerm(
            concept=concept,
            name=name,
            datatype=datatype,
            is_identifier=identifier,
            allowed_values=self.one_of(s),
            unit=unit,
            description=self.text(s, RDFS.comment),
            iri=str(s),
        )
        self.attribute_nodes[s] = (concept, name)

    def one_of(self, s: URIRef) -> tuple[str, ...]:
        for head in self.objects(s, OWL.oneOf):
            walked = self.walk_list(head)
            if walked is not None:
                values, triples = walked
                self.used.update(triples)
                self.use(s, OWL.oneOf, head)
                return values
        return ()

    def walk_list(self, head: Node) -> tuple[tuple[str, ...], set[Triple]] | None:
        values: list[str] = []
        triples: set[Triple] = set()
        node = head
        seen: set[Node] = set()
        while node != RDF.nil:
            if not isinstance(node, BNode) or node in seen or len(seen) > _MAX_LIST_NODES:
                return None
            seen.add(node)
            firsts = list(self.g.objects(node, RDF.first))
            rests = list(self.g.objects(node, RDF.rest))
            if len(firsts) != 1 or len(rests) != 1 or not isinstance(firsts[0], Literal):
                return None
            values.append(str(firsts[0]))
            triples |= {(node, RDF.first, firsts[0]), (node, RDF.rest, rests[0])}
            node = rests[0]
        if len(values) > _MAX_LIST or not all(_fits(v, _MAX_SHORT) for v in values):
            return None
        return tuple(values), triples

    def relation(self, s: URIRef) -> None:
        names = self.member(s, _RELATION)
        if names is None:
            return
        source, relation_type = names
        target = next(
            (t for r in self.objects(s, RDFS.range) if (t := self.concept_of(r)) is not None),
            None,
        )
        if target is None:
            return
        self.use(s, RDFS.range, self.concept_nodes[target])
        self.types(s, {OWL.ObjectProperty})
        for label in self.objects(s, RDFS.label):
            if str(label) == relation_type:
                self.use(s, RDFS.label, label)
        self.relations[(source, relation_type, target)] = RelationTerm(
            source=source,
            relation_type=relation_type,
            target=target,
            description=self.text(s, RDFS.comment),
            iri=str(s),
        )

    def concept(self, name: str, s: URIRef) -> None:
        self.types(s, {SKOS.Concept, OWL.Class, OM.GlossaryTerm})
        label = self.text(s, SKOS.prefLabel, _MAX_SHORT)
        description = self.text(s, SKOS.definition)
        if description is None:
            description = self.text(s, RDFS.comment)
        parent: str | None = None
        for target in self.objects(s, SKOS.broader):
            found = self.concept_of(target)
            if found is not None and found != name:
                parent = found
                self.use(s, SKOS.broader, target)
                break
        synonyms = sorted(
            {str(v) for v in self.objects(s, SKOS.altLabel) if isinstance(v, Literal)}
        )
        kept = [v for v in synonyms if _fits(v, _MAX_SHORT)][:_MAX_LIST]
        for value in self.objects(s, SKOS.altLabel):
            if str(value) in kept and isinstance(value, Literal):
                self.use(s, SKOS.altLabel, value)
        for p, o in self.g.predicate_objects(s):
            self.statement(name, s, p, o, parent)
        self.concepts[name] = ConceptTerm(
            name=name,
            display_name=label if label != name else None,
            description=description,
            parent=parent,
            synonyms=tuple(kept),
            iri=str(s),
            history_notes=tuple(self.history.get(name, [])),
            scope_notes=tuple(self.scope.get(name, [])),
            deprecation=self.deprecated.get(name, False),
            deprecation_reason=self.deprecation_reason.get(name),
        )

    def statement(self, name: str, s: URIRef, p: Node, o: Node, parent: str | None) -> None:
        """The remaining concept predicates: bookkeeping, mappings, axioms, glossary links."""
        if p in _BOOKKEEPING or (p == RDFS.label and str(o) == name):
            self.use(s, p, o)
        elif p in _MATCHES and isinstance(o, URIRef):
            try:
                target = str(_URL.validate_python(str(o)))  # as AddMapping normalises it
            except ValidationError:
                return
            self.mappings.add(MappingTerm(name, _MATCHES[p], target))
            self.use(s, p, o)
        elif p == RDFS.subClassOf:
            self.use(s, p, o)
            if isinstance(o, BNode):
                closure = self.closure(o)
                self.used.update(closure)
                if not self.restriction(name, o):
                    self.class_axioms += 1
            elif parent is None or o != self.concept_nodes[parent]:
                self.class_axioms += 1
        elif p in _ANNOTATIONS:
            self.annotate(name, s, p, o)
        elif isinstance(p, URIRef) and (
            p in (OM.relatedTo, SKOS.related) or not _vocabulary(str(p))
        ):
            target = self.concept_of(o)
            if target is not None:
                self.use(s, p, o)
                if target != name:
                    self.links.add(frozenset((name, target)))

    def annotate(self, name: str, s: URIRef, p: Node, o: Node) -> None:
        """Consume the editorial annotations Lakeloom records per concept (history, scope,
        deprecation, change note) instead of reporting them as unrepresentable triples."""
        if p in (SKOS.historyNote, SKOS.scopeNote):
            if isinstance(o, Literal) and str(o).strip():
                bucket = self.history if p == SKOS.historyNote else self.scope
                bucket.setdefault(name, []).append(str(o))
            self.use(s, p, o)
        elif p == SKOS.changeNote:
            if isinstance(o, Literal) and str(o).strip():
                self.deprecation_reason[name] = str(o)
            self.use(s, p, o)
        elif p == OWL.deprecated:
            deprecated = isinstance(o, Literal) and str(o).strip().lower() in {"true", "1"}
            if deprecated:
                self.deprecated[name] = True
                self.deprecation_reason.setdefault(name, "")
            self.use(s, p, o)

    def closure(self, node: BNode) -> set[Triple]:
        out: set[Triple] = set()
        pending: list[Node] = [node]
        seen: set[Node] = set()
        while pending:
            current = pending.pop()
            if current in seen or len(seen) > _MAX_LIST_NODES:
                continue
            seen.add(current)
            for p, o in self.g.predicate_objects(current):
                out.add((current, p, o))
                if isinstance(o, BNode):
                    pending.append(o)
        return out

    def restriction(self, concept: str, node: BNode) -> bool:
        """Record a cardinality restriction on an attribute of ``concept``; False otherwise."""
        props = list(self.g.objects(node, OWL.onProperty))
        cards = [(p, o) for p, o in self.g.predicate_objects(node) if p in _CARDINALITIES]
        allowed = {RDF.type, OWL.onProperty, OWL.onDataRange, OWL.onClass, *_CARDINALITIES}
        if (
            set(self.g.objects(node, RDF.type)) != {OWL.Restriction}
            or len(props) != 1
            or len(cards) != 1
            or any(p not in allowed for p, _ in self.g.predicate_objects(node))
        ):
            return False
        member = self.attribute_nodes.get(props[0])
        predicate, value = cards[0]
        n = value.toPython() if isinstance(value, Literal) else None
        if member is None or member[0] != concept or not isinstance(n, int) or n < 0:
            return False
        if isinstance(n, bool):
            return False
        kind = _CARDINALITIES[predicate]
        self.restrictions.setdefault(concept, set()).add((member[1], kind, n))
        return True

    def axioms(self) -> dict[str, AxiomTerm]:
        out: dict[str, AxiomTerm] = {}
        for concept, found in self.restrictions.items():
            expressions = tuple(sorted(found))
            primary = all(
                kind == "EXACT" and n == 1 and self.attributes[(concept, prop)].is_identifier
                for prop, kind, n in expressions
            )
            suffix = "PrimaryKey" if primary else "Restrictions"
            name = f"{pascal_name(concept) or 'Concept'}{suffix}"
            out[concept] = AxiomTerm(concept, name, expressions)
        return out

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.invalid:
            n = len(self.invalid)
            out.append(f"{n} terms with invalid names are not imported: {_listed(self.invalid)}")
        if self.foreign:
            out.append(
                f"{len(self.foreign)} subjects outside {self.ns} are not imported: "
                f"{_listed(self.foreign)}"
            )
        if self.unknown:
            out.append(
                f"{len(self.unknown)} attributes and relations of undeclared concepts are not "
                f"imported: {_listed(self.unknown)}"
            )
        out.extend(f"label of {member} not imported" for member in sorted(self.labels))
        out.extend(f"{member} has no range; read as xsd:string" for member in sorted(self.no_range))
        explained = {frozenset((r.source, r.target)) for r in self.relations.values()}
        glossary_only = self.links - explained
        if glossary_only:
            out.append(f"{len(glossary_only)} glossary-only links are not imported")
        if self.class_axioms:
            out.append(f"{self.class_axioms} class axioms not representable")
        left: list[Triple] = [
            (s, p, o)
            for s, p, o in self.g
            if isinstance(s, URIRef) and s not in self.skipped and (s, p, o) not in self.used
        ]
        if left:
            predicates = {self.curie(p) for _, p, _ in left}
            out.append(f"{len(left)} triples not representable: {_listed(predicates)}")
        return out


def extract_terms(graph: Graph, namespace: str) -> TermSet:
    """The model terms of ``graph`` under ``namespace``, keyed by local name.

    See the plan's "Vocabulary read by extract_terms" for what is read; the rest is reported
    in the warnings (invalid names, foreign subjects, glossary-only links, class axioms and
    triples that are not representable, with up to 10 CURIEs each).
    """
    reader = _Reader(graph, namespace)
    _, attributes, relations = reader.classify()
    for s in attributes:
        reader.attribute(s)
    for s in relations:
        reader.relation(s)
    for name, s in sorted(reader.concept_nodes.items()):
        reader.concept(name, s)
    return TermSet(
        concepts=reader.concepts,
        attributes=reader.attributes,
        relations=reader.relations,
        mappings=reader.mappings,
        axioms=reader.axioms(),
        warnings=reader.warnings(),
        namespace=namespace,
    )
