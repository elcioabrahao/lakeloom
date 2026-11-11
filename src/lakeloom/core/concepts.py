"""Concepts of the ontology models: search, explanation and realizing assets.

Concepts come from the cached RDF graphs of the models (OpenMetadata glossaries); the
vocabulary Lakeloom reads from OM's export is recorded in ADR 0009. Realizations (assets
tagged with a term) come from OM's term-asset list, which also returns the *parent table*
of a tagged column and the columns that merely inherit a table-level tag (``Derived``).
Only direct (non-Derived) glossary tags count as realizations; a column realization is
reported as its table FQN plus ``column`` (FR-CTX-03, FR-CTX-04, FR-CTX-06).
"""

import asyncio
import base64
import binascii
import difflib
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal
from weakref import WeakKeyDictionary

from pydantic import BaseModel
from rdflib import BNode, Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from lakeloom.core.models import ModelService
from lakeloom.core.tokens import truncate_markdown
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMError, OMUnavailable
from lakeloom.om.models import OMEntityRef, OMTable
from lakeloom.om.native_rdf import QUDT_UNIT
from lakeloom.sources.models import Page
from lakeloom.untrusted import UntrustedText, code_span, quote_untrusted

__all__ = [
    "MORE_ASSETS_LINE",
    "AssetPage",
    "AssetRef",
    "AttributeInfo",
    "ConceptBundle",
    "ConceptDetail",
    "ConceptMatch",
    "ConceptRef",
    "ConceptSummary",
    "MappingInfo",
    "RelationInfo",
    "asset_line",
    "concept_bundles",
    "concept_markdown",
    "direct_glossary_tags",
    "explain_concept",
    "fetch_tables",
    "find_assets_for_concept",
    "find_concepts",
    "realizing_tables",
    "resolve_concept",
    "score_candidates",
    "summarize_concept",
]

OM_FQN = URIRef("https://open-metadata.org/ontology/fullyQualifiedName")
_MATCH_PREDICATES = (
    SKOS.exactMatch,
    SKOS.closeMatch,
    SKOS.broadMatch,
    SKOS.narrowMatch,
    SKOS.relatedMatch,
)
_MAX_DEPTH = 10
_ASSET_PAGE = 100
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_FIND_HINT = "use find_concepts to look up the concept's IRI or FQN"
_FUZZY_MIN = 0.6
_EXACT_MIN = 0.9  # name, synonym or local-name match: what a bare name resolves to
_MAX_CANDIDATES = 10
_ASSET_CHUNK = 25
_CACHE_MAX = 1024
_NEGATIVE_TTL = 10.0  # seconds a "not in OM" lookup is remembered
_CONCURRENCY = 8
MAX_DESCENDANT_TERMS = 200
MAX_ASSETS_EXAMINED = 2000
MORE_ASSETS_LINE = "- _more assets: call find_assets_for_concept_"
_ARROWS = {"out": "->", "in": "<-", "both": "<->"}


class ConceptRef(BaseModel):
    iri: str | None
    fqn: str
    model: str
    label: str


class ConceptMatch(BaseModel):
    concept: ConceptRef
    score: float
    matched_on: Literal["fqn", "name", "synonym", "local_name", "iri", "fuzzy"]


class RelationInfo(BaseModel):
    type: str
    target: ConceptRef
    direction: Literal["out", "in", "both"]


class AttributeInfo(BaseModel):
    name: str
    iri: str | None = None
    datatype: str | None = None
    unit: str | None = None
    allowed_values: list[str] = []


class MappingInfo(BaseModel):
    kind: str
    target_iri: str


class AssetRef(BaseModel):
    fqn: str
    type: str
    column: str | None = None


class AssetPage(Page[AssetRef]):
    """Assets realizing a concept; ``truncated`` is set when a descendant or asset cap hit."""

    truncated: bool = False


class ConceptSummary(BaseModel):
    """The slice of a concept an asset context embeds (Task 6)."""

    definition: UntrustedText | None = None
    synonyms: list[UntrustedText] = []
    iri: str | None = None
    parents: list[ConceptRef] = []


class ConceptDetail(BaseModel):
    concept: ConceptRef
    definition: UntrustedText | None = None
    synonyms: list[UntrustedText] = []
    parents: list[ConceptRef] = []
    children: list[ConceptRef] = []
    relations: list[RelationInfo] = []
    attributes: list[AttributeInfo] = []
    mappings: list[MappingInfo] = []
    realizations: Page[AssetRef]


# --- graph access -------------------------------------------------------------------------


def _local_name(iri: str) -> str:
    return re.split(r"[/#]", iri.rstrip("/#"))[-1]


