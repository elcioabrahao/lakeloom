# M1c — Publication Read Surfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Software and agents can read ontology models, concepts and asset context through MCP resources and a read-only HTTP API with content negotiation and cache headers; Lakeloom also gains OpenTelemetry and the local stdio transport.

**Architecture:** A small publication layer (`publish/`) turns the M1b core results (ModelService, concepts, asset context) into negotiated representations with ETag/Last-Modified/Link metadata. The HTTP API (`server/http_api.py`, FastAPI router under `/v1`) and MCP resources (`server/resources.py`) are thin adapters over that layer, so both surfaces serve identical bytes. Observability (OTel traces/metrics) and the stdio transport are wired at the app/CLI edge.

**Tech Stack:** existing stack + `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-instrumentation-asgi` (or manual spans), MCP SDK resources and stdio server.

**Spec:** `REQUIREMENTS.md` — FR-PUB-01, FR-PUB-02, FR-PUB-07, FR-PUB-08, NFR-OPS-02, NFR-SEC-04, NFR-SEC-01/06 (auth on the API), FR-AGT-01/05. Part 3 of 3 of M1 (M1a sources/scans and M1b semantic read are merged). Completing it completes M1 ("MCP resources, HTTP read API").

## Decisions already taken

- Releases arrive in M3: every `version` parameter accepts only `latest` (or omitted); any other value → `not_found` with hint "releases are published from M3; use version=latest". `GET /v1/models/{slug}/versions` lists only `latest`.
- `shapes.ttl` (SHACL, FR-PUB-04) is M3: no route and no resource yet (documented in README).
- `context.jsonld` (FR-PUB-05 proper) is M3; M1c serves the minimal context from M1b (`@vocab` = base IRI + model prefixes + `dcterms`, `skos`) at the M1c endpoints and replaces it in M3.
- `Link` header (FR-PUB-07 "to the Git commit or tag it came from"): Git publishing starts in M2, so `latest` artefacts link to their OM source: `Link: <{om_url}/glossary/{slug}>; rel="via"` (and for asset context `<{om_url}/table/{fqn}>; rel="via"`). ADR 0010.
- FR-PUB-06 (dereferenceable IRIs, MAY) is not in M1c.
- Concept name resolution ranks exact name (1.0) > synonym (0.95) > local name (0.9) and resolves when the top tier is unique (from `docs/backlog/m1b-deferred.md`), because the HTTP `/v1/concepts/{iri-or-id}` route and resources rely on it.

## Global Constraints

- All M0/M1a/M1b Global Constraints apply (Python ≥ 3.12; uv/ruff/pyright strict on `src/`; Conventional Commits + `git commit -s` + requirement IDs; identity `Elcio Abrahao <elcio@tuta.com>`; typed errors with hints; untrusted text quoted in Markdown; spec-path test for OM endpoints; no tool/route takes URLs).
- FR-PUB-02 routes verbatim: `GET /v1/models`, `GET /v1/models/{slug}`, `GET /v1/models/{slug}/versions`, `GET /v1/models/{slug}/versions/{version}`, `GET /v1/models/{slug}/shapes` (M3 — omitted now), `GET /v1/models/{slug}/context.jsonld`, `GET /v1/concepts/{iri-or-id}`, `GET /v1/assets/{fqn}/context`.
- Content negotiation media types verbatim: "`Accept: text/turtle`, `application/ld+json`, `application/rdf+xml`, `application/n-triples`, `text/markdown`, `application/json`". Unsupported Accept → 406 with the supported list. No Accept / `*/*` → default per route (models & versions: `text/turtle`; concepts and asset context: `text/markdown`; lists: `application/json`).
- FR-PUB-01 resources verbatim: `lakeloom://models` (list), `lakeloom://models/{slug}/ontology.{ext}` (ext = `ttl`, `jsonld`, `owl`, `nt`), `lakeloom://models/{slug}/shapes.ttl` (M3 — omitted), `lakeloom://models/{slug}/context.jsonld`, `lakeloom://assets/{fqn}/context.md`; "Resources support release pinning (`?version=1.4.0`)".
- FR-PUB-07: "Every published artefact has an `ETag`, `Last-Modified`, and a `Link` header". ETag = strong, `"sha256:<hex of body>"`; Last-Modified = the model's OM `updatedAt` (glossary) or the table's `updatedAt` (asset context), RFC 7231 format; support `If-None-Match` → 304.
- Auth: every `/v1` route requires a valid bearer token with `lakeloom:read` (NFR-SEC-01/06); the M0 middleware already protects non-public paths; scope check per route → 403 `forbidden`.
- FR-PUB-08: "An OpenAPI spec is generated and published for the HTTP API." → committed `docs/api/openapi.json` generated from the app, with a drift test.
- NFR-OPS-02: "OpenTelemetry traces and metrics (OTLP exporter, off unless configured)". Off = no exporter, no-op tracer when `LAKELOOM_OTEL_EXPORTER_OTLP_ENDPOINT` is unset.
- NFR-SEC-04: "stdio transport (local use) uses credentials from environment/config and is treated as the configured local principal; it MUST be disabled in container deployments unless explicitly enabled." New settings: `LAKELOOM_STDIO_PRINCIPAL` (default `local`), `LAKELOOM_STDIO_SCOPES` (default `["lakeloom:read"]`; JSON or comma list). The image already sets `LAKELOOM_STDIO_ENABLED=false`.

