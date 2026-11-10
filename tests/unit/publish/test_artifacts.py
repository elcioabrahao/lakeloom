"""Publication artefacts: ETag, Last-Modified, Link (FR-PUB-02, FR-PUB-07)."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from lakeloom.config import Settings
from lakeloom.core.models import ModelService
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMUnavailable
from lakeloom.om.models import OMEntityRef, OMGlossary, OMGlossaryTerm, OMLineage, OMPage, OMTable
from lakeloom.publish.artifacts import (
    asset_context_artifact,
    concept_artifact,
    etag_for,
    matches_if_none_match,
    model_artifact,
    model_context_artifact,
    models_list_artifact,
)
from lakeloom.publish.negotiation import FORMAT_BY_MEDIA

B = "http://x/ontology/retail/"
TURTLE = f"""\
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix om: <https://open-metadata.org/ontology/> .
@prefix r: <{B}> .
r:Order a skos:Concept, om:GlossaryTerm ;
    skos:prefLabel "Order" ; skos:definition "An order." ;
    om:fullyQualifiedName "retail.Order" .
"""
ORDERS = "svc.lake.retail.orders"
GLOSSARY_MS = 4_102_444_800_000  # year 2100: later than any graph load time
TABLE_MS = 4_000_000_000_000


class FakeOM:
    def __init__(self, glossary_ms: int | None = GLOSSARY_MS) -> None:
        self.glossary_ms = glossary_ms
        self.down = False

    def _glossary(self) -> OMGlossary:
        data: dict[str, Any] = {"id": "id-retail", "name": "retail", "fullyQualifiedName": "retail"}
        if self.glossary_ms is not None:
            data["updatedAt"] = self.glossary_ms
        return OMGlossary.model_validate(data)

    async def list_glossaries(self, *, after: str | None = None, limit: int = 50) -> Any:
        return OMPage[OMGlossary](data=[self._glossary()], after=None)

    async def get_glossary_by_name(self, name: str, **_: object) -> OMGlossary | None:
        if self.down:
            raise OMUnavailable("down")
        return self._glossary() if name == "retail" else None

    async def export_ontology(self, glossary_id: str, fmt: str, **_: object) -> str:
        return TURTLE

    async def get_term(self, id_or_fqn: str, *_: object, **__: object) -> OMGlossaryTerm | None:
        if id_or_fqn != "retail.Order":
            return None
        return OMGlossaryTerm.model_validate(
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Order",
                "fullyQualifiedName": "retail.Order",
                "glossary": {"id": "id-retail", "name": "retail", "type": "glossary"},
            }
        )

    async def get_term_assets(self, term_id: str, *, offset: int = 0, limit: int = 50) -> Any:
        ref = OMEntityRef.model_validate(
            {"id": ORDERS, "type": "table", "name": "orders", "fullyQualifiedName": ORDERS}
        )
        return OMPage[OMEntityRef](data=[ref], after=None, total=1)

    async def get_table_by_fqn(self, fqn: str, **_: object) -> OMTable | None:
        if fqn != ORDERS:
            return None
        return OMTable.model_validate(
            {
                "id": ORDERS,
                "fullyQualifiedName": ORDERS,
                "updatedAt": TABLE_MS,
                "tags": [{"tagFQN": "retail.Order", "source": "Glossary", "labelType": "Manual"}],
                "columns": [],
            }
        )

    async def get_lineage(self, entity_type: str, fqn: str, **_: object) -> OMLineage:
        return OMLineage()


def make(om_url: str | None = "http://om:8585/", **kw: Any) -> tuple[ModelService, FakeOM]:
    om = FakeOM(**kw)
    settings = Settings(public_url="http://x", om_url=om_url)  # pyright: ignore[reportArgumentType]
    return ModelService(om, settings), om  # pyright: ignore[reportArgumentType]


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, UTC)  # HTTP dates: whole seconds


def test_fr_pub_07_etag_is_sha256_of_body() -> None:
    body = b"hello"
    assert etag_for(body) == f'"sha256:{hashlib.sha256(body).hexdigest()}"'


def test_if_none_match_variants() -> None:
    e = etag_for(b"x")
    assert matches_if_none_match("*", e)
    assert matches_if_none_match(e, e)
    assert matches_if_none_match(f'"other", {e}', e)
    assert matches_if_none_match(f"W/{e}", e)
    assert matches_if_none_match(f'"a" ,W/{e} , "b"', e)
    assert not matches_if_none_match('"other"', e)
    assert not matches_if_none_match(None, e)
    assert not matches_if_none_match("", e)
    assert not matches_if_none_match("garbage", e)


@pytest.mark.parametrize(
    ("media", "needle"),
    [
        ("text/turtle", b"@prefix"),
        ("application/ld+json", b"@id"),
        ("application/rdf+xml", b"rdf:RDF"),
        ("application/n-triples", b"<http://x/ontology/retail/Order>"),
    ],
)
async def test_model_artifact_formats_and_links(media: Any, needle: bytes) -> None:
    models, _ = make()
    art = await model_artifact(models, "retail", media, version=None)
    assert needle in art.body
    assert art.media_type == media
    assert art.etag == etag_for(art.body)
    assert art.last_modified == _dt(GLOSSARY_MS)
    assert art.links == ['<http://om:8585/glossary/retail>; rel="via"']
    assert art.body == await models.export("retail", FORMAT_BY_MEDIA[media])


async def test_model_artifact_last_modified_falls_back_to_load_time() -> None:
    models, _ = make(glossary_ms=None)
    before = datetime.now(UTC).replace(microsecond=0)
    art = await model_artifact(models, "retail", "text/turtle", version="latest")
    assert before <= art.last_modified <= datetime.now(UTC)
    again = await model_artifact(models, "retail", "text/turtle", version="latest")
    assert again.last_modified == art.last_modified  # stable while the graph is cached


async def test_no_om_url_means_no_links() -> None:
    models, _ = make(om_url=None)
    art = await model_artifact(models, "retail", "text/turtle", version=None)
    assert art.links == []


@pytest.mark.parametrize("version", ["1.0.0", "v2", ""])
async def test_version_other_than_latest_not_found(version: str) -> None:
    models, _ = make()
    with pytest.raises(LakeloomError) as e:
        await model_artifact(models, "retail", "text/turtle", version=version)
    assert e.value.code == "not_found"
    assert "version=latest" in (e.value.hint or "")
    with pytest.raises(LakeloomError):
        await model_context_artifact(models, "retail", version=version)


async def test_unknown_model_not_found() -> None:
    models, _ = make()
    with pytest.raises(LakeloomError) as e:
        await model_artifact(models, "nope", "text/turtle", version=None)
    assert e.value.code == "not_found"


async def test_model_context_artifact() -> None:
    models, _ = make()
    art = await model_context_artifact(models, "retail", version="latest")
    assert art.media_type == "application/ld+json"
    ctx = json.loads(art.body)["@context"]
    assert ctx["@vocab"] == B
    assert ctx["retail"] == B
    assert ctx["dcterms"] == "http://purl.org/dc/terms/"
    assert ctx["skos"] == "http://www.w3.org/2004/02/skos/core#"
    assert art.etag == etag_for(art.body)
    assert art.last_modified == _dt(GLOSSARY_MS)
    assert art.links == ['<http://om:8585/glossary/retail>; rel="via"']


async def test_models_list_artifact() -> None:
    models, _ = make()
    art = await models_list_artifact(models)
    assert art.media_type == "application/json"
    items = json.loads(art.body)["items"]
    assert [i["slug"] for i in items] == ["retail"]
    assert art.etag == etag_for(art.body)


async def test_concept_artifact_markdown_and_json() -> None:
    models, om = make()
    md = await concept_artifact(models, om, "retail.Order", "text/markdown")  # pyright: ignore[reportArgumentType]
    assert md.media_type == "text/markdown"
    assert b"Order" in md.body
    assert md.last_modified == _dt(GLOSSARY_MS)
    assert md.links == ['<http://om:8585/glossary/retail>; rel="via"']
    js = await concept_artifact(models, om, "retail.Order", "application/json")  # pyright: ignore[reportArgumentType]
    assert json.loads(js.body)["concept"]["fqn"] == "retail.Order"
    assert js.etag != md.etag


async def test_concept_unknown_not_found() -> None:
    models, om = make()
    with pytest.raises(LakeloomError) as e:
        await concept_artifact(models, om, "retail.Nope", "text/markdown")  # pyright: ignore[reportArgumentType]
    assert e.value.code == "not_found"


@pytest.mark.parametrize("media", ["text/markdown", "application/json", "application/ld+json"])
async def test_asset_context_artifact(media: Any) -> None:
    models, om = make()
    art = await asset_context_artifact(models, om, {}, ORDERS, media)  # pyright: ignore[reportArgumentType]
    assert art.media_type == media
    assert art.last_modified == _dt(GLOSSARY_MS)  # max(table, realized model)
    assert art.links == [f'<http://om:8585/table/{ORDERS}>; rel="via"']
    assert art.etag == etag_for(art.body)


async def test_asset_link_percent_encodes_fqn_segment() -> None:
    models, om = make()
    fqn = 'svc."my db".s/x'
    # table not found, but the link builder is exercised separately
    from lakeloom.publish.artifacts import table_link

    assert (
        table_link("http://om:8585", fqn)
        == '<http://om:8585/table/svc.%22my%20db%22.s%2Fx>; rel="via"'
    )
    del models, om


async def test_last_modified_is_max_of_updated_at_and_load_time() -> None:
    models, _ = make(glossary_ms=1_000_000_000_000)  # 2001: older than the graph load
    art = await model_artifact(models, "retail", "text/turtle", version=None)
    assert art.last_modified == (await models.loaded_at("retail")).replace(microsecond=0)


async def test_models_list_without_updated_at_uses_clock_and_links_glossaries() -> None:
    models, _ = make(glossary_ms=None)
    now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    art = await models_list_artifact(models, clock=lambda: now)
    assert art.last_modified == now
    assert art.links == ['<http://om:8585/glossary>; rel="via"']


async def test_asset_without_any_timestamp_uses_clock() -> None:
    models, om = make()

    async def get_table(fqn: str, **_: object) -> OMTable | None:
        return OMTable.model_validate({"id": ORDERS, "fullyQualifiedName": ORDERS})

    om.get_table_by_fqn = get_table  # type: ignore[method-assign]
    now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    art = await asset_context_artifact(
        models,
        om,  # pyright: ignore[reportArgumentType]
        {},
        ORDERS,
        "text/markdown",
        clock=lambda: now,  # pyright: ignore[reportArgumentType]
    )
    assert art.last_modified == now


async def test_om_url_userinfo_is_stripped_from_links() -> None:
    models, _ = make(om_url="http://user:secret@om:8585/base/")
    art = await model_artifact(models, "retail", "text/turtle", version=None)
    assert art.links == ['<http://om:8585/base/glossary/retail>; rel="via"']
    assert "secret" not in "".join(art.links)
