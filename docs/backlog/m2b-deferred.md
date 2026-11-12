# M2b deferred follow-ups

Findings from the M2b task and whole-branch reviews, deliberately deferred. Triage when planning
M2c/M3. Details and rulings: [ADR 0012](../decisions/0012-review-and-human-identity.md).

## Out of scope for M2b
- Auto-approve policies (FR-REV-04) and `min_approvals` (Q6).
- CLI review: needs a human OIDC login flow in the CLI (the operator holds bot credentials).
- Git commits (and PRs, with trailers) on apply: M2c.
- Multi-replica proposal lock (carried from M2a; see m2a-deferred).

## OpenMetadata configuration (documented in ADR 0012)
- OM skips `iss` (and `azp`) checks in public-client mode: any realm client that can mint a
  token with `aud: openmetadata` can call OM as that user. Run OM as a confidential client in
  production.
- OM's default `DataConsumer` role lets every OM user edit glossary terms, descriptions and
  tags directly, outside change sets.

## Tag writes (realizations)
- Read-then-write race: a JSON-Patch `test` op would guard the column index, but rc1 then
  demands `EditAll` on the table.
- Only top-level columns can be tagged (no nested struct fields).

## Permissions
- The pre-check covers `EditAll` on the glossary and the native ontology only; missing
  lease or native-member permissions surface at apply as `forbidden`.
- `request_changes` does not require OM permission (ruling: it touches nothing in OM).

## Review
- `DISCARDED` / missing sides are rebuilt silently during approve (no warning).
- A `stale_base` on a `DELETE_RELATIONSHIP` (its target is the relationship id, not a term)
  still shows OM's operation id.
- The description read-back after an applied `UpdateConcept` builds the term FQN differently
  from the realization term-FQN construction.

## Rebuild and rebase
- The change-set name-length check is not re-run after discards.
- `_concept_iri` fallback on models paired after the fact.
- An interrupted resume rebuilds both sides.
- Rebase reads the terms twice.
- A vanished term referenced only by non-base operations gives `validation_error` (not
  `conflict`).

## On-behalf-of tokens
- `client_secret_basic` without the RFC 6749 §2.3.1 form-encoding of id and secret.
- Exchange mode without issuer or token endpoint is `upstream_unavailable` instead of a
  configuration error at startup.
- A non-finite `expires_in` is not rejected.
- `AppDeps` auto-creates `idp_http` in tests.

## Pending reviews
- Data product = lake schema (no OM `dataProducts` in the stored provenance).
- With several schemas only the first is shown in the text.
- `list_pending` may scan the table; there is no repository-level `list_by_status` test.

## Found by the acceptance test (Task 7)
- Datatypes outside the OWL 2 datatype map (`xsd:date`, `xsd:time`, ...) become
  `rdfs:Literal` on the native side (fixed minimally); decide whether to validate agent
  datatypes against the OWL 2 map at proposal time instead (today an agent `xsd:gYear` is
  accepted and applied as `rdfs:Literal`).
- Lakeloom's TTL caches (OM tables, models; 60 s in dev) outlive a glossary hard-deleted in
  OM: the asset context of a table that realized the deleted model is `not_found` until the
  TTL expires (an unknown model should be skipped, not fail the whole context).
- `guarded` refuses a missing scope before the call body, so those audit rows carry no
  model or proposal id.

## Bootstrap and dev stack
- The bootstrap passes credentials as `curl` arguments (visible in the process list; dev only).
