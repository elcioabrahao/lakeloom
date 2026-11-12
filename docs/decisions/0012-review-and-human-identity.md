# 0012. Review and human identity in OpenMetadata

- Status: accepted (M2b complete; acceptance test `tests/contract/test_m2b_acceptance.py`)
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

### Rebuild (M2b Task 4)

`ProposalService.rebuild_parts(p, parts, *, om, submit, trail)` re-drafts parts of a
proposal against OM's current state. It is the primitive behind `rebase_proposal` (FR-OM-03)
and the review service's re-draft of later parts. All its OM calls go through the `om`
it is given, reads included (the bot for rebase, the reviewer's client in review), so a
review path never falls back to the bot.

- **Fail closed first.** One fresh `OMContext` (current term ids and versions; concepts
  created by a part whose glossary set is `APPLIED` now resolve as existing terms, with
  their kept draft ids) is read, and every part's payload is built with `to_om_all` (one
  call per part, so no duplicate `UPSERT_CONCEPT`) before anything is written. A removal
  of a native definition OM no longer has is a `conflict` naming it.
- **Per part, in order:** the part's rebuild count `k` goes up by one; each side that is
  not `APPLIED` and not already gone is read (`get_change_set`) and discarded (a 404 counts
  as gone); each such side is recreated as `<original name>-r<k>` (the name before the
  first rebuild, kept in `ProposalPart.original_names`; well under OM's 256-character
  limit and within its name pattern), with a fresh `baseWorkingRevision`, and the old id
  is moved to `ProposalPart.superseded_change_set_ids`. With `submit` the new sets are
  submitted. An `APPLIED` side is never rebuilt; a partially applied part (glossary
  applied, native not) gets a new native set only.
- **Progress** is stored after each part, and before an error propagates. A side discarded
  but not yet replaced keeps its old id with state `DISCARDED`; the next rebuild (rebase,
  or `propose_change` / `submit` on that draft) continues from there. Every change set OM
  holds stays recorded (current, superseded or abandoned).
- **Applied parts and resumption (fix round 1).** `submit` (like every transition) skips
  sides already `APPLIED`. Rebase hands the refreshed operations to `rebuild_parts`, which
  assigns them (and their warnings) to a part only when it rebuilds that part, so a part a
  failed rebase never reached keeps the operations its change sets in OM hold. A rebase
  that stops part-way stores `Proposal.rebase_pending`; the next `propose_change`,
  `submit` or `rebase` rebuilds every part that is not fully applied again. In a part whose
  glossary set is `APPLIED`, terms its `RemoveConcept` deleted are not looked up; its
  native side still gets the `DELETE_CONCEPT` of the class (which still exists).
- **Rebase** (`ProposalService.rebase`, audited as `rebase_proposal`): owner (or admin)
  only, and the model's `propose_groups`; status `draft`, `submitted`,
  `changes_requested` or `apply_failed`. It refreshes the change-set states from OM,
  rebuilds every part that is not fully applied (every side with a change set
  `APPLIED`) with the bot, and the proposal becomes a `draft`. Agent-supplied base versions
  (`UpdateConcept`, `RemoveConcept`) are set to the term's current OM version with the
  warning "base version of `<fqn>` (quoted as code) refreshed from X to Y; re-review the change"; a removal's
  native base (taken from OM, never from the agent) that moved since the replaced set gets
  the same warning. A term OM no longer has is a `conflict` naming it, and the proposal is
  unchanged. `propose_change` and `submit` also accept `changes_requested`: they run the
  same rebuild (drafts, not submitted) first, then append or submit.
- **Access groups** (NFR-SEC-07): `models[].access.{propose_groups, review_groups}` in the
  config file; `model_policy.may` is the predicate and `require_group` raises `forbidden`
  "model <m> restricts <action> to groups …". An empty list allows everyone; the CLI
  operator (`Actor`) is exempt. Proposal entry points that create or extend a proposal
  (`propose_from_scan`, `propose_change`, `propose_realization`, `rebase`) check
  `propose`. `Caller` and `Actor` moved to `lakeloom.core.callers` (re-exported by
  `lakeloom.core.proposals`).

### Review (M2b Task 5)

`lakeloom.core.review.ReviewService.review(proposal_id, decision, *, principal, client,
reason, confirm_token)` decides `approve`, `reject` or `request_changes` (FR-REV-01..03),
audited as `review_proposal`.

