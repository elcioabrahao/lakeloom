# M1b deferred follow-ups

Minor findings from the M1b task and whole-branch reviews, deliberately deferred. Triage when planning M1c/M2.

## Suggested for M1c
- Name resolution (`explain_concept`, `find_assets_for_concept`): rank exact name (1.0) over synonym (0.95) over local name (0.9) and resolve when the top tier is unique; today any two matches ≥ 0.9 return `validation_error` with candidates.
- JSON/JSON-LD `max_tokens`: columns are chosen first and sections dropped only when no column fits; consider dropping relationships before columns (Markdown keeps leading columns).
- Realization cache (`concepts._caches`) holds a bound method of its ModelService, so entries are never collected (harmless with one service per process).
- `find_concepts` / `find_assets_for_concept` structured results carry no `token_estimate` (lists are bounded by `limit`).
- Descendant asset cap is soft (each batch term gets the full remaining budget); truncated walks are not cached.
- `get_table_by_fqn` returning None includes the asset unverified; glossary slugs are assumed dot-free; filtered asset pages can be empty with a `next_cursor`.
- `find_assets_for_concept` cursors don't encode `include_descendants` (switching mid-pagination misaligns).
- Tool limits (`limit`, `max_tokens`) are validated in code but not expressed as JSON-schema bounds.
- Export stability at the tool level is tested within the cache TTL (fresh re-export determinism is covered at the service level).
- Duplication to consolidate: `_upstream` (3 copies), `_CONCURRENCY` (2), attribute-line rendering (2), `_sorted_refs` (2).

## Before M2 (writes)
- Call `ModelService.invalidate(slug)` after Lakeloom applies a change set; document the staleness window for direct OM edits (model TTL 60 s, negative table lookups 10 s — README notes it).
- Seed `BASE_IRI` (`http://localhost:8000/ontology/retail/`) must equal `LAKELOOM_PUBLIC_URL`-based base IRIs; OM rc1 doesn't persist glossary namespaces.

## Hardening
- `export_ontology` doesn't check the response Content-Type; `get_term_assets` 404 raises (undocumented); `OMTermRelation.term` is required; lineage edges are `str | ref`.
- RDF: `filterwarnings` entries not module-scoped; Hypothesis alphabet excludes control characters; SAX `<unknown>:` prefix in messages; Turtle errors have no column.
- Seed scripts: no JSON Patch `test` op guarding column indices; partial-failure convergence undocumented.
- ModelService: concurrent cold loads each call `get_model`; padded-but-valid base64 cursors are forwarded to OM; `graph()` returns the shared cached graph (don't mutate).
- Markdown unit/allowed-value attribute path is untested (fixture has none).
