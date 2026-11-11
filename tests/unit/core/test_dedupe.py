"""Duplicate avoidance against models, imports and open proposals (FR-PRO-02)."""

from collections.abc import Mapping
from typing import cast

from test_mapper import POLICY, col, customers, orders, record, table

from lakeloom.config import Settings
from lakeloom.core.dedupe import DedupeResult, dedupe
from lakeloom.core.mapper import map_scan
from lakeloom.core.models import ModelService
from lakeloom.core.operations import (
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddRealization,
    AddRelation,
    OMContext,
    ProposalOperation,
    draft_term_id,
    op_key,
    to_om_all,
)
from lakeloom.om.client import OMClient
from lakeloom.om.models import OMGlossary, OMPage, OMTable
from lakeloom.state.scans import ScannedTable

B = "http://x/ontology/retail/"
PREFIXES = """\
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix om: <https://open-metadata.org/ontology/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix r: <http://x/ontology/retail/> .
"""
RETAIL = (
    PREFIXES
    + """\
r:Order a skos:Concept ; skos:prefLabel "Order" ; skos:altLabel "Purchase" ;
    om:fullyQualifiedName "retail.Order" .
r:Customer a skos:Concept ; skos:prefLabel "Customer" ; skos:altLabel "Client", "Buyer" ;
    om:fullyQualifiedName "retail.Customer" .
r:OrderStatus a skos:Concept ; skos:prefLabel "OrderStatus" ;
    om:fullyQualifiedName "retail.Order.OrderStatus" .
r:placedBy a owl:ObjectProperty ; rdfs:label "placed by" .
"""
)
SALES = (
    PREFIXES
    + """\
<http://x/ontology/sales/> a owl:Ontology ; owl:imports <http://x/ontology/retail/> .
"""
)
CTX = OMContext(glossary_name="retail", glossary_id="g", base_iri=B, ontology_id="o")


def native_ttl(ops: list[ProposalOperation], only: set[str] | None = None) -> str:
    """Turtle for what the native ontology holds once ``ops`` are applied.

    Built from ``to_om_all`` (the same IRI helpers the real write path uses), so the shapes
    of attribute IRIs (``{base}{Concept}.{name}``, no labels) and key restrictions cannot
    drift from what a real export of an applied proposal contains.
    """
    wanted = [
        o for o in ops if isinstance(o, AddAttribute | AddAxiom) and (only is None or _in(o, only))
    ]
    out: list[str] = []
    for item in to_om_all(wanted, CTX).native:
        if item["operationType"] == "UPSERT_PROPERTY":
            prop = item["property"]
            out.append(
                f"<{prop['iri']}> a owl:DatatypeProperty ; rdfs:domain <{prop['domain'][0]}> ;"
                f" rdfs:range <{prop['range'][0]}> ."
            )
        else:
            axiom = item["axiom"]
            for expr in axiom["expressions"]:
                out.append(
                    f"<{axiom['subjectIri']}> rdfs:subClassOf [ a owl:Restriction ;"
                    f" owl:onProperty <{expr['propertyIri']}> ;"
                    ' owl:cardinality "1"^^xsd:nonNegativeInteger ] .'
                )
    return "\n".join(out) + "\n"


def _in(op: ProposalOperation, only: set[str]) -> bool:
    return op_key(op) in only


def mapped(*tables: ScannedTable) -> list[ProposalOperation]:
    return map_scan(record(*tables), POLICY).operations


SOME_ATTRS = RETAIL + native_ttl(
    mapped(orders()), only={"attr:Order.total_amount", "attr:Order.order_id"}
)
RELATION = "r:hasCustomer a owl:ObjectProperty .\nr:Order r:hasCustomer r:Customer .\n"
# retail after scans of customers and orders were applied: attributes, key axioms, relation.
APPLIED = RETAIL + native_ttl(mapped(orders(), customers())) + RELATION


def om_table(fqn: str, tags: list[str], columns: Mapping[str, list[str]] | None = None) -> OMTable:
    def label(t: str) -> dict[str, str]:
        return {"tagFQN": t, "source": "Glossary", "labelType": "Manual"}

    return OMTable.model_validate(
        {
            "id": fqn,
            "fullyQualifiedName": fqn,
            "tags": [label(t) for t in tags],
            "columns": [
                {"name": c, "tags": [label(t) for t in ts]} for c, ts in (columns or {}).items()
            ],
        }
    )


