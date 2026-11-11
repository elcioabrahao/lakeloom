"""Hybrid model against the real OpenMetadata: pair a glossary with a native ontology, apply a
native draft that adds an attribute (test-only apply), and read it back through
``explain_concept`` (FR-CTX-03, FR-OM-02, ADR 0011).

Lakeloom has no apply method (M2b gates applying to humans); the apply call lives in this
test. Throwaway glossary, ontology and change sets are hard-deleted, and the seeded
``retail`` glossary is checked to be untouched.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.concepts import explain_concept
from lakeloom.core.models import ModelService
from lakeloom.core.operations import AddAttribute, AddConcept, OMContext, to_om_all
from lakeloom.om.client import OMClient, OMConflict
from lakeloom.untrusted import UntrustedText

pytestmark = pytest.mark.contract


@pytest.fixture
async def http(om_url: str, om_bot_token: str) -> AsyncIterator[httpx.AsyncClient]:
    headers = {"Authorization": f"Bearer {om_bot_token}"}
    async with httpx.AsyncClient(base_url=om_url, headers=headers, timeout=30) as client:
        yield client


@pytest.fixture
async def client(om_url: str, om_bot_token: str) -> AsyncIterator[OMClient]:
    om = OMClient(om_url, SecretStr(om_bot_token), timeout=30)
    try:
        yield om
    finally:
        await om.aclose()


async def _apply(http: httpx.AsyncClient, change_set_id: str) -> None:
    """Test-only: submit and apply a draft with the bot token (not part of OMClient)."""
    session = f"contract-{uuid.uuid4()}"
    lock = {
        "resourceType": "ontologyChangeSet",
        "resourceId": change_set_id,
        "sessionId": session,
        "leaseSeconds": 60,
    }
    lease = (await http.post("/api/v1/ontologyEditLocks/acquire", json=lock)).raise_for_status()
    token = {"lease": {"sessionId": session, "version": lease.json()["version"]}}
    base = f"/api/v1/ontologyChangeSets/{change_set_id}"
    (await http.post(f"{base}/submit", json=token)).raise_for_status()
    (await http.post(f"{base}/apply", json=token)).raise_for_status()


async def test_fr_ctx_03_explain_shows_attribute_applied_to_the_paired_ontology(
    client: OMClient, http: httpx.AsyncClient
) -> None:
    slug = f"ll-pair-{uuid.uuid4().hex[:8]}"
    base_iri = f"http://localhost:8000/ontology/{slug}/"
    retail = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    change_sets: list[str] = []
    try:
        glossary = await client.create_glossary(name=slug, display_name=None, description="tmp")
        models = ModelService(client, Settings(public_url="http://localhost:8000"))  # pyright: ignore[reportArgumentType]

        # Unpaired: the read path works on the glossary alone.
        unpaired_version = await models.version(slug)
        assert await client.export_native_ontology(slug) is None

        onto = await client.create_ontology(name=slug, iri=base_iri, description="tmp pair")
        with pytest.raises(OMConflict) as info:
            await client.create_ontology(name=slug, iri=base_iri, description="again")
        assert info.value.kind == "exists"
        assert (await client.get_ontology_state(onto.id)).working_revision == 1

        ctx = OMContext(
            glossary_name=slug, glossary_id=glossary.id, base_iri=base_iri, ontology_id=onto.id
        )
        ops = to_om_all(
            [
                AddConcept(
                    op="add_concept",
                    name="Widget",
                    description=UntrustedText(text="A widget", source="contract test"),
                ),
                AddAttribute(
                    op="add_attribute", concept="Widget", name="weight", datatype="xsd:decimal"
                ),
            ],
            ctx,
        )
        glossary_cs = await client.create_change_set(
            name=f"{slug}-g", glossary_id=glossary.id, description="terms", operations=ops.glossary
        )
        native_cs = await client.create_change_set(
            name=f"{slug}-n",
            glossary_id=None,
            ontology=slug,
            base_working_revision=1,
            description="native",
            operations=ops.native,
        )
        change_sets = [glossary_cs.id, native_cs.id]
        await _apply(http, glossary_cs.id)
        await _apply(http, native_cs.id)

        models.invalidate(slug)
        detail = await explain_concept(models, client, f"{slug}.Widget")  # pyright: ignore[reportArgumentType]
        assert [(a.name, a.datatype) for a in detail.attributes] == [
            ("weight", "http://www.w3.org/2001/XMLSchema#decimal")
        ]
        assert await models.version(slug) != unpaired_version
    finally:
        for cs_id in change_sets:
            await http.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
        await http.delete(
            f"/api/v1/ontologies/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
        )
        await http.delete(
            f"/api/v1/glossaries/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
        )
    for path in (
        f"/api/v1/ontologies/name/{slug}",
        f"/api/v1/glossaries/name/{slug}",
        *(f"/api/v1/ontologyChangeSets/{c}" for c in change_sets),
    ):
        assert (await http.get(path, params={"include": "all"})).status_code == 404, path
    after = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    assert (after["version"], after["updatedAt"]) == (retail["version"], retail["updatedAt"])
