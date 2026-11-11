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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, model_validator

from lakeloom.core.model_policy import XSD_DATATYPE_PATTERN
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
    "TermSnapshot",
    "UpdateConcept",
    "draft_term_id",
    "op_key",
    "to_om",
    "to_om_all",
]

MAX_DESCRIPTION_CHARS = 4000
XSD = "http://www.w3.org/2001/XMLSchema#"
_NAME = r"^[A-Za-z][A-Za-z0-9_ ]{0,127}$"
_RELATION = r"^[a-zA-Z][a-zA-Z0-9]*$"
_NS = uuid.UUID("6f1b3c1e-5a0e-4b0a-9a3e-4c6d2f7a9b10")

Name = Annotated[str, Field(pattern=_NAME)]
Ref = Annotated[str, Field(min_length=1, max_length=512)]
RelationType = Annotated[str, Field(pattern=_RELATION)]
Short = Annotated[str, Field(min_length=1, max_length=256)]
ShortList = Annotated[list[Short], Field(max_length=100)]
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
    ref: Annotated[str, Field(max_length=2000)]
    detail: Annotated[str | None, Field(max_length=2000)] = None
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
        existing: Any = out.get("evidence", [])
        if flags and isinstance(existing, list | tuple):
            out["evidence"] = [*cast("list[Any]", existing), *flags]
        return out


class AddConcept(_Op):
    op: Literal["add_concept"]
    name: Name
    display_name: Annotated[str | None, Field(max_length=256)] = None
    description: UntrustedText | None = None
    parent: Ref | None = None
    synonyms: ShortList = Field(default_factory=list[str])


class UpdateConcept(_Op):
    """Changes to an existing concept; ``None`` leaves a field alone."""

    op: Literal["update_concept"]
    fqn: Ref
    base_version: Version
    description: UntrustedText | None = None
    display_name: Annotated[str | None, Field(max_length=256)] = None
    synonyms: ShortList | None = None
    parent: Ref | None = None


class RemoveConcept(_Op):
    op: Literal["remove_concept"]
    fqn: Ref
    base_version: Version


class AddAttribute(_Op):
    op: Literal["add_attribute"]
    concept: Ref
    name: Name
    datatype: Annotated[str, Field(pattern=f"^{XSD_DATATYPE_PATTERN}$")]
    is_identifier: bool = False
    allowed_values: ShortList = Field(default_factory=list[str])
    unit: Annotated[str | None, Field(max_length=64)] = None
    description: UntrustedText | None = None


class RemoveAttribute(_Op):
    op: Literal["remove_attribute"]
    concept: Ref
    name: Name


class AddRelation(_Op):
    op: Literal["add_relation"]
    source: Ref
    relation_type: RelationType
    target: Ref
    description: UntrustedText | None = None


class RemoveRelation(_Op):
    op: Literal["remove_relation"]
    source: Ref
    relation_type: RelationType
    target: Ref


class AddMapping(_Op):
    op: Literal["add_mapping"]
    concept: Ref
    kind: MappingKind
    target_iri: AnyUrl


class RemoveMapping(_Op):
    op: Literal["remove_mapping"]
    concept: Ref
    kind: MappingKind
    target_iri: AnyUrl


class AddAxiom(_Op):
    """``axiom`` is an OM ``OntologyAxiom`` body; it needs a ``name`` (validated in OM, Task 6)."""

    op: Literal["add_axiom"]
    concept: Ref
    axiom: dict[str, Any]

    @model_validator(mode="after")
    def _named(self) -> "AddAxiom":
        if not isinstance(self.axiom.get("name"), str) or not self.axiom["name"]:
            raise ValueError("axiom needs a non-empty string 'name'")
        return self


class RemoveAxiom(_Op):
    op: Literal["remove_axiom"]
    concept: Ref
    name: Short


class AddSynonym(_Op):
    op: Literal["add_synonym"]
    concept_fqn: Ref
    synonym: Short


class AddRealization(_Op):
    op: Literal["add_realization"]
    concept: Ref
    asset_fqn: Ref
    column: str | None = None


