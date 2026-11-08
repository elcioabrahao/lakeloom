# M0 — Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable Lakeloom skeleton: configured, containerised, health-checked, OIDC-protected, talking to a real OpenMetadata 2.1 over a typed client, with a dev stack (OM + Keycloak + Gitea) and green CI.

**Architecture:** One FastAPI app serves `/healthz`, `/readyz`, `/metrics`, `/.well-known/oauth-protected-resource` (public) and mounts the MCP Streamable HTTP app at `/mcp` behind a shared bearer-token ASGI middleware. Core pieces are small modules: `config.py` (settings), `server/auth.py` (OIDC validation, principal), `om/client.py` (async OM client hand-written from the committed OM 2.1 OpenAPI spec and guarded by a spec test), `state/db.py` (engine + ping). `lakeloom serve` runs it under uvicorn.

**Tech Stack:** Python 3.12, uv, ruff, pyright (strict on `src/`), pytest (+ pytest-asyncio, respx, hypothesis), FastAPI, uvicorn, httpx, pydantic v2, pydantic-settings, PyJWT[crypto], SQLAlchemy 2 (async, aiosqlite), prometheus-client, typer, `mcp` SDK; Docker/compose; GitHub Actions.

**Spec:** `REQUIREMENTS.md` (sections 5, 6.2, 8, 9, 10, 11, 12, 13 row M0).

## M0 acceptance (copied from spec §13)

`docker compose up` starts everything; `/readyz` green; CI green; a token from Keycloak is accepted and a wrong-audience token is rejected; `get_server_info` reports OM version and RDF status.

## Global Constraints

- Python **≥ 3.12** (`requires-python = ">=3.12"`), dev pinned to 3.12 via `.python-version`; CI matrix 3.12 and 3.13. (TC-1)
- uv, ruff (lint + format), pyright **strict for `src/`**, pytest. (TC-2)
- Licence **Apache-2.0**; `LICENSE` + `NOTICE`. (NFR-OSS-01)
- Package name `lakeloom`; PyPI already holds placeholder `0.0.1`, so start at `0.1.0.dev0`. Repo URL `https://github.com/elcioabrahao/lakeloom`.
- Conventional Commits, every commit signed off (`git commit -s`), requirement IDs in commit messages, docstrings of implementing modules, and test names (`test_nfr_sec_01_...`). (§0.2, NFR-OSS-04)
- No cloud-provider SDK in the base dependencies; connectors are extras (empty extras `databricks`, `snowflake`, `delta` declared now). **PostgreSQL is the default database for both OpenMetadata and Lakeloom** in the dev stack, CI and docs (owner decision 2026-11-08); `asyncpg` is a base dependency (the `postgres` extra stays as an empty alias for compatibility). SQLite stays supported and remains the code-level fallback default of `LAKELOOM_DB_URL` (spec §10) for unit tests and single-node use. (TC-7, TC-9, TC-11)
- Secrets come from env vars or `*_FILE` files, use `SecretStr`, never logged, never returned. (NFR-SEC-11)
- No web UI. (§0.7)
- Ambiguity: pick the simplest option, write `docs/decisions/NNNN-title.md`, continue. (§0.6)
- Never hard-code OM paths from the docs without checking them against the committed OpenAPI spec; spec wins, record differences in an ADR. (§0.4)
- Before coding against `mcp`, FastAPI middleware, or PyJWT JWKS APIs, check the installed version's docs (context7) — the MCP SDK API has changed across versions.

## Review Focus

1. **OM down or slow** (at startup or per request): `/readyz` returns 503 naming the `openmetadata` check; `get_server_info` returns `openmetadata.reachable: false` with an error string instead of raising. → tests in Task 4 and Task 7.
2. **IdP key rotation / IdP unreachable**: an unknown `kid` triggers one JWKS refetch then 401; an unreachable JWKS endpoint yields 503 `upstream_unavailable`, not 500 and not 200. → tests in Task 5.
3. **Hostile tokens**: no `Authorization`, `Bearer` with empty token, `alg: none`, HS256 signed with the public key (alg confusion), expired, future `nbf`, wrong `iss` all yield 401 with `WWW-Authenticate`. → tests in Task 5.
4. **Secret handling**: `X` and `X_FILE` both set → startup error; `_FILE` content trailing newline stripped; secrets absent from `repr(settings)` and logs. → tests in Task 2.
5. **Read-only root filesystem**: container runs with `--read-only` + tmpfs `/tmp`; an unwritable `/data` makes `/readyz` fail on `git_workspace`/`state_store` with a clear message rather than crashing the process. → tests in Task 6 and verification in Task 8.

## File structure

```
pyproject.toml  uv.lock  .python-version  .gitignore  .editorconfig  .dockerignore
LICENSE  NOTICE  README.md  CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  CHANGELOG.md  GOVERNANCE.md
src/lakeloom/__init__.py          # __version__
src/lakeloom/cli.py               # typer app: version, serve
src/lakeloom/config.py            # Settings (pydantic-settings), _FILE secrets
src/lakeloom/logging.py           # JSON formatter, request-id contextvar
src/lakeloom/state/db.py          # async engine factory, ping
src/lakeloom/om/client.py         # OMClient (async httpx)
src/lakeloom/om/models.py         # pydantic models for the OM responses used
src/lakeloom/om/openapi/openmetadata-<tag>.json   # committed OM spec
src/lakeloom/server/auth.py       # TokenValidator, Principal, BearerAuthMiddleware, PRM document
src/lakeloom/server/health.py     # readiness checks
src/lakeloom/server/http_app.py   # create_app(settings) -> FastAPI
src/lakeloom/server/mcp_app.py    # create_mcp(deps) -> FastMCP, get_server_info
src/lakeloom/server/server_info.py# build_server_info(...) -> ServerInfo
tests/unit/...  tests/contract/...  tests/conftest.py
deploy/docker/Dockerfile
deploy/compose/docker-compose.yml  deploy/compose/keycloak-realm.json  deploy/compose/om-bootstrap.sh  deploy/compose/.env.example
scripts/fetch_om_openapi.sh
docs/decisions/0000-template.md  0001-*.md ...
.github/workflows/ci.yml  .github/workflows/dco.yml  .github/dependabot.yml
.github/ISSUE_TEMPLATE/*  .github/pull_request_template.md
```

