# 0009. Semantic read path (M1b): fixture seeding, IRIs, exports

- Status: accepted
- Date: 2026-11-10
- Requirements: FR-CTX-01, FR-CTX-06, FR-IO-01, FR-GIT-03, NFR-TST-03

## Context

M1b reads ontology models (OM glossaries, ADR 0007) and their realizations (glossary-term
tags on assets). It needs fixture concepts in the dev OpenMetadata, deterministic RDF
exports, and decisions about identifiers. All OM behaviour below was verified live against
OM 2.1.0-rc1 (compose stack, `RDF_ENABLED=false`, no Fuseki container) on 2026-11-10.

## Decision

### Fixture seeding (Task 3)

- Fixture concepts are seeded **directly through OM's APIs** by `scripts/seed_om_concepts.py`,
  not through change sets: FR-OM-02 governs Lakeloom's own writes, which start in M2. The
  compose one-shot `om-seed` runs `seed_om_fixture.py` and then `seed_om_concepts.py` in one
  `sh -c`. Both are idempotent (PUT create-or-update, or check-then-add), the token is read
  from the token file and never printed.
- Glossary `retail` (display "Retail Ontology"); 10 terms: Customer, Order, Product, Invoice,
  OrderStatus (child of Order), CustomerIdentifier, OrderIdentifier, EmailAddress,
  MonetaryAmount, OrderDate. Synonyms: Customer "Client"/"Buyer"; Order "Purchase".
- Endpoints used: `PUT /v1/glossaries`, `PUT /v1/glossaryTerms`, `PUT /v1/relationshipTypes`,
  `POST /v1/glossaryTerms/{id}/relations`, `GET /v1/tables/name/{fqn}`,
  `PATCH /v1/tables/{id}` (`application/json-patch+json`).
- **Child term FQNs nest under the parent.** OrderStatus is `retail.Order.OrderStatus`, so
  the tag on `orders.status` is `retail.Order.OrderStatus` (not `retail.OrderStatus`).
  Consumers must use the term FQN (or id), never `glossary + "." + name`.
- **Relation types.** OM ships `antonym, broader, calculatedFrom, hasPart, narrower, partOf,
  relatedTo, seeAlso, subClassOf, superClassOf, synonym, usedToCalculate`. `placedBy`,
  `billsFor`, `contains` are created as `CUSTOM` relationship types (required fields: category,
  description, displayName, name, paletteKey, rdfPredicate; `rdfPredicate` =
  `<base IRI><name>`). Relations: Order placedBy Customer, Invoice billsFor Order, Order
  contains Product.
- **Relation direction is not preserved by rc1.** `POST /v1/glossaryTerms/{id}/relations`
  stores the edge, but `relatedTerms` shows it on both terms with the same `relationType` and
  no direction marker, and the Turtle export emits the triple in both directions (for example
  both `Order placedBy Customer` and `Customer placedBy Order`). `relationsGraph` orders
  edges inconsistently (Invoice billsFor Order came back as Order to Invoice). Lakeloom must
  treat OM relations as undirected typed links until a fix or an axiom-based representation;
  M1b tools label them "related (placedBy)" without claiming direction. Making a type
  asymmetric through `inverse`/`characteristics` was not attempted.
- **Concept mappings are stored** (`conceptMappings: [{conceptIri, mappingType}]` on the
  term PUT) and exported as `skos:closeMatch` / `skos:exactMatch`. Seeded: Customer CLOSE_MATCH
  `https://schema.org/Person`, Order EXACT_MATCH `https://schema.org/Order`, MonetaryAmount
  EXACT_MATCH `https://schema.org/MonetaryAmount`.
- **Realizations** are glossary-term tags (`source: Glossary`, `labelType: Manual`) written by
  JSON Patch on the table: `add /tags` and `add /columns/<i>/tags` with the full list, so
  classification tags (`PII.Sensitive`) are kept. Column PATCH works reliably. OM *displays*
  table-level glossary tags on each column with `labelType: Derived`; the seed filters Derived
  tags before writing so they are never persisted as column tags. Consumers wanting a
  column's own realizations should ignore `labelType: Derived`.
- Re-running `PUT /v1/tables` (the fixture seed) leaves existing glossary tags in place
  (verified), but the concepts script still runs after it, as in compose, because the tables
  must exist first.

### IRIs and namespaces (Q7)

- Intended base IRI: `{LAKELOOM_PUBLIC_URL}/ontology/{model-slug}/` =
  `http://localhost:8000/ontology/retail/`, prefix `retail`.
