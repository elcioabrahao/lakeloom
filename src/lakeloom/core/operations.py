"""Typed proposal operations and their mapping to OM change-set operations (FR-PRO-05).

A proposal is a list of :data:`ProposalOperation`. :func:`to_om` turns one into the
payloads OM 2.1.0-rc1 accepts (ADR 0011): glossary-change-set operations for terms,
relationships and mappings, and native-ontology operations for classes, properties and
axioms. Lakeloom models are hybrid (Amendment A): a glossary plus a paired native
ontology whose classes reuse the glossary terms' IRIs. Realizations are Lakeloom-side only.

``to_om`` is pure: every id, version and IRI base it needs comes from :class:`OMContext`.
``id`` and ``state`` of each operation are filled later by ``OMClient``.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, model_validator

from lakeloom.errors import LakeloomError
from lakeloom.untrusted import UntrustedText

__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "AddAttribute",
    "AddAxiom",
    "AddConcept",
    "AddMapping",
    "AddRealization",
    "AddRelation",
    "AddSynonym",
    "Evidence",
    "OMContext",
    "OMOps",
    "ProposalOperation",
    "RemoveAttribute",
    "RemoveAxiom",
    "RemoveConcept",
    "RemoveMapping",
    "RemoveRealization",
    "RemoveRelation",
    "UpdateConcept",
    "draft_term_id",
    "op_key",
    "to_om",
]

MAX_DESCRIPTION_CHARS = 4000
XSD = "http://www.w3.org/2001/XMLSchema#"
_NAME = r"^[A-Za-z][A-Za-z0-9_ ]{0,127}$"
_RELATION = r"^[a-zA-Z][a-zA-Z0-9]*$"
_NS = uuid.UUID("6f1b3c1e-5a0e-4b0a-9a3e-4c6d2f7a9b10")

Name = Annotated[str, Field(pattern=_NAME)]
Ref = Annotated[str, Field(min_length=1, max_length=512)]
RelationType = Annotated[str, Field(pattern=_RELATION)]
Version = Annotated[str, Field(pattern=r"^\d+(\.\d+)?$")]
MappingKind = Literal["exactMatch", "closeMatch", "broadMatch", "narrowMatch", "relatedMatch"]
_MAPPING_TYPE: dict[str, str] = {
    "exactMatch": "EXACT_MATCH",
    "closeMatch": "CLOSE_MATCH",
    "broadMatch": "BROAD_MATCH",
    "narrowMatch": "NARROW_MATCH",
    "relatedMatch": "RELATED_MATCH",
}


class Evidence(BaseModel):
    """One piece of evidence behind an operation (FR-PRO-03)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["column", "table", "fk", "pk", "profile", "tag", "comment", "match", "agent"]
    ref: str
    detail: str | None = None
    truncated: bool = False