def _pick(values: Iterable[RdfLiteral]) -> str | None:
    """Prefer an untagged or English literal, then the smallest, for determinism."""
    items = sorted(values, key=lambda v: (v.language not in (None, "en"), v.language or "", str(v)))
    return str(items[0]) if items else None


@dataclass
class _Graph:
    slug: str
    graph: Graph
    concepts: dict[URIRef, ConceptRef] = field(default_factory=dict[URIRef, ConceptRef])
    by_fqn: dict[str, URIRef] = field(default_factory=dict[str, URIRef])
    synonyms: dict[URIRef, set[str]] = field(default_factory=dict[URIRef, set[str]])

    def ref(self, node: URIRef) -> ConceptRef | None:
        return self.concepts.get(node)


def _index(slug: str, graph: Graph) -> _Graph:
    out = _Graph(slug, graph)
    for node in graph.subjects(RDF.type, SKOS.Concept):
        if not isinstance(node, URIRef):
            continue
        iri = str(node)
        fqn_lit = graph.value(node, OM_FQN)
        fqn = str(fqn_lit) if fqn_lit is not None else f"{slug}.{_local_name(iri)}"
        labels = [o for o in graph.objects(node, SKOS.prefLabel) if isinstance(o, RdfLiteral)]
        labels += [o for o in graph.objects(node, RDFS.label) if isinstance(o, RdfLiteral)]
        label = _pick(labels) or _local_name(iri)
        out.concepts[node] = ConceptRef(iri=iri, fqn=fqn, model=slug, label=label)
        out.by_fqn[fqn] = node
        out.synonyms[node] = {str(o) for o in graph.objects(node, SKOS.altLabel)}
    return out


def _concept_objects(g: _Graph, node: URIRef, preds: Iterable[URIRef]) -> set[URIRef]:
    found: set[URIRef] = set()
    for pred in preds:
        found.update(o for o in g.graph.objects(node, pred) if isinstance(o, URIRef))
    return {n for n in found if n in g.concepts}


def _inverse_subjects(g: _Graph, node: URIRef, preds: Iterable[URIRef]) -> set[URIRef]:
    found: set[URIRef] = set()
    for pred in preds:
        found.update(s for s in g.graph.subjects(pred, node) if isinstance(s, URIRef))
    return {n for n in found if n in g.concepts}


def _parents(g: _Graph, node: URIRef) -> set[URIRef]:
    return _concept_objects(g, node, (SKOS.broader, RDFS.subClassOf)) | _inverse_subjects(
        g, node, (SKOS.narrower,)
    )


def _children(g: _Graph, node: URIRef) -> set[URIRef]:
    return _concept_objects(g, node, (SKOS.narrower,)) | _inverse_subjects(
        g, node, (SKOS.broader, RDFS.subClassOf)
    )


def _sorted_refs(g: _Graph, nodes: Iterable[URIRef]) -> list[ConceptRef]:
    refs = [g.concepts[n] for n in nodes]
    return sorted(refs, key=lambda r: (r.label, r.fqn))


def _descendants(g: _Graph, node: URIRef) -> list[URIRef]:
    """Breadth-first, depth-bounded, cycle-safe; the start node is not included."""
    seen = {node}
    order: list[URIRef] = []
    frontier = [node]
    for _ in range(_MAX_DEPTH):
        nxt: list[URIRef] = []
        for current in frontier:
            for child in sorted(_children(g, current), key=str):
                if child not in seen:
                    seen.add(child)
                    order.append(child)
                    nxt.append(child)
        frontier = nxt
        if not frontier:
            break
    return order


def _relations(g: _Graph, node: URIRef) -> list[RelationInfo]:
    """Typed links between concepts through an ``owl:ObjectProperty`` predicate.

    OM rc1 stores each relation on both terms and exports it both ways with no direction,
    so the two stored directions collapse into one ``both`` relation (ADR 0009).
    """
    props = {p for p in g.graph.subjects(RDF.type, OWL.ObjectProperty) if isinstance(p, URIRef)}
    props -= {p for p in props if str(p).startswith(str(SKOS))}
    found: dict[tuple[str, URIRef], None] = {}
    for p in props:
        for o in _concept_objects(g, node, (p,)):
            found[(_local_name(str(p)), o)] = None
        for s in _inverse_subjects(g, node, (p,)):
            found[(_local_name(str(p)), s)] = None
    return [
        RelationInfo(type=t, target=g.concepts[target], direction="both")
        for (t, target) in sorted(found, key=lambda k: (k[0], g.concepts[k[1]].label, str(k[1])))
    ]