## Review Focus

1. **Accept headers from real clients** (`Accept: text/turtle;q=0.9, application/ld+json`, `*/*;q=0.1`, `application/*`, malformed, multiple values): the highest-q supported type wins; ties keep the route's preference order; malformed → default, never 500. → Task 1 tests.
2. **FQNs and IRIs in URL paths** (dots, quotes, `#`, `/` in IRIs, percent-encoding): `/v1/concepts/{iri-or-id}` and `/v1/assets/{fqn}/context` accept percent-encoded IRIs/FQNs (path param `:path`) and resolve exactly; resource URIs `lakeloom://assets/{fqn}/context.md` likewise. → Tasks 2 and 3 tests.
3. **Conditional requests**: `If-None-Match` with the current ETag → 304 with no body and the same ETag; a stale ETag → 200; weak/`*` ETags handled per RFC 9110. → Task 2 tests.
4. **Unknown model/version/concept/asset, OM down**: 404/503 with the typed error JSON body (`{"error":{"code","message","hint"}}`), never HTML or a stack trace; resources return MCP errors with the same codes. → Tasks 2 and 3 tests.
5. **stdio in a container or production**: refused with a clear message unless `LAKELOOM_STDIO_ENABLED=true`; the stdio principal never gets scopes beyond `LAKELOOM_STDIO_SCOPES`. → Task 4 tests.

## File structure

```
src/lakeloom/publish/__init__.py
src/lakeloom/publish/negotiation.py   # Accept parsing, media-type ↔ format
src/lakeloom/publish/artifacts.py     # Artifact(body, media_type, etag, last_modified, links); builders
src/lakeloom/server/http_api.py       # /v1 router
src/lakeloom/server/resources.py      # MCP resources
src/lakeloom/server/stdio.py          # stdio runner with local principal
src/lakeloom/telemetry.py             # OTel setup (traces + metrics), no-op when unset
docs/api/openapi.json
docs/decisions/0010-publication-read-surfaces.md
```

---

### Task 1: Publication layer — negotiation and artefacts

**Files:** Create `src/lakeloom/publish/{__init__,negotiation,artifacts}.py`, `docs/decisions/0010-publication-read-surfaces.md`; Modify `src/lakeloom/core/concepts.py` (name ranking); Test `tests/unit/publish/test_negotiation.py`, `tests/unit/publish/test_artifacts.py`, `tests/unit/core/test_concepts.py`

