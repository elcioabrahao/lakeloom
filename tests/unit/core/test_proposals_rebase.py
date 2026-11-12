"""Per-model propose groups, the proposal rebuild primitive and rebase (NFR-SEC-07, FR-OM-03).

Reuses the fake OM and the kit of ``test_proposals``.
"""

from dataclasses import dataclass
from typing import Any, cast

import pytest
from test_proposals import (
    AGENT,
    FakeOM,
    Kit,
    customers,
    engine_fixture,  # noqa: F401  (the engine fixture)
    make_fixture,  # noqa: F401  (the make fixture)
    shop_term,
    widened,
)

from lakeloom.core.audit import AuditContext
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.core.operations import (
    AddAttribute,
    AddConcept,
    AddSynonym,
    RemoveAttribute,
    RemoveConcept,
    UpdateConcept,
    draft_term_id,
)
from lakeloom.core.proposals import REBASE_HINT, Actor, Proposal
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMUnavailable
from lakeloom.om.models import OMGlossaryTerm, OMTable

ADMIN = Actor(subject="root", scopes=frozenset({"lakeloom:admin"}))
SHOP_STEWARDS = [
    ModelPolicyEntry.model_validate({"name": "shop", "access": {"propose_groups": ["stewards"]}})
]


@dataclass(frozen=True)
class Human:
    """An MCP principal as far as the service cares (subject, type, scopes, groups)."""

    subject: str
    type: str = "human"
    scopes: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()


class Spy:
    """An OM client facade over a :class:`FakeOM` that logs the methods called through it."""

    def __init__(self, inner: FakeOM) -> None:
        self.inner = inner
        self.log: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if callable(attr):

            def call(*args: Any, **kwargs: Any) -> Any:
                self.log.append(name)
                return attr(*args, **kwargs)

            return call
        return attr

    @property
    def client(self) -> OMClient:
        return cast("OMClient", self)


def _apply(om: FakeOM, *cs_ids: str | None) -> None:
    for cs in cs_ids:
        assert cs is not None
        om.change_sets[cs]["state"] = "APPLIED"


async def _store_states(kit: Kit, p: Proposal) -> Proposal:
    """Write the fake's change-set states into the store (what the review service will do)."""
    p = await kit.repo.get(p.id)
    for part in p.parts:
        for side, cs in part.change_set_ids.items():
            if cs is not None:
                part.om_state[side] = kit.om.change_sets[cs]["state"]
    expected = p.revision
    p.revision += 1
    await kit.repo.update(p, expected_revision=expected)
    return p


async def _set_status(kit: Kit, p: Proposal, status: str) -> Proposal:
    p = await kit.repo.get(p.id)
    p.status = status  # type: ignore[assignment]
    expected = p.revision
    p.revision += 1
    await kit.repo.update(p, expected_revision=expected)
    return p


async def _shop(kit: Kit, principal: Any = AGENT) -> Proposal:
    scan_id = await kit.scan(customers())
    return await kit.service.propose_from_scan(scan_id, "shop", principal=principal, client=None)


def _recorded(p: Proposal) -> set[str]:
    out = set(p.change_set_ids()) | {a.id for a in p.abandoned_change_sets}
    for part in p.parts:
        out |= set(part.superseded_change_set_ids)
    return out


# --- NFR-SEC-07 -------------------------------------------------------------------------------


