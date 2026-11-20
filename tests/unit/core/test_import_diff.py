"""The import diff: current terms vs incoming terms as proposal operations (FR-IO-02)."""

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import RDF, RDFS, SKOS

from lakeloom.core.import_diff import ImportOps, import_operations
from lakeloom.core.import_terms import (
    AttributeTerm,
    AxiomTerm,
    ConceptTerm,
    MappingTerm,
    RelationTerm,
    TermSet,
    extract_terms,
)
from lakeloom.core.operations import (
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddHistoryNote,
    AddScopeNote,
    DeprecateConcept,
    Evidence,
    OMContext,
    OMOps,
    RemoveAxiom,
    UpdateConcept,
    draft_term_id,
    to_om_all,
)
from lakeloom.errors import LakeloomError
from lakeloom.om.native_rdf import build_graph
from lakeloom.rdf.canonical import parse_rdf
from lakeloom.rdf.semantic import semantic_view

FIXTURE = Path(__file__).parents[2] / "fixtures" / "rdf" / "retail_m3b_1.1.0.ttl"
M3B = "http://localhost:8000/ontology/retail_m3b/"
NS = "http://localhost:8000/ontology/shop/"
OM = "https://open-metadata.org/ontology/"


def concept(name: str, **kw: Any) -> ConceptTerm:
    fields: dict[str, Any] = {
        "display_name": None,
        "description": None,
        "parent": None,
        "synonyms": (),
        "iri": NS + name,
    }
    return ConceptTerm(name=name, **{**fields, **kw})


def attribute(c: str, name: str, datatype: str = "xsd:string", **kw: Any) -> AttributeTerm:
    fields: dict[str, Any] = {
        "is_identifier": False,
        "allowed_values": (),
        "unit": None,
        "description": None,
        "iri": f"{NS}{c}.{name}",
    }
    return AttributeTerm(concept=c, name=name, datatype=datatype, **{**fields, **kw})


def terms(
    concepts: list[ConceptTerm] = [],  # noqa: B006
    attributes: list[AttributeTerm] = [],  # noqa: B006
    relations: list[RelationTerm] = [],  # noqa: B006
    mappings: list[MappingTerm] = [],  # noqa: B006
    axioms: list[AxiomTerm] = [],  # noqa: B006
) -> TermSet:
    return TermSet(
        concepts={c.name: c for c in concepts},
        attributes={(a.concept, a.name): a for a in attributes},
        relations={(r.source, r.relation_type, r.target): r for r in relations},
        mappings=set(mappings),
        axioms={a.concept: a for a in axioms},
        warnings=[],
        namespace=NS,
    )


EMPTY = terms()
PK = AxiomTerm("Child", "ChildPrimaryKey", (("id", "EXACT", 1),))
FULL = terms(
    [concept("Child", parent="Parent", description="A child"), concept("Parent")],
    [attribute("Child", "id", "xsd:long", is_identifier=True), attribute("Parent", "name")],
    [RelationTerm("Child", "hasParent", "Parent", None, NS + "Child.hasParent")],
    [MappingTerm("Parent", "exactMatch", "https://schema.org/Thing")],
    [PK],
)


def run(current: TermSet, incoming: TermSet, **kw: Any) -> ImportOps:
    options: dict[str, Any] = {
        "mode": "merge",
        "versions": {},
        "axiom_names": {},
        "rule_id": "R-IMPORT",
    }
    return import_operations(current, incoming, **{**options, **kw})


def fixture_terms() -> TermSet:
    return extract_terms(parse_rdf(FIXTURE.read_bytes(), "turtle"), M3B)


def test_import_into_empty_model_adds_everything_in_order() -> None:
    result = run(EMPTY, FULL)
    assert [o.op for o in result.operations] == [
        "add_concept",
        "add_concept",
        "add_attribute",
        "add_attribute",
        "add_relation",
        "add_mapping",
        "add_axiom",
    ]
    parent, child = result.operations[:2]
    assert isinstance(parent, AddConcept) and parent.name == "Parent"
    assert isinstance(child, AddConcept) and child.name == "Child" and child.parent == "Parent"
    assert child.description is not None and child.description.text == "A child"
    axiom = result.operations[-1]
    assert isinstance(axiom, AddAxiom)
    assert axiom.axiom == {
        "name": "ChildPrimaryKey",
        "displayName": "ChildPrimaryKey",
        "description": "ChildPrimaryKey",
        "axiomType": "SUBCLASS_OF",
        "expressions": [
            {
                "kind": "RESTRICTION",
                "restrictionKind": "EXACT",
                "propertyName": "id",
                "cardinality": 1,
            }
        ],
    }
    assert result.counts == {
        "add_concept": 2,
        "add_attribute": 2,
        "add_relation": 1,
        "add_mapping": 1,
        "add_axiom": 1,
    }
    assert result.warnings == []


