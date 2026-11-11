# M2a — Proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents turn a scan into a governed ontology proposal — an OpenMetadata change set in Draft with full provenance and no duplicate concepts — enrich it with typed operations and realizations, inspect it in plain language, and submit it for review; every write is audited.

**Architecture:** A deterministic mapper (`core/mapper.py`) turns a scan record into typed proposal operations (`core/operations.py`) carrying rule IDs and evidence; a dedupe pass (`core/dedupe.py`) rewrites operations that match existing concepts into realizations/synonyms. The proposal service (`core/proposals.py`) persists proposals in the state store and mirrors them into OM change sets through new OM client write methods; realizations (asset tagging) are Lakeloom-side operations applied at approval time (M2b). Stateless HMAC confirm tokens guard destructive/catalog-changing tools; an audit log records every write.

**Tech Stack:** existing stack (FastAPI, MCP SDK 2.x, SQLAlchemy/Alembic, rdflib); no new runtime dependencies.

**Spec:** `REQUIREMENTS.md` — FR-PRO-01/02/03/05/06/08, FR-OM-01/02, FR-CTX-05, FR-AGT-01/02/03/04/05/06/07, NFR-SEC-11/12/14. Part 1 of 3 of M2 (M2a proposals; M2b review with human identity — OM SSO via Keycloak, token exchange, review_proposal, apply, edit leases, rebase, list_pending_reviews, review_with_specialist prompt; M2c Git publishing). M2 acceptance owned here: "re-running the scan proposes no duplicates", and the proposal half of "`bootstrap_data_product` flow creates a model from the fixture lake" (the apply half is M2b).

## Decisions already taken (owner, 2026-11-10)

- M2 is split into M2a/M2b/M2c (this is M2a).
- OM in the dev stack moves to Keycloak SSO in M2b (not M2a). M2a writes use the OM bot JWT (FR-OM-05a) — drafts and submissions only; nothing is applied in M2a.
- `create_model` creates the OM glossary directly through OM's glossary API (admin scope, confirm token): FR-OM-02 lists concepts, relations, attributes, axioms and mappings as change-set-only — a model (glossary) is not in that list. ADR 0011.
- Realizations (FR-CTX-05) are stored as Lakeloom-side operations inside the proposal (OM change sets have no asset-tagging operation) and applied to OM assets when the proposal is approved (M2b). ADR 0011.
- Confirm tokens are stateless HMAC-SHA256 over `{tool, canonical args, state digest, exp}` with a server secret `LAKELOOM_CONFIRM_SECRET` / `_FILE` (multi-replica safe, no table). Default TTL 600 s.
- The audit log lands in M2a (writes start here): JSON lines to stdout (`lakeloom.audit` logger) + `audit_log` table.

## Global Constraints

- All earlier Global Constraints apply (Python ≥ 3.12; uv/ruff/pyright strict on `src/`; Conventional Commits + `git commit -s` + requirement IDs; identity `Elcio Abrahao <elcio@tuta.com>`; typed errors with hints; untrusted text quoted; every OM endpoint in `OMClient.PATHS` + spec-path test; no tool takes URLs; tools via `guarded()`; scopes `lakeloom:read|propose|review|publish|admin`).
- FR-OM-02: "All ontology writes go through change sets. Lakeloom MUST NOT bypass change sets for concepts, relations, attributes, axioms or mappings."
- FR-PRO-01 default rules verbatim: Table/data-product entity → Concept (`owl:Class`); Column → Attribute (`owl:DatatypeProperty`) with data type mapped from the column type; Declared foreign key → Relationship (`owl:ObjectProperty`) with domain and range; Declared primary key → Attribute flagged `isIdentifier` (and `owl:hasKey` axiom if supported by OM); Low-cardinality string column (threshold configurable) → `ENUM` attribute, or child concepts if configured; Table/column comment → Concept/attribute description; Tags → "Kept as hints in provenance; not converted to concepts automatically".
- FR-PRO-02: "search existing concepts (name, synonyms, normalised local name, IRI) across the target model and its imports; when a likely match exists, propose a link/realization or synonym instead of a new concept, and report the match with a confidence score." Threshold: confidence ≥ 0.9 → replace; 0.6–0.9 → keep the new concept and report the candidate as a warning.
- FR-PRO-03 provenance fields: "scan ID, source table FQN, Delta/table version, rule IDs that produced each operation, calling principal, client name, timestamp" — stored in (a) the state store, (b) the OM change set description as a machine-readable block, (c) Git trailers (M2c).
- FR-PRO-08: "Proposals are size-limited (configurable, default 50 operations)" (`LAKELOOM_MAX_PROPOSAL_OPS`, already in Settings); "larger scans are split by table or data product".
- FR-AGT-03: "Destructive or catalog-changing tools require an explicit confirm token returned by a prior dry-run call ... Tokens bind to the previewed state and expire (default 10 minutes)."
- NFR-SEC-12: "Audit log for every write and every review decision: principal, principal type, client, tool/endpoint, model, proposal/change set ID, result. JSON lines to stdout and to the state store."
- Lake comments and OM text are untrusted (NFR-SEC-14): they become descriptions verbatim as data, rendered quoted in Markdown; never interpreted.

