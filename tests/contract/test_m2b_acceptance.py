"""M2b acceptance against the live stack: human approval with OM identity (FR-REV-01, FR-REV-02).

Everything goes through real /mcp with Keycloak tokens. The Lakeloom container exchanges the
reviewer's token for an OpenMetadata one (client ``lakeloom``, ADR 0012); the bot never
approves (NFR-SEC-10). The tests share one throwaway model ``retail_m2b_<hex>`` (unique per
run) and run in file order:

1. the agent bootstraps the model from fixture_delta and submits; approving with the agent's
   default tokens or bob's (no ``lakeloom:review``) is refused by the scope gate; an agent
   token that requested the realm's optional ``lakeloom:review`` passes it and is refused by
   the review service ("agent principals cannot review proposals", audited with the
   proposal) for every decision; nothing is applied;
2. carol has ``lakeloom:review`` but no OM reviewer role: refused by OM's permission;
3. alice previews and confirms: every change set is APPLIED with ``appliedBy == "alice"``, the
   terms are ``updatedBy`` alice, the native classes and attributes exist; the mapper proposes
   no realizations, so the agent proposes ``Order`` on ``orders`` and alice approves it: the
   table carries the tag; explain_concept shows the concept, its attributes and the table;
4. a second proposal: request_changes → the agent's propose_change re-drafts it → resubmit →
   reject with a reason discards its change sets;
5. two proposals touch the term ``Customer`` (a synonym, and an ``update_concept`` with the
   agent's base version): the first is applied, approving the second is a ``conflict`` with
   the rebase hint; rebase_proposal → submit → alice approves → applied. (Two open
   ``update_concept`` of one term are impossible: the second is deduped as already proposed.)
6. a re-scan proposes no concept, attribute or relation again, only the column realizations
   of the applied concepts (R-DEDUPE); once alice approves those, a third scan has nothing to
   propose; the audit log has a human-principal row for every decision.

Teardown (module finalizer, runs on failure too) discards open proposals, hard-deletes every
change set, the ontology and the glossary (recursive), checks they are gone, that no table
keeps a tag of the throwaway glossary; then (even when those checks fail) waits until
Lakeloom's caches no longer name the deleted model (TTL 60 s in dev) and checks that the
seeded ``retail`` glossary and ontology are untouched.
No release is sealed. Tokens are never printed or put in assertion messages (NFR-SEC-11).
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mcp.types import CallToolResult
from pydantic import SecretStr
from stack import TOKEN_URL, mcp_session, token_scopes

from lakeloom.core.audit import AuditRecord
from lakeloom.core.proposals import REBASE_HINT
from lakeloom.core.review import AGENT_REFUSED
from lakeloom.om.client import OMClient
from lakeloom.state.audit import AuditRepository
from lakeloom.state.db import create_engine

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

TABLES = ("customers", "products", "orders", "invoices")
OM_TABLE = "lakeloom_fixture.retail.sales.{}"
REVIEWER = "alice"
CACHE_WAIT_SECONDS = 90  # > the dev stack's model_cache_ttl_seconds (60)


@dataclass
class Run:
    """State shared by the ordered tests of this module."""

    url: str
    agent: str = field(repr=False)  # tokens: kept out of tracebacks (NFR-SEC-11)
    om: httpx.AsyncClient
    bot_token: str = field(repr=False)  # read-only checks and teardown, never an approval
    slug: str
    started: datetime
    proposals: list[str] = field(default_factory=list[str])
    change_sets: set[str] = field(default_factory=set[str])
    decisions: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    main: str | None = None


class Token(str):
    """A bearer token whose repr is redacted: pytest prints frame arguments on failure."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted token>"


@dataclass(frozen=True)
class Tokens:
    """The callers' tokens, with no repr: pytest prints test arguments on failure."""

    admin: str = field(repr=False)
    alice: str = field(repr=False)
    bob: str = field(repr=False)
    carol: str = field(repr=False)
    agent_review: str = field(repr=False)  # lakeloom-agent WITH the optional lakeloom:review


