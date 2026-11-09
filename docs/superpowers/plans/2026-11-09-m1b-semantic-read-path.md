# M1b — Semantic Read Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents can find concepts, have a concept explained, get the business context of a table or column, and export an ontology model in five semantic formats, deterministically.

**Architecture:** OM stays the system of record. A model service loads an ontology model (an OM glossary) by exporting it from OM as Turtle and parsing it with rdflib into a cached graph; concept details (labels, definitions, synonyms, hierarchy, relations, attributes, mappings) are read from that graph plus OM's term API, and realizations (concept ↔ asset) from OM's glossary-term tags on tables/columns. A canonical RDF module gives byte-stable serialisations reused later by Git publishing (M2) and HTTP/MCP resources (M1c).

**Tech Stack:** existing stack + `rdflib` (TC-6); Hypothesis (already a dev dep) for determinism property tests.

**Spec:** `REQUIREMENTS.md` — FR-CTX-01..06, FR-IO-01, FR-GIT-03 (determinism rules, reused here), FR-OM-01 (read endpoints), FR-AGT-01/02/05/06/07, NFR-SEC-14, NFR-PERF-01. Part 2 of 3 of M1 (M1a done; M1c = MCP resources + HTTP read API + OTel/stdio). M1 acceptance owned here: "asset context for `orders` lists concepts realized by its columns (after fixture concepts are seeded); exports in all five formats parse with rdflib; outputs identical across two runs."

## Decisions already taken

- Ontology model = OM glossary (ADR 0007); model **slug** = glossary `name`.
- OM fixture PII tags stay `PII.Sensitive` (owner, 2026-11-09): profile privacy uses lake tags, and M1b acceptance does not need the exact tag.
- Fixture concepts are seeded **directly through OM's APIs by a dev script** (like `om-seed`), not through change sets: FR-OM-02 governs Lakeloom's own writes, which start in M2. Recorded in ADR 0009.
- Release-pinned exports (`version=`) come with releases in M3; M1b exports the current model only.
- "SKOS" export = a SKOS-only projection of the model graph (skos:ConceptScheme, Concept, prefLabel, altLabel, definition, broader, narrower, related, exactMatch/closeMatch/broadMatch/narrowMatch/relatedMatch, inScheme, topConceptOf) serialised as Turtle. ADR 0009.
- JSON-LD asset context uses a minimal `@context` built from the model's prefixes and base IRI; M3's generated `context.jsonld` (FR-PUB-05) replaces it. ADR 0009.

## Global Constraints

- Everything from the M0/M1a Global Constraints applies (Python ≥ 3.12; uv/ruff/pyright strict on `src/`; Conventional Commits + `git commit -s` + requirement IDs; identity `elcio@tuta.com`; `*_FILE` secrets; ADR for every ambiguity; every OM endpoint used is in `OMClient.PATHS` and checked by the spec-path test; typed errors with hints; paginated lists; no tool takes URLs).
- "Deterministic serialisation: the same ontology state MUST produce byte-identical files (stable prefix order, sorted subjects/predicates/objects, canonical blank-node labelling, LF line endings, UTF-8). Covered by property-based tests." (FR-GIT-03 — applied to every M1b export.)
- Export formats: "Turtle, RDF/XML (OWL), N-Triples, JSON-LD and SKOS" (FR-IO-01). "Prefer OM's export endpoint; post-process for determinism."
- Asset input forms: "asset = OM FQN, Unity Catalog name or storage URI" (FR-CTX-01).
- Output formats for context: "Markdown optimised for LLM context windows (default), JSON (pydantic schema published in the docs), and JSON-LD" (FR-CTX-02).
- "Responses carry a `token_estimate` and support `max_tokens` truncation with explicit `truncated: true`." (FR-CTX-06) Token estimate = `ceil(len(text) / 4)` (document in ADR 0009).
- Free text from OM (descriptions, definitions, synonyms) and from lakes is rendered through `quote_untrusted`; names/IRIs through `code_span` (NFR-SEC-14).
- Performance: "Read tools (`get_asset_context`, `explain_concept`, `find_concepts`) p95 < 1.5 s with a warm cache and OM on the same network." (NFR-PERF-01)
- New setting: `LAKELOOM_MODEL_CACHE_TTL_SECONDS` (default 60).

