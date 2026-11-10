"""Asset context: resolve an asset and describe its business meaning (FR-CTX-01, FR-CTX-02).

An asset is given as an OpenMetadata FQN (a table or ``<table>.<column>``), a Unity Catalog
name (``catalog.schema.table[.column]``) or a configured storage URI. Only Delta sources'
configured entries are matched against a URI: no filesystem access and no arbitrary URL is
ever touched (NFR-SEC-13). The realized concepts are the Glossary tags on the table and on
its columns; OM propagates table-level tags to columns as ``Derived``, which do not count
(ADR 0009). The context renders as Markdown (default), JSON or JSON-LD with a token
estimate and whole-section truncation (FR-CTX-02, FR-CTX-06); free text is quoted
(NFR-SEC-14).
"""

import asyncio
import json
import posixpath
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel

from lakeloom.core.concepts import (
    AttributeInfo,
    ConceptBundle,
    ConceptRef,
    ConceptSummary,
    concept_bundles,
    direct_glossary_tags,
    fetch_tables,
    realizing_tables,
)
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.tokens import token_estimate, truncate_markdown
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMError, OMUnavailable
from lakeloom.om.fqn import build_fqn
from lakeloom.om.models import OMEntityRef, OMLineage, OMTable
from lakeloom.sources.base import LakeSource
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.untrusted import UntrustedText, code_span, quote_untrusted

__all__ = [
    "AssetContext",
    "ColumnContext",
    "LineageSummary",
    "RelatedAsset",
    "Rendered",
    "ResolvedAsset",
    "get_asset_context",
    "render_json",
    "render_jsonld",
    "render_markdown",
    "resolve_asset",
]

NOT_FOUND_HINT = (
    "pass an OpenMetadata FQN, a Unity Catalog name (catalog.schema.table[.column]) "
    "or a configured storage URI"
)
_URI_SCHEMES = ("file", "s3", "abfss", "gs")
_MAX_RELATIONS = 20
_MAX_TABLES = 20
_RELATION_CANDIDATES = 50
_MAX_LINEAGE = 50
_COLUMN_CHUNK = 25
_CONCURRENCY = 8
_DCTERMS = "http://purl.org/dc/terms/"
_SKOS = "http://www.w3.org/2004/02/skos/core#"

type _Fetch = Callable[[str], Awaitable[OMTable | None]]


class Rendered[T](BaseModel):
    """A rendering with its own token estimate and truncation flag (FR-CTX-06)."""

    payload: T
    token_estimate: int
    truncated: bool


class ResolvedAsset(BaseModel):
    fqn: str
    table_fqn: str
    column: str | None = None
    om_id: str


class ColumnContext(BaseModel):
    name: str
    data_type: str | None = None
    concepts: list[ConceptRef] = []
    description: UntrustedText | None = None
    attributes: list[AttributeInfo] = []


class RelatedAsset(BaseModel):
    relation: str
    concept: ConceptRef
    tables: list[str]


class LineageSummary(BaseModel):
    upstream: list[str] = []
    downstream: list[str] = []


class AssetContext(BaseModel):
    asset: ResolvedAsset
    description: UntrustedText | None = None
    table_concepts: list[ConceptRef] = []
    columns: list[ColumnContext] = []
    concept_details: dict[str, ConceptSummary] = {}
    relationships: list[RelatedAsset] = []
    owners: list[str] = []
    domains: list[str] = []
    data_products: list[str] = []
    model: str | None = None
    release: str | None = None  # None until releases exist (M3)
    lineage: LineageSummary = LineageSummary()


# --- resolution ---------------------------------------------------------------------------


def _not_found(asset: str) -> LakeloomError:
    return LakeloomError("not_found", f"Cannot resolve asset {asset!r}", NOT_FOUND_HINT)


def _upstream(exc: OMError) -> LakeloomError:
    if isinstance(exc, OMUnavailable):
        return LakeloomError(
            "upstream_unavailable",
            "OpenMetadata is unavailable",
            hint="retry shortly; check that OpenMetadata is running",
        )
    return LakeloomError(
        "upstream_unavailable",
        "OpenMetadata could not serve the asset",
        hint="retry shortly; check the OpenMetadata logs",
    )


def _split_fqn(fqn: str) -> list[str]:
    """Split on dots outside double quotes; parts keep their quoting."""
    parts: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in fqn:
        if ch == '"':
            quoted = not quoted
        if ch == "." and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _unquote(part: str) -> str:
    if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
        return part[1:-1].replace('""', '"')
    return part