- **Checks, in order, nothing sent to OM on a refusal:** human principal (an agent gets
  `forbidden` "agent principals cannot review proposals (FR-REV-01)"); the
  `lakeloom:review` scope; the model's `review_groups`; a reason (reject) or comment
  (request_changes), stored as `UntrustedText` capped at 4,000 characters; the status
  (approve: `submitted` | `apply_failed`; reject: `submitted` | `changes_requested` |
  `apply_failed`; request_changes: `submitted`); `OnBehalfOf.om_for` (its error is final,
  never the bot); and `get_permissions` on the glossary and, when paired, the native
  ontology must allow `APPLY_PERMISSIONS`, else `forbidden` "OpenMetadata does not grant
  <user> review permission on <model>". `request_changes` touches nothing in OM, so it
  stops after the status check and needs no OM identity (it works with `review_identity`
  `unavailable`).
- **Confirm token** (approve, reject): preview (summary + structured diff) -> token ->
  execute. The digest binds the reviewer, the decision, the proposal's status and revision,
  and every change set's OM version and state (read as the reviewer). The whole run holds
  the proposal lock and stores with compare-and-set, so a second confirm of the same token
  (concurrent or replayed) is a `conflict`; with a token, a status that no longer allows
  the decision is also a `conflict`.
- **Reviewer identity only.** Every OM call of a review (permissions, state reads, rebuild,
  apply, tags, discards, the description read-back) goes through the reviewer's client;
  `ProposalService` is used only for its lock, its store write and `rebuild_parts(om=…)`.
  The unit tests record the token of every fake OM call and assert no review call carried
  the bot token.
- **Approve** applies part by part, in order, glossary side then native side, after
  reading each side's state from OM (a set an earlier attempt applied counts as applied,
  so a retry continues from the first unapplied part). **Mechanical staleness** (the only
  automatic re-draft): before a native side, its `baseWorkingRevision` behind the
  ontology's `workingRevision` (an earlier part was just applied), or a side an
  interrupted rebuild discarded → `rebuild_parts([part], om=reviewer, submit=True)` from
  the same reviewed operations, old ids in `superseded_change_set_ids`. Glossary sides are
  applied as drafted (draft ids survive apply). A `stale_revision` at apply gets one
  rebuild and retry; a second is `apply_failed`. A `stale_base` (an agent-supplied
  `base_version`) is never refreshed here: `conflict` naming OM's entity ids, hint "call
  rebase_proposal, then submit_proposal for a new review", status `apply_failed`. Other
  failures: `apply_failed` with progress stored; `OMForbidden` → `forbidden`. The OM
  state is stored after every side.