## Review Focus

1. **Re-running a scan**, and scanning tables whose concepts already exist or are already pending in another open proposal for the same model: no new concept is proposed twice; matches become realizations, with the match reported. → Task 5 and Task 8 tests.
2. **Stale bases / concurrent edits**: OM rejects an operation whose `baseVersion` is stale → typed `conflict` with the affected entity and hint "call rebase_proposal (M2b)"; the Lakeloom proposal stays consistent with OM (no half-recorded operations). → Task 6 tests.
3. **Confirm-token misuse**: token from another tool, other arguments, after the previewed state changed, expired, tampered, or replayed after use → `validation_error`/`conflict` with a clear hint; never executes. → Task 3 tests.
4. **Hostile lake text** (comments with Markdown, prompt-injection, enormous comments) flows into OM descriptions only as data: length-capped (4,000 chars, recorded as truncated in provenance), quoted in summaries, never alters operation structure. → Tasks 4 and 6 tests.
5. **Large scans**: 120 tables → proposals split per table at ≤ 50 operations each, deterministic order and naming, one summary listing all parts. → Task 6 tests.

## File structure

```
src/lakeloom/om/client.py, om/models.py       # + change-set, glossary-create, axiom-validate (modify)
src/lakeloom/core/operations.py               # typed proposal operations + to_om()
src/lakeloom/core/confirm.py                  # HMAC confirm tokens
src/lakeloom/core/audit.py                    # audit records (log + store)
src/lakeloom/core/mapper.py                   # deterministic mapper (rules R-*)
src/lakeloom/core/dedupe.py                   # duplicate avoidance
src/lakeloom/core/proposals.py                # ProposalService
src/lakeloom/state/proposals.py               # ProposalRepository; state/audit.py AuditRepository
src/lakeloom/state/migrations/versions/0002_proposals_audit.py
src/lakeloom/server/tools_proposals.py        # MCP tools; prompts in server/prompts.py
docs/decisions/0011-proposals.md
```

---

### Task 1: OM client — change sets, glossary creation, axiom validation

**Files:** Modify `src/lakeloom/om/client.py`, `src/lakeloom/om/models.py`; Create `docs/decisions/0011-proposals.md` (initial: payload shapes); Test `tests/unit/om/test_client_changesets.py`, `tests/contract/test_om_changesets.py`

