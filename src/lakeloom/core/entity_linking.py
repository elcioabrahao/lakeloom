"""Entity linking: map lakeloom concepts to external Knowledge Graph entities (M6a).

Stage 5+ of the Ontology Pipeline: linking local concepts to Wikidata / schema.org
enables SKOS ``skos:exactMatch`` / ``skos:closeMatch`` etc. and enriches the ontology
with authoritative definitions and multilingual labels.

Supported external KGs:
  - ``"wikidata"``    — Wikidata (wikidata.org) via MediaWiki search API (no auth)
  - ``"schema.org"`` — schema.org type hierarchy via its JSON-LD context

This module is I/O-free: all network functions accept a ``fetch`` callable so that
tests can inject a fake HTTP client.  The MCP tools in ``server/tools_linking.py``
pass ``aiohttp`` or ``httpx``.

Terminology:
  MappingCandidate  — a proposed external URI for a local concept
  MappingKind       — SKOS mapping property (exactMatch, closeMatch, etc.)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import AnyUrl, BaseModel, Field

from lakeloom.core.operations import AddMapping, MappingKind, ProposalOperation

_log = logging.getLogger(__name__)

__all__ = [
    "MappingCandidate",
    "SchemaOrgMatch",
    "WikidataMatch",
    "candidates_to_ops",
    "lookup_schema_org",
    "lookup_wikidata",
]

# Type alias for an async "fetch JSON" callable.
# Accepts (url: str, params: dict) → parsed JSON (dict or list).
FetchFn = Callable[[str, dict[str, str]], Awaitable[Any]]

_WIKIDATA_SEARCH_URL = "https://www.wikidata.org/w/api.php"
_SCHEMA_ORG_URL = "https://schema.org/version/latest/schemaorg-current-https.jsonld"

# Exact-match score threshold: if a Wikidata label exactly matches the concept name
# (case-insensitive) the kind is "exactMatch", otherwise "closeMatch".
_EXACT_MATCH_SCORE = 0.95

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WikidataMatch:
    """One Wikidata entity candidate for a local concept."""

    concept_name: str
    entity_id: str  # e.g. "Q5"
    entity_uri: str  # e.g. "http://www.wikidata.org/entity/Q5"
    label: str
    description: str
    match_score: float  # 0.0-1.0; 1.0 = exact label match
    kind: MappingKind  # suggested SKOS mapping kind


@dataclass(frozen=True)
class SchemaOrgMatch:
    """One schema.org type or property candidate."""

    concept_name: str
    type_id: str  # e.g. "schema:Person"
    type_uri: str  # e.g. "https://schema.org/Person"
    label: str
    description: str
    match_score: float
    kind: MappingKind


# Union type for callers that handle both KGs.
MappingCandidate = WikidataMatch | SchemaOrgMatch


# ---------------------------------------------------------------------------
# Score helpers
# ---------------------------------------------------------------------------


def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity on lower-cased word sets."""
    ws_a = set(re.findall(r"\w+", a.lower()))
    ws_b = set(re.findall(r"\w+", b.lower()))
    if not ws_a or not ws_b:
        return 0.0
    return len(ws_a & ws_b) / len(ws_a | ws_b)


def _match_score(query: str, label: str) -> float:
    if query.lower() == label.lower():
        return 1.0
    if query.lower() in label.lower() or label.lower() in query.lower():
        return 0.8
    return _word_overlap(query, label)


def _kind_from_score(score: float) -> MappingKind:
    if score >= _EXACT_MATCH_SCORE:
        return "exactMatch"
    if score >= 0.6:
        return "closeMatch"
    return "relatedMatch"


# ---------------------------------------------------------------------------
# Wikidata
# ---------------------------------------------------------------------------


async def lookup_wikidata(
    term_name: str,
    *,
    fetch: FetchFn,
    lang: str = "en",
    limit: int = 5,
) -> list[WikidataMatch]:
    """Search Wikidata for entities matching ``term_name``.

    Uses the MediaWiki ``wbsearchentities`` API (no authentication required).
    Scores each result by label similarity; keeps only results with score ≥ 0.1.

    Parameters
    ----------
    term_name:
        The local concept name to search for (e.g. ``"Customer"``).
    fetch:
        Async callable ``(url, params) → parsed JSON``.  Injected by the MCP tool.
    lang:
        BCP 47 language code for labels (default ``"en"``).
    limit:
        Maximum API results to request (1-50).
    """
    params = {
        "action": "wbsearchentities",
        "search": term_name,
        "language": lang,
        "limit": str(min(max(1, limit), 50)),
        "format": "json",
        "type": "item",
    }
    try:
        data = await fetch(_WIKIDATA_SEARCH_URL, params)
    except Exception as exc:  # surfaced in logs: an empty result is indistinguishable otherwise
        _log.warning("Wikidata search failed for %r: %s", term_name, type(exc).__name__)
        return []

    results: list[WikidataMatch] = []
    for item in data.get("search", []):
        entity_id = item.get("id", "")
        label = item.get("label", term_name)
        description = item.get("description", "")
        entity_uri = f"http://www.wikidata.org/entity/{entity_id}"
        score = _match_score(term_name, label)
        if score < 0.1:
            continue
        results.append(
            WikidataMatch(
                concept_name=term_name,
                entity_id=entity_id,
                entity_uri=entity_uri,
                label=label,
                description=description,
                match_score=score,
                kind=_kind_from_score(score),
            )
        )

    results.sort(key=lambda m: -m.match_score)
    return results[:limit]


