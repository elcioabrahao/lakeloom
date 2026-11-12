# 0012. Review and human identity in OpenMetadata

- Status: accepted (§ SSO, § Apply research; later sections follow in M2b)
- Date: 2026-11-12
- Requirements: FR-OM-04, FR-OM-05, FR-REV-02, NFR-SEC-08, NFR-SEC-09, NFR-SEC-10, NFR-SEC-11

## Context

M2b lets a human reviewer apply ontology change sets. ADR 0011 found that the OM bot can
apply any change set with no reviewer, so applying with the bot JWT would hide who
approved a change. FR-OM-05 asks for OM calls made with the human's own identity (an
on-behalf-of token). NFR-SEC-08 prefers RFC 8693 token exchange for that. NFR-SEC-09 falls
back to forwarding the user's token when OM accepts its audience.

Until M2a the dev stack ran OM 2.1.0-rc1 with basic auth. Its users (`admin`) were
unrelated to the Keycloak realm that Lakeloom trusts. So the first step was to move OM to
Keycloak SSO and find out what OM checks. All findings below were observed live on
2026-11-12 against the compose stack (OM `2.1.0-rc1`, Keycloak `26.8.2`). Tokens were kept
in variables, and only their claims were printed.

## Decision

### SSO wiring (dev stack)

**Keycloak realm `lakeloom`** (`deploy/compose/keycloak-realm.json`). All values are
fixed, public DEV ONLY defaults.

- Client `openmetadata`: public, standard flow with PKCE (S256), redirect
  `http://localhost:8585/*`, implicit flow off. It is OM's UI client and the audience of
  OM tokens. Its default scopes include `openmetadata-audience`, so its access tokens also
  carry `aud: openmetadata`. The ID token has that audience anyway.
- Optional client scope `openmetadata-audience`: an audience mapper
  (`included.client.audience: openmetadata`) on access tokens.
- Client `lakeloom`: confidential (secret `dev-lakeloom-secret`). It has no flows of its
  own: no standard flow, no direct grants, no service account. It has
  `standard.token.exchange.enabled: "true"` and the optional scope
  `openmetadata-audience`.
- `lakeloom-dev-cli` also gets `openmetadata-audience` as an optional scope. Contract tests
  and `om-bootstrap` use it to get OM tokens for realm users with a password grant. This
  is dev only: the client already allows direct grants for tests.
- Users (password = user name): `om-admin` (no groups), `alice` and `carol` (group
  `ontology-reviewers`), and `bob` (`data-engineers`, unchanged).

**OM environment** (`x-om-environment` in the compose file):
`AUTHENTICATION_PROVIDER=custom-oidc`, `CUSTOM_OIDC_AUTHENTICATION_PROVIDER_NAME=keycloak`,
`AUTHENTICATION_AUTHORITY=http://localhost:8180/realms/lakeloom`,
`AUTHENTICATION_CLIENT_ID=openmetadata`,
`AUTHENTICATION_CALLBACK_URL=http://localhost:8585/callback`,
`AUTHENTICATION_RESPONSE_TYPE=code` (the client has the implicit flow off; with the default
`id_token`, Keycloak answers `unauthorized_client … Implicit flow is disabled`),
`AUTHENTICATION_PUBLIC_KEYS=[http://localhost:8585/api/v1/system/config/jwks,
http://keycloak:8080/realms/lakeloom/protocol/openid-connect/certs]`,
`AUTHORIZER_ADMIN_PRINCIPALS=[om-admin]` and `AUTHORIZER_PRINCIPAL_DOMAIN=example.invalid`.
The principal claims stay at the default `[email,preferred_username,sub]`, and
`AUTHENTICATION_CLIENT_TYPE` stays `public`. `openmetadata-server` now depends on
`keycloak` being healthy, so the JWKS is reachable when OM starts.

