from dataclasses import replace

import pytest
from pydantic import AnyUrl, TypeAdapter, ValidationError

from lakeloom.core.operations import (
    MAX_DESCRIPTION_CHARS,
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddMapping,
    AddRealization,
    AddRelation,
    AddSynonym,
    Evidence,
    OMContext,
    ProposalOperation,
    RemoveAttribute,
    RemoveAxiom,
    RemoveConcept,
    RemoveMapping,
    RemoveRealization,
    RemoveRelation,
    TermSnapshot,
    UpdateConcept,
    draft_term_id,
    op_key,
    to_om,
    to_om_all,
)
from lakeloom.errors import LakeloomError
from lakeloom.untrusted import UntrustedText

BASE = "http://localhost:8000/ontology/retail/"
ADAPTER: TypeAdapter[ProposalOperation] = TypeAdapter(ProposalOperation)


def text(value: str) -> UntrustedText:
    return UntrustedText(text=value, source="lake comment")


CUSTOMER = AddConcept(op="add_concept", name="Customer", description=text("A buyer"))
ORDER = AddConcept(
    op="add_concept",
    name="Order",
    display_name="Order",
    synonyms=["Purchase"],
    parent="retail.Thing",
)
CTX = OMContext(
    glossary_name="retail",
    glossary_id="g-1",
    base_iri=BASE,
    ontology_id="o-1",
    term_ids={
        "retail.Order": "t-order",
        "retail.Customer": "t-cust",
        "retail.Thing": "t-thing",
        "Order": "t-order",
        "Customer": "t-cust",
    },
    term_versions={"retail.Order": 0.3, "retail.Customer": 1.2},
    terms={
        "retail.Order": TermSnapshot(
            description="An order",
            version=0.3,
            display_name="Order X",
            iri=BASE + "Order",
            parent_id="t-thing",
            synonyms=("Purchase",),
        ),
        "retail.Customer": TermSnapshot(description="A buyer", version=1.2),
    },
    relationship_type_ids={"placedBy": "rt-1"},
    relationship_ids={"rel:Order|placedBy|Customer": "r-1"},
    native_ids={
        "concept:" + BASE + "Order": "nc-1",
        "attr:" + BASE + "Order.order_id": "np-1",
        "axiom:" + BASE + "Order.card": "na-1",
    },
    native_versions={
        "concept:" + BASE + "Order": 0.2,
        "attr:" + BASE + "Order.order_id": 0.1,
        "axiom:" + BASE + "Order.card": 0.1,
    },
)

ALL_OPS: list[ProposalOperation] = [
    CUSTOMER,
    ORDER,
    UpdateConcept(
        op="update_concept",
        fqn="retail.Order",
        base_version="0.3",
        description=text("d"),
        synonyms=["x"],
    ),
    RemoveConcept(op="remove_concept", fqn="retail.Order", base_version="0.3"),
    AddAttribute(
        op="add_attribute",
        concept="Order",
        name="order_id",
        datatype="xsd:string",
        is_identifier=True,
    ),
    RemoveAttribute(op="remove_attribute", concept="Order", name="order_id"),
    AddRelation(op="add_relation", source="Order", relation_type="placedBy", target="Customer"),
    RemoveRelation(
        op="remove_relation", source="Order", relation_type="placedBy", target="Customer"
    ),
    AddMapping(
        op="add_mapping",
        concept="Order",
        kind="closeMatch",
        target_iri=AnyUrl("https://schema.org/Order"),
    ),
    RemoveMapping(
        op="remove_mapping",
        concept="Order",
        kind="closeMatch",
        target_iri=AnyUrl("https://schema.org/Order"),
    ),
    AddAxiom(op="add_axiom", concept="Order", axiom={"name": "card", "axiomType": "SUBCLASS_OF"}),
    RemoveAxiom(op="remove_axiom", concept="Order", name="card"),
    AddSynonym(op="add_synonym", concept_fqn="retail.Order", synonym="Sale"),
    AddRealization(op="add_realization", concept="Order", asset_fqn="a.b.c.orders", column=None),
    RemoveRealization(
        op="remove_realization", concept="Order", asset_fqn="a.b.c.orders", column="id"
    ),
]


