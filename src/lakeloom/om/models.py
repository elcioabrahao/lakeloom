"""Pydantic models for the OpenMetadata responses used in M0 (FR-OM-01, FR-OM-06).

``OMVersion`` follows ``components.schemas.OpenMetadataServerVersion`` in the committed
spec. The spec declares ``/v1/rdf/status`` with an untyped body, so ``RdfStatus`` is
modelled from the live 2.1.0-rc1 response and tolerates extra fields.
"""

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OMVersion(BaseModel):
    """Response of ``GET /v1/system/version``."""

    model_config = ConfigDict(extra="ignore")

    version: str
    revision: str | None = None
    timestamp: int | None = None


class RdfStatus(BaseModel):
    """Response of ``GET /v1/rdf/status`` (camelCase on the wire)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    enabled: bool
    base_uri: str | None = Field(default=None, alias="baseUri")
    storage_type: str | None = Field(default=None, alias="storageType")
    projection_state: str | None = Field(default=None, alias="projectionState")
    ai_enabled: bool | None = Field(default=None, alias="aiEnabled")


class OMCapabilities(BaseModel):
    """What this OpenMetadata instance offers; produced without ever raising."""

    reachable: bool
    version: str | None = None
    rdf_enabled: bool | None = None
    rdf: RdfStatus | None = None
    error: str | None = None


class OMTagLabel(BaseModel):
    """A tag applied to an entity or column (``TagLabel``)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tag_fqn: str = Field(alias="tagFQN")
    source: str | None = None
    label_type: str | None = Field(default=None, alias="labelType")


class OMEntityRef(BaseModel):
    """An ``EntityReference`` (owner, domain, ...)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    type: str | None = None
    name: str | None = None
    fully_qualified_name: str | None = Field(default=None, alias="fullyQualifiedName")


class OMColumn(BaseModel):
    """A table column as returned by ``GET /v1/tables/name/{fqn}?fields=columns``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    data_type: str | None = Field(default=None, alias="dataType")
    description: str | None = None
    tags: list[OMTagLabel] = []


class OMTable(BaseModel):
    """A table entity (FR-SRC-07)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    fully_qualified_name: str = Field(alias="fullyQualifiedName")
    description: str | None = None
    columns: list[OMColumn] = []
    tags: list[OMTagLabel] = []
    owners: list[OMEntityRef] = []
    domains: list[OMEntityRef] = []
    updated_at: int | None = Field(default=None, alias="updatedAt")  # epoch milliseconds


class OMPage[T](BaseModel):
    """A page of an OM list response (``{"data": [...], "paging": {...}}``)."""

    model_config = ConfigDict(extra="ignore")

    data: list[T]
    after: str | None = None
    total: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _lift_paging(cls, value: object) -> object:
        if isinstance(value, dict) and "paging" in value:
            body = cast("dict[str, Any]", value)
            paging = cast("dict[str, Any]", body["paging"] or {})
            return {
                "data": body.get("data", []),
                "after": paging.get("after"),
                "total": paging.get("total"),
            }
        return cast("object", value)


class OMNamespace(BaseModel):
    """An ``OntologyNamespace``: a prefix and the namespace IRI it stands for."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    prefix: str | None = None
    namespace: str | None = None


class OMGlossary(BaseModel):
    """A glossary, i.e. an ontology model (ADR 0007)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    fully_qualified_name: str = Field(alias="fullyQualifiedName")
    display_name: str | None = Field(default=None, alias="displayName")
    description: str | None = None
    namespaces: list[OMNamespace] = []
    term_count: int | None = Field(default=None, alias="termCount")
    updated_at: int | None = Field(default=None, alias="updatedAt")  # epoch milliseconds


class OMTermRelation(BaseModel):
    """A ``TermRelation``: a typed edge from a term to another term."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    term: OMEntityRef
    relation_type: str | None = Field(default=None, alias="relationType")
    status: str | None = None
    provenance: str | None = None


class OMConceptMapping(BaseModel):
    """A ``ConceptMapping`` to an external concept (SKOS match types)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    concept_iri: str = Field(alias="conceptIri")
    mapping_type: str = Field(alias="mappingType")
    scheme_iri: str | None = Field(default=None, alias="schemeIri")
    source: str | None = None


class OMGlossaryTerm(BaseModel):
    """A glossary term, i.e. a concept."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    fully_qualified_name: str = Field(alias="fullyQualifiedName")
    display_name: str | None = Field(default=None, alias="displayName")
    description: str | None = None
    iri: str | None = None
    synonyms: list[str] = []
    parent: OMEntityRef | None = None
    related_terms: list[OMTermRelation] = Field(default=[], alias="relatedTerms")
    concept_mappings: list[OMConceptMapping] = Field(default=[], alias="conceptMappings")
    glossary: OMEntityRef
    tags: list[OMTagLabel] = []
    owners: list[OMEntityRef] = []


