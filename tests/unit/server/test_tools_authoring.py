"""MCP tools for direct authoring: vocabulary, taxonomy and thesaurus (M5b).

Tests focus on:
- Tool registration, annotations and scope guards (FR-AGT-06)
- ``harvest_vocabulary_candidates``: tag normalisation
- ``validate_thesaurus``: delegates to core.thesaurus (integration with SKOS graph)
- ``propose_add_concepts``: creates a ``direct``-origin draft proposal
- Scope guard: read tools require lakeloom:read; write tools require lakeloom:propose
"""

from __future__ import annotations

from typing import Any

import pytest
from kit import AuthKit, DepsFactory, mcp_session
from test_tools_proposals import FakeProposals, proposal
from test_tools_semantic import fake_deps, structured, text

from lakeloom.core.audit import current_audit
from lakeloom.server.mcp_app import create_mcp, create_server_app

READ = "lakeloom:read"
PROPOSE = "lakeloom:propose"

READ_TOOLS = {
    "harvest_vocabulary_candidates",
    "validate_thesaurus",
    "suggest_related_terms",
    "assess_taxonomy_coverage",
}
WRITE_TOOLS = {
    "propose_add_concepts",
    "propose_add_hierarchy",
    "propose_deprecate_concept",
    "propose_add_scope_note",
}
ALL_CV_TOOLS = READ_TOOLS | WRITE_TOOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def deps_with_fake_proposals(make_deps: DepsFactory) -> Any:
    """Deps wired with a FakeProposals that also handles propose_operations."""

    deps = fake_deps(make_deps)
    fake = _ExtendedFakeProposals()
    deps.proposals = fake  # pyright: ignore[reportAttributeAccessIssue]
    return deps


def deps_and_fake(make_deps: DepsFactory) -> tuple[Any, _ExtendedFakeProposals]:
    """Deps wired with a FakeProposals that records the proposed operations."""

    deps = fake_deps(make_deps)
    fake = _ExtendedFakeProposals()
    deps.proposals = fake  # pyright: ignore[reportAttributeAccessIssue]
    return deps, fake


class _ExtendedFakeProposals(FakeProposals):
    """FakeProposals extended with propose_operations (used by CV write tools)."""

    def __init__(self) -> None:
        super().__init__()
        self.last_operations: list[Any] = []

    async def propose_operations(self, model: str, operations: Any, **kw: Any) -> Any:
        self.last_operations = list(operations)
        trail = current_audit()
        p = proposal()
        trail.model, trail.proposal_id, trail.change_set_id = p.model, p.id, "cs-n"
        return p