- **Glossary `namespaces` is not writable in rc1.** `CreateGlossary` has no such field, and
  `PATCH /v1/glossaries/{id}` with `add /namespaces` answers 200 with the value echoed but
  nothing is persisted (a following GET returns `[]`, version unchanged). So the base IRI and
  prefix cannot be stored in OM; Lakeloom derives them from configuration (M3 stores them in
  the model's repository metadata).
- **Term `iri` is honoured.** Without it OM mints `https://open-metadata.org/entity/
  glossaryTerm/<uuid>` (and `.../glossary/<uuid>` for the scheme). With `iri` set on the term
  PUT, the Turtle export uses it everywhere (subject, `skos:broader`, `skos:hasTopConcept`,
  relation objects). The seed therefore sets every term's `iri` to `<base IRI><TermName>`, so
  exports carry stable, dereferenceable-style IRIs. The glossary scheme IRI stays
  `https://open-metadata.org/entity/glossary/<id>` (not settable). Custom relationship types
  take `rdfPredicate` as their IRI.
- Excerpt (`GET /v1/glossaries/{id}/exportOntology?format=turtle`, RDF off):

  ```turtle
  <http://localhost:8000/ontology/retail/OrderStatus>
          rdf:type               om:GlossaryTerm , skos:Concept;
          skos:broader           <http://localhost:8000/ontology/retail/Order>;
          skos:prefLabel         "OrderStatus";
          om:fullyQualifiedName  "retail.Order.OrderStatus" .
  ```

### Exports (Task 1 implementation, `src/lakeloom/rdf/`)

- Export is OM's `exportOntology` (raw RDF). In rc1 it works with RDF off: the dev stack runs `RDF_ENABLED=false` with no Fuseki (the `rdf` compose profile is opt-in) and the export still returns full Turtle, so the graph is built by OM itself, not read from a triple store
  post-processed for determinism (FR-GIT-03): canonical blank-node labels `b0..bn`, sorted
  triples, LF, UTF-8, one trailing newline.
- Predicate/type namespaces without a bound prefix are named `ns1..nsN` in sorted namespace
  order, skipping any `nsN` already used by a caller prefix.
- JSON-LD is an **expanded** top-level array of nodes (full IRIs, no `@context`). Lakeloom
  asset JSON-LD uses a minimal `@context` built from the model's prefixes and base IRI; M3's
  generated `context.jsonld` (FR-PUB-05) replaces it.
- RDF/XML and JSON-LD use **own writers**: rdflib's XML writers order subjects by set
  iteration (non-deterministic) and mis-prefix `xml:lang`; its JSON-LD writer drops
  blank-node-only cycles.
- "SKOS" export = a SKOS-only projection of the model graph: `skos:ConceptScheme`,
  `skos:Concept`, `prefLabel`, `altLabel`, `definition`, `broader`, `narrower`, `related`,
  `exactMatch`/`closeMatch`/`broadMatch`/`narrowMatch`/`relatedMatch`, `inScheme`,
  `topConceptOf`, serialised as Turtle. A `skos:broader` is derived from `rdfs:subClassOf`
  between two concepts when the child has none.
- Release-pinned exports (`version=`) arrive with releases in M3; M1b exports the current
  model only.
- `token_estimate = ceil(len(text) / 4)` for every response (FR-CTX-06); `max_tokens`
  truncation sets `truncated: true`.

## Consequences

- Dev stack always has a small, known ontology for tests (NFR-TST-03); contract tests
  `tests/contract/test_om_concepts_seed.py` pin the behaviours above.
- Undirected relations and the missing base-IRI field limit what M1b can say about the model;
  M2/M3 (Lakeloom-owned writes, repo-backed model) must carry direction and namespaces
  itself.
- If a later OM build persists `namespaces` or directed relations, the seed and tools
  should adopt them.

## Graph vocabulary used by Lakeloom

Read from OM rc1's Turtle export (Task 5, `src/lakeloom/core/concepts.py`). A concept is a
`URIRef` subject typed `skos:Concept`.

| Need | Predicate |
|---|---|
| FQN | `om:fullyQualifiedName` (`https://open-metadata.org/ontology/`); fallback `<slug>.<local name>` |
| Label | `skos:prefLabel`, then `rdfs:label`, then the IRI local name |
| Synonyms | `skos:altLabel` |
| Definition | `skos:definition` (untagged or English preferred) |
| Parent / child | `skos:broader` / `skos:narrower`, each also read inversely; `rdfs:subClassOf` between concepts is accepted as parent |
| Relations | an `owl:ObjectProperty` (other than `skos:*`) between two concepts; the type is the predicate's local name (`placedBy`). OM stores each relation on both terms with no direction, so both stored triples collapse into one relation with `direction: "both"` |
| Mappings | `skos:exactMatch`, `closeMatch`, `broadMatch`, `narrowMatch`, `relatedMatch`; `kind` is the local name |
| Attributes | `owl:DatatypeProperty` with `rdfs:domain` = the concept; datatype from `rdfs:range`, unit from `qudt:unit`, allowed values from `owl:oneOf` on the property or its range. OM rc1 exports none, so they are empty until a model carries them |

Realizations come from `get_term_assets`. OM returns, for a term, its tagged tables, its tagged
columns, the *parent table* of a tagged column and the columns that merely inherit a table-level
tag. Lakeloom keeps only assets whose table or column carries the term as a direct glossary tag
(`labelType != Derived`), which needs one cached `get_table_by_fqn` per table. A column
(`type: tableColumn`) is reported as `AssetRef(fqn=<table FQN>, type="column", column=<name>)`.
`find_concepts` ranks graph candidates plus OM `search_terms` hits (rc1 search finds nothing for
synonyms such as "client"; the graph answers). `include_descendants` walks children to depth 10,
cycle-safe.
