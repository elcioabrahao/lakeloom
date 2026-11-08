# 0003. OpenMetadata dev stack and committed OpenAPI spec

- Status: accepted
- Date: 2026-11-08
- Requirements: NFR-DEP-05, FR-OM-01, FR-OM-04, FR-OM-08, §0.4

## Context

Lakeloom needs a local stack with OpenMetadata (OM) 2.1, an OIDC issuer, a Git
remote and optionally a triple store, and its OM client has to be written against
the real OM API rather than the docs (§0.4). On 2026-11-08 the newest 2.1 tag is
**2.1.0-rc1** (`2.1.0-rc1-release`, published 2026-10-28); 2.1.0 GA is not out
(latest GA is 2.0.7).

## Decision

### Stack: `deploy/compose/docker-compose.yml` (project name `lakeloom`)

| Service | Image (pinned) | Host port | Notes |
|---|---|---|---|
| `postgresql` | `docker.getcollate.io/openmetadata/postgresql:2.1.0-rc1` | none | OM DB **and** the Lakeloom DB (see below) |
| `elasticsearch` | `docker.elastic.co/elasticsearch/elasticsearch:9.3.0` | none | 1 GB heap, as upstream |
| `execute-migrate-all` | `docker.getcollate.io/openmetadata/server:2.1.0-rc1` | — | one-shot migrations |
| `openmetadata-server` | `docker.getcollate.io/openmetadata/server:2.1.0-rc1` | 8585, 8586 | |
| `om-bootstrap` | `alpine:3.22.8` (+ `apk add curl jq`) | — | one-shot, writes bot token |
| `fuseki` (profile `rdf`) | built from `github.com/open-metadata/OpenMetadata.git#2.1.0-rc1-release:docker/rdf-store` (Fuseki 6.2.0), tagged `lakeloom-dev/openmetadata-fuseki:2.1.0-rc1` | none | optional |
| `keycloak` | `quay.io/keycloak/keycloak:26.8.2` | 8180→8080 | `start-dev --import-realm` |
| `gitea` | `gitea/gitea:1.27.3` | 3000 | SQLite, `INSTALL_LOCK=true` |
| `gitea-init` | `gitea/gitea:1.27.3` | — | one-shot: admin + repo |

Start: `docker compose -f deploy/compose/docker-compose.yml up -d` (≈50 s warm).

**Source of the OM services.** The release publishes four compose assets:
`docker-compose.yml` (MySQL), `docker-compose-postgres.yml`,
`docker-compose-openmetadata.yml` (server only) and `docker-compose-ingestion.yml`.
We copy `postgresql`, `elasticsearch`, `execute-migrate-all` and
`openmetadata-server` from **`docker-compose-postgres.yml`**. Two reasons: the
project owner made PostgreSQL the default database for OM and Lakeloom, and the
MySQL asset of this rc is mis-tagged (its image defaults are
`openmetadata/server:1.12.0-SNAPSHOT` / `db:1.12.0-SNAPSHOT`), while the postgres
asset pins `2.1.0-rc1` throughout. Deviations from upstream:

- the Airflow `ingestion` service is left out (not needed in M0; saves an Airflow container);
  consequently `PIPELINE_SERVICE_CLIENT_ENABLED` defaults to `false`;
- the identical server/migrate environment block is one YAML anchor
  (`x-om-environment`); it is upstream's migrate block (a superset of the server one);
- `ELASTICSEARCH_HOST`/`SEARCH_TYPE` defaults lose upstream's stray leading space;
- `container_name`s, bind-mount `./docker-volume/...` and host ports 5432/9200/9300
  are dropped (named volumes, nothing internal published);
- the server health check gets `interval`/`retries`/`start_period` so that
  `service_healthy` dependants wait for a slow first start.

**RDF.** Per OM's *Enabling RDF* page, both the migrate and the server containers
need the same `RDF_*` variables. Compose cannot set variables based on an active
profile, so the anchor carries `RDF_ENABLED=${RDF_ENABLED:-false}` plus
`RDF_STORAGE_TYPE=FUSEKI`, `RDF_ENDPOINT=http://fuseki:3030/openmetadata`,
`RDF_DATASET=openmetadata`, `RDF_REMOTE_USERNAME/PASSWORD=admin/admin`,
`RDF_BASE_URI=https://open-metadata.org/`, and the RDF stack is turned on with

```
RDF_ENABLED=true docker compose -f deploy/compose/docker-compose.yml --profile rdf up -d
```