---

### Task 1: Repository skeleton, tooling, OSS files

**Files:**
- Create: everything in the top-level list above except `uv.lock` (generated); `src/lakeloom/__init__.py`, `src/lakeloom/cli.py`; `docs/decisions/0000-template.md`, `docs/decisions/0001-record-architecture-decisions.md`; `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.md`, `.github/pull_request_template.md` (DCO + requirement-ID checklist)
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `lakeloom.__version__: str` (read via `importlib.metadata.version("lakeloom")`); `lakeloom.cli.app: typer.Typer`; console script `lakeloom = "lakeloom.cli:app"`.

- [ ] **Step 1:** `git init -b main`; move `pypi_files/` out of the tree (or add it to `.gitignore`); write `.gitignore` (Python, `.venv`, `/data`, `.env`, `deploy/compose/.secrets/`).
- [ ] **Step 2:** `pyproject.toml`: build backend hatchling, `version = "0.1.0.dev0"`, metadata/keywords/classifiers taken from the placeholder package in `pypi_files/` with URLs changed to `elcioabrahao/lakeloom`; runtime deps (`fastapi`, `uvicorn[standard]`, `httpx`, `pydantic>=2`, `pydantic-settings`, `pyjwt[crypto]`, `sqlalchemy[asyncio]>=2`, `aiosqlite`, `prometheus-client`, `typer`, `mcp`), extras `postgres=["asyncpg"]`, `databricks=[]`, `snowflake=[]`, `delta=[]`; dev group (`ruff`, `pyright`, `pytest`, `pytest-asyncio`, `respx`, `hypothesis`, `pip-audit`). Add with `uv add` so the latest releases are locked. Tool config: ruff line length 100, rule sets `E,F,I,UP,B,S,ASYNC,RUF` (allow `S101` in tests); pyright `strict = ["src"]`, `pythonVersion = "3.12"`; pytest `asyncio_mode = "auto"`, markers `contract: needs the docker compose dev stack`, default `-m "not contract"`.
- [ ] **Step 3: Write the failing test** `test_cli_version_prints_package_version`: `CliRunner().invoke(app, ["version"])` → exit code 0 and output stripped == `lakeloom.__version__`.
- [ ] **Step 4:** `uv run pytest tests/unit/test_cli.py -v` → FAIL (import error).
- [ ] **Step 5:** Implement `__version__` and `cli.app` with a `version` command.
- [ ] **Step 6:** `uv run pytest -v && uv run ruff check && uv run ruff format --check && uv run pyright` → all pass.
- [ ] **Step 7:** OSS files: `LICENSE` (Apache-2.0 full text), `NOTICE` (`Lakeloom\nCopyright 2026 Elcio Abrahão`), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1, contact = repo owner email via GitHub private reporting), `SECURITY.md` (GitHub private vulnerability reporting, supported versions, 90-day disclosure), `CONTRIBUTING.md` (uv setup, test commands, Conventional Commits, DCO `-s`, requirement IDs, ADR process), `GOVERNANCE.md` (maintainer-led, single maintainer), `CHANGELOG.md` (Keep a Changelog, `## [Unreleased]`), `README.md` (what/why from spec §1, status "pre-alpha, M0", architecture mermaid from spec §4.1, the NFR-OSS-07 non-affiliation statement verbatim in meaning: *independent project, not affiliated with OpenMetadata, Collate, Databricks or Snowflake; trademarks belong to their owners*; quick-start placeholder filled in Task 10).
- [ ] **Step 8:** ADR 0001 records: ADRs in `docs/decisions/`, MADR-lite format (Context / Decision / Consequences); also the repo URL decision (`elcioabrahao/lakeloom` instead of the placeholder's `lakeloom/lakeloom`) and the starting version `0.1.0.dev0`.
- [ ] **Step 9: Commit** `chore: scaffold repository, tooling and community files (NFR-OSS-01, NFR-OSS-02, NFR-OSS-07)`.

---

### Task 2: Settings (`config.py`)

**Files:**
- Create: `src/lakeloom/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` with `env_prefix="LAKELOOM_"`. Fields (defaults exactly per spec §10): `env: Literal["development","production"]="development"`, `config_file: Path|None=None`, `public_url: AnyHttpUrl|None=None`, `om_url: AnyHttpUrl|None=None`, `om_bot_token: SecretStr|None=None`, `oidc_issuer: str|None=None`, `oidc_audience: str="lakeloom"`, `oidc_token_exchange: bool=True`, `scope_group_map: dict[str, list[str]]={}` (JSON), `db_url: str="sqlite:////data/lakeloom.db"`, `git_remote: str|None=None`, `git_branch: str="main"`, `git_mode: Literal["direct","pull_request"]="pull_request"`, `git_ssh_key_file: Path|None=None`, `git_token: SecretStr|None=None`, `forge: Literal["github","gitlab","gitea","none"]="github"`, `profile_samples: bool=False`, `sensitive_tags: list[str]=["PII.*"]`, `max_proposal_ops: int=50`, `triplestore_enabled: bool=False`, `stdio_enabled: bool|None=None` (resolved: `False` in production, `True` in development), `otel_exporter_otlp_endpoint: str|None=None`.
  - M0 additions (record in ADR 0002): `data_dir: Path=Path("/data")` (git workspace = `data_dir/"git"`), `oidc_jwks_url: str|None=None` (override for when the issuer URL is not reachable from the container), `oidc_agent_claim: str="client_id"` (presence ⇒ agent principal, NFR-SEC-03), `oidc_clock_skew_seconds: int=60`, `log_level: str="INFO"`, `host: str="0.0.0.0"`, `port: int=8000`.
  - `def get_settings() -> Settings` (no caching inside; callers pass `Settings` explicitly).
  - Secrets `om_bot_token`, `git_token`: each also read from `LAKELOOM_<NAME>_FILE`; implement as a `model_validator(mode="before")` over the field list `SECRET_FILE_FIELDS = ("om_bot_token", "git_token")`.

- [ ] **Step 1: Write failing tests** (use `monkeypatch.setenv`, `tmp_path`):
  - `test_nfr_dep_03_defaults_match_spec`: `Settings()` with empty env → `oidc_audience=="lakeloom"`, `db_url=="sqlite:////data/lakeloom.db"`, `git_branch=="main"`, `git_mode=="pull_request"`, `forge=="github"`, `profile_samples is False`, `sensitive_tags==["PII.*"]`, `max_proposal_ops==50`, `triplestore_enabled is False`, `stdio_enabled is True`.
  - `test_stdio_disabled_by_default_in_production`: `LAKELOOM_ENV=production` → `stdio_enabled is False`; with `LAKELOOM_STDIO_ENABLED=true` → `True`.
  - `test_nfr_sec_11_secret_from_file_strips_trailing_newline`: file `"abc\n"` via `LAKELOOM_OM_BOT_TOKEN_FILE` → `om_bot_token.get_secret_value()=="abc"`.
  - `test_nfr_sec_11_secret_and_file_both_set_is_error`: both set → `ValidationError` naming `LAKELOOM_OM_BOT_TOKEN`.
  - `test_nfr_sec_11_secrets_not_in_repr`: `"abc" not in repr(settings)` and `not in str(settings.model_dump())`.
  - `test_scope_group_map_parses_json`: `'{"ontology-reviewers":["lakeloom:review"]}'` → dict.
- [ ] **Step 2:** `uv run pytest tests/unit/test_config.py -v` → FAIL.
- [ ] **Step 3:** Implement `Settings` and `get_settings`.
- [ ] **Step 4:** Tests pass; ruff + pyright clean.
- [ ] **Step 5:** Write ADR `0002-additional-m0-settings.md` listing the added variables and why.
- [ ] **Step 6: Commit** `feat(config): settings with _FILE secrets (NFR-DEP-03, NFR-SEC-11)`.

---

### Task 3: Dev stack — OpenMetadata 2.1, Keycloak, Gitea; fetch the OM OpenAPI spec

Prerequisite: a Docker-compatible engine with ≥ 8 GB memory. The owner's dev machine uses **OrbStack** (not Docker Desktop; context `orbstack`, `orb start`; 12 GB / 10 CPUs configured). Use only the standard `docker` / `docker compose` / `docker buildx` CLIs so the stack also works on Docker Engine (CI), Podman and Rancher Desktop. Do not depend on OrbStack-only features such as `*.orb.local` domains.

**Files:**
- Create: `deploy/compose/docker-compose.yml`, `deploy/compose/keycloak-realm.json`, `deploy/compose/om-bootstrap.sh`, `deploy/compose/.env.example`, `scripts/fetch_om_openapi.sh`, `src/lakeloom/om/openapi/openmetadata-<tag>.json`, `docs/decisions/0003-openmetadata-dev-stack-and-spec.md`

**Interfaces:**
- Produces (fixed host ports, used by Tasks 8–10): OM `http://localhost:8585`, Keycloak `http://localhost:8180`, Gitea `http://localhost:3000`, Lakeloom `http://localhost:8000` (service added in Task 8).
- Produces the bot token file in named volume `om-secrets` at `/secrets/om_bot_token` (mounted read-only into Lakeloom in Task 8).
- Keycloak realm `lakeloom`: issuer `http://localhost:8180/realms/lakeloom`; inside the network JWKS is at `http://keycloak:8080/realms/lakeloom/protocol/openid-connect/certs`.

- [ ] **Step 1:** Get the official OM docker compose for the newest 2.1 tag (2.1.0 GA if it is out, otherwise 2.1.0-rc1) from the OpenMetadata GitHub release assets. Copy its `mysql` (or `postgresql`), `elasticsearch`/`opensearch`, `execute-migrate-all` and `openmetadata-server` services; leave out the ingestion/Airflow service (not needed in M0, saves memory). Pin exact image tags. Put Fuseki under compose profile `rdf` and set OM's RDF settings to use it when that profile is active (per the OM "Enabling RDF" docs page in spec §15).
- [ ] **Step 2:** Keycloak service `quay.io/keycloak/keycloak:<current 26.x>` `start-dev --import-realm`, `KC_HOSTNAME=http://localhost:8180`, `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`, port `8180:8080`. Realm JSON contains:
  - client scopes `lakeloom:read`, `lakeloom:propose`, `lakeloom:review`, `lakeloom:publish`, `lakeloom:admin` (all optional client scopes, included in the `scope` claim);
  - groups `data-engineers`, `ontology-reviewers`, `lakeloom-admins`, plus a `groups` claim mapper (full path off);
  - client `lakeloom-agent` (confidential, service accounts only, secret `dev-agent-secret`, default scopes `lakeloom:read lakeloom:propose`, audience mapper → `lakeloom`);
  - client `lakeloom-dev-cli` (public, PKCE S256, direct access grants **on** — dev realm only, used by contract tests to act as a human; audience mapper → `lakeloom`);
  - client `other-app` (confidential, service accounts, secret `dev-other-secret`, **no** `lakeloom` audience mapper) — the wrong-audience token source;
  - users `alice` / `alice` (group `ontology-reviewers`) and `bob` / `bob` (group `data-engineers`).
- [ ] **Step 3:** Gitea service `gitea/gitea:<current 1.x>` with SQLite, `INSTALL_LOCK=true`, port `3000:3000`, and an init step that creates admin `lakeloom-admin` / `lakeloom-admin` and an empty repo `lakeloom-admin/ontologies`.
- [ ] **Step 4:** `om-bootstrap.sh` (one-shot service, `depends_on: openmetadata-server: service_healthy`): log in as the default OM admin, read the `ingestion-bot` JWT, write it to `/secrets/om_bot_token`. Check the login and bot-token endpoints in the spec fetched in Step 6 and fix the script to match if they differ.
- [ ] **Step 5:** `docker compose -f deploy/compose/docker-compose.yml up -d` then verify: `curl -fsS localhost:8585/api/v1/system/version` returns JSON with `version` starting with `2.1`; `curl -fsS localhost:8180/realms/lakeloom/.well-known/openid-configuration | jq -r .issuer` → `http://localhost:8180/realms/lakeloom`; `curl -fsS localhost:3000/api/v1/version` → 200; `docker compose run --rm om-bootstrap` leaves a non-empty token.
- [ ] **Step 6:** `scripts/fetch_om_openapi.sh <om-url> <out>`: try `/swagger.json`, then `/api/swagger.json`, then `/openapi.json`; `jq -S .` for stable diffs; write to `src/lakeloom/om/openapi/openmetadata-<tag>.json`. Run it.
- [ ] **Step 7:** Compare the spec's paths with the OM paths named in REQUIREMENTS FR-OM-01/04 (`/api/v1/ontologyAxioms`, `/api/v1/ontologyAxioms/validate`, `/api/v1/ontologyChangeSets`, `/api/v1/ontology/modeling/iris/preview`, `/api/v1/glossaries/{id}/exportOntology`, `/api/v1/glossaries/{id}/sparql`, `/api/v1/rdf/status`, `/api/v1/ontologyEditLocks`, `/api/v1/system/version`) using `jq '.paths|keys[]'`. ADR 0003 records the pinned tags, the spec URL that worked, a table *documented path → actual path (or "absent")*, the memory budget, and the Keycloak hostname/backchannel decision.
- [ ] **Step 8: Commit** `build(compose): OM 2.1 + Keycloak + Gitea dev stack and OM OpenAPI spec (NFR-DEP-05, FR-OM-08)`.

---

### Task 4: OpenMetadata client (M0 surface)

**Files:**
- Create: `src/lakeloom/om/__init__.py`, `src/lakeloom/om/client.py`, `src/lakeloom/om/models.py`
- Test: `tests/unit/om/test_client.py`, `tests/unit/om/test_spec_paths.py`

**Interfaces:**
- Consumes: `Settings.om_url`, `Settings.om_bot_token` (Task 2); committed spec (Task 3).
- Produces:
  - `models.OMVersion(version: str, revision: str|None, timestamp: int|None)`; `models.RdfStatus` with whatever fields the spec's `/rdf/status` response schema defines (at least `enabled: bool`), `extra="allow"`; `models.OMCapabilities(reachable: bool, version: str|None, rdf_enabled: bool|None, rdf: RdfStatus|None, error: str|None)`.
  - `class OMError(Exception)` with `status: int|None`, and `OMUnavailable(OMError)` (connect errors, timeouts, 5xx).
  - `class OMClient`: `__init__(self, base_url: str, token: SecretStr|None, *, http: httpx.AsyncClient|None=None, timeout: float=5.0)`; `async get_version() -> OMVersion`; `async get_rdf_status() -> RdfStatus`; `async get_capabilities() -> OMCapabilities` (never raises; `rdf_enabled=None` and degrade gracefully when the RDF endpoint is 404 — FR-OM-06); `async aclose() -> None`; class attribute `PATHS: ClassVar[dict[str, tuple[str, str]]]` mapping method name → `(HTTP method, spec path)`.
  - Bearer header `Authorization: Bearer <bot token>`; token never logged.

- [ ] **Step 1: Write failing tests** with `respx`:
  - `test_fr_om_01_get_version_parses_response`: mock `GET /api/v1/system/version` → `{"version":"2.1.0","revision":"abc","timestamp":1}` → `OMVersion(version="2.1.0", ...)`, request had `Authorization: Bearer t`.
  - `test_fr_om_06_capabilities_rdf_enabled` / `..._rdf_endpoint_404_degrades` (`rdf_enabled is None`, `reachable is True`).
  - `test_fr_om_06_capabilities_when_om_unreachable`: `httpx.ConnectError` → `reachable is False`, `error` contains `"connect"`, no exception.
  - `test_om_5xx_raises_om_unavailable` for `get_version`.
  - `test_fr_om_01_client_paths_exist_in_spec` (`test_spec_paths.py`): load the committed spec; every `(method, path)` in `OMClient.PATHS` exists under `paths[path][method.lower()]`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement models (field names from the spec's response schemas) and the client.
- [ ] **Step 4:** Tests pass; pyright strict clean.
- [ ] **Step 5:** ADR `0004-om-client-hand-written-from-spec.md`: M0 hand-writes a thin client plus models for the endpoints it uses (§0.4 allows this); the spec-path test is the guard; we will reconsider code generation when the client surface grows (M2).
- [ ] **Step 6: Commit** `feat(om): async OM client for version and RDF capabilities (FR-OM-01, FR-OM-06)`.

---

### Task 5: OIDC token validation and bearer middleware

**Files:**
- Create: `src/lakeloom/server/__init__.py`, `src/lakeloom/server/auth.py`
- Test: `tests/unit/server/test_auth.py`, `tests/unit/server/conftest.py` (RSA key pair, JWKS, token factory, `httpx.MockTransport` serving discovery + JWKS)

**Interfaces:**
- Consumes: `Settings` OIDC fields (Task 2).
- Produces:
  - `SCOPES: Final = ("lakeloom:read","lakeloom:propose","lakeloom:review","lakeloom:publish","lakeloom:admin")`.
  - `class PrincipalType(StrEnum): AGENT="agent"; HUMAN="human"`.
  - `@dataclass(frozen=True) class Principal: subject: str; type: PrincipalType; client_id: str|None; username: str|None; scopes: frozenset[str]; groups: frozenset[str]; token: SecretStr` (the raw token is kept for later on-behalf-of use, never serialised).
  - `class AuthError(Exception)`: `code: Literal["invalid_token","insufficient_scope"]`, `description: str`; `class IdPUnavailable(Exception)`.
  - `class TokenValidator`: `__init__(self, *, issuer: str, audience: str, http: httpx.AsyncClient, jwks_url: str|None=None, leeway: int=60, agent_claim: str="client_id", scope_group_map: Mapping[str, Sequence[str]]={}, algorithms: Sequence[str]=("RS256","RS384","RS512","ES256","ES384","PS256"))`; `async validate(token: str) -> Principal`.
    - JWKS URL: `jwks_url` or discovery `{issuer}/.well-known/openid-configuration` → `jwks_uri`. Cache keys by `kid`; on unknown `kid` refetch once (at most once per 30 s), then `AuthError`.
    - Checks: signature, `iss == issuer`, `aud` contains `audience`, `exp`, `nbf`, `iat` with `leeway`; header `alg` must be in `algorithms` (rejects `none` and HS*).
    - Scopes = (`scope` claim split on spaces ∩ `SCOPES`) ∪ scopes mapped from the `groups` claim via `scope_group_map`.
    - Type = `AGENT` if `agent_claim` is present in claims, else `HUMAN` (NFR-SEC-03).
  - `def protected_resource_metadata(settings: Settings) -> dict[str, object]` → `{"resource": public_url, "authorization_servers": [issuer], "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"]}` (RFC 9728).
  - `class BearerAuthMiddleware` (pure ASGI): `__init__(self, app, *, validator: TokenValidator, resource_metadata_url: str, public_paths: Sequence[str])`. For non-public HTTP requests: missing/invalid → 401 JSON `{"error":"invalid_token","error_description":...}` with header `WWW-Authenticate: Bearer resource_metadata="<url>"` (plus `error="invalid_token"` when a token was given); `IdPUnavailable` → 503 JSON `{"error":"upstream_unavailable","hint":"identity provider unreachable"}`; success → `scope["state"]["principal"] = Principal`. Exact public paths: `/healthz`, `/readyz`, `/metrics`, `/.well-known/oauth-protected-resource`.

- [ ] **Step 1: Write failing tests**:
  - `test_nfr_sec_01_valid_token_accepted`: RS256 token, right iss/aud → `Principal` with expected scopes.
  - `test_nfr_sec_01_wrong_audience_rejected`, `..._wrong_issuer_rejected`, `..._expired_rejected`, `..._nbf_in_future_rejected`, `..._clock_skew_tolerated` (exp 30 s ago, leeway 60 → accepted).
  - `test_nfr_sec_01_alg_none_rejected`, `test_nfr_sec_01_hs256_with_public_key_rejected`.
  - `test_nfr_sec_01_unknown_kid_refetches_jwks_once`: first JWKS lacks the key, second has it → accepted, exactly 2 JWKS fetches; key never present → `AuthError`, still 2 fetches.
  - `test_idp_unreachable_raises_idp_unavailable`.
  - `test_nfr_sec_03_agent_vs_human`: token with `client_id` → `AGENT`; without → `HUMAN`.
  - `test_nfr_sec_06_scopes_from_scope_claim_and_groups`: `scope="openid lakeloom:read"`, `groups=["ontology-reviewers"]`, map `{"ontology-reviewers":["lakeloom:review"]}` → `{"lakeloom:read","lakeloom:review"}` (`openid` dropped).
  - Middleware (Starlette `TestClient` around a tiny echo app): `test_nfr_sec_02_missing_token_401_with_resource_metadata` (exact header value), `test_empty_bearer_401`, `test_public_paths_skip_auth`, `test_idp_down_503_upstream_unavailable`, `test_nfr_sec_02_prm_document`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement with PyJWT (`jwt.PyJWK`, `jwt.decode(..., algorithms=..., audience=..., issuer=..., leeway=...)`), JWKS fetched via the injected async `httpx` client.
- [ ] **Step 4:** Tests pass; coverage of `server/auth.py` ≥ 85 % (`uv run pytest --cov=lakeloom.server.auth --cov-fail-under=85 tests/unit/server`; add `pytest-cov` to dev).
- [ ] **Step 5:** ADR `0005-own-bearer-middleware.md`: one ASGI middleware protects both the HTTP API and `/mcp`, instead of the MCP SDK's auth helpers, so the two surfaces share one validator and error format.
- [ ] **Step 6: Commit** `feat(auth): OIDC JWT validation, principals and RFC 9728 metadata (NFR-SEC-01, NFR-SEC-02, NFR-SEC-03, NFR-SEC-06)`.

---

### Task 6: State store, readiness checks, HTTP app, logging, metrics

**Files:**
- Create: `src/lakeloom/state/__init__.py`, `src/lakeloom/state/db.py`, `src/lakeloom/server/health.py`, `src/lakeloom/server/http_app.py`, `src/lakeloom/logging.py`
- Test: `tests/unit/state/test_db.py`, `tests/unit/server/test_health.py`, `tests/unit/server/test_http_app.py`, `tests/unit/test_logging.py`

**Interfaces:**
- Consumes: `Settings` (T2), `OMClient` (T4), `TokenValidator`, `BearerAuthMiddleware`, `protected_resource_metadata` (T5).
- Produces:
  - `db.to_async_url(url: str) -> str` (`sqlite://` → `sqlite+aiosqlite://`, `postgresql://` → `postgresql+asyncpg://`, already-async URLs unchanged); `db.create_engine(url: str) -> AsyncEngine` (creates the SQLite parent directory if missing); `async db.ping(engine) -> None`.
  - `health.CheckResult(name: str, ok: bool, detail: str|None)`; `Check = Callable[[], Awaitable[CheckResult]]`; factories `om_check(client: OMClient) -> Check`, `state_store_check(engine) -> Check`, `git_workspace_check(path: Path) -> Check` (dir exists or can be created, a temp file can be written and removed, `git --version` exits 0); `async run_checks(checks: Sequence[Check], timeout: float=3.0) -> list[CheckResult]` (each check under its own timeout; exceptions become `ok=False` with the message).
  - `@dataclass class AppDeps: settings: Settings; om: OMClient; engine: AsyncEngine; validator: TokenValidator` and `def build_deps(settings: Settings) -> AppDeps`.
  - `create_app(deps: AppDeps, *, extra_mounts: Sequence[tuple[str, ASGIApp]] = ()) -> FastAPI` with routes `GET /healthz` → 200 `{"status":"ok"}`; `GET /readyz` → 200/503 `{"status":"ok"|"fail","checks":[{"name","ok","detail"}]}` with check names exactly `openmetadata`, `state_store`, `git_workspace`; `GET /metrics` (prometheus text); `GET /.well-known/oauth-protected-resource`; the `BearerAuthMiddleware` added with the public paths from T5; request-id middleware (accepts `X-Request-ID`, otherwise generates uuid4, echoes it back, puts it in the log context); CORS not added (NFR-SEC-15); OpenAPI docs at `/openapi.json` (NFR: FR-PUB-08 groundwork).
  - `logging.configure_logging(level: str) -> None` (one JSON object per line on stdout: `ts`, `level`, `logger`, `message`, `request_id`, plus `extra`); `logging.request_id_var: ContextVar[str|None]`.
  - Metric `lakeloom_http_requests_total{method,route,status}` (counter), `lakeloom_tool_calls_total{tool,outcome}` (counter, used in T7), `lakeloom_om_request_seconds` (histogram, observed in `OMClient` — small edit to T4's client).

- [ ] **Step 1: Write failing tests**:
  - `test_to_async_url` (three cases), `test_ping_sqlite_in_tmp_path`, `test_ping_postgres` (marked `contract`; uses `LAKELOOM_TEST_DB_URL`, default the dev-stack Lakeloom Postgres URL from ADR 0003).
  - `test_nfr_ops_01_readyz_all_green` (stub checks) → 200; `test_nfr_ops_01_readyz_reports_failing_check` → 503 and the failing entry has `name=="openmetadata"`, `ok is False`; `test_check_timeout_marks_failed`.
  - `test_git_workspace_check_unwritable_dir` (chmod 0o500 tmp dir; skip if running as root) → `ok is False`, detail mentions "not writable".
  - `test_nfr_ops_01_healthz_no_auth`, `test_metrics_exposes_request_counter`, `test_request_id_echoed`, `test_protected_route_requires_token` (any non-public path → 401).
  - `test_nfr_ops_03_json_log_line_has_request_id`.
  - `test_nfr_sec_11_bot_token_not_in_logs`: run a request with `caplog`; the token string never appears.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Move `asyncpg` from the `postgres` extra into base dependencies (`uv add asyncpg`; keep `postgres = []`).
- [ ] **Step 4:** Tests pass; pyright strict clean.
- [ ] **Step 5:** ADR `0006-observability-scope-m0.md`: M0 ships JSON logs + Prometheus; OpenTelemetry traces/OTLP (NFR-OPS-02) go in M1 behind `LAKELOOM_OTEL_EXPORTER_OTLP_ENDPOINT`; Alembic arrives with the first table (scan records, M1).
- [ ] **Step 6: Commit** `feat(server): HTTP app with health, readiness, metrics and JSON logs (NFR-OPS-01, NFR-OPS-02, NFR-OPS-03)`.

---

### Task 7: MCP server with `get_server_info`; `lakeloom serve`

**Files:**
- Create: `src/lakeloom/server/server_info.py`, `src/lakeloom/server/mcp_app.py`
- Modify: `src/lakeloom/cli.py` (add `serve`)
- Test: `tests/unit/server/test_server_info.py`, `tests/unit/server/test_mcp_app.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `AppDeps`, `create_app` (T6), `OMClient.get_capabilities` (T4), `Principal` (T5).
- Produces:
  - `class ServerInfo(BaseModel)`: `name: Literal["lakeloom"]`, `version: str`, `environment: str`, `openmetadata: OMCapabilities`, `features: dict[str, bool]` (keys `triplestore`, `stdio`, `token_exchange`), `sources: list[str]` (empty in M0), `models: list[str]` (empty in M0), `caller: CallerInfo|None` (`subject`, `type`, `scopes`; never the token).
  - `async build_server_info(deps: AppDeps, principal: Principal|None) -> ServerInfo`.
  - `create_mcp(deps: AppDeps) -> FastMCP` registering tool `get_server_info` (no arguments) with annotations `readOnlyHint=True, destructiveHint=False, idempotentHint=True`, a description written for LLMs (what it returns, no side effects, required scope `lakeloom:read`), returning a short Markdown summary plus structured content = `ServerInfo` (FR-AGT-02). The tool reads the `Principal` from the HTTP request state and refuses with a `forbidden` error (`hint`: "token lacks lakeloom:read") if `lakeloom:read` is missing. Increments `lakeloom_tool_calls_total`.
  - `create_app` called with `extra_mounts=[("/mcp", mcp.streamable_http_app())]`; the MCP session manager runs inside the FastAPI lifespan (check the installed `mcp` version's docs for the exact pattern).
  - CLI `lakeloom serve [--host] [--port]` → `uvicorn.run(create_app(build_deps(get_settings()), ...))`; defaults from `Settings.host`/`port`.

- [ ] **Step 1: Write failing tests**:
  - `test_fr_om_06_server_info_reports_om_version_and_rdf` (fake OM client returning `2.1.0`, rdf enabled) and `test_server_info_when_om_down` (`openmetadata.reachable is False`, no exception).
  - `test_server_info_never_contains_token` (dump JSON, token string absent).
  - `test_fr_agt_06_get_server_info_annotations`: list tools from `create_mcp(...)` → one tool `get_server_info` with the three hints above and a non-empty description.
  - `test_mcp_requires_auth`: POST `/mcp` without token → 401 with `WWW-Authenticate`.
  - `test_get_server_info_over_streamable_http`: start the app with an in-process ASGI transport, use the `mcp` client (`streamablehttp_client` + `ClientSession`, with an `httpx` client factory bound to the ASGI app if the installed SDK supports it, otherwise uvicorn on a free port in a background thread) with a valid test token → `call_tool("get_server_info")` structured content has `openmetadata.version == "2.1.0"`.
  - `test_get_server_info_forbidden_without_read_scope`.
  - `test_cli_serve_builds_app` (monkeypatch `uvicorn.run`, assert host/port passed).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Tests pass; `uv run lakeloom serve` starts locally with `LAKELOOM_DATA_DIR=./.data LAKELOOM_DB_URL=sqlite:///./.data/lakeloom.db LAKELOOM_OM_URL=http://localhost:8585 LAKELOOM_OIDC_ISSUER=http://localhost:8180/realms/lakeloom LAKELOOM_PUBLIC_URL=http://localhost:8000` and `curl localhost:8000/healthz` → `{"status":"ok"}`.
- [ ] **Step 5: Commit** `feat(mcp): MCP server over Streamable HTTP with get_server_info (FR-OM-06, FR-AGT-01, FR-AGT-06, FR-AGT-07)`.

---

### Task 8: Container image and Lakeloom in compose

**Files:**
- Create: `deploy/docker/Dockerfile`, `.dockerignore`
- Modify: `deploy/compose/docker-compose.yml` (add `lakeloom` service)

**Interfaces:**
- Produces: image `ghcr.io/elcioabrahao/lakeloom` (local tag `lakeloom:dev`); entrypoint `lakeloom serve`; `EXPOSE 8000`; `VOLUME /data`; user `10001:10001`.

- [ ] **Step 1:** Multi-stage Dockerfile: builder `ghcr.io/astral-sh/uv:<pinned>` + `python:3.12-slim` running `uv sync --frozen --no-dev` into `/app/.venv`; runtime `python:3.12-slim` with `git` installed (`--no-install-recommends`), non-root user 10001, `/data` owned by it, `PYTHONDONTWRITEBYTECODE=1`, `HEALTHCHECK` hitting `/healthz` with Python's stdlib urllib (no curl), OCI labels (`org.opencontainers.image.source=https://github.com/elcioabrahao/lakeloom`, `licenses=Apache-2.0`).
- [ ] **Step 2:** Compose `lakeloom` service: build from repo root, port `8000:8000`, `read_only: true`, `tmpfs: [/tmp]`, volume `lakeloom-data:/data`, `om-secrets:/secrets:ro`, env `LAKELOOM_OM_URL=http://openmetadata-server:8585`, `LAKELOOM_OM_BOT_TOKEN_FILE=/secrets/om_bot_token`, `LAKELOOM_OIDC_ISSUER=http://localhost:8180/realms/lakeloom`, `LAKELOOM_OIDC_JWKS_URL=http://keycloak:8080/realms/lakeloom/protocol/openid-connect/certs`, `LAKELOOM_PUBLIC_URL=http://localhost:8000`, `LAKELOOM_SCOPE_GROUP_MAP={"ontology-reviewers":["lakeloom:read","lakeloom:review"],"data-engineers":["lakeloom:read","lakeloom:propose"],"lakeloom-admins":["lakeloom:admin"]}`, `LAKELOOM_GIT_REMOTE=http://gitea:3000/lakeloom-admin/ontologies.git`, `LAKELOOM_FORGE=gitea`, `LAKELOOM_DB_URL=<Lakeloom Postgres URL from ADR 0003>`; `depends_on` the Lakeloom Postgres healthy, om-bootstrap `service_completed_successfully`, keycloak healthy.
- [ ] **Step 3: Verify:** `docker build -f deploy/docker/Dockerfile -t lakeloom:dev .` succeeds; `docker run --rm lakeloom:dev lakeloom version` prints the version; `docker run --rm --read-only --tmpfs /tmp -v ll:/data lakeloom:dev id -u` → `10001`.
- [ ] **Step 4: Verify:** `docker compose -f deploy/compose/docker-compose.yml up -d --wait` → all services healthy; `curl -fsS localhost:8000/readyz | jq` → `status: ok`, all three checks `ok: true`.
- [ ] **Step 5: Commit** `build(docker): non-root read-only-compatible image and compose service (NFR-DEP-01, NFR-DEP-04, NFR-DEP-05)`.

---

### Task 9: Contract tests against the running stack

**Files:**
- Create: `tests/contract/conftest.py`, `tests/contract/test_m0_acceptance.py`, `tests/contract/test_om_client_contract.py`

**Interfaces:**
- Consumes: ports and Keycloak clients/users from Task 3, Lakeloom service from Task 8.
- Produces: fixtures `agent_token` (client credentials `lakeloom-agent`/`dev-agent-secret`), `human_token` (password grant on `lakeloom-dev-cli`, user `alice`), `wrong_audience_token` (client credentials `other-app`); base URLs from env `LAKELOOM_TEST_URL` (default `http://localhost:8000`), `KEYCLOAK_URL` (default `http://localhost:8180`), `OM_URL` (default `http://localhost:8585`). All tests marked `pytest.mark.contract`; the module skips with a clear message if `/healthz` is unreachable.

- [ ] **Step 1: Write the tests**:
  - `test_nfr_ops_01_readyz_green`: 200, all checks ok (the `state_store` check is running against PostgreSQL).
  - `test_nfr_sec_01_keycloak_agent_token_accepted`: MCP `get_server_info` with `agent_token` succeeds; `caller.type == "agent"`.
  - `test_nfr_sec_03_keycloak_human_token_is_human`: same with `human_token`; `caller.type == "human"`; scopes include `lakeloom:review` (from group map).
  - `test_nfr_sec_01_wrong_audience_rejected`: `wrong_audience_token` → HTTP 401 on `/mcp` with `WWW-Authenticate` containing `resource_metadata=`.
  - `test_nfr_sec_02_prm_published`: `/.well-known/oauth-protected-resource` → `authorization_servers == ["http://localhost:8180/realms/lakeloom"]`.
  - `test_fr_om_06_server_info_reports_om_version_and_rdf`: `openmetadata.reachable is True`, `version` starts with `"2.1"`, `rdf_enabled` is a bool (or `None` only if ADR 0003 documents RDF status as absent).
  - `test_fr_om_08_om_client_against_real_om`: `OMClient(OM_URL, bot token read via `docker compose exec`/env `OM_BOT_TOKEN`)`.get_version() works.
- [ ] **Step 2:** Run against the stack: `uv run pytest -m contract tests/contract -v` → all pass. Then `docker compose stop openmetadata-server` and check by hand that `/readyz` → 503 naming `openmetadata` and `get_server_info` still answers with `reachable: false`; start it again.
- [ ] **Step 3: Commit** `test(contract): M0 acceptance against OM 2.1 + Keycloak (NFR-TST-02, FR-OM-08)`.

---

### Task 10: CI, supply-chain checks, quick start

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/dco.yml`, `.github/dependabot.yml`
- Modify: `README.md` (quick start), `CHANGELOG.md` (Unreleased: M0 entries)

**Interfaces:**
- Produces: CI jobs named `lint`, `typecheck`, `unit`, `contract`, `image` (later milestones extend them).

- [ ] **Step 1:** `ci.yml` on push to `main` and PRs, actions pinned to version tags, `astral-sh/setup-uv`:
  - `lint`: `uv run ruff check` + `uv run ruff format --check`.
  - `typecheck`: `uv run pyright`.
  - `unit`: matrix Python 3.12/3.13, `uv run pytest --cov=lakeloom --cov-report=xml`; `uv run pip-audit` (dependency scanning, NFR-SEC-17).
  - `contract`: `docker compose -f deploy/compose/docker-compose.yml up -d --wait` (timeout 20 min), `uv run pytest -m contract tests/contract`, dump `docker compose logs` on failure, `down -v` always.
  - `image`: `docker/setup-qemu-action` + `setup-buildx-action`; build `linux/amd64,linux/arm64` with `sbom: true`, `provenance: true`, no push; Trivy scan of the amd64 image, failing on CRITICAL/HIGH with fixes available.
- [ ] **Step 2:** `dco.yml`: on PRs, fail if any commit in the PR lacks a `Signed-off-by:` line matching its author.
- [ ] **Step 3:** `dependabot.yml`: `uv` (pip ecosystem), `github-actions`, `docker`, weekly.
- [ ] **Step 4:** README quick start: prerequisites (a Docker-compatible engine with ≥ 8 GB: Docker Engine, OrbStack, Podman or Rancher Desktop; uv), `docker compose -f deploy/compose/docker-compose.yml up -d --wait`, `curl localhost:8000/readyz`, getting an agent token from Keycloak with curl, calling `get_server_info` from an MCP client (Claude Code `claude mcp add --transport http lakeloom http://localhost:8000/mcp`), ports table, dev credentials marked "development only".
- [ ] **Step 5: Verify locally:** `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest` all pass; `actionlint` (via `docker run rhysd/actionlint`) reports no errors on the workflows.
- [ ] **Step 6: Commit** `ci: lint, typecheck, unit, contract and image jobs (NFR-OSS-03, NFR-SEC-17)`.
- [ ] **Step 7:** When the owner gives the go-ahead: `gh repo create elcioabrahao/lakeloom --public --source . --push`, enable private vulnerability reporting, and confirm CI is green on GitHub (last M0 acceptance item).

---

## Out of scope for M0 (owned by later milestones)

stdio transport (NFR-SEC-04, M1 with the CLI read tools), OpenTelemetry traces (ADR 0006, M1), Alembic migrations (first table, M1), token exchange / on-behalf-of OM calls (NFR-SEC-08–10, M2), OM SSO via Keycloak (M2), release/PyPI/GHCR publishing workflow and cosign (M4), docs site (M4), Helm (M4), rate limiting (M4).
