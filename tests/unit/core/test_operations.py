import pytest
from pydantic import TypeAdapter, ValidationError

from lakeloom.core.operations import (
    MAX_DESCRIPTION_CHARS,
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddMapping,
    AddRealization,
    AddRelation,
    AddSynonym,
    OMContext,
    ProposalOperation,
    RemoveAttribute,
    RemoveAxiom,
    RemoveConcept,
    RemoveMapping,
    RemoveRealization,
    RemoveRelation,
    UpdateConcept,
    draft_term_id,
    op_key,
    to_om,
)
from lakeloom.errors import LakeloomError
from lakeloom.untrusted import UntrustedText

BASE = "http://localhost:8000/ontology/retail/"
ADAPTER: TypeAdapter[ProposalOperation] = TypeAdapter(ProposalOperation)


def text(value: str) -> UntrustedText:
    return UntrustedText(text=value, source="lake comment")


CUSTOMER = AddConcept(name="Customer", description=text("A buyer"))
ORDER = AddConcept(name="Order", display_name="Order", synonyms=["Purchase"], parent="retail.Thing")
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
    term_synonyms={"retail.Order": ["Purchase"]},
    relationship_type_ids={"placedBy": "rt-1"},
    relationship_ids={"rel:Order|placedBy|Customer": "r-1"},
    native_ids={
        BASE + "Order": "nc-1",
        BASE + "Order.order_id": "np-1",
        BASE + "Order.card": "na-1",
    },
    native_versions={BASE + "Order": 0.2, BASE + "Order.order_id": 0.1, BASE + "Order.card": 0.1},
)

ALL_OPS: list[ProposalOperation] = [
    CUSTOMER,
    ORDER,
    UpdateConcept(fqn="retail.Order", base_version="0.3", description=text("d"), synonyms=["x"]),
    RemoveConcept(fqn="retail.Order", base_version="0.3"),
    AddAttribute(concept="Order", name="order_id", datatype="xsd:string", is_identifier=True),
    RemoveAttribute(concept="Order", name="order_id", base_version="0.1"),
    AddRelation(source="Order", relation_type="placedBy", target="Customer"),
    RemoveRelation(source="Order", relation_type="placedBy", target="Customer"),
    AddMapping(concept="Order", kind="closeMatch", target_iri="https://schema.org/Order"),  # type: ignore[arg-type]
    RemoveMapping(concept="Order", kind="closeMatch", target_iri="https://schema.org/Order"),  # type: ignore[arg-type]
    AddAxiom(concept="Order", axiom={"name": "card", "axiomType": "SUBCLASS_OF"}),
    RemoveAxiom(concept="Order", name="card", base_version="0.1"),
    AddSynonym(concept_fqn="retail.Order", synonym="Sale"),
    AddRealization(concept="Order", asset_fqn="a.b.c.orders", column=None),
    RemoveRealization(concept="Order", asset_fqn="a.b.c.orders", column="id"),
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
    assert draft_term_id(CUSTOMER) == draft_term_id(AddConcept(name="Customer"))
    assert draft_term_id(CUSTOMER) != draft_term_id(ORDER)


def test_update_concept() -> None:
    op = UpdateConcept(fqn="retail.Order", base_version="0.3", description=text("new"))
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
        },
    }


def test_remove_concept() -> None:
    result = to_om(RemoveConcept(fqn="retail.Order", base_version="0.3"), CTX)
    assert result.glossary == [
        {"operationType": "DELETE_TERM", "targetId": "t-order", "baseVersion": 0.3}
    ]
    assert result.native == [
        {"operationType": "DELETE_CONCEPT", "targetId": "nc-1", "baseVersion": 0.2}
    ]


