"""Proposals: scans and agent edits turned into OM draft change sets (FR-PRO-03..08).

A :class:`Proposal` is a list of :class:`ProposalPart`. Each part holds at most
``max_proposal_ops`` proposal operations (FR-PRO-08) and up to two OM change sets, created,
submitted and discarded together (hybrid model, ADR 0011): ``glossary`` (terms, relations,
mappings) named ``lakeloom-<proposal id>-<n>`` and ``native`` (classes, attributes, axioms)
named ``lakeloom-<proposal id>-<n>-native``. A part of realizations only has no change set:
realizations are Lakeloom-side until approval (M2b). Nothing is ever applied here.

``propose_from_scan`` maps a scan (:mod:`lakeloom.core.mapper`), removes what already exists
or is pending (:mod:`lakeloom.core.dedupe`), drops relations whose relationship type does not
exist in OM (with a warning), splits by table (tables sorted by full name, operations in
mapper order; whole tables are packed into a part, a table larger than the limit is split
into consecutive parts with a warning) and creates the change sets part by part. Every change
set description carries a Markdown summary of the part and a fenced ``lakeloom-provenance``
JSON block (FR-PRO-03).

Consistency with OM: all OM payloads are built (and fail closed) before the first write; each
part is stored right after its change sets exist; a change set whose companion write failed
is discarded and listed in ``Proposal.abandoned_change_sets``, and operations appended to one
side are removed again when the other side fails. So no OM change set is ever unrecorded and
no recorded operation is missing from OM. OM conflicts become ``conflict`` errors with the
hint to rebase.

Rebuild and rebase (FR-OM-03, ADR 0012 § Rebuild): :meth:`ProposalService.rebuild_parts`
re-drafts parts against OM's current state with the client it is given (the bot for
``rebase``, the reviewer's in review); ``rebase`` rebuilds every part that is not fully
applied and refreshes agent-supplied base versions with a warning. ``propose_change`` and
``submit`` accept a ``changes_requested`` proposal and rebuild it first.

Per-model access (NFR-SEC-07): ``propose_from_scan``, ``propose_change``,
``propose_realization`` and ``rebase`` require the model's ``propose_groups``
(:func:`~lakeloom.core.model_policy.require_group`; empty = anyone, the CLI Actor exempt).

Audit ownership (NFR-SEC-12): exactly one audit record per call of a write method
(``propose_from_scan``, ``propose_change``, ``propose_realization``, ``submit``, ``discard``,
``rebase``; ``tool`` = the MCP tool name, ``submit_proposal``, ``discard_proposal`` and
``rebase_proposal`` included, in the CLI too). The service writes it **only when
constructed with an** ``audit`` **repository** (the CLI). MCP tools construct it with
``audit=None`` and rely on ``guarded(audited=True, audit=...)``; the service still fills the
running call's :func:`~lakeloom.core.audit.current_audit` context (model, proposal, change
set, and the error code of a partial result, audited as ``error``) so guarded's record
carries them. Reads (``get_proposal``, ``summary_markdown``, ``diff``) are not audited.

Relations (hybrid model): each ``AddRelation`` is a glossary link plus a native object
property named after the relation; when OM lacks the relationship type the glossary link
uses ``relatedTo`` (with a warning). An unpaired model gets the glossary link only.

Ownership (owner ruling, ADR 0011): only the proposal's creator (``provenance.principal_subject``)
or a caller holding ``lakeloom:admin`` may change (``propose_change``, ``propose_realization``
into it), submit or discard a proposal; anyone else gets ``forbidden`` "proposal belongs to
another principal". The discard confirm token is bound to the caller as well.

Provenance of agent edits (FR-PRO-03): operations an agent passes to ``propose_change`` or
``propose_realization`` carry the rule id ``R-AGENT`` only; rule ids an agent supplies are
replaced, never trusted.

Concurrency: a per-proposal lock serialises edits, a per-model lock serialises
``propose_from_scan`` (dedupe reads the model's open proposals), and the store is
compare-and-set. The locks are in-process: with several replicas, two scans of one model at
the same instant can still propose the same operation (accepted limitation for M2a).
"""

import asyncio
import hashlib
import json
import logging
import uuid
import weakref
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import quote

from pydantic import BaseModel, Field

from lakeloom.config import Settings
from lakeloom.core.audit import AuditContext, AuditRecord, current_audit, record
from lakeloom.core.callers import Actor, Caller
from lakeloom.core.concepts import direct_glossary_tags, resolve_concept
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.context import resolve_asset
from lakeloom.core.dedupe import dedupe, import_slugs, screen
from lakeloom.core.mapper import map_scan
from lakeloom.core.model_policy import ModelPolicyEntry, access_for, policy_for, require_group
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.operations import (
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddMapping,
    AddRealization,
    AddRelation,
    AddSynonym,
    Evidence,
    OMContext,
    OMOps,
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
    remove_concept_native,
    to_om,
    to_om_all,
)
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMConflict, OMError, OMUnavailable
from lakeloom.om.models import OMGlossaryTerm, OMOntology
from lakeloom.sources.base import LakeSource
from lakeloom.state.audit import AuditRepository
from lakeloom.state.ids import new_ulid_id
from lakeloom.state.proposals import OPEN_STATUSES, ProposalRepository
from lakeloom.state.scans import ScanRecord, ScanRepository
from lakeloom.untrusted import code_span, quote_untrusted, single_line

__all__ = [
    "DISCARD_TOOL",
    "REBASE_HINT",
    "REBASE_STATUSES",
    "REBASE_TOOL",
    "AbandonedChangeSet",
    "Actor",
    "Caller",
    "ConfirmPreview",
    "EntityChange",
    "NothingToPropose",
    "Proposal",
    "ProposalDiff",
    "ProposalMatch",
    "ProposalPart",
    "ProposalService",
    "Provenance",
    "TableProvenance",
    "split_parts",
]

ProposalStatus = Literal[
    "draft", "submitted", "discarded", "applied", "apply_failed", "rejected", "changes_requested"
]
Side = Literal["glossary", "native"]
SIDES: tuple[Side, Side] = ("glossary", "native")
DISCARD_TOOL = "discard_proposal"
REBASE_HINT = (
    "the base version changed; call rebase_proposal, then submit_proposal for a new review"
)
UNPAIRED_HINT = "call create_model with pair_existing=true"
NOTHING = "nothing to propose (all matched existing concepts)"
DRAFT_TERM_VERSION = 0.1  # a term created in the same proposal; OM starts terms at 0.1
PROVENANCE_FENCE = "lakeloom-provenance"
REBASE_TOOL = "rebase_proposal"
# statuses rebase accepts (an applied, rejected or discarded proposal is final)
REBASE_STATUSES = ("draft", "submitted", "changes_requested", "apply_failed")
_GONE = ("DISCARDED", "MISSING")  # a change set OM no longer holds as a draft
_MAX_CS_NAME = 256  # OM's change-set name limit (ADR 0011)
# part number -> (its operations with refreshed base versions, the warnings that says)
type Refreshed = Mapping[int, tuple[list[ProposalOperation], list[str]]]
_NATIVE_OPS = (AddConcept, RemoveConcept, AddAttribute, RemoveAttribute, AddAxiom, RemoveAxiom)
_MAX_HINT_ITEMS = 20
_MAX_DETAIL_ITEMS = 100  # per list in NothingToPropose.details()
AGENT_RULE = "R-AGENT"  # the only rule id of an operation an agent passed (not trusted)
ADMIN_SCOPE = "lakeloom:admin"
NOT_OWNER = "proposal belongs to another principal"
_log = logging.getLogger(__name__)
_OM_READS = 8  # concurrent change-set reads


# --- data -----------------------------------------------------------------------------------


def _sides() -> dict[str, str | None]:
    return {"glossary": None, "native": None}


class ProposalPart(BaseModel):
    """One slice of a proposal and its OM change sets (``None`` where a side has no ops)."""

    n: int
    table: str | None = None  # the table when the part covers exactly one
    tables: list[str] = []
    operations: list[ProposalOperation] = []
    change_set_ids: dict[str, str | None] = Field(default_factory=_sides)
    change_set_names: dict[str, str | None] = Field(default_factory=_sides)
    om_state: dict[str, str | None] = Field(default_factory=_sides)
    # rebuilds (rebase, ADR 0012 § Rebuild): replaced change sets, the count so far, and the
    # names the sides had before their first rebuild (rebuilt names add ``-r<k>``)
    superseded_change_set_ids: list[str] = []
    rebuilds: int = 0
    original_names: dict[str, str | None] = Field(default_factory=_sides)


class TableProvenance(BaseModel):
    fqn: str  # the lake table's full name
    om_fqn: str | None = None
    version: int | None = None


class Provenance(BaseModel):
    """FR-PRO-03: where a proposal came from."""

    scan_id: str | None
    tables: list[TableProvenance] = []
    rule_ids: list[str] = []
    principal_subject: str
    principal_type: str
    client: str | None = None
    at: datetime
    ai_generated: bool = False


class ProposalMatch(BaseModel):
    """An existing concept a proposed one resembles (FR-PRO-02)."""

    op_key: str
    candidate_fqn: str
    confidence: float
    matched_on: str
    action: str


class AbandonedChangeSet(BaseModel):
    """A change set discarded because its companion write failed (kept for the record)."""

    id: str
    name: str
    part: int
    side: str
    discarded: bool


class Proposal(BaseModel):
    id: str
    model: str
    status: ProposalStatus
    parts: list[ProposalPart] = []
    scan_id: str | None = None
    provenance: Provenance
    matches: list[ProposalMatch] = []
    warnings: list[str] = []
    abandoned_change_sets: list[AbandonedChangeSet] = []
    # a rebase stopped part-way: the next propose_change / submit / rebase finishes it
    rebase_pending: bool = False
    created_at: datetime
    updated_at: datetime
    revision: int = 0  # compare-and-set counter of the repository

    def change_set_ids(self) -> list[str]:
        """Every OM change set id of the proposal, in part order (glossary, then native)."""
        return [
            cs
            for part in self.parts
            for side in SIDES
            if (cs := part.change_set_ids.get(side)) is not None
        ]

    def operations(self) -> list[ProposalOperation]:
        return [op for part in self.parts for op in part.operations]


class ConfirmPreview(BaseModel):
    """Dry run of a destructive call: what would happen and the token to confirm it."""

    summary: str
    confirm_token: str
    expires_at: int


