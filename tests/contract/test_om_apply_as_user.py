"""A reviewer applies change sets in OpenMetadata as themselves (FR-OM-04, FR-OM-05, FR-REV-02,
NFR-SEC-10; ADR 0012 § Apply research).

The bot creates a throwaway paired model and drafts and submits a one-concept, one-attribute
proposal (a glossary and a native change set). carol (no OM reviewer role) is refused; alice
applies both with her exchanged token through ``OMClient.with_token``, and tags a throwaway
table's column with the new term. The bot never applies. Everything is hard-deleted, never
sealed, and ``retail`` is checked to be untouched. Tokens are never printed.
"""

import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from pydantic import SecretStr

from lakeloom.core.operations import AddAttribute, AddConcept, OMContext, draft_term_id, to_om_all
from lakeloom.om.client import OMClient, OMForbidden
from lakeloom.om.models import APPLY_PERMISSIONS, PermissionResource
from lakeloom.untrusted import UntrustedText

pytestmark = pytest.mark.contract

SCHEMA = "lakeloom_fixture.retail.sales"


@pytest.fixture
async def http(om_url: str, om_bot_token: str) -> AsyncIterator[httpx.AsyncClient]:
    headers = {"Authorization": f"Bearer {om_bot_token}"}
    async with httpx.AsyncClient(base_url=om_url, headers=headers, timeout=60) as client:
        yield client


@pytest.fixture
async def bot(om_url: str, om_bot_token: str) -> AsyncIterator[OMClient]:
    client = OMClient(om_url, SecretStr(om_bot_token), timeout=60)
    try:
        yield client
    finally:
        await client.aclose()


async def test_fr_rev_02_reviewer_applies_as_herself(
    bot: OMClient, http: httpx.AsyncClient, om_token_for: Callable[[str], str]
) -> None:
    slug = f"ll-apply-{uuid.uuid4().hex[:8]}"
    base_iri = f"http://localhost:8000/ontology/{slug}/"
    retail = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    alice = bot.with_token(SecretStr(om_token_for("alice")))
    carol = bot.with_token(SecretStr(om_token_for("carol")))
    change_sets: list[str] = []
    table_id: str | None = None
    try:
        assert (await alice.get_current_user()).name == "alice"
        assert (await carol.get_current_user()).name == "carol"

        glossary = await bot.create_glossary(name=slug, display_name=None, description="tmp")
        onto = await bot.create_ontology(name=slug, iri=base_iri, description="tmp pair")
        ctx = OMContext(
            glossary_name=slug,
            glossary_id=glossary.id,
            base_iri=base_iri,
            ontology_id=onto.id,
            native_classes=frozenset(),
        )
        widget = AddConcept(
            op="add_concept",
            name="Widget",
            description=UntrustedText(text="A widget", source="contract test"),
        )
        weight = AddAttribute(
            op="add_attribute", concept="Widget", name="weight", datatype="xsd:decimal"
        )
        ops = to_om_all([widget, weight], ctx)
        glossary_cs = await bot.create_change_set(
            name=f"{slug}-g", glossary_id=glossary.id, description="terms", operations=ops.glossary
        )
        change_sets.append(glossary_cs.id)
        native_cs = await bot.create_change_set(
            name=f"{slug}-n",
            glossary_id=None,
            ontology=slug,
            base_working_revision=(await bot.get_ontology_state(onto.id)).working_revision,
            description="native",
            operations=ops.native,
        )
        change_sets.append(native_cs.id)
        for cs_id in change_sets:
            assert (await bot.submit_change_set(cs_id)).state == "SUBMITTED"

        # carol: Lakeloom reviewer group, but no OM reviewer role
        targets: list[tuple[PermissionResource, str]] = [
            ("glossary", glossary.id),
            ("ontology", onto.id),
        ]
        for resource, rid in targets:
            perms = await carol.get_permissions(resource, rid)
            assert perms.operations["EditAll"] == "notAllow"
            assert not perms.allows(*APPLY_PERMISSIONS[resource])
            assert perms.allows("ViewAll")
        with pytest.raises(OMForbidden) as refused:
            await carol.apply_change_set(glossary_cs.id)
        assert refused.value.detail is not None and "[EditAll]" in refused.value.detail
        assert (await bot.get_change_set(glossary_cs.id)).state == "SUBMITTED"  # pyright: ignore[reportOptionalMemberAccess]

        # alice: OM role LakeloomOntologyReviewer
        assert (await alice.get_permissions("glossary", glossary.id)).allows(
            *APPLY_PERMISSIONS["glossary"]
        )
        assert (await alice.get_permissions("ontology", onto.id)).allows(
            *APPLY_PERMISSIONS["ontology"]
        )
        applied = await alice.apply_change_set(glossary_cs.id)
        assert applied.state == "APPLIED"
        result = applied.application_result
        assert result is not None
        assert (result.applied_by, result.operations_failed) == ("alice", 0)
        (term_result,) = result.results
        assert term_result.entity_id == draft_term_id(widget)  # rc1 keeps the draft id
        assert (term_result.entity_fqn, term_result.entity_version) == (f"{slug}.Widget", 0.1)

        native = await alice.apply_change_set(native_cs.id)
        assert native.state == "APPLIED"
        assert native.application_result is not None
        assert native.application_result.applied_by == "alice"
        assert native.application_result.operations_applied == 2

        term = (
            (await http.get(f"/api/v1/glossaryTerms/name/{slug}.Widget")).raise_for_status().json()
        )
        assert (term["id"], term["updatedBy"]) == (draft_term_id(widget), "alice")

        # alice realizes the concept on a throwaway table's column
        table = await http.post(
            "/api/v1/tables",
            json={
                "name": slug,
                "databaseSchema": SCHEMA,
                "columns": [{"name": "c1", "dataType": "INT"}],
            },
        )
        table_id = str(table.raise_for_status().json()["id"])
        fqn = f"{SCHEMA}.{slug}"
        widget_fqn = f"{slug}.Widget"
        assert await alice.add_asset_tags("table", fqn, column="c1", term_fqn=widget_fqn)
        assert not await alice.add_asset_tags("table", fqn, column="c1", term_fqn=widget_fqn)
        tagged = await bot.get_table_by_fqn(fqn, fields=("columns",))
        assert tagged is not None
        assert [t.tag_fqn for t in tagged.columns[0].tags] == [widget_fqn]
        assert await alice.remove_asset_tags("table", fqn, column="c1", term_fqn=widget_fqn)
        assert not await alice.remove_asset_tags("table", fqn, column="c1", term_fqn=widget_fqn)
    finally:
        for cs_id in change_sets:
            await http.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
        if table_id is not None:
            await http.delete(f"/api/v1/tables/{table_id}", params={"hardDelete": "true"})
        for kind in ("ontologies", "glossaries"):
            await http.delete(
                f"/api/v1/{kind}/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
            )
    for path in (
        f"/api/v1/ontologies/name/{slug}",
        f"/api/v1/glossaries/name/{slug}",
        f"/api/v1/tables/name/{SCHEMA}.{slug}",
        *(f"/api/v1/ontologyChangeSets/{c}" for c in change_sets),
    ):
        assert (await http.get(path, params={"include": "all"})).status_code == 404, path
    after = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
    assert (after["version"], after["updatedAt"]) == (retail["version"], retail["updatedAt"])
