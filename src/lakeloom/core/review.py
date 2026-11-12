"""Review: approve, reject or request changes on a proposal as the human reviewer (FR-REV-01..03).

:meth:`ReviewService.review` checks, in this order, and audits every refusal ``forbidden``
(or ``validation_error``) with nothing sent to OM:

1. the principal is human ("agent principals cannot review proposals (FR-REV-01)");
2. it holds ``lakeloom:review``;
3. the model's ``review_groups`` (:func:`~lakeloom.core.model_policy.require_group`);
4. reject and request_changes carry a reason or comment (capped like other untrusted text);
5. the status allows the decision (approve: submitted | apply_failed; reject: submitted |
   changes_requested | apply_failed; request_changes: submitted). With a confirm token a
   status that no longer allows it is a ``conflict`` (the previewed state is gone);
6. an OM client acting as the reviewer (:meth:`OnBehalfOf.om_for`; its errors are final,
   the bot is never a fallback, NFR-SEC-10);
7. OM grants that user the apply operations (:data:`~lakeloom.om.models.APPLY_PERMISSIONS`)
   on the glossary and, when paired, the native ontology.

``request_changes`` touches nothing in OM, so it stops after step 5 (no OM identity is
needed). ``approve`` and ``reject`` are preview -> confirm token -> execute; the token's digest
binds the reviewer, the decision, the proposal's status and revision and every change set's
OM version and state (read as the reviewer).

Every OM call of a review goes through the reviewer's client, reads included; the
:class:`~lakeloom.core.proposals.ProposalService` is used for its lock, its store write and
:meth:`~lakeloom.core.proposals.ProposalService.rebuild_parts`, always with that client.

Approve, under the proposal lock, per part in order: a part whose sides are all APPLIED is
skipped (a retry continues from the first unapplied part); each side's state is read from OM
first. The glossary side is applied as drafted. Before the native side, a mechanically stale
draft (its ``baseWorkingRevision`` behind the ontology's ``workingRevision``, typically
because an earlier part was just applied; or a side a rebuild discarded) is re-drafted from
the same reviewed operations with ``rebuild_parts(submit=True)`` (old ids kept as
superseded). A ``stale_revision`` at apply gets one such rebuild and retry; a second one is
``apply_failed``. A ``stale_base`` (an agent-supplied base version) is never rebuilt here: it
is a ``conflict`` naming the entities, with the hint to rebase and submit again (FR-OM-03).
The OM state is stored after every side. After the parts, realizations are tagged
(``add_asset_tags`` / ``remove_asset_tags``), skipping op keys in
``Proposal.realizations_applied``. Success: status ``applied``, a :class:`ReviewRecord` with
the applied change sets and ``om_user`` = OM's ``applicationResult.appliedBy``, and the model
cache is invalidated. A description an applied ``UpdateConcept`` carried is read back; when OM
holds another one, the record gets a warning (normally silent, ADR 0012). Any failure:
status ``apply_failed`` with the progress and a record of the attempt stored.

Reject discards every change set that is not applied (as the reviewer). Parts already applied
stay in OM; the preview, the record and the summary say so (ADR 0012 § Review).

Audit (NFR-SEC-12): one record per call, tool ``review_proposal``, written by the service
only when it has an ``audit`` repository (CLI); MCP tools pass ``audit=None`` and rely on
``guarded``, which reads the running :func:`~lakeloom.core.audit.current_audit` context.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from lakeloom.config import Settings
from lakeloom.core.audit import AuditContext, AuditRecord, current_audit, record
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.model_policy import ModelPolicyEntry, access_for, require_group
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.obo import OnBehalfOf
from lakeloom.core.operations import (
    MAX_DESCRIPTION_CHARS,
    AddRealization,
    RemoveRealization,
    UpdateConcept,
    op_key,
)
from lakeloom.core.proposals import (
    REBASE_HINT,
    SIDES,
    ConfirmPreview,
    Proposal,
    ProposalPart,
    ProposalService,
    ReviewDecision,
    ReviewRecord,
    Side,
    applied_parts_note,
    diff,
    fully_applied,
    map_om_error,
    summary_markdown,
)
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMConflict, OMError, OMForbidden
from lakeloom.om.models import APPLY_PERMISSIONS, OMChangeSet, OMOntology
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.state.audit import AuditRepository
from lakeloom.state.proposals import ProposalRepository
from lakeloom.untrusted import UntrustedText, code_span

__all__ = ["REVIEW_SCOPE", "REVIEW_TOOL", "ReviewService"]

REVIEW_TOOL: Final = "review_proposal"
REVIEW_SCOPE: Final = "lakeloom:review"
AGENT_REFUSED: Final = "agent principals cannot review proposals (FR-REV-01)"
_ALLOWED: Final[dict[str, tuple[str, ...]]] = {
    "approve": ("submitted", "apply_failed"),
    "reject": ("submitted", "changes_requested", "apply_failed"),
    "request_changes": ("submitted",),
}
_GONE: Final = ("DISCARDED", "MISSING")
_OM_READS: Final = 8
_log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(data: dict[str, Any]) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _typed(exc: OMError, what: str) -> LakeloomError:
    """An OM error of the review path as a typed error (403 -> ``forbidden``)."""
    if isinstance(exc, OMForbidden):
        detail = f": {code_span(exc.detail)}" if exc.detail else ""
        return LakeloomError(
            "forbidden",
            f"OpenMetadata refused {what} for the reviewer{detail}",
            "an OpenMetadata admin must grant the reviewer role (LakeloomOntologyReviewer, "
            "ADR 0012); the proposal keeps its progress, approve again afterwards",
        )
    return map_om_error(exc, what)


async def _om[T](what: str, call: Awaitable[T]) -> T:
    """An OM call of the review path, its errors typed (:func:`_typed`)."""
    try:
        return await call
    except OMError as exc:
        raise _typed(exc, what) from None
    except ValueError as exc:
        raise LakeloomError(
            "validation_error",
            f"internal error while {what} ({type(exc).__name__})",
            "report this to the Lakeloom operators",
        ) from None


@dataclass
class _Run:
    """What one approve run did (for its review record)."""

    applied: list[str] = field(default_factory=list[str])
    om_user: str | None = None
    warnings: list[str] = field(default_factory=list[str])
    changed: bool = False  # anything reached OM (the model cache is stale)


class ReviewService:
    def __init__(
        self,
        proposals: ProposalService,
        repo: ProposalRepository,
        obo: OnBehalfOf,
        models: ModelService,
        confirm: ConfirmTokens,
        settings: Settings,
        audit: AuditRepository | None,
        policies: Sequence[ModelPolicyEntry] = (),
    ) -> None:
        self._proposals = proposals
        self._repo = repo
        self._obo = obo
        self._models = models
        self._confirm = confirm
        self._settings = settings
        self._audit = audit
        self._policies = list(policies)

    # -- entry point -----------------------------------------------------------------------

    async def review(
        self,
        proposal_id: str,
        decision: ReviewDecision,
        *,
        principal: Principal,
        client: str | None,
        reason: str | None = None,
        confirm_token: str | None = None,
    ) -> Proposal | ConfirmPreview:
        """Approve, reject or request changes (module docstring)."""

        async def run(trail: AuditContext) -> Proposal | ConfirmPreview:
            trail.proposal_id = proposal_id
            if principal.type != PrincipalType.HUMAN:
                raise LakeloomError(
                    "forbidden",
                    AGENT_REFUSED,
                    "a signed-in human with the lakeloom:review scope must review it",
                )
            if REVIEW_SCOPE not in principal.scopes:
                raise LakeloomError(
                    "forbidden",
                    f"reviewing proposals needs the {REVIEW_SCOPE} scope",
                    "ask an operator to grant it (a reviewer group mapped to the scope)",
                )
            if decision not in _ALLOWED:
                raise LakeloomError(
                    "validation_error",
                    f"unknown review decision {decision!r}",
                    "pass approve, reject or request_changes",
                )
            async with self._proposals.lock(proposal_id):
                p = await self._repo.get(proposal_id)
                trail.model = p.model
                require_group(
                    principal, access_for(self._policies, p.model), "review", model=p.model
                )
                text = self._reason(decision, reason, principal)
                self._require_status(
                    p,
                    decision,
                    confirmed=confirm_token is not None and decision != "request_changes",
                )
                if decision == "request_changes":
                    return await self._request_changes(p, principal, text)
                om = await self._obo.om_for(principal)
                info, ontology = await self._require_om_permission(p, om, principal)
                args = {
                    "proposal_id": p.id,
                    "decision": decision,
                    "principal": principal.subject,
                    "reason": reason,
                }
                digest = await self._state_digest(p, om, principal, decision)
                if confirm_token is None:
                    token, expires = self._confirm.issue(REVIEW_TOOL, args, digest)
                    return ConfirmPreview(
                        summary=self._preview(p, decision),
                        confirm_token=token,
                        expires_at=expires,
                        diff=diff(p),
                    )
                self._confirm.verify(confirm_token, REVIEW_TOOL, args, digest)
                if decision == "reject":
                    return await self._reject(trail, p, om, principal, text)
                return await self._approve(trail, p, om, info, ontology, principal, text)

        return await self._audited(principal, client, run)

    # -- checks ------------------------------------------------------------------------------

    @staticmethod
    def _reason(
        decision: ReviewDecision, reason: str | None, principal: Principal
    ) -> UntrustedText | None:
        text = (reason or "").strip()
        if not text:
            if decision == "approve":
                return None
            what = "a reason" if decision == "reject" else "a comment"
            raise LakeloomError(
                "validation_error",
                f"{decision} needs {what}",
                f"pass reason with {what} for the proposal's author (FR-REV-03)",
            )
        return UntrustedText(
            text=text[:MAX_DESCRIPTION_CHARS], source=f"reviewer {principal.subject}"
        )

    @staticmethod
    def _require_status(p: Proposal, decision: ReviewDecision, *, confirmed: bool) -> None:
        allowed = _ALLOWED[decision]
        if p.status in allowed:
            return
        if confirmed:
            raise LakeloomError(
                "conflict",
                f"proposal {p.id} is now {p.status}; the previewed state changed",
                "call get_proposal to see what happened; review it again if it is still open",
            )
        raise LakeloomError(
            "validation_error",
            f"proposal {p.id} is {p.status}; {decision} needs a {' or '.join(allowed)} proposal",
            "a draft must be submitted first (submit_proposal); applied, rejected and "
            "discarded proposals are final",
        )

    async def _require_om_permission(
        self, p: Proposal, om: OMClient, principal: Principal
    ) -> tuple[ModelInfo, OMOntology | None]:
        """Step 7: the reviewer's OM user may apply to the glossary (and native ontology)."""
        info = await self._models.get_model(p.model, om=om)
        ontology = await _om(
            f"looking up the native ontology of {p.model!r}", om.get_ontology_by_name(p.model)
        )
        targets: list[tuple[Literal["glossary", "ontology"], str]] = [
            ("glossary", info.glossary_id)
        ]
        if ontology is not None:
            targets.append(("ontology", ontology.id))
        user = principal.username or principal.subject
        for resource, target_id in targets:
            try:
                granted = await _om(
                    f"reading the reviewer's permissions on {p.model!r}",
                    om.get_permissions(resource, target_id),
                )
            except LakeloomError as exc:
                if exc.code != "forbidden":
                    raise
                granted = None
            if granted is None or not granted.allows(*APPLY_PERMISSIONS[resource]):
                raise LakeloomError(
                    "forbidden",
                    f"OpenMetadata does not grant {user} review permission on {p.model}",
                    "an OpenMetadata admin must give this user the LakeloomOntologyReviewer "
                    "role (EditAll on the glossary and its native ontology; ADR 0012)",
                )
        return info, ontology

    # -- confirm state -------------------------------------------------------------------------

    async def _states(self, p: Proposal, om: OMClient) -> dict[str, OMChangeSet | None]:
        """Change set id -> the change set as OM holds it now (``None``: gone), read as the
        reviewer with bounded concurrency."""
        sem = asyncio.Semaphore(_OM_READS)

        async def one(cs_id: str) -> tuple[str, OMChangeSet | None]:
            async with sem:
                return cs_id, await _om(f"reading change set {cs_id}", om.get_change_set(cs_id))

        return dict(await asyncio.gather(*(one(c) for c in p.change_set_ids())))

    async def _state_digest(
        self, p: Proposal, om: OMClient, principal: Principal, decision: ReviewDecision
    ) -> str:
        """Ruling C: reviewer, decision, status, revision and every change set's version and
        state (so a second confirm of the same token, or any change meanwhile, conflicts)."""
        states = await self._states(p, om)
        sets: list[list[Any]] = []
        for cs_id in p.change_set_ids():
            cs = states[cs_id]
            sets.append([cs_id, None, "MISSING"] if cs is None else [cs_id, cs.version, cs.state])
        return _digest(
            {
                "id": p.id,
                "reviewer": principal.subject,
                "decision": decision,
                "status": p.status,
                "revision": p.revision,
                "change_sets": sets,
            }
        )

    @staticmethod
    def _preview(p: Proposal, decision: ReviewDecision) -> str:
        sets = [
            cs
            for part in p.parts
            for side in SIDES
            if (cs := part.change_set_ids.get(side)) is not None
            and part.om_state.get(side) != "APPLIED"
        ]
        note = applied_parts_note(p)
        if decision == "approve":
            head = (
                f"Approving proposal {code_span(p.id)} applies its {len(sets)} change set(s) "
                "that are not applied yet, and its realizations, to OpenMetadata as you."
            )
        else:
            head = (
                f"Rejecting proposal {code_span(p.id)} discards its {len(sets)} change set(s) "
                "that are not applied; this cannot be undone."
            )
        if note is not None:
            head += f" Note: {note}."
        return f"{head} Call again with confirm_token to proceed.\n\n{summary_markdown(p)}"

    # -- request_changes ---------------------------------------------------------------------

    async def _request_changes(
        self, p: Proposal, principal: Principal, text: UntrustedText | None
    ) -> Proposal:
        p.status = "changes_requested"
        p.reviews.append(self._record("request_changes", principal, text))
        await self._proposals.save(p)
        return p

    # -- reject ------------------------------------------------------------------------------

    async def _reject(
        self,
        trail: AuditContext,
        p: Proposal,
        om: OMClient,
        principal: Principal,
        text: UntrustedText | None,
    ) -> Proposal:
        states = await self._states(p, om)
        for part in p.parts:
            for side in SIDES:
                cs_id = part.change_set_ids.get(side)
                if cs_id is None:
                    continue
                cs = states.get(cs_id)
                state = "MISSING" if cs is None else str(cs.state)
                part.om_state[side] = state
                if state in (*_GONE, "APPLIED"):
                    continue
                trail.change_set_id = cs_id
                try:
                    out = await _om(f"discarding change set {cs_id}", om.discard_change_set(cs_id))
                except LakeloomError as exc:
                    if exc.code == "not_found":
                        part.om_state[side] = "MISSING"
                        continue
                    await self._keep(p)
                    raise
                part.om_state[side] = str(out.state)
        note = applied_parts_note(p)
        p.status = "rejected"
        p.reviews.append(
            self._record("reject", principal, text, warnings=[note] if note is not None else [])
        )
        await self._proposals.save(p)
        return p

    # -- approve -----------------------------------------------------------------------------

    async def _approve(
        self,
        trail: AuditContext,
        p: Proposal,
        om: OMClient,
        info: ModelInfo,
        ontology: OMOntology | None,
        principal: Principal,
        text: UntrustedText | None,
    ) -> Proposal:
        run = _Run()
        try:
            for part in p.parts:
                if not fully_applied(part):
                    await self._apply_part(trail, p, part, om, ontology, run)
            await self._realize(p, om, info, run)
        except BaseException as exc:
            p.status = "apply_failed"
            stopped = (
                f"apply stopped: {exc.code}: {exc.message}"
                if isinstance(exc, LakeloomError)
                else f"apply stopped: {type(exc).__name__}"
            )
            p.reviews.append(
                self._record(
                    "approve",
                    principal,
                    text,
                    om_user=run.om_user,
                    applied=run.applied,
                    warnings=[*run.warnings, stopped],
                )
            )
            await asyncio.shield(self._keep(p))
            if run.changed:
                self._models.invalidate(p.model)
            raise
        p.status = "applied"
        p.reviews.append(
            self._record(
                "approve",
                principal,
                text,
                om_user=run.om_user,
                applied=run.applied,
                warnings=run.warnings,
            )
        )
        await self._proposals.save(p)
        self._models.invalidate(p.model)
        return p

    async def _apply_part(
        self,
        trail: AuditContext,
        p: Proposal,
        part: ProposalPart,
        om: OMClient,
        ontology: OMOntology | None,
        run: _Run,
    ) -> None:
        rebuilt = False
        for side in SIDES:
            if part.change_set_ids.get(side) is None:
                continue
            cs = await self._refresh(part, side, om)
            if part.om_state.get(side) == "APPLIED":
                continue
            if not rebuilt and await self._stale(part, side, cs, om, ontology):
                await self._rebuild(trail, p, part, om)
                rebuilt = True
            for attempt in (1, 2):
                try:
                    await self._apply_side(trail, p, part, side, om, run)
                    break
                except OMConflict as exc:
                    if exc.kind == "stale_revision" and attempt == 1 and not rebuilt:
                        await self._rebuild(trail, p, part, om)  # a race since the check
                        rebuilt = True
                        continue
                    raise self._conflict(p, part, side, exc) from None

    async def _refresh(self, part: ProposalPart, side: Side, om: OMClient) -> OMChangeSet | None:
        """Store-side state of ``side`` updated from OM (a set applied by an earlier attempt
        whose store write was lost counts as applied)."""
        cs_id = str(part.change_set_ids[side])
        cs = await _om(f"reading change set {cs_id}", om.get_change_set(cs_id))
        part.om_state[side] = "MISSING" if cs is None else str(cs.state)
        return cs

    async def _stale(
        self,
        part: ProposalPart,
        side: Side,
        cs: OMChangeSet | None,
        om: OMClient,
        ontology: OMOntology | None,
    ) -> bool:
        """Ruling A: a side a rebuild discarded, or a native draft behind the ontology."""
        if part.om_state.get(side) in _GONE:
            return True
        if side != "native" or ontology is None or cs is None:
            return False
        base = cs.base_working_revision
        if base is None:
            return False
        state = await _om(
            f"reading the working revision of {ontology.name!r}",
            om.get_ontology_state(ontology.id),
        )
        return base < state.working_revision

    async def _rebuild(
        self, trail: AuditContext, p: Proposal, part: ProposalPart, om: OMClient
    ) -> None:
        """Re-draft the part's unapplied sides from the same reviewed operations (their
        agent-supplied base versions untouched), submitted, as the reviewer."""
        await self._proposals.rebuild_parts(p, [part], om=om, submit=True, trail=trail)

    async def _apply_side(
        self,
        trail: AuditContext,
        p: Proposal,
        part: ProposalPart,
        side: Side,
        om: OMClient,
        run: _Run,
    ) -> None:
        cs_id = part.change_set_ids.get(side)
        if cs_id is None:  # a rebuild left the side without operations
            return
        trail.change_set_id = cs_id
        if part.om_state.get(side) == "DRAFT":  # a rebuild stopped before submitting it
            cs = await _om(f"submitting change set {cs_id}", om.submit_change_set(cs_id))
            part.om_state[side] = str(cs.state)
        try:
            cs = await om.apply_change_set(cs_id)
        except OMConflict:
            raise
        except OMError as exc:
            raise _typed(exc, f"applying change set {cs_id}") from None
        run.changed = True
        part.om_state[side] = str(cs.state)
        result = cs.application_result
        if result is not None and result.applied_by:
            run.om_user = run.om_user or result.applied_by
        if str(cs.state) != "APPLIED" or (result is not None and result.operations_failed):
            failed = 0 if result is None else result.operations_failed
            await self._proposals.save(p)
            raise LakeloomError(
                "upstream_unavailable",
                f"OpenMetadata did not apply change set {cs_id} (state {cs.state}, "
                f"{failed} operation(s) failed)",
                "call get_proposal to see the change set in OpenMetadata; approve again to "
                "retry, or rebase_proposal to re-draft it",
            )
        run.applied.append(cs_id)
        if side == "glossary":
            run.warnings.extend(await self._description_check(p, part, om))
        await self._proposals.save(p)

    def _conflict(
        self, p: Proposal, part: ProposalPart, side: Side, exc: OMConflict
    ) -> LakeloomError:
        cs_id = part.change_set_ids.get(side)
        ids = ", ".join(code_span(e) for e in exc.entities) or "not named by OpenMetadata"
        if exc.kind == "stale_base":
            return LakeloomError(
                "conflict",
                f"OpenMetadata refused to apply change set {cs_id} of proposal {p.id}: a base "
                f"version is stale (affected: {ids})",
                REBASE_HINT,
            )
        if exc.kind == "stale_revision":
            return LakeloomError(
                "conflict",
                f"OpenMetadata refused to apply change set {cs_id} of proposal {p.id}: the "
                "native ontology changed again after the part was re-drafted",
                "approve again to retry (the part is re-drafted from the reviewed operations)",
            )
        return map_om_error(exc, f"applying change set {cs_id}")

    async def _description_check(self, p: Proposal, part: ProposalPart, om: OMClient) -> list[str]:
        """ADR 0012: rc1 applies UPDATE_TERM descriptions; say so when OM holds another."""
        out: list[str] = []
        for op in part.operations:
            if not isinstance(op, UpdateConcept) or op.description is None:
                continue
            fqn = op.fqn if op.fqn.startswith(f"{p.model}.") else f"{p.model}.{op.fqn}"
            try:
                found = await om.get_term(fqn)
            except (OMError, ValueError):
                out.append(f"could not read {fqn} back to check its description")
                continue
            if found is None or (found.description or "").strip() != op.description.text.strip():
                out.append(
                    f"OpenMetadata holds another description for {fqn} than the approved one; "
                    "check the term in OpenMetadata"
                )
        return out

    async def _realize(self, p: Proposal, om: OMClient, info: ModelInfo, run: _Run) -> None:
        """Ruling E: tag realizations after every part, skipping keys already tagged (the
        caller stores ``realizations_applied``, also when a tag fails)."""
        for part in p.parts:
            for op in part.operations:
                if not isinstance(op, AddRealization | RemoveRealization):
                    continue
                key = op_key(op)
                if key in p.realizations_applied:
                    continue
                term = op.concept if "." in op.concept else f"{info.fqn}.{op.concept}"
                tag = om.add_asset_tags if isinstance(op, AddRealization) else om.remove_asset_tags
                await _om(
                    f"tagging {op.asset_fqn} with {term}",
                    tag("table", op.asset_fqn, column=op.column, term_fqn=term),
                )
                run.changed = True
                p.realizations_applied.append(key)

    # -- records and audit ---------------------------------------------------------------------

    @staticmethod
    def _record(
        decision: ReviewDecision,
        principal: Principal,
        text: UntrustedText | None,
        *,
        om_user: str | None = None,
        applied: Sequence[str] = (),
        warnings: Sequence[str] = (),
    ) -> ReviewRecord:
        return ReviewRecord(
            decision=decision,
            reviewer_subject=principal.subject,
            reviewer_username=principal.username,
            om_user=om_user,
            at=_now(),
            reason=text,
            applied=list(applied),
            warnings=list(warnings),
        )

    async def _keep(self, p: Proposal) -> None:
        """Store progress; if even that fails, log the proposal's change sets."""
        try:
            await self._proposals.save(p)
        except Exception as exc:
            _log.error(
                "proposal %s: could not store review progress (%s); change sets %s",
                p.id,
                type(exc).__name__,
                p.change_set_ids(),
            )

    async def _audited(
        self,
        principal: Principal,
        client: str | None,
        run: Callable[[AuditContext], Awaitable[Proposal | ConfirmPreview]],
    ) -> Proposal | ConfirmPreview:
        trail = current_audit()
        try:
            value = await run(trail)
        except LakeloomError as exc:
            await self._write(principal, client, trail, exc.code)
            raise
        except BaseException as exc:
            code = "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal"
            await asyncio.shield(self._write(principal, client, trail, code))
            raise
        await self._write(principal, client, trail, None)
        return value

    async def _write(
        self, principal: Principal, client: str | None, trail: AuditContext, code: str | None
    ) -> None:
        if self._audit is None:
            return  # MCP: guarded() writes the record
        rec = AuditRecord(
            at=_now(),
            principal_subject=principal.subject,
            principal_type=str(principal.type),
            client=client,
            tool=REVIEW_TOOL,
            model=trail.model,
            proposal_id=trail.proposal_id,
            change_set_id=trail.change_set_id,
            result="ok" if code is None else "forbidden" if code == "forbidden" else "error",
            error_code=code,
        )
        await record(self._audit, rec)
