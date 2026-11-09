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
