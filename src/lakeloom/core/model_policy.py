"""Per-model mapping policy for the deterministic mapper (FR-PRO-01).

Operators tune the mapping of a model through the ``models:`` section of the YAML file at
``LAKELOOM_CONFIG_FILE``::

    models:
      - name: retail
        mapping:
          low_cardinality: 30
          enum_as_children: false
          concept_naming: singular_pascal
          attribute_naming: camel
          datatype_map: {money: xsd:decimal}   # merged over DEFAULT_DATATYPES

        access:
          propose_groups: [stewards]   # who may create or extend proposals (NFR-SEC-07)
          review_groups: [reviewers]   # who may review them

A model without an entry gets the defaults. Unknown keys are rejected. An empty (or absent)
group list means no restriction; the CLI operator (:class:`~lakeloom.core.callers.Actor`) is
exempt. Otherwise one of the caller's ``groups`` must be listed (:func:`require_group`).
"""

import re
from collections.abc import Sequence
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lakeloom.core.callers import Actor, Caller
from lakeloom.errors import LakeloomError

__all__ = [
    "DEFAULT_DATATYPES",
    "XSD_DATATYPE_PATTERN",
    "AccessAction",
    "MappingPolicy",
    "ModelAccess",
    "ModelPolicyEntry",
    "access_for",
    "may",
    "policy_for",
    "require_group",
]

AccessAction = Literal["propose", "review"]
GroupName = Annotated[str, Field(min_length=1, max_length=255)]

# Keys are lower-case Spark base types (parameters such as ``(10,2)`` are stripped).
DEFAULT_DATATYPES: dict[str, str] = {
    "string": "xsd:string",
    "varchar": "xsd:string",
    "char": "xsd:string",
    "bigint": "xsd:long",
    "long": "xsd:long",
    "int": "xsd:int",
    "integer": "xsd:int",
    "smallint": "xsd:short",
    "short": "xsd:short",
    "tinyint": "xsd:byte",
    "byte": "xsd:byte",
    "decimal": "xsd:decimal",
    "numeric": "xsd:decimal",
    "double": "xsd:double",
    "float": "xsd:float",
    "boolean": "xsd:boolean",
    "date": "xsd:date",
    "timestamp": "xsd:dateTime",
    "timestamp_ntz": "xsd:dateTime",
    "binary": "xsd:base64Binary",
}

# Single source of truth for xsd datatype names (also used by AddAttribute).
XSD_DATATYPE_PATTERN = r"xsd:[A-Za-z][A-Za-z0-9]*"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MappingPolicy(_Strict):
    model: str
    low_cardinality: int = Field(default=20, ge=1, le=100)
    enum_as_children: bool = False
    concept_naming: Literal["singular_pascal", "pascal", "as_is"] = "singular_pascal"
    attribute_naming: Literal["as_is", "camel"] = "as_is"
    datatype_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_DATATYPES))

    @field_validator("datatype_map")
    @classmethod
    def _xsd_values(cls, value: dict[str, str]) -> dict[str, str]:
        for key, mapped in value.items():
            if not _is_xsd(mapped):
                raise ValueError(f"datatype_map[{key!r}] must look like 'xsd:name', got {mapped!r}")
        return value


class _MappingSettings(_Strict):
    low_cardinality: int = Field(default=20, ge=1, le=100)
    enum_as_children: bool = False
    concept_naming: Literal["singular_pascal", "pascal", "as_is"] = "singular_pascal"
    attribute_naming: Literal["as_is", "camel"] = "as_is"
    datatype_map: dict[str, str] = {}

    @field_validator("datatype_map")
    @classmethod
    def _xsd_values(cls, value: dict[str, str]) -> dict[str, str]:
        for key, mapped in value.items():
            if not _is_xsd(mapped):
                raise ValueError(f"datatype_map[{key!r}] must look like 'xsd:name', got {mapped!r}")
        return value


class ModelAccess(_Strict):
    """Per-model access groups (NFR-SEC-07); an empty list means no restriction."""

    propose_groups: list[GroupName] = []
    review_groups: list[GroupName] = []


class ModelPolicyEntry(_Strict):
    """One ``models:`` item: ``{name, mapping: {...}, access: {...}}``."""

    name: str = Field(min_length=1)
    mapping: _MappingSettings = _MappingSettings()
    access: ModelAccess = ModelAccess()

    def to_policy(self) -> MappingPolicy:
        settings = self.mapping.model_dump()
        merged = dict(DEFAULT_DATATYPES)
        for key, value in self.mapping.datatype_map.items():
            merged[key.strip().lower()] = value
        settings["datatype_map"] = merged
        return MappingPolicy(model=self.name, **settings)


def _is_xsd(value: str) -> bool:
    return re.fullmatch(XSD_DATATYPE_PATTERN, value) is not None


def policy_for(entries: Sequence[ModelPolicyEntry], model: str) -> MappingPolicy:
    """The policy configured for ``model``, or the defaults."""
    for entry in entries:
        if entry.name == model:
            return entry.to_policy()
    return MappingPolicy(model=model)


def access_for(entries: Sequence[ModelPolicyEntry], model: str) -> ModelAccess:
    """The access groups configured for ``model``, or none (no restriction)."""
    for entry in entries:
        if entry.name == model:
            return entry.access
    return ModelAccess()


def _groups(access: ModelAccess, action: AccessAction) -> list[str]:
    return access.propose_groups if action == "propose" else access.review_groups


def may(principal: Caller, access: ModelAccess, action: AccessAction) -> bool:
    """Whether ``principal`` may ``action`` on a model with ``access`` (never raises)."""
    allowed = _groups(access, action)
    if not allowed or isinstance(principal, Actor):
        return True
    groups: object = getattr(principal, "groups", frozenset[str]())
    if not isinstance(groups, frozenset | set):
        return False
    return not set(allowed).isdisjoint(cast("frozenset[str]", groups))


def require_group(
    principal: Caller, access: ModelAccess, action: AccessAction, *, model: str | None = None
) -> None:
    """``forbidden`` unless :func:`may` allows it."""
    if may(principal, access, action):
        return
    which = "this model" if model is None else f"model {model!r}"
    raise LakeloomError(
        "forbidden",
        f"{which} restricts {action} to groups {', '.join(sorted(_groups(access, action)))}",
        "ask an operator to add you to one of these groups (models[].access in the "
        "Lakeloom config), or work on a model open to you",
    )