**Interfaces:**
- Produces (paths/schemas from the committed spec; all in `PATHS`):
  - `create_change_set(*, name: str, glossary_id: str | None, description: str, operations: Sequence[Mapping[str, Any]]) -> OMChangeSet` (`POST /v1/ontologyChangeSets`, body `CreateOntologyChangeSet`; state Draft).
  - `add_operations(change_set_id: str, operations: Sequence[Mapping[str, Any]]) -> OMChangeSet` (`/v1/ontologyChangeSets/{id}/operations`).
  - `get_change_set(id: str) -> OMChangeSet | None`; `submit_change_set(id) -> OMChangeSet`; `discard_change_set(id) -> OMChangeSet`.
  - `validate_axiom(axiom: Mapping[str, Any]) -> OMAxiomValidation` (`POST /v1/ontologyAxioms/validate`).
  - `create_glossary(*, name: str, display_name: str | None, description: str) -> OMGlossary` (glossary create endpoint per spec).
  - Models: `OMChangeSet(id, name, state: Literal["Draft","Submitted","Applied","Apply Failed","Discarded"] | str, operations: list[OMChangeOperation], description, base_working_revision, application_result)`; `OMChangeOperation(id, operation_type, state, base_version, target_id, term, concept, property, relationship, mapping, axiom, glossary, rationale, confidence)`; `OMAxiomValidation(valid: bool, errors: list[str])` (field names from the spec; `extra="allow"`).
  - Error mapping: HTTP 409 / OM "stale base version" responses → `OMConflict(OMError)` carrying the entity id(s) from the body when present.
- [ ] **Step 1: Live research (record in ADR 0011):** with the bot token against the dev OM, create a throwaway glossary and a change set with one op each of `CREATE_TERM`, `UPDATE_TERM`, `UPSERT_PROPERTY` (datatype attribute), `ADD_RELATIONSHIP`, `UPSERT_MAPPING`, `UPSERT_AXIOM` (a hasKey/cardinality example if supported), then `submit` and `discard`; capture the minimal accepted payload for each operation type, what `baseVersion` must contain for UPDATE ops, what a stale base returns, and which op represents an "attribute" in rc1 (UPSERT_PROPERTY vs term fields). Hard-delete the throwaway glossary after.
- [ ] **Step 2: Tests (respx + recorded payloads):** one per method; `test_conflict_maps_to_om_conflict`; spec-path test covers new PATHS. Contract: create → add op → get → submit → discard round-trip on a throwaway glossary (cleaned up).
- [ ] **Step 3:** FAIL → implement → pass; pyright strict.
- [ ] **Step 4: Commit** `feat(om): change sets, glossary creation and axiom validation (FR-OM-01, FR-OM-02)`.

---

### Task 2: Typed proposal operations

**Files:** Create `src/lakeloom/core/operations.py`; Test `tests/unit/core/test_operations.py`

**Interfaces:**
- Consumes: Task 1 payload shapes (ADR 0011).
- Produces (pydantic, discriminated by `op`, frozen; every op has `rule_ids: list[str]`, `evidence: list[Evidence(kind: Literal["column","table","fk","pk","profile","tag","comment","match","agent"], ref: str, detail: str|None)]`, `rationale: UntrustedText|None`):
  - `AddConcept(name, display_name, description: UntrustedText|None, parent: str|None (term FQN), synonyms: list[str])`; `UpdateConcept(fqn, base_version: str, description|display_name|synonyms|parent changes)`; `RemoveConcept(fqn, base_version)`.
  - `AddAttribute(concept: str (name or FQN), name, datatype: str (xsd CURIE), is_identifier: bool, allowed_values: list[str], unit: str|None, description)`; `RemoveAttribute(concept, name, base_version)`.
  - `AddRelation(source: str, relation_type: str, target: str, description)`; `RemoveRelation(...)`.
  - `AddMapping(concept, kind: Literal["exactMatch","closeMatch","broadMatch","narrowMatch","relatedMatch"], target_iri: AnyUrl)`; `RemoveMapping`.
  - `AddAxiom(concept, axiom: dict[str, Any])` (validated through OM in Task 6); `RemoveAxiom`.
  - `AddSynonym(concept_fqn, synonym)`.
  - `AddRealization(concept: str, asset_fqn: str, column: str|None)`; `RemoveRealization(...)` — Lakeloom-side (no OM op).
  - `ProposalOperation = Annotated[Union[...], Field(discriminator="op")]`.
  - `def to_om(op: ProposalOperation, *, glossary_fqn: str, glossary_id: str) -> dict[str, Any] | None` (None for realization ops); `def op_key(op) -> str` (stable identity for dedupe/idempotence, e.g. `concept:retail.Order`, `attr:retail.Order.order_id`).
  - Text limits: descriptions capped at 4,000 chars (`truncated` flag in evidence); names validated (`^[A-Za-z][A-Za-z0-9_ ]{0,127}$` for concept/attribute names; relation types `^[a-zA-Z][a-zA-Z0-9]*$`).