def _one_of(graph: Graph, owner: object) -> list[str]:
    values: list[str] = []
    for head in graph.objects(owner, OWL.oneOf):  # pyright: ignore[reportArgumentType]
        if isinstance(head, (BNode, URIRef)):
            values += [str(v) for v in Collection(graph, head)]
    return values


def _attributes(g: _Graph, node: URIRef) -> list[AttributeInfo]:
    out: list[AttributeInfo] = []
    for prop in sorted(
        (p for p in g.graph.subjects(RDFS.domain, node) if isinstance(p, URIRef)), key=str
    ):
        if (prop, RDF.type, OWL.DatatypeProperty) not in g.graph:
            continue
        label = _pick(o for o in g.graph.objects(prop, RDFS.label) if isinstance(o, RdfLiteral))
        range_ = g.graph.value(prop, RDFS.range)
        allowed = _one_of(g.graph, prop)
        if isinstance(range_, BNode):
            allowed += _one_of(g.graph, range_)
        unit = g.graph.value(prop, QUDT_UNIT)
        out.append(
            AttributeInfo(
                name=label or _local_name(str(prop)),
                iri=str(prop),
                datatype=str(range_) if isinstance(range_, URIRef) else None,
                unit=str(unit) if unit is not None else None,
                allowed_values=sorted(set(allowed)),
            )
        )
    return sorted(out, key=lambda a: (a.name, a.iri or ""))


def _mappings(g: _Graph, node: URIRef) -> list[MappingInfo]:
    out = [
        MappingInfo(kind=_local_name(str(p)), target_iri=str(o))
        for p in _MATCH_PREDICATES
        for o in g.graph.objects(node, p)
        if isinstance(o, URIRef)
    ]
    return sorted(out, key=lambda m: (m.kind, m.target_iri))


def _definition(g: _Graph, node: URIRef) -> UntrustedText | None:
    text = _pick(o for o in g.graph.objects(node, SKOS.definition) if isinstance(o, RdfLiteral))
    if text is None:
        return None
    return UntrustedText(text=text, source=f"OpenMetadata glossary term {g.concepts[node].fqn}")


def _synonyms(g: _Graph, node: URIRef) -> list[UntrustedText]:
    source = f"OpenMetadata glossary term {g.concepts[node].fqn}"
    texts = sorted({str(o) for o in g.graph.objects(node, SKOS.altLabel)})
    return [UntrustedText(text=t, source=source) for t in texts]


# --- service plumbing ---------------------------------------------------------------------


def _upstream(exc: OMError) -> LakeloomError:
    if isinstance(exc, OMUnavailable):
        return LakeloomError(
            "upstream_unavailable",
            "OpenMetadata is unavailable",
            hint="retry shortly; check that OpenMetadata is running",
        )
    if exc.status == 404:
        return LakeloomError("not_found", "Concept not found in OpenMetadata", _FIND_HINT)
    return LakeloomError(
        "upstream_unavailable",
        "OpenMetadata could not serve the concept",
        hint="retry shortly; check the OpenMetadata logs",
    )


