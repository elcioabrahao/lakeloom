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

A model without an entry gets the defaults. Unknown keys are rejected.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["DEFAULT_DATATYPES", "MappingPolicy", "ModelPolicyEntry", "policy_for"]

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

_XSD = r"xsd:[A-Za-z][A-Za-z0-9]*"


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


class ModelPolicyEntry(_Strict):
    """One ``models:`` item: ``{name, mapping: {...}}``."""

    name: str = Field(min_length=1)
    mapping: _MappingSettings = _MappingSettings()

    def to_policy(self) -> MappingPolicy:
        settings = self.mapping.model_dump()
        merged = dict(DEFAULT_DATATYPES)
        for key, value in self.mapping.datatype_map.items():
            merged[key.strip().lower()] = value
        settings["datatype_map"] = merged
        return MappingPolicy(model=self.name, **settings)


def _is_xsd(value: str) -> bool:
    return re.fullmatch(_XSD, value) is not None


def policy_for(entries: list[ModelPolicyEntry], model: str) -> MappingPolicy:
    """The policy configured for ``model``, or the defaults."""
    for entry in entries:
        if entry.name == model:
            return entry.to_policy()
    return MappingPolicy(model=model)