@pytest.mark.parametrize("op", ALL_OPS, ids=lambda o: o.op)
def test_json_round_trip(op: ProposalOperation) -> None:
    again = ADAPTER.validate_json(op.model_dump_json())
    assert again == op
    assert type(again) is type(op)


def test_add_concept_maps_to_term_and_native_class() -> None:
    result = to_om(ORDER, CTX)
    assert result.glossary == [
        {
            "operationType": "CREATE_TERM",
            "term": {
                "id": draft_term_id(ORDER),
                "name": "Order",
                "iri": BASE + "Order",
                "glossary": {"id": "g-1", "type": "glossary"},
                "displayName": "Order",
                "synonyms": ["Purchase"],
                "parent": {"id": "t-thing", "type": "glossaryTerm"},
            },
        }
    ]
    (native,) = result.native
    concept = native["concept"]
    assert native["operationType"] == "UPSERT_CONCEPT"
    assert concept["iri"] == BASE + "Order" == result.glossary[0]["term"]["iri"]
    assert concept["conceptType"] == "OWL_CLASS"
    assert concept["fullyQualifiedName"] == "retail.Order"
    assert concept["ontology"] == {"id": "o-1", "type": "ontology"}
    assert concept["name"] == "Order"
    assert "targetId" not in native


def test_add_concept_description_on_both_sides() -> None:
    result = to_om(CUSTOMER, CTX)
    assert result.glossary[0]["term"]["description"] == "A buyer"
    assert result.native[0]["concept"]["description"] == "A buyer"


def test_draft_term_id_is_deterministic() -> None:
    assert draft_term_id(CUSTOMER) == draft_term_id(AddConcept(op="add_concept", name="Customer"))
    assert draft_term_id(CUSTOMER) != draft_term_id(ORDER)


def test_update_concept() -> None:
    op = UpdateConcept(
        op="update_concept", fqn="retail.Order", base_version="0.3", description=text("new")
    )
    (g,) = to_om(op, CTX).glossary
    assert g == {
        "operationType": "UPDATE_TERM",
        "targetId": "t-order",
        "baseVersion": 0.3,
        "term": {
            "id": "t-order",
            "name": "Order",
            "glossary": {"id": "g-1", "type": "glossary"},
            "description": "new",
            "displayName": "Order X",
            "iri": BASE + "Order",
            "parent": {"id": "t-thing", "type": "glossaryTerm"},
            "synonyms": ["Purchase"],
        },
    }


def test_remove_concept() -> None:
    result = to_om(RemoveConcept(op="remove_concept", fqn="retail.Order", base_version="0.3"), CTX)
    assert result.glossary == [
        {"operationType": "DELETE_TERM", "targetId": "t-order", "baseVersion": 0.3}
    ]
    assert result.native == [
        {"operationType": "DELETE_CONCEPT", "targetId": "nc-1", "baseVersion": 0.2}
    ]


def test_add_attribute_maps_to_native_property() -> None:
    op = AddAttribute(
        op="add_attribute",
        concept="retail.Order",
        name="status",
        datatype="xsd:string",
        allowed_values=["open", "closed"],
        unit="none",
        description=text("state"),
    )
    result = to_om(op, CTX)
    assert result.glossary == []
    (native,) = result.native
    prop = native["property"]
    assert native["operationType"] == "UPSERT_PROPERTY"
    assert prop["kind"] == "DATA"
    assert prop["iri"] == BASE + "Order.status"
    assert prop["name"] == "Order.status"
    assert prop["domain"] == [BASE + "Order"]
    assert prop["range"] == ["http://www.w3.org/2001/XMLSchema#string"]
    assert prop["isIdentifier"] is False
    assert prop["enumValues"] == ["open", "closed"]
    assert prop["unit"] == "none"
    assert prop["description"] == "state"
    assert prop["ontology"] == {"id": "o-1", "type": "ontology"}


