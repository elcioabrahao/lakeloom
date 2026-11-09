"""Source operations shared by the MCP tools and the CLI (FR-AGT-01, FR-SRC-01, FR-SRC-05).

Sources are operator-configured (NFR-SEC-13): callers name a configured source, never a URL.
Upstream MCP sources need :meth:`start`; :func:`start_sources` runs them concurrently with
a bound so an unreachable upstream never blocks startup, and the source then reports
``available=False`` with a typed error.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from lakeloom.errors import ErrorCode, LakeloomError
from lakeloom.sources.base import LakeSource, SourceCapabilities
from lakeloom.sources.models import TableDescription, TableRef

__all__ = [
    "SOURCE_START_TIMEOUT",
    "SourceError",
    "SourceStatus",
    "close_sources",
    "describe_with_version",
    "get_source",
    "list_all_tables",
    "parse_table_name",
    "source_status",
    "start_sources",
]

_log = logging.getLogger("lakeloom.sources")
SOURCE_START_TIMEOUT = 15.0


@runtime_checkable
class _Startable(Protocol):
    async def start(self) -> None: ...


class SourceError(BaseModel):
    """Why a source is unavailable: the typed code and what to do (no upstream detail)."""

    code: ErrorCode
    hint: str


class SourceStatus(BaseModel):
    """A configured source as agents see it (FR-AGT-01)."""

    name: str = Field(description="Source name to pass to the other source tools.")
    kind: str = Field(description="Connector type: delta, unity_catalog, mcp or a plugin.")
    capabilities: SourceCapabilities = Field(
        description="profiling: profile=true works; versions: table versions are known; "
        "constraints: primary/foreign keys are reported."
    )
    available: bool = Field(description="False when the source cannot be used right now.")
    error: SourceError | None = Field(
        default=None, description="Set when available is false: error code and hint."
    )


def source_status(name: str, source: LakeSource) -> SourceStatus:
    """Status of one source; only upstream MCP sources can be unavailable."""
    available = bool(getattr(source, "available", True))
    error: SourceError | None = None
    if not available:
        last = getattr(source, "last_error", None)
        error = (
            SourceError(code=last.code, hint=last.hint)
            if isinstance(last, LakeloomError)
            else SourceError(
                code="upstream_unavailable", hint="check the server logs for this source"
            )
        )
    return SourceStatus(
        name=name,
        kind=source.kind,
        capabilities=source.capabilities,
        available=available,
        error=error,
    )


def get_source(sources: Mapping[str, LakeSource], name: str) -> LakeSource:
    src = sources.get(name)
    if src is None:
        raise LakeloomError(
            "not_found",
            f"source {name!r} is not configured",
            "call list_sources to see the configured sources",
        )
    return src


def parse_table_name(full_name: str) -> TableRef:
    """``catalog.schema.table`` or ``schema.table`` (as list_tables returns them)."""
    parts = full_name.split(".")
    if len(parts) not in (2, 3) or not all(parts):
        raise LakeloomError(
            "validation_error",
            f"table {full_name!r} is not a full table name",
            "pass the full_name returned by list_tables: catalog.schema.table or schema.table",
        )
    if len(parts) == 3:
        return TableRef(catalog=parts[0], schema=parts[1], name=parts[2])
    return TableRef(schema=parts[0], name=parts[1])


async def list_all_tables(
    src: LakeSource, catalog: str | None = None, schema: str | None = None
) -> list[TableRef]:
    """Every table of ``src``, optionally narrowed to one catalog and/or schema."""
    if catalog is not None:
        cats: list[str | None] = [catalog]
    else:
        named = await src.list_catalogs()
        cats = [*named] if named else [None]
        if named:
            try:  # catalog-less tables alongside catalogs must not be dropped
                if await src.list_schemas(None):
                    cats.append(None)
            except LakeloomError:
                pass
    found: list[TableRef] = []
    for cat in cats:
        schemas = [schema] if schema is not None else await src.list_schemas(cat)
        for sch in schemas:
            found.extend(await src.list_tables(cat, sch))
    return found


async def describe_with_version(src: LakeSource, ref: TableRef) -> TableDescription:
    """Describe a table with its current version filled in, whatever the source.

    Some sources (Unity Catalog) leave ``version`` empty in ``describe_table``; the version
    comes from ``get_table_version`` so the result is the same shape everywhere. A source
    without the ``versions`` capability keeps the (empty) version it described.
    """
    description = await src.describe_table(ref)
    if not src.capabilities.versions:
        return description
    version = await src.get_table_version(ref)
    return description.model_copy(update={"version": version})


async def start_sources(
    sources: Mapping[str, LakeSource], bound: float = SOURCE_START_TIMEOUT
) -> None:
    """Start every source that needs it (upstream MCP), concurrently; never raises."""
    startable = {n: s for n, s in sources.items() if isinstance(s, _Startable)}
    if not startable:
        return

    async def start_one(name: str, src: _Startable) -> None:
        try:
            await src.start()
        except Exception as exc:  # start() should not raise; never let one break startup
            _log.warning("source '%s' failed to start (%s)", name, type(exc).__name__)

    try:
        async with asyncio.timeout(bound):
            await asyncio.gather(*(start_one(n, s) for n, s in startable.items()))
    except TimeoutError:
        _log.warning("starting sources timed out after %g s", bound)
    for name in startable:
        src = sources[name]
        if source_status(name, src).available:
            _log.info("source '%s' available", name)
        else:
            last = getattr(src, "last_error", None)
            detail = f"{last.code}: {last.message}" if isinstance(last, LakeloomError) else "?"
            _log.warning("source '%s' unavailable (%s)", name, detail)


async def close_sources(sources: Mapping[str, LakeSource]) -> None:
    """Close every source; one failing close never stops the others."""
    results = await asyncio.gather(*(s.aclose() for s in sources.values()), return_exceptions=True)
    for name, result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            _log.warning("closing source '%s' failed (%s)", name, type(result).__name__)