async def test_propose_refused_outside_propose_groups(make) -> None:
    kit = make(policies=SHOP_STEWARDS)
    outsider = Human("bob", groups=frozenset({"staff"}))
    steward = Human("alice", groups=frozenset({"stewards"}))
    kit.om.tables["svc.db.sales.orders"] = OMTable.model_validate(
        {"id": "t1", "fullyQualifiedName": "svc.db.sales.orders"}
    )
    scan_id = await kit.scan(customers())
    with pytest.raises(LakeloomError) as info:
        await kit.service.propose_from_scan(scan_id, "shop", principal=outsider, client=None)
    assert info.value.code == "forbidden"
    assert info.value.message == "model 'shop' restricts propose to groups stewards"
    assert kit.om.created() == []
    assert (await kit.audit_rows())[-1].result == "forbidden"

    p = await kit.service.propose_from_scan(scan_id, "shop", principal=steward, client=None)
    attr = AddAttribute(op="add_attribute", concept="Customer", name="email", datatype="xsd:string")
    # an admin outside the groups may not extend it either (access is per model)
    with pytest.raises(LakeloomError) as change:
        await kit.service.propose_change(
            p.id, [attr], principal=Human("root", scopes=frozenset({"lakeloom:admin"})), client=None
        )
    assert change.value.code == "forbidden"
    with pytest.raises(LakeloomError) as realization:
        await kit.service.propose_realization(
            None, "shop", "Customer", "svc.db.sales.orders", principal=outsider, client=None
        )
    assert realization.value.code == "forbidden"
    # the CLI operator is exempt; other models are unrestricted
    await kit.service.propose_change(p.id, [attr], principal=Actor(subject="alice"), client=None)
    await kit.service.propose_realization(
        None, "retail", "retail.Customer", "svc.db.sales.orders", principal=outsider, client=None
    )


# --- rebuild -----------------------------------------------------------------------------------


async def test_rebuild_discards_and_recreates_with_fresh_context(make) -> None:
    kit = make(client=Spy)
    bot = cast("Spy", kit.service._om)  # pyright: ignore[reportPrivateUsage]
    kit.om.terms["shop"] = [shop_term("Order", 0.4)]
    p = await _shop(kit)
    synonym = AddSynonym(op="add_synonym", concept_fqn="Order", synonym="Purchase")
    p = await kit.service.propose_change(p.id, [synonym], principal=AGENT, client=None)
    old_glossary, old_native = p.change_set_ids()
    # OM moved on: the term and the native working revision changed
    kit.om.terms["shop"] = [shop_term("Order", 0.7)]
    kit.om.revision = 9
    reviewer = Spy(kit.om)
    bot.log.clear()
    before = len(kit.om.calls)

    await kit.service.rebuild_parts(
        p, p.parts, om=reviewer.client, submit=True, trail=AuditContext()
    )

    assert bot.log == []  # every OM call went through the passed client
    assert {"discard_change_set", "create_change_set", "submit_change_set"} <= set(reviewer.log)
    calls = kit.om.calls[before:]
    assert [c[1] for c in calls if c[0] == "DISCARDED"] == [old_glossary, old_native]
    created = [kw for n, kw in calls if n == "create_change_set"]
    assert [kw["name"] for kw in created] == [
        f"lakeloom-{p.id}-1-r1",
        f"lakeloom-{p.id}-1-native-r1",
    ]
    assert created[1]["base_working_revision"] == 9
    update = next(o for o in created[0]["operations"] if o["operationType"] == "UPDATE_TERM")
    assert update["baseVersion"] == 0.7 and update["term"]["synonyms"] == ["Purchase"]
    part = p.parts[0]
    assert set(part.change_set_ids.values()).isdisjoint({old_glossary, old_native})
    assert part.om_state == {"glossary": "SUBMITTED", "native": "SUBMITTED"}
    assert [c[1] for c in calls if c[0] == "SUBMITTED"] == p.change_set_ids()
    stored = await kit.repo.get(p.id)
    assert stored.parts[0].change_set_ids == part.change_set_ids
    assert stored.parts[0].change_set_names == {
        "glossary": f"lakeloom-{p.id}-1-r1",
        "native": f"lakeloom-{p.id}-1-native-r1",
    }