def test_identifier_attribute_without_optionals() -> None:
    op = AddAttribute(
        op="add_attribute",
        concept="Order",
        name="order_id",
        datatype="xsd:decimal",
        is_identifier=True,
    )
    prop = to_om(op, CTX).native[0]["property"]
    assert prop["isIdentifier"] is True
    assert "enumValues" not in prop and "unit" not in prop and "description" not in prop


def test_remove_attribute_targets_native_property() -> None:
    result = to_om(RemoveAttribute(op="remove_attribute", concept="Order", name="order_id"), CTX)
    assert result.native == [
        {"operationType": "DELETE_PROPERTY", "targetId": "np-1", "baseVersion": 0.1}
    ]
    assert result.glossary == []


def test_add_relation() -> None:
    (g,) = to_om(
        AddRelation(op="add_relation", source="Order", relation_type="placedBy", target="Customer"),
        CTX,
    ).glossary
    assert g["operationType"] == "ADD_RELATIONSHIP"
    assert g["targetId"] == "t-order"
    assert g["baseVersion"] == 0.3
    rel = g["relationship"]
    assert rel["fromTerm"] == {"id": "t-order", "type": "glossaryTerm"}
    assert rel["toTerm"] == {"id": "t-cust", "type": "glossaryTerm"}
    assert rel["relationshipType"] == {"id": "rt-1", "type": "relationshipType"}
    assert (rel["provenance"], rel["status"], rel["createdAt"], rel["createdBy"]) == (
        "Manual",
        "Draft",
        0,
        "lakeloom",
    )
    assert rel["id"]


def test_add_relation_unknown_type_is_validation_error() -> None:
    with pytest.raises(LakeloomError) as err:
        to_om(
            AddRelation(op="add_relation", source="Order", relation_type="nope", target="Customer"),
            CTX,
        )
    assert err.value.code == "validation_error"


def test_unknown_term_is_validation_error() -> None:
    with pytest.raises(LakeloomError) as err:
        to_om(
            AddRelation(
                op="add_relation", source="Ghost", relation_type="placedBy", target="Customer"
            ),
            CTX,
        )
    assert err.value.code == "validation_error"


def test_remove_relation() -> None:
    (g,) = to_om(
        RemoveRelation(
            op="remove_relation", source="Order", relation_type="placedBy", target="Customer"
        ),
        CTX,
    ).glossary
    assert g == {"operationType": "DELETE_RELATIONSHIP", "targetId": "r-1", "baseVersion": 0.3}


@pytest.mark.parametrize(
    ("kind", "om"),
    [
        ("exactMatch", "EXACT_MATCH"),
        ("closeMatch", "CLOSE_MATCH"),
        ("broadMatch", "BROAD_MATCH"),
        ("narrowMatch", "NARROW_MATCH"),
        ("relatedMatch", "RELATED_MATCH"),
    ],
)
def test_add_mapping(kind: str, om: str) -> None:
    op = ADAPTER.validate_python(
        {
            "op": "add_mapping",
            "concept": "Order",
            "kind": kind,
            "target_iri": "https://schema.org/Order",
        }
    )
    (g,) = to_om(op, CTX).glossary
    assert g == {
        "operationType": "UPSERT_MAPPING",
        "targetId": "t-order",
        "baseVersion": 0.3,
        "mapping": {"conceptIri": "https://schema.org/Order", "mappingType": om},
    }


def test_remove_mapping() -> None:
    (g,) = to_om(ALL_OPS[9], CTX).glossary
    assert g["operationType"] == "DELETE_MAPPING"
    assert g["mapping"]["mappingType"] == "CLOSE_MATCH"