def test_import_emits_history_scope_and_deprecation_ops() -> None:
    current = terms()
    incoming = terms(
        [
            concept(
                "Series",
                description="A series of episodes.",
                history_notes=("Recast in 2024.",),
                scope_notes=("Editorial scope.",),
                deprecation=True,
                deprecation_reason="Series modelling will move to a dedicated model.",
            ),
        ]
    )
    result = run(current, incoming)
    assert [o.op for o in result.operations] == [
        "add_concept",
        "add_history_note",
        "add_scope_note",
    ]
    assert isinstance(result.operations[1], AddHistoryNote)
    assert result.operations[1].concept_fqn == "Series"
    assert result.operations[1].note == "Recast in 2024."
    assert isinstance(result.operations[2], AddScopeNote)
    assert result.operations[2].concept_fqn == "Series"
    assert result.operations[2].note == "Editorial scope."


def test_import_deprecates_existing_concept_with_base_version() -> None:
    current = terms([concept("Series", description="A series of episodes.")])
    incoming = terms(
        [
            concept(
                "Series",
                description="A series of episodes.",
                deprecation=True,
                deprecation_reason="Series modelling will move to a dedicated model.",
            ),
        ]
    )
    result = run(current, incoming, versions={"Series": 0.2})
    ops = result.operations
    assert [o.op for o in ops] == ["deprecate_concept"]
    (deprecation,) = [o for o in ops if isinstance(o, DeprecateConcept)]
    assert deprecation.fqn == "Series" and deprecation.base_version == "0.2"
    assert deprecation.reason == "Series modelling will move to a dedicated model."


def test_import_of_current_export_is_empty() -> None:
    result = run(fixture_terms(), fixture_terms(), mode="replace")
    assert result.operations == [] and result.counts == {} and result.warnings == []


def test_changed_description_is_update_with_base_version() -> None:
    current = terms([concept("A", description="old", synonyms=("x",))])
    incoming = terms([concept("A", description="new", display_name="Alpha", synonyms=("x",))])
    result = run(current, incoming, versions={"A": 0.3})
    (op,) = result.operations
    assert isinstance(op, UpdateConcept)
    assert op.fqn == "A" and op.base_version == "0.3"
    assert op.description is not None and op.description.text == "new"
    assert op.display_name == "Alpha" and op.synonyms is None and op.parent is None


def test_missing_version_fails_closed() -> None:
    current = terms([concept("A", description="old")])
    incoming = terms([concept("A", description="new")])
    with pytest.raises(LakeloomError) as err:
        run(current, incoming)
    assert err.value.code == "validation_error"
    assert "A" in err.value.message


def test_changed_attribute_is_upsert() -> None:
    current = terms(
        [concept("A")],
        [attribute("A", "x", description="old"), attribute("A", "y", description="old")],
    )
    incoming = terms(
        [concept("A")],
        [
            attribute(
                "A",
                "x",
                "xsd:decimal",
                allowed_values=("1", "2"),
                unit="unit:M",
                description="new",
            ),
            attribute("A", "y", description="new"),  # description only: not proposed
        ],
    )
    result = run(current, incoming)
    (op,) = result.operations
    assert isinstance(op, AddAttribute)
    assert (op.concept, op.name, op.datatype, op.allowed_values, op.unit) == (
        "A",
        "x",
        "xsd:decimal",
        ["1", "2"],
        "unit:M",
    )
    assert op.description is not None and op.description.text == "new"
    assert result.warnings == [
        "2 attribute descriptions cannot be changed on OpenMetadata 2.1.0-rc1"
    ]
    key = f"attr:{NS}A.x"
    ctx = OMContext(
        glossary_name="shop",
        glossary_id="g-1",
        base_iri=NS,
        ontology_id="o-1",
        native_ids={key: "np-1"},
        native_versions={key: 0.4},
    )
    (native,) = to_om_all(result.operations, ctx).native
    assert native["operationType"] == "UPSERT_PROPERTY"
    assert (native["targetId"], native["baseVersion"]) == ("np-1", 0.4)
    assert native["property"]["enumValues"] == [{"value": "1"}, {"value": "2"}]
    assert native["property"]["unit"] == "unit:M"


def test_merge_never_removes() -> None:
    assert run(FULL, EMPTY).operations == []
    assert run(FULL, terms([concept("Parent")])).operations == []