def test_add_attribute_maps_to_native_property() -> None:
    op = AddAttribute(
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
    assert prop["domain"] == [BASE + "Order"]
    assert prop["range"] == ["http://www.w3.org/2001/XMLSchema#string"]
    assert prop["isIdentifier"] is False
    assert prop["enumValues"] == ["open", "closed"]
    assert prop["unit"] == "none"
    assert prop["description"] == "state"
    assert prop["ontology"] == {"id": "o-1", "type": "ontology"}


def test_identifier_attribute_without_optionals() -> None:
    op = AddAttribute(concept="Order", name="order_id", datatype="xsd:decimal", is_identifier=True)
    prop = to_om(op, CTX).native[0]["property"]
    assert prop["isIdentifier"] is True
    assert "enumValues" not in prop and "unit" not in prop and "description" not in prop


def test_remove_attribute_targets_native_property() -> None:
    result = to_om(RemoveAttribute(concept="Order", name="order_id", base_version="0.1"), CTX)
    assert result.native == [
        {"operationType": "DELETE_PROPERTY", "targetId": "np-1", "baseVersion": 0.1}
    ]
    assert result.glossary == []


def test_add_relation() -> None:
    (g,) = to_om(
        AddRelation(source="Order", relation_type="placedBy", target="Customer"), CTX
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
        to_om(AddRelation(source="Order", relation_type="nope", target="Customer"), CTX)
    assert err.value.code == "validation_error"


def test_unknown_term_is_validation_error() -> None:
    with pytest.raises(LakeloomError) as err:
        to_om(AddRelation(source="Ghost", relation_type="placedBy", target="Customer"), CTX)
    assert err.value.code == "validation_error"


def test_remove_relation() -> None:
    (g,) = to_om(
        RemoveRelation(source="Order", relation_type="placedBy", target="Customer"), CTX
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
        AddAxiom(concept="Order", axiom={"axiomType": "SUBCLASS_OF"})


def test_remove_axiom() -> None:
    result = to_om(RemoveAxiom(concept="Order", name="card", base_version="0.1"), CTX)
    assert result.native == [
        {"operationType": "DELETE_AXIOM", "targetId": "na-1", "baseVersion": 0.1}
    ]


def test_add_synonym_extends_existing() -> None:
    (g,) = to_om(AddSynonym(concept_fqn="retail.Order", synonym="Sale"), CTX).glossary
    assert g["operationType"] == "UPDATE_TERM"
    assert g["term"]["synonyms"] == ["Purchase", "Sale"]
    assert g["baseVersion"] == 0.3
    again = to_om(AddSynonym(concept_fqn="retail.Order", synonym="purchase"), CTX).glossary[0]
    assert again["term"]["synonyms"] == ["Purchase"]


def test_realizations_map_to_nothing() -> None:
    for op in ALL_OPS:
        if op.op in ("add_realization", "remove_realization"):
            result = to_om(op, CTX)
            assert result.glossary == [] and result.native == []


@pytest.mark.parametrize("bad", ["", "1Order", "Or.der", "x" * 129, "Or-der"])
def test_invalid_concept_names_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        AddConcept(name=bad)
    with pytest.raises(ValidationError):
        AddAttribute(concept="Order", name=bad, datatype="xsd:string")


@pytest.mark.parametrize("bad", ["", "1x", "has space", "a-b"])
def test_invalid_relation_types_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        AddRelation(source="a", relation_type=bad, target="b")


def test_invalid_datatype_and_discriminator_rejected() -> None:
    with pytest.raises(ValidationError):
        AddAttribute(concept="Order", name="x", datatype="string")
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"op": "nope"})


def test_ops_are_frozen() -> None:
    with pytest.raises(ValidationError):
        CUSTOMER.name = "Other"  # type: ignore[misc]


def test_description_capped_and_flagged() -> None:
    long = "x" * (MAX_DESCRIPTION_CHARS + 500)
    op = AddConcept(name="Big", description=text(long))
    assert op.description is not None
    assert len(op.description.text) == MAX_DESCRIPTION_CHARS
    flags = [e for e in op.evidence if e.truncated]
    assert len(flags) == 1 and flags[0].ref == "description"
    again = ADAPTER.validate_json(op.model_dump_json())
    assert again == op
    assert len(to_om(op, CTX).glossary[0]["term"]["description"]) == MAX_DESCRIPTION_CHARS
    short = AddConcept(name="Small", description=text("ok"))
    assert not any(e.truncated for e in short.evidence)


def test_op_key_stable() -> None:
    assert op_key(AddConcept(name="Order")) == op_key(ORDER) == "concept:Order"
    assert op_key(ALL_OPS[4]) == "attr:Order.order_id"
    assert (
        op_key(AddAttribute(concept="retail.Order", name="order_id", datatype="xsd:int"))
        == "attr:Order.order_id"
    )
    keys = [op_key(o) for o in ALL_OPS]
    assert len(set(keys)) == len(keys)
    evidence_only = AddConcept(name="Order", rule_ids=["R-1"], rationale=text("why"))
    assert op_key(evidence_only) == op_key(ORDER)
