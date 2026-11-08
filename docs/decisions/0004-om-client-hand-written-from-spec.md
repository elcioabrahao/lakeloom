# 0004. OM client hand-written from the committed spec

- Status: accepted
- Date: 2026-11-08
- Requirements: FR-OM-01, FR-OM-06, §0.4

## Context

M0 needs two OpenMetadata endpoints: `GET /v1/system/version` and
`GET /v1/rdf/status`. Generating a client from the 4.5 MB spec would add a large
generated surface and a build step for two calls.

## Decision

- M0 hand-writes a thin async `httpx` client (`lakeloom.om.client.OMClient`) plus
  pydantic models (§0.4 allows this). Paths are spec keys (`servers[0].url` is `/api`).
- `tests/unit/om/test_spec_paths.py` is the guard: every `(method, path)` in
  `OMClient.PATHS` must exist in the committed spec.
- Spec differences recorded: `/v1/rdf/status` declares an untyped 200 body, so
  `RdfStatus` is modelled from the live 2.1.0-rc1 response (`enabled`, `baseUri`,
  `storageType`, `projectionState`, `aiEnabled`, ...) with `extra="allow"`.
  `/v1/system/version` only declares a `default` response; `OMVersion` follows
  `OpenMetadataServerVersion`.
- Errors: connect errors, timeouts and 5xx become `OMUnavailable`; other 4xx become
  `OMError` with `status`. `get_capabilities` never raises; a 404 on RDF status
  yields `rdf_enabled=None` (FR-OM-06).
- All requests go through one private `_request` method (metrics hook point later).

## Consequences

We reconsider code generation when the client surface grows (M2).