def test_add_axiom_maps_to_native_axiom() -> None:
    op = AddAxiom(
        op="add_axiom",
        concept="Order",
        axiom={
            "name": "orderHasOneId",
            "axiomType": "SUBCLASS_OF",
            "expressions": [{"kind": "RESTRICTION", "restrictionKind": "EXACT", "cardinality": 1}],
        },
    )
    result = to_om(op, CTX)
    assert result.glossary == []
    (native,) = result.native
    axiom = native["axiom"]
    assert native["operationType"] == "UPSERT_AXIOM"
    assert axiom["subjectIri"] == BASE + "Order"
    assert axiom["fullyQualifiedName"] == "retail.orderHasOneId"
    assert axiom["provenance"] == "Manual"
    assert axiom["ontology"] == {"id": "o-1", "type": "ontology"}
    assert axiom["axiomType"] == "SUBCLASS_OF"
    assert axiom["expressions"][0]["cardinality"] == 1


def test_axiom_requires_name() -> None:
    with pytest.raises(ValidationError):
        AddAxiom(op="add_axiom", concept="Order", axiom={"axiomType": "SUBCLASS_OF"})


def test_remove_axiom() -> None:
    result = to_om(RemoveAxiom(op="remove_axiom", concept="Order", name="card"), CTX)
    assert result.native == [
        {"operationType": "DELETE_AXIOM", "targetId": "na-1", "baseVersion": 0.1}
    ]


def test_add_synonym_extends_existing() -> None:
    (g,) = to_om(
        AddSynonym(op="add_synonym", concept_fqn="retail.Order", synonym="Sale"), CTX
    ).glossary
    assert g["operationType"] == "UPDATE_TERM"
    assert g["term"]["synonyms"] == ["Purchase", "Sale"]
    assert g["baseVersion"] == 0.3
    again = to_om(
        AddSynonym(op="add_synonym", concept_fqn="retail.Order", synonym="purchase"), CTX
    ).glossary[0]
    assert again["term"]["synonyms"] == ["Purchase"]


def test_realizations_map_to_nothing() -> None:
    for op in ALL_OPS:
        if op.op in ("add_realization", "remove_realization"):
            result = to_om(op, CTX)
            assert result.glossary == [] and result.native == []


@pytest.mark.parametrize("bad", ["", "1Order", "Or.der", "x" * 129, "Or-der"])
def test_invalid_concept_names_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        AddConcept(op="add_concept", name=bad)
    with pytest.raises(ValidationError):
        AddAttribute(op="add_attribute", concept="Order", name=bad, datatype="xsd:string")


@pytest.mark.parametrize("bad", ["", "1x", "has space", "a-b"])
def test_invalid_relation_types_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        AddRelation(op="add_relation", source="a", relation_type=bad, target="b")


def test_invalid_datatype_and_discriminator_rejected() -> None:
    with pytest.raises(ValidationError):
        AddAttribute(op="add_attribute", concept="Order", name="x", datatype="string")
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"op": "nope"})


def test_ops_are_frozen() -> None:
    with pytest.raises(ValidationError):
        CUSTOMER.name = "Other"  # type: ignore[misc]


def test_description_capped_and_flagged() -> None:
    long = "x" * (MAX_DESCRIPTION_CHARS + 500)
    op = AddConcept(op="add_concept", name="Big", description=text(long))
    assert op.description is not None
    assert len(op.description.text) == MAX_DESCRIPTION_CHARS
    flags = [e for e in op.evidence if e.truncated]
    assert len(flags) == 1 and flags[0].ref == "description"
    again = ADAPTER.validate_json(op.model_dump_json())
    assert again == op
    assert len(to_om(op, CTX).glossary[0]["term"]["description"]) == MAX_DESCRIPTION_CHARS
    short = AddConcept(op="add_concept", name="Small", description=text("ok"))
    assert not any(e.truncated for e in short.evidence)