Both OM containers depend on `fuseki` with `required: false`, so the dependency
only applies when the profile is active. OM ships no prebuilt Fuseki image (its own
compose files build `docker/rdf-store`), so we build from the same git tag; the
first `--profile rdf` start needs network access to build it. Upstream's 4 GB
Fuseki heap is reduced to 1 GB (`FUSEKI_JVM_ARGS`). Verified: `GET /api/v1/rdf/status`
→ `enabled: true, storageType: FUSEKI, projectionState: REBUILDING`; reaching
`READY` needs the initial index (`POST /api/v1/apps/trigger/RdfIndexApp`).

### Lakeloom database

An extra init script, `deploy/compose/lakeloom-db-init.sql`, is mounted into the OM
postgres image's `/docker-entrypoint-initdb.d/` (the image already uses that
mechanism for `openmetadata_db`). It creates role `lakeloom` / password `lakeloom`
and database `lakeloom` (dev only). It runs only on an empty volume. Lakeloom
(Task 8) connects with `postgresql://lakeloom:lakeloom@postgresql:5432/lakeloom`
(add a driver suffix such as `+asyncpg` if the engine factory needs one). Chosen
over a separate `postgres:17-alpine` service: one fewer container (~100 MB).
Note the OM image is PostgreSQL **15.19**.

### Bot token (`om-bootstrap`)

`om-bootstrap.sh` (endpoints checked against the committed spec):

1. `POST /api/v1/users/login` with `{"email": "admin@open-metadata.org", "password": base64("admin")}`
   → `accessToken`. A plain-text password returns 400; base64 is required.
2. `GET /api/v1/bots/name/ingestion-bot` → `botUser.id`.
3. `GET /api/v1/users/auth-mechanism/{id}` → `config.JWTToken`.
4. Sanity check `GET /api/v1/users/loggedInUser` with that token, then write it
   atomically to `/secrets/om_bot_token` (mode 0644) in named volume `om-secrets`.

The token never touches the host filesystem or git. To read it on the host (e.g. for
contract tests):

```
docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps \
  --entrypoint cat om-bootstrap /secrets/om_bot_token
```

(`docker compose cp om-bootstrap:/secrets/om_bot_token -` does not work once the
one-shot container is removed, so the `run` form is the documented one.) If a file
is preferred, write it to the git-ignored `deploy/compose/.secrets/`.

### Keycloak hostname / backchannel

`KC_HOSTNAME=http://localhost:8180` and `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`.
The front-channel URL, and therefore `iss`, is always
`http://localhost:8180/realms/lakeloom`, whether a token is requested from the host
or from inside the network. Back-channel endpoints follow the request host, so
inside the network the discovery document advertises
`jwks_uri = http://keycloak:8080/realms/lakeloom/protocol/openid-connect/certs`
(verified). Lakeloom therefore validates `iss = http://localhost:8180/realms/lakeloom`
and fetches keys via `LAKELOOM_OIDC_JWKS_URL` (ADR 0002). This avoids
`*.orb.local` names and `extra_hosts` hacks, so it works the same on OrbStack, Docker
Engine (CI), Podman and Rancher. Keycloak has no data volume: H2 state is
throw-away and the realm JSON is re-imported whenever the container is recreated.

Realm notes: because the realm JSON defines `clientScopes`, Keycloak creates none of
its built-ins, so the file defines the ones we need: `basic` (`sub`), `profile`,
`email`, `groups` (group names, full path off) and `service_account` (`client_id`
claim on client-credentials tokens, which ADR 0002's `OIDC_AGENT_CLAIM=client_id`
relies on), plus the five `lakeloom:*` scopes (realm optional scopes, included in
`scope`). `lakeloom-agent` has `lakeloom:read lakeloom:propose` as defaults and the
other three optional; `lakeloom-dev-cli` has all five optional; `other-app` has
`lakeloom:read` by default but no audience mapper, so its token differs from a valid
one only in `aud`. Users carry email/first/last name because Keycloak 26's user
profile otherwise rejects password grants ("Account is not fully set up").

### Gitea

`gitea/gitea:1.27.3`, the last 1.x release. The brief asked for "current 1.x";
Gitea has since moved to a new major line (`28.x`, latest `28.3.0`, 2026-11-01).
We stay on 1.27.3 as asked; moving to 28.x is a one-line change if 1.x support ends.

### Committed spec

`scripts/fetch_om_openapi.sh <om-url> <out>` tries `/swagger.json`,
`/api/swagger.json`, `/openapi.json`; **`http://localhost:8585/swagger.json`
worked** (OpenAPI 3.0.1, 1593 paths, 4.5 MB). Output is `jq -S .`; two fetches,
including one from a fresh stack, are byte-identical. Committed as
`src/lakeloom/om/openapi/openmetadata-2.1.0-rc1.json`. Its `info.version` says
`2.0.0-SNAPSHOT` (upstream metadata not bumped); `/api/v1/system/version` reports
`2.1.0-rc1` (revision `75cd5eef`).

