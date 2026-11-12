"""Review service: approve, reject and request changes as the human reviewer (FR-REV-01..03).

Reuses the fake OM and the kit of ``test_proposals``. Every OM client is a :class:`TokenOM`
over one :class:`ReviewOM`, which records the token of every call, so a test can prove that
no review call went through the bot (FR-REV-02, NFR-SEC-10).
"""

import asyncio
import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from test_proposals import (
    AGENT,
    SECRET,
    FakeOM,
    Kit,
    customers,
    engine_fixture,  # noqa: F401  (the engine fixture)
    term,
    widened,
)

from lakeloom.config import Settings
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.core.obo import OnBehalfOf
from lakeloom.core.operations import AddRelation, UpdateConcept
from lakeloom.core.proposals import (
    REBASE_HINT,
    ConfirmPreview,
    Proposal,
    summary_markdown,
)
from lakeloom.core.review import SELF_REVIEW, SESSION_REJECTED, ReviewService
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMConflict, OMError, OMForbidden, OMUnavailable
from lakeloom.om.models import (
    OMApplicationResult,
    OMChangeSet,
    OMGlossaryTerm,
    OMPermissions,
    OMTable,
)
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.untrusted import UntrustedText

BOT = "bot-token"  # a fake token
REVIEW = "lakeloom:review"


def human(
    subject: str = "alice",
    *,
    scopes: Sequence[str] = (REVIEW,),
    groups: Sequence[str] = (),
    kind: PrincipalType = PrincipalType.HUMAN,
) -> Principal:
    return Principal(
        subject=subject,
        type=kind,
        client_id="agent-client" if kind is PrincipalType.AGENT else None,
        username=subject,
        scopes=frozenset(scopes),
        groups=frozenset(groups),
        token=SecretStr(f"user:{subject}"),
    )


ALICE = human()


class ReviewOM(FakeOM):
    """The proposal fake plus apply, permissions, terms and tags; ``token`` is the token of
    the call running now (set by :class:`TokenOM`)."""

    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None
        self.token_calls: list[tuple[str, str]] = []
        self.users = {BOT: "ingestion-bot", "x:alice": "alice", "x:bob": "bob"}
        self.denied: set[str] = set()  # OM users without EditAll on glossary/ontology
        self.descriptions: dict[str, str] = {}  # term FQN -> description OM holds
        self.tags: list[tuple[str, str | None, str, bool]] = []
        self.keep_descriptions = False  # True: apply leaves term descriptions (rc1 defect)
        self.check_bases = False  # True: apply refuses a stale term baseVersion, as rc1 does
        self.apply_failed: dict[str, str] = {}  # cs id -> rc1's FAILED message (once)
        self.failed_ops: dict[str, int] = {}  # cs id -> failed operations of an APPLIED set

    def _cs(self, cs_id: str) -> OMChangeSet:
        cs = super()._cs(cs_id)
        data = self.change_sets[cs_id]
        terms = {o.get("id"): o.get("term") for o in data["operations"]}
        return cs.model_copy(
            update={
                "base_working_revision": data["kw"].get("base_working_revision"),
                "application_result": data.get("result"),
                "operations": [
                    o.model_copy(update={"term": terms.get(o.id)}) for o in cs.operations
                ],
            }
        )

    def _term_by_id(self, term_id: str) -> tuple[str, int] | None:
        for model, terms in self.terms.items():
            for i, t in enumerate(terms):
                if t.id == term_id:
                    return model, i
        return None

    def _check_bases(self, data: dict[str, Any]) -> None:
        """rc1: an operation whose baseVersion is not the term's version refuses the apply."""
        for op in data["operations"]:
            found = self._term_by_id(str(op.get("targetId")))
            if "baseVersion" not in op or found is None:
                continue
            model, i = found
            if float(op["baseVersion"]) != self.terms[model][i].version:
                raise OMConflict("expected version", 400, entity_ids=(op["id"],), kind="stale_base")

    def _bump_terms(self, data: dict[str, Any]) -> None:
        for op in data["operations"]:
            if op["operationType"] not in ("UPDATE_TERM", "ADD_RELATIONSHIP"):
                continue
            found = self._term_by_id(str(op.get("targetId")))
            if found is not None:
                model, i = found
                t = self.terms[model][i]
                self.terms[model][i] = t.model_copy(
                    update={"version": round((t.version or 0) + 0.1, 6)}
                )

    async def apply_change_set(self, cs_id: str) -> OMChangeSet:
        self.calls.append(("APPLY", cs_id))
        self._maybe_fail(f"apply:{cs_id}")
        data = self.change_sets[cs_id]
        if data["state"] not in ("SUBMITTED", "APPLY_FAILED"):  # rc1 re-runs APPLY_FAILED
            raise OMError("not submitted", 400)
        if self.check_bases:
            self._check_bases(data)
        if cs_id in self.apply_failed:  # rc1: every operation rolled back, the set kept
            data["state"] = "APPLY_FAILED"
            data["version"] += 0.1
            message = self.apply_failed.pop(cs_id)
            data["result"] = OMApplicationResult.model_validate(
                {
                    "appliedBy": self.users.get(self.token or ""),
                    "operationsProcessed": 1,
                    "operationsFailed": 1,
                    "results": [{"operationId": "op-1", "status": "FAILED", "message": message}],
                }
            )
            return self._cs(cs_id)
        if self.check_bases:
            self._bump_terms(data)
        base = data["kw"].get("base_working_revision")
        if base is not None:
            if base < self.revision:
                raise OMConflict("Ontology changed since this draft", 412, kind="stale_revision")
            self.revision += 1
        for op in data["operations"]:
            if op["operationType"] == "CREATE_TERM":  # OM keeps the draft id (ADR 0012)
                model = data["kw"]["glossary_id"]
                self.terms[model].append(term(model, op["term"]["name"], 0.1, id=op["term"]["id"]))
            if op["operationType"] == "UPDATE_TERM" and not self.keep_descriptions:
                fqn = f"{data['kw']['glossary_id']}.{op['term']['name']}"
                self.descriptions[fqn] = op["term"]["description"]
        data["state"] = "APPLIED"
        data["version"] += 0.1
        n = len(data["operations"])
        failed = self.failed_ops.get(cs_id, 0)
        data["result"] = OMApplicationResult.model_validate(
            {
                "appliedBy": self.users.get(self.token or ""),
                "operationsProcessed": n,
                "operationsApplied": n - failed,
                "operationsFailed": failed,
                "results": [
                    {"operationId": f"op-{i}", "status": "FAILED", "message": "rc1 said no"}
                    for i in range(failed)
                ],
            }
        )
        return self._cs(cs_id)

    async def get_permissions(self, resource: str, id: str) -> OMPermissions:
        self.calls.append(("get_permissions", (resource, id)))
        self._maybe_fail("get_permissions")
        user = self.users.get(self.token or "")
        access = "notAllow" if user in self.denied else "allow"
        return OMPermissions.model_validate(
            {"resource": resource, "permissions": [{"operation": "EditAll", "access": access}]}
        )

    async def get_term(self, id_or_fqn: str, fields: object = ()) -> OMGlossaryTerm | None:
        if id_or_fqn not in self.descriptions:
            return None
        model, name = id_or_fqn.split(".", 1)
        return term(model, name, 0.2, description=self.descriptions[id_or_fqn])

    async def _tag(self, fqn: str, column: str | None, term_fqn: str, add: bool) -> bool:
        self.calls.append(("tag" if add else "untag", (fqn, column, term_fqn)))
        self._maybe_fail(f"tag:{fqn}")
        self.tags.append((fqn, column, term_fqn, add))
        return True

    async def add_asset_tags(
        self, entity: str, fqn: str, *, column: str | None, term_fqn: str
    ) -> bool:
        return await self._tag(fqn, column, term_fqn, True)

    async def remove_asset_tags(
        self, entity: str, fqn: str, *, column: str | None, term_fqn: str
    ) -> bool:
        return await self._tag(fqn, column, term_fqn, False)

    def applied(self) -> list[str]:
        return [cs for name, cs in self.calls if name == "APPLY"]