- [ ] **Step 1: Tests:** JSON round-trip of each op type; `to_om` output for each OM-backed op matches the ADR 0011 payloads exactly; realization → None; invalid names/relation types → ValidationError; `test_description_capped_and_flagged`; `test_op_key_stable`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): typed proposal operations mapped to OM change-set operations (FR-PRO-05)`.

---

### Task 3: Confirm tokens and audit log (+ migration 0002)

**Files:** Create `src/lakeloom/core/confirm.py`, `src/lakeloom/core/audit.py`, `src/lakeloom/state/audit.py`, `src/lakeloom/state/proposals.py` (table only; repository methods in Task 6), `src/lakeloom/state/migrations/versions/0002_proposals_audit.py`; Modify `src/lakeloom/config.py` (`confirm_secret: SecretStr|None` with `_FILE`; `confirm_ttl_seconds: int = Field(600, ge=30, le=3600)`), `src/lakeloom/server/tools_common.py` (`guarded()` emits an audit record for tools flagged `audited=True`); Test `tests/unit/core/test_confirm.py`, `tests/unit/core/test_audit.py`, `tests/unit/state/test_migrations.py`

**Interfaces:**
- Produces:
  - `ConfirmTokens(secret: bytes, ttl: int, clock)`: `issue(tool: str, args: Mapping[str, Any], state_digest: str) -> str` (base64url of `{"t","a","s","e","n"}` + HMAC; `a` = sha256 of canonical JSON of args excluding `confirm_token`; `n` = random nonce); `verify(token: str, tool: str, args: Mapping, state_digest: str) -> None` raising `LakeloomError("validation_error", "confirm token invalid", hint=…)` for tamper/mismatch/expiry (distinct messages: "expired", "does not match this call", "previewed state changed" — the last as `conflict`). Replay protection: the state digest changes after execution (each guarded tool includes the target's current version/state in the digest), so a used token fails as "previewed state changed". Missing `LAKELOOM_CONFIRM_SECRET` in production → ConfigError at startup; in development a random per-process secret is generated with a WARNING log.
  - Audit: `AuditRecord(at, principal_subject, principal_type, client, tool, model: str|None, proposal_id: str|None, change_set_id: str|None, result: Literal["ok","error","forbidden"], error_code: str|None)`; `async record(audit_repo, record)` → JSON line on logger `lakeloom.audit` + row in `audit_log`; never includes arguments or text.
  - Migration 0002: tables `proposals` (id `prop_<ulid>`, model, status, change_set_ids JSON, scan_id, principal_subject, principal_type, client, created_at, updated_at, payload JSON) indexed (model, status) and `audit_log` (id, at, …fields above) indexed (at).
- [ ] **Step 1: Tests:** token round-trip ok; tampered byte → invalid; other tool/args → "does not match"; expired (fake clock) → expired; state digest changed → conflict; production without secret → ConfigError; `_FILE` secret; audit JSON line has exactly the NFR-SEC-12 fields and no args; migration 0002 upgrade on SQLite + Postgres contract.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): HMAC confirm tokens, audit log and proposal tables (FR-AGT-03, NFR-SEC-12)`.

---

### Task 4: Deterministic mapper

**Files:** Create `src/lakeloom/core/mapper.py`; Modify `src/lakeloom/sources/config.py` or new `src/lakeloom/core/model_policy.py` (per-model mapping policy from `LAKELOOM_CONFIG_FILE` `models:` section); Test `tests/unit/core/test_mapper.py`

