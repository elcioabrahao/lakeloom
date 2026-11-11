# 0007. An ontology model is an OpenMetadata glossary

- Status: accepted; partly superseded by ADR 0011 (hybrid model)
- Date: 2026-11-08
- Requirements: FR-OM-*, FR-ONT-*

## Context

Lakeloom needs a container in OpenMetadata for one ontology. OpenMetadata 2.1 documentation
states "One glossary = one ontology model". A `/api/v1/ontologies` endpoint family appears
in release-candidate material but is not documented and is absent from the rc1 spec we pin
(see ADR 0003).

## Decision

An ontology model maps to one OM glossary; its terms are the classes and concepts. Lakeloom
does not use the undocumented `/api/v1/ontologies` endpoints. No ontology code ships in M1a;
this decision guides M1b.

## Consequences

We rely only on documented, spec-tested endpoints. Revisit when OpenMetadata 2.1 reaches GA
and the ontologies API is documented and stable.

## Update (2026-11-11)

ADR 0011 supersedes the sentence "Lakeloom does not use the undocumented `/api/v1/ontologies`
endpoints": rc1 accepts attributes and axioms only in a native ontology, so a model is the
glossary **plus a paired native ontology** of the same name. The glossary stays the home of
concepts, relations and mappings; the native ontology holds same-IRI classes, attributes and
axioms.
