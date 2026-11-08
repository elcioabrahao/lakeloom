"""Operator-managed YAML source configuration (FR-SRC-01, NFR-SEC-13).

Agents never supply sources or URLs. Credentials are referenced by environment-variable
*name* (``token_env``) and resolved when the source is built; they are never stored here.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    model_validator,
)

from lakeloom.config import ConfigError

__all__ = [
    "DeltaSourceConfig",
    "DeltaTableEntry",
    "McpSourceConfig",
    "PluginSourceConfig",
    "SourceConfig",
    "SourcesConfig",
    "UnityCatalogSourceConfig",
    "load_sources_config",
]

_BUILTIN_TYPES = ("delta", "unity_catalog", "mcp")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DeltaTableEntry(_Base):
    uri: str
    catalog: str | None = None
    schema_: str = Field(alias="schema")
    name: str | None = None
    storage_options: dict[str, str] = {}
    # Operator overlay (ADR 0008): takes precedence over conventions in the Delta log.
    description: str | None = None
    tags: list[str] | None = None
    owners: list[str] | None = None
    primary_key: list[str] | None = None
    foreign_keys: dict[str, str] = {}
    column_tags: dict[str, list[str]] = {}
    column_comments: dict[str, str] = {}

    @model_validator(mode="after")
    def _default_name(self) -> "DeltaTableEntry":
        if self.name is None:
            self.name = self.uri.rstrip("/").rsplit("/", 1)[-1]
        return self


class DeltaSourceConfig(_Base):
    type: Literal["delta"]
    name: str
    tables: list[DeltaTableEntry]
    om_service: str | None = None  # OpenMetadata service name for FQN lookup (FR-SRC-07)


class UnityCatalogSourceConfig(_Base):
    type: Literal["unity_catalog"]
    name: str
    url: AnyHttpUrl
    token_env: str | None = None
    catalogs: list[str] | None = None
    read_storage: bool = False
    om_service: str | None = None


class McpSourceConfig(_Base):
    type: Literal["mcp"]
    name: str
    transport: Literal["streamable_http", "stdio"]
    url: AnyHttpUrl | None = None
    command: list[str] | None = None
    token_env: str | None = None
    mapping: Path
    om_service: str | None = None


class PluginSourceConfig(BaseModel):
    """Any other ``type``: resolved through the ``lakeloom.sources`` entry-point group."""

    model_config = ConfigDict(extra="forbid")

    type: str
    name: str
    options: dict[str, Any] = {}


def _tag(value: Any) -> str:
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    return kind if kind in _BUILTIN_TYPES else "plugin"  # pyright: ignore[reportUnknownVariableType]


SourceConfig = Annotated[
    Annotated[DeltaSourceConfig, Tag("delta")]
    | Annotated[UnityCatalogSourceConfig, Tag("unity_catalog")]
    | Annotated[McpSourceConfig, Tag("mcp")]
    | Annotated[PluginSourceConfig, Tag("plugin")],
    Discriminator(_tag),
]


class SourcesConfig(BaseModel):
    sources: list[SourceConfig] = []

    @model_validator(mode="after")
    def _unique_names(self) -> "SourcesConfig":
        seen: set[str] = set()
        for source in self.sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name: {source.name!r}")
            seen.add(source.name)
        return self


def load_sources_config(path: Path | None) -> SourcesConfig:
    """Load the YAML file; a missing path or file means no sources."""
    if path is None or not path.exists():
        return SourcesConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown position"
        raise ConfigError(f"invalid YAML in {path}: {exc.problem} at {where}") from None
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"cannot read sources config {path}: {exc}") from None
    if raw is None:
        return SourcesConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping with a 'sources' list")
    try:
        return SourcesConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"invalid sources config {path}: {problems}") from None
