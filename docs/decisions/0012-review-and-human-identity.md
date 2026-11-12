# 0012. Review and human identity in OpenMetadata

- Status: accepted (§ SSO; later sections follow in M2b)
- Date: 2026-11-12
- Requirements: FR-OM-05, NFR-SEC-08, NFR-SEC-09

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
`LakeloomOntologyReviewerPolicy` with one allow rule: operations `ViewAll`, `Create`,
`EditAll` on resources `ontologyChangeSet`, `glossary`, `glossaryTerm`, `ontology`,
`ontologyConcept`, `ontologyProperty`, `ontologyAxiom`. Role `LakeloomOntologyReviewer`
holds that policy and is granted to **alice only**. carol (same Keycloak group) and bob do
not get it. The applied term showed `updatedBy: alice`, so the reviewer's identity reaches
OM's history. The probe also saw the applied `UPDATE_TERM` change its description, unlike
the M2a observation in ADR 0011. Task 2 re-checks this.

The rule set is a superset of what the probe needed. Properties, axioms, `DELETE_TERM` and
relationship operations were not probed one by one, and `Delete` is not granted. Task 2
re-verifies the exact operation names.

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