**Interfaces:**
- Consumes: M1b `ModelService` (`get_model`, `export`, graph), `concepts.explain_concept` + `concept_markdown`, `context.get_asset_context` + `render_markdown/render_json/render_jsonld` (each returns `Rendered(payload, token_estimate, truncated)`), `rdf.canonical.MEDIA_TYPES`/`ExportFormat`.
- Produces:
  - `negotiation.MediaType = Literal["text/turtle","application/ld+json","application/rdf+xml","application/n-triples","text/markdown","application/json"]`; `def choose(accept: str | None, offered: Sequence[MediaType]) -> MediaType | None` (RFC 9110 q-values, wildcards `*/*` and `type/*`, ties → order of `offered`; `None` only when nothing offered is acceptable; empty/absent/malformed Accept → `offered[0]`); `FORMAT_BY_MEDIA: dict[MediaType, ExportFormat]` (`text/turtle`→turtle, `application/ld+json`→jsonld, `application/rdf+xml`→rdfxml, `application/n-triples`→ntriples).
  - `artifacts.Artifact(body: bytes, media_type: str, etag: str, last_modified: datetime, links: list[str])` (frozen pydantic); `def etag_for(body: bytes) -> str` (`'"sha256:' + hexdigest + '"'`); `def matches_if_none_match(header: str | None, etag: str) -> bool` (handles `*`, comma lists, `W/` prefix compared weakly).
  - Builders (async): `model_artifact(models, slug, media: MediaType, *, version: str | None) -> Artifact`; `model_context_artifact(models, slug, *, version) -> Artifact` (minimal JSON-LD context, `application/ld+json`); `concept_artifact(models, om, concept: str, media: Literal["text/markdown","application/json"]) -> Artifact`; `asset_context_artifact(models, om, sources, fqn: str, media: Literal["text/markdown","application/json","application/ld+json"]) -> Artifact`; `models_list_artifact(models) -> Artifact` (JSON). `version` ∉ {None, "latest"} → `LakeloomError("not_found", …, hint="releases are published from M3; use version=latest")`. `last_modified` from OM `updatedAt` (add `updated_at: int|None` (epoch ms) to `OMGlossary`/`OMTable` models if missing); `links` per Decisions.
  - Concept name ranking in `concepts._resolve_by_name`: top tier = highest score among hits ≥ 0.9; resolve if exactly one hit in the top tier; else `validation_error` listing candidates.
- [ ] **Step 1: Tests:** negotiation table-driven (`text/turtle;q=0.9, application/ld+json` → jsonld; `*/*;q=0.1` → offered[0]; `application/*` with offered [turtle, jsonld] → jsonld (first `application/…` in offered order); `text/html` only → None; garbage → offered[0]; q=0 excludes); `test_fr_pub_07_etag_is_sha256_of_body`; `test_if_none_match_variants` (`*`, list, `W/"…"`); `test_model_artifact_formats_and_links` (fake ModelService: body = export bytes, media type, Link `rel="via"`, Last-Modified from updatedAt); `test_version_other_than_latest_not_found`; `test_concept_name_exact_beats_synonym` (name "Order" exact for Order and synonym of another → resolves Order).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass; ruff + pyright clean.
- [ ] **Step 5:** ADR 0010 (Link to OM until Git, version=latest only until M3, minimal context.jsonld, ETag scheme, defaults per route).
- [ ] **Step 6: Commit** `feat(publish): content negotiation and cache-validated artefacts (FR-PUB-02, FR-PUB-07)`.

---

### Task 2: Read-only HTTP API + OpenAPI

**Files:** Create `src/lakeloom/server/http_api.py`, `docs/api/openapi.json`; Modify `src/lakeloom/server/http_app.py` (include router; keep `/openapi.json` behind auth as today), `README.md` (HTTP API section); Test `tests/unit/server/test_http_api.py`, `tests/contract/test_http_api.py`

**Interfaces:**
- Consumes: Task 1 builders, M0 auth (`Principal` in `request.state`, `SCOPES`), AppDeps (`models`, `om`, `sources`).
- Produces routes (FR-PUB-02, all GET, scope `lakeloom:read`):
  - `/v1/models` → JSON list (`ModelInfo` items, cursor/limit query) — offered `[application/json]`.
  - `/v1/models/{slug}` → model ontology — offered `[text/turtle, application/ld+json, application/rdf+xml, application/n-triples]`.
  - `/v1/models/{slug}/versions` → JSON `[{"version":"latest","media_types":[…]}]`.
  - `/v1/models/{slug}/versions/{version}` → same as `/v1/models/{slug}` for `latest`, else 404.
  - `/v1/models/{slug}/context.jsonld` → minimal context (`application/ld+json`; also served for `application/json`).
  - `/v1/concepts/{concept:path}` (IRI, FQN, id or name; percent-decoded) → offered `[text/markdown, application/json]`.
  - `/v1/assets/{fqn:path}/context` → offered `[text/markdown, application/json, application/ld+json]`; query `max_tokens`.
  - Every 200 sets `ETag`, `Last-Modified`, `Link`, `Content-Type` (with `; charset=utf-8` for text types), `Vary: Accept`, `Cache-Control: private, max-age=0, must-revalidate`; `If-None-Match` match → 304 (no body, same validators). Errors → JSON `{"error":{…}}` with status: not_found 404, forbidden 403, validation_error 400, upstream_unavailable 503, rate_limited 429, conflict 409; 406 for unacceptable Accept with `{"error":{"code":"validation_error","message":"not acceptable","hint":"supported: …"}}`.
  - `docs/api/openapi.json` = `json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"` from an app built with test deps; drift test regenerates and compares.
