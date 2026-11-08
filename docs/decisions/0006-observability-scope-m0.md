# 0006. Observability scope for M0

- Status: accepted
- Date: 2026-11-08
- Requirements: NFR-OPS-01, NFR-OPS-02, NFR-OPS-03

## Context

NFR-OPS-02 asks for metrics and OpenTelemetry traces. M0 only needs a running,
probe-able service; tracing needs an exporter, sampling policy and instrumented tool
calls that do not exist yet.

## Decision

- M0 ships JSON logs on stdout (one object per line with `ts`, `level`, `logger`,
  `message`, `request_id`, plus extras), `/healthz`, `/readyz` and Prometheus metrics
  at `/metrics`. Logs never include request headers, the OM bot token or bearer tokens.
- Metrics live in `lakeloom.metrics` on a dedicated registry (`lakeloom_http_requests_total`
  labelled by route template, `lakeloom_tool_calls_total`, `lakeloom_om_request_seconds`).
- OpenTelemetry traces and OTLP export (NFR-OPS-02) arrive in M1, enabled by
  `LAKELOOM_OTEL_EXPORTER_OTLP_ENDPOINT`.
- Alembic arrives with the first table (scan records, M1). Until then the state store
  only needs `SELECT 1` for readiness; PostgreSQL (asyncpg, now a base dependency) is the
  dev-stack default and SQLite the fallback.

## Consequences

Operators get probes, metrics and correlatable logs now; traces wait for M1.