def _normalise_uri(uri: str) -> str:
    parts = urlsplit(uri.strip())
    path = parts.path
    if parts.scheme.lower() == "file":
        path = posixpath.normpath(path) if path else path
    path = path.rstrip("/") if len(path) > 1 else path
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))


def _is_uri(asset: str) -> bool:
    return "://" in asset and asset.split("://", 1)[0].lower() in _URI_SCHEMES


async def _fetch_or_raise(fetch: _Fetch, fqn: str) -> OMTable | None:
    try:
        return await fetch(fqn)
    except OMError as exc:
        raise _upstream(exc) from exc


def _with_column(asset: str, table: OMTable, column: str | None) -> tuple[ResolvedAsset, OMTable]:
    if column is not None and not any(c.name == column for c in table.columns):
        raise _not_found(asset)
    fqn = table.fully_qualified_name
    return (
        ResolvedAsset(
            fqn=f"{fqn}.{column}" if column else fqn,
            table_fqn=fqn,
            column=column,
            om_id=table.id,
        ),
        table,
    )


async def _resolve(
    asset: str, sources: Mapping[str, LakeSource], fetch: _Fetch
) -> tuple[ResolvedAsset, OMTable]:
    asset = asset.strip()
    if _is_uri(asset):
        wanted = _normalise_uri(asset)
        for name in sorted(sources):
            src = sources[name]
            if not isinstance(src, DeltaStorageSource) or not src.om_service:
                continue
            for uri, ref in src.table_uris().items():
                if _normalise_uri(uri) == wanted and ref.catalog:
                    fqn = build_fqn(src.om_service, ref.catalog, ref.schema_, ref.name)
                    table = await _fetch_or_raise(fetch, fqn)
                    if table is not None:
                        return _with_column(asset, table, None)
        raise _not_found(asset)
    parts = _split_fqn(asset)
    if not all(parts):
        raise _not_found(asset)
    # 1. an OpenMetadata FQN: table (service.database.schema.table) or table.column
    if len(parts) >= 4:
        table = await _fetch_or_raise(fetch, asset)
        if table is not None:
            return _with_column(asset, table, None)
    if len(parts) >= 5:
        table = await _fetch_or_raise(fetch, ".".join(parts[:-1]))
        if table is not None:
            return _with_column(asset, table, _unquote(parts[-1]))
    # 2. a Unity Catalog name under each source's OM service
    if len(parts) in (3, 4):
        for name in sorted(sources):
            service = sources[name].om_service
            if not service:
                continue
            fqn = build_fqn(service, *(_unquote(p) for p in parts[:3]))
            table = await _fetch_or_raise(fetch, fqn)
            if table is not None:
                column = _unquote(parts[3]) if len(parts) == 4 else None
                return _with_column(asset, table, column)
    raise _not_found(asset)


async def resolve_asset(
    asset: str, *, sources: Mapping[str, LakeSource], om: OMClient
) -> ResolvedAsset:
    """Resolve an OM FQN, Unity Catalog name or storage URI to its OM table (FR-CTX-01)."""
    resolved, _ = await _resolve(asset, sources, om.get_table_by_fqn)
    return resolved


# --- context ------------------------------------------------------------------------------


def _label(entity: OMEntityRef) -> str:
    return entity.name or entity.fully_qualified_name or entity.id or ""