def test_op_key_stable() -> None:
    assert op_key(AddConcept(op="add_concept", name="Order")) == op_key(ORDER) == "concept:Order"
    assert op_key(ALL_OPS[4]) == "attr:Order.order_id"
    assert (
        op_key(
            AddAttribute(
                op="add_attribute", concept="retail.Order", name="order_id", datatype="xsd:int"
            )
        )
        == "attr:Order.order_id"
    )
    keys = [op_key(o) for o in ALL_OPS]
    assert len(set(keys)) == len(keys)
    evidence_only = AddConcept(
        op="add_concept", name="Order", rule_ids=["R-1"], rationale=text("why")
    )
    assert op_key(evidence_only) == op_key(ORDER)


def syn(term: str, word: str) -> AddSynonym:
    return AddSynonym(op="add_synonym", concept_fqn=term, synonym=word)


def test_to_om_all_folds_term_updates_into_one_update_term() -> None:
    ops: list[ProposalOperation] = [
        syn("retail.Order", "Sale"),
        UpdateConcept(op="update_concept", fqn="Order", base_version="0.3", display_name="Ord"),
        syn("Order", "Deal"),
        syn("Customer", "Buyer"),
    ]
    result = to_om_all(ops, CTX)
    updates = result.glossary
    assert [g["targetId"] for g in updates] == ["t-order", "t-cust"]
    order = updates[0]["term"]
    assert order["synonyms"] == ["Purchase", "Sale", "Deal"]
    assert order["displayName"] == "Ord"
    assert order["description"] == "An order" and order["iri"] == BASE + "Order"
    assert updates[1]["term"]["synonyms"] == ["Buyer"]
    assert updates[1]["baseVersion"] == 1.2


def test_to_om_all_declares_first_then_keeps_order_of_other_ops() -> None:
    rel = AddRelation(
        op="add_relation", source="Order", relation_type="placedBy", target="Customer"
    )
    result = to_om_all([syn("Order", "Sale"), rel, CUSTOMER], CTX)
    assert [g["operationType"] for g in result.glossary] == [
        "CREATE_TERM",
        "UPDATE_TERM",
        "ADD_RELATIONSHIP",
    ]
    assert [n["operationType"] for n in result.native] == ["UPSERT_CONCEPT", "UPSERT_PROPERTY"]


def test_synonym_on_concept_created_in_batch_is_folded_into_create() -> None:
    result = to_om_all([CUSTOMER, syn("Customer", "Buyer")], CTX)
    assert [g["operationType"] for g in result.glossary] == ["CREATE_TERM"]
    assert result.glossary[0]["term"]["synonyms"] == ["Buyer"]
    update = UpdateConcept(op="update_concept", fqn="Customer", base_version="0.1")
    with pytest.raises(LakeloomError):
        to_om_all([CUSTOMER, update], CTX)


def test_term_update_without_snapshot_fails_closed() -> None:
    with pytest.raises(LakeloomError) as err:
        to_om(syn("Thing", "x"), CTX)
    assert err.value.code == "validation_error"
    ctx = OMContext(**{**CTX.__dict__, "terms": {}})
    with pytest.raises(LakeloomError):
        to_om(syn("Order", "x"), ctx)


@pytest.mark.parametrize("ref", ["finance.Customer", "other.Order.x"])
def test_foreign_model_reference_rejected(ref: str) -> None:
    rel = AddRelation(op="add_relation", source="Order", relation_type="placedBy", target=ref)
    with pytest.raises(LakeloomError) as err:
        to_om(rel, CTX)
    assert err.value.code == "validation_error" and "import" in err.value.hint
    with pytest.raises(LakeloomError):
        to_om(AddAttribute(op="add_attribute", concept=ref, name="x", datatype="xsd:int"), CTX)
    with pytest.raises(LakeloomError):
        to_om(syn(ref, "x"), CTX)


def test_remove_concept_fails_closed_without_native_class() -> None:
    ctx = OMContext(**{**CTX.__dict__, "native_ids": {}})
    with pytest.raises(LakeloomError):
        to_om(RemoveConcept(op="remove_concept", fqn="Order", base_version="0.3"), ctx)