async def test_rebuild_skips_applied_parts(make) -> None:
    kit = make(max_ops=8)
    scan_id = await kit.scan(*widened(4))  # 2 parts x 2 change sets
    p = await kit.service.propose_from_scan(scan_id, "shop", principal=AGENT, client=None)
    await kit.service.submit(p.id, principal=AGENT, client=None)
    first, second = p.parts
    _apply(kit.om, *first.change_set_ids.values())  # part 1 fully applied
    _apply(kit.om, second.change_set_ids["glossary"])  # part 2: glossary only
    # the terms both glossary sets created now exist in OM, with their draft ids

    kit.om.terms["shop"] = [
        OMGlossaryTerm.model_validate(
            {
                "id": draft_term_id(op),
                "name": op.name,
                "fullyQualifiedName": f"shop.{op.name}",
                "description": "applied",
                "version": 0.1,
                "iri": f"http://x/ontology/shop/{op.name}",
                "glossary": {"id": "id-shop", "type": "glossary"},
            }
        )
        for op in p.operations()
        if isinstance(op, AddConcept)
    ]
    p = await _store_states(kit, p)
    before = len(kit.om.calls)

    out = await kit.service.rebase(p.id, principal=AGENT, client=None)

    calls = kit.om.calls[before:]
    assert [c[1] for c in calls if c[0] == "DISCARDED"] == [second.change_set_ids["native"]]
    created = [kw for n, kw in calls if n == "create_change_set"]
    assert [kw["name"] for kw in created] == [f"lakeloom-{p.id}-2-native-r1"]
    assert out.parts[0].change_set_ids == first.change_set_ids  # untouched
    assert out.parts[0].superseded_change_set_ids == []
    assert out.parts[1].change_set_ids["glossary"] == second.change_set_ids["glossary"]
    assert out.parts[1].om_state["glossary"] == "APPLIED"
    assert out.parts[1].superseded_change_set_ids == [second.change_set_ids["native"]]
    assert out.status == "draft"


async def test_rebuild_records_superseded_ids(make) -> None:
    kit = make()
    p = await _shop(kit)
    g0, n0 = p.change_set_ids()
    del kit.om.change_sets[g0]  # gone from OM already (404): nothing to discard
    trail = AuditContext()
    await kit.service.rebuild_parts(p, p.parts, om=kit.om.client, submit=False, trail=trail)
    g1, n1 = p.change_set_ids()
    await kit.service.rebuild_parts(p, p.parts, om=kit.om.client, submit=False, trail=trail)
    part = (await kit.repo.get(p.id)).parts[0]
    assert part.superseded_change_set_ids == [g0, n0, g1, n1]
    assert part.change_set_names == {
        "glossary": f"lakeloom-{p.id}-1-r2",
        "native": f"lakeloom-{p.id}-1-native-r2",
    }
    assert [c[1] for c in kit.om.calls if c[0] == "DISCARDED"] == [n0, g1, n1]
    assert part.om_state == {"glossary": "DRAFT", "native": "DRAFT"}
    assert _recorded(await kit.repo.get(p.id)) == set(kit.om.change_sets) | {g0}


# --- rebase -----------------------------------------------------------------------------------


