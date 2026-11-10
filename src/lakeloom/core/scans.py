"""Scan service (FR-SRC-07, FR-SRC-10, NFR-PERF-02).

Walks a configured lake source, describes every table (and optionally profiles it),
looks tables up in OpenMetadata, and persists a :class:`ScanRecord`. One broken table
never aborts the scan: its error is recorded on the table and the status is ``partial``.
A profiling failure is recorded separately (``profile_error``) and never fails the table.
"""

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime

from lakeloom.config import Settings
from lakeloom.core.sources import describe_with_version
from lakeloom.errors import LakeloomError
from lakeloom.metrics import SCAN_SECONDS
from lakeloom.om.client import OMClient, OMUnavailable
from lakeloom.om.fqn import build_fqn
from lakeloom.sources.base import LakeSource, ProfilePolicy
from lakeloom.sources.models import TableRef, TableVersion
from lakeloom.state.scans import ScannedTable, ScanRecord, ScanRepository, new_scan_id
from lakeloom.telemetry import record_scan, tracer
from lakeloom.untrusted import code_span, single_line

__all__ = ["ScanService", "summarize"]

_MAX_CLIENT = 200


def _error_dict(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, LakeloomError):
        return dict(exc.to_dict()["error"])
    return dict(
        LakeloomError(
            "upstream_unavailable",
            f"unexpected failure while scanning: {type(exc).__name__}",
            "check the source connectivity and credentials, then retry the scan",
        ).to_dict()["error"]
    )


def _matches_table(ref: TableRef, wanted: set[str]) -> bool:
    return ref.name in wanted or ref.full_name in wanted