class OMLineageEdge(BaseModel):
    """An ``Edge`` of the lineage graph; endpoints are ids or references."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    from_entity: str | OMEntityRef | None = Field(default=None, alias="fromEntity")
    to_entity: str | OMEntityRef | None = Field(default=None, alias="toEntity")


def _as_list(value: object) -> list[object]:
    if isinstance(value, dict):
        return list(cast("dict[str, object]", value).values())
    if isinstance(value, list):
        return list(cast("list[object]", value))
    return []


def _unwrap_node(node: object) -> object:
    if isinstance(node, dict) and "entity" in node:
        return cast("dict[str, object]", node)["entity"]
    return cast("object", node)


class OMLineage(BaseModel):
    """Lineage around an entity.

    The live server keys ``nodes`` and the edge sets by FQN or id, each node wrapping the
    entity as ``{"entity": {...}, "nodeDepth": n}``; both are flattened to lists here.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    nodes: list[OMEntityRef] = []
    upstream_edges: list[OMLineageEdge] = Field(default=[], alias="upstreamEdges")
    downstream_edges: list[OMLineageEdge] = Field(default=[], alias="downstreamEdges")

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        out = dict(cast("dict[str, object]", value))
        out["nodes"] = [_unwrap_node(n) for n in _as_list(out.get("nodes"))]
        for key in ("upstreamEdges", "downstreamEdges"):
            out[key] = _as_list(out.get(key))
        return out


# --- Change sets (M2a, FR-OM-02; ADR 0011) -------------------------------------------------

ChangeSetState = Literal["DRAFT", "SUBMITTED", "APPLIED", "APPLY_FAILED", "DISCARDED"]
"""``OntologyChangeSet.state`` as rc1 sends it (the docs write "Draft", "Apply Failed", ...)."""


class OMChangeOperation(BaseModel):
    """An ``OntologyChangeOperation``. Payload objects are kept as raw JSON (ADR 0011)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    operation_type: str = Field(alias="operationType")
    state: str | None = None  # ACTIVE | UNDONE
    base_version: float | None = Field(default=None, alias="baseVersion")
    target_id: str | None = Field(default=None, alias="targetId")
    term: dict[str, Any] | None = None
    concept: dict[str, Any] | None = None
    property: dict[str, Any] | None = None
    relationship: dict[str, Any] | None = None
    mapping: dict[str, Any] | None = None
    axiom: dict[str, Any] | None = None
    glossary: dict[str, Any] | None = None
    rationale: str | None = None
    confidence: float | None = None


class OMChangeOperationResult(BaseModel):
    """One ``OntologyChangeOperationResult`` of an apply."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    operation_id: str = Field(alias="operationId")
    status: str
    entity: OMEntityRef | None = None
    entity_version: float | None = Field(default=None, alias="entityVersion")
    message: str | None = None


class OMChangeApplicationResult(BaseModel):
    """``OntologyChangeApplicationResult``: what an apply did."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    applied_at: int | None = Field(default=None, alias="appliedAt")
    applied_by: str | None = Field(default=None, alias="appliedBy")
    operations_processed: int | None = Field(default=None, alias="operationsProcessed")
    operations_applied: int | None = Field(default=None, alias="operationsApplied")
    operations_failed: int | None = Field(default=None, alias="operationsFailed")
    results: list[OMChangeOperationResult] = []


class OMChangeSet(BaseModel):
    """An ``OntologyChangeSet`` (Draft -> Submitted -> Applied / Apply failed / Discarded)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    fully_qualified_name: str | None = Field(default=None, alias="fullyQualifiedName")
    display_name: str | None = Field(default=None, alias="displayName")
    description: str | None = None
    state: ChangeSetState | str
    operations: list[OMChangeOperation] = []
    undo_cursor: int | None = Field(default=None, alias="undoCursor")
    glossaries: list[OMEntityRef] = []
    ontology: OMEntityRef | None = None
    base_working_revision: int | None = Field(default=None, alias="baseWorkingRevision")
    application_result: OMChangeApplicationResult | None = Field(
        default=None, alias="applicationResult"
    )
    version: float | None = None
    updated_at: int | None = Field(default=None, alias="updatedAt")  # epoch milliseconds


class OMEditLease(BaseModel):
    """An ``OntologyEditLock``: the authoring lease submit/discard/operations require."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    resource_type: str = Field(alias="resourceType")
    resource_id: str = Field(alias="resourceId")
    session_id: str = Field(alias="sessionId")
    version: int
    expires_at: int | None = Field(default=None, alias="expiresAt")

    def token(self) -> dict[str, object]:
        """The ``OntologyEditLeaseToken`` to send with a command."""
        return {"sessionId": self.session_id, "version": self.version}


class OMAxiomViolation(BaseModel):
    """An ``OntologyProfileViolation``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    severity: str
    code: str
    path: str | None = None
    message: str


class OMAxiomValidation(BaseModel):
    """``OntologyProfileReport`` of ``POST /v1/ontologyAxioms/validate``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    valid: bool
    violations: list[OMAxiomViolation] = []

    @property
    def errors(self) -> list[str]:
        """Messages of the ERROR-severity violations."""
        return [v.message for v in self.violations if v.severity == "ERROR"]
