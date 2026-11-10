"""Ontology models (OM glossaries): listing, cached RDF graphs, deterministic export.

A model is an OpenMetadata glossary; its slug is the glossary name. The graph comes from
OM's own export (turtle, parsed with rdflib) and is cached per slug for
``model_cache_ttl_seconds``. After the TTL a failed reload raises: stale graphs are never
served beyond it (FR-IO-01, FR-GIT-03, ADR 0009).
"""

import asyncio
import base64
import binascii
import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from rdflib import Graph

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMError, OMUnavailable
from lakeloom.om.models import OMGlossary
from lakeloom.rdf.canonical import ExportFormat, parse_rdf, serialize
from lakeloom.sources.models import Page
from lakeloom.untrusted import UntrustedText

__all__ = ["ModelInfo", "ModelService"]

_LIST_MODELS_HINT = "call list_models to see the available model slugs"


class ModelInfo(BaseModel):
    slug: str
    glossary_id: str
    fqn: str
    display_name: str | None = None
    description: UntrustedText | None = None
    base_iri: str | None = None
    prefixes: dict[str, str] = {}
    term_count: int | None = None


@dataclass
class _Entry:
    graph: Graph
    info: ModelInfo
    loaded_at: float


def _upstream(exc: OMError) -> LakeloomError:
    if isinstance(exc, OMUnavailable):
        return LakeloomError(
            "upstream_unavailable",
            "OpenMetadata is unavailable",
            hint="retry shortly; check that OpenMetadata is running",
        )
    if exc.status == 404:
        return LakeloomError("not_found", "Model not found in OpenMetadata", _LIST_MODELS_HINT)
    return LakeloomError(
        "upstream_unavailable",
        "OpenMetadata could not serve the model",
        hint="retry shortly; check the OpenMetadata logs",
    )


class ModelService:
    def __init__(
        self,
        om: OMClient,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._om = om
        self._settings = settings
        self._clock = clock
        self._cache: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # --- models -----------------------------------------------------------------------

    def _info(self, glossary: OMGlossary) -> ModelInfo:
        slug = glossary.name
        base_iri: str | None = None
        prefixes: dict[str, str] = {}
        # OM rc1 does not persist namespaces; prefer them if a later build does, else config.
        for ns in glossary.namespaces:
            if ns.prefix and ns.namespace:
                prefixes[ns.prefix] = ns.namespace
                base_iri = base_iri or ns.namespace
        if not prefixes and self._settings.public_base_url is not None:
            base_iri = f"{self._settings.public_base_url}/ontology/{slug}/"
            prefixes = {slug: base_iri}
        source = f"OpenMetadata glossary {glossary.fully_qualified_name}"
        return ModelInfo(
            slug=slug,
            glossary_id=glossary.id,
            fqn=glossary.fully_qualified_name,
            display_name=glossary.display_name,
            description=(
                UntrustedText(text=glossary.description, source=source)
                if glossary.description
                else None
            ),
            base_iri=base_iri,
            prefixes=prefixes,
            term_count=glossary.term_count,
        )

    async def list_models(self, cursor: str | None, limit: int) -> Page[ModelInfo]:
        after: str | None = None
        if cursor:
            try:
                after = base64.urlsafe_b64decode(cursor.encode()).decode()
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                raise LakeloomError(
                    "validation_error",
                    "Invalid cursor",
                    hint="pass the next_cursor from a previous list_models call, or omit it",
                ) from exc
        try:
            page = await self._om.list_glossaries(after=after, limit=limit)
        except OMError as exc:
            raise _upstream(exc) from exc
        next_cursor = base64.urlsafe_b64encode(page.after.encode()).decode() if page.after else None
        return Page[ModelInfo](items=[self._info(g) for g in page.data], next_cursor=next_cursor)

    async def get_model(self, slug: str) -> ModelInfo:
        try:
            glossary = await self._om.get_glossary_by_name(slug)
        except OMError as exc:
            raise _upstream(exc) from exc
        if glossary is None:
            raise LakeloomError("not_found", f"Unknown model {slug!r}", _LIST_MODELS_HINT)
        return self._info(glossary)

    # --- graph and export -------------------------------------------------------------

    def clear_cache(self) -> None:
        self._cache.clear()

    def _fresh(self, slug: str) -> _Entry | None:
        entry = self._cache.get(slug)
        ttl = self._settings.model_cache_ttl_seconds
        if entry is not None and ttl > 0 and self._clock() - entry.loaded_at < ttl:
            return entry
        return None

    async def graph(self, slug: str) -> Graph:
        return (await self._load(slug)).graph

    async def _load(self, slug: str) -> _Entry:
        cached = self._fresh(slug)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(slug, asyncio.Lock())
        async with lock:
            cached = self._fresh(slug)  # another caller may have loaded while we waited
            if cached is not None:
                return cached
            self._cache.pop(slug, None)  # expired: never serve it if the reload fails
            info = await self.get_model(slug)
            try:
                text = await self._om.export_ontology(info.glossary_id, "turtle")
            except OMError as exc:
                raise _upstream(exc) from exc
            try:
                graph = parse_rdf(text, "turtle")
            except LakeloomError as exc:
                raise LakeloomError(
                    "upstream_unavailable",
                    f"OpenMetadata returned an unreadable export for {slug!r}",
                    hint="check the OpenMetadata RDF export; retry shortly",
                ) from exc
            entry = _Entry(graph, info, self._clock())
            self._cache[slug] = entry
            return entry

    async def export(self, slug: str, fmt: ExportFormat) -> bytes:
        entry = await self._load(slug)
        return serialize(entry.graph, fmt, prefixes=entry.info.prefixes)