class TokenOM:
    """An OM client acting with ``token``: delegates to the shared fake and logs the token."""

    def __init__(self, inner: ReviewOM, token: str) -> None:
        self.inner = inner
        self.token = token
        self.session_rejected = False  # like OMClient: set by an HTTP 401

    def with_token(self, token: SecretStr) -> "TokenOM":
        return TokenOM(self.inner, token.get_secret_value())

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            self.inner.token_calls.append((self.token, name))
            result = attr(*args, **kwargs)
            if not inspect.iscoroutine(result):
                return result
            awaited = cast("Any", result)

            async def run() -> Any:
                self.inner.token = self.token
                try:
                    return await awaited
                except OMError as exc:
                    if exc.status == 401:
                        self.session_rejected = True
                    raise

            return run()

        return call


class FakeObo:
    """On-behalf-of in exchange mode: the reviewer's client carries ``x:<subject>``."""

    mode = "exchange"

    def __init__(self, om: ReviewOM) -> None:
        self.om = om
        self.calls = 0
        self.forgotten: list[str] = []

    def forget(self, principal: Principal) -> None:
        self.forgotten.append(principal.subject)

    async def om_for(self, principal: Principal) -> OMClient:
        if principal.type is not PrincipalType.HUMAN:
            raise LakeloomError("forbidden", "on-behalf-of OM access is for human principals", "h")
        self.calls += 1
        return cast("OMClient", TokenOM(self.om, f"x:{principal.subject}"))


class ReviewKit(Kit):
    om: ReviewOM

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        max_ops: int = 4,  # widened(2): two parts
        policies: Sequence[ModelPolicyEntry] = (),
        obo: Any = None,
        audited: bool = True,
    ) -> None:
        om = ReviewOM()
        bot = cast("OMClient", TokenOM(om, BOT))
        super().__init__(engine, om, audited=True, max_ops=max_ops, policies=policies, client=bot)
        self.obo = obo or FakeObo(om)
        self.review = ReviewService(
            self.service,
            self.repo,
            cast("OnBehalfOf", self.obo),
            self.models,
            ConfirmTokens(SECRET, 600),
            Settings(public_url="http://x"),  # pyright: ignore[reportCallIssue]
            self.audit if audited else None,
            policies,
        )

    async def reviews(self) -> list[Any]:
        """The ``review_proposal`` audit rows, oldest first."""
        return [r for r in await self.audit_rows() if r.tool == "review_proposal"]

    def mark(self) -> int:
        return len(self.om.token_calls)

    def tokens_since(self, mark: int) -> set[str]:
        return {token for token, _ in self.om.token_calls[mark:]}

    async def submitted(self, *tables: Any, model: str = "shop") -> Proposal:
        scan_id = await self.scan(*(tables or (customers(),)))
        p = await self.service.propose_from_scan(scan_id, model, principal=AGENT, client=None)
        return await self.service.submit(p.id, principal=AGENT, client=None)

    async def approve(self, p: Proposal, who: Principal = ALICE, **kw: Any) -> Proposal:
        preview = await self.review.review(p.id, "approve", principal=who, client="pytest", **kw)
        assert isinstance(preview, ConfirmPreview)
        out = await self.review.review(
            p.id,
            "approve",
            principal=who,
            client="pytest",
            confirm_token=preview.confirm_token,
            **kw,
        )
        assert isinstance(out, Proposal)
        return out