**Interfaces:**
- Consumes: `ScanRecord` (M1a), operations (Task 2).
- Produces:
  - `MappingPolicy(model: str, low_cardinality: int = 20, enum_as_children: bool = False, concept_naming: Literal["singular_pascal","pascal","as_is"] = "singular_pascal", attribute_naming: Literal["as_is","camel"] = "as_is", datatype_map: dict[str,str] = DEFAULT_DATATYPES)`; loaded from YAML `models: [{name, mapping: {...}}]`; defaults when absent.
  - `DEFAULT_DATATYPES`: Spark-style types → xsd: `string`→`xsd:string`, `bigint`→`xsd:long`, `int`→`xsd:int`, `smallint`→`xsd:short`, `tinyint`→`xsd:byte`, `decimal(p,s)`→`xsd:decimal`, `double`→`xsd:double`, `float`→`xsd:float`, `boolean`→`xsd:boolean`, `date`→`xsd:date`, `timestamp`/`timestamp_ntz`→`xsd:dateTime`, `binary`→`xsd:base64Binary`, other → `xsd:string` + warning.
  - `def map_scan(record: ScanRecord, policy: MappingPolicy) -> MappingResult(operations: list[ProposalOperation], warnings: list[str])` — rules with IDs: `R-TABLE` (table → AddConcept, name per naming policy, description from table comment `R-COMMENT`), `R-COLUMN` (column → AddAttribute with datatype `R-TYPE`, description from column comment), `R-PK` (PK columns → `is_identifier=True`; plus an `owl:hasKey` AddAxiom if ADR 0011 says OM supports it), `R-FK` (FK → AddRelation `references<Target>` → relation type `has<TargetConcept>` camelCase, domain = this concept, range = target concept), `R-ENUM` (string column with profile `distinct_count ≤ low_cardinality` and top-k values available → `allowed_values`; if samples withheld → no enum + warning "enum skipped: sample values withheld"; `enum_as_children` → AddConcept children with parent instead), `R-TAGS` (tags → evidence only). Operation order: per table sorted by table name; within a table: concept, attributes (column order), relations, axioms. Naming: `singular_pascal` = split on `_`/space, singularise the last word by rules (`ies`→`y`, `sses`→`ss`, trailing `s` (not `ss`) → drop), PascalCase join; collisions within one scan → suffix `2`, `3` with warning.
- [ ] **Step 1: Tests (fixture lake scan record built in tests):** `test_fr_pro_01_orders_maps_to_concept_attributes_relation` (Order concept; 5 attributes with xsd types; order_id is_identifier; relation `hasCustomer` Order→Customer from FK; descriptions from comments; rule IDs on each op); `test_enum_from_profile_when_samples_on` / `test_enum_skipped_when_samples_withheld`; `test_enum_as_children_policy`; `test_tags_only_in_evidence`; `test_singularisation_cases` (orders→Order, categories→Category, addresses→Address, status→Status, glasses→Glass); `test_deterministic_output` (shuffled tables → identical ops); `test_unknown_type_warns`; `test_hostile_comment_is_data_only` (comment with Markdown/prompt text appears verbatim in description, capped, no structural effect).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): deterministic mapper from scans to proposal operations (FR-PRO-01)`.

---

### Task 5: Duplicate avoidance

**Files:** Create `src/lakeloom/core/dedupe.py`; Test `tests/unit/core/test_dedupe.py`

**Interfaces:**
- Consumes: concepts scoring (`core/concepts.py` name/synonym/local-name/IRI scorer from M1b — expose a public `score_candidates(models, query, model=…)` if not already public), `ModelService`, open proposals (Task 6 repository — pass a callable `pending_concept_keys(model) -> set[str]` to keep this module independent).
- Produces: `async def dedupe(ops: list[ProposalOperation], *, model: str, models: ModelService, om: OMClient, pending: Callable[[str], Awaitable[set[str]]], asset_by_table: Mapping[str, tuple[str, str|None]]) -> DedupeResult(operations: list[ProposalOperation], matches: list[Match(op_key, candidate_fqn, confidence, matched_on)], warnings: list[str])`. Rules: AddConcept whose name matches an existing concept (in the model or its imports — imports from the model graph's `owl:imports`) with confidence ≥ 0.9 → replaced by `AddRealization(candidate, asset_fqn of the table)` (+ `AddSynonym` when matched on a different label than the proposed name, e.g. proposed "Client" vs existing "Customer" via synonym → no synonym needed; proposed "Purchase"→ matched synonym → nothing); its AddAttribute ops re-target the existing concept and are dropped when an attribute with the same name already exists (from graph `owl:DatatypeProperty` with `rdfs:domain`); 0.6–0.9 → keep + warning with the candidate; already pending in an open proposal (`op_key` in pending) → dropped with warning "already proposed in <proposal id>". Relations/mappings/axioms deduped by `op_key` against existing graph triples and pending keys. Columns that map onto an existing concept's attribute → `AddRealization(concept, table, column)` when the column isn't already realized (from OM table tags).
- [ ] **Step 1: Tests (fake model graph with Customer/Order/OrderStatus etc. and fake pending):** `test_fr_pro_02_existing_concept_becomes_realization` (scan of `orders` into model `retail` → no AddConcept Order; AddRealization(retail.Order, orders); match reported with confidence 1.0); `test_synonym_match` (table `clients` → matches Customer via synonym "Client" → realization, match on synonym 0.95); `test_mid_confidence_kept_with_warning`; `test_pending_proposal_suppresses_duplicate`; `test_existing_attribute_dropped`; `test_already_realized_column_dropped`; `test_rescan_proposes_nothing_new` (ops already applied → empty result except no-op warnings).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): duplicate avoidance against models, imports and open proposals (FR-PRO-02)`.