class RemoveRealization(_Op):
    op: Literal["remove_realization"]
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
class TermSnapshot:
    """The current state of an existing glossary term.

    UPDATE_TERM replaces the term (live check, ADR 0011: an omitted ``iri`` was dropped), so
    updates are always built from a full snapshot. ``description`` is required by OM.
    """

    description: str
    version: float
    display_name: str | None = None
    iri: str | None = None
    parent_id: str | None = None
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class OMContext:
    """Everything :func:`to_om_all` needs to know about OM, supplied by the caller (no I/O).

    Term tables are keyed by term FQN or bare name; terms created in the same proposal are
    registered by the caller with :func:`draft_term_id`. ``terms`` holds snapshots of
    existing terms (needed to update them). ``native_ids``/``native_versions`` hold the
    existing native definitions, keyed ``concept:<iri>``, ``attr:<iri>`` or ``axiom:<iri>``
    (needed only for removals). ``relationship_ids`` is keyed by the relation's op key.
    """

    glossary_name: str
    glossary_id: str
    base_iri: str
    ontology_id: str
    ontology_name: str | None = None
    term_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    term_versions: Mapping[str, float] = field(default_factory=dict[str, float])
    terms: Mapping[str, TermSnapshot] = field(default_factory=dict[str, TermSnapshot])
    relationship_type_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    relationship_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    native_ids: Mapping[str, str] = field(default_factory=dict[str, str])
    native_versions: Mapping[str, float] = field(default_factory=dict[str, float])
    # Hybrid model: relations also become native object properties when the model is paired.
    paired: bool = True
    # Glossary relationship type used when OM does not define the relation's own type.
    fallback_relationship_type: str | None = "relatedTo"


@dataclass(frozen=True)
class OMOps:
    """OM operations for proposal operations: glossary change set and native change set."""

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
            return _relation_key(op.source, op.relation_type, op.target)
        case RemoveRelation():
            return "remove-" + _relation_key(op.source, op.relation_type, op.target)
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


def _relation_key(source: str, relation_type: str, target: str) -> str:
    return f"rel:{_bare(source)}|{relation_type}|{_bare(target)}"


def _uid(kind: str, key: str) -> str:
    return str(uuid.uuid5(_NS, f"{kind}:{key}"))


def draft_term_id(op: AddConcept) -> str:
    """Deterministic draft id of the term an ``AddConcept`` creates."""
    return _uid("term", op_key(op))


def _fail(message: str, hint: str) -> LakeloomError:
    return LakeloomError("validation_error", message, hint)


def _local(ctx: OMContext, ref: str) -> str:
    """The bare term name of ``ref``; a ref qualified by another model is rejected."""
    if "." in ref and ref.rsplit(".", 1)[0] != ctx.glossary_name:
        raise _fail(
            f"reference {ref!r} points outside model {ctx.glossary_name!r}",
            "cross-model references need ontology imports, which are not supported yet; "
            "use a concept of this model",
        )
    return _bare(ref)


def _find[V](table: Mapping[str, V], ctx: OMContext, ref: str) -> V | None:
    name = _local(ctx, ref)
    for candidate in (f"{ctx.glossary_name}.{name}", name):
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
    return f"{ctx.base_iri}{quote(_local(ctx, ref), safe='')}"


def _member_iri(ctx: OMContext, concept: str, member: str) -> str:
    return f"{_concept_iri(ctx, concept)}.{quote(member, safe='')}"


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


def to_om_all(
    ops: Sequence[ProposalOperation],
    ctx: OMContext,
    *,
    in_change_set: Collection[str] = (),
) -> OMOps:
    """Map a batch of proposal operations to OM operations; the entry point for callers.

    All changes to one existing term (``UpdateConcept``, ``AddSynonym``) are folded, in
    order, into a single ``UPDATE_TERM`` built from the term's snapshot: separate updates
    from the same static context would overwrite each other. A synonym added to a concept
    created in the same batch is folded into its ``CREATE_TERM``. Relations and mappings on a
    term created in the same batch carry no ``baseVersion`` (rc1 rejects one, ADR 0011);
    ``in_change_set`` names concepts created earlier in the change set the batch is appended
    to, which are treated the same way. Declarations come first on each side (``CREATE_TERM``,
    ``UPSERT_CONCEPT``; otherwise in batch order): rc1 rejects an operation that references a
    term created by a later one, e.g. ``Invoice hasOrder Order`` when invoices sort first.
    """
    created = {op.name: op for op in ops if isinstance(op, AddConcept)}
    unversioned = set(created) | set(in_change_set)
    folded: dict[str, list[UpdateConcept | AddSynonym]] = {}
    extra_synonyms: dict[str, list[str]] = {}
    for op in ops:
        if isinstance(op, UpdateConcept | AddSynonym):
            name = _local(ctx, op.fqn if isinstance(op, UpdateConcept) else op.concept_fqn)
            if name in created:
                if isinstance(op, UpdateConcept):
                    raise _fail(
                        f"concept {name!r} is created in this proposal and cannot be updated",
                        "change the AddConcept operation instead",
                    )
                extra_synonyms.setdefault(name, []).append(op.synonym)
            else:
                folded.setdefault(name, []).append(op)
    glossary: list[dict[str, Any]] = []
    native: list[dict[str, Any]] = []
    done: set[str] = set()
    for op in ops:
        if isinstance(op, UpdateConcept | AddSynonym):
            name = _local(ctx, op.fqn if isinstance(op, UpdateConcept) else op.concept_fqn)
            if name in created or name in done:
                continue
            done.add(name)
            glossary.append(_update_term(name, folded[name], ctx))
            continue
        if isinstance(op, AddConcept) and op.name in extra_synonyms:
            merged = [*op.synonyms]
            for synonym in extra_synonyms[op.name]:
                if synonym.casefold() not in {x.casefold() for x in merged}:
                    merged.append(synonym)
            op = op.model_copy(update={"synonyms": merged})
        out = to_om(op, ctx)
        anchor = _anchor(op)
        if anchor is not None and _local(ctx, anchor) in unversioned:
            # rc1: "baseVersion must be absent for a term created in this change set"
            out = OMOps(
                glossary=[{k: v for k, v in o.items() if k != "baseVersion"} for o in out.glossary],
                native=out.native,
            )
        glossary.extend(out.glossary)
        native.extend(out.native)
    return OMOps(
        glossary=_declarations_first(glossary, "CREATE_TERM"),
        native=_declarations_first(native, "UPSERT_CONCEPT"),
    )