@pytest.fixture
def kit(engine: AsyncEngine) -> ReviewKit:
    return ReviewKit(engine)


@pytest.fixture
def build(engine: AsyncEngine):
    def make(**kw: Any) -> ReviewKit:
        return ReviewKit(engine, **kw)

    return make


async def _two_parts(kit: ReviewKit) -> Proposal:
    p = await kit.submitted(*widened(2))
    assert len(p.parts) == 2
    return p


async def _error(call: Any) -> LakeloomError:
    with pytest.raises(LakeloomError) as info:
        await call
    return info.value


# --- FR-REV-01 -----------------------------------------------------------------------------


async def test_fr_rev_01_agent_refused_explicitly_and_audited(kit: ReviewKit) -> None:
    p = await kit.submitted()
    agent = human("agent-7", kind=PrincipalType.AGENT)
    mark = kit.mark()
    for decision in ("approve", "reject", "request_changes"):
        err = await _error(
            kit.review.review(p.id, decision, principal=agent, client="c", reason="r")
        )
        assert err.code == "forbidden"
        assert err.message == "agent principals cannot review proposals (FR-REV-01)"
    assert kit.om.token_calls[mark:] == []
    assert kit.obo.calls == 0
    rows = await kit.reviews()
    assert len(rows) == 3
    assert {(r.tool, r.result, r.principal_type, r.principal_subject) for r in rows} == {
        ("review_proposal", "forbidden", "agent", "agent-7")
    }
    assert (await kit.repo.get(p.id)).status == "submitted"


async def test_human_without_review_scope_refused(kit: ReviewKit) -> None:
    p = await kit.submitted()
    mark = kit.mark()
    err = await _error(
        kit.review.review(p.id, "approve", principal=human(scopes=("lakeloom:read",)), client=None)
    )
    assert err.code == "forbidden" and "lakeloom:review" in err.message
    assert kit.om.token_calls[mark:] == []
    assert (await kit.reviews())[0].result == "forbidden"


async def test_outside_review_groups_refused(build) -> None:
    policies = [
        ModelPolicyEntry.model_validate({"name": "shop", "access": {"review_groups": ["leads"]}})
    ]
    kit = build(policies=policies)
    p = await kit.submitted()
    mark = kit.mark()
    err = await _error(
        kit.review.review(p.id, "approve", principal=human(groups=("staff",)), client=None)
    )
    assert err.code == "forbidden"
    assert err.message == "model 'shop' restricts review to groups leads"
    assert kit.om.token_calls[mark:] == []
    out = await kit.approve(p, human(groups=("leads",)))
    assert out.status == "applied"


async def test_om_permission_denied_refused_before_any_write(kit: ReviewKit) -> None:
    p = await kit.submitted()
    kit.om.denied.add("alice")
    before = len(kit.om.calls)
    for decision in ("approve", "reject"):
        err = await _error(
            kit.review.review(p.id, decision, principal=ALICE, client=None, reason="r")
        )
        assert err.code == "forbidden"
        assert err.message == "OpenMetadata does not grant alice review permission on shop"
    names = {name for name, _ in kit.om.calls[before:]}
    assert names <= {"get_permissions", "list_terms"}
    assert (await kit.repo.get(p.id)).status == "submitted"
    assert [r.result for r in await kit.reviews()] == ["forbidden", "forbidden"]


# --- FR-REV-02 -----------------------------------------------------------------------------


async def test_fr_rev_02_apply_uses_reviewer_token_never_bot(kit: ReviewKit) -> None:
    p = await kit.submitted()
    mark = kit.mark()
    out = await kit.approve(p)
    assert out.status == "applied"
    assert kit.tokens_since(mark) == {"x:alice"}
    writes = {"apply_change_set", "get_permissions"}
    assert writes <= {name for _, name in kit.om.token_calls[mark:]}
    review = out.reviews[-1]
    assert review.decision == "approve"
    assert review.reviewer_subject == "alice" and review.reviewer_username == "alice"
    assert review.om_user == "alice"  # OM's applicationResult.appliedBy
    assert review.applied == p.change_set_ids()
    stored = await kit.repo.get(p.id)
    assert stored.status == "applied" and stored.reviews == out.reviews
    rows = await kit.reviews()
    assert (rows[-1].tool, rows[-1].result, rows[-1].principal_type) == (
        "review_proposal",
        "ok",
        "human",
    )


async def test_approve_preview_then_confirm_applies_parts_in_order(kit: ReviewKit) -> None:
    p = await _two_parts(kit)
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    assert kit.om.applied() == []
    assert p.id in preview.summary and "Approving" in preview.summary
    assert preview.diff is not None and preview.diff.changes
    out = await kit.review.review(
        p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
    )
    assert isinstance(out, Proposal) and out.status == "applied"
    names = [kit.om.change_sets[cs]["name"] for cs in kit.om.applied()]
    assert names == [
        f"lakeloom-{p.id}-1",
        f"lakeloom-{p.id}-1-native",
        f"lakeloom-{p.id}-2",
        f"lakeloom-{p.id}-2-native-r1",  # rebuilt: part 1 moved the working revision
    ]
    assert all(state == "APPLIED" for part in out.parts for state in part.om_state.values())


