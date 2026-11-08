# M0 deferred follow-ups

Minor findings from the M0 task and whole-branch reviews that were deliberately deferred. Triage these when planning M1.

## Suggested for M1

- Auth production hardening: token-type check (`typ` at+jwt / Keycloak typ=Bearer), discovery `issuer` and `crit` header checks, https-only jwks_uri outside development, no `error=invalid_token` for non-Bearer schemes (RFC 6750 §3.1), rate-limit the 503 traceback log.
- Extra allowed-hosts setting for MCP behind proxies (Host allow-list returns 421 today).
- Config hardening: empty `*_FILE` / empty secret file handling, raw JSONDecodeError for malformed lists, validate log_level/port/clock skew.
- OM client: wrap pydantic ValidationError and 3xx in OMError; trailing-slash and malformed-body tests.
- Metrics: collapse unknown HTTP methods to OTHER.
- /readyz details: avoid `str(exc)` and filesystem paths on the public endpoint; kill git subprocess on timeout.
- Unexpected exceptions in get_server_info → typed error (FR-AGT-05).
- Test hygiene: default-checks test does real DNS; misnamed CORS test; lifespan-close test assertion; pytest-asyncio loop-scope deprecation warning; contract test for lakeloom-admins mapping (needs an admin user in the dev realm).
- Switch the code default of LAKELOOM_DB_URL to PostgreSQL (owner decision: later).

## Release hardening (M4)

- Pin all GitHub Actions by commit SHA; attach SBOM/provenance on push; buildx GHA cache; setup-uv cache/version pin.
- Exclude the 4.5 MB OpenAPI spec from the wheel/image.
- Loosen dependency lower bounds (currently equal to the resolved versions).
- Stateful MCP sessions are not bound to a principal: multi-replica needs sticky sessions or stateless mode.

## Cosmetic / docs

- ADR 0003 line ~48 still says host ports were dropped (the table and appendix are correct).
- .env.example lists Gitea/Keycloak values compose doesn't read; om-bootstrap runs `apk add` each start; Fuseki built from a remote git ref.
- README examples could use 127.0.0.1 instead of localhost; document that Kubernetes ignores the image HEALTHCHECK.
- CODE_OF_CONDUCT blank lines; SECURITY.md supported-versions row.