The spec's paths are relative to `servers[].url = /api`; so spec `/v1/x` is
`/api/v1/x` on the wire.

| Documented (REQUIREMENTS) | Actual (spec, `/api` prefix) | Methods |
|---|---|---|
| `/api/v1/ontologyAxioms` | `/api/v1/ontologyAxioms` | GET, POST, PUT |
| `/api/v1/ontologyAxioms/validate` | `/api/v1/ontologyAxioms/validate` | POST |
| `/api/v1/ontologyChangeSets` | `/api/v1/ontologyChangeSets` (+ `/{id}/operations`, `/undo`, `/redo`, `/submit`, `/apply`, `/discard`) | GET, POST |
| `/api/v1/ontology/modeling/iris/preview` | **absent** (also 404 live); no replacement found | — |
| `/api/v1/glossaries/{id}/exportOntology` | `/api/v1/glossaries/{id}/exportOntology` | GET |
| `/api/v1/glossaries/{id}/sparql` | `/api/v1/glossaries/{id}/sparql` | POST |
| `/api/v1/rdf/status` | `/api/v1/rdf/status` | GET |
| `/api/v1/ontologyEditLocks` | **absent as a collection** (404 live); actual: `POST /api/v1/ontologyEditLocks/acquire`, `PUT /api/v1/ontologyEditLocks/renew`, `GET`/`DELETE /api/v1/ontologyEditLocks/{resourceType}/{resourceId}` | — |
| `/api/v1/system/version` | `/api/v1/system/version` | GET |
| "the import endpoint" (FR-OM-01) | `PUT /api/v1/glossaries/name/{name}/importRdf` (also `POST /api/v1/ontologies/{id}/imports/preview`) | PUT |

Also noticed for the OM client task: 2.1 has a first-class `/api/v1/ontologies`
resource family (releases, working diff, activation, state, scoped SPARQL) and
`/api/v1/ontologyConcepts`, `/api/v1/ontologyProperties`, `/api/v1/ontologyReleases`,
next to the glossary-based endpoints REQUIREMENTS assumes. Task 4 should decide which
family models "ontology model" before coding FR-OM-01.

### Memory budget

Measured with `docker stats --no-stream` (default profile, idle after start):
OM server ≈1.0 GiB, Elasticsearch ≈1.7 GiB, Keycloak ≈0.65–0.86 GiB, Gitea ≈0.14 GiB,
Postgres ≈0.1 GiB — **≈3.8 GiB total**. With `--profile rdf`, Fuseki adds ≈0.45 GiB
idle (1 GB heap cap). Fits the 8 GB minimum with room for Lakeloom (Task 8).

## Consequences

- Contract tests (FR-OM-08) run against a pinned rc; when 2.1.0 GA ships, bump the
  four OM tags and the Fuseki git ref, re-run the fetch script, and diff the spec.
- `FR-OM-01` IRI preview and `FR-OM-04` lease paths must follow the table above,
  not REQUIREMENTS; IRI preview has no spec equivalent and needs a decision
  (client-side minting or a later OM version) in the OM client task.
- All credentials in the stack are public dev defaults; `.env.example` marks them.

## Appendix (Task 8): Lakeloom in the stack, and the CI wait command

- `docker compose -f deploy/compose/docker-compose.yml up -d --wait` returns 0 with the
  one-shot services (`execute-migrate-all`, `om-bootstrap`, `gitea-init`) present, on
  Compose v5.1.2: services that exit 0 are accepted by `--wait`. Task 10's CI uses exactly
  this command (add `--wait-timeout 600` on slow runners). A cold start took ~50 s warm.
- `lakeloom` depends on postgres, openmetadata-server and keycloak (healthy) and on
  `om-bootstrap` and `gitea-init` (`service_completed_successfully`).
- Postgres healthcheck now requires TCP on 127.0.0.1 and the `lakeloom` role/db
  (`pg_isready` + `psql`), because the init-script phase runs a temporary socket-only server.
- All published ports bind to 127.0.0.1: 8585 (OM), 8180 (Keycloak), 3000 (Gitea),
  8000 (Lakeloom), 5432 (Postgres, for `LAKELOOM_TEST_DB_URL`). The OM admin port 8586 is
  no longer published (the healthcheck runs inside the container).
- The OM bot token in `om-secrets` is written `10001:10001`, mode 0440 (Lakeloom runs as
  uid 10001, read-only root filesystem, `/tmp` tmpfs, state in the `lakeloom-data` volume).
- Image pins: `ghcr.io/astral-sh/uv:0.11.12`, `python:3.12.14-slim-trixie`.