@dataclass(frozen=True)
class Secrets:
    """The run fixture's credentials, wrapped before anything in it can fail (NFR-SEC-11)."""

    agent: str = field(repr=False)
    bot: str = field(repr=False)


def _agent_review_token() -> Token:
    """lakeloom-agent's client_credentials token requesting the realm's optional
    ``lakeloom:review`` scope: an agent that passes the scope gate (FR-REV-01)."""
    r = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": "lakeloom-agent",
            "client_secret": "dev-agent-secret",  # DEV ONLY fixed public dev credential
            "scope": "lakeloom:review",
        },
        timeout=10,
    )
    assert r.status_code == 200, f"token request failed: {r.status_code}"  # no body: a token
    return Token(str(r.json()["access_token"]))


# These fixtures only wrap (nothing in them can fail), so no frame holding a raw token fails.
@pytest.fixture(scope="module")
def who(admin_agent_token: str, human_token: str, bob_token: str, carol_token: str) -> Tokens:
    raw = (admin_agent_token, human_token, bob_token, carol_token)
    return Tokens(*(Token(t) for t in raw), agent_review=_agent_review_token())


@pytest.fixture(scope="module")
def secrets(agent_token: str, om_bot_token: str) -> Secrets:
    return Secrets(Token(agent_token), Token(om_bot_token))


async def call(url: str, token: str, tool: str, args: dict[str, Any]) -> CallToolResult:
    async with mcp_session(url, token) as session:
        return await session.call_tool(tool, args)