## Review Focus

1. **A model with blank nodes, language-tagged literals and non-ASCII text** (axioms, OWL restrictions): exports stay byte-identical across runs and processes and still parse. → Task 1 Hypothesis test + Task 4 test.
2. **Asset given in any of the three forms, or not resolvable** (unknown UC name, storage URI not in any configured source, OM FQN of a non-table): resolves to the same context, or returns `not_found` with a hint listing the accepted forms. → Task 6 tests.
3. **Hostile concept text** (definition containing Markdown links, fake headings, prompt-injection): Markdown outputs keep it quoted and inert. → Tasks 5 and 6 tests.
4. **OM down / RDF disabled / glossary deleted between calls**: tools return `upstream_unavailable` or `not_found` with hints; a cached model is served until TTL and then errors cleanly. → Tasks 4 and 6 tests.
5. **Large outputs** (a table with hundreds of columns, a concept with hundreds of realizing assets): `max_tokens` truncation keeps the document well-formed and sets `truncated: true`; lists paginate. → Tasks 5 and 6 tests.

## File structure

```
src/lakeloom/rdf/__init__.py
src/lakeloom/rdf/canonical.py      # canonicalise graph, deterministic serialise
src/lakeloom/rdf/skos.py           # SKOS projection
src/lakeloom/om/models.py          # + glossary/term/asset/lineage models (modify)
src/lakeloom/om/client.py          # + read endpoints (modify)
src/lakeloom/core/models.py        # ModelService: list/get/load graph/export
src/lakeloom/core/concepts.py      # find_concepts, explain_concept, find_assets_for_concept
src/lakeloom/core/context.py       # resolve_asset, get_asset_context, renderers
src/lakeloom/core/tokens.py        # token_estimate, truncate
src/lakeloom/server/tools_semantic.py
scripts/seed_om_concepts.py
docs/decisions/0009-semantic-read-path.md
```

---

### Task 1: Canonical RDF serialisation and SKOS projection

**Files:** Create `src/lakeloom/rdf/{__init__,canonical,skos}.py`; Modify `pyproject.toml` (`uv add rdflib`); Test `tests/unit/rdf/test_canonical.py`, `tests/unit/rdf/test_skos.py`

**Interfaces:**
- Produces:
  - `ExportFormat = Literal["turtle","rdfxml","ntriples","jsonld","skos"]`; `MEDIA_TYPES: dict[ExportFormat, str]` = turtle `text/turtle`, rdfxml `application/rdf+xml`, ntriples `application/n-triples`, jsonld `application/ld+json`, skos `text/turtle`; `EXTENSIONS` = `ttl`, `owl`, `nt`, `jsonld`, `skos.ttl`.
  - `def parse_rdf(data: str | bytes, fmt: Literal["turtle","rdfxml","ntriples","jsonld"]) -> rdflib.Graph` — parse errors → `LakeloomError("validation_error", …, hint=…)` with line/column when rdflib provides them.
  - `def canonicalize(graph: Graph) -> Graph` — canonical blank-node labels via `rdflib.compare.to_canonical_graph`, keeps namespace bindings.
  - `def serialize(graph: Graph, fmt: ExportFormat, *, prefixes: Mapping[str, str] | None = None) -> bytes` — always canonicalises first; prefixes bound in sorted order (only the given ones + `rdf`, `rdfs`, `owl`, `xsd`, `skos`, `dcterms`); output UTF-8, LF line endings, trailing newline. N-Triples: lines sorted. Turtle: rdflib's sorted Turtle output. RDF/XML and JSON-LD: serialise from a graph whose triples are inserted in sorted order, then verify stability (if rdflib's RDF/XML or JSON-LD output depends on insertion order, post-process: JSON-LD → `json.dumps(sort_keys=True, indent=2)` with `@graph` sorted by `@id`; RDF/XML → generated from the sorted graph with `pretty-xml` and checked by the property test). `skos` → `serialize(skos_projection(graph), "turtle")`.
  - `skos.skos_projection(graph: Graph) -> Graph` — keeps only triples whose predicate is in the SKOS list in "Decisions" (plus `rdf:type skos:Concept|skos:ConceptScheme`); additionally maps `rdfs:subClassOf` between classes that are also `skos:Concept` to `skos:broader` when no `skos:broader` exists.
