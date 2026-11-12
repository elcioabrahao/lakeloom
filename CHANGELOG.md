# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository skeleton, tooling (uv, ruff, pyright, pytest) and community files.
- `lakeloom version` CLI command.
- Settings from environment variables and `*_FILE` secrets.
- OpenMetadata client (async, OpenAPI-checked).
- OIDC bearer-token authentication with RFC 9728 protected-resource metadata.
- Health, readiness and Prometheus metrics endpoints, and JSON logs.
- MCP server over Streamable HTTP with the `get_server_info` tool.
- Container image (non-root, read-only-filesystem compatible).
- Docker Compose dev stack (OpenMetadata 2.1.0-rc1, Keycloak, Gitea, PostgreSQL, optional RDF profile).
- Contract tests against the dev stack.
- CI: lint, typecheck, unit (Python 3.12 and 3.13), contract and image jobs with dependency audit,
  SBOM, provenance and Trivy scan; DCO check; Dependabot.
- M1a lake sources: `LakeSource` protocol, operator YAML config (`LAKELOOM_CONFIG_FILE`) and
  `lakeloom.sources` plugin entry points; Delta storage (delta-rs, extra `delta`), Unity Catalog
  (REST API) and upstream MCP (mapping YAML; OpenMetadata mapping shipped, Databricks and
  Snowflake as examples to verify) connectors.
- Streaming column profiles computed in place, with sample values off by default, sensitive-tag
  exclusion and value truncation.
- Scan service and scan record store (Alembic migrations) with per-table errors and
  OpenMetadata lookup by FQN.
- MCP tools `list_sources`, `list_tables`, `describe_table` and `scan_lake_source`;
  `get_server_info` now lists the configured sources with their availability.
- CLI commands `lakeloom sources`, `lakeloom tables SOURCE` and `lakeloom scan SOURCE`, plus
  `lakeloom db upgrade`.
- Dev stack: Unity Catalog OSS, fixture seeding for OpenMetadata and Unity Catalog, and three
  configured sources (`fixture_delta`, `fixture_uc`, `openmetadata_mcp`).
- A warning at startup when an upstream MCP tool used by a mapping is not annotated read-only.
- M1b semantic read path: model service over OpenMetadata glossaries with deterministic RDF
  export (Turtle, RDF/XML, N-Triples, JSON-LD, SKOS), concept search and explanation, and asset
  context in Markdown, JSON and JSON-LD with token estimates and `max_tokens` truncation.
- MCP tools `list_models`, `get_model`, `find_concepts`, `explain_concept`,
  `find_assets_for_concept`, `get_asset_context` and `export_model`.
- CLI commands `lakeloom models`, `lakeloom concept`, `lakeloom context` and `lakeloom export`.
- Setting `LAKELOOM_MODEL_CACHE_TTL_SECONDS` (default 60).
- M1c publication read surfaces: read-only HTTP API under `/v1` (models, versions, concepts and
  asset context) with a committed OpenAPI spec, and MCP resources serving the same bytes.
- Content negotiation (`Accept` with q-values, `406` listing supported types) and strong `ETag`,
  `Last-Modified`, `Link` and `If-None-Match` handling on every artefact.
- stdio transport with a configured local principal (`LAKELOOM_STDIO_PRINCIPAL`,
  `LAKELOOM_STDIO_SCOPES`), refused in containers unless explicitly enabled.
- OpenTelemetry traces and metrics over OTLP, off unless `LAKELOOM_OTEL_EXPORTER_OTLP_ENDPOINT`
  is set.
- Concept name resolution ranks exact name, then synonym, then local name.
- M1 acceptance contract test across MCP tools, MCP resources and HTTP.
- M2a proposals:
  - MCP tools `create_model` (scope `lakeloom:admin`; glossary plus paired native ontology),
    `propose_from_scan`, `propose_change`, `propose_realization`, `get_proposal`,
    `diff_proposal`, `submit_proposal` and `discard_proposal`. Proposals are OpenMetadata
    Draft change sets, and nothing is applied.
  - Confirm tokens for `create_model` and `discard_proposal`, bound to the calling principal.
  - Only a proposal's creator or a `lakeloom:admin` caller may change, submit or discard it.
  - Incremental scans: operations on a concept still pending in another open proposal are
    dropped with a warning instead of failing; `propose_change` skips operations pending
    elsewhere and concepts or attributes the model already has.
  - Realizations are limited to concepts of the model and the models it imports.
  - Attributes, axioms and relations on existing concepts of a model paired with
    `pair_existing=true` declare the concept's native class first.
  - Rule ids of agent edits are always `R-AGENT`; operation provenance is size-limited.
  - One audit record per write (partial results are audited as errors); MCP prompts need
    `lakeloom:read`.
  - MCP prompts `bootstrap_data_product` and `explain_table`.
  - CLI commands `lakeloom propose`, `lakeloom proposal` and `lakeloom submit`.
  - Dev realm client `lakeloom-admin-agent`.
  - M2a acceptance contract test.
- M2b review:
  - MCP tools `review_proposal` (approve, reject or request changes; human principals only,
    scope `lakeloom:review`, confirm token for approve and reject), `rebase_proposal` and
    `list_pending_reviews` (grouped by model and data product, paginated).
  - Per-model `access` groups, on-behalf-of OpenMetadata tokens for reviewers and a review
    service that applies proposals as the reviewer.
  - MCP prompt `review_with_specialist`.
  - CLI commands `lakeloom pending` and `lakeloom rebase`.
