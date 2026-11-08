"""Pydantic models for the OpenMetadata responses used in M0 (FR-OM-01, FR-OM-06).

``OMVersion`` follows ``components.schemas.OpenMetadataServerVersion`` in the committed
spec. The spec declares ``/v1/rdf/status`` with an untyped body, so ``RdfStatus`` is
modelled from the live 2.1.0-rc1 response and tolerates extra fields.
"""

from pydantic import BaseModel, ConfigDict, Field


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