---

### Task 6: Proposal service

**Files:** Create `src/lakeloom/core/proposals.py`; Modify `src/lakeloom/state/proposals.py` (ProposalRepository); Test `tests/unit/core/test_proposals.py`, `tests/contract/test_proposals_live.py`

**Interfaces:**
- Consumes: Tasks 1–5, `ScanRepository`, `ModelService` (invalidate on submit is not needed — nothing applied), audit.
- Produces:
  - `Proposal(id, model, status: Literal["draft","submitted","discarded","applied","apply_failed","rejected","changes_requested"], parts: list[ProposalPart(change_set_id: str|None, table: str|None, operations: list[ProposalOperation], om_state: str)], scan_id: str|None, provenance: Provenance, matches, warnings, created_at, updated_at)`; `Provenance(scan_id, tables: list[{fqn, version}], rule_ids: list[str], principal_subject, principal_type, client, at, ai_generated: bool = False)`.
  - `ProposalService(om, models, scans, repo, audit, settings)`:
    - `async propose_from_scan(scan_id: str, model: str, *, principal, client, tables: list[str]|None = None) -> Proposal` — map → dedupe → split into parts of ≤ `max_proposal_ops` by table (a table larger than the limit is split into consecutive parts with a warning) → for each part create an OM change set (Draft) named `lakeloom-<proposal id>-<n>` with description = Markdown summary of that part + fenced block ```` ```lakeloom-provenance ```` JSON (provenance + op keys) → persist. Realization-only parts create no change set. Empty result → `validation_error` "nothing to propose (all matched existing concepts)" with the matches in the hint/structured content.
    - `async propose_change(proposal_id, operations: list[ProposalOperation], *, principal, client) -> Proposal` — only in `draft`; validate; axioms through `om.validate_axiom` (invalid → validation_error with OM's messages, quoted); dedupe against the proposal itself; append to the last part (new part when full) via `add_operations`.
    - `async propose_realization(proposal_id | None, model, concept, asset, column|None, ...) -> Proposal` — creates or extends a draft with AddRealization (asset resolved via the M1b asset resolver; concept via explain resolver).
    - `async get_proposal(id) -> Proposal` (refreshes OM states); `summary_markdown(p) -> str` (FR-PRO-06: what is added/changed/removed per kind, why (rule IDs + evidence), warnings, matches; untrusted text quoted); `diff(p) -> ProposalDiff` (structured per-entity before/after where known).
    - `async submit(id, ...) -> Proposal` — draft → OM submit for every part → status `submitted`.
    - `async discard(id, ..., confirm_token: str|None) -> Proposal | ConfirmPreview` — destructive: without token returns a preview (`ConfirmPreview(summary, confirm_token, expires_at)`); with a valid token discards all OM parts → `discarded`.
    - Concurrency: OM `OMConflict` on add/submit → `LakeloomError("conflict", …, hint="the base version changed; call rebase_proposal (available in M2b)")`; repository updates happen only after OM succeeded; partial multi-part failures leave already-created parts recorded with their OM state (no orphan change sets unrecorded).
    - Every call writes an audit record.
- [ ] **Step 1: Tests (fake OM recording calls + SQLite repo):** `test_fr_pro_03_provenance_in_store_and_description` (block parses as JSON with scan id, table versions, rule ids, principal, client, timestamp); `test_fr_pro_08_split_by_table` (120 tables → parts ≤ 50 ops, deterministic names); `test_fr_pro_06_summary_lists_adds_and_reasons_and_quotes_comments`; `test_propose_change_validates_axiom_via_om`; `test_propose_change_only_in_draft`; `test_submit_moves_all_parts`; `test_discard_requires_confirm_token` (preview → token → discard; replay → conflict); `test_conflict_maps_and_store_consistent`; `test_realization_only_proposal_has_no_change_set`; `test_audit_written_for_each_write`. Contract (live OM, model created in-test via `create_glossary`, cleaned up): propose_from_scan on fixture_delta into a fresh model → Draft change sets in OM with provenance block; submit → Submitted; re-run propose_from_scan → only pending-duplicate warnings (no new ops).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): proposal service with OM change sets, provenance and size limits (FR-PRO-03, FR-PRO-05, FR-PRO-06, FR-PRO-08)`.