async def test_later_part_rebuilt_when_mechanically_stale(kit: ReviewKit) -> None:
    p = await _two_parts(kit)
    old_glossary, old_native = p.parts[1].change_set_ids.values()
    out = await kit.approve(p)
    part = out.parts[1]
    # the glossary side is applied as drafted; only the native side is re-drafted
    assert part.change_set_ids["glossary"] == old_glossary
    assert part.change_set_ids["native"] != old_native
    assert part.superseded_change_set_ids == [old_native]
    new = kit.om.change_sets[str(part.change_set_ids["native"])]
    assert new["kw"]["base_working_revision"] == 6  # after part 1's native apply
    assert kit.om.change_sets[str(old_native)]["state"] == "DISCARDED"
    assert out.parts[0].superseded_change_set_ids == []  # part 1 was current
    assert out.reviews[-1].applied == out.change_set_ids()


async def test_stale_base_is_conflict_and_apply_failed(kit: ReviewKit) -> None:
    p = await kit.submitted()
    glossary = p.parts[0].change_set_ids["glossary"]
    assert glossary is not None
    kit.om.fail[f"apply:{glossary}"] = OMConflict(
        "expected version 0.1 but found 0.2",
        400,
        entity_ids=("op-17",),
        kind="stale_base",
    )
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    err = await _error(
        kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )
    )
    assert err.code == "conflict"
    assert "op-17" in err.message
    assert err.hint == REBASE_HINT
    stored = await kit.repo.get(p.id)
    assert stored.status == "apply_failed"
    assert stored.parts[0].change_set_ids["glossary"] == glossary  # never auto-rebuilt
    assert stored.parts[0].superseded_change_set_ids == []
    assert stored.reviews[-1].decision == "approve" and stored.reviews[-1].warnings
    assert (await kit.reviews())[-1].error_code == "conflict"


async def test_partial_failure_then_retry_continues(kit: ReviewKit) -> None:
    p = await _two_parts(kit)
    second = p.parts[1].change_set_ids["glossary"]
    kit.om.fail[f"apply:{second}"] = OMUnavailable("down", 503)
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    err = await _error(
        kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )
    )
    assert err.code == "upstream_unavailable"
    stored = await kit.repo.get(p.id)
    assert stored.status == "apply_failed"
    assert stored.parts[0].om_state == {"glossary": "APPLIED", "native": "APPLIED"}
    assert stored.parts[1].om_state["glossary"] == "SUBMITTED"
    first_run = kit.om.applied()
    assert first_run == [*p.parts[0].change_set_ids.values(), second]  # the last one failed

    out = await kit.approve(p)
    assert out.status == "applied"
    retried = kit.om.applied()[len(first_run) :]  # part 1 is not applied again
    assert set(retried).isdisjoint(set(p.parts[0].change_set_ids.values()))
    assert retried[0] == second


async def test_realizations_tagged_after_apply_idempotent(kit: ReviewKit) -> None:
    for name in ("orders", "customers"):
        fqn = f"svc.db.sales.{name}"
        kit.om.tables[fqn] = OMTable.model_validate({"id": f"t-{name}", "fullyQualifiedName": fqn})
    p = await kit.service.propose_realization(
        None, "retail", "retail.Customer", "svc.db.sales.orders", principal=AGENT, client=None
    )
    p = await kit.service.propose_realization(
        p.id, None, "retail.Customer", "svc.db.sales.customers", principal=AGENT, client=None
    )
    p = await kit.service.submit(p.id, principal=AGENT, client=None)
    kit.om.fail["tag:svc.db.sales.customers"] = OMUnavailable("down", 503)
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    err = await _error(
        kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )
    )
    assert err.code == "upstream_unavailable"
    stored = await kit.repo.get(p.id)
    assert stored.status == "apply_failed"
    assert stored.realizations_applied == ["real:Customer|svc.db.sales.orders|"]

    out = await kit.approve(p)
    assert out.status == "applied"
    assert out.realizations_applied == [
        "real:Customer|svc.db.sales.orders|",
        "real:Customer|svc.db.sales.customers|",
    ]
    tagged = [args for name, args in kit.om.calls if name == "tag"]
    assert tagged == [
        ("svc.db.sales.orders", None, "retail.Customer"),
        ("svc.db.sales.customers", None, "retail.Customer"),  # failed
        ("svc.db.sales.customers", None, "retail.Customer"),  # retried, orders not again
    ]


# --- FR-REV-03 -----------------------------------------------------------------------------


async def test_fr_rev_03_reject_requires_reason_discards_and_stores_reason(
    kit: ReviewKit,
) -> None:
    p = await kit.submitted()
    for reason in (None, "", "   "):
        err = await _error(
            kit.review.review(p.id, "reject", principal=ALICE, client=None, reason=reason)
        )
        assert err.code == "validation_error"
    mark = kit.mark()
    preview = await kit.review.review(
        p.id, "reject", principal=ALICE, client=None, reason="Not our *domain*"
    )
    assert isinstance(preview, ConfirmPreview)
    assert not [c for c in kit.om.calls if c[0] == "DISCARDED"]
    out = await kit.review.review(
        p.id,
        "reject",
        principal=ALICE,
        client=None,
        reason="Not our *domain*",
        confirm_token=preview.confirm_token,
    )
    assert isinstance(out, Proposal) and out.status == "rejected"
    assert [c[1] for c in kit.om.calls if c[0] == "DISCARDED"] == p.change_set_ids()
    assert kit.tokens_since(mark) == {"x:alice"}
    review = out.reviews[-1]
    assert review.decision == "reject"
    assert review.reason is not None and review.reason.text == "Not our *domain*"
    assert review.reason.untrusted
    stored = await kit.repo.get(p.id)
    assert stored.status == "rejected" and stored.reviews[-1].reason == review.reason
    assert kit.om.applied() == []


