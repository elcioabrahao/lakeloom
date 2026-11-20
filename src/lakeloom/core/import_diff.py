"""The import diff: the model's current terms vs a file's terms, as proposal operations.

Pure (no I/O): OM term versions and native axiom names come from the caller. The operations
follow the plan's "Import diff" decision as amended by ADR 0016: a changed attribute or
axiom is an ``Add*`` again (``to_om`` addresses it to the existing definition), attribute
description edits are reported instead of proposed (rc1 ignores them, R2), and definitions
OpenMetadata keeps as tombstones are never created again (R4).
"""

import json
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any, Literal
from urllib.parse import quote

from pydantic import AnyUrl, BaseModel

from lakeloom.core.import_terms import AttributeTerm, AxiomTerm, ConceptTerm, MappingTerm, TermSet
from lakeloom.core.operations import (
    MAX_AXIOM_BYTES,
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddHistoryNote,
    AddMapping,
    AddRelation,
    AddScopeNote,
    DeprecateConcept,
    Evidence,
    ProposalOperation,
    RemoveAttribute,
    RemoveAxiom,
    RemoveConcept,
    RemoveMapping,
    RemoveRelation,
    UpdateConcept,
)
from lakeloom.errors import LakeloomError
from lakeloom.untrusted import UntrustedText

__all__ = ["ImportMode", "ImportOps", "import_operations"]

ImportMode = Literal["merge", "replace"]
ImportRule = Literal["R-IMPORT", "R-GIT-RESTORE"]
_MAX_REF = 2000  # Evidence.ref
_VERSION = re.compile(r"^\d+(\.\d+)?$")
_SOURCES: dict[str, str] = {"R-IMPORT": "imported file", "R-GIT-RESTORE": "Git release"}
_OM = "OpenMetadata 2.1.0-rc1"


class ImportOps(BaseModel):
    """The operations of an import, their counts by operation name, and the warnings."""

    operations: list[ProposalOperation]
    counts: dict[str, int]
    warnings: list[str]


def _fail(message: str, hint: str) -> LakeloomError:
    return LakeloomError("validation_error", message, hint)


def _parents_first(names: Iterable[str], parent_of: Callable[[str], str | None]) -> list[str]:
    """``names`` sorted, each after its parent when the parent is among them (cycles kept)."""
    pool = sorted(set(names))
    members = set(pool)
    ordered: list[str] = []
    done: set[str] = set()

    def visit(name: str, path: set[str]) -> None:
        if name in done or name in path:
            return
        parent = parent_of(name)
        if parent is not None and parent in members:
            visit(parent, path | {name})
        done.add(name)
        ordered.append(name)

    for name in pool:
        visit(name, set())
    return ordered