- **Realizations** are tagged after all parts (`add_asset_tags` / `remove_asset_tags`,
  term FQN = the concept's FQN, `<model glossary>.<Concept>` for a bare name), skipping op
  keys in `Proposal.realizations_applied`; a tag failure is `apply_failed` and the retry
  tags only the rest.
- **Success:** status `applied`; a `ReviewRecord` (decision, reviewer subject and user
  name, `om_user` = `applicationResult.appliedBy`, applied change set ids, warnings);
  `ModelService.invalidate(model)` (also after a partial apply). A description an applied
  `UpdateConcept` carried is read back; if OM holds another, the record gets a warning.
  On rc1 this is normally silent (UPDATE_TERM descriptions apply, above). A failed attempt
  also leaves a `ReviewRecord` whose last warning says where it stopped.
- **Reject** discards every change set that is not applied, as the reviewer (404 = gone);
  status `rejected`; the reason is stored. **Request changes**: status
  `changes_requested`, comment stored.
- **Reviews are provenance** (FR-REV-03): `Proposal.reviews` is stored with the proposal
  and rendered by `summary_markdown` under "Reviews", reasons as quoted untrusted text.
- **Open statuses and applied parts (ruling B).** `OPEN_STATUSES` is now `draft`,
  `submitted`, `changes_requested`, `apply_failed`: their operations count as pending for
  dedupe, and they can be discarded. Discard and reject of a proposal with applied parts
  discard only the unapplied sides; the preview, the stored record (`warnings` of the
  proposal on discard, of the `ReviewRecord` on reject) and the summary say "parts already
  applied stay in OpenMetadata: …". OM has no undo for an applied change set.

### Pending reviews (M2b Task 6)

`list_pending_reviews` (and `lakeloom pending`) list `submitted` and `apply_failed` proposals,
oldest first, keyset-paginated by proposal id (`ProposalRepository.list_by_status`; opaque
base64 cursor). For a caller, models whose `review_groups` exclude them are skipped and so are
proposals they created; the CLI operator sees all. Grouping is by model, then data product.
**Data product = lake schema:** the stored provenance holds no OM `dataProducts`, and the
listing makes no OM calls, so a proposal's data product is the `catalog.schema` of its tables'
lake full names (the first, sorted, when it spans several; `(no table)` for realization-only
proposals). Age is the time since the proposal was last stored (its submission). There is no
CLI review command: a decision must carry a human's own OM identity, which the CLI operator
(bot credentials) does not have.

### Dev stack wiring and acceptance (M2b Task 7)

The Lakeloom container runs in `exchange` mode: `LAKELOOM_OIDC_CLIENT_ID=lakeloom`,
`LAKELOOM_OIDC_CLIENT_SECRET=dev-lakeloom-secret` (DEV ONLY) and
`LAKELOOM_OIDC_TOKEN_ENDPOINT=http://keycloak:8080/realms/lakeloom/protocol/openid-connect/token`.
The token endpoint is set explicitly because the issuer host (`localhost:8180`) is not
reachable from inside the container; Keycloak's back channel follows the request host, so
the exchange on the compose-network host works for tokens issued by `localhost:8180`.
Forward mode cannot work in this stack (OM refuses `aud: lakeloom`, above).

`tests/contract/test_m2b_acceptance.py` runs the whole flow over real `/mcp` with Keycloak
tokens on a throwaway model (`retail_m2b_<hex>`, hard-deleted, never sealed): the default
agent tokens and bob are refused by the `lakeloom:review` scope check (nothing is read or
applied); an agent token that requested the realm's optional `lakeloom:review` scope passes
that gate and is refused by the review service for approve, reject and request_changes
(`forbidden` "agent principals cannot review proposals (FR-REV-01)", audited with the
proposal id); carol is refused by OM's permission; alice's approval applies every change set
with `applicationResult.appliedBy == "alice"`, the new terms are `updatedBy` alice and the
realization tag on `orders` has `appliedBy` alice; reject,
request changes and the agent's re-draft, the stale-base conflict and rebase, and a re-scan
after apply are covered, as are the audit rows (`principal_type` human for every decision).
Live findings of that run:

- **xsd:date is not an OWL 2 datatype.** OM rc1 drafts a data property with range
  `xsd:date` but refuses it at apply (400 "Data property ranges require supported OWL 2
  datatypes"). The native range of an attribute is now its xsd datatype when it is in the
  OWL 2 datatype map and `rdfs:Literal` otherwise (`operations.OWL2_XSD_DATATYPES`); the
  proposal keeps the precise datatype. Fixture `orders.order_date` (Spark `date`) hit it.
- **The mapper proposes no realizations for new concepts.** Realizations need a concept
  OM has, so the first scan of an empty model proposes concepts only; after apply, a
  re-scan proposes the column realizations of the applied concepts (dedupe rule
  `R-DEDUPE`) and nothing else, and once those are applied a further scan has nothing new.
- **Scope refusals carry no proposal id in the audit.** `guarded` refuses a missing
  `lakeloom:review` before the call body runs, so those rows have the principal and the
  tool but no model or proposal. The service's agent refusal carries the proposal id but no
  model (it comes before the proposal is read); carol's refusal carries both.
- **Caches outlive an OM-side delete.** Lakeloom caches OM tables and models for
  `model_cache_ttl_seconds` (60 in dev); after a model's glossary is hard-deleted in OM,
  the asset context of a table that realized it is `not_found` until the TTL expires. The
  acceptance teardown waits for it (backlog).
- Two open `update_concept` of one term are impossible through the tools: the second is
  dropped as "already proposed in …" by dedupe, and one proposal changes an existing
  concept once. The stale case is therefore a synonym (first proposal) and an
  `update_concept` with the agent's base version (second proposal) on the same term.

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
- FR-REV-01 and FR-REV-02 hold live: only a human holding `lakeloom:review` and OM's
  reviewer permission can approve or reject, and OM's own audit (`appliedBy`, `updatedBy`,
  the table tag's `appliedBy`) names the reviewer. The bot is never used to approve
  (NFR-SEC-10); `review_identity` `unavailable` fails closed.
- Deployments must configure token exchange (a confidential client allowed to exchange,
  and a client scope that carries the OM audience) or run OM so that it accepts Lakeloom's
  own tokens (forward mode). Neither is optional for review; there is no bot fallback.
- An OM user with the reviewer role can apply change sets directly in OM, outside
  Lakeloom; and per the `DataConsumer` finding above any OM user can edit terms directly.
  Lakeloom governs the path through Lakeloom, not OM itself.
- Deferred items of M2b are listed in `docs/backlog/m2b-deferred.md`.