- [ ] **Step 1: Tests:** `test_fr_git_03_same_graph_same_bytes_all_formats` (two graphs built in different insertion orders → equal bytes for all five formats); Hypothesis `test_fr_git_03_property_insertion_order_and_bnodes` (random triples incl. blank nodes, language tags `@pt`/`@en`, non-ASCII, datatypes; shuffled → equal bytes; output re-parses to an isomorphic graph for turtle/rdfxml/ntriples/jsonld); `test_lf_and_trailing_newline`; `test_parse_error_reports_line` (bad Turtle → validation_error mentioning the line); `test_skos_projection_keeps_only_skos`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass (Hypothesis `max_examples=200`); ruff + pyright clean.
- [ ] **Step 5: Commit** `feat(rdf): deterministic RDF serialisation and SKOS projection (FR-GIT-03, FR-IO-01)`.

---

### Task 2: OM client — glossary, term, asset and lineage reads

**Files:** Modify `src/lakeloom/om/client.py`, `src/lakeloom/om/models.py`; Test `tests/unit/om/test_client_semantic.py`

**Interfaces:**
- Produces (paths from the committed spec; all in `PATHS`):
  - `list_glossaries(*, after: str|None=None, limit: int=50) -> OMPage[OMGlossary]`; `get_glossary_by_name(name: str, fields: Sequence[str]=("owners","tags","domains")) -> OMGlossary|None`.
  - `list_terms(glossary_fqn: str, *, after: str|None=None, limit: int=100, fields=("parent","synonyms","relatedTerms","conceptMappings","owners","tags","iri")) -> OMPage[OMGlossaryTerm]`; `get_term(id_or_fqn: str, fields=…) -> OMGlossaryTerm|None` (UUID → by id, else by name); `search_terms(q: str, *, glossary_fqn: str|None=None, limit: int=20) -> list[OMGlossaryTerm]`.
  - `get_term_assets(term_id: str, *, offset: int=0, limit: int=50) -> OMPage[OMEntityRef]`.
  - `export_ontology(glossary_id: str, fmt: Literal["turtle","rdfxml","ntriples","jsonld"]="turtle", *, include_relations: bool=True) -> str` (`/v1/glossaries/{id}/exportOntology`).
  - `get_table_by_fqn` gains `fields` incl. `columns,tags,owners,domains,dataProducts,tableConstraints`.
  - `get_lineage(entity_type: str, fqn: str, *, upstream_depth: int=1, downstream_depth: int=1) -> OMLineage` (`/v1/lineage/getLineage` with `fqn`, `type`, depths).
  - Models (`extra="allow"`, aliases for camelCase): `OMGlossary(id, name, fully_qualified_name, display_name, description, namespaces: list[OMNamespace])`, `OMNamespace` (fields per spec `OntologyNamespace`), `OMGlossaryTerm(id, name, fully_qualified_name, display_name, description, iri: str|None, synonyms: list[str], parent: OMEntityRef|None, related_terms: list[OMTermRelation], concept_mappings: list[OMConceptMapping], glossary: OMEntityRef)`, `OMTermRelation` (per spec `TermRelation`: `relation_type`, target term ref, `status`), `OMConceptMapping` (per spec), `OMTagLabel` gains `source` and `label_type`, `OMLineage(nodes: list[OMEntityRef], upstream_edges, downstream_edges)`, `OMPage[T](data: list[T], after: str|None, total: int|None)`.
- [ ] **Step 1: Tests (respx):** one per method incl. query params and 404 → None; `test_export_ontology_returns_text`; spec-path test passes for all new `PATHS`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement; read the response schemas from the spec for field names. **Step 4:** Pass; pyright clean.
- [ ] **Step 5: Commit** `feat(om): glossary, term, asset, export and lineage reads (FR-OM-01)`.

---

### Task 3: Fixture concepts seeded in OM

**Files:** Create `scripts/seed_om_concepts.py`, `docs/decisions/0009-semantic-read-path.md`; Modify `deploy/compose/docker-compose.yml` (om-seed runs both scripts in order), ADR 0003 appendix; Test `tests/contract/test_om_concepts_seed.py`