class EntityChange(BaseModel):
    entity: str
    kind: str
    change: Literal["added", "changed", "removed"]
    part: int
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class ProposalDiff(BaseModel):
    proposal_id: str
    model: str
    changes: list[EntityChange] = []


class NothingToPropose(LakeloomError):
    """``validation_error``: dedupe left no operation; ``matches``/``warnings`` say why."""

    def __init__(self, matches: list[ProposalMatch], warnings: list[str]) -> None:
        shown = [f"{m.op_key} -> {m.candidate_fqn} ({m.confidence:.2f})" for m in matches]
        shown += warnings
        more = len(shown) - _MAX_HINT_ITEMS
        hint = "; ".join(_inline(s) for s in shown[:_MAX_HINT_ITEMS])
        if more > 0:
            hint += f"; and {more} more"
        super().__init__(
            "validation_error",
            NOTHING,
            hint or "the scan held nothing new for this model",
        )
        self.matches = matches
        self.warnings = warnings

    def details(self) -> dict[str, Any]:
        """Matches and warnings, at most 100 of each; ``*_truncated`` counts the rest."""
        cap = _MAX_DETAIL_ITEMS
        return {
            "matches": [m.model_dump(mode="json") for m in self.matches[:cap]],
            "warnings": list(self.warnings[:cap]),
            "matches_truncated": max(0, len(self.matches) - cap),
            "warnings_truncated": max(0, len(self.warnings) - cap),
        }


# --- pure helpers ---------------------------------------------------------------------------


def _fail(message: str, hint: str) -> LakeloomError:
    return LakeloomError("validation_error", message, hint)


def _now() -> datetime:
    return datetime.now(UTC)


def _table_of(op: ProposalOperation, tables: Sequence[str]) -> str | None:
    """The scanned table an operation came from (from its evidence), or ``None``."""
    by_length = sorted(tables, key=len, reverse=True)
    for evidence in op.evidence:
        for name in by_length:
            if evidence.ref == name or evidence.ref.startswith(name + "."):
                return name
    return None


def split_parts(
    ops: Sequence[ProposalOperation], tables: Sequence[str], limit: int
) -> tuple[list[tuple[list[str], list[ProposalOperation]]], list[str]]:
    """Split ``ops`` into parts of at most ``limit`` operations (FR-PRO-08).

    Operations are grouped by source table, tables in full-name order (operations without a
    table last), each group in its original order. Whole tables are packed into a part while
    they fit; a table with more than ``limit`` operations gets consecutive parts of its own
    and a warning. Returns ``[(tables, operations), ...]`` and the warnings.
    """
    limit = max(1, limit)
    groups: dict[str | None, list[ProposalOperation]] = {}
    for op in ops:
        groups.setdefault(_table_of(op, tables), []).append(op)
    keys: list[str | None] = [*sorted(k for k in groups if k is not None)]
    if None in groups:
        keys.append(None)
    parts: list[tuple[list[str], list[ProposalOperation]]] = []
    warnings: list[str] = []
    cur_tables: list[str] = []
    cur_ops: list[ProposalOperation] = []

    def flush() -> None:
        nonlocal cur_tables, cur_ops
        if cur_ops:
            parts.append((cur_tables, cur_ops))
        cur_tables, cur_ops = [], []

    for key in keys:
        chunk = groups[key]
        if len(chunk) > limit:
            flush()
            pieces = [chunk[i : i + limit] for i in range(0, len(chunk), limit)]
            what = f"table {key}" if key is not None else "operations without a table"
            warnings.append(
                f"{what} has {len(chunk)} operations, more than the limit of {limit}; "
                f"split into {len(pieces)} consecutive parts"
            )
            parts.extend(([key] if key is not None else [], piece) for piece in pieces)
            continue
        if len(cur_ops) + len(chunk) > limit:
            flush()
        if key is not None:
            cur_tables.append(key)
        cur_ops.extend(chunk)
    flush()
    return parts, warnings


def _bare(ref: str) -> str:
    return ref.rsplit(".", 1)[-1]


def _local_ref(ref: str, model: str) -> str | None:
    """The bare name of a ref to ``model`` (``None`` for a ref into another model). An FQN's
    first segment is the model (child terms nest: ``retail.Order.OrderStatus``)."""
    if "." in ref and ref.split(".", 1)[0] != model:
        return None
    return _bare(ref)


def _term_refs(op: ProposalOperation) -> list[str]:
    """Refs whose OM term id (and version) the operation's OM payload needs."""
    match op:
        case AddConcept():
            return [op.parent] if op.parent else []
        case UpdateConcept():
            return [op.fqn, *([op.parent] if op.parent else [])]
        case RemoveConcept():
            return [op.fqn]
        case AddRelation():
            return [op.source, op.target]
        case RemoveRelation():
            return [op.source]
        case AddMapping() | RemoveMapping():
            return [op.concept]
        case AddSynonym():
            return [op.concept_fqn]
        case _:
            return []


def _native_refs(op: ProposalOperation) -> list[str]:
    """Concepts whose class a native operation points at (their term IRI is needed)."""
    match op:
        case AddAttribute() | AddAxiom() | RemoveAttribute() | RemoveAxiom():
            return [op.concept]
        case AddRelation():
            return [op.source, op.target]
        case _:
            return []


def _updated_term(op: ProposalOperation) -> str | None:
    if isinstance(op, UpdateConcept):
        return _bare(op.fqn)
    if isinstance(op, AddSynonym):
        return _bare(op.concept_fqn)
    return None


def _needs_native(ops: Iterable[ProposalOperation]) -> bool:
    return any(isinstance(op, _NATIVE_OPS) for op in ops)


def _rule_ids(ops: Iterable[ProposalOperation]) -> list[str]:
    return sorted({r for op in ops for r in op.rule_ids})


def _truncated(op: ProposalOperation) -> bool:
    return any(e.truncated for e in op.evidence)


def _inline(text: str) -> str:
    """Untrusted text on one Markdown line, inert: no line breaks, and ``\\``, ``[``, ``]``
    and ``!`` escaped so no link or image can render."""
    out = single_line(text).replace("\\", "\\\\")
    for ch in "[]!":
        out = out.replace(ch, "\\" + ch)
    return out


def _json_block(data: Mapping[str, Any]) -> str:
    """Compact JSON that cannot close its Markdown fence (backticks as ``\\u0060``)."""
    text = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return text.replace("`", "\\u0060")


def provenance_block(proposal: Proposal, part: ProposalPart) -> dict[str, Any]:
    """The machine-readable provenance of one part (FR-PRO-03)."""
    prov = proposal.provenance
    tables = [t for t in prov.tables if t.fqn in part.tables] if part.tables else prov.tables
    return {
        "lakeloom_provenance": 1,
        "proposal_id": proposal.id,
        "part": part.n,
        "model": proposal.model,
        "scan_id": prov.scan_id,
        "tables": [t.model_dump(mode="json") for t in tables],
        "rule_ids": _rule_ids(part.operations),
        "principal_subject": prov.principal_subject,
        "principal_type": prov.principal_type,
        "client": prov.client,
        "at": prov.at.isoformat(),
        "ai_generated": prov.ai_generated,
        "operations": [
            {"key": op_key(op), "rule_ids": list(op.rule_ids), "truncated": _truncated(op)}
            for op in part.operations
        ],
    }


def _count_line(ops: Sequence[ProposalOperation]) -> list[str]:
    counts: dict[str, list[str]] = {}
    for op in ops:
        counts.setdefault(op.op, []).append(op_key(op))
    lines: list[str] = []
    for kind in sorted(counts):
        keys = counts[kind]
        shown = ", ".join(code_span(k) for k in keys[:10])
        more = f" and {len(keys) - 10} more" if len(keys) > 10 else ""
        lines.append(f"- {kind}: {len(keys)} ({shown}{more})")
    return lines


def part_description(proposal: Proposal, part: ProposalPart, side: Side) -> str:
    """OM change-set description: a short summary plus the provenance block."""
    tables = ", ".join(code_span(t) for t in part.tables) or "none"
    lines = [
        f"Lakeloom proposal {code_span(proposal.id)}, part {part.n} ({side} change set) "
        f"for model {code_span(proposal.model)}.",
        "",
        f"Source tables: {tables}. Operations in this part: {len(part.operations)}.",
        "Operations may be appended later; the Lakeloom store is authoritative.",
        "",
        *_count_line(part.operations),
        "",
        f"```{PROVENANCE_FENCE}",
        _json_block(provenance_block(proposal, part)),
        "```",
    ]
    return "\n".join(lines)


def parse_provenance(description: str) -> dict[str, Any]:
    """Read the ``lakeloom-provenance`` block back from a change-set description."""
    start = description.index(f"```{PROVENANCE_FENCE}\n") + len(PROVENANCE_FENCE) + 4
    end = description.index("\n```", start)
    data = json.loads(description[start:end])
    if not isinstance(data, dict):
        raise ValueError("provenance block is not an object")
    return data  # pyright: ignore[reportUnknownVariableType]


def _indent(block: str, by: str = "  ") -> str:
    return "\n".join(by + line for line in block.splitlines())


_TITLES = {
    "add_concept": "Concepts",
    "add_attribute": "Attributes",
    "add_relation": "Relations",
    "add_mapping": "Mappings",
    "add_axiom": "Axioms",
    "add_synonym": "Synonyms",
    "add_realization": "Realizations",
    "update_concept": "Concepts",
    "remove_concept": "Concepts",
    "remove_attribute": "Attributes",
    "remove_relation": "Relations",
    "remove_mapping": "Mappings",
    "remove_axiom": "Axioms",
    "remove_realization": "Realizations",
}
_ORDER = list(dict.fromkeys(_TITLES.values()))


def _op_line(op: ProposalOperation) -> str:
    match op:
        case AddConcept():
            parent = f" under {code_span(op.parent)}" if op.parent else ""
            head = f"{code_span(op.name)}{parent}"
        case AddAttribute():
            ident = ", identifier" if op.is_identifier else ""
            head = f"{code_span(f'{op.concept}.{op.name}')} ({code_span(op.datatype)}{ident})"
            if op.allowed_values:
                head += " values " + ", ".join(code_span(v) for v in op.allowed_values[:20])
        case AddRelation() | RemoveRelation():
            head = f"{code_span(op.source)} {code_span(op.relation_type)} {code_span(op.target)}"
        case AddMapping() | RemoveMapping():
            head = f"{code_span(op.concept)} {op.kind} {code_span(str(op.target_iri))}"
        case AddAxiom():
            head = f"{code_span(str(op.axiom['name']))} on {code_span(op.concept)}"
        case AddSynonym():
            head = f"{code_span(op.synonym)} for {code_span(op.concept_fqn)}"
        case AddRealization() | RemoveRealization():
            column = f" column {code_span(op.column)}" if op.column else ""
            head = f"{code_span(op.concept)} by {code_span(op.asset_fqn)}{column}"
        case _:
            head = code_span(op_key(op))
    rules = ", ".join(code_span(r) for r in op.rule_ids) or "none"
    return f"- {head} — rules: {rules}"