def test_replace_removes_in_reverse_order() -> None:
    result = run(
        FULL,
        EMPTY,
        mode="replace",
        versions={"Child": 1.1, "Parent": 2.0},
        axiom_names={"Child": ["ChildPrimaryKey"]},
    )
    assert [(o.op, getattr(o, "fqn", None)) for o in result.operations] == [
        ("remove_axiom", None),
        ("remove_mapping", None),
        ("remove_relation", None),
        ("remove_attribute", None),
        ("remove_attribute", None),
        ("remove_concept", "Child"),
        ("remove_concept", "Parent"),
    ]
    assert result.counts == {
        "remove_axiom": 1,
        "remove_mapping": 1,
        "remove_relation": 1,
        "remove_attribute": 2,
        "remove_concept": 2,
    }


def test_replace_removes_axioms_by_om_name() -> None:
    current = terms([concept("Child")], [attribute("Child", "id")], axioms=[PK])
    incoming = terms([concept("Child")], [attribute("Child", "id")])
    names = {"Child": ["ChildPrimaryKey", "Child Key 2"]}
    result = run(current, incoming, mode="replace", axiom_names=names)
    removals = [o for o in result.operations if isinstance(o, RemoveAxiom)]
    assert [(o.concept, o.name) for o in removals] == [
        ("Child", "Child Key 2"),
        ("Child", "ChildPrimaryKey"),
    ]
    with pytest.raises(LakeloomError) as err:
        run(current, incoming, mode="replace")
    assert err.value.code == "validation_error"


def test_changed_axiom_upserts_the_om_axiom() -> None:
    current = terms([concept("Child")], [attribute("Child", "id")], axioms=[PK])
    changed = AxiomTerm("Child", "ChildRestrictions", (("id", "MIN", 1),))
    incoming = terms([concept("Child")], [attribute("Child", "id")], axioms=[changed])
    result = run(current, incoming, axiom_names={"Child": ["Child_pk"]})
    (op,) = result.operations
    assert isinstance(op, AddAxiom)
    assert op.axiom["name"] == "Child_pk"
    assert op.axiom["expressions"][0]["restrictionKind"] == "MIN"


def test_rule_id_and_import_evidence_on_every_op() -> None:
    long_iri = NS + "Parent" + "x" * 2100
    incoming = terms(
        [concept("Child", parent="Parent"), concept("Parent", iri=long_iri)],
        list(FULL.attributes.values()),
        list(FULL.relations.values()),
        list(FULL.mappings),
        list(FULL.axioms.values()),
    )
    result = run(EMPTY, incoming, rule_id="R-GIT-RESTORE")
    assert result.operations
    for op in result.operations:
        assert op.rule_ids == ["R-GIT-RESTORE"]
        (evidence,) = op.evidence
        assert evidence.kind == "import"
    refs = {getattr(op, "name", None): op.evidence[0] for op in result.operations}
    assert refs["Child"] == Evidence(kind="import", ref=NS + "Child")
    assert refs["Parent"].truncated is True and len(refs["Parent"].ref) == 2000
    assert refs["id"].ref == NS + "Child.id"


def test_readding_deleted_definition_skipped_with_warning() -> None:
    deleted = [f"attr:{NS}Child.id", f"axiom:{NS}Child.ChildPrimaryKey", f"attr:{NS}Other.z"]
    result = run(EMPTY, FULL, deleted=deleted)
    names = {(o.op, getattr(o, "name", None)) for o in result.operations}
    assert ("add_attribute", "id") not in names
    assert not any(o.op == "add_axiom" for o in result.operations)
    assert ("add_attribute", "name") in names and ("add_concept", "Child") in names
    assert result.warnings == [
        "2 definitions were deleted in OpenMetadata and cannot be created again: "
        "attribute Child.id, axiom ChildPrimaryKey"
    ]
    # an existing definition is never a re-creation
    assert run(FULL, FULL, deleted=deleted).warnings == []
    # a deleted concept also leaves out what depends on it
    cascade = run(EMPTY, FULL, deleted=[f"concept:{NS}Parent"])
    assert cascade.operations == []
    assert cascade.warnings == [
        "1 definitions were deleted in OpenMetadata and cannot be created again: concept Parent",
        "6 operations that depend on them are left out",
    ]


def test_update_keeps_other_changes_when_new_parent_is_dropped() -> None:
    """Only the parent change depends on the dropped concept; label, description and
    synonym edits are still proposed."""
    current = terms([concept("A", description="old")])
    incoming = terms(
        [
            concept("A", description="new", display_name="Alpha", synonyms=("x",), parent="P"),
            concept("P"),
        ]
    )
    result = run(current, incoming, versions={"A": 0.3}, deleted=[f"concept:{NS}P"])
    (op,) = result.operations
    assert isinstance(op, UpdateConcept)
    assert op.parent is None and op.display_name == "Alpha" and op.synonyms == ["x"]
    assert op.description is not None and op.description.text == "new"
    assert result.warnings[-1] == "1 operations that depend on them are left out"
    # a parent change alone is dropped entirely
    only_parent = run(
        terms([concept("A")]),
        terms([concept("A", parent="P"), concept("P")]),
        versions={"A": 0.3},
        deleted=[f"concept:{NS}P"],
    )
    assert only_parent.operations == []