**Interfaces:**
- Produces in OM (idempotent, PUT create-or-update; token from file; never printed):
  - Glossary `retail` (display "Retail Ontology"), description; ontology namespace/base IRI `http://localhost:8000/ontology/retail/` (Q7 default `{LAKELOOM_PUBLIC_URL}/ontology/{model-slug}/`) and prefix `retail` — set through whatever field the spec/live OM offers on the glossary (`namespaces`), or record in ADR 0009 if OM rc1 has no writable base-IRI field.
  - Terms (glossary `retail`): `Customer`, `Order`, `Product`, `Invoice`, `OrderStatus` (parent `Order`), `CustomerIdentifier`, `OrderIdentifier`, `EmailAddress`, `MonetaryAmount`, `OrderDate`; synonyms (`Customer`: "Client", "Buyer"; `Order`: "Purchase"); definitions in business English; relations `Order —placedBy→ Customer`, `Invoice —billsFor→ Order`, `Order —contains→ Product` using relationship types that exist in OM (create custom relationship types via `/v1/relationshipTypes` if needed, recorded in ADR 0009); concept mappings `Customer` closeMatch `https://schema.org/Person`, `Order` exactMatch `https://schema.org/Order`, `MonetaryAmount` exactMatch `https://schema.org/MonetaryAmount` (only if the term API accepts conceptMappings; else ADR).
  - Realizations (glossary-term tags on assets, source `Glossary`): table `orders` → `retail.Order`; `orders.order_id` → `retail.OrderIdentifier`; `orders.customer_id` → `retail.CustomerIdentifier`; `orders.status` → `retail.OrderStatus`; `orders.total_amount` → `retail.MonetaryAmount`; `orders.order_date` → `retail.OrderDate`; table `customers` → `retail.Customer`; `customers.email` → `retail.EmailAddress`; `customers.customer_id` → `retail.CustomerIdentifier`; tables `products` → `retail.Product`, `invoices` → `retail.Invoice`. Applied by PATCH on the table entity (JSON Patch on `/tags` and `/columns/<i>/tags`), keeping existing classification tags.