async def test_reject_reason_is_length_capped(kit: ReviewKit) -> None:
    p = await kit.submitted()
    out = await kit.review.review(
        p.id, "request_changes", principal=ALICE, client=None, reason="x" * 10_000
    )
    assert isinstance(out, Proposal)
    reason = out.reviews[-1].reason
    assert reason is not None and len(reason.text) == 4000


async def test_reject_after_partial_apply_keeps_applied_parts(kit: ReviewKit) -> None:
    p = await _two_parts(kit)
    kit.om.fail[f"apply:{p.parts[1].change_set_ids['glossary']}"] = OMUnavailable("down", 503)
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    await _error(
        kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )
    )
    preview = await kit.review.review(p.id, "reject", principal=ALICE, client=None, reason="no")
    assert isinstance(preview, ConfirmPreview)
    assert "parts already applied stay in OpenMetadata: 1" in preview.summary
    out = await kit.review.review(
        p.id,
        "reject",
        principal=ALICE,
        client=None,
        reason="no",
        confirm_token=preview.confirm_token,
    )
    assert isinstance(out, Proposal) and out.status == "rejected"
    assert out.parts[0].om_state == {"glossary": "APPLIED", "native": "APPLIED"}
    assert set(out.parts[1].om_state.values()) == {"DISCARDED"}
    assert "parts already applied stay in OpenMetadata: 1" in out.reviews[-1].warnings
    assert "parts already applied stay in OpenMetadata: 1" in summary_markdown(out)


async def test_discard_of_apply_failed_keeps_applied_parts(kit: ReviewKit) -> None:
    p = await _two_parts(kit)
    kit.om.fail[f"apply:{p.parts[1].change_set_ids['glossary']}"] = OMUnavailable("down", 503)
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    await _error(
        kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )
    )
    # apply_failed is still open (OPEN_STATUSES): its owner may discard it
    gone = await kit.service.discard(p.id, principal=AGENT, client=None)
    assert isinstance(gone, ConfirmPreview)
    assert "parts already applied stay in OpenMetadata: 1" in gone.summary
    out = await kit.service.discard(
        p.id, principal=AGENT, client=None, confirm_token=gone.confirm_token
    )
    assert isinstance(out, Proposal) and out.status == "discarded"
    assert out.parts[0].om_state == {"glossary": "APPLIED", "native": "APPLIED"}
    assert "parts already applied stay in OpenMetadata: 1" in out.warnings


async def test_request_changes_requires_comment_no_om_call(kit: ReviewKit) -> None:
    p = await kit.submitted()
    err = await _error(kit.review.review(p.id, "request_changes", principal=ALICE, client=None))
    assert err.code == "validation_error"
    mark = kit.mark()
    out = await kit.review.review(
        p.id, "request_changes", principal=ALICE, client=None, reason="Split Customer"
    )
    assert isinstance(out, Proposal) and out.status == "changes_requested"
    assert kit.om.token_calls[mark:] == []
    assert kit.obo.calls == 0
    review = out.reviews[-1]
    assert review.decision == "request_changes" and review.om_user is None
    assert review.reason is not None and review.reason.text == "Split Customer"
    assert (await kit.repo.get(p.id)).status == "changes_requested"
    # only a submitted proposal can get a change request
    err = await _error(
        kit.review.review(p.id, "request_changes", principal=ALICE, client=None, reason="again")
    )
    assert err.code == "validation_error"
    rows = await kit.reviews()
    assert [r.result for r in rows] == ["error", "ok", "error"]


async def test_status_gates_each_decision(kit: ReviewKit) -> None:
    scan_id = await kit.scan(customers())
    draft = await kit.service.propose_from_scan(scan_id, "shop", principal=AGENT, client=None)
    for decision in ("approve", "reject", "request_changes"):
        err = await _error(
            kit.review.review(draft.id, decision, principal=ALICE, client=None, reason="r")
        )
        assert err.code == "validation_error" and "draft" in err.message


# --- concurrency and tokens ------------------------------------------------------------------


async def test_concurrent_confirms_apply_once(kit: ReviewKit) -> None:
    p = await kit.submitted()
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)

    async def confirm() -> Proposal | ConfirmPreview:
        return await kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )

    results = await asyncio.gather(confirm(), confirm(), return_exceptions=True)
    ok = [r for r in results if isinstance(r, Proposal)]
    failed = [r for r in results if isinstance(r, LakeloomError)]
    assert len(ok) == 1 and len(failed) == 1
    assert failed[0].code == "conflict"
    assert kit.om.applied() == p.change_set_ids()  # each set applied exactly once