def _evidence_lines(op: ProposalOperation) -> list[str]:
    lines: list[str] = []
    refs = [f"{e.kind} {code_span(e.ref)}" for e in op.evidence if e.kind != "comment"]
    if refs:
        lines.append("  - evidence: " + ", ".join(refs[:10]))
    description = getattr(op, "description", None)
    if description is not None:
        lines.append(_indent(quote_untrusted(description.text, source=description.source)))
    if op.rationale is not None:
        lines.append(_indent(quote_untrusted(op.rationale.text, source=op.rationale.source)))
    for e in op.evidence:
        if e.detail:
            lines.append(_indent(quote_untrusted(e.detail, source=f"evidence {e.kind}")))
    return lines


def summary_markdown(p: Proposal) -> str:
    """FR-PRO-06: what is added, changed and removed per kind, why (rules and evidence),
    warnings and matches. Untrusted text is quoted; names are inert code spans."""
    prov = p.provenance
    ops = p.operations()
    client = f" via {code_span(prov.client)}" if prov.client else ""
    lines = [
        f"# Proposal {code_span(p.id)} for model {code_span(p.model)}",
        "",
        f"- Status: {p.status}",
        f"- Operations: {len(ops)} in {len(p.parts)} part(s)",
        f"- Scan: {code_span(p.scan_id) if p.scan_id else 'none'}",
        f"- Requested by: {_inline(prov.principal_type)} "
        f"{code_span(prov.principal_subject)}{client} at {prov.at.isoformat()}",
        "",
        "## Parts",
    ]
    for part in p.parts:
        tables = ", ".join(code_span(t) for t in part.tables) or "no table"
        sets = [
            f"{side} change set {code_span(cs)} ({part.om_state.get(side) or 'unknown'})"
            for side in SIDES
            if (cs := part.change_set_ids.get(side)) is not None
        ]
        held = "; ".join(sets) if sets else "no OM change set (realizations only)"
        lines.append(f"- Part {part.n} ({tables}): {len(part.operations)} operations; {held}")
    for change, prefix in (("Added", "add_"), ("Changed", "update_"), ("Removed", "remove_")):
        chosen = [op for op in ops if op.op.startswith(prefix)]
        if not chosen:
            continue
        lines += ["", f"## {change}"]
        for title in _ORDER:
            group = [op for op in chosen if _TITLES[op.op] == title]
            if not group:
                continue
            lines += ["", f"### {title} ({len(group)})"]
            for op in group:
                lines.append(_op_line(op))
                lines.extend(_evidence_lines(op))
    if p.matches:
        lines += ["", "## Matches with existing concepts"]
        for m in p.matches:
            lines.append(
                f"- {code_span(m.op_key)} -> {code_span(m.candidate_fqn)} "
                f"(confidence {m.confidence:.2f}, matched on {_inline(m.matched_on)}; "
                f"{_inline(m.action)})"
            )
    if p.warnings:
        lines += ["", "## Warnings"]
        lines.extend(f"- {code_span(w)}" for w in p.warnings)
    if p.abandoned_change_sets:
        lines += ["", "## Abandoned change sets"]
        for a in p.abandoned_change_sets:
            state = "discarded" if a.discarded else "could not be discarded"
            lines.append(f"- {code_span(a.name)} ({code_span(a.id)}), part {a.part}: {state}")
    return "\n".join(lines).rstrip() + "\n"


_DIFF_EXCLUDE = {"op", "rule_ids", "evidence", "rationale"}


def diff(p: Proposal) -> ProposalDiff:
    """Structured per-entity changes. ``after`` is the proposed state; ``before`` is known
    only for removals (the removed identity); updates are relative to the OM term."""
    changes: list[EntityChange] = []
    for part in p.parts:
        for op in part.operations:
            kind = _TITLES[op.op].lower().rstrip("s")
            data = op.model_dump(mode="json", exclude=_DIFF_EXCLUDE, exclude_none=True)
            entity = op_key(op).removeprefix("remove-").removeprefix("update-")
            if op.op.startswith("remove_"):
                changes.append(
                    EntityChange(
                        entity=entity, kind=kind, change="removed", part=part.n, before=data
                    )
                )
            else:
                change: Literal["added", "changed"] = (
                    "changed" if op.op.startswith("update_") else "added"
                )
                changes.append(
                    EntityChange(entity=entity, kind=kind, change=change, part=part.n, after=data)
                )
    return ProposalDiff(proposal_id=p.id, model=p.model, changes=changes)


# --- OM error mapping -----------------------------------------------------------------------


def _om_error(exc: OMError, what: str) -> LakeloomError:
    if isinstance(exc, OMConflict):
        ids = ", ".join(exc.entity_ids) or "not named by OpenMetadata"
        if exc.kind == "exists":
            return LakeloomError(
                "conflict",
                f"OpenMetadata refused {what}: the name is already taken",
                "call get_proposal to see what was created, then retry",
            )
        if exc.kind == "lease":
            return LakeloomError(
                "conflict",
                f"OpenMetadata refused {what}: the change set is held by another session "
                f"(affected: {ids})",
                f"retry shortly; if it keeps failing, {REBASE_HINT}",
            )
        return LakeloomError(
            "conflict",
            f"OpenMetadata refused {what}: the base version is stale (affected: {ids})",
            REBASE_HINT,
        )
    if isinstance(exc, OMUnavailable):
        return LakeloomError(
            "upstream_unavailable",
            f"OpenMetadata is unavailable while {what}",
            "retry shortly; check that OpenMetadata is running",
        )
    detail = f": {code_span(exc.detail)}" if exc.detail else ""
    if exc.status == 404:
        return LakeloomError(
            "not_found",
            f"OpenMetadata has no such entity while {what}{detail}",
            "call get_proposal to refresh the proposal's OM state",
        )
    if exc.status is not None and 400 <= exc.status < 500:
        return LakeloomError(
            "validation_error",
            f"OpenMetadata rejected {what} (HTTP {exc.status}){detail}",
            "check the operations; OpenMetadata's message is quoted above",
        )
    return LakeloomError(
        "upstream_unavailable",
        f"OpenMetadata failed while {what}",
        "retry shortly; check the OpenMetadata logs",
    )


async def _om[T](what: str, call: Awaitable[T]) -> T:
    try:
        return await call
    except OMError as exc:
        raise _om_error(exc, what) from None
    except ValueError as exc:  # client misuse: a Lakeloom bug, never a raw ValueError
        raise LakeloomError(
            "validation_error",
            f"internal error while {what} ({type(exc).__name__})",
            "report this to the Lakeloom operators",
        ) from None


def _declared_iri(item: Mapping[str, Any]) -> str | None:
    """The IRI an UPSERT_CONCEPT / UPSERT_PROPERTY operation declares."""
    for key in ("concept", "property"):
        entity: object = item.get(key)
        if isinstance(entity, dict):
            iri = cast("dict[str, Any]", entity).get("iri")
            if isinstance(iri, str):
                return iri
    return None


def _property_iris(axiom: Mapping[str, Any]) -> set[str]:
    expressions: object = axiom.get("expressions")
    if not isinstance(expressions, list):
        return set()
    out: set[str] = set()
    for expr in cast("list[object]", expressions):
        if isinstance(expr, dict):
            iri = cast("dict[str, Any]", expr).get("propertyIri")
            if isinstance(iri, str):
                out.add(iri)
    return out