def _declarations_first(ops: list[dict[str, Any]], declaration: str) -> list[dict[str, Any]]:
    """Stable partition: the ``declaration`` operations, then the rest (each in order)."""
    first = [o for o in ops if o.get("operationType") == declaration]
    return first + [o for o in ops if o.get("operationType") != declaration]


def _anchor(op: ProposalOperation) -> str | None:
    """The term whose id is an operation's ``targetId`` (relations: the source)."""
    if isinstance(op, AddRelation | RemoveRelation):
        return op.source
    if isinstance(op, AddMapping | RemoveMapping):
        return op.concept
    return None


def to_om(op: ProposalOperation, ctx: OMContext) -> OMOps:
    """Map one proposal operation to OM operations (see ADR 0011 for the payloads).

    Prefer :func:`to_om_all` for a proposal: it merges the updates of one term.
    """
    match op:
        case AddConcept():
            return _add_concept(op, ctx)
        case UpdateConcept() | AddSynonym():
            return to_om_all([op], ctx)
        case RemoveConcept():
            return _remove_concept(op, ctx)
        case AddAttribute():
            return _add_attribute(op, ctx)
        case RemoveAttribute():
            iri = _member_iri(ctx, op.concept, op.name)
            return OMOps(native=[_native_delete("DELETE_PROPERTY", "attr", iri, ctx)])
        case AddRelation():
            return _add_relation(op, ctx)
        case RemoveRelation():
            return _remove_relation(op, ctx)
        case AddMapping() | RemoveMapping():
            return _mapping(op, ctx)
        case AddAxiom():
            return _add_axiom(op, ctx)
        case RemoveAxiom():
            iri = _member_iri(ctx, op.concept, op.name)
            return OMOps(native=[_native_delete("DELETE_AXIOM", "axiom", iri, ctx)])
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


def _update_term(
    name: str, changes: Sequence[UpdateConcept | AddSynonym], ctx: OMContext
) -> dict[str, Any]:
    """One UPDATE_TERM carrying the full desired term (snapshot + folded changes)."""
    snapshot = _find(ctx.terms, ctx, name)
    term_id = _find(ctx.term_ids, ctx, name)
    if snapshot is None or term_id is None:
        raise _fail(
            f"no snapshot of term {name!r}",
            "pass the term's current state in OMContext.terms (and its id in term_ids) "
            "so the update can carry the full term",
        )
    description = snapshot.description
    display_name = snapshot.display_name
    parent_id = snapshot.parent_id
    synonyms = list(snapshot.synonyms)
    base_version = snapshot.version
    for change in changes:
        if isinstance(change, AddSynonym):
            if change.synonym.casefold() not in {s.casefold() for s in synonyms}:
                synonyms.append(change.synonym)
            continue
        base_version = float(change.base_version)
        if change.description is not None:
            description = change.description.text
        if change.display_name is not None:
            display_name = change.display_name
        if change.synonyms is not None:
            synonyms = list(change.synonyms)
        if change.parent is not None:
            parent_id = _term_ref(ctx, change.parent)["id"]
    term: dict[str, Any] = {
        "id": term_id,
        "name": name,
        "glossary": _glossary_ref(ctx),
        "description": description,
        "synonyms": synonyms,
    }
    if display_name is not None:
        term["displayName"] = display_name
    if snapshot.iri is not None:
        term["iri"] = snapshot.iri
    if parent_id is not None:
        term["parent"] = {"id": parent_id, "type": "glossaryTerm"}
    return {
        "operationType": "UPDATE_TERM",
        "targetId": term_id,
        "baseVersion": base_version,
        "term": term,
    }