def _fake_context(ops: list[Any]) -> OMContext:
    created = [o for o in ops if isinstance(o, AddConcept)]
    return OMContext(
        glossary_name="retail_m3b",
        glossary_id="g-1",
        base_iri=M3B,
        ontology_id="o-1",
        term_ids={o.name: draft_term_id(o, "retail_m3b") for o in created},
        term_versions={o.name: 0.1 for o in created},
        relationship_type_ids={"relatedTo": "rt-1"},
    )


def test_round_trip_through_to_om() -> None:
    result = run(EMPTY, fixture_terms())
    out = to_om_all(result.operations, _fake_context(result.operations))
    native = Counter(o["operationType"] for o in out.native)
    assert native == {"UPSERT_CONCEPT": 4, "UPSERT_PROPERTY": 23, "UPSERT_AXIOM": 4}
    assert Counter(o["operationType"] for o in out.glossary) == {
        "CREATE_TERM": 4,
        "ADD_RELATIONSHIP": 2,
    }


def _mint(out: OMOps) -> Graph:
    """The model graph OM would export after applying ``out``: the glossary export (ADR 0016
    R1) plus the native members (native_rdf), without the native labels of terms."""
    glossary = Graph()
    iris = {o["term"]["id"]: o["term"]["iri"] for o in out.glossary if "term" in o}
    for o in out.glossary:
        if o["operationType"] == "CREATE_TERM":
            term = o["term"]
            node = URIRef(term["iri"])
            glossary.add((node, RDF.type, SKOS.Concept))
            glossary.add((node, RDF.type, URIRef(OM + "GlossaryTerm")))
            glossary.add((node, SKOS.prefLabel, Literal(term.get("displayName", term["name"]))))
            glossary.add((node, URIRef(OM + "fullyQualifiedName"), Literal(term["name"])))
            if "description" in term:
                glossary.add((node, SKOS.definition, Literal(term["description"])))
            for synonym in term.get("synonyms", []):
                glossary.add((node, SKOS.altLabel, Literal(synonym)))
        elif o["operationType"] == "ADD_RELATIONSHIP":
            rel = o["relationship"]
            a, b = URIRef(iris[rel["fromTerm"]["id"]]), URIRef(iris[rel["toTerm"]["id"]])
            for s, t in ((a, b), (b, a)):
                glossary.add((s, URIRef(OM + "relatedTo"), t))
                glossary.add((s, SKOS.related, t))
    native = build_graph(
        [o["concept"] for o in out.native if o["operationType"] == "UPSERT_CONCEPT"],
        [o["property"] for o in out.native if o["operationType"] == "UPSERT_PROPERTY"],
        [o["axiom"] for o in out.native if o["operationType"] == "UPSERT_AXIOM"],
    )
    for s, p, o in native:
        if p in (RDFS.label, RDFS.comment) and (s, RDF.type, SKOS.Concept) in glossary:
            continue
        glossary.add((s, p, o))
    return glossary


def test_minted_back_graph_is_semantically_equal() -> None:
    result = run(EMPTY, fixture_terms())
    minted = _mint(to_om_all(result.operations, _fake_context(result.operations)))
    original = parse_rdf(FIXTURE.read_bytes(), "turtle")
    assert isomorphic(semantic_view(minted, M3B), semantic_view(original, M3B))
    # and reading the minted graph back gives the same terms
    assert run(extract_terms(minted, M3B), fixture_terms(), mode="replace").operations == []


def test_oversized_axiom_skipped_with_warning() -> None:
    names = [f"a{i}" for i in range(300)]
    big = AxiomTerm("Child", "ChildRestrictions", tuple((n, "MIN", 1) for n in sorted(names)))
    incoming = terms([concept("Child")], [attribute("Child", n) for n in names], axioms=[big])
    result = run(EMPTY, incoming)
    assert not any(o.op == "add_axiom" for o in result.operations)
    assert result.warnings == [
        "1 axioms are too large to import (more than 16384 bytes of JSON): ChildRestrictions"
    ]


def test_moving_an_existing_concept_is_reported_not_proposed() -> None:
    """OM 2.1.0-rc1 cannot re-parent a term: the import keeps the other edits and warns."""
    result = run(
        terms([concept("A", description="old"), concept("P")]),
        terms([concept("A", description="new", parent="P"), concept("P")]),
        versions={"A": 0.3},
    )
    (op,) = result.operations
    assert isinstance(op, UpdateConcept) and op.parent is None
    assert any("change their parent and are not moved" in w and ": A" in w for w in result.warnings)
