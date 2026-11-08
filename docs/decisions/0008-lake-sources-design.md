# 0008. Lake source design

- Status: accepted
- Date: 2026-11-08
- Requirements: FR-SRC-01, FR-SRC-05, FR-SRC-06, FR-SRC-08, FR-SRC-09, FR-AGT-05, NFR-SEC-13, NFR-SEC-14

## Context

M1a adds read-only lake connectors (Delta, Unity Catalog, upstream MCP, plugins) that feed
scans of table metadata and profiles.

## Decision

- **Protocol.** Connectors implement `lakeloom.sources.base.LakeSource` (async, read-only)
  and declare `SourceCapabilities` (profiling, versions, constraints). Models are frozen
  pydantic classes; lake-provided comments are `UntrustedText` and rendered with
  `quote_untrusted` (NFR-SEC-14).
- **Configuration.** Operators write a YAML file (`sources:` list discriminated by `type`:
  `delta`, `unity_catalog`, `mcp`; any other type is a plugin). Agents never supply sources
  or URLs (NFR-SEC-13). Credentials are referenced by environment-variable name
  (`token_env`) and resolved at build time, never stored in the model.
- **Plugins.** Unknown types are looked up in the `lakeloom.sources` entry-point group;
  the object is `Callable[[PluginSourceConfig, Settings], LakeSource]`. Built-ins live in
  modules exposing `build_source(config, settings)`. Unknown types raise `ConfigError`.
- **Unity Catalog over REST.** The connector uses the UC REST API through httpx rather than
  `databricks-sdk`, which keeps the base install small and works against Unity Catalog OSS.
  Cloud SDKs stay optional; `deltalake` + `pyarrow` live in the `delta` extra.
- **Errors.** `lakeloom.errors.LakeloomError` carries the six FR-AGT-05 codes with a hint;
  `to_dict()` is the tool error shape.
- **New settings.**

  | Variable | Default |
  |---|---|
  | `LAKELOOM_PROFILE_TOPK` | 10 |
  | `LAKELOOM_PROFILE_LOW_CARDINALITY` | 50 (distinct values) |
  | `LAKELOOM_PROFILE_VALUE_MAX_LENGTH` | 64 (characters) |
  | `LAKELOOM_SCAN_CONCURRENCY` | 8 |

- **Delta key/tag conventions:** see Task 2.

## Consequences

New connectors need no core changes. The base install runs without the `delta` extra; the
Delta connector reports a clear "install lakeloom[delta]" error instead.