async def test_rebase_refreshes_base_versions_with_warning(make) -> None:
    kit = make()
    kit.om.terms["shop"] = [shop_term("Order", 0.4)]
    attr_iri = "http://x/ontology/shop/Customer.legacy"
    kit.om.native["shop"] = (
        [],
        [{"id": "prop-legacy", "iri": attr_iri, "name": "Customer.legacy", "version": 2.0}],
        [],
    )
    p = await _shop(kit)
    update = UpdateConcept(
        op="update_concept", fqn="Order", base_version="0.4", description=None, synonyms=["Buy"]
    )
    drop = RemoveAttribute(op="remove_attribute", concept="Customer", name="legacy")
    p = await kit.service.propose_change(p.id, [update, drop], principal=AGENT, client=None)
    await kit.service.submit(p.id, principal=AGENT, client=None)
    kit.om.terms["shop"] = [shop_term("Order", 0.6)]
    kit.om.native["shop"] = (
        [],
        [{"id": "prop-legacy", "iri": attr_iri, "name": "Customer.legacy", "version": 3.0}],
        [],
    )
    before = len(kit.om.calls)

    out = await kit.service.rebase(p.id, principal=AGENT, client="pytest")

    assert out.status == "draft"
    ops = out.operations()
    refreshed = next(o for o in ops if isinstance(o, UpdateConcept))
    assert refreshed.base_version == "0.6"
    assert "base version of `shop.Order` refreshed from 0.4 to 0.6; re-review the change" in (
        out.warnings
    )
    assert (
        "base version of `shop.Customer.legacy` refreshed from 2 to 3; re-review the change"
        in out.warnings
    )
    created = [kw for n, kw in kit.om.calls[before:] if n == "create_change_set"]
    glossary = next(o for o in created[0]["operations"] if o["operationType"] == "UPDATE_TERM")
    assert glossary["baseVersion"] == 0.6
    delete = next(o for o in created[1]["operations"] if o["operationType"] == "DELETE_PROPERTY")
    assert delete["baseVersion"] == 3.0
    stored = await kit.repo.get(p.id)
    assert stored.operations() == ops and stored.status == "draft"
    last = (await kit.audit_rows())[-1]
    assert (last.tool, last.result, last.proposal_id) == ("rebase_proposal", "ok", p.id)

    # the entity is gone: conflict naming it, proposal unchanged
    kit.om.terms["shop"] = []
    snapshot = await kit.repo.get(p.id)
    with pytest.raises(LakeloomError) as info:
        await kit.service.rebase(p.id, principal=AGENT, client=None)
    assert info.value.code == "conflict" and "shop.Order" in info.value.message
    assert (await kit.repo.get(p.id)).model_dump() == snapshot.model_dump()


async def test_rebase_only_owner_and_allowed_statuses(make) -> None:
    kit = make()
    p = await _shop(kit)
    with pytest.raises(LakeloomError) as other:
        await kit.service.rebase(p.id, principal=Actor(subject="someone"), client=None)
    assert other.value.code == "forbidden"
    for status in ("draft", "submitted", "changes_requested", "apply_failed"):
        await _set_status(kit, p, status)
        out = await kit.service.rebase(p.id, principal=ADMIN, client=None)
        assert out.status == "draft", status
    for status in ("applied", "rejected", "discarded"):
        await _set_status(kit, p, status)
        with pytest.raises(LakeloomError) as info:
            await kit.service.rebase(p.id, principal=AGENT, client=None)
        assert info.value.code == "validation_error" and status in info.value.message


async def test_propose_change_after_changes_requested_rebuilds_then_appends(make) -> None:
    kit = make()
    p = await _shop(kit)
    await kit.service.submit(p.id, principal=AGENT, client=None)
    old = p.change_set_ids()
    await _set_status(kit, p, "changes_requested")
    attr = AddAttribute(op="add_attribute", concept="Customer", name="email", datatype="xsd:string")
    before = len(kit.om.calls)

    out = await kit.service.propose_change(p.id, [attr], principal=AGENT, client=None)

    calls = kit.om.calls[before:]
    assert [c[1] for c in calls if c[0] == "DISCARDED"] == old
    assert not [c for c in calls if c[0] == "SUBMITTED"]  # rebuilt as drafts
    native = out.parts[0].change_set_ids["native"]
    assert native not in old
    names = [c[0] for c in calls]
    assert names.index("add_operations") > max(
        i for i, n in enumerate(names) if n == "create_change_set"
    )
    assert next(c[1][0] for c in calls if c[0] == "add_operations") == native
    assert out.status == "draft" and out.parts[0].superseded_change_set_ids == old
    assert len(out.parts[0].operations) == len(p.parts[0].operations) + 1

    # submit after changes_requested: rebuild (drafts), then submit the new sets
    await _set_status(kit, out, "changes_requested")
    current = out.change_set_ids()
    done = await kit.service.submit(p.id, principal=AGENT, client=None)
    assert done.status == "submitted"
    assert set(done.change_set_ids()).isdisjoint(current)
    assert done.parts[0].om_state == {"glossary": "SUBMITTED", "native": "SUBMITTED"}
    assert done.parts[0].change_set_names["native"] == f"lakeloom-{p.id}-1-native-r2"