def _native_delete(kind: str, prefix: str, iri: str, ctx: OMContext) -> dict[str, Any]:
    key = f"{prefix}:{iri}"
    if key not in ctx.native_ids or key not in ctx.native_versions:
        raise _fail(
            f"native definition {iri} is not known",
            f"pass its id and version in OMContext.native_ids/native_versions under {key!r}",
        )
    return {
        "operationType": kind,
        "targetId": ctx.native_ids[key],
        "baseVersion": ctx.native_versions[key],
    }


def _remove_concept(op: RemoveConcept, ctx: OMContext) -> OMOps:
    iri = _concept_iri(ctx, op.fqn)
    glossary = {
        "operationType": "DELETE_TERM",
        "targetId": _lookup(ctx.term_ids, ctx, op.fqn, "term id"),
        "baseVersion": float(op.base_version),
    }
    return OMOps(
        glossary=[glossary], native=[_native_delete("DELETE_CONCEPT", "concept", iri, ctx)]
    )


def _add_attribute(op: AddAttribute, ctx: OMContext) -> OMOps:
    domain = _concept_iri(ctx, op.concept)
    name = f"{_bare(op.concept)}.{op.name}"
    prop: dict[str, Any] = {
        "id": _uid("property", op_key(op)),
        "name": name,
        "displayName": op.name,
        "fullyQualifiedName": f"{_ontology_name(ctx)}.{name}",
        "iri": _member_iri(ctx, op.concept, op.name),
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
    fallback = ctx.fallback_relationship_type
    if name not in ctx.relationship_type_ids and fallback in ctx.relationship_type_ids:
        name = str(fallback)  # the native object property keeps the intended name
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
        ],
        native=[_object_property(op, ctx)] if ctx.paired else [],
    )


def _object_property(op: AddRelation, ctx: OMContext) -> dict[str, Any]:
    """The relation as a native ``owl:ObjectProperty`` (kind OBJECT): domain the source class,
    range the target class; IRI and name follow the property scheme (ADR 0011)."""
    name = f"{_bare(op.source)}.{op.relation_type}"
    prop: dict[str, Any] = {
        "id": _uid("object-property", op_key(op)),
        "name": name,
        "displayName": op.relation_type,
        "fullyQualifiedName": f"{_ontology_name(ctx)}.{name}",
        "iri": _member_iri(ctx, op.source, op.relation_type),
        "kind": "OBJECT",
        "domain": [_concept_iri(ctx, op.source)],
        "range": [_concept_iri(ctx, op.target)],
        "ontology": _ontology_ref(ctx),
    }
    if op.description is not None:
        prop["description"] = op.description.text
    return {"operationType": "UPSERT_PROPERTY", "property": prop}


def _remove_relation(op: RemoveRelation, ctx: OMContext) -> OMOps:
    key = _relation_key(op.source, op.relation_type, op.target)
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
        ],
        native=_remove_object_property(op, ctx),
    )


def _remove_object_property(op: RemoveRelation, ctx: OMContext) -> list[dict[str, Any]]:
    """Delete the relation's native object property when the ontology has one."""
    key = f"attr:{_member_iri(ctx, op.source, op.relation_type)}"
    if not ctx.paired or key not in ctx.native_ids or key not in ctx.native_versions:
        return []
    return [
        {
            "operationType": "DELETE_PROPERTY",
            "targetId": ctx.native_ids[key],
            "baseVersion": ctx.native_versions[key],
        }
    ]


def _mapping(op: AddMapping | RemoveMapping, ctx: OMContext) -> OMOps:
    kind = "UPSERT_MAPPING" if isinstance(op, AddMapping) else "DELETE_MAPPING"
    return OMOps(
        glossary=[
            {
                "operationType": kind,
                "targetId": _lookup(ctx.term_ids, ctx, op.concept, "term id"),
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
    expressions = axiom.get("expressions")
    if isinstance(expressions, list):
        axiom["expressions"] = [
            _resolve_property(cast("dict[str, Any]", e), op.concept, ctx)
            if isinstance(e, dict)
            else e
            for e in cast("list[Any]", expressions)
        ]
    return OMOps(native=[{"operationType": "UPSERT_AXIOM", "axiom": axiom}])


def _resolve_property(expr: dict[str, Any], concept: str, ctx: OMContext) -> dict[str, Any]:
    """Turn a ``propertyName`` (an attribute of ``concept``) into the OM ``propertyIri``."""
    name = expr.get("propertyName")
    if not isinstance(name, str):
        return expr
    resolved = {k: v for k, v in expr.items() if k != "propertyName"}
    resolved.setdefault("propertyIri", _member_iri(ctx, concept, name))
    return resolved