- [ ] **Step 1: Tests (TestClient + fake deps):** one per route incl. negotiation and headers; `test_if_none_match_304`; `test_unacceptable_accept_406`; `test_requires_read_scope_403`; `test_no_token_401` (existing middleware); `test_percent_encoded_iri_and_fqn_paths` (`/v1/concepts/http%3A%2F%2Flocalhost%3A8000%2Fontology%2Fretail%2FOrder`, `/v1/assets/lakeloom_fixture.retail.sales.orders/context`); `test_errors_are_json_not_html` (unknown model 404 JSON; OM down 503 JSON); `test_fr_pub_08_openapi_committed_matches`. Contract: `test_http_models_turtle_parses_and_etag_stable` (two GETs same ETag; If-None-Match 304), `test_http_orders_context_markdown_lists_column_concepts`, `test_http_concept_order_json`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass (rebuild lakeloom for contract); ruff + pyright clean.
- [ ] **Step 5: Commit** `feat(http): read-only HTTP API with content negotiation and OpenAPI (FR-PUB-02, FR-PUB-07, FR-PUB-08)`.

---

### Task 3: MCP resources

**Files:** Create `src/lakeloom/server/resources.py`; Modify `src/lakeloom/server/mcp_app.py`; Test `tests/unit/server/test_resources.py`, `tests/contract/test_mcp_resources.py`

