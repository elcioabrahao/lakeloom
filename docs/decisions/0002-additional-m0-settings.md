# 0002. Additional M0 settings

- Status: accepted
- Date: 2026-11-08
- Requirements: NFR-DEP-03, NFR-SEC-11, NFR-SEC-03

## Context

REQUIREMENTS.md §10 lists the configuration surface, but the M0 server needs a few
knobs the spec does not name (state location, JWKS reachability from a container,
agent detection, logging, binding).

## Decision

Add these `LAKELOOM_*` settings beyond §10:

| Variable | Default | Why |
|---|---|---|
| `DATA_DIR` | `/data` | Root of writable state; the git workspace is `DATA_DIR/git`. |
| `OIDC_JWKS_URL` | unset | Override for when the issuer URL is not reachable from the container. |
| `OIDC_AGENT_CLAIM` | `client_id` | Presence of this claim marks an agent principal (NFR-SEC-03). |
| `OIDC_CLOCK_SKEW_SECONDS` | `60` | Leeway for `exp`/`nbf` validation. |
| `LOG_LEVEL` | `INFO` | Log verbosity. |
| `HOST` | `0.0.0.0` | Bind address inside the container. |
| `PORT` | `8000` | Bind port. |

Also: `Settings.public_base_url` returns `public_url` without the trailing slash
pydantic adds, so derived URLs never contain `//`. `sensitive_tags` accepts a JSON
list or a comma-separated string. `om_url`, `oidc_issuer` and `public_url` are not
validated as required in `Settings`; the dependency builder fails fast instead, so
`lakeloom version` and tests work with an empty environment.

## Consequences

Operators get a few more variables to document in `.env.example` and the README.
Secrets read from `*_FILE` are stripped of surrounding whitespace; setting both
`X` and `X_FILE` is a startup error.
`*_FILE` is accepted for `om_bot_token`, `git_token` and `db_url` (which may embed a
database password). `db_url` and `git_remote` stay plain strings but are excluded from
`repr(settings)` (NFR-SEC-11).