class _Diff:
    def __init__(
        self,
        current: TermSet,
        incoming: TermSet,
        *,
        mode: ImportMode,
        versions: Mapping[str, float],
        axiom_names: Mapping[str, list[str]],
        rule_id: ImportRule,
        deleted: Collection[str],
    ) -> None:
        self.cur = current
        self.inc = incoming
        self.replace = mode == "replace"
        self.versions = versions
        self.axiom_names = axiom_names
        self.rule_id = rule_id
        self.deleted = set(deleted)
        self.ns = current.namespace
        self.adds: list[ProposalOperation] = []
        self.removals: list[ProposalOperation] = []
        self.tombstones: set[str] = set()
        self.dependents = 0
        self.dropped_concepts: set[str] = set()
        self.dropped_attributes: set[tuple[str, str]] = set()
        self.description_edits = 0
        self.retargeted: set[str] = set()
        self.reparented: set[str] = set()  # OM rc1 cannot apply a parent change
        self.oversized: set[str] = set()

    # helpers -------------------------------------------------------------------------------

    def common(self, ref: str) -> dict[str, Any]:
        evidence = Evidence(kind="import", ref=ref[:_MAX_REF], truncated=len(ref) > _MAX_REF)
        return {"rule_ids": [self.rule_id], "evidence": [evidence]}

    def text(self, value: str | None) -> UntrustedText | None:
        return None if value is None else UntrustedText(text=value, source=_SOURCES[self.rule_id])

    def version(self, name: str) -> str:
        if name not in self.versions:
            raise _fail(
                f"no OpenMetadata version known for concept {name!r}",
                "pass the term's version (OMClient.get_term) in versions",
            )
        value = f"{self.versions[name]}"
        if not _VERSION.match(value):
            raise _fail(
                f"version {value!r} of concept {name!r} is not a version number",
                "pass the term's version as OpenMetadata reports it (e.g. 0.3)",
            )
        return value

    def names_of(self, concept: str) -> list[str]:
        if concept not in self.axiom_names:
            raise _fail(
                f"no OpenMetadata axiom names known for concept {concept!r}",
                "pass the concept's native axiom names (list_native_members) in axiom_names",
            )
        return sorted(self.axiom_names[concept])

    def concept_iri(self, name: str) -> str:
        known = self.cur.concepts.get(name)
        return known.iri if known is not None else f"{self.ns}{quote(name, safe='')}"

    def subject(self, name: str) -> str:
        """The subject IRI of a concept as read (the file's, else the model's)."""
        known = self.inc.concepts.get(name) or self.cur.concepts.get(name)
        return known.iri if known is not None else f"{self.ns}{quote(name, safe='')}"

    def tombstoned(self, key: str, label: str) -> bool:
        if key in self.deleted:
            self.tombstones.add(label)
            return True
        return False

    # additions -----------------------------------------------------------------------------

    def concepts(self) -> None:
        inc, cur = self.inc.concepts, self.cur.concepts
        new = [n for n in inc if n not in cur]
        for name in _parents_first(new, lambda n: inc[n].parent):
            term = inc[name]
            key = f"concept:{self.concept_iri(name)}"
            if self.tombstoned(key, f"concept {name}"):
                self.dropped_concepts.add(name)
                continue
            if term.parent in self.dropped_concepts:
                self.dropped_concepts.add(name)
                self.dependents += 1
                continue
            self.adds.append(
                AddConcept(
                    op="add_concept",
                    name=name,
                    display_name=term.display_name,
                    description=self.text(term.description),
                    parent=term.parent,
                    synonyms=list(term.synonyms),
                    **self.common(term.iri),
                )
            )
            for note in term.history_notes:
                self.adds.append(
                    AddHistoryNote(
                        op="add_history_note",
                        concept_fqn=name,
                        note=note[:2000],
                        lang="en",
                        **self.common(term.iri),
                    )
                )
            for note in term.scope_notes:
                self.adds.append(
                    AddScopeNote(
                        op="add_scope_note",
                        concept_fqn=name,
                        note=note[:2000],
                        lang="en",
                        **self.common(term.iri),
                    )
                )
        for name in sorted(n for n in inc if n in cur):
            term = inc[name]
            if term.deprecation and (reason := term.deprecation_reason):
                self.adds.append(
                    DeprecateConcept(
                        op="deprecate_concept",
                        fqn=name,
                        base_version=self.version(name),
                        reason=reason,
                        successor=None,
                        **self.common(term.iri),
                    )
                )
            self.update(cur[name], term)

    def update(self, cur: ConceptTerm, inc: ConceptTerm) -> None:
        changes: dict[str, Any] = {}
        if inc.display_name != cur.display_name:
            changes["display_name"] = inc.display_name or inc.name
        if inc.description is not None and inc.description != cur.description:
            changes["description"] = self.text(inc.description)
        if inc.synonyms != cur.synonyms:
            changes["synonyms"] = list(inc.synonyms)
        if inc.parent is not None and inc.parent != cur.parent:
            if inc.parent in self.dropped_concepts:
                self.dependents += 1  # only the parent change depends on it; keep the rest
            else:
                # OM 2.1.0-rc1 applies a parent change by re-inserting the term, which fails
                # on its own id (duplicate key): report the move instead of proposing it
                self.reparented.add(inc.name)
        if not changes:
            return
        self.adds.append(
            UpdateConcept(
                op="update_concept",
                fqn=inc.name,
                base_version=self.version(inc.name),
                **changes,
                **self.common(inc.iri),
            )
        )

    def attributes(self) -> None:
        for key in sorted(self.inc.attributes):
            term = self.inc.attributes[key]
            existing = self.cur.attributes.get(key)
            if existing is not None:
                if term.description is not None and term.description != existing.description:
                    self.description_edits += 1
                if _structure(term) == _structure(existing):
                    continue
            else:
                iri = f"{self.concept_iri(term.concept)}.{quote(term.name, safe='')}"
                if self.tombstoned(f"attr:{iri}", f"attribute {term.concept}.{term.name}"):
                    self.dropped_attributes.add(key)
                    continue
                if term.concept in self.dropped_concepts:
                    self.dropped_attributes.add(key)
                    self.dependents += 1
                    continue
            self.adds.append(
                AddAttribute(
                    op="add_attribute",
                    concept=term.concept,
                    name=term.name,
                    datatype=term.datatype,
                    is_identifier=term.is_identifier,
                    allowed_values=list(term.allowed_values),
                    unit=term.unit,
                    description=self.text(term.description),
                    **self.common(term.iri),
                )
            )

    def relations(self) -> None:
        current = {(s, r): t for s, r, t in self.cur.relations}
        for key in sorted(self.inc.relations):
            if key in self.cur.relations:
                continue  # descriptions of native properties cannot change on rc1 (R2)
            term = self.inc.relations[key]
            source, relation_type, target = key
            if (source, relation_type) in current:
                self.retargeted.add(f"{source}.{relation_type}")
                continue
            iri = f"{self.concept_iri(source)}.{quote(relation_type, safe='')}"
            if self.tombstoned(f"attr:{iri}", f"relation {source}.{relation_type}"):
                continue
            if {source, target} & self.dropped_concepts:
                self.dependents += 1
                continue
            self.adds.append(
                AddRelation(
                    op="add_relation",
                    source=source,
                    relation_type=relation_type,
                    target=target,
                    description=self.text(term.description),
                    **self.common(term.iri),
                )
            )

    def mappings(self) -> None:
        for m in sorted(self.inc.mappings - self.cur.mappings, key=_mapping_key):
            if m.concept in self.dropped_concepts:
                self.dependents += 1
                continue
            self.adds.append(
                AddMapping(
                    op="add_mapping",
                    concept=m.concept,
                    kind=m.kind,
                    target_iri=AnyUrl(m.target_iri),
                    **self.common(self.subject(m.concept)),
                )
            )

    def axioms(self) -> None:
        for concept in sorted(self.inc.axioms):
            term = self.inc.axioms[concept]
            existing = self.cur.axioms.get(concept)
            name = term.name
            if existing is not None:
                if existing.expressions == term.expressions:
                    continue
                names = self.names_of(concept)
                if len(names) == 1:
                    name = names[0]  # the upsert must address OM's own axiom (R2)
                elif self.replace:
                    self.remove_axioms(concept, [n for n in names if n != name])
            else:
                iri = f"{self.concept_iri(concept)}.{quote(name, safe='')}"
                existing_names = self.axiom_names.get(concept, [])
                if name not in existing_names and self.tombstoned(f"axiom:{iri}", f"axiom {name}"):
                    continue
            properties = {(concept, p) for p, _, _ in term.expressions}
            if concept in self.dropped_concepts or properties & self.dropped_attributes:
                self.dependents += 1
                continue
            body = _axiom_body(name, term)
            if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) > MAX_AXIOM_BYTES:
                self.oversized.add(name)  # AddAxiom would refuse it
                continue
            self.adds.append(
                AddAxiom(
                    op="add_axiom",
                    concept=concept,
                    axiom=body,
                    **self.common(self.subject(concept)),
                )
            )

    # removals (replace mode) ---------------------------------------------------------------

    def remove_axioms(self, concept: str, names: Iterable[str]) -> None:
        for name in names:
            self.removals.append(
                RemoveAxiom(
                    op="remove_axiom",
                    concept=concept,
                    name=name,
                    **self.common(self.concept_iri(concept)),
                )
            )

    def removed(self) -> list[ProposalOperation]:
        cur, inc = self.cur, self.inc
        for concept in sorted(set(cur.axioms) - set(inc.axioms)):
            self.remove_axioms(concept, self.names_of(concept))
        for m in sorted(cur.mappings - inc.mappings, key=_mapping_key):
            self.removals.append(
                RemoveMapping(
                    op="remove_mapping",
                    concept=m.concept,
                    kind=m.kind,
                    target_iri=AnyUrl(m.target_iri),
                    **self.common(self.concept_iri(m.concept)),
                )
            )
        for key in sorted(set(cur.relations) - set(inc.relations)):
            source, relation_type, target = key
            if f"{source}.{relation_type}" in self.retargeted:
                continue
            self.removals.append(
                RemoveRelation(
                    op="remove_relation",
                    source=source,
                    relation_type=relation_type,
                    target=target,
                    **self.common(cur.relations[key].iri),
                )
            )
        for key in sorted(set(cur.attributes) - set(inc.attributes)):
            term = cur.attributes[key]
            self.removals.append(
                RemoveAttribute(
                    op="remove_attribute",
                    concept=term.concept,
                    name=term.name,
                    **self.common(term.iri),
                )
            )
        gone = [n for n in cur.concepts if n not in inc.concepts]
        for name in reversed(_parents_first(gone, lambda n: cur.concepts[n].parent)):
            self.removals.append(
                RemoveConcept(
                    op="remove_concept",
                    fqn=name,
                    base_version=self.version(name),
                    **self.common(cur.concepts[name].iri),
                )
            )
        return self.removals

    def warnings(self) -> list[str]:
        out = list(self.inc.warnings)
        if self.tombstones:
            out.append(
                f"{len(self.tombstones)} definitions were deleted in OpenMetadata and cannot be "
                f"created again: {', '.join(sorted(self.tombstones))}"
            )
        if self.dependents:
            out.append(f"{self.dependents} operations that depend on them are left out")
        if self.retargeted:
            out.append(
                f"{len(self.retargeted)} relations change their target and are not imported "
                f"({_OM} cannot retarget an object property): "
                f"{', '.join(sorted(self.retargeted)[:10])}"
            )
        if self.reparented:
            out.append(
                f"{len(self.reparented)} concepts change their parent and are not moved "
                f"({_OM} cannot re-parent an existing term; move them in OpenMetadata): "
                f"{', '.join(sorted(self.reparented)[:10])}"
            )
        if self.oversized:
            out.append(
                f"{len(self.oversized)} axioms are too large to import (more than "
                f"{MAX_AXIOM_BYTES} bytes of JSON): {', '.join(sorted(self.oversized)[:10])}"
            )
        if self.description_edits:
            out.append(
                f"{self.description_edits} attribute descriptions cannot be changed on {_OM}"
            )
        return out