class FakeOM:
    def __init__(self, turtles: dict[str, str], tables: dict[str, OMTable]) -> None:
        self.turtles = turtles
        self.tables = tables

    @property
    def client(self) -> OMClient:
        return cast("OMClient", self)

    async def list_glossaries(self, *, after: str | None = None, limit: int = 50):
        data = [
            OMGlossary.model_validate({"id": f"id-{n}", "name": n, "fullyQualifiedName": n})
            for n in self.turtles
        ]
        return OMPage[OMGlossary](data=data, after=None)

    async def get_glossary_by_name(self, name: str, **_: object) -> OMGlossary | None:
        if name not in self.turtles:
            return None
        return OMGlossary.model_validate(
            {"id": f"id-{name}", "name": name, "fullyQualifiedName": name}
        )

    async def export_ontology(self, glossary_id: str, fmt: str, **_: object) -> str:
        return self.turtles[glossary_id.removeprefix("id-")]

    async def get_table_by_fqn(self, fqn: str, *_: object, **__: object) -> OMTable | None:
        return self.tables.get(fqn)


def make(
    turtles: dict[str, str] | None = None, tables: dict[str, OMTable] | None = None
) -> tuple[ModelService, FakeOM]:
    om = FakeOM(turtles or {"retail": RETAIL}, tables or {})
    models = ModelService(om, Settings(public_url="http://x"))  # pyright: ignore[reportArgumentType]
    return models, om


async def no_pending(model: str) -> dict[str, str]:
    return {}


ASSETS = {
    "sales.orders": ("lake.sales.orders", None),
    "sales.customers": ("lake.sales.customers", None),
    "sales.clients": ("lake.sales.clients", None),
    "sales.ordering": ("lake.sales.ordering", None),
    "sales.widgets": ("lake.sales.widgets", None),
}


async def run(
    tables: list[ScannedTable],
    *,
    model: str = "retail",
    turtles: dict[str, str] | None = None,
    om_tables: dict[str, OMTable] | None = None,
    pending: Mapping[str, str] | None = None,
) -> DedupeResult:
    models, om = make(turtles, om_tables)
    ops = map_scan(record(*tables), POLICY).operations
    held = dict(pending or {})

    async def pend(m: str) -> Mapping[str, str]:
        return held

    return await dedupe(
        ops,
        model=model,
        models=models,
        om=om.client,
        pending=pend,
        asset_by_table=ASSETS,
    )


def of[T: ProposalOperation](result: DedupeResult, kind: type[T]) -> list[T]:
    return [o for o in result.operations if isinstance(o, kind)]


def one_table(name: str, columns: list[str] | None = None) -> ScannedTable:
    cols = [col(c, "string", i) for i, c in enumerate(columns or ["label"])]
    return table(name, cols, comment=f"{name} data")


async def test_fr_pro_02_existing_concept_becomes_realization() -> None:
    res = await run([orders(), customers()])
    assert not of(res, AddConcept)
    real = {(o.concept, o.asset_fqn, o.column) for o in of(res, AddRealization)}
    assert ("retail.Order", "lake.sales.orders", None) in real
    assert ("retail.Customer", "lake.sales.customers", None) in real
    by_key = {m.op_key: m for m in res.matches}
    assert by_key["concept:Order"].candidate_fqn == "retail.Order"
    assert by_key["concept:Order"].confidence == 1.0
    assert by_key["concept:Order"].matched_on == "name"
    # attributes of the replaced concept stay attached to the existing one
    assert {a.concept for a in of(res, AddAttribute)} == {"Order", "Customer"}
    rel = of(res, AddRelation)
    assert [(r.source, r.target) for r in rel] == [("Order", "Customer")]


async def test_synonym_match() -> None:
    res = await run([one_table("clients")])
    assert not of(res, AddConcept)
    assert [(o.concept, o.asset_fqn) for o in of(res, AddRealization)] == [
        ("retail.Customer", "lake.sales.clients")
    ]
    m = res.matches[0]
    assert (m.candidate_fqn, m.confidence, m.matched_on) == ("retail.Customer", 0.95, "synonym")
    assert [a.concept for a in of(res, AddAttribute)] == ["Customer"]