def _encode_offset(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_offset(cursor: str | None, tool: str) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.b64decode(cursor.replace("-", "+").replace("_", "/"), validate=True)
        offset = int(raw.decode())
        if (
            base64.urlsafe_b64encode(raw).decode() != cursor
            or offset < 0
            or str(offset) != raw.decode()
        ):
            raise ValueError(cursor)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise LakeloomError(
            "validation_error",
            "Invalid cursor",
            hint=f"pass the next_cursor from a previous {tool} call, or omit it",
        ) from exc
    return offset


async def _all_slugs(models: ModelService) -> list[str]:
    slugs: list[str] = []
    cursor: str | None = None
    while True:
        page = await models.list_models(cursor, 100)
        slugs += [m.slug for m in page.items]
        cursor = page.next_cursor
        if cursor is None:
            return slugs


async def _graph(models: ModelService, slug: str) -> _Graph:
    """The model's concept index, built once per loaded graph (NFR-PERF-01)."""
    return await models.derived(slug, "concepts.index", lambda graph: _index(slug, graph))


@dataclass
class _Resolved:
    g: _Graph
    node: URIRef
    term_id: str | None = None

    @property
    def ref(self) -> ConceptRef:
        return self.g.concepts[self.node]


def _not_found(concept: str) -> LakeloomError:
    return LakeloomError("not_found", f"Unknown concept {concept!r}", _FIND_HINT)


async def _resolve_by_name(
    models: ModelService, concept: str, within: Sequence[str] | None = None
) -> _Resolved:
    """A plain name: the highest-ranked tier of matches (name > synonym > local name) resolves
    when it holds exactly one concept; otherwise the candidates are listed. ``within`` limits
    the search to those models."""
    hits: list[tuple[float, _Graph, URIRef]] = []
    for slug in await _all_slugs(models) if within is None else within:
        try:
            g = await _graph(models, slug)
        except LakeloomError as exc:
            if exc.code == "not_found":
                continue  # deleted between listing and loading
            raise
        for node, ref in g.concepts.items():
            scored = _score(concept, _Candidate(ref, g.synonyms[node]))
            if scored is not None and scored[0] >= _EXACT_MIN:
                hits.append((scored[0], g, node))
    if not hits:
        raise _not_found(concept)
    # Rank: exact name (1.0) > synonym (0.95) > local name (0.9); the top tier must be unique.
    top = max(score for score, _, _ in hits)
    hits = [h for h in hits if h[0] == top]
    if len(hits) == 1:
        return _Resolved(hits[0][1], hits[0][2])
    fqns = sorted(g.concepts[n].fqn for _, g, n in hits)
    shown = ", ".join(f"`{f}`" for f in fqns[:_MAX_CANDIDATES])
    more = f" and {len(fqns) - _MAX_CANDIDATES} more" if len(fqns) > _MAX_CANDIDATES else ""
    raise LakeloomError(
        "validation_error",
        f"Ambiguous concept name {concept!r}: {len(fqns)} concepts match",
        f"pass one FQN: {shown}{more}",
    )


async def resolve_concept(
    models: ModelService, om: OMClient, concept: str, *, within: Sequence[str]
) -> ConceptRef:
    """Resolve ``concept`` (IRI, term UUID, FQN or name) to a concept of one of the models
    ``within``; no assets are read. A concept of another model is a ``validation_error``."""
    ref = (await _resolve(models, om, concept, within)).ref
    if ref.model not in within:
        allowed = ", ".join(f"`{w}`" for w in within)
        raise LakeloomError(
            "validation_error",
            f"concept {ref.fqn!r} is in model {ref.model!r}, not in {within[0]!r} or a model "
            "it imports",
            f"use a concept of {allowed}",
        )
    return ref


async def _resolve(
    models: ModelService, om: OMClient, concept: str, within: Sequence[str] | None = None
) -> _Resolved:
    """Resolve an IRI, a term UUID, a term FQN or a plain name (FR-CTX-03); a plain name or
    IRI is looked up only ``within`` those models when given."""
    concept = concept.strip()
    slugs = within
    if "://" in concept or concept.startswith("urn:"):
        node = URIRef(concept)
        for slug in await _all_slugs(models) if slugs is None else slugs:
            g = await _graph(models, slug)
            if node in g.concepts:
                return _Resolved(g, node)
        raise _not_found(concept)
    if _UUID.match(concept):
        try:
            term = await om.get_term(concept, ())
        except OMError as exc:
            raise _upstream(exc) from exc
        if term is None:
            raise _not_found(concept)
        found = await _by_fqn(models, term.fully_qualified_name)
        if found is None:
            raise _not_found(concept)
        return _Resolved(found[0], found[1], term.id)
    if "." in concept:  # a term FQN is always <glossary>.<term>[...]
        found = await _by_fqn(models, concept)
        if found is not None:
            return _Resolved(*found)
    return await _resolve_by_name(models, concept, slugs)


async def _by_fqn(models: ModelService, fqn: str) -> tuple[_Graph, URIRef] | None:
    try:
        g = await _graph(models, fqn.split(".", 1)[0])
    except LakeloomError as exc:
        if exc.code == "not_found":
            return None
        raise
    node = g.by_fqn.get(fqn)
    return None if node is None else (g, node)


# --- find_concepts ------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[\s_\-.]+", "", text).casefold()


@dataclass
class _Candidate:
    ref: ConceptRef
    synonyms: set[str]


def _score(query: str, cand: _Candidate) -> tuple[float, str] | None:
    ref = cand.ref
    if ref.iri is not None and query == ref.iri:
        return 1.0, "iri"
    q = query.casefold()
    if q == ref.fqn.casefold():
        return 1.0, "fqn"
    if q == ref.label.casefold():
        return 1.0, "name"
    if any(q == s.casefold() for s in cand.synonyms):
        return 0.95, "synonym"
    nq = _norm(query)
    local = _local_name(ref.iri) if ref.iri else ref.fqn.rsplit(".", 1)[-1]
    if nq and nq in {_norm(local), _norm(ref.label)}:
        return 0.9, "local_name"
    names = {_norm(local), _norm(ref.label), *(_norm(s) for s in cand.synonyms)}
    best = max((difflib.SequenceMatcher(None, nq, n).ratio() for n in names if n), default=0.0)
    if best >= _FUZZY_MIN:
        return round(best * 0.8, 6), "fuzzy"
    return None


async def find_concepts(
    models: ModelService,
    om: OMClient,
    query: str,
    *,
    model: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> Page[ConceptMatch]:
    offset = _decode_offset(cursor, "find_concepts")
    query = query.strip()
    if not query:
        raise LakeloomError(
            "validation_error", "Empty query", hint="pass a concept name, synonym, FQN or IRI"
        )
    if model is not None:
        await models.get_model(model)  # unknown -> not_found (hint: list_models)
        slugs = [model]
    else:
        slugs = await _all_slugs(models)
    cands: dict[str, _Candidate] = {}
    for slug in slugs:
        g = await _graph(models, slug)
        for node, ref in g.concepts.items():
            cands[ref.fqn] = _Candidate(ref, g.synonyms[node])
    try:
        hits = await om.search_terms(query, glossary_fqn=model, limit=limit)
    except OMError:
        hits = []  # search is a supplement; the model graphs already answered
    for term in hits:
        slug = term.glossary.name or term.fully_qualified_name.split(".", 1)[0]
        if term.fully_qualified_name in cands or (model is not None and slug != model):
            continue
        ref = ConceptRef(
            iri=term.iri,
            fqn=term.fully_qualified_name,
            model=slug,
            label=term.display_name or term.name,
        )
        cands[ref.fqn] = _Candidate(ref, set(term.synonyms))
    matches: list[ConceptMatch] = []
    for cand in cands.values():
        scored = _score(query, cand)
        if scored is not None:
            score, matched_on = scored
            matches.append(
                ConceptMatch(
                    concept=cand.ref,
                    score=score,
                    matched_on=matched_on,  # pyright: ignore[reportArgumentType]
                )
            )
    matches.sort(key=lambda m: (-m.score, m.concept.label, m.concept.fqn))
    window = matches[offset : offset + limit]
    more = offset + limit < len(matches)
    return Page[ConceptMatch](
        items=window, next_cursor=_encode_offset(offset + limit) if more else None
    )


async def score_candidates(
    models: ModelService, query: str, *, model: str | Sequence[str] | None = None
) -> list[ConceptMatch]:
    """Score the concepts of ``model`` (one slug, several, or all models) against ``query``.

    The same tiered scorer as :func:`find_concepts` (IRI/FQN/name 1.0, synonym 0.95, local
    name 0.9, fuzzy up to 0.8), without the OM search supplement. Best first, ties by label
    and FQN, so the result is deterministic.
    """
    if model is None:
        slugs = await _all_slugs(models)
    else:
        slugs = [model] if isinstance(model, str) else list(model)
    matches: list[ConceptMatch] = []
    for slug in slugs:
        g = await _graph(models, slug)
        for node, ref in g.concepts.items():
            scored = _score(query, _Candidate(ref, g.synonyms[node]))
            if scored is not None:
                matches.append(
                    ConceptMatch(
                        concept=ref,
                        score=scored[0],
                        matched_on=scored[1],  # pyright: ignore[reportArgumentType]
                    )
                )
    matches.sort(key=lambda m: (-m.score, m.concept.label, m.concept.fqn))
    return matches


# --- realizations -------------------------------------------------------------------------


def direct_glossary_tags(tags: Iterable[object], *, column: bool) -> list[str]:
    """Sorted term FQNs an asset realizes directly: Glossary tags not ``Derived``.

    One rule for tables and columns (ADR 0009): OM propagates table-level glossary tags to
    columns as ``Derived``, and a ``Derived`` table tag comes from elsewhere (a parent or a
    data product), so neither counts as a realization. ``column`` names the asset kind for
    the caller's clarity; the rule is the same for both.
    """
    return sorted(
        {
            fqn
            for t in tags
            if isinstance(fqn := getattr(t, "tag_fqn", None), str)
            and getattr(t, "source", None) == "Glossary"
            and getattr(t, "label_type", None) != "Derived"
        }
    )


def _direct(tags: Iterable[object], term_fqn: str, *, column: bool) -> bool:
    return term_fqn in direct_glossary_tags(tags, column=column)


class _Cache:
    """Per-ModelService caches with the model TTL: OM tables and per-term realizations."""

    def __init__(self) -> None:
        self.tables: OrderedDict[str, tuple[float, OMTable | None]] = OrderedDict()
        self.lists: OrderedDict[str, tuple[float, tuple[list[AssetRef], int]]] = OrderedDict()
        self.ttl = 0.0
        self.now: Callable[[], float] = time.monotonic

    def _get[V](self, store: OrderedDict[str, tuple[float, V]], key: str) -> tuple[bool, V | None]:
        hit = store.get(key)
        ttl = min(self.ttl, _NEGATIVE_TTL) if hit is not None and hit[1] is None else self.ttl
        if hit is None or ttl <= 0 or self.now() - hit[0] >= ttl:
            store.pop(key, None)
            return False, None
        store.move_to_end(key)
        return True, hit[1]

    def invalidate(self, slug: str | None) -> None:
        """Drop realizations of ``slug``'s terms (all when ``None``) and every cached table:
        tables are not per model, and their tags may have changed in OM."""
        self.tables.clear()
        if slug is None:
            self.lists.clear()
        else:
            for key in [k for k in self.lists if k.split(".", 1)[0] == slug]:
                del self.lists[key]

    def _put[V](self, store: OrderedDict[str, tuple[float, V]], key: str, value: V) -> None:
        if self.ttl <= 0:
            return
        store[key] = (self.now(), value)
        store.move_to_end(key)
        while len(store) > _CACHE_MAX:
            store.popitem(last=False)


_caches: WeakKeyDictionary[ModelService, _Cache] = WeakKeyDictionary()


def _cache_for(models: ModelService) -> _Cache:
    cache = _caches.get(models)
    if cache is None:
        cache = _caches[models] = _Cache()
        models.on_invalidate(cache.invalidate)
    cache.ttl = models.cache_ttl
    cache.now = models.now
    return cache


async def _fetch_tables(om: OMClient, cache: _Cache, fqns: list[str]) -> dict[str, OMTable | None]:
    found: dict[str, OMTable | None] = {}
    missing: list[str] = []
    for fqn in dict.fromkeys(fqns):
        hit, table = cache._get(cache.tables, fqn)  # pyright: ignore[reportPrivateUsage]
        if hit:
            found[fqn] = table
        else:
            missing.append(fqn)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(fqn: str) -> tuple[str, OMTable | None]:
        async with sem:
            try:
                return fqn, await om.get_table_by_fqn(fqn)
            except OMError as exc:
                raise _upstream(exc) from exc

    for fqn, table in await asyncio.gather(*(one(f) for f in missing)):
        found[fqn] = table
        cache._put(cache.tables, fqn, table)  # pyright: ignore[reportPrivateUsage]
    return found


_COLUMN_TYPES = {"tablecolumn", "column"}


async def _realizing(
    om: OMClient, cache: _Cache, term_fqn: str, refs: list[OMEntityRef]
) -> list[AssetRef]:
    plan: list[tuple[str, str, str | None, bool]] = []  # (fqn, table_fqn, column, is_column)
    for ref in refs:
        fqn = ref.fully_qualified_name
        if not fqn:
            continue
        kind = (ref.type or "").lower()
        is_column = kind in _COLUMN_TYPES
        column = (ref.name or fqn.rsplit(".", 1)[-1]) if is_column else None
        table_fqn = fqn
        if column is not None:
            suffix = f".{column}"
            table_fqn = fqn[: -len(suffix)] if fqn.endswith(suffix) else fqn.rsplit(".", 1)[0]
        plan.append((fqn, table_fqn, column, is_column))
    kinds = {r.fully_qualified_name: (r.type or "").lower() for r in refs}
    tables = await _fetch_tables(
        om,
        cache,
        [t for f, t, _, c in plan if c or kinds[f] == "table"],
    )
    out: list[AssetRef] = []
    for fqn, table_fqn, column, is_column in plan:
        if not is_column and kinds[fqn] != "table":
            out.append(AssetRef(fqn=fqn, type=kinds[fqn] or "unknown"))
            continue
        table = tables[table_fqn]
        if table is not None:
            if is_column:
                col = next((c for c in table.columns if c.name == column), None)
                if col is None or not _direct(col.tags, term_fqn, column=True):
                    continue
            elif not _direct(table.tags, term_fqn, column=False):
                continue
        out.append(AssetRef(fqn=table_fqn, type="column" if is_column else "table", column=column))
    return out


async def _term_id(om: OMClient, res: _Resolved) -> str | None:
    if res.term_id is not None:
        return res.term_id
    try:
        term = await om.get_term(res.ref.fqn, ())
    except OMError as exc:
        raise _upstream(exc) from exc
    return term.id if term is not None else None


async def _assets_page(
    om: OMClient, cache: _Cache, res: _Resolved, offset: int, limit: int
) -> tuple[list[AssetRef], int | None, int]:
    """One OM page for a term -> (realizing assets, next OM offset, assets examined)."""
    term_id = await _term_id(om, res)
    if term_id is None:
        return [], None, 0
    try:
        page = await om.get_term_assets(term_id, offset=offset, limit=limit)
    except OMError as exc:
        raise _upstream(exc) from exc
    taken = offset + len(page.data)
    more = bool(page.data) and (
        page.after is not None or (page.total is not None and taken < page.total)
    )
    assets = await _realizing(om, cache, res.ref.fqn, page.data)
    return assets, taken if more else None, len(page.data)


async def _term_assets_all(
    om: OMClient, cache: _Cache, res: _Resolved, budget: int
) -> tuple[list[AssetRef], int, bool]:
    """Every realizing asset of one term -> (assets, examined, complete). Cached when complete."""
    hit, value = cache._get(cache.lists, res.ref.fqn)  # pyright: ignore[reportPrivateUsage]
    if hit and value is not None:
        return value[0], value[1], True
    out: list[AssetRef] = []
    examined = 0
    pos: int | None = 0
    while pos is not None:
        if examined >= budget:
            return out, examined, False
        assets, pos, n = await _assets_page(om, cache, res, pos, _ASSET_PAGE)
        out += assets
        examined += n
    cache._put(cache.lists, res.ref.fqn, (out, examined))  # pyright: ignore[reportPrivateUsage]
    return out, examined, True


# --- explain / assets ---------------------------------------------------------------------


async def explain_concept(models: ModelService, om: OMClient, concept: str) -> ConceptDetail:
    res = await _resolve(models, om, concept)
    g, node = res.g, res.node
    assets, nxt, _ = await _assets_page(om, _cache_for(models), res, 0, 50)
    return ConceptDetail(
        concept=res.ref,
        definition=_definition(g, node),
        synonyms=_synonyms(g, node),
        parents=_sorted_refs(g, _parents(g, node)),
        children=_sorted_refs(g, _children(g, node)),
        relations=_relations(g, node),
        attributes=_attributes(g, node),
        mappings=_mappings(g, node),
        realizations=Page[AssetRef](
            items=assets, next_cursor=_encode_offset(nxt) if nxt is not None else None
        ),
    )


async def summarize_concept(models: ModelService, om: OMClient, concept: str) -> ConceptSummary:
    res = await _resolve(models, om, concept)
    return ConceptSummary(
        definition=_definition(res.g, res.node),
        synonyms=_synonyms(res.g, res.node),
        iri=res.ref.iri,
        parents=_sorted_refs(res.g, _parents(res.g, res.node)),
    )


async def find_assets_for_concept(
    models: ModelService,
    om: OMClient,
    concept: str,
    *,
    include_descendants: bool = False,
    cursor: str | None = None,
    limit: int = 50,
) -> AssetPage:
    offset = _decode_offset(cursor, "find_assets_for_concept")
    res = await _resolve(models, om, concept)
    cache = _cache_for(models)
    if not include_descendants:
        assets, nxt, _ = await _assets_page(om, cache, res, offset, limit)
        return AssetPage(items=assets, next_cursor=_encode_offset(nxt) if nxt is not None else None)
    descendants = _descendants(res.g, res.node)
    truncated = len(descendants) > MAX_DESCENDANT_TERMS - 1
    nodes = [res.node, *descendants[: MAX_DESCENDANT_TERMS - 1]]
    merged: dict[tuple[str, str | None], AssetRef] = {}
    remaining = MAX_ASSETS_EXAMINED
    for start in range(0, len(nodes), _CONCURRENCY):
        if remaining <= 0:
            truncated = True
            break
        batch = [
            _Resolved(res.g, n, res.term_id if n == res.node else None)
            for n in nodes[start : start + _CONCURRENCY]
        ]
        results = await asyncio.gather(
            *(_term_assets_all(om, cache, sub, remaining) for sub in batch)
        )
        for assets, examined, complete in results:
            remaining -= examined
            truncated = truncated or not complete
            for a in assets:
                merged.setdefault((a.fqn, a.column), a)
    ordered = sorted(merged.values(), key=lambda a: (a.fqn, a.column or ""))
    window = ordered[offset : offset + limit]
    more = offset + limit < len(ordered)
    return AssetPage(
        items=window,
        next_cursor=_encode_offset(offset + limit) if more else None,
        truncated=truncated,
    )


# --- helpers for asset contexts (Task 6) ---------------------------------------------------


class ConceptBundle(BaseModel):
    """Everything an asset context embeds about one concept."""

    ref: ConceptRef
    summary: ConceptSummary
    attributes: list[AttributeInfo] = []
    relations: list[RelationInfo] = []


async def fetch_tables(
    models: ModelService, om: OMClient, fqns: list[str]
) -> dict[str, OMTable | None]:
    """OM tables by FQN through the per-service TTL cache (``None`` = not in OM)."""
    return await _fetch_tables(om, _cache_for(models), fqns)


async def concept_bundles(models: ModelService, fqns: Iterable[str]) -> dict[str, ConceptBundle]:
    """Bundles for the term FQNs known to a model graph; unknown FQNs are left out."""
    by_slug: dict[str, list[str]] = {}
    for fqn in dict.fromkeys(fqns):
        by_slug.setdefault(fqn.split(".", 1)[0], []).append(fqn)
    out: dict[str, ConceptBundle] = {}
    for slug, members in by_slug.items():
        try:
            g = await _graph(models, slug)
        except LakeloomError as exc:
            if exc.code == "not_found":
                continue  # a glossary that is not a model, or deleted meanwhile
            raise
        for fqn in members:
            node = g.by_fqn.get(fqn)
            if node is None:
                continue
            out[fqn] = ConceptBundle(
                ref=g.concepts[node],
                summary=ConceptSummary(
                    definition=_definition(g, node),
                    synonyms=_synonyms(g, node),
                    iri=g.concepts[node].iri,
                    parents=_sorted_refs(g, _parents(g, node)),
                ),
                attributes=_attributes(g, node),
                relations=_relations(g, node),
            )
    return out


async def realizing_tables(models: ModelService, om: OMClient, concept_fqn: str) -> list[str]:
    """Sorted FQNs of the tables realizing a concept directly (first OM page of assets).

    A realizing column contributes its table; other asset types (dashboards, topics...)
    are left out.
    """
    res = await _resolve(models, om, concept_fqn)
    assets, _, _ = await _term_assets_all(om, _cache_for(models), res, _ASSET_PAGE)
    return sorted({a.fqn for a in assets if a.type in ("table", "column")})


# --- Markdown -----------------------------------------------------------------------------


def _ref_line(label: str, ref: ConceptRef) -> str:
    return f"- {label}: {code_span(ref.label)} ({code_span(ref.fqn)})"


def asset_line(a: AssetRef) -> str:
    if a.column:
        return f"- {code_span(a.fqn)} (column {code_span(a.column)})"
    return f"- {code_span(a.fqn)} (type {code_span(a.type)})"


def concept_markdown(detail: ConceptDetail, *, max_tokens: int | None = None) -> tuple[str, bool]:
    c = detail.concept
    source = f"OpenMetadata glossary term {c.fqn}"
    head = [f"# {code_span(c.label)}", ""]
    if c.iri:
        head.append(f"- IRI: {code_span(c.iri)}")
    head += [f"- FQN: {code_span(c.fqn)}", f"- Model: {code_span(c.model)}"]
    sections = ["\n".join(head)]
    if detail.definition is not None:
        sections.append(
            "## Definition\n\n"
            + quote_untrusted(detail.definition.text, source=detail.definition.source)
        )
    if detail.synonyms:
        sections.append(
            "## Synonyms\n\n"
            + quote_untrusted("\n".join(s.text for s in detail.synonyms), source=source)
        )
    if detail.parents or detail.children:
        lines = [_ref_line("Parent", p) for p in detail.parents]
        lines += [_ref_line("Child", ch) for ch in detail.children]
        sections.append("## Hierarchy\n\n" + "\n".join(lines))
    if detail.relations:
        lines = [
            f"- {code_span(r.type)} {_ARROWS[r.direction]}"
            f" {code_span(r.target.label)} ({code_span(r.target.fqn)})"
            for r in detail.relations
        ]
        sections.append("## Relations\n\n" + "\n".join(lines))
    if detail.attributes:
        lines: list[str] = []
        for a in detail.attributes:
            extra = [f"datatype {code_span(a.datatype)}"] if a.datatype else []
            if a.unit:
                extra.append(f"unit {code_span(a.unit)}")
            if a.allowed_values:
                extra.append("one of " + ", ".join(code_span(v) for v in a.allowed_values))
            lines.append(f"- {code_span(a.name)}" + (f": {'; '.join(extra)}" if extra else ""))
        sections.append("## Attributes\n\n" + "\n".join(lines))
    if detail.mappings:
        lines = [f"- {code_span(m.kind)}: {code_span(m.target_iri)}" for m in detail.mappings]
        sections.append("## Mappings\n\n" + "\n".join(lines))
    items = detail.realizations.items
    if items:
        chunks = [items[i : i + _ASSET_CHUNK] for i in range(0, len(items), _ASSET_CHUNK)]
        for n, chunk in enumerate(chunks):
            body = "\n".join(asset_line(a) for a in chunk)
            if n == 0:
                body = "## Realizing assets\n\n" + body
            if n == len(chunks) - 1 and detail.realizations.next_cursor:
                body += "\n" + MORE_ASSETS_LINE
            sections.append(body)
    return truncate_markdown(sections, max_tokens)