def data(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result.content
    return dict(result.structured_content)


def ok(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is not True, json.dumps(result.structured_content)[:3000]
    return data(result)


def error(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is True, json.dumps(result.structured_content)[:3000]
    return dict(data(result)["error"])


def _change_sets(proposal: dict[str, Any]) -> set[str]:
    return {
        cs for part in proposal["parts"] for cs in part["change_set_ids"].values() if cs is not None
    }


def track(run: Run, out: dict[str, Any]) -> dict[str, Any]:
    """Record a proposal result's id and change sets for the teardown; returns ``out``."""
    pid = str(out["proposal_id"])
    if pid not in run.proposals:
        run.proposals.append(pid)
    run.change_sets |= _change_sets(out["proposal"])
    return out


async def _change_set(om: httpx.AsyncClient, cs_id: str) -> dict[str, Any]:
    return dict((await om.get(f"/api/v1/ontologyChangeSets/{cs_id}")).raise_for_status().json())


async def _term(om: httpx.AsyncClient, fqn: str) -> dict[str, Any] | None:
    r = await om.get(f"/api/v1/glossaryTerms/name/{fqn}", params={"fields": "synonyms"})
    return None if r.status_code == 404 else dict(r.raise_for_status().json())


async def _table_tags(om: httpx.AsyncClient, table: str) -> set[str]:
    r = await om.get(
        f"/api/v1/tables/name/{OM_TABLE.format(table)}", params={"fields": "tags,columns"}
    )
    body = r.raise_for_status().json()
    tags = {t["tagFQN"] for t in body.get("tags") or []}
    for column in body.get("columns") or []:
        tags |= {t["tagFQN"] for t in column.get("tags") or []}
    return tags


async def _table_tag(om: httpx.AsyncClient, table: str, tag: str) -> dict[str, Any]:
    """The table-level tag label ``tag`` of a fixture table."""
    r = await om.get(f"/api/v1/tables/name/{OM_TABLE.format(table)}", params={"fields": "tags"})
    labels = [t for t in r.raise_for_status().json().get("tags") or [] if t["tagFQN"] == tag]
    assert len(labels) == 1, (table, tag)
    return dict(labels[0])


async def _review(
    run: Run, token: str, pid: str, decision: str, reason: str | None = None
) -> dict[str, Any]:
    """Preview, then confirm, as ``token`` (``request_changes`` is immediate)."""
    args: dict[str, Any] = {"proposal_id": pid, "decision": decision}
    if reason is not None:
        args["reason"] = reason
    out = ok(await call(run.url, token, "review_proposal", args))
    if decision != "request_changes":
        assert out["decided"] is False and out["confirm_token"]
        args["confirm_token"] = out["confirm_token"]
        out = ok(await call(run.url, token, "review_proposal", args))
    assert out["decided"] is True and out["decision"] == decision
    run.decisions.append((pid, decision))
    return out


async def _assert_applied_by_alice(run: Run, cs_ids: set[str]) -> None:
    assert cs_ids
    for cs_id in cs_ids:
        cs = await _change_set(run.om, cs_id)
        assert cs["state"] == "APPLIED", (cs_id, cs["state"])
        assert cs["applicationResult"]["appliedBy"] == REVIEWER, cs_id


async def _new_proposal(run: Run, concept: str, table: str, column: str | None) -> str:
    """A new agent proposal of ``run.slug``, started with a realization."""
    args: dict[str, Any] = {"concept": concept, "asset": OM_TABLE.format(table), "model": run.slug}
    if column is not None:
        args["column"] = column
    out = track(run, ok(await call(run.url, run.agent, "propose_realization", args)))
    assert out["status"] == "draft"
    return str(out["proposal_id"])


async def _teardown(run: Run) -> None:
    for pid in run.proposals:
        preview = await call(run.url, run.agent, "discard_proposal", {"proposal_id": pid})
        if preview.is_error:
            continue  # applied, rejected or already discarded
        args = {"proposal_id": pid, "confirm_token": data(preview)["confirm_token"]}
        await call(run.url, run.agent, "discard_proposal", args)
    for pid in run.proposals:  # every change set the proposal ever had, superseded ones too
        shown = await call(run.url, run.agent, "get_proposal", {"proposal_id": pid})
        if not shown.is_error:
            proposal = data(shown)["proposal"]
            run.change_sets |= _change_sets(proposal)
            for part in proposal["parts"]:
                run.change_sets |= {
                    s for s in part.get("superseded_change_set_ids", []) if isinstance(s, str)
                }
    om = run.om
    for cs_id in run.change_sets:
        await om.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
    for kind in ("ontologies", "glossaries"):
        await om.delete(
            f"/api/v1/{kind}/name/{run.slug}", params={"hardDelete": "true", "recursive": "true"}
        )
    for path in (
        f"/api/v1/ontologies/name/{run.slug}",
        f"/api/v1/glossaries/name/{run.slug}",
        *(f"/api/v1/ontologyChangeSets/{c}" for c in run.change_sets),
    ):
        assert (await om.get(path, params={"include": "all"})).status_code == 404, path
    for table in TABLES:
        left = {t for t in await _table_tags(om, table) if t.startswith(f"{run.slug}.")}
        assert not left, (table, left)


async def _wait_for_lakeloom_caches(run: Run) -> None:
    """Lakeloom caches OM tables and models for ``model_cache_ttl_seconds`` (60 in dev); a
    model deleted behind its back makes the asset context of the tables that realized it
    ``not_found`` until then. Wait it out so the next contract module sees a clean stack."""
    deadline = asyncio.get_running_loop().time() + CACHE_WAIT_SECONDS
    async with httpx.AsyncClient(base_url=run.url, headers=_bearer(run.agent), timeout=30) as api:
        for table in TABLES:
            while True:
                r = await api.get(f"/v1/assets/{OM_TABLE.format(table)}/context")
                if r.status_code == 200 and run.slug not in r.text:
                    break
                assert asyncio.get_running_loop().time() < deadline, (table, r.status_code)
                await asyncio.sleep(2)


def _bearer(token: str) -> httpx.Headers:
    """Headers kept in no named local: ``pytest -l`` would print a dict holding the token."""
    return httpx.Headers({"Authorization": f"Bearer {token}"})


async def _retail(om: httpx.AsyncClient) -> list[tuple[Any, Any]]:
    """Version and updatedAt of the seeded ``retail`` glossary and ontology."""
    out: list[tuple[Any, Any]] = []
    for kind in ("glossaries", "ontologies"):
        body = (await om.get(f"/api/v1/{kind}/name/retail")).raise_for_status().json()
        out.append((body["version"], body["updatedAt"]))
    return out


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def run(lakeloom_url: str, om_url: str, secrets: Secrets) -> AsyncIterator[Run]:
    # the bot: read-only checks and the teardown only
    async with httpx.AsyncClient(base_url=om_url, headers=_bearer(secrets.bot), timeout=60) as om:
        retail = await _retail(om)
        state = Run(
            url=lakeloom_url,
            agent=secrets.agent,
            om=om,
            bot_token=secrets.bot,
            slug=f"retail_m2b_{uuid.uuid4().hex[:8]}",
            started=datetime.now(UTC) - timedelta(seconds=2),  # audit rows of this run
        )
        try:
            yield state
        finally:
            try:
                await _teardown(state)
            finally:
                try:
                    await _wait_for_lakeloom_caches(state)
                finally:
                    assert await _retail(om) == retail, "the seeded retail model changed"


def _main(run: Run) -> str:
    if run.main is None:
        pytest.skip("the bootstrap proposal of test_m2b_agent_cannot_approve is missing")
    return run.main


async def test_m2b_agent_cannot_approve(run: Run, who: Tokens) -> None:
    info = data(await call(run.url, who.alice, "get_server_info", {}))
    assert info["review_identity"] == "exchange"

    scanned = ok(await call(run.url, run.agent, "scan_lake_source", {"source": "fixture_delta"}))
    assert scanned["status"] == "completed"
    args = {"name": run.slug, "description": "M2b acceptance throwaway model"}
    preview = ok(await call(run.url, who.admin, "create_model", args))
    confirmed = {**args, "confirm_token": preview["confirm_token"]}
    assert ok(await call(run.url, who.admin, "create_model", confirmed))["created"]

    proposed = track(
        run,
        ok(
            await call(
                run.url,
                run.agent,
                "propose_from_scan",
                {"scan_id": scanned["scan_id"], "model": run.slug},
            )
        ),
    )
    pid = str(proposed["proposal_id"])
    submitted = ok(await call(run.url, run.agent, "submit_proposal", {"proposal_id": pid}))
    assert submitted["status"] == "submitted"
    run.main = pid

    approve = {"proposal_id": pid, "decision": "approve"}
    no_scope = "review_proposal requires the lakeloom:review scope"
    for token in (run.agent, who.admin, who.bob):  # refused by the scope gate (guarded)
        refused = error(await call(run.url, token, "review_proposal", approve))
        assert (refused["code"], refused["message"]) == ("forbidden", no_scope), refused
    # an agent holding lakeloom:review passes the gate and is refused by the review service
    assert "lakeloom:review" in token_scopes(who.agent_review)
    for decision in ("approve", "reject", "request_changes"):
        args = {"proposal_id": pid, "decision": decision, "reason": "agent decision"}
        refused = error(await call(run.url, who.agent_review, "review_proposal", args))
        assert (refused["code"], refused["message"]) == ("forbidden", AGENT_REFUSED), refused
    refused = error(await call(run.url, who.bob, "list_pending_reviews", {}))
    assert refused["code"] == "forbidden", refused

    for cs_id in _change_sets(proposed["proposal"]):
        assert (await _change_set(run.om, cs_id))["state"] == "SUBMITTED"
    assert await _term(run.om, f"{run.slug}.Customer") is None
    shown = ok(await call(run.url, run.agent, "get_proposal", {"proposal_id": pid}))
    assert shown["status"] == "submitted"


async def test_m2b_carol_refused_by_om_permission(run: Run, who: Tokens) -> None:
    pid = _main(run)
    approve = {"proposal_id": pid, "decision": "approve"}
    refused = error(await call(run.url, who.carol, "review_proposal", approve))
    assert refused["code"] == "forbidden", refused
    assert "OpenMetadata does not grant" in refused["message"], refused
    shown = ok(await call(run.url, run.agent, "get_proposal", {"proposal_id": pid}))
    assert shown["status"] == "submitted"
    assert await _term(run.om, f"{run.slug}.Customer") is None


async def test_m2b_alice_approves_and_om_records_alice(run: Run, who: Tokens) -> None:
    pid = _main(run)
    pending = ok(await call(run.url, who.alice, "list_pending_reviews", {"model": run.slug}))
    listed = {
        e["id"] for m in pending["models"] for dp in m["data_products"] for e in dp["proposals"]
    }
    assert pid in listed

    args = {"proposal_id": pid, "decision": "approve"}
    preview = ok(await call(run.url, who.alice, "review_proposal", args))
    assert preview["decided"] is False and preview["confirm_token"] and preview["diff"]
    assert (await _term(run.om, f"{run.slug}.Customer")) is None  # dry run: nothing applied
    args["confirm_token"] = preview["confirm_token"]
    out = ok(await call(run.url, who.alice, "review_proposal", args))
    assert out["decided"] is True and out["proposal"]["status"] == "applied"
    run.decisions.append((pid, "approve"))
    replay = error(await call(run.url, who.alice, "review_proposal", args))
    assert replay["code"] == "conflict", replay

    proposal = track(run, {"proposal_id": pid, "proposal": out["proposal"]})["proposal"]
    await _assert_applied_by_alice(run, _change_sets(proposal))
    concepts = {
        op["name"]
        for part in proposal["parts"]
        for op in part["operations"]
        if op["op"] == "add_concept"
    }
    assert {"Customer", "Order"} <= concepts
    for name in concepts:
        term = await _term(run.om, f"{run.slug}.{name}")
        assert term is not None, name
        assert term["updatedBy"] == REVIEWER, name

    om = OMClient(str(run.om.base_url), SecretStr(run.bot_token), timeout=60)
    try:
        classes, properties, _ = await om.list_native_members(run.slug)
    finally:
        await om.aclose()
    class_names = {str(c.get("name")) for c in classes}
    assert concepts <= class_names, class_names
    attributes = [
        op for part in proposal["parts"] for op in part["operations"] if op["op"] == "add_attribute"
    ]
    assert attributes
    property_names = {str(p.get("name")) for p in properties}
    expected = {f"{a['concept'].rsplit('.', 1)[-1]}.{a['name']}" for a in attributes}
    assert expected <= property_names, sorted(expected - property_names)

    # The mapper proposes no realizations: the agent asks for one on the new concept.
    assert f"{run.slug}.Order" not in await _table_tags(run.om, "orders")
    realized = await _new_proposal(run, f"{run.slug}.Order", "orders", None)
    ok(await call(run.url, run.agent, "submit_proposal", {"proposal_id": realized}))
    tagged = await _review(run, who.alice, realized, "approve")
    assert tagged["proposal"]["status"] == "applied"
    assert tagged["proposal"]["realizations_applied"]
    assert f"{run.slug}.Order" in await _table_tags(run.om, "orders")
    label = await _table_tag(run.om, "orders", f"{run.slug}.Order")
    assert label.get("appliedBy") == REVIEWER, label.get("appliedBy")

    explained = ok(
        await call(run.url, who.alice, "explain_concept", {"concept": f"{run.slug}.Order"})
    )
    assert explained["concept"]["fqn"] == f"{run.slug}.Order"
    order_attrs = {a["name"] for a in attributes if a["concept"].endswith("Order")}
    assert order_attrs and order_attrs <= {a["name"] for a in explained["attributes"]}
    realizing = {a["fqn"] for a in explained["realizations"]["items"]}
    assert OM_TABLE.format("orders") in realizing, realizing


async def test_m2b_reject_and_request_changes(run: Run, who: Tokens) -> None:
    _main(run)
    pid = await _new_proposal(run, "Customer", "orders", "customer_id")
    synonym = {"op": "add_synonym", "concept_fqn": f"{run.slug}.Order", "synonym": "Purchase"}
    out = track(
        run,
        ok(
            await call(
                run.url, run.agent, "propose_change", {"proposal_id": pid, "operations": [synonym]}
            )
        ),
    )
    first = _change_sets(out["proposal"])
    assert first
    ok(await call(run.url, run.agent, "submit_proposal", {"proposal_id": pid}))

    reason = "Please also give Customer the synonym 'Shopper'"
    changed = await _review(run, who.alice, pid, "request_changes", reason)
    assert changed["proposal"]["status"] == "changes_requested"

    # One proposal changes an existing concept once: the follow-up edit is another term.
    sale = {"op": "add_synonym", "concept_fqn": f"{run.slug}.Customer", "synonym": "Shopper"}
    redrafted = track(
        run,
        ok(
            await call(
                run.url, run.agent, "propose_change", {"proposal_id": pid, "operations": [sale]}
            )
        ),
    )
    assert redrafted["status"] == "draft"
    for cs_id in _change_sets(redrafted["proposal"]):
        assert (await _change_set(run.om, cs_id))["state"] == "DRAFT"
    resubmitted = ok(await call(run.url, run.agent, "submit_proposal", {"proposal_id": pid}))
    assert resubmitted["status"] == "submitted"
    final = _change_sets(resubmitted["proposal"])

    rejected = await _review(run, who.alice, pid, "reject", "Synonyms are out of scope")
    assert rejected["proposal"]["status"] == "rejected"
    for cs_id in final | first:
        assert (await _change_set(run.om, cs_id))["state"] == "DISCARDED", cs_id
    for name, rejected_synonym in (("Order", "Purchase"), ("Customer", "Shopper")):
        term = await _term(run.om, f"{run.slug}.{name}")
        assert term is not None and rejected_synonym not in (term.get("synonyms") or [])


async def test_m2b_stale_then_rebase(run: Run, who: Tokens) -> None:
    _main(run)
    fqn = f"{run.slug}.Customer"
    term = await _term(run.om, fqn)
    assert term is not None

    first = await _new_proposal(run, "Order", "invoices", "order_id")
    synonym = {"op": "add_synonym", "concept_fqn": fqn, "synonym": "Client"}
    track(
        run,
        ok(
            await call(
                run.url,
                run.agent,
                "propose_change",
                {"proposal_id": first, "operations": [synonym]},
            )
        ),
    )
    second = await _new_proposal(run, "Customer", "customers", None)
    description = "A person or company that buys from the retailer (M2b acceptance)."
    update = {
        "op": "update_concept",
        "fqn": fqn,
        "base_version": str(term["version"]),
        "description": {"text": description, "source": "agent"},
    }
    track(
        run,
        ok(
            await call(
                run.url,
                run.agent,
                "propose_change",
                {"proposal_id": second, "operations": [update]},
            )
        ),
    )
    for pid in (first, second):
        ok(await call(run.url, run.agent, "submit_proposal", {"proposal_id": pid}))

    applied = await _review(run, who.alice, first, "approve")
    assert applied["proposal"]["status"] == "applied"
    await _assert_applied_by_alice(run, _change_sets(applied["proposal"]))
    after = await _term(run.om, fqn)
    assert after is not None and "Client" in (after.get("synonyms") or [])
    assert after["version"] != term["version"]

    args = {"proposal_id": second, "decision": "approve"}
    preview = ok(await call(run.url, who.alice, "review_proposal", args))
    args["confirm_token"] = preview["confirm_token"]
    stale = error(await call(run.url, who.alice, "review_proposal", args))
    assert stale["code"] == "conflict", stale
    assert stale["hint"] == REBASE_HINT, stale

    rebased = track(
        run, ok(await call(run.url, run.agent, "rebase_proposal", {"proposal_id": second}))
    )
    assert rebased["status"] == "draft"
    assert rebased["warnings"], rebased  # the agent's base version was refreshed
    ok(await call(run.url, run.agent, "submit_proposal", {"proposal_id": second}))
    done = await _review(run, who.alice, second, "approve")
    assert done["proposal"]["status"] == "applied"
    await _assert_applied_by_alice(run, _change_sets(done["proposal"]))
    final = await _term(run.om, fqn)
    assert final is not None and final["description"] == description
    assert final["updatedBy"] == REVIEWER


async def _propose_from_rescan(run: Run) -> CallToolResult:
    scanned = ok(await call(run.url, run.agent, "scan_lake_source", {"source": "fixture_delta"}))
    args = {"scan_id": scanned["scan_id"], "model": run.slug}
    return await call(run.url, run.agent, "propose_from_scan", args)


async def test_m2b_rescan_no_duplicates_after_apply(run: Run, who: Tokens) -> None:
    """Once the concepts exist, a re-scan proposes none of them again (FR-PRO-02): only the
    column realizations of the applied concepts (dedupe rule R-DEDUPE), none of the
    realizations already applied; after those are approved, a third scan has nothing new."""
    _main(run)
    rerun = await _propose_from_rescan(run)
    out = track(run, ok(rerun))
    ops = [op for part in out["proposal"]["parts"] for op in part["operations"]]
    assert ops and {op["op"] for op in ops} == {"add_realization"}, {op["op"] for op in ops}
    assert all(op["concept"].startswith(f"{run.slug}.") for op in ops)
    assert all("R-DEDUPE" in op["rule_ids"] for op in ops)
    proposed = {(op["concept"].rsplit(".", 1)[-1], op["asset_fqn"], op["column"]) for op in ops}
    already = {  # proposed by the earlier tests: applied, except orders.customer_id (rejected)
        ("Order", OM_TABLE.format("orders"), None),
        ("Customer", OM_TABLE.format("customers"), None),
        ("Order", OM_TABLE.format("invoices"), "order_id"),
        ("Customer", OM_TABLE.format("orders"), "customer_id"),
    }
    assert not proposed & already, proposed & already
    assert not _change_sets(out["proposal"])  # realizations are Lakeloom-side until approval
    pid = str(out["proposal_id"])
    ok(await call(run.url, run.agent, "submit_proposal", {"proposal_id": pid}))
    applied = await _review(run, who.alice, pid, "approve")
    assert applied["proposal"]["status"] == "applied"
    assert len(applied["proposal"]["realizations_applied"]) == len(ops)

    again = await _propose_from_rescan(run)
    if again.is_error is not True:  # unexpected: record it so the teardown removes it
        track(run, data(again))
        pytest.fail("re-scan proposed again: " + json.dumps(data(again))[:2000])
    refused = data(again)
    assert refused["error"]["code"] == "validation_error"
    assert refused["error"]["message"].startswith("nothing to propose"), refused
    assert refused["matches"]


async def test_m2b_audit_has_human_rows_for_every_decision(run: Run) -> None:
    _main(run)
    assert run.decisions
    engine = create_engine(os.environ["LAKELOOM_TEST_DB_URL"])
    try:
        repo = AuditRepository(engine)
        rows: list[AuditRecord] = []
        cursor: str | None = None
        while True:
            page = await repo.list(run.started, cursor, 500)
            rows += page.items
            cursor = page.next_cursor
            if cursor is None:
                break
    finally:
        await engine.dispose()
    mine = set(run.proposals)
    reviews = [r for r in rows if r.tool == "review_proposal" and r.proposal_id in mine]
    for pid, decision in run.decisions:
        decided = [r for r in reviews if r.proposal_id == pid and r.result == "ok"]
        assert decided, (pid, decision)
        assert all(r.principal_type == "human" for r in decided), (pid, decision)
    ok_rows = [r for r in reviews if r.result == "ok"]
    assert all(r.principal_type == "human" for r in ok_rows)
    # in the service, with the proposal: the agent holding lakeloom:review (approve, reject,
    # request_changes; FR-REV-01), then carol (OM's permission)
    in_service = [r for r in reviews if r.proposal_id == run.main and r.result == "forbidden"]
    assert [r.principal_type for r in in_service] == ["agent", "agent", "agent", "human"]
    # the agent is refused before the proposal is read (no model yet); carol after it
    assert [r.model for r in in_service] == [None, None, None, run.slug]
    # the agents and bob: refused by the scope check, before any proposal is read
    by_scope = [
        r.principal_type
        for r in rows
        if r.tool == "review_proposal" and r.result == "forbidden" and r.proposal_id is None
    ]
    assert sorted(by_scope) == ["agent", "agent", "human"], by_scope