def _names(items: list[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        ref = item if isinstance(item, OMEntityRef) else OMEntityRef.model_validate(item)
        if label := _label(ref):
            out.append(label)
    return sorted(dict.fromkeys(out))


def _endpoint(value: str | OMEntityRef | None) -> tuple[str | None, str | None]:
    if isinstance(value, OMEntityRef):
        return value.id, value.fully_qualified_name
    return value, None


def _lineage_summary(lineage: OMLineage, table: OMTable) -> LineageSummary:
    by_id = {n.id: n.fully_qualified_name for n in lineage.nodes if n.id and n.fully_qualified_name}

    def other(edge_end: str | OMEntityRef | None) -> str | None:
        end_id, end_fqn = _endpoint(edge_end)
        return end_fqn or (by_id.get(end_id) if end_id else None)

    upstream: list[str] = []
    downstream: list[str] = []
    for edge in lineage.upstream_edges:
        if _endpoint(edge.to_entity)[0] == table.id and (fqn := other(edge.from_entity)):
            upstream.append(fqn)
    for edge in lineage.downstream_edges:
        if _endpoint(edge.from_entity)[0] == table.id and (fqn := other(edge.to_entity)):
            downstream.append(fqn)
    return LineageSummary(
        upstream=sorted(set(upstream))[:_MAX_LINEAGE],
        downstream=sorted(set(downstream))[:_MAX_LINEAGE],
    )


async def _lineage(om: OMClient, table: OMTable) -> LineageSummary:
    try:
        lineage = await om.get_lineage(
            "table", table.fully_qualified_name, upstream_depth=1, downstream_depth=1
        )
        return _lineage_summary(lineage, table)
    except (OMError, ValueError, KeyError, TypeError):
        return LineageSummary()  # lineage is a supplement: never an error


async def _relationships(
    models: ModelService, om: OMClient, table: OMTable, concepts: list[ConceptBundle]
) -> list[RelatedAsset]:
    candidates: dict[tuple[str, str], ConceptRef] = {}
    for bundle in concepts:
        for rel in bundle.relations:
            candidates.setdefault((rel.type, rel.target.fqn), rel.target)
    chosen = sorted(candidates.items())[:_RELATION_CANDIDATES]
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def tables_of(fqn: str) -> list[str]:
        async with sem:
            try:
                found = await realizing_tables(models, om, fqn)
            except LakeloomError as exc:
                if exc.code == "not_found":
                    return []
                raise
        return [t for t in found if t != table.fully_qualified_name][:_MAX_TABLES]

    results = await asyncio.gather(*(tables_of(fqn) for (_, fqn), _ in chosen))
    related = [
        RelatedAsset(relation=rel, concept=ref, tables=tables)
        for ((rel, _), ref), tables in zip(chosen, results, strict=True)
        if tables
    ]
    return related[:_MAX_RELATIONS]


def _sorted_refs(fqns: list[str], bundles: Mapping[str, ConceptBundle]) -> list[ConceptRef]:
    refs = [bundles[f].ref for f in fqns if f in bundles]
    return sorted(refs, key=lambda r: (r.label, r.fqn))


async def get_asset_context(
    asset: str,
    *,
    models: ModelService,
    om: OMClient,
    sources: Mapping[str, LakeSource],
) -> AssetContext:
    async def fetch(fqn: str) -> OMTable | None:
        return (await fetch_tables(models, om, [fqn]))[fqn]

    resolved, table = await _resolve(asset, sources, fetch)
    columns = [c for c in table.columns if resolved.column in (None, c.name)]
    table_fqns = direct_glossary_tags(table.tags, column=False)
    column_fqns = {c.name: direct_glossary_tags(c.tags, column=True) for c in columns}
    wanted = [*table_fqns, *(f for fqns in column_fqns.values() for f in fqns)]
    bundles, lineage = await asyncio.gather(concept_bundles(models, wanted), _lineage(om, table))
    table_concepts = _sorted_refs(table_fqns, bundles)
    column_ctx = [
        ColumnContext(
            name=c.name,
            data_type=c.data_type,
            concepts=_sorted_refs(column_fqns[c.name], bundles),
            description=(
                UntrustedText(
                    text=c.description,
                    source=f"OpenMetadata column {resolved.table_fqn}.{c.name}",
                )
                if c.description
                else None
            ),
            attributes=sorted(
                {
                    a.name + (a.iri or ""): a
                    for f in column_fqns[c.name]
                    if f in bundles
                    for a in bundles[f].attributes
                }.values(),
                key=lambda a: (a.name, a.iri or ""),
            ),
        )
        for c in columns
    ]
    realized = [
        *table_concepts,
        *(r for col in column_ctx for r in col.concepts),
    ]
    details = {r.fqn: bundles[r.fqn].summary for r in realized}
    relationships = await _relationships(
        models, om, table, [bundles[r.fqn] for r in table_concepts]
    )
    extra: dict[str, Any] = table.model_extra or {}
    ctx = AssetContext(
        asset=resolved,
        description=(
            UntrustedText(text=table.description, source=f"OpenMetadata table {resolved.table_fqn}")
            if table.description
            else None
        ),
        table_concepts=table_concepts,
        columns=column_ctx,
        concept_details=dict(sorted(details.items())),
        relationships=relationships,
        owners=_names(table.owners),
        domains=_names(table.domains),
        data_products=_names(list(extra.get("dataProducts") or [])),
        model=realized[0].model if realized else None,
        lineage=lineage,
    )
    return ctx


# --- Markdown -----------------------------------------------------------------------------


def _concept_list(refs: list[ConceptRef]) -> str:
    return ", ".join(f"{code_span(r.label)} ({code_span(r.fqn)})" for r in refs)


def _indent(block: str) -> str:
    return "\n".join(f"  {line}" for line in block.splitlines())


def _column_line(col: ColumnContext) -> str:
    head = f"- {code_span(col.name)}"
    if col.data_type:
        head += f" ({code_span(col.data_type)})"
    head += ": " + (_concept_list(col.concepts) if col.concepts else "_no mapped concept_")
    lines = [head]
    if col.description is not None:
        quoted = quote_untrusted(col.description.text, source=col.description.source)
        lines.append(_indent(quoted))
    for attr in col.attributes:
        extra = [f"datatype {code_span(attr.datatype)}"] if attr.datatype else []
        if attr.unit:
            extra.append(f"unit {code_span(attr.unit)}")
        if attr.allowed_values:
            extra.append("one of " + ", ".join(code_span(v) for v in attr.allowed_values))
        lines.append(
            f"  - attribute {code_span(attr.name)}" + (f": {'; '.join(extra)}" if extra else "")
        )
    return "\n".join(lines)


def _concept_section(first: bool, ref: ConceptRef, summary: ConceptSummary) -> str:
    lines = [f"### {code_span(ref.label)} ({code_span(ref.fqn)})", ""]
    if summary.iri:
        lines.append(f"- IRI: {code_span(summary.iri)}")
    if summary.parents:
        lines.append(f"- Parents: {_concept_list(summary.parents)}")
    source = f"OpenMetadata glossary term {ref.fqn}"
    if summary.definition is not None:
        lines += ["", quote_untrusted(summary.definition.text, source=summary.definition.source)]
    if summary.synonyms:
        lines += [
            "",
            "Synonyms:",
            "",
            quote_untrusted("\n".join(s.text for s in summary.synonyms), source=source),
        ]
    body = "\n".join(lines)
    return ("## Concepts\n\n" + body) if first else body


def _sections(ctx: AssetContext) -> list[str]:
    a = ctx.asset
    head = [f"# Asset context: {code_span(a.fqn)}", ""]
    head.append(f"- Table: {code_span(a.table_fqn)}")
    if a.column:
        head.append(f"- Column: {code_span(a.column)}")
    sections = ["\n".join(head)]
    if ctx.description is not None:
        sections.append(
            "## Description\n\n"
            + quote_untrusted(ctx.description.text, source=ctx.description.source)
        )
    sections.append(
        "## Table concepts\n\n"
        + (_concept_list(ctx.table_concepts) if ctx.table_concepts else "_none_")
    )
    if ctx.columns:
        chunks = [
            ctx.columns[i : i + _COLUMN_CHUNK] for i in range(0, len(ctx.columns), _COLUMN_CHUNK)
        ]
        for n, chunk in enumerate(chunks):
            body = "\n".join(_column_line(c) for c in chunk)
            sections.append(("## Columns\n\n" + body) if n == 0 else body)
    shown: set[str] = set()
    refs = [*ctx.table_concepts, *(r for c in ctx.columns for r in c.concepts)]
    for ref in refs:
        if ref.fqn in shown:
            continue
        sections.append(_concept_section(not shown, ref, ctx.concept_details[ref.fqn]))
        shown.add(ref.fqn)
    if ctx.relationships:
        lines = [
            f"- {code_span(r.relation)} {code_span(r.concept.label)} ({code_span(r.concept.fqn)}): "
            + ", ".join(code_span(t) for t in r.tables)
            for r in ctx.relationships
        ]
        sections.append("## Relationships\n\n" + "\n".join(lines))
    stewardship = [
        f"- {title}: " + ", ".join(code_span(v) for v in values)
        for title, values in (
            ("Owners", ctx.owners),
            ("Domains", ctx.domains),
            ("Data products", ctx.data_products),
        )
        if values
    ]
    if stewardship:
        sections.append("## Owners, domains and data products\n\n" + "\n".join(stewardship))
    lineage = [
        f"- {title}: " + ", ".join(code_span(v) for v in values)
        for title, values in (
            ("Upstream", ctx.lineage.upstream),
            ("Downstream", ctx.lineage.downstream),
        )
        if values
    ]
    if lineage:
        sections.append("## Lineage\n\n" + "\n".join(lineage))
    if ctx.model:
        model = f"- Model: {code_span(ctx.model)}"
        if ctx.release:
            model += f"\n- Release: {code_span(ctx.release)}"
        sections.append("## Model\n\n" + model)
    return sections


def render_markdown(ctx: AssetContext, *, max_tokens: int | None = None) -> Rendered[str]:
    """Markdown for LLM context windows; whole sections only, ``max_tokens`` honoured."""
    text, truncated = truncate_markdown(_sections(ctx), max_tokens)
    return Rendered(payload=text, token_estimate=token_estimate(text), truncated=truncated)


# --- JSON and JSON-LD ---------------------------------------------------------------------


def _dump(ctx: AssetContext) -> dict[str, Any]:
    return ctx.model_dump(mode="json", by_alias=True)


def _keep_columns(ctx: AssetContext, n: int) -> AssetContext:
    """The first ``n`` columns, with concept details only for the concepts still shown."""
    columns = ctx.columns[:n]
    shown = {r.fqn for r in ctx.table_concepts} | {r.fqn for c in columns for r in c.concepts}
    details = {k: v for k, v in ctx.concept_details.items() if k in shown}
    return ctx.model_copy(update={"columns": columns, "concept_details": details})


# Dropped in this order when no column fits (relationships, lineage, stewardship, concept
# details, description), leaving the asset, its table concepts and the model at worst.
_DROPS: tuple[dict[str, Any], ...] = (
    {"relationships": []},
    {"lineage": LineageSummary()},
    {"owners": [], "domains": [], "data_products": []},
    {"concept_details": {}},
    {"description": None},
)


def _fit(
    ctx: AssetContext, max_tokens: int | None, build: Callable[[AssetContext], dict[str, Any]]
) -> Rendered[dict[str, Any]]:
    """Fit the serialised payload into ``max_tokens`` (FR-CTX-06).

    Keeps the longest prefix of columns that fits (concept details pruned to the concepts
    still shown); when even no column fits, drops whole sections in ``_DROPS`` order.
    ``truncated`` is true when anything was dropped or the minimal skeleton still exceeds
    ``max_tokens``.
    """

    def size(c: AssetContext) -> int:
        return token_estimate(json.dumps(build(c)))

    if max_tokens is None or size(ctx) <= max_tokens:
        return Rendered(payload=build(ctx), token_estimate=size(ctx), truncated=False)
    lo, hi = 0, len(ctx.columns) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size(_keep_columns(ctx, mid)) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    out = _keep_columns(ctx, max(lo, 0))
    for drop in _DROPS:
        if size(out) <= max_tokens:
            break
        out = out.model_copy(update=drop)
    return Rendered(payload=build(out), token_estimate=size(out), truncated=True)


def render_json(ctx: AssetContext, *, max_tokens: int | None = None) -> Rendered[dict[str, Any]]:
    """The context as JSON (schema: ``docs/schemas/asset-context.schema.json``)."""
    return _fit(ctx, max_tokens, _dump)


def _asset_iri(fqn: str) -> str:
    return "urn:lakeloom:asset:" + quote(fqn, safe="")


def render_jsonld(
    ctx: AssetContext, model: ModelInfo | None = None, *, max_tokens: int | None = None
) -> Rendered[dict[str, Any]]:
    """JSON-LD with a minimal ``@context``; concepts link through ``dcterms:subject``."""

    def build(c: AssetContext) -> dict[str, Any]:
        context: dict[str, str] = {}
        if model is not None:
            context.update(model.prefixes)
            if model.base_iri:
                context["@vocab"] = model.base_iri
        context.update({"dcterms": _DCTERMS, "skos": _SKOS})  # fixed: never overridden

        def subjects(refs: list[ConceptRef]) -> list[dict[str, str]]:
            return [{"@id": r.iri} for r in refs if r.iri]

        asset = c.asset
        node: dict[str, Any] = {
            "@id": _asset_iri(asset.table_fqn),
            "dcterms:identifier": asset.table_fqn,
            "dcterms:subject": subjects(c.table_concepts),
        }
        graph: list[dict[str, Any]] = [node]
        for col in c.columns:
            graph.append(
                {
                    "@id": _asset_iri(f"{asset.table_fqn}.{col.name}"),
                    "dcterms:identifier": f"{asset.table_fqn}.{col.name}",
                    "dcterms:isPartOf": {"@id": node["@id"]},
                    "dcterms:subject": subjects(col.concepts),
                }
            )
        return {"@context": context, "@graph": graph}

    return _fit(ctx, max_tokens, build)