async def test_mid_confidence_kept_with_warning() -> None:
    res = await run([one_table("ordering")])
    assert [c.name for c in of(res, AddConcept)] == ["Ordering"]
    assert not of(res, AddRealization)
    assert any("retail.Order" in w and "0.62" in w for w in res.warnings)
    assert res.matches[0].candidate_fqn == "retail.Order"
    assert 0.6 <= res.matches[0].confidence < 0.9


async def test_low_confidence_kept_silently() -> None:
    res = await run([one_table("widgets")])
    assert [c.name for c in of(res, AddConcept)] == ["Widget"]
    assert res.warnings == []
    assert res.matches == []


async def test_pending_proposal_suppresses_duplicate() -> None:
    pending = {"concept:Widget": "prop_01", "attr:Widget.label": "prop_01"}
    res = await run([one_table("widgets")], pending=pending)
    assert res.operations == []
    assert any("already proposed in prop_01" in w for w in res.warnings)


async def test_existing_attribute_dropped() -> None:
    res = await run([orders()], turtles={"retail": SOME_ATTRS})
    names = {a.name for a in of(res, AddAttribute)}
    assert "total_amount" not in names and "order_id" not in names  # IRI Order.<name>
    assert {"customer_id", "order_date", "status"} <= names


async def test_existing_attribute_column_gets_realization_unless_realized() -> None:
    tables = {
        "lake.sales.orders": om_table(
            "lake.sales.orders", ["retail.Order"], {"order_id": ["retail.Order"]}
        )
    }
    res = await run([orders()], turtles={"retail": SOME_ATTRS}, om_tables=tables)
    real = {(o.concept, o.asset_fqn, o.column) for o in of(res, AddRealization)}
    assert ("retail.Order", "lake.sales.orders", "total_amount") in real
    assert ("retail.Order", "lake.sales.orders", "order_id") not in real  # already tagged
    assert ("retail.Order", "lake.sales.orders", None) not in real  # table already realized
    assert any("already realized" in w for w in res.warnings)


async def test_already_realized_column_dropped() -> None:
    models, om = make(
        tables={"lake.sales.orders": om_table("lake.sales.orders", [], {"x": ["retail.Order"]})}
    )
    ops: list[ProposalOperation] = [
        AddRealization(
            op="add_realization", concept="retail.Order", asset_fqn="lake.sales.orders", column="x"
        ),
        AddRealization(
            op="add_realization", concept="retail.Order", asset_fqn="lake.sales.orders", column="y"
        ),
    ]
    res = await dedupe(
        ops, model="retail", models=models, om=om.client, pending=no_pending, asset_by_table={}
    )
    assert [o.column for o in of(res, AddRealization)] == ["y"]


async def test_rescan_proposes_nothing_new() -> None:
    tables = {
        "lake.sales.customers": om_table(
            "lake.sales.customers",
            ["retail.Customer"],
            {"customer_id": ["retail.Customer"], "name": ["retail.Customer"]},
        ),
        "lake.sales.orders": om_table(
            "lake.sales.orders",
            ["retail.Order"],
            {
                c: ["retail.Order"]
                for c in ("order_id", "customer_id", "order_date", "status", "total_amount")
            },
        ),
    }
    res = await run([orders(), customers()], turtles={"retail": APPLIED}, om_tables=tables)
    assert res.operations == []  # concepts, attributes, FK relation and PK axioms all deduped
    assert res.warnings == [
        "retail.Customer is already realized by lake.sales.customers",
        "retail.Order is already realized by lake.sales.orders",
    ]


async def test_enum_child_without_table_is_kept_with_warning() -> None:
    models, om = make()
    child = AddConcept(op="add_concept", name="Customer", parent="Order")
    res = await dedupe(
        [child],
        model="retail",
        models=models,
        om=om.client,
        pending=no_pending,
        asset_by_table={},
    )
    assert [c.name for c in of(res, AddConcept)] == ["Customer"]
    assert not of(res, AddRealization)
    assert any("no source table" in w for w in res.warnings)


