# 0011. Proposals: OM change sets, glossary creation, axiom validation

- Status: accepted (OM payload shapes, M2a Task 1; hybrid model, Amendment A and Task 5b; later M2a tasks extend it)
- Date: 2026-11-11
- Requirements: FR-OM-01, FR-OM-02, FR-OM-05a, FR-PRO-01, FR-PRO-03, FR-CTX-05

## Context

M2a turns scans into proposals: OM ontology change sets that a reviewer applies later
(M2b). FR-OM-02: "All ontology writes go through change sets. Lakeloom MUST NOT bypass
change sets for concepts, relations, attributes, axioms or mappings." The OM 2.1 docs say
change sets go "Draft → Submitted → Applied (or Apply Failed, Discarded); operations are
typed and carry the base version of the entity they modify", and name an `UPSERT_ATTRIBUTE`
operation that the rc1 spec does not have. Everything below was checked live against OM
2.1.0-rc1 (compose dev stack, bot JWT `ingestion-bot`) on 2026-11-11, on throwaway glossaries
`llresearch1`/`llresearch2` and a throwaway native ontology `llresearch1`, all hard-deleted
afterwards. The seeded `retail` glossary was not touched.

## Decision

### Models (glossaries) are created directly

`create_model` creates the glossary with `POST /v1/glossaries` (`CreateGlossary`; required
`name`, `description`; optional `displayName`). A model is not one of the FR-OM-02 kinds.
Duplicate name → **409** `Entity already exists`. rc1 also has a `CREATE_GLOSSARY`
operation; it is not used.

### Two kinds of change set; one cannot mix them

rc1 has two disjoint change-set flavours:

| Flavour | Target fields | Allowed operations (observed) |
|---|---|---|
| Glossary change set | `glossaries: [<glossary name/FQN>, ...]` (at least one) | `CREATE_TERM`, `UPDATE_TERM`, `DELETE_TERM`, `ADD_RELATIONSHIP`, `UPSERT_MAPPING` (and the other relationship/mapping ops) |
| Native ontology change set ("native draft") | `ontology: <ontology name>` + `baseWorkingRevision` | `UPSERT_CONCEPT`, `UPSERT_PROPERTY`, `UPSERT_AXIOM` (and their deletes) |

- `UPSERT_PROPERTY`, `UPSERT_CONCEPT` or `UPSERT_AXIOM` in a glossary change set → **400**
  `Axioms must be authored in an ontology, not a glossary change set`.
- `ontology` together with `glossaries`, or without `baseWorkingRevision` → **400**
  `Native drafts require one ontology and its baseWorkingRevision`.
- No glossary and no ontology → **400** `Ontology change sets require unique editable
  glossaries`.
- **A glossary change set can span glossaries** (`glossaries: ["a", "b"]` accepted).
- `glossaries` takes **names/FQNs, not ids**: an id → **404** `glossary instance for <id>
  not found`. `OMClient.create_change_set(glossary_id=...)` therefore resolves an id to its
  FQN with `GET /v1/glossaries/{id}` (a non-UUID is used as is).
- The native ontology is a separate entity (`POST /v1/ontologies`, required `name`, `iri`,
  `namespace`), *not* the glossary. Its `GET /v1/ontologies/{id}/state` returns
  `{"workingRevision": n, "lastReleaseRevision": 0, "activationRevision": 0}`;
  `workingRevision` starts at 1 and grows by one per applied definition.

