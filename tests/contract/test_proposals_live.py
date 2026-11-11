"""Proposal service against the real OpenMetadata (FR-PRO-03, FR-PRO-05, FR-PRO-08, ADR 0011).

In-process: the fixture lake is scanned from the repository checkout, the throwaway model
(glossary + paired native ontology) is created in the test, and the proposal's change sets
are drafted, submitted and discarded in OM. Nothing is applied. Everything created (change
sets, ontology, glossary, relationship types the test had to add) is hard-deleted and checked
to be gone; the seeded ``retail`` glossary is checked to be untouched.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.models import ModelService
from lakeloom.core.operations import AddRelation, op_key
from lakeloom.core.proposals import (
    Actor,
    ConfirmPreview,
    NothingToPropose,
    Proposal,
    ProposalService,
    parse_provenance,
)
from lakeloom.core.scans import ScanService
from lakeloom.om.client import OMClient
from lakeloom.sources.config import DeltaSourceConfig, DeltaTableEntry
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.state.audit import AuditRepository
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.proposals import ProposalRepository
from lakeloom.state.scans import ScanRepository

pytestmark = pytest.mark.contract

LAKE = Path(__file__).resolve().parents[1] / "fixtures" / "lake"
TABLES = ("customers", "products", "orders", "invoices")
RELATION_TYPES = ("hasCustomer", "hasOrder")  # what the mapper names the fixture's FKs
AGENT = Actor(subject="contract-agent", type="agent")


@pytest.fixture
async def http(om_url: str, om_bot_token: str) -> AsyncIterator[httpx.AsyncClient]:
    headers = {"Authorization": f"Bearer {om_bot_token}"}
    async with httpx.AsyncClient(base_url=om_url, headers=headers, timeout=60) as client:
        yield client


@pytest.fixture
async def om(om_url: str, om_bot_token: str) -> AsyncIterator[OMClient]:
    client = OMClient(om_url, SecretStr(om_bot_token), timeout=60)
    try:
        yield client
    finally:
        await client.aclose()


async def _ensure_relation_types(http: httpx.AsyncClient, slug: str) -> list[str]:
    """Add the FK relationship types if OM lacks them; return the ids the test created."""
    created: list[str] = []
    for name in RELATION_TYPES:
        found = await http.get(f"/api/v1/relationshipTypes/name/{name}")
        if found.status_code == 200:
            continue
        body = {
            "name": name,
            "displayName": name,
            "description": "contract-test relationship type (deleted after the test)",
            "category": "CUSTOM",
            "paletteKey": "BLUE",
            "rdfPredicate": f"http://localhost:8000/ontology/{slug}/{name}",
        }
        response = (await http.put("/api/v1/relationshipTypes", json=body)).raise_for_status()
        created.append(str(response.json()["id"]))
    return created


async def _change_set(http: httpx.AsyncClient, cs_id: str) -> dict[str, object]:
    return (await http.get(f"/api/v1/ontologyChangeSets/{cs_id}")).raise_for_status().json()


async def test_fr_pro_03_propose_submit_rerun_discard_live(
    om: OMClient, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    slug = f"ll-prop-{uuid.uuid4().hex[:8]}"
    base_iri = f"http://localhost:8000/ontology/{slug}/"
    retail = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(engine)
    change_sets: list[str] = []
    relation_types: list[str] = []
    try:
        await om.create_glossary(name=slug, display_name=None, description="contract tmp")
        await om.create_ontology(name=slug, iri=base_iri, description="contract tmp pair")
        relation_types = await _ensure_relation_types(http, slug)

        settings = Settings(public_url="http://localhost:8000", max_proposal_ops=20)  # pyright: ignore[reportCallIssue]
        source = DeltaStorageSource(
            DeltaSourceConfig(
                type="delta",
                name="fixture_delta",
                om_service="lakeloom_fixture",
                tables=[
                    DeltaTableEntry(uri=(LAKE / t).as_uri(), catalog="retail", schema="sales")
                    for t in TABLES
                ],
            )
        )
        scans = ScanRepository(engine)
        scan = await ScanService({"fixture_delta": source}, scans, om, settings).scan(
            "fixture_delta", principal_subject="contract-agent", principal_type="agent", client=None
        )
        assert scan.status == "completed"
        models = ModelService(om, settings)
        service = ProposalService(
            om,
            models,
            scans,
            ProposalRepository(engine),
            AuditRepository(engine),
            settings,
            confirm=ConfirmTokens(b"c" * 32, 600),
        )

        p = await service.propose_from_scan(scan.id, slug, principal=AGENT, client="contract")
        change_sets = p.change_set_ids()
        assert len(p.parts) >= 2  # split at 20 operations (FR-PRO-08)
        assert all(len(part.operations) <= 20 for part in p.parts)
        assert sum(isinstance(op, AddRelation) for op in p.operations()) == 2
        for part in p.parts:
            assert part.change_set_ids["glossary"] and part.change_set_ids["native"]
            for side in ("glossary", "native"):
                cs = await _change_set(http, str(part.change_set_ids[side]))
                assert cs["state"] == "DRAFT"
                suffix = "-native" if side == "native" else ""
                assert cs["name"] == f"lakeloom-{p.id}-{part.n}{suffix}"
                block = parse_provenance(str(cs["description"]))
                assert block["scan_id"] == scan.id and block["proposal_id"] == p.id
                assert block["client"] == "contract"
                assert {t["fqn"] for t in block["tables"]} == set(part.tables)
                assert all(t["version"] is not None for t in block["tables"])
                assert [o["key"] for o in block["operations"]] == [
                    op_key(op) for op in part.operations
                ]

        submitted = await service.submit(p.id, principal=AGENT, client="contract")
        assert submitted.status == "submitted"
        for cs_id in change_sets:
            assert (await _change_set(http, cs_id))["state"] == "SUBMITTED"

        with pytest.raises(NothingToPropose) as rerun:
            await service.propose_from_scan(scan.id, slug, principal=AGENT, client="contract")
        assert rerun.value.warnings
        assert all(w.startswith(f"already proposed in {p.id}: ") for w in rerun.value.warnings)

        preview = await service.discard(p.id, principal=AGENT, client="contract")
        assert isinstance(preview, ConfirmPreview)
        done = await service.discard(
            p.id, principal=AGENT, client="contract", confirm_token=preview.confirm_token
        )
        assert isinstance(done, Proposal) and done.status == "discarded"
        for cs_id in change_sets:
            assert (await _change_set(http, cs_id))["state"] == "DISCARDED"
    finally:
        await engine.dispose()
        for cs_id in change_sets:
            await http.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
        await http.delete(
            f"/api/v1/ontologies/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
        )
        await http.delete(
            f"/api/v1/glossaries/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
        )
        for rt in relation_types:
            await http.delete(f"/api/v1/relationshipTypes/{rt}", params={"hardDelete": "true"})
    for path in (
        f"/api/v1/ontologies/name/{slug}",
        f"/api/v1/glossaries/name/{slug}",
        *(f"/api/v1/ontologyChangeSets/{c}" for c in change_sets),
        *(f"/api/v1/relationshipTypes/{r}" for r in relation_types),
    ):
        assert (await http.get(path, params={"include": "all"})).status_code == 404, path
    after = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    assert (after["version"], after["updatedAt"]) == (retail["version"], retail["updatedAt"])