async def call_read(
    make_deps: DepsFactory,
    kit: AuthKit,
    tool: str,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    """Call a READ tool (uses real model graph, no fake proposals needed)."""
    app = create_server_app(fake_deps(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        return await session.call_tool(tool, args or {})


async def call_write(
    make_deps: DepsFactory,
    kit: AuthKit,
    tool: str,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    """Call a WRITE tool (uses fake proposals)."""
    app = create_server_app(deps_with_fake_proposals(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        return await session.call_tool(tool, args or {})


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


async def test_cv_tools_registered(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    assert ALL_CV_TOOLS <= set(tools), f"Missing: {ALL_CV_TOOLS - set(tools)}"


async def test_read_tools_are_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    for name in READ_TOOLS:
        ann = tools[name].annotations
        assert ann is not None
        assert ann.read_only_hint is True, f"{name} should be read_only"
        assert ann.destructive_hint is False


async def test_write_tools_are_not_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    for name in WRITE_TOOLS:
        ann = tools[name].annotations
        assert ann is not None
        assert ann.read_only_hint is False, f"{name} should not be read_only"
        assert ann.destructive_hint is False


# ---------------------------------------------------------------------------
# harvest_vocabulary_candidates
# ---------------------------------------------------------------------------


async def test_list_cv_candidates_normalises_tags(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps,
        kit,
        "harvest_vocabulary_candidates",
        {"tags": ["PII.Email", "Domain.Order_Management", "!!!"]},
        scope=READ,
    )
    data = structured(result)
    assert data["total_input"] == 3
    assert data["total_valid"] == 2
    names = [c["name"] for c in data["candidates"]]
    assert "Email" in names
    assert "Order Management" in names


async def test_list_cv_candidates_frequency_map_sorts(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps,
        kit,
        "harvest_vocabulary_candidates",
        {
            "tags": ["PII.Email", "Domain.Order"],
            "frequency_map": {"PII.Email": 5, "Domain.Order": 20},
        },
        scope=READ,
    )
    data = structured(result)
    assert data["candidates"][0]["name"] == "Order"


async def test_list_cv_candidates_requires_read_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    # Token with no scope at all → forbidden
    result = await call_read(
        make_deps,
        kit,
        "harvest_vocabulary_candidates",
        {"tags": ["PII.Email"]},
        scope=None,  # drops the scope claim
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# validate_thesaurus (uses fake model's SKOS graph)
# ---------------------------------------------------------------------------


async def test_validate_thesaurus_ok(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps,
        kit,
        "validate_thesaurus",
        {"model": "retail"},
        scope=READ,
    )
    data = structured(result)
    assert data["model"] == "retail"
    assert isinstance(data["total_concepts"], int)
    assert isinstance(data["error_count"], int)


async def test_validate_thesaurus_missing_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps,
        kit,
        "validate_thesaurus",
        {"model": "no-such-model"},
        scope=READ,
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# assess_taxonomy_coverage
# ---------------------------------------------------------------------------


async def test_assess_taxonomy_coverage(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps,
        kit,
        "assess_taxonomy_coverage",
        {"model": "retail", "topics": ["Order", "Customer"]},
        scope=READ,
    )
    data = structured(result)
    assert data["model"] == "retail"
    assert 0.0 <= data["coverage_ratio"] <= 1.0


# ---------------------------------------------------------------------------
# propose_add_concepts
# ---------------------------------------------------------------------------


async def test_propose_add_term_creates_draft(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_write(
        make_deps,
        kit,
        "propose_add_concepts",
        {
            "model": "retail",
            "terms": [{"name": "Invoice", "definition": "A billing document."}],
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert "error" not in data, f"Tool returned error: {data.get('error')}"
    assert data["status"] == "draft"
    assert data["proposal_id"]


async def test_propose_add_term_requires_propose_scope(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    # Read scope is not enough for a write tool.
    result = await call_write(
        make_deps,
        kit,
        "propose_add_concepts",
        {"model": "retail", "terms": [{"name": "Invoice"}]},
        scope=READ,
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


async def test_propose_add_term_multiple_terms(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_write(
        make_deps,
        kit,
        "propose_add_concepts",
        {
            "model": "retail",
            "terms": [
                {"name": "Shipment"},
                {"name": "Return", "broader": "retail.Order"},
            ],
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert data["status"] == "draft"


# ---------------------------------------------------------------------------
# propose_add_hierarchy
# ---------------------------------------------------------------------------


async def test_propose_add_hierarchy(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_write(
        make_deps,
        kit,
        "propose_add_hierarchy",
        {
            "model": "retail",
            "parent_name": "PaymentMethod",
            "children": ["CreditCard", "BankTransfer"],
            "parent_definition": "A means of settling an invoice.",
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert data["status"] == "draft"


async def test_propose_add_hierarchy_children_get_descriptions(
    make_deps: DepsFactory,
    kit: AuthKit,
) -> None:
    deps, fake = deps_and_fake(make_deps)
    app = create_server_app(deps)
    async with mcp_session(app, kit.token(scope=PROPOSE)) as session:
        result = await session.call_tool(
            "propose_add_hierarchy",
            {
                "model": "retail",
                "parent_name": "PaymentMethod",
                "children": ["CreditCard", "BankTransfer"],
                "parent_definition": "A means of settling an invoice.",
            },
        )
    data = structured(result)
    assert data["status"] == "draft"
    ops = fake.last_operations
    children_ops = [
        o for o in ops if o.op == "add_concept" and str(o.name) in {"CreditCard", "BankTransfer"}
    ]
    assert len(children_ops) == 2
    for op in children_ops:
        assert op.description is not None, f"add_concept {op.name} is missing a description"
        assert op.description.text


# ---------------------------------------------------------------------------
# propose_add_scope_note
# ---------------------------------------------------------------------------


async def test_propose_add_scope_note_missing_concept(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_write(
        make_deps,
        kit,
        "propose_add_scope_note",
        {
            "model": "retail",
            "concept": "NonExistentConcept9999",
            "note": "Should fail.",
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Real resolution paths (audit fixes): exact FQNs, no silent zero results
# ---------------------------------------------------------------------------


class _Recording(_ExtendedFakeProposals):
    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[tuple[list[Any], dict[str, Any]]] = []

    async def propose_operations(self, model: str, operations: Any, **kw: Any) -> Any:
        self.recorded.append((list(operations), kw))
        return await super().propose_operations(model, operations, **kw)


async def call_recorded(
    make_deps: DepsFactory, kit: AuthKit, tool: str, args: dict[str, Any]
) -> tuple[Any, _Recording]:
    deps = fake_deps(make_deps)
    rec = _Recording()
    deps.proposals = rec  # pyright: ignore[reportAttributeAccessIssue]
    async with mcp_session(create_server_app(deps), kit.token(scope=PROPOSE)) as session:
        return await session.call_tool(tool, args), rec


async def test_suggest_related_terms_finds_cooccurring_concepts(
    make_deps: DepsFactory, kit: AuthKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakeloom.core.concepts import AssetRef
    from lakeloom.server import tools_analytics
    from lakeloom.server.tools_analytics import RealizationIndex

    real = tools_analytics.load_realization_index

    async def overlapping(deps: Any, model: str) -> RealizationIndex:
        index = await real(deps, model)  # the real graph and OM asset lookups
        t = "svc.retail.sales.orders"
        for name in ("OrderDate", "MonetaryAmount"):  # column realizations on the same table
            iri = f"http://x/ontology/retail/{name}"
            index.assets[iri] = [AssetRef(fqn=t, type="table", column=name.lower())]
        return index

    monkeypatch.setattr(tools_analytics, "load_realization_index", overlapping)
    result = await call_read(
        make_deps, kit, "suggest_related_terms", {"model": "retail", "min_shared": 1}, scope=READ
    )
    data = structured(result)
    assert "error" not in data, data
    assert data["total_suggestions"] > 0
    body = text(result)
    assert "`OrderDate`" in body or "`MonetaryAmount`" in body  # labels, not IRIs
    assert "http" not in body.split("Next:")[0]


async def test_deprecate_resolves_concept_and_successor_fqns(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    args = {"model": "retail", "concept": "OrderDate", "reason": "r", "successor": "MonetaryAmount"}
    result, rec = await call_recorded(make_deps, kit, "propose_deprecate_concept", args)
    assert "error" not in structured(result), structured(result)
    (op,), kw = rec.recorded[0]
    assert (op.fqn, op.successor) == ("retail.OrderDate", "retail.MonetaryAmount")
    assert kw["recheck_pending"] is True


async def test_deprecate_rejects_unknown_successor_and_self(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    base = {"model": "retail", "concept": "retail.OrderDate", "reason": "r"}
    result, _ = await call_recorded(
        make_deps, kit, "propose_deprecate_concept", {**base, "successor": "Nope"}
    )
    assert structured(result)["error"]["code"] == "not_found"
    result, _ = await call_recorded(
        make_deps, kit, "propose_deprecate_concept", {**base, "successor": "OrderDate"}
    )
    assert structured(result)["error"]["code"] == "validation_error"


async def test_scope_note_uses_the_exact_fqn(make_deps: DepsFactory, kit: AuthKit) -> None:
    result, rec = await call_recorded(
        make_deps,
        kit,
        "propose_add_scope_note",
        {"model": "retail", "concept": "retail.Order.OrderStatus", "note": "n"},
    )
    assert "error" not in structured(result), structured(result)
    assert rec.recorded[0][0][0].concept_fqn == "retail.Order.OrderStatus"


async def test_hierarchy_reuses_an_existing_parent(make_deps: DepsFactory, kit: AuthKit) -> None:
    result, rec = await call_recorded(
        make_deps,
        kit,
        "propose_add_hierarchy",
        {"model": "retail", "parent_name": "Order", "children": ["Refund"]},
    )
    assert "error" not in structured(result), structured(result)
    ops = rec.recorded[0][0]
    assert [(o.name, o.parent) for o in ops] == [("Refund", "retail.Order")]


async def test_hierarchy_creates_a_new_parent(make_deps: DepsFactory, kit: AuthKit) -> None:
    _, rec = await call_recorded(
        make_deps,
        kit,
        "propose_add_hierarchy",
        {"model": "retail", "parent_name": "Shipment", "children": ["Parcel"]},
    )
    ops = rec.recorded[0][0]
    assert [(o.name, o.parent) for o in ops] == [("Shipment", None), ("Parcel", "Shipment")]


# ---------------------------------------------------------------------------
# Phase 4: relations, history notes, harvesting, definition and stage warnings
# ---------------------------------------------------------------------------


async def test_add_related_is_a_glossary_only_related_to(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result, rec = await call_recorded(
        make_deps,
        kit,
        "propose_add_relation",
        {"model": "retail", "source": "OrderDate", "target": "MonetaryAmount"},
    )
    assert "error" not in structured(result), structured(result)
    ((op,), kw) = rec.recorded[0]
    assert (op.op, op.source, op.relation_type, op.target, op.glossary_only) == (
        "add_relation",
        "retail.OrderDate",
        "relatedTo",
        "retail.MonetaryAmount",
        True,
    )
    assert kw["recheck_pending"] is True


async def test_add_broader_refuses_self_and_cycles(make_deps: DepsFactory, kit: AuthKit) -> None:
    base = {"model": "retail", "relation": "broader"}
    result, _ = await call_recorded(
        make_deps, kit, "propose_add_relation", {**base, "source": "Order", "target": "Order"}
    )
    assert structured(result)["error"]["code"] == "validation_error"
    result, _ = await call_recorded(
        make_deps,
        kit,
        "propose_add_relation",
        {**base, "source": "Order", "target": "retail.Order.OrderStatus"},
    )
    assert "cycle" in structured(result)["error"]["message"]


async def test_add_history_note_uses_the_exact_fqn(make_deps: DepsFactory, kit: AuthKit) -> None:
    result, rec = await call_recorded(
        make_deps,
        kit,
        "propose_add_history_note",
        {"model": "retail", "concept": "OrderDate", "note": "Was OrderTimestamp until v2."},
    )
    assert "error" not in structured(result), structured(result)
    op = rec.recorded[0][0][0]
    assert (op.op, op.concept_fqn) == ("add_history_note", "retail.OrderDate")


async def test_add_concepts_warns_about_missing_definitions(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    terms = [{"name": "Refund", "definition": "Money returned."}, {"name": "Voucher"}]
    _, rec = await call_recorded(
        make_deps, kit, "propose_add_concepts", {"model": "retail", "terms": terms}
    )
    warnings = rec.recorded[0][1]["warnings"]
    assert any("1 concept(s) have no definition (Voucher)" in w for w in warnings)


async def test_hierarchy_warns_when_the_vocabulary_is_undefined(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    # the retail fixture defines 1 of its 6 concepts
    _, rec = await call_recorded(
        make_deps,
        kit,
        "propose_add_hierarchy",
        {"model": "retail", "parent_name": "Order", "children": ["Refund"]},
    )
    assert any("finishes the controlled vocabulary" in w for w in rec.recorded[0][1]["warnings"])


async def test_harvest_without_tags_reads_om_classifications(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    from lakeloom.om.models import OMPage, OMTag

    deps = fake_deps(make_deps)

    async def list_tags(*, after: str | None = None, limit: int = 500) -> OMPage[OMTag]:
        rows = [
            {"id": "1", "name": "Email", "fullyQualifiedName": "PII.Email", "usageCount": 7},
            {"id": "2", "name": "Old", "fullyQualifiedName": "PII.Old", "deprecated": True},
            {"id": "3", "name": "Order_Total", "fullyQualifiedName": "Sales.Order_Total"},
        ]
        return OMPage[OMTag](data=[OMTag.model_validate(r) for r in rows], after=None)

    deps.om.list_tags = list_tags  # pyright: ignore[reportAttributeAccessIssue]
    async with mcp_session(create_server_app(deps), kit.token(scope=READ)) as session:
        result = await session.call_tool("harvest_vocabulary_candidates", {})
    data = structured(result)
    assert "error" not in data, data
    names = [c["name"] for c in data["candidates"]]
    assert names[0] == "Email" and "Order Total" in names and "Old" not in names
    assert "OpenMetadata classification tags" in text(result)


async def test_add_broader_reparents_with_the_term_version(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    from lakeloom.om.models import OMGlossaryTerm

    deps = fake_deps(make_deps)
    rec = _Recording()
    deps.proposals = rec  # pyright: ignore[reportAttributeAccessIssue]

    async def get_term(fqn: str, *_: object) -> OMGlossaryTerm:
        return OMGlossaryTerm.model_validate(
            {
                "id": "t",
                "name": fqn.rsplit(".", 1)[-1],
                "fullyQualifiedName": fqn,
                "version": 0.4,
                "glossary": {"id": "id-retail", "name": "retail", "type": "glossary"},
            }
        )

    deps.om.get_term = get_term  # pyright: ignore[reportAttributeAccessIssue]
    args = {"model": "retail", "source": "OrderDate", "target": "Order", "relation": "broader"}
    async with mcp_session(create_server_app(deps), kit.token(scope=PROPOSE)) as session:
        result = await session.call_tool("propose_add_relation", args)
    assert "error" not in structured(result), structured(result)
    op = rec.recorded[0][0][0]
    assert (op.op, op.fqn, op.parent, op.base_version) == (
        "update_concept",
        "retail.OrderDate",
        "retail.Order",
        "0.4",
    )


async def test_add_related_refuses_an_already_linked_pair(
    make_deps: DepsFactory, kit: AuthKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OM keeps one relationship per pair: an RT on a pair a typed link (placedBy) already
    joins would be applied as a no-op, so it is refused when proposed."""
    from rdflib import Graph, Literal, URIRef
    from rdflib.namespace import OWL, RDF, SKOS

    from lakeloom.rdf.skos_enrichment import OM_FQN
    from lakeloom.server import tools_thesaurus

    g = Graph()
    b = "http://x/ontology/retail/"
    for name in ("Order", "OrderDate"):
        g.add((URIRef(b + name), RDF.type, SKOS.Concept))
        g.add((URIRef(b + name), OM_FQN, Literal(f"retail.{name}")))
    g.add((URIRef(b + "placedOn"), RDF.type, OWL.ObjectProperty))
    g.add((URIRef(b + "Order"), URIRef(b + "placedOn"), URIRef(b + "OrderDate")))

    async def fake(models: Any, repo: Any, model: str, **_: Any) -> Graph:
        return g

    monkeypatch.setattr(tools_thesaurus, "effective_graph", fake)
    result, rec = await call_recorded(
        make_deps,
        kit,
        "propose_add_relation",
        {"model": "retail", "source": "Order", "target": "OrderDate"},
    )
    err = structured(result)["error"]
    assert err["code"] == "validation_error" and "already linked (placedOn)" in err["message"]
    assert rec.recorded == []
