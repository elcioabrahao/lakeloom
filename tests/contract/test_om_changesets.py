"""Change-set round trip against the real OpenMetadata (FR-OM-01, FR-OM-02, ADR 0011).

Works on a throwaway glossary that is hard-deleted afterwards; nothing is applied and the
seeded ``retail`` glossary is checked to be untouched.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from lakeloom.om.client import OMClient, OMConflict, OMError

pytestmark = pytest.mark.contract


async def _retail_fingerprint(http: httpx.AsyncClient) -> tuple[object, ...]:
    glossary = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    params = {"glossary": glossary["id"], "limit": "100"}
    terms = (await http.get("/api/v1/glossaryTerms", params=params)).raise_for_status().json()
    terms = terms["data"]
    return (
        glossary["version"],
        glossary["updatedAt"],
        tuple(sorted((t["fullyQualifiedName"], t["version"], t["updatedAt"]) for t in terms)),
    )


@pytest.fixture
async def http(om_url: str, om_bot_token: str) -> AsyncIterator[httpx.AsyncClient]:
    """Raw access for the retail check and hard-delete cleanup (not part of OMClient)."""
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


async def test_fr_om_02_change_set_round_trip(client: OMClient, http: httpx.AsyncClient) -> None:
    before = await _retail_fingerprint(http)
    suffix = uuid.uuid4().hex[:8]
    glossary_id: str | None = None
    change_set_id: str | None = None
    try:
        glossary = await client.create_glossary(
            name=f"ll-contract-{suffix}", display_name="Lakeloom contract", description="tmp"
        )
        glossary_id = glossary.id
        with pytest.raises(OMConflict):
            await client.create_glossary(name=glossary.name, display_name=None, description="x")

        created = await client.create_change_set(
            name=f"ll-contract-cs-{suffix}",
            glossary_id=glossary.id,
            description="contract round trip",
            operations=[
                {
                    "operationType": "CREATE_TERM",
                    "term": {
                        "id": str(uuid.uuid4()),
                        "name": "Widget",
                        "description": "A widget",
                        "glossary": {"id": glossary.id, "type": "glossary"},
                    },
                }
            ],
        )
        change_set_id = created.id
        assert created.state == "DRAFT"
        assert [g.id for g in created.glossaries] == [glossary.id]
        assert [op.operation_type for op in created.operations] == ["CREATE_TERM"]

        added = await client.add_operations(
            created.id,
            [
                {
                    "operationType": "CREATE_TERM",
                    "term": {
                        "id": str(uuid.uuid4()),
                        "name": "Gadget",
                        "description": "A gadget",
                        "glossary": {"id": glossary.id, "type": "glossary"},
                    },
                }
            ],
        )
        assert [op.term["name"] for op in added.operations if op.term] == ["Widget", "Gadget"]
        assert added.operations[0].id == created.operations[0].id
        assert added.undo_cursor == 2

        fetched = await client.get_change_set(created.id)
        assert fetched is not None and fetched.state == "DRAFT"
        assert len(fetched.operations) == 2

        submitted = await client.submit_change_set(created.id)
        assert submitted.state == "SUBMITTED"
        with pytest.raises(OMError) as info:  # a submitted change set is no longer editable
            await client.add_operations(created.id, [])
        assert info.value.status == 400

        discarded = await client.discard_change_set(created.id)
        assert discarded.state == "DISCARDED"
        assert discarded.application_result is None

        # Nothing was applied: the throwaway glossary has no terms.
        terms = await client.list_terms(glossary.id, fields=())
        assert terms.data == []

        with pytest.raises(OMError) as info:  # axioms validate against a native ontology
            await client.validate_axiom(
                {
                    "name": "a",
                    "displayName": "a",
                    "description": "a",
                    "axiomType": "SUBCLASS_OF",
                    "subjectIri": "http://example.org/A",
                    "provenance": "Manual",
                    "ontology": f"ll-missing-{suffix}",
                    "expressions": [{"kind": "NAMED_CLASS", "classIri": "http://example.org/B"}],
                }
            )
        assert info.value.status == 404
        assert await client.get_change_set(str(uuid.uuid4())) is None
    finally:
        if change_set_id is not None:
            await http.delete(
                f"/api/v1/ontologyChangeSets/{change_set_id}", params={"hardDelete": "true"}
            )
        if glossary_id is not None:
            await http.delete(
                f"/api/v1/glossaries/{glossary_id}",
                params={"hardDelete": "true", "recursive": "true"},
            )
    if change_set_id is not None:
        url = f"/api/v1/ontologyChangeSets/{change_set_id}"
        gone = await http.get(url, params={"include": "all"})
        assert gone.status_code == 404
    if glossary_id is not None:
        gone = await http.get(f"/api/v1/glossaries/{glossary_id}", params={"include": "all"})
        assert gone.status_code == 404
    assert await _retail_fingerprint(http) == before