---

### Task 7: MCP tools, prompts and CLI

**Files:** Create `src/lakeloom/server/tools_proposals.py`, `src/lakeloom/server/prompts.py`; Modify `src/lakeloom/server/mcp_app.py`, `src/lakeloom/server/http_app.py` (AppDeps gains `proposals: ProposalService`, `confirm: ConfirmTokens`), `src/lakeloom/cli.py` (`lakeloom propose SCAN_ID --model M`, `lakeloom proposal ID`, `lakeloom submit ID`), `README.md`, `CHANGELOG.md`; Test `tests/unit/server/test_tools_proposals.py`, `tests/unit/server/test_prompts.py`

**Interfaces:**
- Tools (descriptions for LLMs incl. side effects; annotations: read-only tools readOnly/idempotent; writers readOnly=False; destructive=True for discard; idempotent=False for creators):
  - `create_model(name, display_name=None, description=None, confirm_token=None)` — scope `lakeloom:admin`; preview → token → creates OM glossary; base IRI per M1b rule; audited.
  - `propose_from_scan(scan_id, model, tables=None)` — `lakeloom:propose`; returns summary + structured Proposal + matches.
  - `propose_change(proposal_id, operations: list[ProposalOperation])` — `lakeloom:propose`; the JSON schema exposes the discriminated union.
  - `propose_realization(concept, asset, column=None, proposal_id=None, model=None)` — `lakeloom:propose`.
  - `get_proposal(proposal_id)`, `diff_proposal(proposal_id)` — `lakeloom:read`.
  - `submit_proposal(proposal_id)` — `lakeloom:propose`.
  - `discard_proposal(proposal_id, confirm_token=None)` — `lakeloom:propose`; destructive.
  - Prompts (FR-AGT-04): `bootstrap_data_product(source, model, tables?)` — step-by-step instructions: list/describe tables → scan → `create_model` if missing → `propose_from_scan` → review the summary with the user → enrich with `propose_change` (definitions, synonyms, mappings) → `submit_proposal`; `explain_table(asset)` — `get_asset_context` + `explain_concept` for each realized concept, in plain language. (`review_with_specialist` M2b, `drift_check` M3.)