async def test_rebuild_failure_midway_keeps_store_consistent(make) -> None:
    kit = make(max_ops=8)
    scan_id = await kit.scan(*widened(4))  # 2 parts
    p = await kit.service.propose_from_scan(scan_id, "shop", principal=AGENT, client=None)
    await kit.service.submit(p.id, principal=AGENT, client=None)

    created = 0
    real = kit.om.create_change_set

    async def flaky(**kw: Any):
        nonlocal created
        created += 1
        if created == 4:  # part 2, native side
            raise OMUnavailable("down")
        return await real(**kw)

    kit.om.create_change_set = flaky  # type: ignore[method-assign]
    with pytest.raises(LakeloomError) as info:
        await kit.service.rebase(p.id, principal=AGENT, client=None)
    assert info.value.code == "upstream_unavailable"
    stored = await kit.repo.get(p.id)
    assert stored.status == "draft"  # rebuilding started: the reviewed drafts are gone
    assert _recorded(stored) == set(kit.om.change_sets)  # every OM change set is recorded
    first, second = stored.parts
    assert first.change_set_names["native"] == f"lakeloom-{p.id}-1-native-r1"
    assert second.change_set_names["glossary"] == f"lakeloom-{p.id}-2-r1"
    assert second.om_state["native"] == "DISCARDED"  # discarded, not yet replaced
    assert stored.rebase_pending
    assert last_error(await kit.audit_rows()) == ("rebase_proposal", "upstream_unavailable")

    # submit continues the interrupted rebuild before submitting
    done = await kit.service.submit(p.id, principal=AGENT, client=None)
    assert done.status == "submitted"
    assert {s for part in done.parts for s in part.om_state.values()} == {"SUBMITTED"}
    assert _recorded(done) == set(kit.om.change_sets)
    assert done.parts[1].change_set_names["native"] == f"lakeloom-{p.id}-2-native-r2"


def last_error(rows: list[Any]) -> tuple[str, str | None]:
    return rows[-1].tool, rows[-1].error_code


def test_rebase_hint_is_actionable() -> None:
    assert "rebase_proposal" in REBASE_HINT and "M2b" not in REBASE_HINT


async def test_submit_after_rebase_skips_applied_parts(make) -> None:
    kit = make(max_ops=8)
    scan_id = await kit.scan(*widened(4))  # 2 parts
    p = await kit.service.propose_from_scan(scan_id, "shop", principal=AGENT, client=None)
    await kit.service.submit(p.id, principal=AGENT, client=None)
    first = p.parts[0]
    _apply(kit.om, *first.change_set_ids.values())  # part 1 applied, part 2 failed to apply
    await _store_states(kit, p)
    await _set_status(kit, p, "apply_failed")
    rebased = await kit.service.rebase(p.id, principal=AGENT, client=None)
    assert rebased.status == "draft"
    before = len(kit.om.calls)

    out = await kit.service.submit(p.id, principal=AGENT, client=None)

    submitted = [c[1] for c in kit.om.calls[before:] if c[0] == "SUBMITTED"]
    assert submitted == list(out.parts[1].change_set_ids.values())  # only the rebuilt part
    assert out.status == "submitted"
    assert out.parts[0].om_state == {"glossary": "APPLIED", "native": "APPLIED"}