def _with_ids(ops: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copies of OM operations with a fresh operation ``id`` (needed to undo an append)."""
    return [{**op, "id": str(uuid.uuid4())} for op in ops]


@dataclass(frozen=True)
class _Done:
    """One OM write of a part: a created change set (``appended_to`` None) or an append."""

    side: Side
    appended_to: str | None
    op_ids: list[str]


@dataclass
class _Target:
    info: ModelInfo
    base_iri: str
    ontology: OMOntology | None


# --- service --------------------------------------------------------------------------------


class ProposalService:
    def __init__(
        self,
        om: OMClient,
        models: ModelService,
        scans: ScanRepository,
        repo: ProposalRepository,
        audit: AuditRepository | None,
        settings: Settings,
        *,
        confirm: ConfirmTokens,
        sources: Mapping[str, LakeSource] | None = None,
        policies: Sequence[ModelPolicyEntry] = (),
    ) -> None:
        self._om = om
        self._models = models
        self._scans = scans
        self._repo = repo
        self._audit = audit
        self._settings = settings
        self._confirm = confirm
        self._sources: Mapping[str, LakeSource] = sources or {}
        self._policies = list(policies)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @property
    def max_ops(self) -> int:
        return max(1, self._settings.max_proposal_ops)

    def _lock(self, proposal_id: str) -> asyncio.Lock:
        lock = self._locks.get(proposal_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[proposal_id] = lock
        return lock

    # -- audit ---------------------------------------------------------------------------

    async def _audited[T](
        self,
        tool: str,
        caller: Caller,
        client: str | None,
        run: Callable[[AuditContext], Awaitable[tuple[T, str | None]]],
    ) -> T:
        """Run a write; one audit record (when this service owns auditing, see module doc).

        ``run`` returns its value and an error code for a partial result (still returned)."""
        trail = current_audit()
        try:
            value, partial = await run(trail)
            trail.error_code = partial  # guarded() audits a partial result as an error
        except LakeloomError as exc:
            await self._record(tool, caller, client, trail, exc.code)
            raise
        except BaseException as exc:
            code = "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal"
            await asyncio.shield(self._record(tool, caller, client, trail, code))
            raise
        await self._record(tool, caller, client, trail, partial, ok=partial is None)
        return value

    async def _record(
        self,
        tool: str,
        caller: Caller,
        client: str | None,
        trail: AuditContext,
        code: str | None,
        *,
        ok: bool = False,
    ) -> None:
        if self._audit is None:
            return  # MCP: guarded() writes the record (module docstring)
        rec = AuditRecord(
            at=_now(),
            principal_subject=caller.subject,
            principal_type=str(caller.type),
            client=client,
            tool=tool,
            model=trail.model,
            proposal_id=trail.proposal_id,
            change_set_id=trail.change_set_id,
            result="ok" if ok else "forbidden" if code == "forbidden" else "error",
            error_code=code,
        )
        await record(self._audit, rec)

    # -- persistence ---------------------------------------------------------------------

    async def _save(self, p: Proposal) -> None:
        expected = p.revision
        p.revision += 1
        p.updated_at = _now()
        try:
            await self._repo.update(p, expected_revision=expected)
        except BaseException:
            p.revision = expected
            raise

    # -- OM context ----------------------------------------------------------------------

    async def _target(self, model: str, *, om: OMClient | None = None) -> _Target:
        om = om or self._om
        info = await self._models.get_model(model, om=om)
        if info.base_iri is None:
            raise _fail(
                f"model {model!r} has no base IRI",
                "configure LAKELOOM_PUBLIC_URL so model IRIs can be derived",
            )
        ontology = await _om(
            f"looking up the native ontology of {model!r}", om.get_ontology_by_name(model)
        )
        return _Target(info, info.base_iri, ontology)

    def _require_pair(self, target: _Target, ops: Iterable[ProposalOperation]) -> None:
        if target.ontology is None and _needs_native(ops):
            raise _fail(
                f"model {target.info.slug!r} has no paired native ontology for concepts, "
                "attributes and axioms",
                UNPAIRED_HINT,
            )

    async def _terms_by_name(
        self, info: ModelInfo, *, om: OMClient | None = None
    ) -> dict[str, list[OMGlossaryTerm]]:
        om = om or self._om
        out: dict[str, list[OMGlossaryTerm]] = {}
        after: str | None = None
        seen: set[str] = set()
        while True:
            page = await _om(
                f"listing the terms of {info.slug!r}",
                om.list_terms(info.glossary_id, after=after, limit=100),
            )
            for term in page.data:
                out.setdefault(term.name, []).append(term)
            if not page.after or page.after in seen:
                return out
            seen.add(page.after)
            after = page.after

    async def _context(
        self,
        target: _Target,
        ops: Sequence[ProposalOperation],
        *,
        existing: Collection[str] = (),
        deleted: Collection[str] = (),
        om: OMClient | None = None,
    ) -> OMContext:
        """Everything ``to_om_all`` needs for ``ops`` (the whole proposal), read from OM.
        Fails closed (``validation_error``) when a referenced term is missing. ``existing``
        names concepts an ``AddConcept`` in ``ops`` created that OM now has (an applied part):
        they resolve as existing terms. ``deleted`` names terms an applied ``RemoveConcept``
        deleted: they are not looked up (only their native class is still addressed)."""
        om = om or self._om
        slug = target.info.slug
        created = {
            op.name: op for op in ops if isinstance(op, AddConcept) and op.name not in existing
        }
        term_ids: dict[str, str] = {n: draft_term_id(op) for n, op in created.items()}
        term_versions: dict[str, float] = dict.fromkeys(created, DRAFT_TERM_VERSION)
        terms: dict[str, TermSnapshot] = {}
        wanted = {
            name
            for op in ops
            for ref in (*_term_refs(op), *_native_refs(op))
            if (name := _local_ref(ref, slug)) is not None
            and name not in created
            and name not in deleted
        }
        if wanted:
            by_name = await self._terms_by_name(target.info, om=om)
            for name in sorted(wanted):
                found = by_name.get(name, [])
                if not found:
                    raise await self._missing_concept(slug, name)
                if len(found) > 1:
                    fqns = ", ".join(code_span(t.fully_qualified_name) for t in found)
                    raise _fail(f"concept name {name!r} is ambiguous in {slug!r}", fqns)
                term = found[0]
                if term.version is None:
                    raise LakeloomError(
                        "upstream_unavailable",
                        f"OpenMetadata returned term {name!r} without a version",
                        "retry shortly; check the OpenMetadata logs",
                    )
                term_ids[name] = term.id
                term_versions[name] = term.version
                terms[name] = TermSnapshot(
                    description=term.description or "",
                    version=term.version,
                    display_name=term.display_name,
                    iri=term.iri,
                    parent_id=term.parent.id if term.parent else None,
                    synonyms=tuple(term.synonyms),
                )
        relationship_types: dict[str, str] = {}
        if any(isinstance(op, AddRelation) for op in ops):
            relationship_types = await _om(
                "listing relationship types", om.list_relationship_types()
            )
        native_ids: dict[str, str] = {}
        native_versions: dict[str, float] = {}
        native_classes: set[str] | None = None
        removes = any(isinstance(op, RemoveConcept | RemoveAttribute | RemoveAxiom) for op in ops)
        if target.ontology is not None and (removes or terms):
            concepts, properties, axioms = await _om(
                f"listing the native members of {slug!r}", om.list_native_members(slug)
            )
            # existing terms whose class the native ontology lacks get one (pair_existing)
            native_classes = {str(iri) for m in concepts if (iri := m.get("iri")) is not None}
            for prefix, members in (("concept", concepts), ("attr", properties)):
                for m in members:
                    _native_entry(native_ids, native_versions, f"{prefix}:{m.get('iri')}", m)
            for m in axioms:
                iri = f"{m.get('subjectIri')}.{quote(str(m.get('name')), safe='')}"
                _native_entry(native_ids, native_versions, f"axiom:{iri}", m)
        return OMContext(
            glossary_name=slug,
            glossary_id=target.info.glossary_id,
            base_iri=target.base_iri,
            ontology_id=target.ontology.id if target.ontology else "",
            ontology_name=slug,
            term_ids=term_ids,
            term_versions=term_versions,
            terms=terms,
            relationship_type_ids=relationship_types,
            native_ids=native_ids,
            native_versions=native_versions,
            native_classes=native_classes,
            paired=target.ontology is not None,
        )

    async def _missing_concept(self, slug: str, name: str) -> LakeloomError:
        """The error for a referenced concept OM does not have: pending in another open
        proposal (wait for it), or unknown (propose it here)."""
        pid = (await self._repo.pending_op_keys(slug)).get(f"concept:{name}")
        if pid is not None:
            return _fail(
                f"concept {name!r} does not exist in model {slug!r} yet",
                f"{name} is pending in proposal {pid}; propose again after it is applied "
                "(do not propose it a second time)",
            )
        return _fail(
            f"concept {name!r} does not exist in model {slug!r}",
            "propose it with add_concept in the same proposal, or check the name with "
            "find_concepts",
        )

    @staticmethod
    def _relation_warnings(ops: Iterable[ProposalOperation], ctx: OMContext) -> list[str]:
        """Say where a relation's glossary link falls back to ``relatedTo`` or is the only
        representation (unpaired model)."""
        out: list[str] = []
        for op in ops:
            if not isinstance(op, AddRelation):
                continue
            if op.relation_type not in ctx.relationship_type_ids:
                out.append(
                    f"relationship type {op.relation_type} not defined in OpenMetadata; glossary "
                    f"link uses relatedTo (native object property keeps {op.relation_type})"
                )
            if not ctx.paired:
                out.append(
                    f"model {ctx.glossary_name} has no paired native ontology; {op_key(op)} is a "
                    "glossary link only (call create_model with pair_existing=true)"
                )
        return sorted(set(out))

    # -- change sets -----------------------------------------------------------------------

    def _cs_name(self, p: Proposal, n: int, side: Side) -> str:
        base = f"lakeloom-{p.id}-{n}" + ("-native" if side == "native" else "")
        taken = {a.name for a in p.abandoned_change_sets}
        name, k = base, 1
        while name in taken:
            k += 1
            name = f"{base}-r{k}"
        return name

    async def _create_side(
        self,
        p: Proposal,
        part: ProposalPart,
        shown: ProposalPart,
        side: Side,
        ops: list[dict[str, Any]],
        t: _Target,
        *,
        om: OMClient | None = None,
        name: str | None = None,
    ) -> None:
        om = om or self._om
        name = name or self._cs_name(p, part.n, side)
        description = part_description(p, shown, side)
        display = f"Lakeloom proposal {p.id} part {part.n} ({side})"
        if side == "glossary":
            cs = await _om(
                f"creating change set {name}",
                om.create_change_set(
                    name=name,
                    glossary_id=t.info.fqn,
                    description=description,
                    operations=ops,
                    display_name=display,
                ),
            )
        else:
            if t.ontology is None:  # _require_pair ran first; never reached
                raise _fail(f"model {t.info.slug!r} has no paired native ontology", UNPAIRED_HINT)
            state = await _om(
                f"reading the working revision of {t.info.slug!r}",
                om.get_ontology_state(t.ontology.id),
            )
            cs = await _om(
                f"creating change set {name}",
                om.create_change_set(
                    name=name,
                    glossary_id=None,
                    ontology=t.info.slug,
                    base_working_revision=state.working_revision,
                    description=description,
                    operations=ops,
                    display_name=display,
                ),
            )
        part.change_set_ids[side] = cs.id
        part.change_set_names[side] = cs.name
        part.om_state[side] = str(cs.state)

    async def _abandon(self, p: Proposal, part: ProposalPart, side: Side) -> None:
        """Discard a change set whose companion write failed; record it on the proposal."""
        cs_id, name = part.change_set_ids[side], part.change_set_names[side]
        if cs_id is None:
            return
        discarded = True
        try:
            await self._om.discard_change_set(cs_id)
        except (OMError, ValueError):
            discarded = False
        p.abandoned_change_sets.append(
            AbandonedChangeSet(
                id=cs_id, name=name or cs_id, part=part.n, side=side, discarded=discarded
            )
        )
        part.change_set_ids[side] = part.change_set_names[side] = part.om_state[side] = None

    async def _write_part(
        self, p: Proposal, part: ProposalPart, chunk: list[ProposalOperation], om: OMOps, t: _Target
    ) -> list[_Done]:
        """Put ``om`` into the part's change sets (creating missing sides), all or nothing,
        then add ``chunk`` to the part. On any failure, cancellation included, what was
        written is undone (shielded) before the error propagates. Returns what was written,
        for :meth:`_save_or_undo`."""
        done: list[_Done] = []
        sides: list[tuple[Side, list[dict[str, Any]]]] = [
            ("glossary", om.glossary),
            ("native", om.native),
        ]
        try:
            for side, ops in sides:
                if not ops:
                    continue
                prepared = _with_ids(ops)
                cs_id = part.change_set_ids[side]
                if cs_id is None:
                    # the description lists the part as it will be once this write succeeds
                    shown = part.model_copy(update={"operations": [*part.operations, *chunk]})
                    await self._create_side(p, part, shown, side, prepared, t)
                    done.append(_Done(side, None, []))
                else:
                    cs = await _om(
                        f"adding operations to change set {cs_id}",
                        self._om.add_operations(cs_id, prepared),
                    )
                    part.om_state[side] = str(cs.state)
                    done.append(_Done(side, cs_id, [str(o["id"]) for o in prepared]))
        except BaseException:
            left = await asyncio.shield(self._undo(p, part, done))
            p.warnings.extend(
                f"change set {cs} may still hold operations that are not in this proposal; "
                "discard the proposal and propose again"
                for cs in left
            )
            raise
        part.operations.extend(chunk)
        return done

    async def _undo(self, p: Proposal, part: ProposalPart, done: Sequence[_Done]) -> list[str]:
        """Revert writes of one part: discard created change sets (recorded as abandoned),
        remove appended operations. Returns the change sets that could not be reverted."""
        left: list[str] = []
        for d in reversed(done):
            if d.appended_to is None:
                cs_id = part.change_set_ids[d.side]
                await self._abandon(p, part, d.side)
                if p.abandoned_change_sets and not p.abandoned_change_sets[-1].discarded:
                    left.append(str(cs_id))
                continue
            try:
                await self._om.remove_operations(d.appended_to, d.op_ids)
            except (OMError, ValueError):
                left.append(d.appended_to)
        return left

    async def _save_or_undo(
        self,
        p: Proposal,
        part: ProposalPart,
        done: Sequence[_Done],
        chunk: Sequence[ProposalOperation],
        *,
        new_part: bool,
    ) -> None:
        """Store ``p`` after OM accepted a part's writes. If the store write fails (or the call
        is cancelled), the OM writes are undone so OM holds nothing the store does not know."""
        try:
            await self._save(p)
        except BaseException:
            left = await asyncio.shield(self._undo(p, part, done))
            if new_part:
                p.parts.remove(part)
            else:
                del part.operations[len(part.operations) - len(chunk) :]
            ids = [str(a.id) for a in p.abandoned_change_sets]
            _log.error(
                "proposal %s: store write failed after OM writes; undone (abandoned change "
                "sets %s; not reverted %s)",
                p.id,
                ids,
                left,
            )
            raise

    async def _drop_ghost(self, p: Proposal) -> None:
        """A proposal whose creation failed before any part was stored: mark it discarded when
        it is the only record of a discarded change set, else (or if that fails) delete the
        row; the abandoned change set ids are logged if even that fails."""
        try:
            if p.abandoned_change_sets and p.status != "discarded":
                p.status = "discarded"
                p.warnings.append("no part could be created; see the abandoned change sets")
                try:
                    await self._save(p)
                    return
                except Exception:
                    p.status = "draft"  # fall through: no ghost draft row may stay
            if p.status != "discarded":
                await self._repo.delete(p.id)
        except Exception as exc:
            _log.error(
                "proposal %s: could not clean up after a failed creation (%s); abandoned "
                "change sets %s",
                p.id,
                type(exc).__name__,
                [a.id for a in p.abandoned_change_sets],
            )

    # -- propose_from_scan -------------------------------------------------------------------

    async def propose_from_scan(
        self,
        scan_id: str,
        model: str,
        *,
        principal: Caller,
        client: str | None,
        tables: list[str] | None = None,
    ) -> Proposal:
        async def run(trail: AuditContext) -> tuple[Proposal, str | None]:
            trail.model = model
            self._require_propose(model, principal)
            # dedupe reads the open proposals of the model: two concurrent scans of one model
            # must not both propose the same concept (single replica; see module docstring)
            async with self._lock(f"model:{model}"):
                return await self._from_scan(trail, scan_id, model, principal, client, tables)

        return await self._audited("propose_from_scan", principal, client, run)

    @staticmethod
    def _select(rec: ScanRecord, tables: list[str] | None) -> ScanRecord:
        if not tables:
            return rec
        wanted = set(tables)
        chosen = [t for t in rec.tables if t.ref.name in wanted or t.ref.full_name in wanted]
        known = {t.ref.name for t in chosen} | {t.ref.full_name for t in chosen}
        missing = sorted(wanted - known)
        if missing:
            raise _fail(
                f"tables not in scan {rec.id}: {', '.join(code_span(m) for m in missing)}",
                "pass table names or full names from the scan record",
            )
        return rec.model_copy(update={"tables": chosen})

    async def _from_scan(
        self,
        trail: AuditContext,
        scan_id: str,
        model: str,
        caller: Caller,
        client: str | None,
        tables: list[str] | None,
    ) -> tuple[Proposal, str | None]:
        rec = self._select(await self._scans.get(scan_id), tables)
        target = await self._target(model)
        mapped = map_scan(rec, policy_for(self._policies, model))
        assets = {t.ref.full_name: (t.om_fqn, None) for t in rec.tables if t.om_fqn}
        result = await dedupe(
            mapped.operations,
            model=model,
            models=self._models,
            om=self._om,
            pending=self._repo.pending_op_keys,
            asset_by_table=assets,
        )
        warnings = [*mapped.warnings, *result.warnings]
        matches = [
            ProposalMatch(
                op_key=m.op_key,
                candidate_fqn=m.candidate_fqn,
                confidence=m.confidence,
                matched_on=m.matched_on,
                action=m.action,
            )
            for m in result.matches
        ]
        ops = result.operations
        if not ops:
            raise NothingToPropose(matches, sorted(set(warnings)))
        self._require_pair(target, ops)
        names = [t.ref.full_name for t in rec.tables]
        groups, split_warnings = split_parts(ops, names, self.max_ops)
        ctx = await self._context(target, ops)
        planned = [(tbls, chunk, to_om_all(chunk, ctx)) for tbls, chunk in groups]  # fail closed
        warnings += self._relation_warnings(ops, ctx)
        used = {t for tbls, _, _ in planned for t in tbls}
        now = _now()
        provenance = Provenance(
            scan_id=rec.id,
            tables=[
                TableProvenance(fqn=t.ref.full_name, om_fqn=t.om_fqn, version=t.version.version)
                for t in sorted(rec.tables, key=lambda x: x.ref.full_name)
                if t.ref.full_name in used
            ],
            rule_ids=_rule_ids(ops),
            principal_subject=caller.subject,
            principal_type=str(caller.type),
            client=client,
            at=now,
        )
        p = Proposal(
            id=new_ulid_id("prop_"),
            model=model,
            status="draft",
            scan_id=rec.id,
            provenance=provenance,
            matches=matches,
            warnings=sorted(set(warnings)) + split_warnings,
            created_at=now,
            updated_at=now,
        )
        trail.proposal_id = p.id
        await self._repo.add(p)
        try:
            for n, (tbls, chunk, om) in enumerate(planned, 1):
                part = ProposalPart(n=n, tables=tbls, table=tbls[0] if len(tbls) == 1 else None)
                try:
                    done = await self._write_part(p, part, chunk, om, target)
                except LakeloomError as exc:
                    if not p.parts:
                        raise
                    return await self._partial(p, n, len(planned), len(chunk), exc)
                p.parts.append(part)
                await self._save_or_undo(p, part, done, chunk, new_part=True)
                ids = p.change_set_ids()
                trail.change_set_id = ids[-1] if ids else trail.change_set_id
        except BaseException:
            if not p.parts:
                await asyncio.shield(self._drop_ghost(p))
            raise
        return p, None

    async def _partial(
        self, p: Proposal, n: int, total: int, count: int, exc: LakeloomError
    ) -> tuple[Proposal, str | None]:
        """Part ``n`` (> 1) failed: keep the stored parts and say so."""
        p.warnings.append(
            f"part {n} of {total} could not be created ({exc.code}: {exc.message}); its "
            f"{count} operations and those of later parts are not in this proposal; run "
            "propose_from_scan again to propose them"
        )
        await self._save(p)
        return p, exc.code

    # -- propose_change ----------------------------------------------------------------------

    async def propose_change(
        self,
        proposal_id: str,
        operations: Sequence[ProposalOperation],
        *,
        principal: Caller,
        client: str | None,
    ) -> Proposal:
        # Provenance of agent edits is the server's to state (FR-PRO-03): rule ids are replaced.
        stamped = [op.model_copy(update={"rule_ids": [AGENT_RULE]}) for op in operations]

        async def run(trail: AuditContext) -> tuple[Proposal, str | None]:
            trail.proposal_id = proposal_id
            async with self._lock(proposal_id):
                p = await self._repo.get(proposal_id)
                trail.model = p.model
                self._require_owner(p, principal)
                self._require_propose(p.model, principal)
                self._require_draft(p, "changed", reopen=True)
                ops: list[ProposalOperation] = []
                realized: list[str] = []
                for op in stamped:
                    out = await self._agent_realization(p.model, op)
                    if isinstance(out, str):
                        realized.append(out)
                    else:
                        ops.append(out)
                if not ops:
                    raise NothingToPropose([], realized)
                await self._reopen(trail, p)  # changes requested: rebuild first, then append
                await self._append(trail, p, ops, screened=True, dropped=realized)
                return p, None

        return await self._audited("propose_change", principal, client, run)

    async def _agent_realization(
        self, model: str, op: ProposalOperation
    ) -> ProposalOperation | str:
        """An ``add_realization`` from propose_change resolved like propose_realization's
        (a string: why it was dropped)."""
        if not isinstance(op, AddRealization):
            return op
        resolved = await self._realization(model, op.concept, op.asset_fqn, op.column, strict=False)
        if isinstance(resolved, str):
            return resolved
        return resolved.model_copy(update={"evidence": [*resolved.evidence, *op.evidence][:20]})

    @staticmethod
    def _require_owner(p: Proposal, caller: Caller) -> None:
        """Owner ruling (ADR 0011): the creator or a ``lakeloom:admin`` caller."""
        if caller.subject == p.provenance.principal_subject:
            return
        scopes: object = getattr(caller, "scopes", frozenset[str]())
        if isinstance(scopes, frozenset | set) and ADMIN_SCOPE in scopes:
            return
        raise LakeloomError(
            "forbidden",
            NOT_OWNER,
            "only its creator or a lakeloom:admin principal can change, submit or discard "
            "it; start your own proposal (propose_from_scan or propose_realization)",
        )

    @staticmethod
    def _require_draft(p: Proposal, action: str, *, reopen: bool = False) -> None:
        """Only a draft (``reopen``: or a ``changes_requested`` proposal, rebuilt first)."""
        if p.status == "draft" or (reopen and p.status == "changes_requested"):
            return
        allowed = "draft or changes_requested" if reopen else "draft"
        raise _fail(
            f"proposal {p.id} is {p.status}; only {allowed} proposals can be {action}",
            "start a new proposal (propose_from_scan or propose_realization)",
        )

    def _require_propose(self, model: str, principal: Caller) -> None:
        """NFR-SEC-07: the model's ``propose_groups`` (an Actor is exempt)."""
        require_group(principal, access_for(self._policies, model), "propose", model=model)

    async def _append(
        self,
        trail: AuditContext,
        p: Proposal,
        operations: list[ProposalOperation],
        *,
        screened: bool = False,
        dropped: Sequence[str] = (),
    ) -> None:
        """Append ``operations``; ``screened`` also applies FR-PRO-02 (:func:`screen`): ops
        pending in another proposal or naming an existing concept/attribute are dropped."""
        self._require_draft(p, "changed")
        existing = p.operations()
        have = {op_key(op) for op in existing}
        new: list[ProposalOperation] = []
        skipped: list[str] = []
        for op in operations:
            key = op_key(op)
            if key in have:
                skipped.append(key)
                continue
            have.add(key)
            new.append(op)
        if not new:
            raise _fail(
                "nothing to add: every operation is already in this proposal",
                "; ".join(code_span(k) for k in skipped[:_MAX_HINT_ITEMS]) or "pass operations",
            )
        dropped = list(dropped)
        found: list[ProposalMatch] = []
        if screened:
            result = await screen(
                new,
                model=p.model,
                models=self._models,
                pending=await self._repo.pending_op_keys(p.model),
                own=p.id,
            )
            new = result.operations
            dropped += result.warnings
            found = [
                ProposalMatch(
                    op_key=m.op_key,
                    candidate_fqn=m.candidate_fqn,
                    confidence=m.confidence,
                    matched_on=m.matched_on,
                    action=m.action,
                )
                for m in result.matches
            ]
            if not new:
                raise NothingToPropose(
                    found, [*dropped, *(f"already in this proposal: {k}" for k in skipped)]
                )
        updated = {n for op in existing if (n := _updated_term(op)) is not None}
        clash = sorted({n for op in new if (n := _updated_term(op)) in updated})
        if clash:
            raise _fail(
                f"concepts already changed in this proposal: {', '.join(clash)}",
                "one proposal changes an existing concept once; discard it and propose the "
                "combined change",
            )
        drafts = {op.name for op in existing if isinstance(op, AddConcept)}
        for op in new:
            name = _updated_term(op)
            if name is not None and name in drafts:
                what = "synonym" if isinstance(op, AddSynonym) else "description/update"
                raise _fail(
                    f"{name} is created by this proposal",
                    f"include the {what} when proposing it, or discard and re-propose",
                )
        target = await self._target(p.model)
        self._require_pair(target, new)
        ctx = await self._context(target, [*existing, *new])
        chunks = self._chunks(p, new)
        planned = [
            (
                index,
                chunk,
                to_om_all(
                    chunk,
                    ctx,
                    # concepts created earlier in the change set this chunk is appended to
                    in_change_set=()
                    if index is None
                    else {
                        op.name for op in p.parts[index].operations if isinstance(op, AddConcept)
                    },
                    # existing concepts whose class an earlier native op there declared
                    declared_classes=()
                    if index is None
                    else {
                        name
                        for op in p.parts[index].operations
                        for ref in _native_refs(op)
                        if (name := _local_ref(ref, p.model)) is not None
                    },
                ),
            )
            for index, chunk in chunks
        ]
        await self._validate_axioms(target, [*existing, *new], new, ctx)
        p.warnings.extend(f"already in this proposal: {k}" for k in skipped)
        p.warnings.extend(w for w in dropped if w not in p.warnings)
        p.matches.extend(m for m in found if m not in p.matches)
        p.warnings.extend(w for w in self._relation_warnings(new, ctx) if w not in p.warnings)
        for index, chunk, om in planned:
            noted = (len(p.abandoned_change_sets), len(p.warnings))
            part = ProposalPart(n=len(p.parts) + 1) if index is None else p.parts[index]
            try:
                done = await self._write_part(p, part, chunk, om, target)
            except LakeloomError:
                if (len(p.abandoned_change_sets), len(p.warnings)) != noted:
                    await self._save(p)  # keep the record of a discarded change set
                raise
            if index is None:
                p.parts.append(part)
            await self._save_or_undo(p, part, done, chunk, new_part=index is None)
            ids = p.change_set_ids()
            trail.change_set_id = ids[-1] if ids else None

    def _chunks(
        self, p: Proposal, new: list[ProposalOperation]
    ) -> list[tuple[int | None, list[ProposalOperation]]]:
        """``new`` split into (index of the existing last part | None for a new part, ops)."""
        out: list[tuple[int | None, list[ProposalOperation]]] = []
        rest = list(new)
        if p.parts:
            room = self.max_ops - len(p.parts[-1].operations)
            if room > 0:
                out.append((len(p.parts) - 1, rest[:room]))
                rest = rest[room:]
        while rest:
            out.append((None, rest[: self.max_ops]))
            rest = rest[self.max_ops :]
        return out

    async def _validate_axioms(
        self,
        target: _Target,
        everything: Sequence[ProposalOperation],
        new: Sequence[ProposalOperation],
        ctx: OMContext,
    ) -> None:
        axioms = [op for op in new if isinstance(op, AddAxiom)]
        if not axioms or target.ontology is None:
            return
        native = to_om_all(
            [op for op in everything if isinstance(op, AddConcept | AddAttribute | AddAxiom)], ctx
        ).native
        declared = {iri for item in native if (iri := _declared_iri(item)) is not None}
        for op in axioms:
            item = next(
                o for o in to_om_all([op], ctx).native if o["operationType"] == "UPSERT_AXIOM"
            )
            body = dict(item["axiom"])
            for key in ("id", "fullyQualifiedName"):
                body.pop(key, None)
            name = str(body["name"])
            body["ontology"] = target.ontology.name
            body.setdefault("displayName", name)
            body.setdefault("description", name)
            report = await _om(f"validating axiom {name!r}", self._om.validate_axiom(body))
            subject, props = str(body.get("subjectIri")), _property_iris(body)
            errors = [
                v.message
                for v in report.violations
                if v.severity == "ERROR"
                and not (v.code == "UNDECLARED_CLASS" and subject in declared)
                and not (v.code == "UNDECLARED_PROPERTY" and props and props <= declared)
            ]
            if errors:
                raise _fail(
                    f"axiom {name!r} is invalid",
                    "OpenMetadata reported: " + "; ".join(code_span(e) for e in errors[:10]),
                )

    # -- propose_realization -----------------------------------------------------------------

    async def propose_realization(
        self,
        proposal_id: str | None,
        model: str | None,
        concept: str,
        asset: str,
        column: str | None = None,
        *,
        principal: Caller,
        client: str | None,
    ) -> Proposal:
        async def run(trail: AuditContext) -> tuple[Proposal, str | None]:
            trail.proposal_id = proposal_id
            trail.model = model
            if proposal_id is not None:
                async with self._lock(proposal_id):
                    p = await self._repo.get(proposal_id)
                    if model is not None and model != p.model:
                        raise _fail(
                            f"proposal {p.id} is for model {p.model!r}, not {model!r}",
                            "omit model or pass the proposal's model",
                        )
                    trail.model = p.model
                    self._require_owner(p, principal)
                    self._require_propose(p.model, principal)
                    self._require_draft(p, "changed")
                    op = cast(
                        "AddRealization", await self._realization(p.model, concept, asset, column)
                    )
                    await self._append(trail, p, [op])
                    return p, None
            if model is None:
                raise _fail("pass a model or a proposal_id", "name the model to propose into")
            self._require_propose(model, principal)
            await self._models.get_model(model)
            op = cast("AddRealization", await self._realization(model, concept, asset, column))
            p = await self._new_realization_proposal(model, op, principal, client)
            trail.proposal_id = p.id
            return p, None

        return await self._audited("propose_realization", principal, client, run)

    async def _realization(
        self, model: str, concept: str, asset: str, column: str | None, *, strict: bool = True
    ) -> AddRealization | str:
        """A realization of a concept of ``model`` or a model it imports (plain resolve, no
        assets read). When it is pending in an open proposal or the asset already realizes
        the concept: ``validation_error`` (``strict``), else the reason as a warning (the
        pending check is then left to :func:`screen`)."""
        within = [model, *await import_slugs(self._models, model)]
        ref = await resolve_concept(self._models, self._om, concept, within=within)
        resolved = await resolve_asset(asset, sources=self._sources, om=self._om)
        if column is not None and resolved.column is not None and column != resolved.column:
            raise _fail(
                f"asset {asset!r} names column {resolved.column!r}, not {column!r}",
                "pass the column either in the asset or as column",
            )
        if column is not None and resolved.column is None:
            resolved = await resolve_asset(
                f"{resolved.table_fqn}.{column}", sources=self._sources, om=self._om
            )
        op = AddRealization(
            op="add_realization",
            concept=ref.fqn,
            asset_fqn=resolved.table_fqn,
            column=resolved.column,
            rule_ids=[AGENT_RULE],
            evidence=[Evidence(kind="agent", ref=resolved.fqn)],
        )
        if strict:
            pending = await self._repo.pending_op_keys(model)
            key = op_key(op)
            if key in pending:
                raise _fail(f"already proposed in {pending[key]}: {key}", "review that proposal")
        table = await _om(
            f"reading table {resolved.table_fqn}", self._om.get_table_by_fqn(resolved.table_fqn)
        )
        if table is not None:
            if resolved.column is None:
                tags = table.tags
            else:
                col = next((c for c in table.columns if c.name == resolved.column), None)
                tags = col.tags if col else []
            if ref.fqn in direct_glossary_tags(tags, column=resolved.column is not None):
                why = f"{resolved.fqn} already realizes {ref.fqn}"
                if not strict:
                    return f"{why}; dropped: {op_key(op)}"
                raise _fail(why, "nothing to propose; call get_asset_context to see its concepts")
        return op

    async def _new_realization_proposal(
        self, model: str, op: AddRealization, caller: Caller, client: str | None
    ) -> Proposal:
        now = _now()
        p = Proposal(
            id=new_ulid_id("prop_"),
            model=model,
            status="draft",
            parts=[ProposalPart(n=1, operations=[op])],
            provenance=Provenance(
                scan_id=None,
                tables=[TableProvenance(fqn=op.asset_fqn, om_fqn=op.asset_fqn)],
                rule_ids=list(op.rule_ids),
                principal_subject=caller.subject,
                principal_type=str(caller.type),
                client=client,
                at=now,
            ),
            created_at=now,
            updated_at=now,
        )
        await self._repo.add(p)
        return p

    # -- read --------------------------------------------------------------------------------

    async def get_proposal(self, proposal_id: str) -> Proposal:
        """The proposal with each change set's state refreshed from OM (``MISSING`` when OM
        no longer has it). A read: the refreshed state is returned, never stored (writes keep
        the stored states current, and a read must not move ``updated_at``)."""
        p = await self._repo.get(proposal_id)
        states = await self._om_states(p)
        for part in p.parts:
            for side in SIDES:
                cs = part.change_set_ids.get(side)
                if cs is not None:
                    part.om_state[side] = states[cs][1]
        return p

    async def _om_states(self, p: Proposal) -> dict[str, tuple[float | None, str]]:
        """Change set id -> (OM version, OM state), read with bounded concurrency."""
        sem = asyncio.Semaphore(_OM_READS)

        async def one(cs_id: str) -> tuple[str, tuple[float | None, str]]:
            async with sem:
                cs = await _om(f"reading change set {cs_id}", self._om.get_change_set(cs_id))
            return cs_id, (None, "MISSING") if cs is None else (cs.version, str(cs.state))

        return dict(await asyncio.gather(*(one(c) for c in p.change_set_ids())))

    def summary_markdown(self, p: Proposal) -> str:
        return summary_markdown(p)

    def diff(self, p: Proposal) -> ProposalDiff:
        return diff(p)

    # -- submit / discard ----------------------------------------------------------------------

    async def submit(self, proposal_id: str, *, principal: Caller, client: str | None) -> Proposal:
        async def run(trail: AuditContext) -> tuple[Proposal, str | None]:
            trail.proposal_id = proposal_id
            async with self._lock(proposal_id):
                p = await self._repo.get(proposal_id)
                trail.model = p.model
                self._require_owner(p, principal)
                self._require_draft(p, "submitted", reopen=True)
                await self._reopen(trail, p)  # changes requested: rebuild first, then submit
                await self._transition(trail, p, "SUBMITTED", self._om.submit_change_set)
                p.status = "submitted"
                await self._save(p)
                return p, None

        return await self._audited("submit_proposal", principal, client, run)

    async def _transition(
        self,
        trail: AuditContext,
        p: Proposal,
        state: str,
        command: Callable[[str], Awaitable[Any]],
    ) -> None:
        """Move every change set to ``state``; sides already there are skipped, so a retry
        after a partial failure continues. Progress is stored before an error is raised."""
        for part in p.parts:
            for side in SIDES:
                cs_id = part.change_set_ids.get(side)
                if cs_id is None or part.om_state.get(side) in (state, "APPLIED"):
                    continue
                if state == "DISCARDED" and part.om_state.get(side) == "MISSING":
                    continue  # gone from OM: nothing left to discard
                trail.change_set_id = cs_id
                try:
                    cs = await _om(f"moving change set {cs_id} to {state}", command(cs_id))
                except LakeloomError as exc:
                    if state == "DISCARDED" and exc.code == "not_found":
                        part.om_state[side] = "MISSING"  # deleted meanwhile: already gone
                        continue
                    await self._save(p)
                    raise
                part.om_state[side] = str(cs.state)

    async def _digest(self, p: Proposal) -> tuple[str, dict[str, tuple[float | None, str]]]:
        states = await self._om_states(p)
        data = {
            "id": p.id,
            "status": p.status,
            "updated_at": p.updated_at.isoformat(),
            "change_sets": [[cs, *states[cs]] for cs in p.change_set_ids()],
        }
        return hashlib.sha256(_json_block(data).encode("utf-8")).hexdigest(), states

    async def discard(
        self,
        proposal_id: str,
        *,
        principal: Caller,
        client: str | None,
        confirm_token: str | None = None,
    ) -> Proposal | ConfirmPreview:
        """Destructive (FR-AGT-03): without a token, a preview with a confirm token bound to
        the proposal's current state (status, update time, every change set's OM version and
        state); with it, every change set is discarded and the proposal becomes ``discarded``.
        A replayed token fails as ``conflict`` (the state it was bound to is gone)."""

        async def run(trail: AuditContext) -> tuple[Proposal | ConfirmPreview, str | None]:
            trail.proposal_id = proposal_id
            args = {"proposal_id": proposal_id, "principal": principal.subject}
            async with self._lock(proposal_id):
                p = await self._repo.get(proposal_id)
                trail.model = p.model
                self._require_owner(p, principal)
                digest, states = await self._digest(p)
                if confirm_token is None:
                    self._require_open(p)
                    token, expires = self._confirm.issue(DISCARD_TOOL, args, digest)
                    count = len(p.change_set_ids())
                    summary = (
                        f"Discarding proposal {code_span(p.id)} discards its {count} "
                        "OpenMetadata change set(s); this cannot be undone. Call again with "
                        "confirm_token to proceed.\n\n" + summary_markdown(p)
                    )
                    return ConfirmPreview(
                        summary=summary, confirm_token=token, expires_at=expires
                    ), None
                self._confirm.verify(confirm_token, DISCARD_TOOL, args, digest)
                self._require_open(p)
                for part in p.parts:  # act on what OM holds now (MISSING: already gone)
                    for side in SIDES:
                        cs = part.change_set_ids.get(side)
                        if cs is not None:
                            part.om_state[side] = states[cs][1]
                await self._transition(trail, p, "DISCARDED", self._om.discard_change_set)
                p.status = "discarded"
                await self._save(p)
                return p, None

        return await self._audited(DISCARD_TOOL, principal, client, run)

    @staticmethod
    def _require_open(p: Proposal) -> None:
        if p.status not in OPEN_STATUSES:
            raise _fail(
                f"proposal {p.id} is {p.status}; only draft or submitted proposals can be "
                "discarded",
                "nothing to do",
            )

    # -- rebuild / rebase (ADR 0012 § Rebuild) -------------------------------------------------

    async def rebase(self, proposal_id: str, *, principal: Caller, client: str | None) -> Proposal:
        """FR-OM-03: rebuild every part that is not fully applied against OM's current state
        (fresh term ids and versions, the current native working revision), with the bot
        client. Agent-supplied base versions are refreshed with a warning; a referenced entity
        OM no longer has is a ``conflict`` and leaves the proposal unchanged. The proposal
        becomes a ``draft`` (submit it again for a new review)."""

        async def run(trail: AuditContext) -> tuple[Proposal, str | None]:
            trail.proposal_id = proposal_id
            async with self._lock(proposal_id):
                p = await self._repo.get(proposal_id)
                trail.model = p.model
                self._require_owner(p, principal)
                self._require_propose(p.model, principal)
                if p.status not in REBASE_STATUSES:
                    raise _fail(
                        f"proposal {p.id} is {p.status}; only {', '.join(REBASE_STATUSES)} "
                        "proposals can be rebased",
                        "start a new proposal (propose_from_scan or propose_realization)",
                    )
                await self._rebuild_open(trail, p, every=True)
                return p, None

        return await self._audited(REBASE_TOOL, principal, client, run)

    async def _reopen(self, trail: AuditContext, p: Proposal) -> None:
        """Before ``propose_change`` / ``submit``: a ``changes_requested`` proposal is rebuilt
        (drafts, not submitted) and becomes a draft; a draft whose rebase stopped part-way
        (``rebase_pending``) is rebased again (every part not fully applied); a draft whose
        rebuild was interrupted (a side discarded, not yet replaced) finishes it."""
        if p.status == "changes_requested" or (p.status == "draft" and p.rebase_pending):
            await self._rebuild_open(trail, p, every=True)
        elif p.status == "draft" and any(_interrupted(part) for part in p.parts):
            await self._rebuild_open(trail, p, every=False)

    async def _rebuild_open(self, trail: AuditContext, p: Proposal, *, every: bool) -> None:
        """Rebuild the parts that are not fully applied (``every``) or whose rebuild was
        interrupted, after refreshing base versions; the proposal becomes a draft (stored)."""
        states = await self._om_states(p)
        for part in p.parts:
            for side in SIDES:
                cs = part.change_set_ids.get(side)
                if cs is not None:
                    part.om_state[side] = states[cs][1]
        parts = [
            part
            for part in p.parts
            if ((not _fully_applied(part)) if every else _interrupted(part))
        ]
        refreshed: Refreshed = {}
        if parts:
            refreshed = await self._refreshed_bases(p, parts, await self._target(p.model))
        p.status = "draft"
        # stored with the first progress: a stop part-way leaves later parts untouched in OM
        # (old operations, old change sets), and the next call rebases them
        p.rebase_pending = every
        await self.rebuild_parts(
            p, parts, om=self._om, submit=False, trail=trail, refreshed=refreshed
        )
        p.rebase_pending = False
        await self._save(p)

    async def _refreshed_bases(
        self, p: Proposal, parts: Sequence[ProposalPart], target: _Target
    ) -> Refreshed:
        """Each part's operations with every agent-supplied ``base_version`` (UpdateConcept,
        RemoveConcept) set to the term's current OM version, plus a warning per changed
        entity. Nothing is modified here (:meth:`rebuild_parts` assigns them to a part just
        before rebuilding it); a term OM no longer has is a ``conflict``."""
        slug = p.model
        checked = [
            op
            for part in parts
            if part.om_state.get("glossary") != "APPLIED"
            for op in part.operations
            if isinstance(op, UpdateConcept | RemoveConcept)
        ]
        if not checked:
            return {}
        by_name = await self._terms_by_name(target.info)
        current: dict[str, float] = {}
        gone: list[str] = []
        for op in checked:
            name = _local_ref(op.fqn, slug)
            if name is None:
                continue  # another model's term: to_om_all refuses it
            found = by_name.get(name, [])
            if not found:
                gone.append(f"{slug}.{name}")
            elif len(found) == 1 and found[0].version is not None:
                current[name] = found[0].version
        if gone:
            raise LakeloomError(
                "conflict",
                "OpenMetadata no longer has "
                + ", ".join(code_span(g) for g in sorted(set(gone)))
                + f"; proposal {p.id} was not rebased",
                "remove the operations on it (discard the proposal and propose again without "
                "them), or check the name with find_concepts",
            )
        out: dict[int, tuple[list[ProposalOperation], list[str]]] = {}
        for part in parts:
            if part.om_state.get("glossary") == "APPLIED":
                continue
            ops: list[ProposalOperation] = []
            warnings: list[str] = []
            for op in part.operations:
                name = (
                    _local_ref(op.fqn, slug)
                    if isinstance(op, UpdateConcept | RemoveConcept)
                    else None
                )
                if (
                    isinstance(op, UpdateConcept | RemoveConcept)
                    and name in current
                    and float(op.base_version) != current[name]
                ):
                    new = _version_text(current[name])
                    warnings.append(_refresh_warning(f"{slug}.{name}", op.base_version, new))
                    op = op.model_copy(update={"base_version": new})
                ops.append(op)
            out[part.n] = (ops, warnings)
        return out

    async def rebuild_parts(
        self,
        p: Proposal,
        parts: Sequence[ProposalPart],
        *,
        om: OMClient,
        submit: bool,
        trail: AuditContext,
        refreshed: Refreshed | None = None,
    ) -> None:
        """Re-draft ``parts`` of ``p`` against OM's current state; the caller holds the
        proposal's lock. Every OM call goes through ``om`` (the bot for rebase, the
        reviewer's client in review). All payloads are built first (fail closed, nothing
        written). Then, per part: its sides that are not APPLIED are discarded (a 404 or an
        already discarded set: gone), recreated as ``<original name>-r<k>`` (``k`` = the
        part's rebuild count) with the old ids moved to ``superseded_change_set_ids``, and
        submitted when ``submit``. An APPLIED side is never rebuilt. Progress is stored after
        each part and before an error propagates (a side discarded but not yet replaced keeps
        its old id as ``DISCARDED``, and the next rebuild continues).

        ``refreshed`` (part number -> operations, warnings; from rebase) replaces a part's
        operations, and adds its warnings, only when that part is rebuilt, so the store never
        holds operations a change set in OM does not. A part whose glossary set is APPLIED
        gets its native side only; terms its applied ``RemoveConcept`` deleted are not looked
        up (the native ``DELETE_CONCEPT`` addresses the class, which still exists)."""
        if not parts:
            return
        refreshed = refreshed or {}

        def ops_of(part: ProposalPart) -> list[ProposalOperation]:
            return refreshed[part.n][0] if part.n in refreshed else part.operations

        target = await self._target(p.model, om=om)
        open_parts = [part for part in p.parts if not _fully_applied(part)]
        ops = [op for part in open_parts for op in ops_of(part)]
        self._require_pair(target, ops)
        applied = [part for part in p.parts if part.om_state.get("glossary") == "APPLIED"]
        existing = {
            op.name for part in applied for op in part.operations if isinstance(op, AddConcept)
        }
        deleted = {
            name
            for part in applied
            for op in part.operations
            if isinstance(op, RemoveConcept) and (name := _local_ref(op.fqn, p.model)) is not None
        }
        ctx = await self._context(target, ops, existing=existing, deleted=deleted, om=om)
        for part in parts:
            self._require_native_members(p, part, ctx, ops_of(part))
        planned = [(part, _plan(part, ops_of(part), ctx)) for part in parts]  # fail closed
        for part, om_ops in planned:
            try:
                if part.n in refreshed:
                    part.operations, warnings = refreshed[part.n]
                    p.warnings.extend(w for w in warnings if w not in p.warnings)
                await self._rebuild_part(p, part, om_ops, ctx, target, om=om, trail=trail)
                if submit:
                    await self._submit_part(part, om=om, trail=trail)
            except BaseException:
                await asyncio.shield(self._keep_progress(p))
                raise
            await self._save(p)

    @staticmethod
    def _require_native_members(
        p: Proposal, part: ProposalPart, ctx: OMContext, ops: Sequence[ProposalOperation]
    ) -> None:
        """A native definition a removal names must still exist (``conflict`` otherwise)."""
        if part.om_state.get("native") == "APPLIED" or not ctx.paired:
            return
        glossary_applied = part.om_state.get("glossary") == "APPLIED"
        gone: list[str] = []
        for op in ops:
            if isinstance(op, RemoveAttribute | RemoveAxiom):
                try:
                    to_om(op, ctx)
                except LakeloomError:
                    gone.append(f"{p.model}.{_bare(op.concept)}.{op.name}")
            elif isinstance(op, RemoveConcept) and glossary_applied:
                try:
                    remove_concept_native(op, ctx)
                except LakeloomError:
                    gone.append(f"{p.model}.{_bare(op.fqn)}")
        if gone:
            raise LakeloomError(
                "conflict",
                "OpenMetadata no longer has " + ", ".join(code_span(g) for g in gone),
                "remove the operations on it (discard the proposal and propose again without them)",
            )

    async def _rebuild_part(
        self,
        p: Proposal,
        part: ProposalPart,
        planned: OMOps,
        ctx: OMContext,
        target: _Target,
        *,
        om: OMClient,
        trail: AuditContext,
    ) -> None:
        part.rebuilds += 1
        old_bases: dict[str, float] = {}
        for side in SIDES:
            cs_id = part.change_set_ids.get(side)
            if cs_id is None or part.om_state.get(side) in _GONE:
                continue
            trail.change_set_id = cs_id
            cs = await _om(f"reading change set {cs_id}", om.get_change_set(cs_id))
            state = "MISSING" if cs is None else str(cs.state)
            if cs is not None:
                old_bases.update(
                    (o.target_id, o.base_version)
                    for o in cs.operations
                    if o.target_id is not None and o.base_version is not None
                )
            if state not in (*_GONE, "APPLIED"):
                try:
                    cs = await _om(f"discarding change set {cs_id}", om.discard_change_set(cs_id))
                    state = str(cs.state)
                except LakeloomError as exc:
                    if exc.code != "not_found":
                        raise
                    state = "MISSING"  # deleted meanwhile: already gone
            part.om_state[side] = state
        p.warnings.extend(
            w for w in self._native_base_warnings(p, part, ctx, old_bases) if w not in p.warnings
        )
        sides: list[tuple[Side, list[dict[str, Any]]]] = [
            ("glossary", planned.glossary),
            ("native", planned.native),
        ]
        for side, side_ops in sides:
            if part.om_state.get(side) == "APPLIED":
                continue  # never rebuilt
            old = part.change_set_ids.get(side)
            if part.original_names.get(side) is None:
                part.original_names[side] = part.change_set_names.get(side) or self._cs_name(
                    p, part.n, side
                )
            if side_ops:
                name = f"{part.original_names[side]}-r{part.rebuilds}"
                if len(name) > _MAX_CS_NAME:
                    raise _fail(
                        f"rebuilt change set name is longer than {_MAX_CS_NAME} characters",
                        "discard the proposal and propose again",
                    )
                await self._create_side(
                    p, part, part, side, _with_ids(side_ops), target, om=om, name=name
                )
                trail.change_set_id = part.change_set_ids[side]
            else:
                part.change_set_ids[side] = part.change_set_names[side] = None
                part.om_state[side] = None
            if old is not None:
                part.superseded_change_set_ids.append(old)

    @staticmethod
    def _native_base_warnings(
        p: Proposal, part: ProposalPart, ctx: OMContext, old_bases: Mapping[str, float]
    ) -> list[str]:
        """A removal's native base version (taken from OM, not from the agent) that moved
        since the replaced change set was drafted."""
        if part.om_state.get("native") == "APPLIED":
            return []
        out: list[str] = []
        for op in part.operations:
            if not isinstance(op, RemoveAttribute | RemoveAxiom):
                continue
            item = to_om(op, ctx).native[0]
            before, now = old_bases.get(str(item["targetId"])), float(item["baseVersion"])
            if before is not None and before != now:
                entity = f"{p.model}.{_bare(op.concept)}.{op.name}"
                out.append(_refresh_warning(entity, _version_text(before), _version_text(now)))
        return out

    async def _submit_part(self, part: ProposalPart, *, om: OMClient, trail: AuditContext) -> None:
        for side in SIDES:
            cs_id = part.change_set_ids.get(side)
            if cs_id is None or part.om_state.get(side) != "DRAFT":
                continue
            trail.change_set_id = cs_id
            cs = await _om(f"moving change set {cs_id} to SUBMITTED", om.submit_change_set(cs_id))
            part.om_state[side] = str(cs.state)

    async def _keep_progress(self, p: Proposal) -> None:
        """Store what a failed rebuild reached; if even that fails, log the change sets."""
        try:
            await self._save(p)
        except Exception as exc:
            _log.error(
                "proposal %s: could not store rebuild progress (%s); change sets %s, superseded %s",
                p.id,
                type(exc).__name__,
                p.change_set_ids(),
                [cs for part in p.parts for cs in part.superseded_change_set_ids],
            )