**Interfaces:**
- Consumes: Task 1 builders; MCP SDK 2.x resource/resource-template API (check installed SDK: static resources, URI templates, `resources/list`, `resources/templates/list`, `resources/read`, MIME types, query params in template URIs — if templates can't carry `?version=`, parse the query string from the requested URI yourself).
- Produces:
  - `lakeloom://models` → JSON list (`application/json`).
  - `lakeloom://models/{slug}/ontology.{ext}` → ext `ttl`→turtle, `jsonld`→jsonld, `owl`→rdfxml, `nt`→ntriples; MIME from Task 1.
  - `lakeloom://models/{slug}/context.jsonld` → minimal context.
  - `lakeloom://assets/{fqn}/context.md` → Markdown asset context (FQN percent-encoded in the URI).
  - `?version=latest` accepted; other versions → MCP error with code/hint from LakeloomError; unknown ext → `validation_error`.
  - Resource reads require `lakeloom:read` (principal from request state, same as tools); bytes identical to the HTTP API for the same representation (shared builders).
  - `resources/list` lists `lakeloom://models` and, per model, its concrete ontology/context URIs (bounded to 100 models).
- [ ] **Step 1: Tests:** unit with the in-process MCP client (`tests/unit/server/kit.py`): list contains `lakeloom://models` and the four ontology URIs for a fake model; read each ext → expected MIME and bytes equal to `models.export`; `?version=1.0.0` → error `not_found` with hint; unknown ext → `validation_error`; `test_resource_requires_read_scope`; `test_asset_context_resource_percent_encoded_fqn`. Contract: read `lakeloom://models/retail/ontology.ttl` → parses with rdflib; bytes equal `GET /v1/models/retail` with `Accept: text/turtle`; `lakeloom://assets/lakeloom_fixture.retail.sales.orders/context.md` lists the five column concepts.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5: Commit** `feat(mcp): MCP resources for models and asset context (FR-PUB-01)`.

---

### Task 4: stdio transport with the local principal

**Files:** Create `src/lakeloom/server/stdio.py`; Modify `src/lakeloom/config.py` (`stdio_principal: str = "local"`, `stdio_scopes: list[str] = ["lakeloom:read"]` with NoDecode JSON/comma parsing and validation ⊆ `SCOPES`), `src/lakeloom/cli.py` (`lakeloom serve --transport http|stdio`, default http), `src/lakeloom/server/mcp_app.py` / `tools_common.py` (principal lookup falls back to a context-local principal when there is no HTTP request), README (Claude Code/desktop config example for stdio); Test `tests/unit/server/test_stdio.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `async def run_stdio(deps: AppDeps) -> None` — builds the same MCP server (tools + resources), sets the local `Principal(subject=settings.stdio_principal, type=HUMAN, client_id=None, username=settings.stdio_principal, scopes=frozenset(settings.stdio_scopes), groups=frozenset(), token=SecretStr(""))` in a contextvar used by `guarded()`/resources when no HTTP request is present, runs the app lifespan pieces needed (sources start/close, migrations) and serves stdio. `lakeloom serve --transport stdio`: if `not settings.stdio_enabled` → stderr "stdio transport is disabled (set LAKELOOM_STDIO_ENABLED=true)" and exit 2. Logs go to stderr only (stdout is the protocol stream).
- [ ] **Step 1: Tests:** `test_nfr_sec_04_stdio_refused_when_disabled` (production env → exit 2 with message); `test_stdio_principal_scopes_from_settings` (scopes outside SCOPES → ConfigError); `test_stdio_session_end_to_end` (spawn `python -m lakeloom.cli serve --transport stdio` with SQLite + fake/unused OM settings via the MCP stdio client; `tools/list` includes `get_server_info`; `call_tool("get_server_info")` → `caller.subject == "local"`); `test_stdio_scan_forbidden_without_propose`; `test_no_stdout_logging_on_stdio`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5: Commit** `feat(server): stdio transport with configured local principal (NFR-SEC-04)`.

---

### Task 5: OpenTelemetry traces and metrics

**Files:** Create `src/lakeloom/telemetry.py`; Modify `src/lakeloom/server/http_app.py` (lifespan setup/shutdown, ASGI tracing middleware), `src/lakeloom/server/tools_common.py` (span per tool call: name `mcp.tool/<tool>`, attributes `lakeloom.tool`, `lakeloom.outcome`, `lakeloom.principal_type`), `src/lakeloom/om/client.py` (span per OM request: `om.<operation>`, `http.status_code`), `src/lakeloom/logging.py` (add `trace_id`/`span_id` to JSON log records when a span is active), `pyproject.toml`, README (observability section); Test `tests/unit/test_telemetry.py`

**Interfaces:**
- Produces: `def configure_telemetry(settings: Settings) -> TelemetryHandle` (no-op handle when `otel_exporter_otlp_endpoint` is None: no exporter threads, global no-op tracer); when set: `TracerProvider` with `BatchSpanProcessor(OTLPSpanExporter(endpoint=…/v1/traces))`, `MeterProvider` with `PeriodicExportingMetricReader(OTLPMetricExporter(…/v1/metrics))`, resource `service.name=lakeloom`, `service.version=__version__`; `TelemetryHandle.shutdown()` flushes. OTel metrics mirror the Prometheus ones (tool calls by name/outcome, OM latency histogram) — Prometheus `/metrics` stays.
- [ ] **Step 1: Tests:** `test_nfr_ops_02_off_by_default` (no endpoint → tracer is no-op; no exporter created); `test_spans_for_tool_and_om_calls` (in-memory span exporter injected via a test hook → a tool call produces `mcp.tool/get_server_info` with outcome attribute and a child `om.get_capabilities`); `test_log_record_has_trace_id_inside_span`; `test_no_token_in_span_attributes`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5: Commit** `feat(ops): OpenTelemetry traces and metrics, off unless configured (NFR-OPS-02)`.

---

### Task 6: M1 wrap-up

**Files:** Modify `README.md` (M1 status, surfaces overview table: tools / resources / HTTP routes), `CHANGELOG.md` (`[Unreleased]` M1c entries), `docs/backlog/m1c-deferred.md` (created by the controller at the end), `tests/contract/test_m1_acceptance.py` (single module re-asserting the full M1 acceptance through all three surfaces)

- [ ] **Step 1:** `tests/contract/test_m1_acceptance.py`: (a) scan of `fixture_delta` returns all four tables with versions (MCP); (b) asset context for orders lists the five column concepts via MCP tool, MCP resource and HTTP; (c) for each of the four RDF formats, the `export_model` tool output, the HTTP `GET /v1/models/retail` body (matching `Accept`) and the MCP resource read are byte-identical and parse with rdflib; the SKOS format via the tool parses as Turtle. (Fresh re-export determinism is already covered by M1b's service-level contract test.)
- [ ] **Step 2:** Run the full suites; rebuild stack; all green.
- [ ] **Step 3: Commit** `test(contract): M1 acceptance across tools, resources and HTTP; docs (M1)`.

---

## Out of scope for M1c

SHACL shapes and `context.jsonld` generation, releases and version pinning beyond `latest`, SPARQL proxy, dereferenceable IRIs (M3); proposals/change sets/Git (M2); rate limiting, Helm (M4).
