# 0010. Publication read surfaces (M1c): negotiation, validators, Links

- Status: accepted
- Date: 2026-11-10
- Requirements: FR-PUB-01, FR-PUB-02, FR-PUB-05, FR-PUB-07

## Context

M1c publishes the semantic core over HTTP (`/v1`) and as MCP resources. Both surfaces share
one publication layer (`lakeloom.publish`) so that a body, its `ETag`, `Last-Modified` and
`Link` are identical whichever way it is fetched. Releases and Git publishing do not exist
yet (M2/M3).

## Decision

- **Negotiation.** `negotiation.choose` implements RFC 9110 `Accept`: q-values, `*/*` and
  `type/*` ranges, the most specific matching range deciding a type's q (`text/turtle;q=0, */*`
  refuses only turtle). Ties keep the route's preference order; absent, empty or malformed
  headers select the route default and never fail; `None` means nothing offered is acceptable
  (406 with the supported list). Defaults per route: models and versions `text/turtle`;
  concepts and asset context `text/markdown`; lists `application/json`; `context.jsonld`
  `application/ld+json`.
- **ETag.** Strong, `"sha256:<hex of body>"`. `If-None-Match` handles `*`, comma lists and weak
  (`W/`) validators with weak comparison, and answers 304 with the same ETag. Bodies are
  deterministic (canonical RDF serialisation, sorted-key JSON), so equal content gives an
  equal ETag across processes and replicas.
- **Last-Modified.** The model's OM glossary `updatedAt` (concepts and the model context use it
  too) or the table's `updatedAt` (asset context), epoch milliseconds from OM truncated to
  whole seconds (HTTP resolution). OM does not always stamp the entity: when `updatedAt` is
  absent the time the model graph was loaded is used (`ModelService.loaded_at`, stable while
  the graph is cached); an asset context with neither uses the Unix epoch. The ETag, not
  `Last-Modified`, is the authoritative validator.
- **Link.** FR-PUB-07 asks for a link to the Git commit or tag an artefact came from. Git
  publishing starts in M2, so until then `latest` artefacts link to their OpenMetadata source:
  `<{om_url}/glossary/{slug}>; rel="via"` and, for asset context,
  `<{om_url}/table/{fqn}>; rel="via"` (FQN percent-encoded as one path segment). With no
  `om_url` configured the list is empty. M2/M3 replace or supplement these with commit/tag
  links.
- **Versions.** Releases arrive in M3. Every `version` parameter accepts only `latest` (or
  nothing); any other value is `not_found` with the hint "releases are published from M3; use
  version=latest".
- **Model context.** `context.jsonld` proper (FR-PUB-05) is M3. M1c serves the minimal
  `@context` already used by the asset JSON-LD (`@vocab` = the model base IRI, the model
  prefixes, `dcterms`, `skos`), built by the single function `minimal_jsonld_context`.
- **Concept names.** A plain name resolves when the best-ranked tier of matches (exact name
  1.0 > synonym 0.95 > local name 0.9) holds exactly one concept; otherwise a
  `validation_error` lists the candidates. The HTTP route and the resources rely on this.

## Consequences

- Adding Git (M2) changes only `Link`; consumers should treat `rel="via"` as opaque provenance.
- A `Last-Modified` based on load time can move when the graph is reloaded even if the content
  did not change; conditional requests stay correct because they validate on the ETag.