class _Op(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_ids: list[str] = Field(default_factory=list[str])
    evidence: list[Evidence] = Field(default_factory=list[Evidence])
    rationale: UntrustedText | None = None

    @model_validator(mode="before")
    @classmethod
    def _cap_text(cls, data: Any) -> Any:
        """Cap description/rationale at 4,000 chars and flag the cut in the evidence."""
        if not isinstance(data, dict):
            return data
        out: dict[str, Any] = dict(cast("dict[str, Any]", data))
        flags: list[Evidence] = []
        for field_name in ("description", "rationale"):
            raw: Any = out.get(field_name)
            value = raw.model_dump() if isinstance(raw, UntrustedText) else raw
            if isinstance(value, dict):
                value = cast("dict[str, Any]", value)
                full = str(value.get("text", ""))
                if len(full) <= MAX_DESCRIPTION_CHARS:
                    continue
                out[field_name] = {**value, "text": full[:MAX_DESCRIPTION_CHARS]}
                flags.append(
                    Evidence(
                        kind="comment",
                        ref=field_name,
                        detail=f"{field_name} truncated to {MAX_DESCRIPTION_CHARS} characters",
                        truncated=True,
                    )
                )
        if flags:
            out["evidence"] = [*out.get("evidence", []), *flags]
        return out


class AddConcept(_Op):
    op: Literal["add_concept"] = "add_concept"
    name: Name
    display_name: str | None = None
    description: UntrustedText | None = None
    parent: Ref | None = None
    synonyms: list[str] = Field(default_factory=list)


class UpdateConcept(_Op):
    """Changes to an existing concept; ``None`` leaves a field alone."""

    op: Literal["update_concept"] = "update_concept"
    fqn: Ref
    base_version: Version
    description: UntrustedText | None = None
    display_name: str | None = None
    synonyms: list[str] | None = None
    parent: Ref | None = None


class RemoveConcept(_Op):
    op: Literal["remove_concept"] = "remove_concept"
    fqn: Ref
    base_version: Version


class AddAttribute(_Op):
    op: Literal["add_attribute"] = "add_attribute"
    concept: Ref
    name: Name
    datatype: Annotated[str, Field(pattern=r"^xsd:[A-Za-z]+$")]
    is_identifier: bool = False
    allowed_values: list[str] = Field(default_factory=list)
    unit: str | None = None
    description: UntrustedText | None = None


class RemoveAttribute(_Op):
    op: Literal["remove_attribute"] = "remove_attribute"
    concept: Ref
    name: Name
    base_version: Version


class AddRelation(_Op):
    op: Literal["add_relation"] = "add_relation"
    source: Ref
    relation_type: RelationType
    target: Ref
    description: UntrustedText | None = None


class RemoveRelation(_Op):
    op: Literal["remove_relation"] = "remove_relation"
    source: Ref
    relation_type: RelationType
    target: Ref


class AddMapping(_Op):
    op: Literal["add_mapping"] = "add_mapping"
    concept: Ref
    kind: MappingKind
    target_iri: AnyUrl


class RemoveMapping(_Op):
    op: Literal["remove_mapping"] = "remove_mapping"
    concept: Ref
    kind: MappingKind
    target_iri: AnyUrl


class AddAxiom(_Op):
    """``axiom`` is an OM ``OntologyAxiom`` body; it needs a ``name`` (validated in OM, Task 6)."""

    op: Literal["add_axiom"] = "add_axiom"
    concept: Ref
    axiom: dict[str, Any]

    @model_validator(mode="after")
    def _named(self) -> "AddAxiom":
        if not isinstance(self.axiom.get("name"), str) or not self.axiom["name"]:
            raise ValueError("axiom needs a non-empty string 'name'")
        return self


class RemoveAxiom(_Op):
    op: Literal["remove_axiom"] = "remove_axiom"
    concept: Ref
    name: Annotated[str, Field(min_length=1, max_length=256)]
    base_version: Version


class AddSynonym(_Op):
    op: Literal["add_synonym"] = "add_synonym"
    concept_fqn: Ref
    synonym: Annotated[str, Field(min_length=1, max_length=256)]


class AddRealization(_Op):
    op: Literal["add_realization"] = "add_realization"
    concept: Ref
    asset_fqn: Ref
    column: str | None = None


class RemoveRealization(_Op):
    op: Literal["remove_realization"] = "remove_realization"
    concept: Ref
    asset_fqn: Ref
    column: str | None = None


ProposalOperation = Annotated[
    AddConcept
    | UpdateConcept
    | RemoveConcept
    | AddAttribute
    | RemoveAttribute
    | AddRelation
    | RemoveRelation
    | AddMapping
    | RemoveMapping
    | AddAxiom
    | RemoveAxiom
    | AddSynonym
    | AddRealization
    | RemoveRealization,
    Field(discriminator="op"),
]


@dataclass(frozen=True, kw_only=True)
class OMContext:
    """Everything :func:`to_om` needs to know about OM, supplied by the caller (no I/O).

    ``term_ids``/``term_versions``/``term_synonyms`` are keyed by term FQN or bare name;
    terms created in the same proposal are registered by the caller with
    :func:`draft_term_id`. ``native_ids``/``native_versions`` are keyed by IRI (existing
    native classes, properties and axioms, needed only for removals);
    ``relationship_ids`` by :func:`op_key` of the relation op.
    """

    glossary_name: str
    glossary_id: str
    base_iri: str
    ontology_id: str
    ontology_name: str | None = None
    term_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    term_versions: Mapping[str, float] = field(default_factory=dict[str, float])
    term_synonyms: Mapping[str, Sequence[str]] = field(default_factory=dict[str, Sequence[str]])
    relationship_type_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    relationship_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    native_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    native_versions: Mapping[str, float] = field(default_factory=dict[str, float])


@dataclass(frozen=True)
class OMOps:
    """OM operations for one proposal operation: glossary change set and native change set."""

    glossary: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    native: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


def _bare(ref: str) -> str:
    return ref.rsplit(".", 1)[-1]


def op_key(op: ProposalOperation) -> str:
    """Stable identity of an operation, for dedupe and idempotence."""
    match op:
        case AddConcept():
            return f"concept:{op.name}"
        case UpdateConcept():
            return f"update-concept:{_bare(op.fqn)}"
        case RemoveConcept():
            return f"remove-concept:{_bare(op.fqn)}"
        case AddAttribute():
            return f"attr:{_bare(op.concept)}.{op.name}"
        case RemoveAttribute():
            return f"remove-attr:{_bare(op.concept)}.{op.name}"
        case AddRelation():
            return f"rel:{_bare(op.source)}|{op.relation_type}|{_bare(op.target)}"
        case RemoveRelation():
            return f"remove-rel:{_bare(op.source)}|{op.relation_type}|{_bare(op.target)}"
        case AddMapping():
            return f"map:{_bare(op.concept)}|{op.kind}|{op.target_iri}"
        case RemoveMapping():
            return f"remove-map:{_bare(op.concept)}|{op.kind}|{op.target_iri}"
        case AddAxiom():
            return f"axiom:{_bare(op.concept)}.{op.axiom['name']}"
        case RemoveAxiom():
            return f"remove-axiom:{_bare(op.concept)}.{op.name}"
        case AddSynonym():
            return f"syn:{_bare(op.concept_fqn)}|{op.synonym.casefold()}"
        case AddRealization():
            return f"real:{_bare(op.concept)}|{op.asset_fqn}|{op.column or ''}"
        case RemoveRealization():
            return f"remove-real:{_bare(op.concept)}|{op.asset_fqn}|{op.column or ''}"


def _uid(kind: str, key: str) -> str:
    return str(uuid.uuid5(_NS, f"{kind}:{key}"))


def draft_term_id(op: AddConcept) -> str:
    """Deterministic draft id of the term an ``AddConcept`` creates."""
    return _uid("term", op_key(op))


def _fail(message: str, hint: str) -> LakeloomError:
    return LakeloomError("validation_error", message, hint)


def _find[V](table: Mapping[str, V], ctx: OMContext, ref: str) -> V | None:
    for candidate in (ref, _bare(ref), f"{ctx.glossary_name}.{_bare(ref)}"):
        if candidate in table:
            return table[candidate]
    return None


def _lookup[V](table: Mapping[str, V], ctx: OMContext, ref: str, what: str) -> V:
    found = _find(table, ctx, ref)
    if found is None:
        raise _fail(
            f"no {what} known for {ref!r}",
            f"resolve {what} for this term before building the operation "
            "(register terms created in the same proposal with draft_term_id)",
        )
    return found


def _concept_iri(ctx: OMContext, ref: str) -> str:
    return f"{ctx.base_iri}{quote(_bare(ref), safe='')}"


def _term_ref(ctx: OMContext, ref: str) -> dict[str, str]:
    return {"id": _lookup(ctx.term_ids, ctx, ref, "term id"), "type": "glossaryTerm"}


def _glossary_ref(ctx: OMContext) -> dict[str, str]:
    return {"id": ctx.glossary_id, "type": "glossary"}


def _ontology_ref(ctx: OMContext) -> dict[str, str]:
    return {"id": ctx.ontology_id, "type": "ontology"}


def _ontology_name(ctx: OMContext) -> str:
    return ctx.ontology_name or ctx.glossary_name


def _text(value: UntrustedText | None) -> str | None:
    return None if value is None else value.text


def to_om(op: ProposalOperation, ctx: OMContext) -> OMOps:
    """Map one proposal operation to OM operations (see ADR 0011 for the payloads)."""
    match op:
        case AddConcept():
            return _add_concept(op, ctx)
        case UpdateConcept():
            return _update_concept(op, ctx)
        case RemoveConcept():
            return _remove_concept(op, ctx)
        case AddAttribute():
            return _add_attribute(op, ctx)
        case RemoveAttribute():
            iri = f"{ctx.base_iri}{quote(_bare(op.concept), safe='')}.{quote(op.name, safe='')}"
            return OMOps(native=[_native_delete("DELETE_PROPERTY", iri, ctx)])
        case AddRelation():
            return _add_relation(op, ctx)
        case RemoveRelation():
            return _remove_relation(op, ctx)
        case AddMapping() | RemoveMapping():
            return _mapping(op, ctx)
        case AddAxiom():
            return _add_axiom(op, ctx)
        case RemoveAxiom():
            iri = f"{ctx.base_iri}{quote(_bare(op.concept), safe='')}.{quote(op.name, safe='')}"
            return OMOps(native=[_native_delete("DELETE_AXIOM", iri, ctx)])
        case AddSynonym():
            return _add_synonym(op, ctx)
        case AddRealization() | RemoveRealization():
            return OMOps()


def _add_concept(op: AddConcept, ctx: OMContext) -> OMOps:
    iri = _concept_iri(ctx, op.name)
    term: dict[str, Any] = {
        "id": draft_term_id(op),
        "name": op.name,
        "iri": iri,
        "glossary": _glossary_ref(ctx),
    }
    description = _text(op.description)
    if description is not None:
        term["description"] = description
    if op.display_name:
        term["displayName"] = op.display_name
    if op.synonyms:
        term["synonyms"] = list(op.synonyms)
    if op.parent:
        term["parent"] = _term_ref(ctx, op.parent)
    concept: dict[str, Any] = {
        "id": _uid("concept", op_key(op)),
        "name": op.name,
        "fullyQualifiedName": f"{_ontology_name(ctx)}.{op.name}",
        "iri": iri,
        "conceptType": "OWL_CLASS",
        "ontology": _ontology_ref(ctx),
    }
    if description is not None:
        concept["description"] = description
    return OMOps(
        glossary=[{"operationType": "CREATE_TERM", "term": term}],
        native=[{"operationType": "UPSERT_CONCEPT", "concept": concept}],
    )


def _update_concept(op: UpdateConcept, ctx: OMContext) -> OMOps:
    term_id = _lookup(ctx.term_ids, ctx, op.fqn, "term id")
    term: dict[str, Any] = {
        "id": term_id,
        "name": _bare(op.fqn),
        "glossary": _glossary_ref(ctx),
    }
    if op.description is not None:
        term["description"] = op.description.text
    if op.display_name is not None:
        term["displayName"] = op.display_name
    if op.synonyms is not None:
        term["synonyms"] = list(op.synonyms)
    if op.parent is not None:
        term["parent"] = _term_ref(ctx, op.parent)
    return OMOps(
        glossary=[
            {
                "operationType": "UPDATE_TERM",
                "targetId": term_id,
                "baseVersion": float(op.base_version),
                "term": term,
            }
        ]
    )


def _native_delete(kind: str, iri: str, ctx: OMContext) -> dict[str, Any]:
    if iri not in ctx.native_ids or iri not in ctx.native_versions:
        raise _fail(
            f"native definition {iri} is not known",
            "pass its id and version in OMContext.native_ids/native_versions",
        )
    return {
        "operationType": kind,
        "targetId": ctx.native_ids[iri],
        "baseVersion": ctx.native_versions[iri],
    }


def _remove_concept(op: RemoveConcept, ctx: OMContext) -> OMOps:
    iri = _concept_iri(ctx, op.fqn)
    glossary = [
        {
            "operationType": "DELETE_TERM",
            "targetId": _lookup(ctx.term_ids, ctx, op.fqn, "term id"),
            "baseVersion": float(op.base_version),
        }
    ]
    native = [_native_delete("DELETE_CONCEPT", iri, ctx)] if iri in ctx.native_ids else []
    return OMOps(glossary=glossary, native=native)


def _add_attribute(op: AddAttribute, ctx: OMContext) -> OMOps:
    domain = _concept_iri(ctx, op.concept)
    iri = f"{domain}.{quote(op.name, safe='')}"
    name = f"{_bare(op.concept)}_{op.name}"
    prop: dict[str, Any] = {
        "id": _uid("property", op_key(op)),
        "name": name,
        "displayName": op.name,
        "fullyQualifiedName": f"{_ontology_name(ctx)}.{name}",
        "iri": iri,
        "kind": "DATA",
        "domain": [domain],
        "range": [XSD + op.datatype.removeprefix("xsd:")],
        "isIdentifier": op.is_identifier,
        "ontology": _ontology_ref(ctx),
    }
    if op.description is not None:
        prop["description"] = op.description.text
    if op.allowed_values:
        prop["enumValues"] = list(op.allowed_values)
    if op.unit is not None:
        prop["unit"] = op.unit
    return OMOps(native=[{"operationType": "UPSERT_PROPERTY", "property": prop}])


def _relationship_type(ctx: OMContext, name: str) -> dict[str, str]:
    if name not in ctx.relationship_type_ids:
        raise _fail(
            f"unknown relationship type {name!r}",
            "use a relationship type from list_relationship_types",
        )
    return {"id": ctx.relationship_type_ids[name], "type": "relationshipType"}


def _add_relation(op: AddRelation, ctx: OMContext) -> OMOps:
    source = _term_ref(ctx, op.source)
    relationship: dict[str, Any] = {
        "id": _uid("relationship", op_key(op)),
        "fromTerm": source,
        "toTerm": _term_ref(ctx, op.target),
        "relationshipType": _relationship_type(ctx, op.relation_type),
        "provenance": "Manual",
        "status": "Draft",
        "createdAt": 0,
        "createdBy": "lakeloom",
    }
    if op.description is not None:
        relationship["description"] = op.description.text
    return OMOps(
        glossary=[
            {
                "operationType": "ADD_RELATIONSHIP",
                "targetId": source["id"],
                "baseVersion": _lookup(ctx.term_versions, ctx, op.source, "term version"),
                "relationship": relationship,
            }
        ]
    )


def _remove_relation(op: RemoveRelation, ctx: OMContext) -> OMOps:
    key = op_key(AddRelation(**op.model_dump(exclude={"op"})))
    if key not in ctx.relationship_ids:
        raise _fail(
            f"relationship {key} is not known",
            "pass its OM relationship id in OMContext.relationship_ids",
        )
    return OMOps(
        glossary=[
            {
                "operationType": "DELETE_RELATIONSHIP",
                "targetId": ctx.relationship_ids[key],
                "baseVersion": _lookup(ctx.term_versions, ctx, op.source, "term version"),
            }
        ]
    )


def _mapping(op: AddMapping | RemoveMapping, ctx: OMContext) -> OMOps:
    term_id = _lookup(ctx.term_ids, ctx, op.concept, "term id")
    return OMOps(
        glossary=[
            {
                "operationType": "UPSERT_MAPPING"
                if isinstance(op, AddMapping)
                else "DELETE_MAPPING",
                "targetId": term_id,
                "baseVersion": _lookup(ctx.term_versions, ctx, op.concept, "term version"),
                "mapping": {
                    "conceptIri": str(op.target_iri),
                    "mappingType": _MAPPING_TYPE[op.kind],
                },
            }
        ]
    )


def _add_axiom(op: AddAxiom, ctx: OMContext) -> OMOps:
    name = str(op.axiom["name"])
    axiom: dict[str, Any] = {
        "provenance": "Manual",
        "fullyQualifiedName": f"{_ontology_name(ctx)}.{name}",
        **op.axiom,
        "id": _uid("axiom", op_key(op)),
        "subjectIri": _concept_iri(ctx, op.concept),
        "ontology": _ontology_ref(ctx),
    }
    return OMOps(native=[{"operationType": "UPSERT_AXIOM", "axiom": axiom}])


def _add_synonym(op: AddSynonym, ctx: OMContext) -> OMOps:
    term_id = _lookup(ctx.term_ids, ctx, op.concept_fqn, "term id")
    existing = list(_find(ctx.term_synonyms, ctx, op.concept_fqn) or [])
    if op.synonym.casefold() not in {s.casefold() for s in existing}:
        existing.append(op.synonym)
    return OMOps(
        glossary=[
            {
                "operationType": "UPDATE_TERM",
                "targetId": term_id,
                "baseVersion": _lookup(ctx.term_versions, ctx, op.concept_fqn, "term version"),
                "term": {
                    "id": term_id,
                    "name": _bare(op.concept_fqn),
                    "glossary": _glossary_ref(ctx),
                    "synonyms": existing,
                },
            }
        ]
    )
