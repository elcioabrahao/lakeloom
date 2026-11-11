"""Deterministic mapper from a scan record to proposal operations (FR-PRO-01).

No LLM, no network: the same scan and policy always give the same operations in the same
order. Every operation records the rule IDs that produced it and the evidence behind it.

======================  =========================================================
``R-TABLE``             table -> ``AddConcept`` (name by the naming policy)
``R-COMMENT``           table/column comment -> description (data, never interpreted)
``R-DESC-DEFAULT``      no table comment -> "Concept derived from table <name>."
                        (OM rejects terms without a description, ADR 0011)
``R-COLUMN``            column -> ``AddAttribute``
``R-TYPE``              Spark type -> ``xsd:`` datatype (``MappingPolicy.datatype_map``)
``R-PK``                declared primary key -> ``is_identifier`` plus an exact-1
                        cardinality ``AddAxiom`` (OM has no ``owl:hasKey``, ADR 0011)
``R-FK``                declared foreign key -> ``AddRelation`` ``has<Target>``
``R-ENUM``              low-cardinality string column -> ``allowed_values`` (or child
                        concepts when ``enum_as_children``)
``R-TAGS``              tags -> evidence only, never concepts
======================  =========================================================

Order: tables sorted by full name; per table the concept, its attributes (column order),
enum child concepts, relations, then axioms.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from lakeloom.core.model_policy import MappingPolicy
from lakeloom.core.operations import (
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddRelation,
    Evidence,
    ProposalOperation,
)
from lakeloom.sources.models import ColumnInfo, ColumnProfile, Constraint, TableDescription
from lakeloom.state.scans import ScannedTable, ScanRecord
from lakeloom.untrusted import UntrustedText

__all__ = ["MappingResult", "map_scan", "singularize"]

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_STRING_TYPES = {"string", "varchar", "char"}
_MAX_NAME = 128
_SOURCE = "lakeloom:mapper"


@dataclass
class MappingResult:
    operations: list[ProposalOperation] = field(default_factory=list[ProposalOperation])
    warnings: list[str] = field(default_factory=list[str])


def singularize(word: str) -> str:
    """Singularise one word by simple rules (``ies``->``y``, ``sses``->``ss``, trailing ``s``)."""
    low = word.lower()
    if len(word) <= 3:
        return word
    if low.endswith("ies"):
        return word[:-3] + "y"
    if low.endswith("sses"):
        return word[:-2]
    if low.endswith(("xes", "ches", "shes")):
        return word[:-2]
    if low.endswith(("ss", "us", "is")):
        return word
    if low.endswith("s"):
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [w for w in _SPLIT.split(text) if w]


def _valid_name(text: str, fallback: str) -> str:
    """Coerce ``text`` into the operation ``Name`` pattern."""
    name = re.sub(r"[^A-Za-z0-9_ ]", "_", text).strip()
    if not name:
        name = fallback
    if not name[0].isalpha():
        name = fallback[0] + name
    return name[:_MAX_NAME]


def _pascal(words: list[str]) -> str:
    return "".join(w[0].upper() + w[1:] for w in words)


def _concept_name(table: str, policy: MappingPolicy) -> str:
    match policy.concept_naming:
        case "as_is":
            return _valid_name(table, "Table")
        case "pascal":
            return _valid_name(_pascal(_words(table)), "Table")
        case _:
            words = _words(table)
            if words:
                words[-1] = singularize(words[-1])
            return _valid_name(_pascal(words), "Table")


def _attribute_name(column: str, policy: MappingPolicy) -> str:
    if policy.attribute_naming == "as_is":
        return _valid_name(column, "column")
    words = _words(column)
    if not words:
        return _valid_name(column, "column")
    first = words[0][0].lower() + words[0][1:]
    return _valid_name(first + _pascal(words[1:]), "column")


def _relation_type(target_concept: str) -> str:
    return "has" + (_pascal(_words(target_concept)) or "Target")


def _datatype(raw: str, policy: MappingPolicy) -> tuple[str, bool]:
    base = raw.strip().lower().split("(", 1)[0].split("<", 1)[0].strip()
    mapped = policy.datatype_map.get(base)
    return (mapped, False) if mapped else ("xsd:string", True)


class _Names:
    """Hands out unique concept names within one scan (collisions get ``2``, ``3``...)."""

    def __init__(self, warnings: list[str]) -> None:
        self._used: set[str] = set()
        self._warnings = warnings

    def claim(self, wanted: str, what: str) -> str:
        name, n = wanted, 1
        while name.lower() in self._used:
            n += 1
            name = f"{wanted}{n}"
        if n > 1:
            self._warnings.append(f"name collision: {what} -> {name!r} (wanted {wanted!r})")
        self._used.add(name.lower())
        return name


def _text(value: UntrustedText | None) -> UntrustedText | None:
    return value if value is not None and value.text.strip() else None


def _tag_evidence(tags: list[str], ref: str) -> list[Evidence]:
    return [Evidence(kind="tag", ref=ref, detail=tag) for tag in tags]


def map_scan(record: ScanRecord, policy: MappingPolicy) -> MappingResult:
    """Map every described table in ``record`` to proposal operations."""
    warnings: list[str] = []
    tables: list[tuple[ScannedTable, TableDescription]] = []
    for scanned in record.tables:
        if scanned.description is None:
            warnings.append(f"table {scanned.ref.full_name} skipped: no description in scan")
        else:
            tables.append((scanned, scanned.description))
    tables.sort(key=lambda pair: pair[0].ref.full_name)

    names = _Names(warnings)
    concept_of: dict[str, str] = {}
    by_schema_name: dict[str, str] = {}
    for scanned, _ in tables:
        full = scanned.ref.full_name
        concept_of[full] = names.claim(_concept_name(scanned.ref.name, policy), f"table {full}")
        by_schema_name.setdefault(f"{scanned.ref.schema_}.{scanned.ref.name}", concept_of[full])

    ops: list[ProposalOperation] = []
    for scanned, desc in tables:
        ops.extend(_map_table(scanned, desc, concept_of, by_schema_name, policy, names, warnings))
    return MappingResult(operations=ops, warnings=sorted(set(warnings)))


def _map_table(
    scanned: ScannedTable,
    desc: TableDescription,
    concept_of: dict[str, str],
    by_schema_name: dict[str, str],
    policy: MappingPolicy,
    names: _Names,
    warnings: list[str],
) -> list[ProposalOperation]:
    full = scanned.ref.full_name
    concept = concept_of[full]
    out: list[ProposalOperation] = []

    comment = _text(desc.comment)
    rules = ["R-TABLE", "R-COMMENT" if comment else "R-DESC-DEFAULT"]
    evidence = [Evidence(kind="table", ref=full)]
    if comment:
        evidence.append(Evidence(kind="comment", ref=full))
        description = comment
    else:
        description = UntrustedText(text=f"Concept derived from table {full}.", source=_SOURCE)
    if desc.tags:
        rules.append("R-TAGS")
        evidence += _tag_evidence(desc.tags, full)
    out.append(
        AddConcept(
            op="add_concept",
            name=concept,
            description=description,
            rule_ids=rules,
            evidence=evidence,
        )
    )

    pk_columns: list[str] = []
    for constraint in desc.constraints:
        if constraint.kind == "primary_key":
            pk_columns.extend(c for c in constraint.columns if c not in pk_columns)
    profiles = {c.name: c for c in scanned.profile.columns} if scanned.profile else {}

    attr_names: dict[str, str] = {}
    used_attrs: set[str] = set()
    children: list[ProposalOperation] = []
    for column in sorted(desc.columns, key=lambda c: c.position):
        attr = _attribute_name(column.name, policy)
        wanted, n = attr, 1
        while attr.lower() in used_attrs:
            n += 1
            attr = f"{wanted}{n}"
        if n > 1:
            warnings.append(f"name collision: column {full}.{column.name} -> {attr!r}")
        used_attrs.add(attr.lower())
        attr_names[column.name] = attr
        datatype, unknown = _datatype(column.data_type, policy)
        if unknown:
            warnings.append(
                f"unknown type {column.data_type!r} for {full}.{column.name}; mapped to xsd:string"
            )
        is_pk = column.name in pk_columns
        values = _enum_values(full, column, profiles.get(column.name), is_pk, policy, warnings)
        allowed = [] if policy.enum_as_children else (values or [])
        out.append(_attribute(full, concept, attr, column, datatype, is_pk, allowed, bool(values)))
        if policy.enum_as_children and values:
            children += _enum_children(full, concept, column, values, names, warnings)
    out.extend(children)

    out.extend(_relations(full, concept, desc, concept_of, by_schema_name, policy, warnings))

    if pk_columns:
        out.append(_pk_axiom(full, concept, pk_columns, attr_names))
    return out


def _attribute(
    full: str,
    concept: str,
    attr: str,
    column: ColumnInfo,
    datatype: str,
    is_pk: bool,
    allowed: list[str],
    is_enum: bool,
) -> AddAttribute:
    ref = f"{full}.{column.name}"
    rules = ["R-COLUMN", "R-TYPE"]
    evidence = [Evidence(kind="column", ref=ref, detail=column.data_type)]
    comment = _text(column.comment)
    if comment:
        rules.append("R-COMMENT")
        evidence.append(Evidence(kind="comment", ref=ref))
    if is_pk:
        rules.append("R-PK")
        evidence.append(Evidence(kind="pk", ref=ref))
    if is_enum:
        rules.append("R-ENUM")
        evidence.append(Evidence(kind="profile", ref=ref, detail="low-cardinality sample values"))
    if column.tags:
        rules.append("R-TAGS")
        evidence += _tag_evidence(column.tags, ref)
    return AddAttribute(
        op="add_attribute",
        concept=concept,
        name=attr,
        datatype=datatype,
        is_identifier=is_pk,
        allowed_values=allowed,
        description=comment,
        rule_ids=rules,
        evidence=evidence,
    )


def _enum_values(
    full: str,
    column: ColumnInfo,
    profile: ColumnProfile | None,
    is_pk: bool,
    policy: MappingPolicy,
    warnings: list[str],
) -> list[str] | None:
    """Sorted enum values for a low-cardinality string column, else ``None`` (R-ENUM)."""
    base = column.data_type.strip().lower().split("(", 1)[0]
    if base not in _STRING_TYPES or is_pk or profile is None:
        return None
    distinct = profile.distinct_count
    if distinct is not None and (distinct < 1 or distinct > policy.low_cardinality):
        return None
    ref = f"{full}.{column.name}"
    if profile.values_withheld is not None:
        warnings.append(f"enum skipped: sample values withheld for {ref}")
        return None
    if distinct is None or profile.top_k is None:
        return None
    values = sorted({v for v, _ in profile.top_k if v.strip() and len(v) <= 256})
    if len(values) < distinct:
        warnings.append(f"enum skipped: sample values incomplete for {ref}")
        return None
    return values


def _enum_children(
    full: str,
    concept: str,
    column: ColumnInfo,
    values: list[str],
    names: _Names,
    warnings: list[str],
) -> list[ProposalOperation]:
    ref = f"{full}.{column.name}"
    out: list[ProposalOperation] = []
    for value in values:
        words = _words(value)
        if not words:
            warnings.append(f"enum child skipped: value {value!r} of {ref} has no usable name")
            continue
        name = names.claim(_valid_name(concept + _pascal(words), "Value"), f"enum value of {ref}")
        out.append(
            AddConcept(
                op="add_concept",
                name=name,
                parent=concept,
                description=UntrustedText(
                    text=f"Value {value!r} of {full}.{column.name}.", source=_SOURCE
                ),
                rule_ids=["R-ENUM", "R-DESC-DEFAULT"],
                evidence=[
                    Evidence(kind="column", ref=ref, detail=column.data_type),
                    Evidence(kind="profile", ref=ref, detail="low-cardinality sample values"),
                ],
            )
        )
    return out


_KEY_SUFFIX = re.compile(r"(_id|_key)$", re.IGNORECASE)


def _fk_relation_type(columns: list[str]) -> str:
    """``has`` + the FK column name(s) minus a trailing ``_id``/``_key``, PascalCased."""
    stripped = [_KEY_SUFFIX.sub("", c) for c in columns]
    words = [w for c in stripped for w in _words(c)]
    return "has" + (_pascal(words) or "Target")


def _relations(
    full: str,
    concept: str,
    desc: TableDescription,
    concept_of: dict[str, str],
    by_schema_name: dict[str, str],
    policy: MappingPolicy,
    warnings: list[str],
) -> list[ProposalOperation]:
    """One relation per declared FK (R-FK).

    ``has<Target>`` when it is the table's only FK to that target, otherwise named after
    the FK column(s); residual collisions get a numeric suffix and a warning.
    """
    resolved: list[tuple[Constraint, str, str]] = []
    for constraint in desc.constraints:
        target = constraint.ref_table
        if constraint.kind != "foreign_key" or target is None:
            continue
        target_full = target.full_name
        target_concept = concept_of.get(target_full) or by_schema_name.get(
            f"{target.schema_}.{target.name}"
        )
        if target_concept is None:
            target_concept = _concept_name(target.name, policy)
            warnings.append(
                f"FK target {target_full} not in scan; relation assumes concept {target_concept}"
            )
        resolved.append((constraint, target_full, target_concept))
    per_target: dict[str, int] = {}
    for _, _, target_concept in resolved:
        per_target[target_concept] = per_target.get(target_concept, 0) + 1

    used: set[str] = set()
    out: list[ProposalOperation] = []
    for constraint, target_full, target_concept in resolved:
        wanted = (
            _relation_type(target_concept)
            if per_target[target_concept] == 1
            else _fk_relation_type(constraint.columns)
        )
        rtype, n = wanted, 1
        while rtype in used:
            n += 1
            rtype = f"{wanted}{n}"
        if n > 1:
            warnings.append(f"name collision: FK from {full} to {target_full} -> {rtype!r}")
        used.add(rtype)
        to = f"{target_full}({', '.join(constraint.ref_columns)})"
        out.append(
            AddRelation(
                op="add_relation",
                source=concept,
                relation_type=rtype,
                target=target_concept,
                rule_ids=["R-FK"],
                evidence=[
                    Evidence(kind="fk", ref=f"{full}.{c}", detail=f"references {to}")
                    for c in constraint.columns
                ],
            )
        )
    return out


def _pk_axiom(
    full: str, concept: str, pk_columns: list[str], attr_names: dict[str, str]
) -> AddAxiom:
    """Exact-cardinality-1 restriction per key column (OM has no ``owl:hasKey``, ADR 0011)."""
    expressions: list[dict[str, Any]] = [
        {
            "kind": "RESTRICTION",
            "restrictionKind": "EXACT",
            "propertyName": attr_names[c],
            "cardinality": 1,
        }
        for c in pk_columns
        if c in attr_names
    ]
    return AddAxiom(
        op="add_axiom",
        concept=concept,
        axiom={
            "name": f"{_pascal(_words(concept)) or 'Concept'}PrimaryKey",
            "displayName": f"{concept} primary key",
            "description": f"Primary key of table {full}.",
            "axiomType": "SUBCLASS_OF",
            "expressions": expressions,
        },
        rule_ids=["R-PK"],
        evidence=[Evidence(kind="pk", ref=f"{full}.{c}") for c in pk_columns],
    )