# ---------------------------------------------------------------------------
# schema.org
# ---------------------------------------------------------------------------

# Lightweight static index of schema.org types built from the JSON-LD context.
# Populated lazily by _schema_org_types(); cleared on demand (tests).
# label.lower() → {id, uri, description}; one slot, filled on the first successful download
_schema_org: dict[str, dict[str, dict[str, str]]] = {}


def _text(value: object) -> str:
    """A JSON-LD literal: a plain value or ``{"@value": ...}``."""
    if isinstance(value, dict):
        return str(cast("dict[str, object]", value).get("@value", ""))
    return "" if value is None else str(value)


def _parse_schema_org(jsonld: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Extract type name → {id, uri, description} from a schema.org JSON-LD graph."""
    index: dict[str, dict[str, str]] = {}
    items: list[dict[str, Any]] = jsonld.get("@graph", [])
    for item in items:
        type_id = item.get("@id", "")
        if not type_id.startswith("schema:"):
            continue
        # Only types (rdfs:Class) and properties (rdf:Property).
        raw_type: object = item.get("@type", "")
        types = cast("list[object]", raw_type) if isinstance(raw_type, list) else [raw_type]
        if not {"rdfs:Class", "rdf:Property"} & {str(t) for t in types}:
            continue
        label = _text(item.get("rdfs:label"))
        if not label:
            continue
        uri = f"https://schema.org/{type_id.removeprefix('schema:')}"
        index[label.lower()] = {
            "id": type_id,
            "uri": uri,
            "description": _text(item.get("rdfs:comment")),
        }
    return index


async def lookup_schema_org(
    term_name: str,
    *,
    fetch: FetchFn,
    limit: int = 5,
) -> list[SchemaOrgMatch]:
    """Search schema.org for a type or property matching ``term_name``.

    Downloads the full schema.org JSON-LD on the first call; subsequent calls
    reuse the in-process cache.

    Parameters
    ----------
    term_name:
        The local concept name to search for.
    fetch:
        Async callable ``(url, params) → parsed JSON``.
    limit:
        Maximum results to return.
    """
    index = _schema_org.get("index")
    if index is None:
        try:
            index = _parse_schema_org(await fetch(_SCHEMA_ORG_URL, {}))
        except Exception as exc:  # a failed download is retried on the next call, not cached
            _log.warning("schema.org download failed: %s", type(exc).__name__)
            return []
        _schema_org["index"] = index
    results: list[SchemaOrgMatch] = []
    for entry in index.values():
        label = entry["id"].removeprefix("schema:")
        score = _match_score(term_name, label)
        if score < 0.1:
            continue
        results.append(
            SchemaOrgMatch(
                concept_name=term_name,
                type_id=entry["id"],
                type_uri=entry["uri"],
                label=label,
                description=entry["description"],
                match_score=score,
                kind=_kind_from_score(score),
            )
        )

    results.sort(key=lambda m: -m.match_score)
    return results[:limit]


def clear_schema_org_cache() -> None:
    """Clear the in-process schema.org cache (used in tests)."""
    _schema_org.clear()


# ---------------------------------------------------------------------------
# Convert approved candidates → ProposalOperations
# ---------------------------------------------------------------------------


class ApprovedMapping(BaseModel):
    """One approved mapping submitted by the user for a proposal."""

    concept_fqn: str = Field(description="FQN of the local concept (e.g. 'retail.Customer').")
    target_uri: str = Field(description="External entity URI (from suggest_external_mappings).")
    kind: MappingKind = Field(
        default="closeMatch",
        description="SKOS mapping kind (exactMatch, closeMatch, etc.).",
    )


def candidates_to_ops(
    approved: list[ApprovedMapping],
    rule_id: str = "R-ENTITY-LINK",
) -> list[ProposalOperation]:
    """Convert approved mapping candidates to :class:`~lakeloom.core.operations.AddMapping` ops.

    Parameters
    ----------
    approved:
        List of :class:`ApprovedMapping` objects (concept FQN + URI + kind).
    rule_id:
        Rule ID recorded in the operation's ``rule_ids`` list.

    Raises ``ValueError`` if ``target_uri`` is not a valid URL.
    """
    ops: list[ProposalOperation] = []
    for m in approved:
        try:
            uri = AnyUrl(m.target_uri)
        except Exception as exc:
            raise ValueError(
                f"invalid URI for concept {m.concept_fqn!r}: {m.target_uri!r}"
            ) from exc
        ops.append(
            AddMapping(
                op="add_mapping",
                concept=m.concept_fqn,
                kind=m.kind,
                target_iri=uri,
                rule_ids=[rule_id],
            )
        )
    return ops