async def test_replayed_token_conflict(kit: ReviewKit) -> None:
    p = await kit.submitted()
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    await kit.review.review(
        p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
    )
    err = await _error(
        kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )
    )
    assert err.code == "conflict"
    # a token is bound to its reviewer
    q = await kit.submitted(widened(1)[0])
    preview = await kit.review.review(q.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    err = await _error(
        kit.review.review(
            q.id,
            "approve",
            principal=human("bob"),
            client=None,
            confirm_token=preview.confirm_token,
        )
    )
    assert err.code == "validation_error"
    assert kit.om.applied() == p.change_set_ids()


async def test_token_bound_to_om_state(kit: ReviewKit) -> None:
    p = await kit.submitted()
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    assert isinstance(preview, ConfirmPreview)
    kit.om.change_sets[p.change_set_ids()[0]]["version"] += 1
    err = await _error(
        kit.review.review(
            p.id, "approve", principal=ALICE, client=None, confirm_token=preview.confirm_token
        )
    )
    assert err.code == "conflict"
    assert kit.om.applied() == []


async def test_obo_unconfigured_fails_closed(engine: AsyncEngine, tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        settings = Settings(public_url="http://x")  # pyright: ignore[reportCallIssue]
        holder = ReviewKit(engine)
        obo = OnBehalfOf(settings, http, holder.service._om)  # pyright: ignore[reportPrivateUsage]
        assert obo.mode == "unavailable"
        kit = ReviewKit(engine, obo=obo)
        p = await kit.submitted()
        mark = kit.mark()
        for decision in ("approve", "reject"):
            err = await _error(
                kit.review.review(p.id, decision, principal=ALICE, client=None, reason="r")
            )
            assert err.code == "forbidden" and err.message == "human review is not configured"
        assert kit.om.token_calls[mark:] == []  # never the bot
        assert (await kit.repo.get(p.id)).status == "submitted"


async def test_exchange_error_never_falls_back_to_the_bot(kit: ReviewKit) -> None:
    p = await kit.submitted()

    async def refused(principal: Principal) -> OMClient:
        raise LakeloomError("forbidden", "token exchange refused", "h")

    kit.obo.om_for = refused  # type: ignore[method-assign]
    mark = kit.mark()
    err = await _error(kit.review.review(p.id, "approve", principal=ALICE, client=None))
    assert err.code == "forbidden" and err.message == "token exchange refused"
    assert kit.om.token_calls[mark:] == []


# --- after apply -----------------------------------------------------------------------------


async def test_model_cache_invalidated_after_apply(
    kit: ReviewKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await kit.submitted()
    seen: list[str | None] = []
    monkeypatch.setattr(kit.models, "invalidate", lambda slug=None: seen.append(slug))
    await kit.approve(p)
    assert seen == ["shop"]


async def _with_update(kit: ReviewKit) -> Proposal:
    kit.om.terms["shop"] = [term("shop", "Customer", 0.1)]
    kit.om.descriptions["shop.Customer"] = "existing"
    scan_id = await kit.scan(widened(1)[0])
    p = await kit.service.propose_from_scan(scan_id, "shop", principal=AGENT, client=None)
    update = UpdateConcept(
        op="update_concept",
        fqn="shop.Customer",
        base_version="0.1",
        description=UntrustedText(text="Buyers", source="agent"),
    )
    p = await kit.service.propose_change(p.id, [update], principal=AGENT, client=None)
    return await kit.service.submit(p.id, principal=AGENT, client=None)


async def test_description_recheck_silent_when_om_applied_it(kit: ReviewKit) -> None:
    p = await _with_update(kit)
    out = await kit.approve(p)
    assert out.reviews[-1].warnings == []  # OM applied the description: silent
    assert kit.om.descriptions["shop.Customer"] == "Buyers"


async def test_description_recheck_warns_when_om_dropped_it(kit: ReviewKit) -> None:
    p = await _with_update(kit)
    kit.om.keep_descriptions = True
    out = await kit.approve(p)
    assert out.status == "applied"
    (warning,) = out.reviews[-1].warnings
    assert "shop.Customer" in warning and "description" in warning


async def test_summary_shows_reviews(kit: ReviewKit) -> None:
    p = await kit.submitted()
    hostile = "Fix it ![x](https://evil/?q=1)\n# heading"
    out = await kit.review.review(
        p.id, "request_changes", principal=ALICE, client=None, reason=hostile
    )
    assert isinstance(out, Proposal)
    text = summary_markdown(out)
    assert "## Reviews" in text
    assert "request_changes by `alice`" in text
    assert "> [untrusted text from reviewer alice]" in text
    assert "![x](https://evil" not in text and "\n# heading" not in text


# --- final-review fixes (M2b) ------------------------------------------------------------------


async def _confirm(kit: ReviewKit, p: Proposal, who: Principal = ALICE) -> LakeloomError:
    """Preview, then a confirm that fails; the error."""
    preview = await kit.review.review(p.id, "approve", principal=who, client=None)
    assert isinstance(preview, ConfirmPreview)
    return await _error(
        kit.review.review(
            p.id, "approve", principal=who, client=None, confirm_token=preview.confirm_token
        )
    )


async def test_stale_base_conflict_names_the_entity_not_the_operation_id(kit: ReviewKit) -> None:
    """FR-OM-03: OM names its operation id; the reviewer reads the term."""
    p = await _with_update(kit)
    glossary = str(p.parts[-1].change_set_ids["glossary"])
    op_id = next(
        o["id"]
        for o in kit.om.change_sets[glossary]["operations"]
        if o["operationType"] == "UPDATE_TERM"
    )
    kit.om.fail[f"apply:{glossary}"] = OMConflict(
        "expected version", 400, entity_ids=(op_id,), kind="stale_base"
    )
    err = await _confirm(kit, p)
    assert err.code == "conflict" and err.hint == REBASE_HINT
    assert "affected: `shop.Customer`" in err.message
    assert op_id not in err.message


async def test_stale_base_conflict_keeps_ids_it_cannot_match(kit: ReviewKit) -> None:
    p = await kit.submitted()
    glossary = str(p.parts[0].change_set_ids["glossary"])
    kit.om.fail[f"apply:{glossary}"] = OMConflict(
        "expected version", 400, entity_ids=("0b5c0d6e-unknown",), kind="stale_base"
    )
    err = await _confirm(kit, p)
    assert err.code == "conflict" and "affected: `0b5c0d6e-unknown`" in err.message


async def test_apply_failed_set_is_reapplied_on_retry(kit: ReviewKit) -> None:
    """Live rc1 (ADR 0012): an APPLY_FAILED set is rolled back and kept; apply re-runs it."""
    p = await kit.submitted()
    glossary = str(p.parts[0].change_set_ids["glossary"])
    kit.om.apply_failed[glossary] = "A term with the name 'Customer' already exists"
    err = await _confirm(kit, p)
    assert err.code == "upstream_unavailable"
    assert "state APPLY_FAILED, 1 operation(s) failed" in err.message
    assert "already exists" in err.message  # OM's reason, quoted
    assert "approve again to re-run it once the cause is fixed" in err.hint
    stored = await kit.repo.get(p.id)
    assert stored.status == "apply_failed"
    assert stored.parts[0].om_state["glossary"] == "APPLY_FAILED"

    out = await kit.approve(p)
    assert out.status == "applied"
    assert kit.om.applied().count(glossary) == 2
    assert out.parts[0].change_set_ids["glossary"] == glossary  # not re-drafted


async def test_apply_failed_set_is_discarded_on_reject(kit: ReviewKit) -> None:
    p = await kit.submitted()
    glossary = str(p.parts[0].change_set_ids["glossary"])
    kit.om.apply_failed[glossary] = "no"
    await _confirm(kit, p)
    preview = await kit.review.review(p.id, "reject", principal=ALICE, client=None, reason="no")
    assert isinstance(preview, ConfirmPreview)
    out = await kit.review.review(
        p.id,
        "reject",
        principal=ALICE,
        client=None,
        reason="no",
        confirm_token=preview.confirm_token,
    )
    assert isinstance(out, Proposal) and out.status == "rejected"
    assert ("DISCARDED", glossary) in kit.om.calls
    assert set(out.parts[0].om_state.values()) == {"DISCARDED"}


async def test_applied_with_failed_operations_warns_and_does_not_promise_a_retry(
    kit: ReviewKit,
) -> None:
    p = await kit.submitted()
    glossary = str(p.parts[0].change_set_ids["glossary"])
    kit.om.failed_ops[glossary] = 1
    out = await kit.approve(p)
    assert out.status == "applied"
    assert glossary in out.reviews[-1].applied
    (warning,) = [w for w in out.reviews[-1].warnings if glossary in w]
    assert "1 failed operation(s)" in warning and "`rc1 said no`" in warning
    assert "approve again" not in warning


async def _relation_after_update(kit: ReviewKit, order_base: str | None = None) -> Proposal:
    """Part A: UpdateConcept Customer (+ Product); part B: AddRelation Customer -> Order
    (+ UpdateConcept Order with the agent's ``order_base``). max_ops=2."""
    kit.om.terms["shop"] = [term("shop", name, 0.1) for name in ("Customer", "Order", "Product")]
    scan_id = await kit.scan(widened(1)[0])
    p = await kit.service.propose_from_scan(scan_id, "shop", principal=AGENT, client=None)

    def update(name: str, base: str = "0.1") -> UpdateConcept:
        return UpdateConcept(
            op="update_concept",
            fqn=f"shop.{name}",
            base_version=base,
            description=UntrustedText(text=f"{name}!", source="agent"),
        )

    relation = AddRelation(
        op="add_relation", source="shop.Customer", target="shop.Order", relation_type="hasCustomer"
    )
    ops: list[Any] = [update("Customer"), update("Product"), relation]
    if order_base is not None:
        ops.append(update("Order", order_base))
    p = await kit.service.propose_change(p.id, ops, principal=AGENT, client=None)
    p = await kit.service.submit(p.id, principal=AGENT, client=None)
    a, b = p.parts[-2:]
    assert [o.op for o in a.operations] == ["update_concept", "update_concept"]
    assert b.operations[0].op == "add_relation"
    kit.om.check_bases = True
    return p


async def test_relation_on_a_term_an_earlier_part_updated_is_rebuilt_not_a_conflict(
    build,
) -> None:
    """ADR 0012 § Review: a base Lakeloom took from OM (not the agent's) that an earlier
    part of the same approve moved is re-drafted once, then applied."""
    kit: ReviewKit = build(max_ops=2)
    p = await _relation_after_update(kit)
    old = p.parts[-1].change_set_ids["glossary"]
    out = await kit.approve(p)
    assert out.status == "applied"
    part = out.parts[-1]
    assert part.change_set_ids["glossary"] != old
    assert old in part.superseded_change_set_ids
    new = kit.om.change_sets[str(part.change_set_ids["glossary"])]
    (relation,) = [o for o in new["operations"] if o["operationType"] == "ADD_RELATIONSHIP"]
    assert relation["baseVersion"] == 0.2  # Customer after part A's apply
    assert kit.om.change_sets[str(old)]["state"] == "DISCARDED"


async def test_stale_agent_base_still_conflicts_after_a_derived_rebuild(build) -> None:
    kit: ReviewKit = build(max_ops=2)
    p = await _relation_after_update(kit, order_base="0.1")
    i = next(i for i, t in enumerate(kit.om.terms["shop"]) if t.name == "Order")
    kit.om.terms["shop"][i] = term("shop", "Order", 0.3)  # someone edited Order meanwhile
    err = await _confirm(kit, p)
    assert err.code == "conflict" and err.hint == REBASE_HINT
    assert "affected: `shop.Order`" in err.message
    stored = await kit.repo.get(p.id)
    assert stored.status == "apply_failed"
    part = stored.parts[-1]
    assert len(part.superseded_change_set_ids) == 2  # rebuilt once for Customer's relation
    new = kit.om.change_sets[str(part.change_set_ids["glossary"])]
    (update,) = [o for o in new["operations"] if o["operationType"] == "UPDATE_TERM"]
    assert update["baseVersion"] == 0.1  # the agent's base: never refreshed


async def test_reviewer_cannot_review_own_proposal(kit: ReviewKit) -> None:
    p = await kit.submitted()  # created by agent-7
    me = human(AGENT.subject)
    mark = kit.mark()
    for decision in ("approve", "reject", "request_changes"):
        err = await _error(kit.review.review(p.id, decision, principal=me, client="c", reason="r"))
        assert err.code == "forbidden" and err.message == SELF_REVIEW
    assert kit.om.token_calls[mark:] == []
    assert kit.obo.calls == 0
    rows = await kit.reviews()
    assert [r.result for r in rows] == ["forbidden"] * 3
    assert {r.proposal_id for r in rows} == {p.id}
    assert (await kit.repo.get(p.id)).status == "submitted"
    # an admin gets no exemption
    admin = human(AGENT.subject, scopes=(REVIEW, "lakeloom:admin"))
    err = await _error(kit.review.review(p.id, "approve", principal=admin, client=None))
    assert err.message == SELF_REVIEW


async def test_om_401_on_apply_is_forbidden_and_forgets_the_exchanged_token(
    kit: ReviewKit,
) -> None:
    p = await kit.submitted()
    glossary = str(p.parts[0].change_set_ids["glossary"])
    kit.om.fail[f"apply:{glossary}"] = OMError("OpenMetadata returned HTTP 401", 401)
    err = await _confirm(kit, p)
    assert err.code == "forbidden" and err.message == SESSION_REJECTED
    assert "sign in" in err.hint
    assert kit.obo.forgotten == ["alice"]
    assert (await kit.repo.get(p.id)).status == "apply_failed"
    assert (await kit.reviews())[-1].error_code == "forbidden"
    out = await kit.approve(p)  # signed in again
    assert out.status == "applied"


async def test_om_401_on_a_read_is_forbidden_too(kit: ReviewKit) -> None:
    p = await kit.submitted()
    kit.om.fail["get_permissions"] = OMError("OpenMetadata returned HTTP 401", 401)
    err = await _error(kit.review.review(p.id, "approve", principal=ALICE, client=None))
    assert err.code == "forbidden" and err.message == SESSION_REJECTED
    assert kit.obo.forgotten == ["alice"]
    assert (await kit.repo.get(p.id)).status == "submitted"


async def test_request_changes_on_apply_failed(kit: ReviewKit) -> None:
    p = await kit.submitted()
    kit.om.fail[f"apply:{p.parts[0].change_set_ids['glossary']}"] = OMUnavailable("down", 503)
    await _confirm(kit, p)
    out = await kit.review.review(
        p.id, "request_changes", principal=ALICE, client=None, reason="split it"
    )
    assert isinstance(out, Proposal) and out.status == "changes_requested"


async def test_om_forbidden_part_way_keeps_progress(kit: ReviewKit) -> None:
    p = await _two_parts(kit)
    second = str(p.parts[1].change_set_ids["glossary"])
    kit.om.fail[f"apply:{second}"] = OMForbidden(
        "OpenMetadata returned HTTP 403 (forbidden)", 403, "operations [EditAll] not allowed"
    )
    err = await _confirm(kit, p)
    assert err.code == "forbidden"
    assert "operations [EditAll] not allowed" in err.message
    assert "LakeloomOntologyReviewer" in err.hint
    stored = await kit.repo.get(p.id)
    assert stored.status == "apply_failed"
    assert stored.parts[0].om_state == {"glossary": "APPLIED", "native": "APPLIED"}
    assert stored.parts[1].om_state["glossary"] == "SUBMITTED"
    review = stored.reviews[-1]
    assert review.applied == list(p.parts[0].change_set_ids.values())
    assert any(w.startswith("apply stopped: forbidden") for w in review.warnings)
    out = await kit.approve(p)  # the role granted: continues with part 2
    assert out.status == "applied"
    assert kit.om.applied().count(str(p.parts[0].change_set_ids["glossary"])) == 1


async def test_concurrent_confirms_by_two_reviewers_apply_once(kit: ReviewKit) -> None:
    p = await kit.submitted()
    bob = human("bob")
    previews = [
        await kit.review.review(p.id, "approve", principal=who, client=None) for who in (ALICE, bob)
    ]
    assert all(isinstance(x, ConfirmPreview) for x in previews)

    async def confirm(who: Principal, preview: Any) -> Proposal | ConfirmPreview:
        return await kit.review.review(
            p.id, "approve", principal=who, client=None, confirm_token=preview.confirm_token
        )

    results = await asyncio.gather(
        confirm(ALICE, previews[0]), confirm(bob, previews[1]), return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, Proposal)]
    failed = [r for r in results if isinstance(r, LakeloomError)]
    assert len(ok) == 1 and len(failed) == 1
    assert failed[0].code == "conflict"
    assert kit.om.applied() == p.change_set_ids()  # each set applied exactly once
    assert len([r for r in ok[0].reviews if r.decision == "approve"]) == 1