- [ ] **Step 1: Contract tests:** `test_seed_creates_retail_glossary_terms` (10 terms, synonyms, parent of OrderStatus); `test_seed_realizations_on_orders` (all five orders columns carry the expected glossary-term tags and the table carries `retail.Order`); `test_concept_seed_idempotent` (run twice → same term ids, no duplicate tags); `test_export_ontology_contains_terms` (turtle export parses with rdflib and contains the 10 terms' IRIs).
- [ ] **Step 2:** Implement + compose; `docker compose -f deploy/compose/docker-compose.yml up -d --build --wait` (om-seed exits 0). **Step 3:** Contract pass. **Step 4:** ADR 0009 (seeding approach, IRIs, relation types, mapping support, what OM rc1 does/doesn't store).
- [ ] **Step 5: Commit** `feat(dev): seed fixture concepts and realizations in OM (FR-CTX-01, NFR-TST-03)`.

---

### Task 4: Model service and export

**Files:** Create `src/lakeloom/core/models.py`; Modify `src/lakeloom/config.py` (`model_cache_ttl_seconds: int = Field(60, ge=0, le=3600)`); Test `tests/unit/core/test_models.py`

**Interfaces:**
- Consumes: Task 1 (`parse_rdf`, `serialize`, `ExportFormat`), Task 2 OM reads.
- Produces:
  - `ModelInfo(slug: str, glossary_id: str, fqn: str, display_name: str|None, description: UntrustedText|None, base_iri: str|None, prefixes: dict[str,str], term_count: int|None)`.
  - `class ModelService`: `__init__(self, om: OMClient, settings: Settings, *, clock: Callable[[], float] = time.monotonic)`; `async list_models(cursor: str|None, limit: int) -> Page[ModelInfo]`; `async get_model(slug: str) -> ModelInfo` (unknown → `not_found`, hint "call list_models"); `async graph(slug: str) -> rdflib.Graph` (OM `export_ontology(id, "turtle")` → `parse_rdf`; cached per slug for `model_cache_ttl_seconds`; concurrent callers share one load via a per-slug `asyncio.Lock`; on OM error after TTL expiry → raise, never serve stale beyond TTL); `async export(slug: str, fmt: ExportFormat) -> bytes` (`serialize(await graph(slug), fmt, prefixes=info.prefixes)`).
  - Base IRI/prefixes come from the glossary's namespaces (Task 3 / ADR 0009); fallback base IRI `f"{settings.public_base_url}/ontology/{slug}/"`.
- [ ] **Step 1: Tests (fake OM with a canned Turtle export containing blank nodes and `@pt`/`@en` labels):** `test_fr_io_01_export_all_five_formats_parse` (each format re-parses with rdflib — skos via turtle); `test_fr_io_01_export_identical_across_two_runs` (two exports, cache cleared in between → equal bytes); `test_graph_cached_within_ttl` (one OM call for two `graph()` calls; second load after clock passes TTL); `test_concurrent_loads_share_one_fetch`; `test_unknown_model_not_found`; `test_om_down_after_ttl_raises_upstream_unavailable`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass; pyright clean.
- [ ] **Step 5: Commit** `feat(core): model service with cached graphs and deterministic export (FR-IO-01, FR-GIT-03)`.

---

### Task 5: Concepts — find, explain, assets for concept

**Files:** Create `src/lakeloom/core/concepts.py`, `src/lakeloom/core/tokens.py`; Test `tests/unit/core/test_concepts.py`, `tests/unit/core/test_tokens.py`

**Interfaces:**
- Consumes: ModelService (T4), OM term/asset reads (T2).
- Produces:
  - `tokens.token_estimate(text: str) -> int` (`ceil(len/4)`); `tokens.truncate_markdown(sections: Sequence[str], max_tokens: int|None) -> tuple[str, bool]` (keeps whole sections in order, appends `> _truncated: further sections omitted_` when cut; never cuts inside a section).
  - `ConceptRef(iri: str|None, fqn: str, model: str, label: str)`; `ConceptMatch(concept: ConceptRef, score: float, matched_on: Literal["name","synonym","local_name","iri","fuzzy"])`.
  - `async find_concepts(models: ModelService, om: OMClient, query: str, *, model: str|None=None, cursor: str|None=None, limit: int=20) -> Page[ConceptMatch]` — scoring: exact name 1.0, exact synonym 0.95, normalised local name (case/space/underscore/camel-insensitive) 0.9, IRI equality 1.0, else fuzzy `difflib.SequenceMatcher` ratio ≥ 0.6 → ratio × 0.8; candidates from the model graphs (`skos:prefLabel`/`rdfs:label`, `skos:altLabel`) plus OM `search_terms` results; sorted by score desc then label; deduplicated by fqn.
  - `ConceptDetail(concept: ConceptRef, definition: UntrustedText|None, synonyms: list[UntrustedText], parents: list[ConceptRef], children: list[ConceptRef], relations: list[RelationInfo(type: str, target: ConceptRef, direction: Literal["out","in"])], attributes: list[AttributeInfo(name: str, iri: str|None, datatype: str|None, unit: str|None, allowed_values: list[str])], mappings: list[MappingInfo(kind: str, target_iri: str)], realizations: Page[AssetRef(fqn: str, type: str, column: str|None)])`.
  - `async explain_concept(models, om, concept: str) -> ConceptDetail` — `concept` = IRI, term FQN, or term id; unknown → `not_found` (hint: "use find_concepts"); attributes read from the model graph (`owl:DatatypeProperty` with `rdfs:domain` = the concept; datatype from `rdfs:range`; units/allowed values from `qudt:unit`/`owl:oneOf` or OM fields if present — absent → empty); realizations from `get_term_assets` (first page, limit 50, `next_cursor`).
  - `async find_assets_for_concept(models, om, concept: str, *, include_descendants: bool=False, cursor: str|None=None, limit: int=50) -> Page[AssetRef]`.
  - `def concept_markdown(detail: ConceptDetail, *, max_tokens: int|None=None) -> tuple[str, bool]` — sections: title (code_span label + IRI), definition (quote_untrusted), synonyms, hierarchy, relations, attributes, mappings, realizing assets.
- [ ] **Step 1: Tests** (fake model graph + fake OM): `test_fr_ctx_03_explain_order` (definition, parent/children OrderStatus, relation placedBy Customer, mapping schema:Order, realizations include `orders`); `test_find_concepts_scoring` ("client" → Customer via synonym 0.95; "order status" → OrderStatus via local name; "custmer" → Customer fuzzy); `test_find_concepts_paginates`; `test_fr_ctx_04_assets_include_descendants` (Order + OrderStatus assets); `test_nfr_sec_14_hostile_definition_quoted` (definition with `[x](https://evil)`, `# SYSTEM`, prompt text → quoted, inert); `test_fr_ctx_06_truncation_keeps_sections_whole` (300 realizations, `max_tokens=200` → `truncated=True`, output ends with the truncation note, no half section); `test_explain_unknown_not_found`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5: Commit** `feat(core): find_concepts, explain_concept, find_assets_for_concept (FR-CTX-03, FR-CTX-04, FR-CTX-06)`.

---

### Task 6: Asset context

**Files:** Create `src/lakeloom/core/context.py`; Test `tests/unit/core/test_context.py`

**Interfaces:**
- Consumes: T4, T5, OM table/lineage reads, M1a `sources` (`dict[str, LakeSource]` with `om_service`, Delta entries' `uri`, `build_fqn`).
- Produces:
  - `async resolve_asset(asset: str, *, sources: Mapping[str, LakeSource], om: OMClient) -> ResolvedAsset(fqn: str, table_fqn: str, column: str|None, om_id: str)` — order: (1) storage URI (`file://`, `s3://`, `abfss://`, `gs://`) → the Delta source entry with that URI (normalised, trailing `/` ignored) → its ref → `build_fqn(om_service, …)`; (2) OM FQN that exists in OM (table, or `table.column`); (3) UC name `catalog.schema.table[.column]` → each source with `om_service` set → `build_fqn(om_service, catalog, schema, table)` exists in OM; unresolvable → `not_found` with hint "pass an OpenMetadata FQN, a Unity Catalog name (catalog.schema.table[.column]) or a configured storage URI".
  - `AssetContext(asset: ResolvedAsset, table_concepts: list[ConceptRef], columns: list[ColumnContext(name, data_type, concepts: list[ConceptRef], description: UntrustedText|None, attributes: list[AttributeInfo])], concept_details: dict[str, ConceptSummary(definition, synonyms, iri, parents)], relationships: list[RelatedAsset(relation: str, concept: ConceptRef, tables: list[str])], owners: list[str], domains: list[str], data_products: list[str], model: str|None, release: str|None (None until M3), lineage: LineageSummary(upstream: list[str], downstream: list[str]), token_estimate: int, truncated: bool)`.
  - `async get_asset_context(asset: str, *, models: ModelService, om: OMClient, sources: …, max_tokens: int|None=None) -> AssetContext` — realized concepts = glossary-term tag labels (source `Glossary`) on the table and its columns (or only the given column); concept summaries from the model graph; relationships: for each realized table concept, relations from the graph whose target concept is realized by other tables (via `get_term_assets`); lineage from `get_lineage` depth 1 (failure → empty + no error).
  - Renderers: `render_markdown(ctx) -> str`, `render_json(ctx) -> dict` (pydantic `model_dump(mode="json", by_alias=True)`; JSON schema exported via `AssetContext.model_json_schema()` into `docs/schemas/asset-context.schema.json`), `render_jsonld(ctx, model: ModelInfo) -> dict` (`@context` with `@vocab`=base IRI + prefixes; asset as `@id` = OM entity href or `urn:lakeloom:asset:<fqn>`; concepts as `@type`/`dcterms:subject`).
- [ ] **Step 1: Tests** (fake OM with the seeded shape, fixture sources): `test_fr_ctx_01_orders_context_lists_column_concepts` (**M1 acceptance shape**: table concept `Order`; columns map order_id→OrderIdentifier, customer_id→CustomerIdentifier, status→OrderStatus, total_amount→MonetaryAmount, order_date→OrderDate); `test_fr_ctx_01_three_input_forms_same_context` (OM FQN, `retail.sales.orders` UC name, `file:///…/orders` URI → same `table_fqn`); `test_column_asset_context` (`…orders.status` → only that column); `test_unresolvable_asset_not_found_with_forms_hint`; `test_relationships_point_to_customers_table`; `test_lineage_failure_is_empty`; `test_fr_ctx_02_json_and_jsonld_render` (JSON validates against the exported schema; JSON-LD parses with rdflib into ≥ 1 triple); `test_fr_ctx_06_token_estimate_and_truncation` (table with 400 columns, `max_tokens=500` → `truncated=True`, Markdown well-formed); `test_nfr_sec_14_hostile_column_description_quoted`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5: Commit** `feat(core): asset context with three asset forms and three renderings (FR-CTX-01, FR-CTX-02, FR-CTX-06)`.

---

### Task 7: MCP tools, CLI, acceptance

**Files:** Create `src/lakeloom/server/tools_semantic.py`; Modify `src/lakeloom/server/mcp_app.py`, `src/lakeloom/server/http_app.py` (AppDeps gains `models: ModelService`), `src/lakeloom/cli.py` (`lakeloom models`, `lakeloom concept QUERY|IRI`, `lakeloom context ASSET [--format md|json|jsonld]`, `lakeloom export MODEL --format F [--out PATH]`), `README.md`, `CHANGELOG.md`; Test `tests/unit/server/test_tools_semantic.py`, `tests/contract/test_m1b_acceptance.py`

**Interfaces:**
- MCP tools (scope `lakeloom:read`; annotations readOnly=True, destructive=False, idempotent=True, openWorld=False; LLM-oriented descriptions; typed errors; `guarded()` from M1a):
  - `list_models(cursor=None, limit=50)`, `get_model(model: str)`.
  - `find_concepts(query: str, model: str|None=None, cursor=None, limit=20)`.
  - `explain_concept(concept: str, max_tokens: int|None=None)`.
  - `find_assets_for_concept(concept: str, include_descendants: bool=False, cursor=None, limit=50)`.
  - `get_asset_context(asset: str, format: Literal["markdown","json","jsonld"]="markdown", max_tokens: int|None=None)` — text = Markdown (or a one-line summary for json/jsonld), structured content = the chosen rendering + `token_estimate` + `truncated`.
  - `export_model(model: str, format: ExportFormat="turtle")` — text = the serialisation (≤ 8 MiB, else `validation_error` hint "use the CLI or HTTP API"), structured = `{model, format, media_type, bytes, sha256}`.
- [ ] **Step 1: Tests:** unit — annotations/scopes for all seven tools; `get_asset_context` json mode structured content validates against the schema; `export_model` sha256 stable across two calls; CLI `context` prints Markdown. Contract (`tests/contract/test_m1b_acceptance.py`, agent token): **`test_m1_acceptance_orders_context_lists_column_concepts`** (MCP `get_asset_context("lakeloom_fixture.retail.sales.orders")` → the five column concepts and table concept Order); **`test_m1_acceptance_exports_five_formats_parse_and_are_stable`** (MCP `export_model("retail", f)` for all five formats → each parses with rdflib (skos as turtle); two calls → identical sha256); `test_find_concepts_client_finds_customer`; `test_explain_concept_order`; `test_nfr_perf_01_warm_read_tools_p95` (20 warm calls each of get_asset_context/explain_concept/find_concepts; p95 < 1.5 s).
- [ ] **Step 2:** FAIL. **Step 3:** Implement; `docker compose -f deploy/compose/docker-compose.yml up -d --build --wait`. **Step 4:** Unit + contract pass; pytest output pristine; ruff + pyright clean.
- [ ] **Step 5:** README (semantic tools section, examples), CHANGELOG `[Unreleased]`.
- [ ] **Step 6: Commit** `feat(mcp): semantic read tools, CLI and M1b acceptance (FR-CTX-01..04, FR-IO-01, FR-AGT-01)`.

---

## Out of scope for M1b

MCP resources and HTTP read API, OTel, stdio transport (M1c); release-pinned exports, SHACL, `context.jsonld`, import (M3); proposals and change sets (M2); SPARQL (M3).
