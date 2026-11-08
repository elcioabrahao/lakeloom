# 0005. Own bearer-token middleware for HTTP API and MCP

- Status: accepted
- Date: 2026-11-08
- Requirements: NFR-SEC-01, NFR-SEC-02, NFR-SEC-03, NFR-SEC-06, FR-AGT-05

## Context

Lakeloom serves two surfaces from one ASGI app: the HTTP API and MCP Streamable HTTP
at `/mcp`. Both need the same OAuth 2.1 resource-server behaviour: validate JWTs from
an OIDC issuer, answer `401` with an RFC 9728 `WWW-Authenticate` challenge, and return
`503 upstream_unavailable` when the IdP is down. The MCP SDK ships auth helpers, but
they only cover the MCP mount, their API has changed between SDK versions, and their
error bodies differ from the typed errors the HTTP API returns (FR-AGT-05).

## Decision

- One pure-ASGI `BearerAuthMiddleware` (`lakeloom.server.auth`) wraps the whole app.
  It protects every path except the exact public paths `/healthz`, `/readyz`,
  `/metrics` and `/.well-known/oauth-protected-resource`. On success it stores the
  `Principal` at `scope["state"]["principal"]`. WebSocket requests are authenticated
  too (fail closed); a failed handshake is closed with code 1008.
- One `TokenValidator`, built on PyJWT, is shared by both surfaces:
  - Only asymmetric algorithms are allowed (configuring `HS*` or `none` is a startup
    error). Each JWK is bound to exactly one algorithm (its `alg`, or the token's
    `alg` if that matches the key type), so alg-confusion is impossible.
  - `jku`, `x5u` and embedded `jwk` headers are never used; keys come only from the
    configured JWKS URL or the issuer's discovery document. `use: enc` and `oct`
    keys are ignored.
  - Required claims: `exp`, `iss`, `aud`, `sub`; `nbf` and `iat` are checked when
    present; all with `LAKELOOM_OIDC_CLOCK_SKEW_SECONDS` leeway. Issuer match is exact.
  - Tokens longer than 16 KiB and tokens without a string `kid` are rejected before
    any key lookup.
  - JWKS cache: keyed by `kid`, max age 1 h (so revoked keys disappear). An unknown
    `kid` triggers one refetch, at most once per 30 s. A lock serialises fetches.
    If a periodic refresh fails, cached keys keep serving and the refresh is retried
    after 30 s; with no cached keys the request gets `503`.
- `Authorization` scheme matching is case-insensitive (RFC 7235); more than one
  `Authorization` header is a `401`. Error descriptions are fixed strings, never the
  token or IdP output. `403 insufficient_scope` is left to per-tool checks (later).

## Consequences

- One error format and one audit-relevant principal for both surfaces.
- We own security-critical code; it is covered by unit tests for hostile tokens, key
  rotation, IdP outages and concurrency, and contract tests against Keycloak follow.
- Not checked (yet): RFC 9068 `typ: at+jwt` (Keycloak does not emit it by default) and
  the discovery document's `issuer` member. If the app is mounted under a
  `root_path`, the public paths must be given with that prefix.

## Addendum (2026-11-08): the PRM `resource` is the origin, not `/mcp`

The RFC 9728 document at `/.well-known/oauth-protected-resource` (NFR-SEC-02) publishes
`resource` = `LAKELOOM_PUBLIC_URL` (the origin, no trailing slash), not
`<public_url>/mcp`, and is served at the root well-known path rather than a
path-suffixed `/.well-known/oauth-protected-resource/mcp`.

Rationale: Lakeloom is one resource server for both the HTTP API and the MCP endpoint;
both accept the same tokens with the same audience (`LAKELOOM_OIDC_AUDIENCE`), so a
single resource identifier describes them. The `WWW-Authenticate` challenge on every
`401`, including `/mcp`, points at this one document.

Revisit if a strict RFC 8707 / MCP client rejects metadata whose `resource` does not
equal the URL it connected to (`.../mcp`); the fix would be a second, path-suffixed
PRM document for `/mcp`, not a change of token audience.