def test_native_keys_are_namespaced() -> None:
    ctx = OMContext(
        **{
            **CTX.__dict__,
            "native_ids": {"axiom:" + BASE + "Order.order_id": "x"},
            "native_versions": {"axiom:" + BASE + "Order.order_id": 0.1},
        }
    )
    with pytest.raises(LakeloomError):
        to_om(RemoveAttribute(op="remove_attribute", concept="Order", name="order_id"), ctx)


def test_op_is_required_and_in_schema() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"name": "Order"})
    with pytest.raises(ValidationError):
        AddConcept.model_validate({"name": "Order"})
    assert "op" in AddConcept.model_json_schema()["required"]


def test_field_caps() -> None:
    too_many = [f"s{i}" for i in range(101)]
    with pytest.raises(ValidationError):
        AddConcept(op="add_concept", name="X", synonyms=too_many)
    with pytest.raises(ValidationError):
        AddConcept(op="add_concept", name="X", synonyms=["y" * 257])
    with pytest.raises(ValidationError):
        AddConcept(op="add_concept", name="X", display_name="d" * 257)
    with pytest.raises(ValidationError):
        AddAttribute(
            op="add_attribute", concept="X", name="y", datatype="xsd:int", allowed_values=too_many
        )
    with pytest.raises(ValidationError):
        AddAttribute(op="add_attribute", concept="X", name="y", datatype="xsd:int", unit="u" * 65)
    with pytest.raises(ValidationError):
        Evidence(kind="agent", ref="r" * 2001)
    with pytest.raises(ValidationError):
        Evidence(kind="agent", ref="r", detail="d" * 2001)


def test_cap_text_robust_to_none() -> None:
    long = {"text": "x" * 5000, "source": "s"}
    with pytest.raises(ValidationError):
        AddConcept.model_validate(
            {"op": "add_concept", "name": "X", "description": long, "evidence": None}
        )
    with pytest.raises(ValidationError):
        AddConcept.model_validate(
            {
                "op": "add_concept",
                "name": "X",
                "description": None,
                "rationale": None,
                "evidence": None,
            }
        )


def test_relation_and_mapping_from_a_term_created_in_the_batch_have_no_base_version() -> None:
    """Live rc1 (ADR 0011): "baseVersion must be absent for a term created in this change set"."""
    widget = AddConcept(op="add_concept", name="Widget", description=text("w"))
    ctx = replace(
        CTX,
        term_ids={**CTX.term_ids, "Widget": draft_term_id(widget)},
        term_versions={**CTX.term_versions, "Widget": 0.1},
    )
    relation = AddRelation(
        op="add_relation", source="Widget", relation_type="placedBy", target="Customer"
    )
    mapping = AddMapping(
        op="add_mapping", concept="Widget", kind="closeMatch", target_iri=AnyUrl("https://s.org/W")
    )
    existing = AddRelation(
        op="add_relation", source="Order", relation_type="placedBy", target="Widget"
    )
    out = to_om_all([widget, relation, mapping, existing], ctx).glossary
    by_type = {o["operationType"]: o for o in out if o["operationType"] != "ADD_RELATIONSHIP"}
    rels = [o for o in out if o["operationType"] == "ADD_RELATIONSHIP"]
    assert "baseVersion" not in rels[0] and rels[0]["targetId"] == draft_term_id(widget)
    assert "baseVersion" not in by_type["UPSERT_MAPPING"]
    assert rels[1]["baseVersion"] == 0.3  # from an existing term: still versioned


def test_relation_is_also_a_native_object_property() -> None:
    rel = AddRelation(
        op="add_relation", source="Order", relation_type="placedBy", target="Customer"
    )
    (n,) = to_om(rel, CTX).native
    assert n["operationType"] == "UPSERT_PROPERTY"
    prop = n["property"]
    assert prop["kind"] == "OBJECT"
    assert prop["name"] == "Order.placedBy" and prop["displayName"] == "placedBy"
    assert prop["iri"] == BASE + "Order.placedBy"
    assert prop["domain"] == [BASE + "Order"] and prop["range"] == [BASE + "Customer"]
    assert prop["ontology"] == {"id": "o-1", "type": "ontology"}
    assert to_om(rel, replace(CTX, paired=False)).native == []


