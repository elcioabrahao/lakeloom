"""Review MCP tools (FR-REV-01, FR-REV-05, FR-AGT-02, FR-AGT-06, NFR-SEC-12).

``review_proposal`` and ``rebase_proposal`` run against recording fakes (the services have their
own tests); ``list_pending_reviews`` runs against the real service over a SQLite store.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kit import AuthKit, DepsFactory, mcp_session
from mcp.types import CallToolResult

from lakeloom.core.audit import current_audit
from lakeloom.core.model_policy import ModelAccess, ModelPolicyEntry
from lakeloom.core.operations import AddConcept
from lakeloom.core.proposals import (
    ConfirmPreview,
    Proposal,
    ProposalPart,
    Provenance,
    TableProvenance,
)
from lakeloom.errors import LakeloomError
from lakeloom.server.http_app import AppDeps
from lakeloom.server.mcp_app import create_mcp, create_server_app
from lakeloom.state.db import upgrade
from lakeloom.state.ids import new_ulid_id
from lakeloom.state.proposals import ProposalRepository

REVIEW = "lakeloom:review"
PROPOSE = "lakeloom:propose"
NOW = datetime.now(UTC)
TOKEN = "tok"  # noqa: S105 - the fake service's confirm token


def make_proposal(
    pid: str,
    *,
    model: str = "shop",
    status: str = "submitted",
    subject: str = "agent-1",
    tables: tuple[str, ...] = ("retail.sales.orders",),
    age_s: int = 3600,
    ops: int = 2,
) -> Proposal:
    at = NOW - timedelta(seconds=age_s)
    return Proposal.model_validate(
        {
            "id": pid,
            "model": model,
            "status": status,
            "parts": [
                ProposalPart(
                    n=1,
                    tables=list(tables),
                    operations=[
                        AddConcept(op="add_concept", name=f"C{i}", rule_ids=["R-TABLE"])
                        for i in range(ops)
                    ],
                    change_set_ids={"glossary": "cs-g", "native": "cs-n"},
                )
            ],
            "provenance": Provenance(
                scan_id="s",
                tables=[TableProvenance(fqn=t) for t in tables],
                principal_subject=subject,
                principal_type="agent",
                at=at,
            ),
            "created_at": at,
            "updated_at": at,
        }
    )


class FakeReview:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.p = make_proposal("prop_1")

    async def review(self, proposal_id: str, decision: str, **kw: Any) -> Any:
        self.calls.append({"proposal_id": proposal_id, "decision": decision, **kw})
        trail = current_audit()
        trail.model, trail.proposal_id = self.p.model, proposal_id
        principal = kw["principal"]
        if principal.type.value == "agent":
            raise LakeloomError(
                "forbidden", "agent principals cannot review proposals", "ask human"
            )
        token = kw.get("confirm_token")
        if decision != "request_changes" and token is None:
            return ConfirmPreview(summary="Approving prop_1", confirm_token=TOKEN, expires_at=9)
        status = {"approve": "applied", "reject": "rejected"}.get(decision, "changes_requested")
        return self.p.model_copy(update={"status": status})


class FakeRebase:
    def __init__(self, deps: AppDeps) -> None:
        self.real = deps.proposals
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def rebase(self, proposal_id: str, **kw: Any) -> Proposal:
        self.calls.append((proposal_id, kw))
        current_audit().proposal_id = proposal_id
        p = make_proposal(proposal_id, status="draft")
        return p.model_copy(update={"warnings": ["base version of Customer refreshed"]})

    def summary_markdown(self, p: Proposal) -> str:
        return self.real.summary_markdown(p)


def structured(r: CallToolResult) -> dict[str, Any]:
    assert r.structured_content is not None, r
    return dict(r.structured_content)


def text(r: CallToolResult) -> str:
    block = r.content[0]
    assert block.type == "text"
    return block.text


async def rows(deps: AppDeps) -> list[Any]:
    return list((await deps.audit.list(None, None, 100)).items)


def with_policies(deps: AppDeps, policies: list[ModelPolicyEntry]) -> AppDeps:
    return AppDeps(
        settings=deps.settings,
        om=deps.om,
        engine=deps.engine,
        validator=deps.validator,
        idp_http=deps.idp_http,
        policies=policies,
    )


async def test_review_tools_annotations_and_scopes(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(make_deps()).list_tools()}
    expected = {
        "review_proposal": (REVIEW, (False, True, False)),
        "rebase_proposal": (PROPOSE, (False, False, False)),
        "list_pending_reviews": (REVIEW, (True, False, True)),
    }
    for name, (scope, flags) in expected.items():
        t = tools[name]
        assert t.annotations is not None
        a = t.annotations
        assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint) == flags, name
        assert f"Requires scope `{scope}`" in (t.description or ""), name
        assert t.output_schema is not None
        for prop, schema in t.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{name}.{prop}"
    desc = tools["review_proposal"].description or ""
    assert "human principals only" in desc.lower()
    assert "their own sign-in" in desc
    assert "confirm_token" in desc and "Side effect" in desc
    assert "Side effect" in (tools["rebase_proposal"].description or "")
    enum = tools["review_proposal"].input_schema["properties"]["decision"]["enum"]
    assert enum == ["approve", "reject", "request_changes"]


async def test_review_tool_agent_refused(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = make_deps()
    deps.review = FakeReview()  # pyright: ignore[reportAttributeAccessIssue]
    await upgrade(deps.engine)
    token = kit.token(scope=REVIEW, client_id="agent-x")
    async with mcp_session(create_server_app(deps), token) as s:
        r = await s.call_tool("review_proposal", {"proposal_id": "prop_1", "decision": "approve"})
    assert structured(r)["error"]["code"] == "forbidden"
    (row,) = await rows(deps)
    assert (row.tool, row.result, row.error_code) == ("review_proposal", "forbidden", "forbidden")


async def test_review_preview_then_confirm(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = make_deps()
    fake = FakeReview()
    deps.review = fake  # pyright: ignore[reportAttributeAccessIssue]
    await upgrade(deps.engine)
    async with mcp_session(create_server_app(deps), kit.token(scope=REVIEW)) as s:
        preview = await s.call_tool(
            "review_proposal", {"proposal_id": "prop_1", "decision": "approve"}
        )
        done = await s.call_tool(
            "review_proposal",
            {"proposal_id": "prop_1", "decision": "approve", "confirm_token": TOKEN},
        )
        changes = await s.call_tool(
            "review_proposal",
            {"proposal_id": "prop_1", "decision": "request_changes", "reason": "rename"},
        )
    p = structured(preview)
    assert (p["decided"], p["confirm_token"], p["expires_at"]) == (False, TOKEN, 9)
    assert "confirm_token" in text(preview) and "human" in text(preview)
    d = structured(done)
    assert d["decided"] is True and d["proposal"]["status"] == "applied"
    assert d["confirm_token"] is None
    assert structured(changes)["proposal"]["status"] == "changes_requested"
    assert [c["confirm_token"] for c in fake.calls] == [None, TOKEN, None]
    assert fake.calls[2]["reason"] == "rename"
    assert fake.calls[0]["principal"].subject == "user-123"
    got = await rows(deps)
    assert sorted(r.tool for r in got) == ["review_proposal"] * 3  # one row per call


async def test_rebase_tool(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = make_deps()
    fake = FakeRebase(deps)
    deps.proposals = fake  # pyright: ignore[reportAttributeAccessIssue]
    await upgrade(deps.engine)
    async with mcp_session(create_server_app(deps), kit.token(scope=PROPOSE), client_name="c") as s:
        r = await s.call_tool("rebase_proposal", {"proposal_id": "prop_1"})
    assert structured(r)["status"] == "draft"
    assert "refreshed" in text(r) and "submit_proposal" in text(r)
    assert fake.calls[0][1]["client"] == "c"
    (row,) = await rows(deps)
    assert (row.tool, row.result, row.proposal_id) == ("rebase_proposal", "ok", "prop_1")


async def seed(deps: AppDeps, *proposals: Proposal) -> None:
    await upgrade(deps.engine)
    repo = ProposalRepository(deps.engine)
    for p in proposals:
        await repo.add(p)


async def test_list_pending_groups_by_model_and_data_product_and_excludes_own(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    deps = make_deps()
    await seed(
        deps,
        make_proposal("prop_a", tables=("retail.sales.orders", "retail.sales.items")),
        make_proposal("prop_b", tables=("retail.crm.customers",), status="apply_failed"),
        make_proposal("prop_c", model="hr", tables=()),
        make_proposal("prop_own", subject="alice"),
        make_proposal("prop_draft", status="draft"),
        make_proposal("prop_done", status="applied"),
    )
    async with mcp_session(create_server_app(deps), kit.token(scope=REVIEW, sub="alice")) as s:
        r = await s.call_tool("list_pending_reviews", {})
    data = structured(r)
    assert data["next_cursor"] is None
    groups = {
        (m["model"], dp["data_product"]): [e["id"] for e in dp["proposals"]]
        for m in data["models"]
        for dp in m["data_products"]
    }
    assert groups == {
        ("hr", "(no table)"): ["prop_c"],
        ("shop", "retail.crm"): ["prop_b"],
        ("shop", "retail.sales"): ["prop_a"],
    }
    entry = data["models"][1]["data_products"][1]["proposals"][0]
    assert entry["status"] == "submitted" and entry["parts"] == 1
    assert entry["operations"] == {"add_concept": 2}
    assert entry["proposer"] == "agent-1" and entry["age_seconds"] >= 3600
    assert "prop_own" not in json.dumps(data)
    assert "retail.sales" in text(r) and "prop_a" in text(r)


async def test_list_pending_respects_review_groups(make_deps: DepsFactory, kit: AuthKit) -> None:
    base = make_deps()
    deps = with_policies(
        base,
        [ModelPolicyEntry(name="hr", access=ModelAccess(review_groups=["hr-reviewers"]))],
    )
    await seed(deps, make_proposal("prop_s"), make_proposal("prop_h", model="hr"))
    async with mcp_session(create_server_app(deps), kit.token(scope=REVIEW, sub="alice")) as s:
        plain = await s.call_tool("list_pending_reviews", {})
    bob = kit.token(scope=REVIEW, sub="bob", groups=["hr-reviewers"])
    async with mcp_session(create_server_app(deps), bob) as s:
        member = await s.call_tool("list_pending_reviews", {})
        scoped = await s.call_tool("list_pending_reviews", {"model": "shop"})
    ids = lambda r: sorted(  # noqa: E731
        e["id"]
        for m in structured(r)["models"]
        for dp in m["data_products"]
        for e in dp["proposals"]
    )
    assert ids(plain) == ["prop_s"]
    assert ids(member) == ["prop_h", "prop_s"]
    assert ids(scoped) == ["prop_s"]


async def test_list_pending_paginates(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = make_deps()
    made = [make_proposal(new_ulid_id("prop_", 1_000 + i)) for i in range(5)]
    mine = make_proposal(new_ulid_id("prop_", 1_002), subject="alice")
    await seed(deps, *made, mine)

    async def ids(s: Any, **args: Any) -> tuple[list[str], str | None]:
        data = structured(await s.call_tool("list_pending_reviews", args))
        got = [
            e["id"] for m in data["models"] for dp in m["data_products"] for e in dp["proposals"]
        ]
        return got, data["next_cursor"]

    async with mcp_session(create_server_app(deps), kit.token(scope=REVIEW, sub="alice")) as s:
        first, c1 = await ids(s, limit=2)
        second, c2 = await ids(s, limit=2, cursor=c1)
        third, c3 = await ids(s, limit=2, cursor=c2)
        bad = await s.call_tool("list_pending_reviews", {"cursor": "!!"})
    assert first + second + third == sorted(p.id for p in made)
    assert c1 and c2 and c3 is None and len(third) == 1
    assert structured(bad)["error"]["code"] == "validation_error"


@pytest.mark.parametrize("tool", ["list_pending_reviews"])
async def test_list_pending_needs_review_scope(
    make_deps: DepsFactory, kit: AuthKit, tool: str
) -> None:
    deps = make_deps()
    await upgrade(deps.engine)
    async with mcp_session(create_server_app(deps), kit.token(scope="lakeloom:read")) as s:
        r = await s.call_tool(tool, {})
    assert structured(r)["error"]["code"] == "forbidden"