**Which op is an "attribute" in rc1.** `UPSERT_PROPERTY` with `property.kind: "DATA"`
(`owl:DatatypeProperty`; `OBJECT` and `ANNOTATION` also exist) and `isIdentifier`, `range`
(XSD IRIs), `enumValues`, `unit`. There is no attribute field on glossary terms.
**But a property's `domain` must be a class declared in the same native ontology**: a
property whose domain is a glossary term's IRI is accepted as a draft and fails at apply
with **400** `Ontology validation failed: Declare or import the referenced class: <term
IRI>` (`violations[].code = UNDECLARED_CLASS`, `memberKind: PROPERTY`). Native classes are
`UPSERT_CONCEPT` (`OntologyConcept`, `conceptType: OWL_CLASS | SKOS_CONCEPT | BOTH`), which
are distinct from glossary terms. ADR 0007 ("a model is a glossary; do not use
`/v1/ontologies`") therefore cannot hold for attributes and axioms as-is; how a Lakeloom
model maps onto glossary + native ontology is an open decision for the M2a controller (see
Consequences).

**Relationship types** are referenced as `relationship.relationshipType: {"id": <uuid>,
"type": "relationshipType"}` (ids from `GET /v1/relationshipTypes`, including the seeded
custom `placedBy`/`billsFor`/`contains`, ADR 0009). The draft does not check the id: a random
id plus `name` was accepted at create time, so Lakeloom must resolve real ids itself.

**hasKey.** `OntologyAxiom.axiomType` is `SUBCLASS_OF | EQUIVALENT_CLASS | DISJOINT_WITH |
CLASS_ASSERTION | OBJECT_PROPERTY_ASSERTION | DATA_PROPERTY_ASSERTION`: no `owl:hasKey`.
Primary keys are expressed as `isIdentifier: true` on the property, optionally plus an
exact-cardinality `SUBCLASS_OF` restriction (below). Both were applied successfully.

### Minimal accepted operation payloads (ids redacted to `<…>`)

Every operation needs a client-chosen `id` (UUID) and `state: "ACTIVE"` (else 400 `query
param id must not be null`); `OMClient` fills both. Embedded entities also need their own
`id`. **Ids of created entities are not kept**: on apply OM mints new ids, and remaps
references inside the same change set (an `ADD_RELATIONSHIP` whose `toTerm.id` was the
draft id of a `CREATE_TERM` in the same set was applied to the new term).

Rule (verbatim from rc1): `targetId is required for non-create operations`; `baseVersion is
required for an existing target` (any `targetId` triggers it); for native definitions
`Existing definitions require targetId and baseVersion; new definitions require neither`.

```jsonc
// CREATE_TERM (glossary change set)
{"id": "<op>", "state": "ACTIVE", "operationType": "CREATE_TERM",
 "term": {"id": "<draft term>", "name": "Widget", "description": "a widget",
          "glossary": {"id": "<glossary>", "type": "glossary"}}}
// optional on term: displayName, synonyms, iri, parent, conceptMappings, ...

// UPDATE_TERM — term is the full desired term; baseVersion = the term's current `version`
{"id": "<op>", "state": "ACTIVE", "operationType": "UPDATE_TERM",
 "targetId": "<term>", "baseVersion": 0.1,
 "term": {"id": "<term>", "name": "Base", "description": "Base term updated",
          "glossary": {"id": "<glossary>", "type": "glossary"}}}

// DELETE_TERM
{"id": "<op>", "state": "ACTIVE", "operationType": "DELETE_TERM",
 "targetId": "<term>", "baseVersion": 0.1}

// ADD_RELATIONSHIP — targetId/baseVersion are the *from* term's; all relationship fields
// below are required by the schema (createdAt/createdBy/status/provenance/id)
{"id": "<op>", "state": "ACTIVE", "operationType": "ADD_RELATIONSHIP",
 "targetId": "<from term>", "baseVersion": 0.1,
 "relationship": {"id": "<rel>", "fromTerm": {"id": "<from term>", "type": "glossaryTerm"},
   "toTerm": {"id": "<to term>", "type": "glossaryTerm"},
   "relationshipType": {"id": "<relationshipType>", "type": "relationshipType"},
   "provenance": "Manual", "status": "Draft", "createdAt": 0, "createdBy": "lakeloom"}}

// UPSERT_MAPPING — on an existing term
{"id": "<op>", "state": "ACTIVE", "operationType": "UPSERT_MAPPING",
 "targetId": "<term>", "baseVersion": 0.1,
 "mapping": {"conceptIri": "https://schema.org/Thing", "mappingType": "CLOSE_MATCH"}}

// UPSERT_CONCEPT (native ontology change set) — new definition: no targetId/baseVersion
{"id": "<op>", "state": "ACTIVE", "operationType": "UPSERT_CONCEPT",
 "concept": {"id": "<concept>", "name": "Widget", "fullyQualifiedName": "<ontology>.Widget",
   "iri": "http://localhost:8000/ontology/<model>/Widget", "conceptType": "OWL_CLASS",
   "ontology": {"id": "<ontology>", "type": "ontology"}, "description": "A widget"}}

// UPSERT_PROPERTY — the attribute (owl:DatatypeProperty)
{"id": "<op>", "state": "ACTIVE", "operationType": "UPSERT_PROPERTY",
 "property": {"id": "<property>", "name": "widgetWeight",
   "fullyQualifiedName": "<ontology>.widgetWeight",
   "iri": "http://localhost:8000/ontology/<model>/widgetWeight", "kind": "DATA",
   "description": "weight", "domain": ["http://localhost:8000/ontology/<model>/Widget"],
   "range": ["http://www.w3.org/2001/XMLSchema#decimal"], "isIdentifier": true,
   "ontology": {"id": "<ontology>", "type": "ontology"}}}

// UPSERT_AXIOM — cardinality: Widget ⊑ (= 1 widgetWeight)
{"id": "<op>", "state": "ACTIVE", "operationType": "UPSERT_AXIOM",
 "axiom": {"id": "<axiom>", "name": "widgetHasOneWeight",
   "fullyQualifiedName": "<ontology>.widgetHasOneWeight",
   "displayName": "Widget has exactly one weight", "description": "card",
   "axiomType": "SUBCLASS_OF", "subjectIri": "http://localhost:8000/ontology/<model>/Widget",
   "provenance": "Manual", "ontology": {"id": "<ontology>", "type": "ontology"},
   "expressions": [{"kind": "RESTRICTION", "restrictionKind": "EXACT",
     "propertyIri": "http://localhost:8000/ontology/<model>/widgetWeight", "cardinality": 1}]}}
```

Draft creation validates shape only (required fields, target/base rules, flavour). It does
**not** check that base versions are current, that referenced ids exist, or that IRIs are
declared; submit does not either. All of that happens at apply.

### Base versions and stale bases

- `baseVersion` is the entity's OM `version` (a float such as `0.1`, `18.9`) at the time the
  operation was drafted; for relationships and mappings it is the *from/target term's*
  version. Native drafts instead carry one change-set-wide `baseWorkingRevision` (the
  ontology's `workingRevision`).
- **Stale glossary base** (UPDATE_TERM with `baseVersion` 0.05 while the term was 0.1):
  create 201, submit 200, apply **400** `Ontology operation '<op id>' expected version 0.05
  but found 0.1`; nothing applied, change set stays `SUBMITTED`, no `applicationResult`.
- **Stale native revision** (draft at revision 1, ontology at 4): apply **412** `Ontology
  changed since this draft was created`.
- Within one change set, several operations on the same term may all carry the version from
  before the change set (three ops on a term at 0.1 applied; it ended at 0.3).
- `OMClient` maps 409, 412 and the 400 `expected version … but found …` to `OMConflict`,
  with the UUIDs named in OM's message in `entity_ids` (the operation id for a stale base;
  the change set id for a lease conflict). OM's message is kept in `OMError.detail`
  (capped at 500 chars, untrusted), never in `str(exc)`.

### Authoring leases (not in the docs)

`PUT …/operations`, `submit`, `discard`, `apply` (and `undo`/`redo`) all require
`{"lease": {"sessionId": s, "version": n}}` (else 400 `query param lease must not be null`).
A lease is taken with `POST /v1/ontologyEditLocks/acquire`
`{"resourceType": "ontologyChangeSet", "resourceId": <cs id>, "sessionId": s,
"leaseSeconds": 60}` → `OntologyEditLock` (`version` increments per acquisition; default
lease 60 s). Another session while held → **409** `… is being edited by another session`;
a wrong/expired lease → **409** `The ontology edit lease is missing, expired, or no longer
owned by this session`; re-acquire by the same session needs `expectedVersion`. Released with
`DELETE /v1/ontologyEditLocks/ontologyChangeSet/{id}?sessionId=s` (204); apply releases it,
submit/discard do not. `OMClient` takes a fresh `lakeloom-<uuid>` session per command and
releases it in `finally` (best effort). Creating a change set needs no lease.

### Change-set names and lifecycle

- `name`: 1–256 chars, pattern `^((?!::)[^>"\x00-\x1f])*$` (no `::`, `>`, `"` or control
  chars); spaces, dots and `/` are allowed (FQN then becomes quoted, e.g.
  `"a name.with.dots"`). Names are unique: a duplicate → **409** `Entity already exists`.
  `displayName` and `description` are required (400 if missing).
- `state` on the wire is upper case: `DRAFT`, `SUBMITTED`, `APPLIED`, `APPLY_FAILED`,
  `DISCARDED` (the docs' "Draft", "Apply Failed" are labels). `undoCursor` = number of
  active operations; operations past it are `UNDONE`.
- `PUT …/operations` (`UpdateOntologyChangeSet` `{lease, operations, undoCursor}`)
  **replaces** the whole list (the old operation ids disappear); `OMClient.add_operations`
  reads the active operations and sends them back with the new ones. Only `DRAFT` is
  editable (else 400 `… is not editable in state SUBMITTED`).
- `submit` → 200 with the full `OntologyChangeSet`, `state: SUBMITTED`, version bumped,
  `changeDescription` `state DRAFT→SUBMITTED`; no review task or reviewers are created
  (`reviewTask`/`reviewers` absent). An empty change set can be submitted.
- `discard` → 200 with the change set, `state: DISCARDED`; allowed from DRAFT and SUBMITTED.
  Repeated transitions → 400 `transition from DISCARDED to DISCARDED is not allowed`.
- `GET /v1/ontologyChangeSets/{id}` of an unknown id → 404 (`get_change_set` → `None`).
  `GET /v1/ontologyChangeSets?state=SUBMITTED` filters by state.
- Hard delete: `DELETE /v1/ontologyChangeSets/{id}?hardDelete=true` (any state).

### Apply (research only; Lakeloom never applies in M2a)

- `POST …/apply` `{"lease": …}` → 200 with `state: APPLIED` and `applicationResult`
  `{appliedAt, appliedBy, operationsProcessed, operationsApplied, operationsFailed,
  results: [{operationId, status: APPLIED, entity: {id, type, name, fullyQualifiedName, …},
  entityVersion}]}`. Native validation failures and stale bases fail the whole change set
  (nothing applied).
- **The bot can apply its own change sets, even straight from `DRAFT`, with no reviewer.**
  No reviewers were configured on the glossary or change set; reviewer gating was not
  observed. M2b must therefore gate apply in Lakeloom (`lakeloom:review`/`publish`) and
  should apply with the reviewer's own identity once OM uses Keycloak SSO; the bot JWT must
  never be used to apply on behalf of a proposer.
- Observed rc1 defect: an applied `UPDATE_TERM` changed `synonyms` but **not**
  `description` (term version bumped, description unchanged). M2b must re-check description
  updates.

### Axiom validation

`POST /v1/ontologyAxioms/validate` takes a `CreateOntologyAxiom` (`ontology` is the native
ontology's **name**; required `name`, `displayName`, `description`, `axiomType`,
`subjectIri`, `provenance`) and returns `OntologyProfileReport`
`{"valid": bool, "violations": [{"severity": "ERROR"|"WARNING", "code", "path",
"message"}]}`. It checks against the ontology's *applied* declarations: before the concept
and property were applied it reported `UNDECLARED_CLASS` (`/subjectIri`) and
`UNDECLARED_PROPERTY` (`/propertyIri`); afterwards `{"valid": true, "violations": []}`. An
`owl:Thing` subject → `RESERVED_SUBJECT_IRI`. An unknown ontology (or a glossary name) →
404 `ontology instance for <x> not found`. `OMAxiomValidation.errors` lists the ERROR
messages.

## Consequences

- `OMClient` gains `create_glossary`, `create_change_set` (glossary *or* native ontology),
  `add_operations`, `get_change_set`, `submit_change_set`, `discard_change_set`,
  `validate_axiom`, the lease handling they need, and `OMConflict`. There is deliberately no
  `apply` method in M2a.
- **Decided (owner, Amendment A): hybrid model.** Each Lakeloom model is the OM glossary
  (concepts as terms, relations, mappings; unchanged from M1) **plus a paired native OM
  ontology with the same name**, whose classes (`OntologyConcept`, `OWL_CLASS`) reuse the
  glossary terms' IRIs and which carries attributes (`UPSERT_PROPERTY`, kind DATA) and axioms.
  A proposal becomes up to two change sets (glossary first, then native). This **explicitly
  supersedes ADR 0007's "Lakeloom does not use the undocumented `/api/v1/ontologies`
  endpoints"**: the rc1 spec has them and attributes cannot be expressed without them.
- M2b's apply path must handle leases, 400/412 conflicts, id remapping of created entities
  (`applicationResult.results[].entity.id`), and must not rely on OM reviewer gating.

## Appendix: typed operations and IRI scheme (M2a Task 2)

`lakeloom.core.operations` holds the typed proposal operations and the pure
`to_om(op, OMContext) -> OMOps(glossary, native)` (hybrid model, Amendment A).

IRI scheme (`OMContext.base_iri` is the model base, e.g. `http://localhost:8000/ontology/retail/`):

- concept / glossary term / native class: `{base_iri}{Name}` (percent-encoded)
- attribute (native `DATA` property): `{base_iri}{Concept}.{attribute}`; its native `name` is
  `{Concept}.{attribute}` (property names are ontology-wide, attribute names are not; a dotted
  name was accepted and applied live, 2026-11-11), `displayName` the attribute name. Range is the XSD IRI of the `xsd:` CURIE; `domain` is `[concept IRI]`.
- axiom: `subjectIri` is the concept IRI; removals address axioms by `{base_iri}{Concept}.{axiom name}`.
- `OMContext.native_ids`/`native_versions` keys are namespaced `concept:<iri>`, `attr:<iri>`, `axiom:<iri>`.

| Operation | Glossary change set | Native change set |
|---|---|---|
| AddConcept | CREATE_TERM (draft id = uuid5 of op key) | UPSERT_CONCEPT (OWL_CLASS, same IRI/name/description) |
| UpdateConcept | one UPDATE_TERM per term with the full term built from a `TermSnapshot` plus the changes | - |
| RemoveConcept | DELETE_TERM | DELETE_CONCEPT (fails closed if the class is not in `native_ids`) |
| AddAttribute / RemoveAttribute | - | UPSERT_PROPERTY / DELETE_PROPERTY |
| AddRelation / RemoveRelation | ADD_RELATIONSHIP / DELETE_RELATIONSHIP | - |
| AddMapping / RemoveMapping | UPSERT_MAPPING / DELETE_MAPPING (`closeMatch` -> `CLOSE_MATCH`, ...) | - |
| AddAxiom / RemoveAxiom | - | UPSERT_AXIOM / DELETE_AXIOM |
| AddSynonym | folded into the term's single UPDATE_TERM (snapshot synonyms + new, case-insensitive dedupe); into the CREATE_TERM if the concept is created in the same batch | - |
| Add/RemoveRealization | - | - (Lakeloom-side) |

Delete payloads (`DELETE_PROPERTY/AXIOM/CONCEPT/RELATIONSHIP/MAPPING`) follow the generic rule
(`targetId` + `baseVersion`) and the rc1 operation enum; they were not exercised live.

`to_om_all(ops, ctx)` is the entry point for proposals: it folds every `UpdateConcept` and
`AddSynonym` of one term into one `UPDATE_TERM` (separate updates would overwrite each other).
References qualified by another model (`finance.Customer`) are rejected (no cross-model links
in M2a; imports needed).

### UPDATE_TERM replaces the term (live check, 2026-11-11)

On a throwaway glossary (hard-deleted afterwards) a term with description, displayName,
synonyms `[a]` and iri was updated by an UPDATE_TERM whose `term` held only id, name, glossary,
description and synonyms `[a, b]`. After apply: synonyms `[a, b]`, description and displayName
kept, but **`iri` became null**. So an update must carry the full term (the iri ties the
glossary term to its native class), and a `term` without `description` is rejected at create
(400 `query param description must not be null`). Lakeloom builds every UPDATE_TERM from a
snapshot of the current term and fails closed (`validation_error`) when it is missing.

## Appendix: the paired native ontology (M2a Task 5b)

Verified live against 2.1.0-rc1 on 2026-11-11 (throwaway ontologies, bot JWT).

- `POST /v1/ontologies` needs `name`, `iri` and `namespace`; **`namespace` must be an absolute
  IRI** (400 `Ontology namespace must be an absolute IRI`), so `OMClient.create_ontology` sends
  the model base IRI as both. Duplicate name -> 409 `Entity already exists`.
- Lookup by name: `GET /v1/ontologies/name/{fqn}` (404 when absent). State:
  `GET /v1/ontologies/{id}/state` -> `workingRevision` (1 on creation, +1 per applied
  definition: a class and a property took it from 1 to 3).
- Members of the working state: `GET /v1/ontologyConcepts|ontologyProperties|ontologyAxioms
  ?ontology=<name>&limit=&after=&fields=*` (paged: `paging.after`). A property's `name` is the
  native `{Concept}.{attr}`; its FQN is `<ontology>."{Concept}.{attr}"`.
- **There is no RDF export of the working state.** `GET /v1/ontologyReleases/{id}/export?format=`
  exports a *sealed release* (`owl:Class`, `owl:DatatypeProperty` with `rdfs:domain`/`range`,
  plus `owl:Axiom` annotation blobs), and sealing (`POST /v1/ontologies/{id}/releases`, needs
  `expectedWorkingRevision` and `expectedLatestReleaseRevision`) is a write that cannot be
  undone: **an ontology with a release cannot be hard-deleted** (400 `has retained releases;
  archive it instead`; a soft delete works). `GET /v1/rdf/ontology` is OM's own schema
  ontology, `.../context` is Markdown, `.../working/diff` lists changed members as JSON.
  The read path therefore rebuilds RDF from the working members (`lakeloom.om.native_rdf`):
  `owl:Class` per `OWL_CLASS`/`BOTH` concept, the property kind as `owl:DatatypeProperty` /
  `ObjectProperty` / `AnnotationProperty` with `rdfs:domain`, `rdfs:range`, `rdfs:label`
  (display name if set) and `rdfs:comment`, `owl:oneOf` for `enumValues`, `qudt:unit`,
  `owl:FunctionalProperty` for identifiers, and `SUBCLASS_OF` axioms as `rdfs:subClassOf`
  (named class or cardinality restriction). Lakeloom never seals releases.
- **Read path.** `ModelService.graph(slug)` parses the glossary export and, if an ontology
  named `slug` exists, the native export, into one rdflib graph, then the existing canonical
  serialiser. A missing pair is a debug log, not an error. The cache entry carries a
  `version` (`g<glossary updatedAt>-n<workingRevision or ->`), exposed as
  `ModelService.version(slug)`, so a validator changes when either side changes; the
  `workingRevision` is read before and after the members and the read repeated (3 attempts)
  if it moved. Native classes are `owl:Class`, glossary concepts `skos:Concept`: the concept
  index sees each concept once, and `explain_concept` finds attributes as
  `owl:DatatypeProperty` + `rdfs:domain` of the concept IRI.
- **Conflict kinds.** `OMConflict.kind` is `exists` (409 already exists), `lease` (other
  409), `stale_base` (400 expected version) or `stale_revision` (412). The lease release
  runs shielded from cancellation.
- Applying is test/dev only (seed, contract test): the bot token can apply its own drafts, so
  M2b must enforce human-only apply in Lakeloom (FR-REV-01/02).
