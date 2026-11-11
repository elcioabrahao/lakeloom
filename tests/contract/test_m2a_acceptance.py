"""M2a acceptance against the live stack: the bootstrap flow over real /mcp (FR-AGT-01/03/04).

scan fixture_delta → create_model (dry run + confirm, admin client) → propose_from_scan →
proposal in OpenMetadata as Draft change sets with a provenance block → submit → Submitted;
a re-scan and second propose_from_scan proposes no concept twice (everything is reported as
already proposed or matched). The model name is unique per run (``retail_auto_<hex>``), so
reruns and leftovers never collide. Teardown discards the proposals through the tool
(dry run + confirm) and hard-deletes the change sets, the ontology and the glossary, then
checks they are gone (404 with ``include=all``) and that the seeded ``retail`` glossary is
untouched. Nothing is applied and no release is sealed.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult
from stack import mcp_session

from lakeloom.core.proposals import parse_provenance

pytestmark = pytest.mark.contract


@pytest.fixture
async def om(om_url: str, om_bot_token: str) -> AsyncIterator[httpx.AsyncClient]:
    headers = {"Authorization": f"Bearer {om_bot_token}"}
    async with httpx.AsyncClient(base_url=om_url, headers=headers, timeout=60) as client:
        yield client


async def call(url: str, token: str, tool: str, args: dict[str, Any]) -> CallToolResult:
    async with mcp_session(url, token) as session:
        return await session.call_tool(tool, args)


def data(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result
    return dict(result.structured_content)


def ok(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is not True, json.dumps(result.structured_content)
    return data(result)


async def scan(url: str, token: str) -> str:
    out = ok(await call(url, token, "scan_lake_source", {"source": "fixture_delta"}))
    assert out["status"] == "completed"
    return str(out["scan_id"])


async def _discard(url: str, token: str, proposal_id: str) -> None:
    preview = await call(url, token, "discard_proposal", {"proposal_id": proposal_id})
    if preview.is_error:
        return  # already discarded (or never stored)
    confirm = data(preview)["confirm_token"]
    args = {"proposal_id": proposal_id, "confirm_token": confirm}
    assert ok(await call(url, token, "discard_proposal", args))["discarded"] is True


async def test_m2a_acceptance_bootstrap_flow(
    lakeloom_url: str, agent_token: str, admin_agent_token: str, om: httpx.AsyncClient
) -> None:
    slug = f"retail_auto_{uuid.uuid4().hex[:8]}"
    retail = (await om.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    proposals: list[str] = []
    change_sets: list[str] = []
    try:
        scan_id = await scan(lakeloom_url, agent_token)

        # create_model is admin-only and confirm-token protected (FR-AGT-03)
        forbidden = await call(lakeloom_url, agent_token, "create_model", {"name": slug})
        assert data(forbidden)["error"]["code"] == "forbidden"
        args = {"name": slug, "description": "M2a acceptance throwaway model"}
        preview = ok(await call(lakeloom_url, admin_agent_token, "create_model", args))
        assert preview["created"] is False and preview["creates"] == ["glossary", "ontology"]
        base_iri = str(preview["base_iri"])
        assert base_iri.endswith(f"/ontology/{slug}/")
        assert (await om.get(f"/api/v1/glossaries/name/{slug}")).status_code == 404
        confirmed = {**args, "confirm_token": preview["confirm_token"]}
        created = ok(await call(lakeloom_url, admin_agent_token, "create_model", confirmed))
        assert created["created"] is True
        replay = await call(lakeloom_url, admin_agent_token, "create_model", confirmed)
        assert data(replay)["error"]["code"] == "conflict"
        ontology = (await om.get(f"/api/v1/ontologies/name/{slug}")).raise_for_status().json()
        assert (ontology["iri"], ontology["namespace"]) == (base_iri, base_iri)
        assert (await om.get(f"/api/v1/glossaries/name/{slug}")).status_code == 200

        # propose_from_scan → Draft change sets with provenance (FR-PRO-03)
        result = await call(
            lakeloom_url, agent_token, "propose_from_scan", {"scan_id": scan_id, "model": slug}
        )
        out = ok(result)
        pid = str(out["proposal_id"])
        proposals.append(pid)
        assert out["status"] == "draft"
        change_sets = [
            cs
            for part in out["proposal"]["parts"]
            for cs in part["change_set_ids"].values()
            if cs is not None
        ]
        assert change_sets
        concepts = [
            op["name"]
            for part in out["proposal"]["parts"]
            for op in part["operations"]
            if op["op"] == "add_concept"
        ]
        assert {"Customer", "Order"} <= set(concepts)
        assert len(concepts) == len(set(concepts))
        text = result.content[0]
        assert text.type == "text" and f"# Proposal `{pid}`" in text.text
        for cs_id in change_sets:
            cs = (await om.get(f"/api/v1/ontologyChangeSets/{cs_id}")).raise_for_status().json()
            assert cs["state"] == "DRAFT"
            block = parse_provenance(str(cs["description"]))
            assert (block["scan_id"], block["proposal_id"]) == (scan_id, pid)
            assert block["principal_type"] == "agent" and block["principal_subject"]
            assert {t["fqn"] for t in block["tables"]} <= {
                f"retail.sales.{t}" for t in ("customers", "products", "orders", "invoices")
            }

        shown = ok(await call(lakeloom_url, agent_token, "get_proposal", {"proposal_id": pid}))
        assert {s for part in shown["proposal"]["parts"] for s in part["om_state"].values()} <= {
            "DRAFT",
            None,
        }
        changes = ok(await call(lakeloom_url, agent_token, "diff_proposal", {"proposal_id": pid}))
        assert any(c["change"] == "added" and c["kind"] for c in changes["changes"])

        submitted = ok(
            await call(lakeloom_url, agent_token, "submit_proposal", {"proposal_id": pid})
        )
        assert submitted["status"] == "submitted"
        for cs_id in change_sets:
            cs = (await om.get(f"/api/v1/ontologyChangeSets/{cs_id}")).raise_for_status().json()
            assert cs["state"] == "SUBMITTED"

        # re-running the scan proposes nothing twice (FR-PRO-02)
        rescan_id = await scan(lakeloom_url, agent_token)
        assert rescan_id != scan_id
        rerun = await call(
            lakeloom_url, agent_token, "propose_from_scan", {"scan_id": rescan_id, "model": slug}
        )
        assert rerun.is_error is True
        again = data(rerun)
        assert again["error"]["code"] == "validation_error"
        if not again["error"]["message"].startswith("nothing to propose"):
            pytest.fail(json.dumps(again)[:2000])
        assert again["warnings"] or again["matches"]
        assert all(w.startswith(f"already proposed in {pid}: ") for w in again["warnings"])
    finally:
        for pid in proposals:
            await _discard(lakeloom_url, agent_token, pid)
        for cs_id in change_sets:
            await om.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
        for kind in ("ontologies", "glossaries"):
            await om.delete(
                f"/api/v1/{kind}/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
            )
    for path in (
        f"/api/v1/ontologies/name/{slug}",
        f"/api/v1/glossaries/name/{slug}",
        *(f"/api/v1/ontologyChangeSets/{c}" for c in change_sets),
    ):
        assert (await om.get(path, params={"include": "all"})).status_code == 404, path
    after = (await om.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    assert (after["version"], after["updatedAt"]) == (retail["version"], retail["updatedAt"])


async def test_m2a_prompts_listed_live(lakeloom_url: str, agent_token: str) -> None:
    async with mcp_session(lakeloom_url, agent_token) as session:
        names = {p.name for p in (await session.list_prompts()).prompts}
        got = await session.get_prompt(
            "bootstrap_data_product", {"source": "fixture_delta", "model": "retail"}
        )
    assert {"bootstrap_data_product", "explain_table"} <= names
    content = got.messages[0].content
    assert content.type == "text" and "propose_from_scan" in content.text
