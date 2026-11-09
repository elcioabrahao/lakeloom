# M1a deferred follow-ups

Minor findings from the M1a task and whole-branch reviews that were deliberately deferred. Triage when planning M1b/M1c.

## Decide before M1b
- OM fixture tags are lossy: `PII.Email` / `PII.Name` are seeded as OM's built-in `PII.Sensitive`, and `Domain.*` table tags are dropped. Seed a non-exclusive `Lakeloom` classification with the exact tags if M1b's asset context or realization logic needs them.

## Suggested for M1b
- Normalise column `position` to one base across sources (Delta/UC 0-based, MCP/OM 1-based) and document it.
- CLI `tables` / `sources` start every MCP source even when only a Delta source is named (up to 15 s).
- A scan started while the state store is still migrating runs fully and then fails on write; check readiness first.
- Scan enumeration duplicates `core/sources.list_all_tables`; `list_catalogs` is called twice.
- `list_tables` re-enumerates the source on every page and the cursor is not bound to the filters; dotted names can't round-trip through `parse_table_name`; the OM mapping's `fqn: "{om_service}.{full_name}"` is unquoted.
- Delta connector: nested Arrow types render bare (`array`/`struct`/`map`); `committed_at` is None without `commitInfo`; `get_table_version` does a full describe; duplicate table entries overwrite silently; malformed FK targets (log and overlay) are skipped silently; empty overlay comment vs empty description handled differently.
- Profiling: ns timestamps raise without pandas; `samples_disabled` flag also set on numeric columns; file-wide pyright relaxation in `profiling.py`; NaN counted in top-k.
- MCP adapter: `structuredContent` / HTTP JSON body size is not capped by the SDK (documented in ADR 0008); `tools/list` paging has no repeated-cursor check; a capped column list can exceed `MAX_ITEMS`; use a read-only OM bot for the MCP source (dev uses ingestion-bot, which can write).
- MCP source start delays the lifespan by up to 15 s when an upstream is unreachable; consider a background start.
- Non-Lakeloom exceptions are labelled `upstream_unavailable` (consider an `internal_error` code — needs a spec decision).
- Migration retry keeps reporting "schema upgrade pending" on non-connection errors; report the last error class instead. ULIDs are not monotonic within one millisecond.
- UC: `storage_root_map` normalisation treats the bucket/host as part of the path (`s3://b/../x` → `s3://x`); symlinks inside a mapped root are followed. Seed scripts: PUT paths not in `OMClient.PATHS`, `ALREADY_EXISTS` check not status-restricted, raw tracebacks on connection errors.
- GFM autolinks bare URLs inside quoted untrusted text (click required; no auto-fetch).

## Release hardening (M4)
- `storage_options` in the sources YAML is a plaintext dict; recommend ambient or `_FILE` credentials.
- UC OSS state lives inside its container (re-seed with `docker compose ... run --rm uc-init`); om-seed, uc-init and lakeloom all build `lakeloom:dev` (extra CI time); the `delta` extra adds ~290 MB to the image.