**`om-bootstrap.sh`**: gets an `om-admin` token from Keycloak with a password grant on
`lakeloom-dev-cli` and `scope=openid openmetadata-audience`. It checks
`GET /v1/users/loggedInUser` → `isAdmin: true`. Then it creates the OM users `alice`,
`bob` and `carol` if they are missing (`POST /v1/users`, email `<name>@example.invalid`).
It creates the reviewer policy and role if missing and grants the role to alice only
(JSON Patch on `/roles`). Last, it reads the `ingestion-bot` JWT as before. Each step
looks up before it writes, so a re-run only logs "exists" (verified). No password or
token is echoed.

### What OM 2.1.0-rc1 checks on a Keycloak token

Read in `JwtFilter`/`ProviderTokenValidator` (tag `2.1.0-rc1-release`) and confirmed live:

- **Signature.** The `kid` is looked up across the configured public-key URLs. An
  unknown `kid` gives 401 `Token signing key not found in configured public keys`. A
  known `kid` with a different key gives 401 `Token verification failed. Public key
  mismatch`. `exp` is checked.
- **Audience.** This check applies to every token *not* signed with OM's own key. `aud`
  must contain a configured client id, either `AUTHENTICATION_CLIENT_ID` or
  `OIDC_CLIENT_ID`, by exact match. Otherwise OM returns 401 `Token audience [lakeloom]
  does not include a client configured for this deployment`. A token with no `aud` gets
  the same error with `[]`. **`azp` is not checked.** An access token whose
  `azp` is `lakeloom-dev-cli` or `lakeloom` is accepted when `aud` has `openmetadata`.
