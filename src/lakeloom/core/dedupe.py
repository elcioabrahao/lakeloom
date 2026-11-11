"""Duplicate avoidance: reuse existing concepts instead of proposing new ones (FR-PRO-02).

Input is the mapper's operations. Output is the operations a proposal should really carry:

* an ``AddConcept`` whose name (or source table name, singular or plural) matches an existing
  concept of the target model or of a model it imports with confidence >= 0.9 is replaced by an
  ``AddRealization`` of that concept by the table's OM asset; operations that referred to the
  proposed concept are re-targeted to the existing one;
* a match of 0.6 to below 0.9 keeps the new concept and adds a warning naming the candidate;
* below 0.6 nothing is said;
* an attribute that already exists on the matched concept is dropped, and its column is
  realized instead (unless OM already tags it);
* an operation whose :func:`op_key` is in an open proposal is dropped with the proposal id;
* relations, mappings, axioms and realizations already present in the graph (or in OM tags)
  are dropped;
* an operation that refers to a concept which is neither in the model nor proposed here but
  pending in another open proposal (an incremental scan: ``customers`` proposed first, then
  ``customers`` + ``orders``) is dropped with the warning "depends on <Name> pending in
  proposal <id>; propose again after it is applied" — OM has no term to point at yet.

:func:`screen` applies the exact checks (pending op keys of other proposals, existing concept
and attribute names) to operations an agent passes to ``propose_change``.

Operations that would point at a concept of an *imported* model cannot be applied in M2a (the
operation mapping only knows concepts of its own model), so they are dropped with a warning
rather than emitted. Realizations may point at imported concepts: they are Lakeloom-side.

No synonym is added when a match was found through a synonym or a different label: the
existing concept already answers to that name. Nothing is written to OM.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from lakeloom.core.concepts import (
    OM_FQN,
    ConceptMatch,
    direct_glossary_tags,
    fetch_tables,
    score_candidates,
)
from lakeloom.core.mapper import singularize
from lakeloom.core.models import ModelService
from lakeloom.core.operations import (
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddMapping,
    AddRealization,
    AddRelation,
    AddSynonym,
    ProposalOperation,
    op_key,
)
from lakeloom.om.client import OMClient
from lakeloom.om.models import OMTable

__all__ = ["DedupeResult", "Match", "dedupe", "import_slugs", "screen"]

REALIZE_MIN = 0.9
WARN_MIN = 0.6


@dataclass(frozen=True)
class Match:
    """An existing concept an ``AddConcept`` resembles (``action``: realized or kept)."""

    op_key: str
    candidate_fqn: str
    confidence: float
    matched_on: str
    action: str = "kept"


@dataclass
class DedupeResult:
    operations: list[ProposalOperation] = field(default_factory=list[ProposalOperation])
    matches: list[Match] = field(default_factory=list[Match])
    warnings: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class _Target:
    """The existing concept an ``AddConcept`` was folded into."""

    fqn: str
    model: str
    node: URIRef | None

    def ref(self, model: str) -> str:
        """How operations of ``model`` refer to it: bare name locally, FQN when imported."""
        return self.fqn.rsplit(".", 1)[-1] if self.model == model else self.fqn


def _norm(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _local(iri: str) -> str:
    return iri.rstrip("/#").replace("#", "/").rsplit("/", 1)[-1]


def _names(graph: Graph, node: object) -> set[str]:
    local = _local(str(node))
    # native members are ``{base}{Concept}.{name}``: the part after the last dot is the name
    out = {_norm(local), _norm(local.rsplit(".", 1)[-1])}
    out |= {_norm(str(o)) for o in graph.objects(node, RDFS.label)}  # pyright: ignore[reportArgumentType]
    return out


def _attribute_names(graph: Graph, node: URIRef) -> set[str]:
    out: set[str] = set()
    for prop in graph.subjects(RDFS.domain, node):
        if isinstance(prop, URIRef) and (prop, RDF.type, OWL.DatatypeProperty) in graph:
            out |= _names(graph, prop)
    return out


def _node_of(graph: Graph, model: str, ref: str) -> URIRef | None:
    """The concept node a ref (bare name or FQN of ``model``) names."""
    name = ref.rsplit(".", 1)[-1]
    found: list[URIRef] = []
    for node in graph.subjects(RDF.type, SKOS.Concept):
        if not isinstance(node, URIRef):
            continue
        fqn = graph.value(node, OM_FQN)
        if ref == str(fqn) or f"{model}.{name}" == str(fqn):
            return node
        labels = {str(o) for o in graph.objects(node, SKOS.prefLabel)}
        if name in labels or _local(str(node)) == name:
            found.append(node)
    return sorted(found, key=str)[0] if found else None


def _relation_exists(graph: Graph, source: URIRef, kind: str, target: URIRef) -> bool:
    wanted = _norm(kind)
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        if not isinstance(prop, URIRef) or str(prop).startswith(str(SKOS)):
            continue
        if wanted not in _names(graph, prop):
            continue
        if (source, prop, target) in graph or (target, prop, source) in graph:
            return True
        # hybrid model: a relation is also a native object property (domain -> range)
        if (prop, RDFS.domain, source) in graph and (prop, RDFS.range, target) in graph:
            return True
    return False


def _restricted(graph: Graph, node: URIRef) -> set[str]:
    """Normalised names of the properties ``node`` carries restrictions on."""
    out: set[str] = set()
    for restriction in graph.objects(node, RDFS.subClassOf):
        if isinstance(restriction, BNode):
            for prop in graph.objects(restriction, OWL.onProperty):
                out |= _names(graph, prop)
    return out


def _axiom_exists(graph: Graph, node: URIRef, op: AddAxiom) -> bool:
    expressions = op.axiom.get("expressions")
    if not isinstance(expressions, list) or not expressions:
        return False
    have = _restricted(graph, node)
    for expr in cast("list[Any]", expressions):
        if not isinstance(expr, dict):
            return False
        name = cast("dict[str, Any]", expr).get("propertyName")
        if not isinstance(name, str) or _norm(name) not in have:
            return False
    return True


def _mapping_exists(graph: Graph, node: URIRef, op: AddMapping) -> bool:
    return (node, SKOS[op.kind], URIRef(str(op.target_iri))) in graph


async def import_slugs(models: ModelService, model: str) -> list[str]:
    """Slugs of the models ``model`` imports (``owl:imports`` of their base IRIs), sorted."""
    return await _import_slugs(models, model, await models.graph(model))


async def _import_slugs(models: ModelService, model: str, graph: Graph) -> list[str]:
    wanted = {str(o).rstrip("/#") for o in graph.objects(None, OWL.imports)}
    if not wanted:
        return []
    slugs: list[str] = []
    cursor: str | None = None
    while True:
        page = await models.list_models(cursor, 100)
        slugs += [
            m.slug
            for m in page.items
            if m.slug != model and m.base_iri is not None and m.base_iri.rstrip("/#") in wanted
        ]
        cursor = page.next_cursor
        if cursor is None:
            return sorted(slugs)


def _local_name(ref: str, model: str) -> str | None:
    """The bare concept name of a ref into ``model`` (``None`` for another model)."""
    if "." in ref and ref.split(".", 1)[0] != model:
        return None
    return ref.rsplit(".", 1)[-1]


def _endpoints(op: ProposalOperation) -> list[str]:
    """Concept refs an operation needs to exist in OM (realizations are Lakeloom-side)."""
    match op:
        case AddConcept():
            return [op.parent] if op.parent else []
        case AddAttribute() | AddAxiom() | AddMapping():
            return [op.concept]
        case AddRelation():
            return [op.source, op.target]
        case AddSynonym():
            return [op.concept_fqn]
        case _:
            return []


def _table_of(op: AddConcept) -> str | None:
    return next((e.ref for e in op.evidence if e.kind == "table"), None)


def _queries(op: AddConcept, table: str | None) -> list[str]:
    queries = [op.name]
    if table is not None:
        bare = table.rsplit(".", 1)[-1]
        queries += [bare, singularize(bare)]
    return list(dict.fromkeys(queries))


def _rank(match: ConceptMatch) -> tuple[float, str]:
    return (-match.score, match.concept.fqn)


class _Dedupe:
    def __init__(
        self,
        *,
        model: str,
        models: ModelService,
        om: OMClient,
        pending: Mapping[str, str],
        asset_by_table: Mapping[str, tuple[str, str | None]],
    ) -> None:
        self.model = model
        self.models = models
        self.om = om
        self.pending = pending
        self.asset_by_table = asset_by_table
        self.result = DedupeResult()
        self.graphs: dict[str, Graph] = {}
        self.replaced: dict[str, _Target] = {}  # proposed concept name -> existing concept
        self.tables: dict[str, OMTable | None] = {}
        self.seen: set[str] = set()

    def warn(self, text: str) -> None:
        self.result.warnings.append(text)

    # -- helpers -----------------------------------------------------------------------

    def _pending_drop(self, op: ProposalOperation) -> bool:
        key = op_key(op)
        if key in self.pending:
            self.warn(f"already proposed in {self.pending[key]}: {key}")
            return True
        return False

    def _realized(self, concept_fqn: str, asset: str, column: str | None) -> bool:
        table = self.tables.get(asset)
        if table is None:
            return False
        if column is None:
            return concept_fqn in direct_glossary_tags(table.tags, column=False)
        col = next((c for c in table.columns if c.name == column), None)
        return col is not None and concept_fqn in direct_glossary_tags(col.tags, column=True)

    def _realization(
        self, target: _Target, asset: str, column: str | None, src: ProposalOperation
    ) -> list[ProposalOperation]:
        op = AddRealization(
            op="add_realization",
            concept=target.fqn,
            asset_fqn=asset,
            column=column,
            rule_ids=[*src.rule_ids, "R-DEDUPE"],
            evidence=list(src.evidence),
        )
        key = op_key(op)
        if key in self.pending:
            self.warn(f"already proposed in {self.pending[key]}: {key}")
            return []
        if self._realized(target.fqn, asset, column):
            if column is None:
                self.warn(f"{target.fqn} is already realized by {asset}")
            return []
        if key in self.seen:
            return []
        self.seen.add(key)
        return [op]

    def _asset(self, table: str | None) -> str | None:
        entry = None if table is None else self.asset_by_table.get(table)
        return None if entry is None else entry[0]

    # -- concept resolution ------------------------------------------------------------

    async def _best(self, op: AddConcept, slugs: list[str]) -> ConceptMatch | None:
        best: ConceptMatch | None = None
        for query in _queries(op, _table_of(op)):
            for match in await score_candidates(self.models, query, model=slugs):
                if best is None or _rank(match) < _rank(best):
                    best = match
                break  # sorted: the first is this query's best
        return best

    async def _plan(self, ops: list[ProposalOperation], slugs: list[str]) -> None:
        for op in ops:
            if not isinstance(op, AddConcept) or op_key(op) in self.pending:
                continue
            best = await self._best(op, slugs)
            if best is None or best.score < WARN_MIN:
                continue
            ref = best.concept
            table = _table_of(op)
            asset = self._asset(table)
            action = "kept"
            if best.score >= REALIZE_MIN:
                if asset is None:
                    why = f"table {table} has no OM asset" if table else "it has no source table"
                    self.warn(
                        f"concept {op.name} matches {ref.fqn} ({best.score:.2f}) but {why} "
                        "to realize it; kept as new"
                    )
                else:
                    action = "realized"
                    node = URIRef(ref.iri) if ref.iri else None
                    self.replaced[op.name] = _Target(ref.fqn, ref.model, node)
            else:
                self.warn(
                    f"concept {op.name} may duplicate existing {ref.fqn} "
                    f"(confidence {best.score:.2f}, matched on {best.matched_on}); "
                    "kept as new, review before approving"
                )
            self.result.matches.append(
                Match(op_key(op), ref.fqn, best.score, best.matched_on, action)
            )

    # -- per-operation rewriting -------------------------------------------------------

    def _ref(self, ref: str) -> tuple[str, _Target | None]:
        name = ref
        if "." in ref:  # a qualified ref (FQN, possibly nested) matches only this model
            if ref.split(".", 1)[0] != self.model:
                return ref, None
            name = ref.rsplit(".", 1)[-1]
        target = self.replaced.get(name)
        if target is None:
            return ref, None
        return target.ref(self.model), target

    def _foreign(self, target: _Target | None) -> bool:
        return target is not None and target.model != self.model

    def _drop_foreign(self, kind: str, target: _Target) -> None:
        self.warn(f"{kind} to imported concept {target.fqn} needs cross-model support (not in M2a)")

    def _graph_of(self, target: _Target | None) -> tuple[Graph, str] | None:
        slug = self.model if target is None else target.model
        graph = self.graphs.get(slug)
        return None if graph is None else (graph, slug)

    def _attribute(self, op: AddAttribute) -> list[ProposalOperation]:
        concept, target = self._ref(op.concept)
        if target is None:
            return [] if self._pending_drop(op) else [op]
        found = self._graph_of(target)
        exists = (
            found is not None
            and target.node is not None
            and _norm(op.name) in _attribute_names(found[0], target.node)
        )
        if exists:
            return self._column_realization(op, target)
        if self._foreign(target):
            self._drop_foreign("attribute", target)
            return []
        moved = op.model_copy(update={"concept": concept})
        return [] if self._pending_drop(moved) else [moved]

    def _column_realization(self, op: AddAttribute, target: _Target) -> list[ProposalOperation]:
        evidence = next((e for e in op.evidence if e.kind == "column"), None)
        if evidence is None or "." not in evidence.ref:
            return []
        table, column = evidence.ref.rsplit(".", 1)
        asset = self.asset_by_table.get(table)
        if asset is None:
            return []
        return self._realization(target, asset[0], column, op)

    def _relation(self, op: AddRelation) -> list[ProposalOperation]:
        source, src_t = self._ref(op.source)
        target, tgt_t = self._ref(op.target)
        foreign = [t for t in (src_t, tgt_t) if t is not None and self._foreign(t)]
        if foreign:
            for t in foreign:
                self._drop_foreign("relation", t)
            return []
        moved = op.model_copy(update={"source": source, "target": target})
        if self._pending_drop(moved):
            return []
        graph = self.graphs[self.model]
        a, b = _node_of(graph, self.model, source), _node_of(graph, self.model, target)
        if a is not None and b is not None and _relation_exists(graph, a, op.relation_type, b):
            return []
        return [moved]

    def _on_concept(self, op: AddAxiom | AddMapping | AddSynonym) -> list[ProposalOperation]:
        attr = "concept_fqn" if isinstance(op, AddSynonym) else "concept"
        concept, target = self._ref(cast("str", getattr(op, attr)))
        if target is not None and self._foreign(target):
            self._drop_foreign(op.op.removeprefix("add_"), target)
            return []
        moved = op.model_copy(update={attr: concept})
        if self._pending_drop(moved):
            return []
        graph = self.graphs[self.model]
        node = _node_of(graph, self.model, concept)
        if node is not None:
            if isinstance(moved, AddAxiom) and _axiom_exists(graph, node, moved):
                return []
            if isinstance(moved, AddMapping) and _mapping_exists(graph, node, moved):
                return []
        return [moved]

    def _concept(self, op: AddConcept) -> list[ProposalOperation]:
        if self._pending_drop(op):
            return []
        target = self.replaced.get(op.name)
        if target is None:
            return [self._reparent(op)]
        table = _table_of(op)
        asset = self._asset(table)
        if asset is None:  # replaced only when an asset was known
            return [op]
        return self._realization(target, asset, None, op)

    def _reparent(self, op: AddConcept) -> AddConcept:
        if op.parent is None:
            return op
        parent, target = self._ref(op.parent)
        if self._foreign(target) and target is not None:
            self.warn(
                f"parent {target.fqn} is in an imported model; proposing {op.name} without a "
                "parent (cross-model not in M2a)"
            )
            return op.model_copy(update={"parent": None})
        return op.model_copy(update={"parent": parent})

    def _realization_op(self, op: AddRealization) -> list[ProposalOperation]:
        if self._pending_drop(op):
            return []
        if self._realized(op.concept, op.asset_fqn, op.column):
            return []
        return [op]

    def _pending_endpoint(self, op: ProposalOperation, created: set[str]) -> str | None:
        """The warning for an op whose local concept is pending in another proposal only."""
        graph = self.graphs[self.model]
        for ref in _endpoints(op):
            name = _local_name(ref, self.model)
            if name is None or name in created or _node_of(graph, self.model, name) is not None:
                continue
            pid = self.pending.get(f"concept:{name}")
            if pid is not None:
                return (
                    f"{op_key(op)} depends on {name} pending in proposal {pid}; "
                    "propose again after it is applied"
                )
        return None

    def _drop_pending_endpoints(self, ops: list[ProposalOperation]) -> list[ProposalOperation]:
        while True:  # dropping a child concept can orphan the ops that refer to it
            created = {op.name for op in ops if isinstance(op, AddConcept)}
            kept: list[ProposalOperation] = []
            for op in ops:
                why = self._pending_endpoint(op, created)
                if why is None:
                    kept.append(op)
                else:
                    self.warn(why)
            if len(kept) == len(ops):
                return kept
            ops = kept

    def rewrite(self, op: ProposalOperation) -> list[ProposalOperation]:
        match op:
            case AddConcept():
                return self._concept(op)
            case AddAttribute():
                return self._attribute(op)
            case AddRelation():
                return self._relation(op)
            case AddAxiom() | AddMapping() | AddSynonym():
                return self._on_concept(op)
            case AddRealization():
                return self._realization_op(op)
            case _:
                return [] if self._pending_drop(op) else [op]

    async def run(self, ops: list[ProposalOperation]) -> DedupeResult:
        base = await self.models.graph(self.model)
        self.graphs[self.model] = base
        slugs = [self.model, *await _import_slugs(self.models, self.model, base)]
        for slug in slugs[1:]:
            self.graphs[slug] = await self.models.graph(slug)
        await self._plan(ops, slugs)
        wanted: list[str] = []
        for op in ops:
            if isinstance(op, AddConcept) and op.name in self.replaced:
                asset = self.asset_by_table.get(_table_of(op) or "")
                if asset is not None:
                    wanted.append(asset[0])
            elif isinstance(op, AddRealization):
                wanted.append(op.asset_fqn)
            elif isinstance(op, AddAttribute):
                evidence = next((e for e in op.evidence if e.kind == "column"), None)
                asset = (
                    self.asset_by_table.get(evidence.ref.rsplit(".", 1)[0]) if evidence else None
                )
                if asset is not None:
                    wanted.append(asset[0])
        self.tables = await fetch_tables(self.models, self.om, sorted(set(wanted)))
        out: list[ProposalOperation] = []
        for op in ops:
            out.extend(self.rewrite(op))
        self.result.operations = self._drop_pending_endpoints(out)
        self.result.warnings = sorted(set(self.result.warnings))
        return self.result


async def dedupe(
    ops: list[ProposalOperation],
    *,
    model: str,
    models: ModelService,
    om: OMClient,
    pending: Callable[[str], Awaitable[Mapping[str, str]]],
    asset_by_table: Mapping[str, tuple[str, str | None]],
) -> DedupeResult:
    """Remove from ``ops`` whatever the model, its imports or open proposals already hold.

    ``pending(model)`` returns the :func:`op_key` -> proposal id of every operation in an open
    proposal for ``model`` (the id goes into the "already proposed in <id>" warning).
    ``asset_by_table`` maps a scanned table's full name to ``(OM table FQN, None)``; the OM FQN
    is what realizations record. The result keeps the mapper's order, with a realization right
    where the replaced ``AddConcept`` was; warnings are sorted.
    """
    return await _Dedupe(
        model=model,
        models=models,
        om=om,
        pending=await pending(model),
        asset_by_table=asset_by_table,
    ).run(list(ops))


async def screen(
    ops: Sequence[ProposalOperation],
    *,
    model: str,
    models: ModelService,
    pending: Mapping[str, str],
    own: str,
) -> DedupeResult:
    """FR-PRO-02 for operations an agent passes to ``propose_change`` (exact checks only).

    Dropped with a warning: an operation whose :func:`op_key` is pending in another open
    proposal (``pending``: op key -> proposal id; ``own`` is the proposal being changed), an
    ``AddConcept`` whose name is an existing concept of ``model`` and an ``AddAttribute``
    already on its concept. Existing names are also reported as matches (confidence 1.0).
    """
    graph = await models.graph(model)
    result = DedupeResult()
    for op in ops:
        key = op_key(op)
        pid = pending.get(key)
        if pid is not None and pid != own:
            result.warnings.append(f"already proposed in {pid}: {key}")
            continue
        found = _existing(graph, model, op)
        if found is not None:
            fqn, what = found
            result.matches.append(Match(key, fqn, 1.0, "name", "dropped"))
            result.warnings.append(f"{what} already exists in {fqn}; dropped: {key}")
            continue
        result.operations.append(op)
    return result


def _existing(graph: Graph, model: str, op: ProposalOperation) -> tuple[str, str] | None:
    """``(concept FQN, what)`` when ``op`` adds a concept or attribute ``model`` already has."""
    if isinstance(op, AddConcept):
        node = _node_of(graph, model, op.name)
        return None if node is None else (_fqn(graph, model, node), f"concept {op.name}")
    if isinstance(op, AddAttribute):
        name = _local_name(op.concept, model)
        node = None if name is None else _node_of(graph, model, name)
        if node is not None and _norm(op.name) in _attribute_names(graph, node):
            return _fqn(graph, model, node), f"attribute {op.name}"
    return None


def _fqn(graph: Graph, model: str, node: URIRef) -> str:
    fqn = graph.value(node, OM_FQN)
    return str(fqn) if fqn is not None else f"{model}.{_local(str(node))}"
