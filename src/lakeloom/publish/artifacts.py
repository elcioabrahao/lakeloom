"""Cache-validated publication artefacts (FR-PUB-02, FR-PUB-07).

An :class:`Artifact` is a body plus the validators a cache needs: a strong ``ETag`` (SHA-256 of
the body), ``Last-Modified`` (OpenMetadata ``updatedAt`` of the source entity, falling back to
the time the model graph was loaded) and ``Link`` values pointing at the OpenMetadata entity the
artefact was built from (``rel="via"``; Git links arrive with M2, ADR 0010).
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from lakeloom.core.concepts import concept_markdown, explain_concept, fetch_tables
from lakeloom.core.context import (
    get_asset_context,
    minimal_jsonld_context,
    render_json,
    render_jsonld,
    render_markdown,
)
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient
from lakeloom.publish.negotiation import FORMAT_BY_MEDIA, MediaType
from lakeloom.sources.base import LakeSource

__all__ = [
    "Artifact",
    "asset_context_artifact",
    "concept_artifact",
    "etag_for",
    "glossary_link",
    "matches_if_none_match",
    "model_artifact",
    "model_context_artifact",
    "models_list_artifact",
    "table_link",
]

_EPOCH = datetime.fromtimestamp(0, UTC)
_LIST_LIMIT = 200
_MODELS_LIST_PAGES = 50  # safety bound on pagination while building the models list


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    body: bytes
    media_type: str
    etag: str
    last_modified: datetime
    links: list[str]


def etag_for(body: bytes) -> str:
    return '"sha256:' + hashlib.sha256(body).hexdigest() + '"'


def _opaque(tag: str) -> str:
    tag = tag.strip()
    return tag[2:] if tag.startswith(("W/", "w/")) else tag


def matches_if_none_match(header: str | None, etag: str) -> bool:
    """``If-None-Match`` evaluation with the weak comparison of RFC 9110 section 13.1.2."""
    if not header or not header.strip():
        return False
    if header.strip() == "*":
        return True
    wanted = _opaque(etag)
    return any(_opaque(candidate) == wanted for candidate in header.split(",") if candidate.strip())


def glossary_link(om_url: str, slug: str) -> str:
    return f'<{om_url}/glossary/{quote(slug, safe="")}>; rel="via"'


def table_link(om_url: str, fqn: str) -> str:
    return f'<{om_url}/table/{quote(fqn, safe="")}>; rel="via"'


def _from_ms(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


def _check_version(version: str | None) -> None:
    if version not in (None, "latest"):
        raise LakeloomError(
            "not_found",
            f"Unknown version {version!r}",
            hint="releases are published from M3; use version=latest",
        )


def _json_bytes(data: Any) -> bytes:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode()


def _make(body: bytes, media_type: str, last_modified: datetime, links: list[str]) -> Artifact:
    return Artifact(
        body=body,
        media_type=media_type,
        etag=etag_for(body),
        last_modified=last_modified.replace(microsecond=0),  # HTTP dates have 1 s resolution
        links=links,
    )


async def _model_stamp(models: ModelService, info: ModelInfo) -> tuple[datetime, list[str]]:
    modified = _from_ms(info.updated_at) or await models.loaded_at(info.slug)
    om_url = models.om_url
    return modified, [] if om_url is None else [glossary_link(om_url, info.slug)]


async def model_artifact(
    models: ModelService, slug: str, media: MediaType, *, version: str | None
) -> Artifact:
    _check_version(version)
    fmt = FORMAT_BY_MEDIA.get(media)
    if fmt is None:
        raise LakeloomError(
            "validation_error",
            f"{media} is not an ontology serialisation",
            hint="use text/turtle, application/ld+json, application/rdf+xml or "
            "application/n-triples",
        )
    info = await models.get_model(slug)
    body = await models.export(slug, fmt)  # loads (or reuses) the graph for loaded_at below
    modified, links = await _model_stamp(models, info)
    return _make(body, media, modified, links)


async def model_context_artifact(
    models: ModelService, slug: str, *, version: str | None
) -> Artifact:
    """The minimal JSON-LD ``@context`` of a model (the full ``context.jsonld`` is M3)."""
    _check_version(version)
    info = await models.get_model(slug)
    await models.graph(slug)  # fail like the other artefacts when the export is unreadable
    context = minimal_jsonld_context(info)  # the one the asset JSON-LD carries
    modified, links = await _model_stamp(models, info)
    return _make(_json_bytes({"@context": context}), "application/ld+json", modified, links)


async def models_list_artifact(models: ModelService) -> Artifact:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(_MODELS_LIST_PAGES):
        page = await models.list_models(cursor, _LIST_LIMIT)
        items += [m.model_dump(mode="json") for m in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    newest = max((m["updated_at"] for m in items if m.get("updated_at")), default=None)
    return _make(_json_bytes({"items": items}), "application/json", _from_ms(newest) or _EPOCH, [])


async def concept_artifact(
    models: ModelService,
    om: OMClient,
    concept: str,
    media: Literal["text/markdown", "application/json"],
) -> Artifact:
    detail = await explain_concept(models, om, concept)
    if media == "text/markdown":
        body = concept_markdown(detail)[0].encode()
    else:
        body = _json_bytes(detail.model_dump(mode="json"))
    info = await models.get_model(detail.concept.model)
    modified, links = await _model_stamp(models, info)
    return _make(body, media, modified, links)


async def asset_context_artifact(
    models: ModelService,
    om: OMClient,
    sources: Mapping[str, LakeSource],
    fqn: str,
    media: Literal["text/markdown", "application/json", "application/ld+json"],
) -> Artifact:
    ctx = await get_asset_context(fqn, models=models, om=om, sources=sources)
    model = await models.get_model(ctx.model) if ctx.model else None
    if media == "text/markdown":
        body = render_markdown(ctx).payload.encode()
    elif media == "application/json":
        body = _json_bytes(render_json(ctx).payload)
    else:
        body = _json_bytes(render_jsonld(ctx, model).payload)
    table = (await fetch_tables(models, om, [ctx.asset.table_fqn]))[ctx.asset.table_fqn]
    modified = _from_ms(table.updated_at if table else None)
    if modified is None:
        modified = await models.loaded_at(model.slug) if model else _EPOCH
    om_url = models.om_url
    links = [] if om_url is None else [table_link(om_url, ctx.asset.table_fqn)]
    return _make(body, media, modified, links)
