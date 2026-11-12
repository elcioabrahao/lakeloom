"""Pydantic models for the OpenMetadata responses used in M0 (FR-OM-01, FR-OM-06).

``OMVersion`` follows ``components.schemas.OpenMetadataServerVersion`` in the committed
spec. The spec declares ``/v1/rdf/status`` with an untyped body, so ``RdfStatus`` is
modelled from the live 2.1.0-rc1 response and tolerates extra fields.
"""

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdflib import Graph


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
    version: float | None = None  # the baseVersion of operations on this term


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


class OMOperationResult(BaseModel):
    """One ``OntologyChangeOperationResult`` of an apply.

    OM nests the touched entity as an ``EntityReference``; its id and FQN are lifted to
    ``entity_id``/``entity_fqn``. A ``CREATE_TERM`` keeps its draft id (ADR 0012).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    operation_id: str = Field(alias="operationId")
    status: str  # IN_PROGRESS | APPLIED | ROLLED_BACK | SKIPPED | FAILED
    entity_id: str | None = Field(default=None, alias="entityId")
    entity_fqn: str | None = Field(default=None, alias="entityFqn")
    entity_type: str | None = Field(default=None, alias="entityType")
    entity_version: float | None = Field(default=None, alias="entityVersion")
    message: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _lift_entity(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        body = dict(cast("dict[str, Any]", value))
        if isinstance(body.get("entity"), dict):
            entity = cast("dict[str, Any]", body.pop("entity"))
            body.setdefault("entityId", entity.get("id"))
            body.setdefault("entityFqn", entity.get("fullyQualifiedName"))
            body.setdefault("entityType", entity.get("type"))
        return body


class OMApplicationResult(BaseModel):
    """``OntologyChangeApplicationResult``: what an apply did and who applied it."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    applied_at: int | None = Field(default=None, alias="appliedAt")
    applied_by: str | None = Field(default=None, alias="appliedBy")
    operations_processed: int = Field(default=0, alias="operationsProcessed")
    operations_applied: int = Field(default=0, alias="operationsApplied")
    operations_failed: int = Field(default=0, alias="operationsFailed")
    results: list[OMOperationResult] = []


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
    application_result: OMApplicationResult | None = Field(default=None, alias="applicationResult")
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


# --- Native ontologies (M2a Task 5b, Amendment A; ADR 0011) --------------------------------


class OMOntology(BaseModel):
    """A native OM ontology (``POST /v1/ontologies``), the pair of a glossary."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    fully_qualified_name: str | None = Field(default=None, alias="fullyQualifiedName")
    description: str | None = None
    iri: str | None = None
    namespace: str | None = None
    version: float | None = None


class OMOntologyState(BaseModel):
    """``GET /v1/ontologies/{id}/state``: ``workingRevision`` grows per applied definition."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    working_revision: int = Field(alias="workingRevision")
    last_release_revision: int = Field(default=0, alias="lastReleaseRevision")
    activation_revision: int = Field(default=0, alias="activationRevision")


class OMNativeExport(BaseModel):
    """A native ontology's working state at one ``working_revision``, as an rdflib ``graph``
    (what the client builds) or as ``turtle`` text (parsed by the reader when no graph)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    working_revision: int
    turtle: str = ""
    graph: Graph | None = None


class OMRelationshipType(BaseModel):
    """A ``RelationshipType`` (``GET /v1/relationshipTypes``): referenced by id in
    ``ADD_RELATIONSHIP`` (ADR 0011)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str


# --- Identity and permissions (M2b Task 2; ADR 0012 § Apply research) ----------------------


class OMUser(BaseModel):
    """``GET /v1/users/loggedInUser``: the OM user a token acts as."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    email: str | None = None
    is_admin: bool | None = Field(default=None, alias="isAdmin")
    is_bot: bool | None = Field(default=None, alias="isBot")


PermissionAccess = Literal["allow", "deny", "notAllow", "conditionalAllow", "conditionalDeny"]
"""``Permission.access`` in the spec. Only ``allow`` is an unconditional yes."""

PermissionResource = Literal["glossary", "ontology"]
"""OM entity types Lakeloom asks permissions for: a model's glossary and its native ontology."""

APPLY_PERMISSIONS: dict[PermissionResource, tuple[str, ...]] = {
    "glossary": ("EditAll",),
    "ontology": ("EditAll",),
}
"""Operations the caller needs on the target to apply a change set (live rc1, ADR 0012).

Applying a glossary change set needs ``EditAll`` on the glossary; a native one ``EditAll`` on
the ontology. Both also need ``EditAll`` on ``ontologyChangeSet`` (the authoring lease) and a
native set needs ``Create``/``Delete`` on ``ontologyConcept``/``ontologyProperty``/
``ontologyAxiom``; OM checks those at apply time only, so they are not pre-checked here.
"""


class OMPermissions(BaseModel):
    """``ResourcePermission`` of ``GET /v1/permissions/{resource}/{id}`` for the caller:
    ``operations`` maps an OM operation name to its access."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    resource: str | None = None
    operations: dict[str, PermissionAccess | str] = {}

    @model_validator(mode="before")
    @classmethod
    def _index(cls, value: object) -> object:
        if isinstance(value, dict) and "permissions" in value:
            body = cast("dict[str, Any]", value)
            entries = body["permissions"]
            ops: dict[str, str] = {}
            for entry in cast("list[object]", entries if isinstance(entries, list) else []):
                if isinstance(entry, dict):
                    item = cast("dict[str, Any]", entry)
                    operation, access = item.get("operation"), item.get("access")
                    if isinstance(operation, str) and isinstance(access, str):
                        ops[operation] = access
            return {"resource": body.get("resource"), "operations": ops}
        return cast("object", value)

    def allows(self, *operations: str) -> bool:
        """``True`` when every operation is ``allow`` (unknown or conditional ones are not)."""
        return all(self.operations.get(op) == "allow" for op in operations)