async def test_existing_attribute_of_imported_concept_dropped_silently() -> None:
    res = await run(
        [customers()],
        model="sales",
        turtles={"sales": SALES, "retail": APPLIED},
    )
    assert not of(res, AddAttribute)  # customer_id and name exist on retail.Customer
    assert not any("attribute" in w for w in res.warnings)
    assert {(o.concept, o.column) for o in of(res, AddRealization)} >= {
        ("retail.Customer", None),
        ("retail.Customer", "customer_id"),
    }


async def test_imported_parent_dropped_and_output_accepted_by_to_om() -> None:
    models, om = make(turtles={"sales": SALES, "retail": RETAIL})
    ops: list[ProposalOperation] = [
        *mapped(customers()),
        AddConcept(op="add_concept", name="Gold", parent="Customer"),
    ]
    res = await dedupe(
        ops,
        model="sales",
        models=models,
        om=om.client,
        pending=no_pending,
        asset_by_table=ASSETS,
    )
    gold = [c for c in of(res, AddConcept) if c.name == "Gold"]
    assert gold and gold[0].parent is None
    assert any("parent retail.Customer is in an imported model" in w for w in res.warnings)
    ctx = OMContext(
        glossary_name="sales",
        glossary_id="g",
        base_iri="http://x/ontology/sales/",
        ontology_id="o",
        term_ids={c.name: draft_term_id(c) for c in of(res, AddConcept)},
        term_versions={c.name: 0.1 for c in of(res, AddConcept)},
    )
    to_om_all(res.operations, ctx)  # must not raise


async def test_qualified_ref_matches_only_own_model() -> None:
    models, om = make()
    ops: list[ProposalOperation] = [
        AddRelation(op="add_relation", source="other.Order", relation_type="x", target="Thing"),
    ]
    res = await dedupe(
        ops,
        model="retail",
        models=models,
        om=om.client,
        pending=no_pending,
        asset_by_table={},
    )
    assert [r.source for r in of(res, AddRelation)] == ["other.Order"]


async def test_relation_and_axiom_pending_keys_dropped() -> None:
    plain = await run([orders(), customers()], turtles={"retail": PREFIXES})
    rel = of(plain, AddRelation)[0]
    axiom = of(plain, AddAxiom)[0]
    res = await run(
        [orders(), customers()],
        turtles={"retail": PREFIXES},
        pending={op_key(rel): "prop_7", op_key(axiom): "prop_8"},
    )
    assert not of(res, AddRelation) or all(op_key(r) != op_key(rel) for r in of(res, AddRelation))
    assert all(op_key(a) != op_key(axiom) for a in of(res, AddAxiom))
    assert any("prop_7" in w for w in res.warnings)
    assert any("prop_8" in w for w in res.warnings)


async def test_imported_concept_realized_and_foreign_relation_dropped() -> None:
    res = await run(
        [orders(), customers()],
        model="sales",
        turtles={"sales": SALES, "retail": RETAIL},
    )
    # orders also matches retail.Order through the import
    assert not of(res, AddConcept)
    real = {o.concept for o in of(res, AddRealization)}
    assert real == {"retail.Order", "retail.Customer"}
    # ops that would point outside the model are dropped, never emitted
    assert not of(res, AddRelation)
    assert any(
        "relation to imported concept retail.Customer needs cross-model support (not in M2a)" in w
        for w in res.warnings
    )
    assert res.warnings == sorted(res.warnings)


async def test_output_is_deterministic_and_realization_follows_position() -> None:
    a = await run([orders(), customers()])
    b = await run([customers(), orders()])
    assert [op_key(o) for o in a.operations] == [op_key(o) for o in b.operations]
    keys = [op_key(o) for o in a.operations]
    assert keys[0].startswith("real:Customer")


async def test_unknown_asset_keeps_concept_with_warning() -> None:
    models, om = make()
    ops = map_scan(record(orders()), POLICY).operations
    res = await dedupe(
        ops,
        model="retail",
        models=models,
        om=om.client,
        pending=no_pending,
        asset_by_table={},
    )
    assert [c.name for c in of(res, AddConcept)] == ["Order"]
    assert any("no OM asset" in w for w in res.warnings)