- [ ] **Step 1: Tests:** annotations/scopes for all tools; `test_fr_agt_03_create_model_preview_then_confirm`; `test_discard_preview_then_confirm`; `test_propose_change_schema_exposes_union`; `test_prompts_listed_and_render_arguments` (prompts/list, prompts/get); `test_writes_are_audited`; CLI propose/proposal/submit print summaries. Contract (`tests/contract/test_m2a_acceptance.py`, agent token with propose (+ an admin-scoped client for create_model — add `lakeloom:admin` to the dev realm for a new client `lakeloom-admin-agent` / secret `dev-admin-secret`, or map admin via group for a test user)): **bootstrap flow** — scan fixture_delta → create_model `retail_auto` (preview + confirm) → propose_from_scan → proposal in OM as Draft with provenance → submit → Submitted; **re-run scan + propose_from_scan → no duplicate concept ops** (all reported as already proposed/matched); cleanup: discard (preview+confirm) and delete the test glossary in teardown.
- [ ] **Step 2:** FAIL → implement → pass (rebuild/recreate lakeloom for contract).
- [ ] **Step 3:** README "Proposals" section (flow, confirm tokens, provenance block, audit), CHANGELOG.
- [ ] **Step 4: Commit** `feat(mcp): proposal tools, prompts and CLI (FR-AGT-01, FR-AGT-03, FR-AGT-04, FR-CTX-05)`.

---

## Out of scope for M2a

Approve/reject/request changes, apply, human identity and token exchange, OM SSO, edit leases, rebase_proposal, list_pending_reviews, auto-approve policies, `review_with_specialist` (M2b); Git commits/PRs (M2c); import/drift/releases (M3).

---

## Amendment A (owner decision 2026-11-11, after Task 1 research — ADR 0011)

OM 2.1.0-rc1 accepts attributes (`UPSERT_PROPERTY`, kind DATA) and axioms (`UPSERT_AXIOM`) only in a **native ontology** change set (`ontology` + `baseWorkingRevision`), and a property's domain must be a class declared in that ontology (`UPSERT_CONCEPT`). A change set cannot mix glossary and native operations. **Decision: hybrid model.** Each Lakeloom model = the OM glossary (concepts as terms, relations, mappings, realizations — unchanged from M1) **plus a paired native ontology with the same name**, whose classes reuse the **same IRI** as the glossary terms and carry attributes and axioms. Consequences for the tasks:

- **Task 2:** `to_om(op, …)` returns `OMOps(glossary: list[dict], native: list[dict])`. `AddConcept` → `CREATE_TERM` (glossary) **and** `UPSERT_CONCEPT` (native, `conceptType: OWL_CLASS`, same IRI, same name/description). `AddAttribute` → `UPSERT_PROPERTY` (native, kind DATA, domain = the class IRI, range = xsd IRI, `isIdentifier`, `enumValues`, `unit`). `AddAxiom` → `UPSERT_AXIOM` (native). Relations/mappings/synonyms/term updates → glossary. Realizations → neither.
- **Task 6:** each proposal part has up to two OM change sets (`change_set_ids: {glossary, native}`), created, submitted and discarded together; the native one needs the ontology's current `workingRevision` as `baseWorkingRevision`. Applying in M2b: glossary set first, then native.
- **Task 7:** `create_model` creates the glossary **and** the paired native ontology (`name` = slug, `iri` = model base IRI, `namespace` = slug). For models without a paired ontology (e.g. the seeded `retail`), proposal creation fails with `validation_error` and hint "call create_model with pair_existing=true" — `create_model(name, …, pair_existing: bool=False)` creates only the missing paired ontology for an existing glossary.
- **New Task 5b (before Task 6): read path merges the paired ontology.** `ModelService.graph(slug)` = glossary export ∪ native ontology export (OM endpoint for native ontology RDF per the spec; record in ADR 0011), so M1b's attribute lookup (`owl:DatatypeProperty` with `rdfs:domain`) and Task 5's "existing attribute" check see native attributes. Export stays deterministic (same canonical serialiser). Contract: after pairing a throwaway model and applying a native draft that adds an attribute (research-style, cleaned up), `explain_concept` shows the attribute.
- **Seed:** `scripts/seed_om_concepts.py` also creates the paired native ontology for `retail` (classes with the 10 term IRIs; no attributes yet), idempotent.
- **OM client (Task 1 finding):** submit/discard/add-operations require an edit lease; the client acquires and releases one per call (FR-OM-04). Stale bases surface only at apply (M2b). The bot token can apply its own drafts directly — **M2b must enforce human-only apply in Lakeloom** (FR-REV-01/02).