class ScanService:
    def __init__(
        self,
        sources: Mapping[str, LakeSource],
        repo: ScanRepository,
        om: OMClient | None,
        settings: Settings,
    ) -> None:
        self._sources = sources
        self._repo = repo
        self._om = om
        self._settings = settings

    async def _enumerate(
        self,
        src: LakeSource,
        catalogs: list[str] | None,
        schemas: list[str] | None,
        tables: list[str] | None,
        warnings: list[str],
    ) -> list[TableRef]:
        named = await src.list_catalogs()
        cats: list[str | None] = [*named]
        if not named:
            cats = [None]
        else:
            try:  # catalog-less tables alongside catalogs must not be dropped
                if await src.list_schemas(None):
                    cats.append(None)
            except LakeloomError:
                pass
        if catalogs is not None:
            cats = [c for c in cats if c is not None and c in catalogs]  # filter excludes None
        found: list[TableRef] = []
        seen_schemas: set[str] = set()
        for cat in cats:
            for schema in await src.list_schemas(cat):
                if schemas is not None and schema not in schemas:
                    continue
                seen_schemas.add(schema)
                found.extend(await src.list_tables(cat, schema))
        if tables is not None:
            wanted = set(tables)
            found = [t for t in found if _matches_table(t, wanted)]
            for value in tables:
                if not any(_matches_table(t, {value}) for t in found):
                    warnings.append(f"table filter {code_span(value)} matched no table")
        if catalogs is not None:
            known = set(await src.list_catalogs())
            warnings.extend(
                f"catalog filter {code_span(c)} matched nothing" for c in catalogs if c not in known
            )
        if schemas is not None:
            warnings.extend(
                f"schema filter {code_span(s)} matched nothing"
                for s in schemas
                if s not in seen_schemas
            )
        return found

    async def _lookup_om(
        self, src: LakeSource, ref: TableRef, row: ScannedTable, warnings: list[str]
    ) -> None:
        if self._om is None or not src.om_service:
            return
        parts = [src.om_service, *([ref.catalog] if ref.catalog else []), ref.schema_, ref.name]
        fqn = build_fqn(*parts)
        try:
            table = await self._om.get_table_by_fqn(fqn)
        except Exception as exc:
            reason = "upstream_unavailable" if isinstance(exc, OMUnavailable) else "upstream_error"
            warnings.append(f"OpenMetadata lookup failed for {code_span(ref.full_name)}: {reason}")
            return
        if table is not None:
            row.om_fqn = table.fully_qualified_name
            row.om_id = table.id

    async def _scan_table(
        self,
        src: LakeSource,
        ref: TableRef,
        profile: bool,
        policy: ProfilePolicy,
        sem: asyncio.Semaphore,
        warnings: list[str],
    ) -> ScannedTable:
        async with sem:
            try:
                description = await describe_with_version(src, ref)
            except Exception as exc:
                return ScannedTable(ref=ref, version=TableVersion(), error=_error_dict(exc))
            row = ScannedTable(ref=ref, version=description.version, description=description)
            if profile and src.capabilities.profiling:
                # A profile failure keeps the description and version (FR-SRC-10).
                try:
                    row.profile = await src.profile_table(ref, policy)
                except Exception as exc:
                    row.profile_error = _error_dict(exc)
            await self._lookup_om(src, ref, row, warnings)
            return row

    async def scan(
        self,
        source: str,
        *,
        catalogs: list[str] | None = None,
        schemas: list[str] | None = None,
        tables: list[str] | None = None,
        profile: bool = False,
        principal_subject: str,
        principal_type: str,
        client: str | None,
    ) -> ScanRecord:
        started = time.perf_counter()
        # Span: source name (operator-configured), counts and status only (NFR-SEC-14).
        with tracer().start_as_current_span(
            "scan", attributes={"lakeloom.source": source, "lakeloom.profile": profile}
        ) as span:
            record = await self._scan(
                source,
                catalogs=catalogs,
                schemas=schemas,
                tables=tables,
                profile=profile,
                principal_subject=principal_subject,
                principal_type=principal_type,
                client=client,
            )
            failed = sum(1 for t in record.tables if t.error)
            span.set_attribute("lakeloom.scan.tables", len(record.tables))
            span.set_attribute("lakeloom.scan.tables_failed", failed)
            span.set_attribute("lakeloom.scan.status", record.status)
            elapsed = time.perf_counter() - started
            SCAN_SECONDS.labels(source=source).observe(elapsed)
            record_scan(source, elapsed)
            return record

    async def _scan(
        self,
        source: str,
        *,
        catalogs: list[str] | None = None,
        schemas: list[str] | None = None,
        tables: list[str] | None = None,
        profile: bool = False,
        principal_subject: str,
        principal_type: str,
        client: str | None,
    ) -> ScanRecord:
        src = self._sources.get(source)
        if src is None:
            raise LakeloomError(
                "not_found",
                f"source {source!r} is not configured",
                "call list_sources to see the configured sources",
            )
        warnings: list[str] = []
        if profile and not src.capabilities.profiling:
            warnings.append(
                f"source {code_span(source)} does not support profiling; tables were "
                "described without profiles"
            )
        scanned: list[ScannedTable] = []
        status = "failed"
        try:
            refs = await self._enumerate(src, catalogs, schemas, tables, warnings)
        except Exception as exc:
            err = _error_dict(exc)
            warnings.append(f"enumeration failed: {err.get('code')}: {err.get('message')}")
            refs = []
        else:
            if not refs:
                warnings.append("no tables matched the requested filters")
        if refs:
            sem = asyncio.Semaphore(max(1, self._settings.scan_concurrency))
            policy = ProfilePolicy.from_settings(self._settings)
            scanned = list(
                await asyncio.gather(
                    *(self._scan_table(src, r, profile, policy, sem, warnings) for r in refs)
                )
            )
            failed = sum(1 for t in scanned if t.error)
            status = (
                "completed" if failed == 0 else "failed" if failed == len(scanned) else "partial"
            )
        record = ScanRecord(
            id=new_scan_id(),
            source=source,
            created_at=datetime.now(UTC),
            principal_subject=principal_subject,
            principal_type=principal_type,
            client=client[:_MAX_CLIENT] if client else client,
            status=status,  # pyright: ignore[reportArgumentType]
            tables=scanned,
            warnings=warnings,
        )
        await self._repo.add(record)
        return record


def summarize(record: ScanRecord) -> str:
    """Markdown summary. Never includes comments; names are inert code spans (NFR-SEC-14)."""
    failed = [t for t in record.tables if t.error]
    lines = [
        f"# Scan {record.id} of {code_span(record.source)}",
        "",
        f"- Status: {record.status}",
        f"- Tables: {len(record.tables)} "
        f"({len(record.tables) - len(failed)} ok, {len(failed)} failed)",
        f"- Requested by: {record.principal_type} {code_span(record.principal_subject)}",
        "",
    ]
    ok = [t for t in record.tables if not t.error]
    if ok:
        lines.append("## Tables")
        for t in ok:
            v = "unknown" if t.version.version is None else str(t.version.version)
            om = f", OpenMetadata {code_span(t.om_fqn)}" if t.om_fqn else ""
            lines.append(f"- {code_span(t.ref.full_name)}: version {v}{om}")
        lines.append("")
    if failed:
        lines.append("## Failed tables")
        for t in failed:
            code = t.error["code"] if t.error else "unknown"
            lines.append(f"- {code_span(t.ref.full_name)}: {single_line(str(code))}")
        lines.append("")
    if record.warnings:
        lines.append("## Warnings")
        # Warnings can embed upstream text (error messages): one line each, always.
        lines.extend(f"- {single_line(w)}" for w in record.warnings)
    return "\n".join(lines).rstrip() + "\n"