def _fully_applied(part: ProposalPart) -> bool:
    """Every side with a change set is APPLIED (vacuously true for a part without any)."""
    return all(
        part.om_state.get(side) == "APPLIED"
        for side in SIDES
        if part.change_set_ids.get(side) is not None
    )


def _interrupted(part: ProposalPart) -> bool:
    """A rebuild stopped after discarding a side and before replacing it."""
    return any(
        part.change_set_ids.get(side) is not None and part.om_state.get(side) in _GONE
        for side in SIDES
    )


def _plan(part: ProposalPart, ops: Sequence[ProposalOperation], ctx: OMContext) -> OMOps:
    """A part's payload. With its glossary set APPLIED only the native side is rebuilt, and a
    ``RemoveConcept`` there (its term already deleted) contributes its ``DELETE_CONCEPT``."""
    if part.om_state.get("glossary") != "APPLIED":
        return to_om_all(ops, ctx)
    rest = to_om_all([op for op in ops if not isinstance(op, RemoveConcept)], ctx)
    deletes = [remove_concept_native(op, ctx) for op in ops if isinstance(op, RemoveConcept)]
    return OMOps(native=[*rest.native, *deletes])


def _version_text(version: float) -> str:
    """An OM version as ``base_version`` text (``0.6``, ``2``)."""
    return f"{version:.6f}".rstrip("0").rstrip(".")


def _refresh_warning(entity: str, before: str, after: str) -> str:
    return (
        f"base version of {code_span(entity)} refreshed from {before} to {after}; "
        "re-review the change"
    )


def _native_entry(
    ids: dict[str, str], versions: dict[str, float], key: str, member: Mapping[str, Any]
) -> None:
    member_id, version = member.get("id"), member.get("version")
    if isinstance(member_id, str) and isinstance(version, int | float):
        ids[key] = member_id
        versions[key] = float(version)
