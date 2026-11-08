"""The LakeSource protocol every connector implements (FR-SRC-01)."""

from typing import Protocol, Self

from pydantic import BaseModel

from lakeloom.config import Settings
from lakeloom.sources.models import (
    TableDescription,
    TableProfile,
    TableRef,
    TableVersion,
)

__all__ = ["LakeSource", "ProfilePolicy", "SourceCapabilities"]


class SourceCapabilities(BaseModel):
    """What a source can do; unsupported calls raise ``validation_error``."""

    profiling: bool
    versions: bool
    constraints: bool


class ProfilePolicy(BaseModel):
    """Privacy rules for profiling (FR-SRC-09)."""

    samples: bool
    sensitive_tags: list[str]
    top_k: int
    low_cardinality: int
    value_max_length: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            samples=settings.profile_samples,
            sensitive_tags=list(settings.sensitive_tags),
            top_k=settings.profile_topk,
            low_cardinality=settings.profile_low_cardinality,
            value_max_length=settings.profile_value_max_length,
        )


class LakeSource(Protocol):
    """A configured, read-only view of one lake. Operators configure sources (NFR-SEC-13)."""

    name: str
    kind: str
    capabilities: SourceCapabilities
    om_service: str | None  # OpenMetadata service name for FQN lookup (FR-SRC-07)

    async def list_catalogs(self) -> list[str]: ...

    async def list_schemas(self, catalog: str | None) -> list[str]: ...

    async def list_tables(self, catalog: str | None, schema: str) -> list[TableRef]: ...

    async def describe_table(self, ref: TableRef) -> TableDescription: ...

    async def get_table_version(self, ref: TableRef) -> TableVersion: ...

    async def profile_table(self, ref: TableRef, policy: ProfilePolicy) -> TableProfile:
        """Raises ``LakeloomError("validation_error", ...)`` when profiling is unsupported."""
        ...

    async def aclose(self) -> None: ...