- **Issuer.** OM compares `iss` exactly, but only with an issuer it learned through its
  server-side (confidential) code-flow handler. With `AUTHENTICATION_CLIENT_TYPE=public`
  there is no such handler, so **`iss` is not checked**. Trust then rests on the JWKS list:
  only keys of the Keycloak realm (and OM's own key) validate. OM logs at start-up: `OIDC
  provider custom-oidc requires CONFIDENTIAL client type, but got: public`.
- **User name.** With the legacy `jwtPrincipalClaims` `[email, preferred_username, sub]`,
  the first claim present wins. The user name is the local part of `email`
  (`alice@example.invalid` → `alice`). The email must match the OM user's email. The admin
  principal is created at start-up as `om-admin@<AUTHORIZER_PRINCIPAL_DOMAIN>`. With
  the default domain `open-metadata.org`, the `om-admin` Keycloak token (email
  `om-admin@example.invalid`) got **400 `Account already exists. Please contact
  administrator.`**. Hence `AUTHORIZER_PRINCIPAL_DOMAIN=example.invalid`.
- **Unknown user.** A valid Keycloak token for a user OM does not know gets **404 `Cannot
  find user with provided name and email`** on `GET /v1/users/loggedInUser`. The API does
  not provision users, even with `enableSelfSignup: true`, so `om-bootstrap` creates them.
  Keycloak's `sid` claim is not mistaken for an OM session.
- **Bot JWT.** It still validates through the first public-key URL (OM's own JWKS), and
  `loggedInUser` returns `ingestion-bot`.
- `GET /v1/users/loggedInUser` with alice's OM-audience token returns `alice`
  (`isAdmin: false`). With om-admin's token it returns `om-admin` (`isAdmin: true`).
- `GET /v1/system/config/auth` reports `provider: custom-oidc`, `providerName: keycloak`,
  the authority and `clientId: openmetadata`.

### Token exchange (NFR-SEC-08): works

Request: as client `lakeloom` (client secret in the body),
`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, `subject_token` = alice's
Lakeloom access token (from `lakeloom-dev-cli`, `aud: lakeloom`),
`subject_token_type=…:access_token`, `requested_token_type=…:access_token`,
`audience=openmetadata`, **`scope=openmetadata-audience`**.

- Without `scope=openmetadata-audience`, Keycloak 26.8 (standard token exchange V2) answers
  400 `invalid_request` / `Requested audience not available: openmetadata`. V2 only grants
  audiences that the requested client scopes make available. With the scope and no
  `audience` parameter, the result is the same as with both.
- Result: an access token with `aud: openmetadata` only, `azp: lakeloom`, the same `sub`,
  `preferred_username` and `email` as the subject, `scope: profile email`, and
  `expires_in` 300. OM accepts it as `alice`. carol's exchanged token is accepted as
  `carol`.
- A subject token whose `aud` does not contain `lakeloom` gets 403 `access_denied` /
  `Client is not within the token audience`. An audience the scopes do not grant gets 400
  `Requested audience not available: <client>`.

### Forwarding (NFR-SEC-09): not possible with this realm

alice's Lakeloom token (`aud: lakeloom`) sent to OM gives **401 `Token audience [lakeloom]
does not include a client configured for this deployment`**. Forwarding would work only if
Lakeloom's own tokens also carried `openmetadata` in `aud`. That widens every Lakeloom
token into an OM token, so it is not done. Token exchange is the path.

### OM reviewer role: `LakeloomOntologyReviewer` (created by bootstrap)

Probed with throwaway glossaries and native ontologies that the bot created. Each change
set (glossary: `UPDATE_TERM` on a bot-created term plus `CREATE_TERM`; native:
`UPSERT_CONCEPT`) was created and submitted by the bot and then applied by the probed user.
Everything was hard-deleted afterwards.

| Principal (OM roles) | Lease (`POST /v1/ontologyEditLocks/acquire`) | Apply glossary | Apply native |
|---|---|---|---|
| carol (default `DataConsumer` via the Organization team) | **403** `operations [EditAll] not allowed` | — | — |
| `DataSteward` (temporarily on bob) | **403** `operations [EditAll] not allowed` | — | — |
| `ViewAll, EditAll` on `ontologyChangeSet` only | 200 | **403** `[EditAll]` | **403** `[EditAll]` |
| … plus `glossary`, `ontology` | 200 | 200 APPLIED | **403** `[Create]` |
| … plus `Create` on `ontologyConcept` | 200 | 200 APPLIED | 200 APPLIED |
| alice (`LakeloomOntologyReviewer`) | 200 | 200 APPLIED (2/2) | 200 APPLIED (1/1) |

OM's default roles are not enough. `DataSteward` has no `EditAll`, and the change-set lease
already needs `EditAll` on `ontologyChangeSet`. So bootstrap creates policy
`LakeloomOntologyReviewerPolicy`. Task 1 gave it one broad allow rule (`ViewAll`, `Create`,
`EditAll` on seven resources); Task 2 trimmed it to the measured minimum (§ Apply research,
"Reviewer role: the rule set"). Role `LakeloomOntologyReviewer`
holds that policy and is granted to **alice only**. carol (same Keycloak group) and bob do
not get it. The applied term showed `updatedBy: alice`, so the reviewer's identity reaches
OM's history. The probe also saw the applied `UPDATE_TERM` change its description, unlike
the M2a observation in ADR 0011. Task 2 re-checks this.

That first rule set was a superset of what the probe needed; Task 2 measured each operation
kind and replaced it (below).

### Data volume

First, auth was switched in place on the old OM data. The Keycloak tokens validated, but
the old basic-auth users carried over. `admin` stays in the database but can no longer log
in. The `om-admin` principal was created at start-up with an `@open-metadata.org` email,
which clashed with the Keycloak email ("Account already exists"). A from-scratch `up` (as
in CI) does not have these problems, so the dev data volumes `lakeloom_om-db-data` and
`lakeloom_es-data` were removed and re-created. `om-db-data` also holds the dev `lakeloom`
state-store database. After an upgrade from an older checkout, run
`docker compose … down` and remove those two volumes once. Basic-auth users do not carry
over.

### Apply research (M2b Task 2, live 2026-11-12)

Method: scratch scripts (not committed). For each run the bot created a throwaway glossary and
paired native ontology (`ll-r2-<hex>`), a bot-created term `Base`, and a throwaway table in
`lakeloom_fixture.retail.sales`; it drafted every change set with `to_om_all` (or the ADR 0011
payloads) and submitted it. The probed user then applied each set with an exchanged token
(lease, then `POST /v1/ontologyChangeSets/{id}/apply`). Afterwards everything was
hard-deleted, nothing was sealed, and `retail`'s version and `updatedAt` were unchanged. To
find the minimal rules, a temporary policy `LLProbePolicy` / role `LLProbeRole` held one
candidate rule set and was granted to bob (who has no OM roles) for one run. The grant was
removed and both were hard-deleted after each run.

**Apply.**

- An applied `CREATE_TERM` **keeps its draft id** (Lakeloom's uuid5 `draft_term_id`). The
  operation result's `entity.id` and the stored term both have it, at version **0.1**. This
  corrects ADR 0011 ("Ids of created entities are not kept"). Native upserts keep their
  uuid5 ids too (`ontologyConcept`/`ontologyProperty` ids are the `_uid(...)` values).
- An applied `UPDATE_TERM` keeps the term's id, moves it 0.1 → 0.2, and **does change
  `description`** ("Base original" → "Base UPDATED"), together with synonyms. The M2a rc1
  defect (ADR 0011: description not updated) did not reproduce in any run. Task 1 saw
  the same.
- `DELETE_TERM` soft-deletes: the term has `deleted: true` at the next version and is
  404 without `include=all`.
- `UPSERT_MAPPING` on an existing term applies.
- A **SUBMITTED** set needs no other transition: apply moves it straight to `APPLIED`. A
  failed apply (400, 412, 403) leaves it `SUBMITTED` with no `applicationResult`. OM releases
  the lease on apply: a later `DELETE …/ontologyEditLocks/…` answers 404, which
  `OMClient._release` ignores.
- Result body (`applicationResult`): `{appliedAt, appliedBy, operationsProcessed,
  operationsApplied, operationsFailed, results: [{operationId, status: APPLIED, entity:
  {id, type, name, fullyQualifiedName, description, displayName, deleted, href},
  entityVersion}]}`. `OMOperationResult` lifts `entity.id` / `entity.fullyQualifiedName`
  to `entity_id` / `entity_fqn`.

**Cross-part staleness.**

- Two native sets drafted at `baseWorkingRevision` 1: the first applies (the revision goes
  1 → 3, one step per definition). The second then fails **412** `Ontology changed since
  this draft was created` and stays `SUBMITTED` → `OMConflict(kind="stale_revision")`. Each
  later native part must be re-drafted at the current revision.
- A glossary `ADD_RELATIONSHIP` drafted in another part, with `targetId` and `fromTerm` set
  to the draft id of a term created by an earlier part and `baseVersion` 0.1, **applies**
  once that earlier part is applied (`Gadget relatedTo Widget`, Gadget 0.1 → 0.2). This works
  because draft ids are kept: there is nothing to remap, as long as parts are applied in
  order and the endpoint term is still at 0.1.
- Stale glossary base: an `UPDATE_TERM` at `baseVersion` 0.1 on a term now at 0.2 →
  **400** `Ontology operation '<op id>' expected version 0.1 but found 0.2` →
  `OMConflict(kind="stale_base")`, with the operation id in `entities`.

**Native.**

- `UPSERT_PROPERTY` `kind: OBJECT` (the relation's object property, domain and range the two
  classes) **applies** (backlog item closed), in one set with the `UPSERT_CONCEPT` that
  declares its domain class. Two `to_om_all` calls concatenated into one draft gave the same
  `UPSERT_CONCEPT` twice. rc1 rejected that at create with 400 `A native draft may change
  each definition only once`. One call with all operations does not duplicate it.
- `UPSERT_AXIOM` (cardinality restriction), `DELETE_PROPERTY`, `DELETE_AXIOM` and
  `DELETE_CONCEPT` apply.

**Approver.** `applicationResult.appliedBy` is `alice` for every set alice applies. The
created and updated terms, concepts and properties have `updatedBy: alice`. A tag she adds
has `appliedBy: alice`, and the table has `updatedBy: alice`. The bot only drafted and
submitted.

**Permissions.**

- Endpoint: `GET /v1/permissions/{resource}/{id}` (spec `getResourcePermission`). With no
  `user` parameter it answers for the caller: `{"resource": "glossary", "permissions":
  [{"operation", "access", "rule"?, "policy"?, "role"?}]}`. `access` is one of `allow`,
  `deny`, `notAllow`, `conditionalAllow`, `conditionalDeny`. The native ontology's entity type
  is `ontology` (as in `/v1/ontologies`), so `OMClient.get_permissions` takes
  `"glossary" | "ontology"`.
- alice on a bot-created glossary / ontology: `EditAll: allow` (from
  `LakeloomOntologyReviewerPolicy`); `Delete: notAllow`.
- carol: `EditAll: notAllow`, but `ViewAll`, `EditDescription`, `EditTags`,
  `EditGlossaryTerms` are `allow` (DataConsumer). Without an id
  (`/v1/permissions/ontologyChangeSet`), carol's `EditAll` reads `conditionalAllow`, because
  owner conditions cannot be evaluated without an entity. So Lakeloom asks per entity and
  treats only `allow` as yes (`OMPermissions.allows`).
- What OM checks (the 403 text names the operation, e.g. `Principal:
  CatalogPrincipal{name='carol'} operations [EditAll] not allowed`):

| Step | OM check |
|---|---|
| lease (`ontologyEditLocks/acquire` on a change set) | `EditAll` on `ontologyChangeSet` |
| apply a glossary set (create/update/delete term, relationship, mapping) | `EditAll` on `glossary` |
| apply a native set | `EditAll` on `ontology` |
| … new concept / property / axiom | `Create` on `ontologyConcept` / `ontologyProperty` / `ontologyAxiom` |
| … `DELETE_CONCEPT` / `DELETE_PROPERTY` / `DELETE_AXIOM` | `Delete` on that member type |
| … `UPSERT_*` of an *existing* member (`targetId`) | `EditAll` on that member type |
| tag a table or column with a term (JSON Patch on `/tags`) | `EditTags`/`EditGlossaryTerms` (DataConsumer) |

  `DELETE_TERM` needs no `Delete` (it is a soft delete under the glossary's `EditAll`).
  `glossaryTerm` needs no rule. No `ViewAll` rule is needed, because DataConsumer grants
  `ViewAll` on all resources.
- `APPLY_PERMISSIONS` (`lakeloom.om.models`) = `{"glossary": ("EditAll",), "ontology":
  ("EditAll",)}`: the per-target checks Task 5 can make in advance with
  `get_permissions`. The lease and member checks happen only at apply, and their failure is
  an `OMForbidden` (403) with the operation in `detail`.

**Reviewer role: the rule set** (`deploy/compose/om-bootstrap.sh`; evidence: one
probe-role run per variant):

| Rule | Resources | Operations | Without it |
|---|---|---|---|
| `LakeloomReviewerChangeSetLease` | `ontologyChangeSet` | `EditAll` | every apply: lease 403 `[EditAll]` (carol) |
| `LakeloomReviewerApplyTargets` | `glossary`, `ontology` | `EditAll` | without `glossary`: glossary apply 403 `[EditAll]`; without `ontology`: native apply 403 `[EditAll]` |
| `LakeloomReviewerNativeDefinitions` | `ontologyConcept`, `ontologyProperty`, `ontologyAxiom` | `Create`, `Delete` | concept only: property/axiom upserts 403 `[Create]`; without `ontologyAxiom`: axiom 403 `[Create]`; `Delete` only on property: `DELETE_CONCEPT` 403 `[Delete]` |

Dropped from Task 1's rule: `ViewAll` (DataConsumer has it), `Create` on `glossary`,
`glossaryTerm`, `ontology` and `ontologyChangeSet`, all of `glossaryTerm`, and `EditAll` on
the native member types. `EditAll` on members would only be needed to update an existing
native definition in place (403 `[EditAll]` measured). Lakeloom never drafts that: `to_om_all`
emits `UPSERT_*` only for new definitions, and removals as `DELETE_*`. `DELETE_AXIOM` was
measured only with `Delete` granted on all three member types. Requiring `Delete` on
`ontologyAxiom` is inferred from the concept and property cases. With this rule set, alice ran
the whole scenario: every set applied except the expected 412, the expected stale-base 400,
and the in-place member update (403, not used). bob and carol (no role) are refused at the
lease.

Bootstrap converges the rules. When the policy exists, its rules (compared by name, effect,
condition and sorted resources/operations) are replaced with JSON Patch `replace /rules` if
they differ ("updated policy … rules"). Otherwise it logs "rules current". The first run on
the Task 1 dev volume updated it; the second reported current.

**Tags.** As alice, a JSON Patch `[{"op": "add", "path": "/columns/0/tags", "value": [...own
tags, {tagFQN: <term>, source: Glossary, labelType: Manual, state: Confirmed}]}]` (content
type `application/json-patch+json`) on `PATCH /v1/tables/{id}` → 200, with the tag
`appliedBy: alice`. Clearing the list works the same way. carol has `EditTags` and
`EditGlossaryTerms` on tables from DataConsumer, so tagging is **not** gated by the
reviewer role. Lakeloom's own gate (`lakeloom:review`) is what limits it. A JSON Patch
`test` op (to guard the column index against a concurrent column change) makes rc1 demand
**`EditAll` on the table** (403), so `add_asset_tags` does not use one. The index comes from
the read just before, as in the seed script. The merge rules (`lakeloom.om.tags`): keep the
asset's own tags, drop `labelType: Derived` ones (a table's tags shown on its columns), and
add or remove only the glossary tag. No write happens when nothing changes (`False`).

**Client surface** (`lakeloom.om.client`): `with_token` (shares the `httpx.AsyncClient`,
does not own it, and no repr shows a token), `apply_change_set`, `get_permissions`,
`get_current_user` (`/v1/users/loggedInUser`), `add_asset_tags` / `remove_asset_tags`
(`PATCH /v1/tables/{id}`). All paths are in `PATHS` and checked against the spec. A 403 raises
`OMForbidden`. Every `OMError` now has a `hint` (forbidden, `stale_base`, `stale_revision`,
`lease`, `exists`). `OMConflict.entities` aliases `entity_ids`.

### On-behalf-of (`lakeloom.core.obo.OnBehalfOf`)

`mode` is `exchange` (exchange on, `LAKELOOM_OIDC_CLIENT_ID` and secret set), `forward`
(exchange off, `LAKELOOM_OM_FORWARD_USER_TOKEN=true`) or `unavailable` (human review fails
closed; the bot is never used). `get_server_info` reports it as `review_identity`. The
exchange form carries `audience=<LAKELOOM_OM_AUDIENCE>` and, unless
`LAKELOOM_OM_EXCHANGE_SCOPE` is set to an empty value (read as unset), `scope=<LAKELOOM_OM_EXCHANGE_SCOPE>` (default
`openmetadata-audience`, the finding above). An exchange error is `forbidden` (400/401/403)
or `upstream_unavailable` and never falls back to forwarding. Exchanged tokens are cached by
the SHA-256 of the subject token until `expires_in - 30 s`.

## Consequences

- A Lakeloom reviewer action can reach OM as the reviewer through token exchange
  (client `lakeloom`, `audience=openmetadata`, `scope=openmetadata-audience`). OM then
  enforces its own role. Lakeloom's `lakeloom:review` scope (group map) and OM's
  `LakeloomOntologyReviewer` role are separate gates: carol has the first but not the
  second.
- OM does not check `iss` or `azp` in public-client mode. Any client of the realm that can
  mint a token with `aud: openmetadata` can call OM as that user. In this realm that is
  `openmetadata`, `lakeloom` (exchange) and `lakeloom-dev-cli` (dev only). A production
  deployment should run OM as a confidential client, which also enables the issuer check,
  and should keep the OM audience scope off general-purpose clients.
- OM's default `DataConsumer` role (every user, through the Organization team) already
  grants `EditDescription` and `EditGlossaryTerms` on all resources. carol can therefore
  edit term descriptions directly in OM, outside change sets. Lakeloom cannot prevent
  that. It is an OM-side governance setting.
- The OM UI login (browser, code + PKCE on client `openmetadata`) was checked on the
  Keycloak side only. A scripted authorization-code flow with PKCE to
  `http://localhost:8585/callback` returned ID and access tokens that OM accepts as
  `alice`. The OM UI itself was not driven in a browser. Given the start-up warning above,
  a manual login at http://localhost:8585 is still to be confirmed.
- The bot token keeps working. All earlier contract tests pass unchanged on the new stack.