def _structure(term: AttributeTerm) -> tuple[object, ...]:
    """The fields an rc1 upsert of an existing native property can change (R2)."""
    return (term.datatype, term.is_identifier, term.allowed_values, term.unit)


def _mapping_key(m: MappingTerm) -> tuple[str, str, str]:
    return (m.concept, m.kind, m.target_iri)


def _axiom_body(name: str, term: AxiomTerm) -> dict[str, Any]:
    """The ``OntologyAxiom`` body of a concept's restrictions (the mapper's PK shape).

    The RDF carries no axiom label or description, but OpenMetadata 2.1.0-rc1 refuses a draft
    whose new axiom lacks them (400 ``query param displayName must not be null``), so both
    are the axiom's name, as when Lakeloom validates an axiom (``ProposalService``)."""
    return {
        "name": name,
        "displayName": name,
        "description": name,
        "axiomType": "SUBCLASS_OF",
        "expressions": [
            {
                "kind": "RESTRICTION",
                "restrictionKind": kind,
                "propertyName": prop,
                "cardinality": n,
            }
            for prop, kind, n in term.expressions
        ],
    }


def import_operations(
    current: TermSet,
    incoming: TermSet,
    *,
    mode: ImportMode,
    versions: Mapping[str, float],
    axiom_names: Mapping[str, list[str]],
    rule_id: ImportRule,
    deleted: Collection[str] = (),
) -> ImportOps:
    """The operations that turn ``current`` (the model's terms, read under its base IRI) into
    ``incoming`` (the file's): additions and updates, plus removals when ``mode="replace"``.

    Order: concepts (parents first), attributes, relations, mappings, axioms; then removals
    in reverse (axioms, mappings, relations, attributes, concepts children first). Every
    operation carries ``rule_id`` and one ``import`` evidence naming its subject IRI.
    ``versions`` maps a concept to its OM term version (updates, removals) and
    ``axiom_names`` a concept to its OM native axiom names (changed or removed axioms); a
    needed entry that is missing is a ``validation_error``. ``deleted`` holds
    :func:`~lakeloom.core.operations.native_member_key` keys of tombstoned definitions, which
    are not created again (with what depends on them) and are named in a warning.
    """
    diff = _Diff(
        current,
        incoming,
        mode=mode,
        versions=versions,
        axiom_names=axiom_names,
        rule_id=rule_id,
        deleted=deleted,
    )
    diff.concepts()
    diff.attributes()
    diff.relations()
    diff.mappings()
    diff.axioms()
    operations = [*diff.adds, *(diff.removed() if diff.replace else [])]
    counts = dict(Counter(op.op for op in operations))
    return ImportOps(operations=operations, counts=counts, warnings=diff.warnings())