async def test_failed_rebase_stores_only_what_om_holds_and_resumes(make) -> None:
    kit = make(max_ops=8)
    kit.om.terms["shop"] = [shop_term("Order", 0.4)]
    scan_id = await kit.scan(*widened(4))  # parts 1 and 2 full
    p = await kit.service.propose_from_scan(scan_id, "shop", principal=AGENT, client=None)
    update = UpdateConcept(op="update_concept", fqn="Order", base_version="0.4", synonyms=["B"])
    p = await kit.service.propose_change(p.id, [update], principal=AGENT, client=None)
    assert len(p.parts) == 3  # the update is part 3
    await kit.service.submit(p.id, principal=AGENT, client=None)
    third = p.parts[2].change_set_ids["glossary"]
    kit.om.terms["shop"] = [shop_term("Order", 0.6)]
    created = 0
    real = kit.om.create_change_set

    async def flaky(**kw: Any):
        nonlocal created
        created += 1
        if created == 2:  # part 1, native side
            raise OMUnavailable("down")
        return await real(**kw)

    kit.om.create_change_set = flaky  # type: ignore[method-assign]
    with pytest.raises(LakeloomError):
        await kit.service.rebase(p.id, principal=AGENT, client=None)
    stored = await kit.repo.get(p.id)
    assert stored.status == "draft" and stored.rebase_pending
    # part 3 was never rebuilt: its stored operation still matches its change set in OM
    untouched = next(o for o in stored.parts[2].operations if isinstance(o, UpdateConcept))
    assert untouched.base_version == "0.4"
    assert stored.parts[2].change_set_ids["glossary"] == third
    assert not any("refreshed" in w for w in stored.warnings)
    assert _recorded(stored) == set(kit.om.change_sets)

    # submit resumes the whole rebase (every part not fully applied), then submits
    before = len(kit.om.calls)
    done = await kit.service.submit(p.id, principal=AGENT, client=None)
    assert done.status == "submitted" and not done.rebase_pending
    assert third in done.parts[2].superseded_change_set_ids
    refreshed = next(o for o in done.parts[2].operations if isinstance(o, UpdateConcept))
    assert refreshed.base_version == "0.6"
    new = [kw for n, kw in kit.om.calls[before:] if n == "create_change_set"]
    update_op = next(
        o for kw in new for o in kw["operations"] if o["operationType"] == "UPDATE_TERM"
    )
    assert update_op["baseVersion"] == 0.6
    assert {s for part in done.parts for s in part.om_state.values() if s} == {"SUBMITTED"}
    assert _recorded(done) == set(kit.om.change_sets)


async def test_rebuild_partly_applied_part_with_remove_concept(make) -> None:
    kit = make()
    legacy_iri = "http://x/ontology/shop/Legacy"
    kit.om.terms["shop"] = [shop_term("Legacy", 0.3)]
    kit.om.native["shop"] = (
        [{"id": "concept-legacy", "iri": legacy_iri, "name": "Legacy", "version": 1.0}],
        [],
        [],
    )
    p = await _shop(kit)
    drop = RemoveConcept(op="remove_concept", fqn="Legacy", base_version="0.3")
    p = await kit.service.propose_change(p.id, [drop], principal=AGENT, client=None)
    await kit.service.submit(p.id, principal=AGENT, client=None)
    glossary, native = p.change_set_ids()
    _apply(kit.om, glossary)  # the term is deleted; the native class is not
    kit.om.terms["shop"] = [
        OMGlossaryTerm.model_validate(
            {
                "id": draft_term_id(next(o for o in p.operations() if isinstance(o, AddConcept))),
                "name": "Customer",
                "fullyQualifiedName": "shop.Customer",
                "description": "applied",
                "version": 0.1,
                "iri": "http://x/ontology/shop/Customer",
                "glossary": {"id": "id-shop", "type": "glossary"},
            }
        )
    ]
    await _store_states(kit, p)
    await _set_status(kit, p, "apply_failed")
    before = len(kit.om.calls)

    out = await kit.service.rebase(p.id, principal=AGENT, client=None)

    created = [kw for n, kw in kit.om.calls[before:] if n == "create_change_set"]
    assert [kw["name"] for kw in created] == [f"lakeloom-{p.id}-1-native-r1"]
    delete = next(o for o in created[0]["operations"] if o["operationType"] == "DELETE_CONCEPT")
    assert delete["targetId"] == "concept-legacy"
    assert out.parts[0].change_set_ids["glossary"] == glossary
    assert out.parts[0].superseded_change_set_ids == [native]