def test_unknown_relationship_type_falls_back_to_related_to_on_the_glossary() -> None:
    ctx = replace(CTX, relationship_type_ids={**CTX.relationship_type_ids, "relatedTo": "rt-rel"})
    rel = AddRelation(
        op="add_relation", source="Order", relation_type="hasBuyer", target="Customer"
    )
    out = to_om(rel, ctx)
    assert out.glossary[0]["relationship"]["relationshipType"]["id"] == "rt-rel"
    assert out.native[0]["property"]["name"] == "Order.hasBuyer"  # intended name kept


def test_remove_relation_deletes_known_object_property() -> None:
    key = "attr:" + BASE + "Order.placedBy"
    ctx = replace(
        CTX,
        native_ids={**CTX.native_ids, key: "np-9"},
        native_versions={**CTX.native_versions, key: 0.4},
    )
    rel = RemoveRelation(
        op="remove_relation", source="Order", relation_type="placedBy", target="Customer"
    )
    assert to_om(rel, ctx).native == [
        {"operationType": "DELETE_PROPERTY", "targetId": "np-9", "baseVersion": 0.4}
    ]


def test_append_to_change_set_with_draft_term_strips_base_version() -> None:
    widget = AddConcept(op="add_concept", name="Widget", description=text("w"))
    ctx = replace(
        CTX,
        term_ids={**CTX.term_ids, "Widget": draft_term_id(widget)},
        term_versions={**CTX.term_versions, "Widget": 0.1},
    )
    rel = AddRelation(op="add_relation", source="Widget", relation_type="placedBy", target="Order")
    assert to_om_all([rel], ctx).glossary[0]["baseVersion"] == 0.1
    (g,) = to_om_all([rel], ctx, in_change_set={"Widget"}).glossary
    assert "baseVersion" not in g


def test_terms_and_classes_are_declared_before_operations_that_reference_them() -> None:
    """Live rc1 (M2a Task 7): "a referenced term must be created by an earlier operation".

    One part can hold a relation whose target table sorts before it (``Invoice hasOrder
    Order``): every CREATE_TERM / UPSERT_CONCEPT goes first, in proposal order.
    """
    invoice = AddConcept(op="add_concept", name="Invoice", description=text("i"))
    widget = AddConcept(op="add_concept", name="Widget", description=text("w"))
    ctx = replace(
        CTX,
        term_ids={
            **CTX.term_ids,
            "Invoice": draft_term_id(invoice),
            "Widget": draft_term_id(widget),
        },
        term_versions={**CTX.term_versions, "Invoice": 0.1, "Widget": 0.1},
    )
    early = AddRelation(
        op="add_relation", source="Invoice", relation_type="placedBy", target="Widget"
    )
    attr = AddAttribute(op="add_attribute", concept="Widget", name="size", datatype="xsd:int")
    out = to_om_all([invoice, early, widget, attr], ctx)
    assert [o["operationType"] for o in out.glossary] == [
        "CREATE_TERM",
        "CREATE_TERM",
        "ADD_RELATIONSHIP",
    ]
    assert [o["term"]["name"] for o in out.glossary[:2]] == ["Invoice", "Widget"]
    kinds = [o["operationType"] for o in out.native]
    assert kinds[:2] == ["UPSERT_CONCEPT", "UPSERT_CONCEPT"]
    assert sorted(kinds[2:]) == ["UPSERT_PROPERTY", "UPSERT_PROPERTY"]


def test_nested_fqn_of_own_model_is_local() -> None:
    """``retail.Order.OrderStatus`` (a child term's FQN) is a ref into the model itself."""
    nested = f"{CTX.glossary_name}.Order.OrderStatus"
    attr = AddAttribute(op="add_attribute", concept=nested, name="code", datatype="xsd:string")
    (item,) = to_om(attr, CTX).native
    assert item["property"]["domain"] == [BASE + "OrderStatus"]
