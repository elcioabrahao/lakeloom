# M1c deferred follow-ups

Minor findings from the M1c task and whole-branch reviews, deliberately deferred. Triage when planning M2.

## HTTP polish (small, M2 or M4)
- OpenAPI lists 422 responses for `/v1` (the API returns typed 400) and has no `securitySchemes` (bearer) — cheap FR-PUB-08 accuracy wins.
- HEAD returns 405; unknown `/v1/...` paths return FastAPI's `{"detail":"Not Found"}` instead of the typed error body.
- The 400 hint is the same for every bad query parameter.
- 500 responses use code `upstream_unavailable` (an `internal_error` code needs a spec decision; the same applies to tools and scans).
- `/v1/models/{slug}/versions` loads and exports the graph just for Last-Modified/Link; `get_model` calls OM even on requests that end in 304.
- Models list silently caps at 50 pages × 200 models.
- Add `LAKELOOM_OM_PUBLIC_URL` for Link headers (today they show the internal OM host, e.g. `http://openmetadata-server:8585/...`); M2 replaces them with Git links anyway.
- No inbound `traceparent` extraction: HTTP server spans are always roots.

## Negotiation / validators edge cases
- Quoted commas inside Accept parameters split incorrectly; the non-numeric `q` test doesn't discriminate.
- `ModelService.om_url` rebuild drops a query string; IPv6 hosts untested; `loaded_wall` not on the injected clock.
- A model's Last-Modified can move forward on a graph reload with unchanged content (the ETag stays authoritative).

## MCP resources
- Extra path segments return `validation_error` instead of `not_found`.
- Resources registered through the SDK (`add_resource`) are ignored by the `LakeloomMCP` router.
- Forbidden `resources/list` calls are not counted; forbidden reads use `resource="any"`.
- `forbidden` maps to the JSON-RPC internal-error code (the real code is in `data.error.code`).

## stdio
- `build_deps(http_auth=False)` deps are not structurally rejected by `create_server_app` (fails closed today via the placeholder issuer).
- The stdio scope check also runs in HTTP mode; an empty scope list is accepted; add a test for an explicit `LAKELOOM_STDIO_ENABLED=false` in development.

## Observability
- Tool and resource spans are marked ERROR for client-caused outcomes (`not_found`, `validation_error`); align with the OM rule (errors only for 5xx/transport).

## Tests
- M1 acceptance asserts table versions only as `is not None`.
