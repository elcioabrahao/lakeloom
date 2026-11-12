# M2a deferred follow-ups

Findings from the M2a task and whole-branch reviews, deliberately deferred. Triage when planning
M2b/M2c.

## Carried into M2b (needed before apply)
- Done (M2b): cross-part draft ids. Applied `CREATE_TERM`s keep their draft ids, approve
  applies glossary before native and part k before part k+1, and later native parts on an
  old base revision are re-drafted during approve (ADR 0012 § Apply research, § Review).
- Done (M2b): native `OBJECT` properties (relations) apply live (ADR 0012 § Apply research;
  `test_m2b_acceptance` applies the fixture's `hasCustomer` / `hasOrder`).
- Done (M2b): human-only apply. `review_proposal` refuses agents and callers without
  `lakeloom:review` or OM's reviewer permission, and applies with the reviewer's exchanged
  token, never the bot (FR-REV-01/02, NFR-SEC-10; `test_m2b_acceptance`).
- Still open: the per-model proposal lock is in-process: with several replicas two scans of
  one model at the same instant can propose the same operation (out of scope for M2b).
- Still open: dev OM holds the soft-deleted, name-reserving `llprobe5b` ontology from the
  Task 5b probe (dev only; remove when OM allows it).

## Proposals and summaries
- Compact summaries for very large scans: cap the operation details listed in the Markdown
  summary (FR-AGT-02 token budgets).
- `proposals.py` is large; split it into modules (summary/provenance rendering, OM writes,
  service).
- Pending lookup (`pending_op_keys`) loads every open proposal of the model per call; index it.
- `UpdateConcept` that changes nothing is not detected; it is not mirrored on the native class.
- `DELETE_RELATIONSHIP` `targetId` semantics are unverified live.
- A second `relatedTo` fallback link for the same term pair added by a later `propose_change`
  call is not merged with the first (one link per pair is guaranteed within one batch only).

## Dedupe (FR-PRO-02)
- "Equivalent IRIs" are matched through names and local names only (`AddConcept` carries no
  IRI).
- `op_key` uses bare names, so keys are not distinct across models.
- `score_candidates` duplicates the scoring loop of `find_concepts`.
- Native-only classes (no glossary term) are not indexed as concepts.

## Mapper (FR-PRO-01)
- `STRING COLLATE ...` column types are not recognised as strings.
- Enum child concepts carry the raw value in their description.
- Enum values longer than 256 characters are dropped without saying which.
- The xsd datatype map has no allowlist beyond the shared pattern. (M2b: the native range
  falls back to `rdfs:Literal` outside the OWL 2 datatype map; see m2b-deferred.)

## Operations and ids
- uuid5 ids of draft terms and native members are not model-scoped.

## Confirm tokens and audit
- No clock-skew leeway on token expiry across replicas.
- The audit cursor orders by id; records within the same millisecond have no stable order.
