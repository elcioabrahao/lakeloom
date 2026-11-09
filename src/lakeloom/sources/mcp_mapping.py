"""Declarative mapping from upstream MCP tools to LakeSource operations (FR-SRC-05).

A mapping YAML binds each LakeSource operation to one upstream tool: the tool name (which
must appear in the upstream's ``tools/list``), an argument template, and JMESPath
expressions that pick the operation's fields out of the tool result. Example::

    version: 1
    operations:
      describe_table:
        tool: get_entity_details
        arguments: {entityType: table, fqn: "{om_service}.{full_name}"}
        result:
          fields: {comment: description, columns: "columns[].{name: name, ...}"}
        not_found: "statusCode == `404`"

Argument string leaves may contain the placeholders ``{catalog}``, ``{schema}``,
``{name}``, ``{full_name}``, ``{cursor}`` and ``{om_service}`` (which ones depends on the
operation; checked at load time). A leaf that is exactly one placeholder is replaced by the
raw value, or dropped when the value is absent (the first page has no ``{cursor}``). An
embedded placeholder is substituted textually, and its value must not contain ``"``, ``\\``
or control characters, so agent-supplied names cannot break out of a JSON string in the
template (for example an OpenSearch ``queryFilter``).
"""

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import jmespath
import jmespath.exceptions
import jmespath.parser
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lakeloom.config import ConfigError
from lakeloom.errors import LakeloomError

__all__ = [
    "OPERATIONS",
    "McpMapping",
    "OperationMapping",
    "OperationName",
    "evaluate",
    "load_mapping",
    "parse_mapping",
    "render_arguments",
]

OperationName = Literal[
    "list_catalogs",
    "list_schemas",
    "list_tables",
    "describe_table",
    "get_table_version",
    "profile_table",
]
OPERATIONS: tuple[OperationName, ...] = (
    "list_catalogs",
    "list_schemas",
    "list_tables",
    "describe_table",
    "get_table_version",
    "profile_table",
)
LIST_OPERATIONS = frozenset({"list_catalogs", "list_schemas", "list_tables"})

_TABLE = frozenset({"catalog", "schema", "name", "full_name", "om_service"})
PLACEHOLDERS: dict[str, frozenset[str]] = {
    "list_catalogs": frozenset({"om_service", "cursor"}),
    "list_schemas": frozenset({"catalog", "om_service", "cursor"}),
    "list_tables": frozenset({"catalog", "schema", "om_service", "cursor"}),
    "describe_table": _TABLE,
    "get_table_version": _TABLE,
    "profile_table": _TABLE,
}
FIELDS: dict[str, frozenset[str]] = {
    "list_catalogs": frozenset({"name"}),
    "list_schemas": frozenset({"name"}),
    "list_tables": frozenset({"catalog", "schema", "name"}),
    "describe_table": frozenset(
        {
            "comment",
            "columns",
            "partition_columns",
            "constraints",
            "tags",
            "owners",
            "properties",
            "storage_location",
            "version",
            "committed_at",
        }
    ),
    "get_table_version": frozenset({"version", "committed_at"}),
    "profile_table": frozenset({"row_count", "columns"}),
}
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "list_catalogs": frozenset({"name"}),
    "list_schemas": frozenset({"name"}),
    "list_tables": frozenset({"name"}),
    "describe_table": frozenset({"columns"}),
    "get_table_version": frozenset({"version"}),
    "profile_table": frozenset({"row_count", "columns"}),
}

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNSAFE_EMBEDDED = re.compile(r'["\\\x00-\x1f\x7f]')


@lru_cache(maxsize=512)
def _compile(expression: str) -> jmespath.parser.ParsedResult:
    return jmespath.compile(expression)


def evaluate(expression: str, data: object) -> object:
    """Evaluate a (load-time validated) JMESPath expression; missing paths give ``None``."""
    return cast("object", _compile(expression).search(data))


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_jmespath(value: str) -> str:
    try:
        _compile(value)
    except jmespath.exceptions.JMESPathError as exc:
        raise ValueError(f"invalid JMESPath {value!r}: {exc}") from None
    return value


class ResultMapping(_Base):
    items: str | None = None
    fields: dict[str, str]

    @field_validator("items")
    @classmethod
    def _items(cls, value: str | None) -> str | None:
        return None if value is None else _check_jmespath(value)

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: dict[str, str]) -> dict[str, str]:
        for expression in value.values():
            _check_jmespath(expression)
        return value


class PaginationMapping(_Base):
    cursor: str

    @field_validator("cursor")
    @classmethod
    def _cursor(cls, value: str) -> str:
        return _check_jmespath(value)


class OperationMapping(_Base):
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = {}
    result: ResultMapping
    pagination: PaginationMapping | None = None
    # Evaluated on an error result: truthy means the object does not exist (not_found).
    not_found: str | None = None

    @field_validator("not_found")
    @classmethod
    def _not_found(cls, value: str | None) -> str | None:
        return None if value is None else _check_jmespath(value)


class McpMapping(_Base):
    version: Literal[1]
    operations: dict[OperationName, OperationMapping]


def _placeholders(template: object) -> set[str]:
    if isinstance(template, str):
        return set(_PLACEHOLDER.findall(template))
    if isinstance(template, Mapping):
        found: set[str] = set()
        for value in cast("Mapping[str, object]", template).values():
            found |= _placeholders(value)
        return found
    if isinstance(template, list):
        found = set()
        for value in cast("list[object]", template):
            found |= _placeholders(value)
        return found
    return set()


def _check_operation(name: str, op: OperationMapping) -> list[str]:
    problems: list[str] = []
    allowed = PLACEHOLDERS[name] if op.pagination else PLACEHOLDERS[name] - {"cursor"}
    for placeholder in sorted(_placeholders(op.arguments) - allowed):
        if placeholder == "cursor":
            problems.append(f"{name}: {{cursor}} is used but no pagination is configured")
        else:
            problems.append(f"{name}: placeholder {{{placeholder}}} is not allowed here")
    if op.pagination is not None:
        if name not in LIST_OPERATIONS:
            problems.append(f"{name}: pagination is only allowed on list operations")
        elif "cursor" not in _placeholders(op.arguments):
            problems.append(f"{name}: pagination needs a {{cursor}} argument")
    unknown = sorted(set(op.result.fields) - FIELDS[name])
    if unknown:
        problems.append(f"{name}: unknown result fields {unknown}")
    missing = sorted(REQUIRED_FIELDS[name] - set(op.result.fields))
    if missing:
        problems.append(f"{name}: result fields {missing} are required")
    if name in LIST_OPERATIONS and op.result.items is None:
        problems.append(f"{name}: result.items is required for list operations")
    return problems


def parse_mapping(raw: object, *, where: str) -> McpMapping:
    """Validate a decoded mapping document; every problem is reported as ``ConfigError``."""
    try:
        mapping = McpMapping.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"invalid MCP mapping {where}: {problems}") from None
    issues = [p for name, op in mapping.operations.items() for p in _check_operation(name, op)]
    if issues:
        raise ConfigError(f"invalid MCP mapping {where}: {'; '.join(issues)}")
    return mapping


def load_mapping(path: Path) -> McpMapping:
    """Read and validate a mapping YAML file (validated at load time, Review Focus 5)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in MCP mapping {path}: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read MCP mapping {path}: {exc.strerror}") from None
    return parse_mapping(raw, where=str(path))


_ABSENT = object()


def _render(template: object, values: Mapping[str, str | None]) -> object:
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole:
            key = whole.group(1)
            if key not in values:
                raise ConfigError(f"unknown placeholder {{{key}}} in MCP mapping arguments")
            value = values[key]
            return _ABSENT if value is None else value

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                raise ConfigError(f"unknown placeholder {{{key}}} in MCP mapping arguments")
            value = values[key]
            if value is None:
                raise LakeloomError(
                    "validation_error",
                    f"this source needs a value for {key}",
                    hint=(
                        "set om_service in the source configuration"
                        if key == "om_service"
                        else f"pass {key} explicitly"
                    ),
                )
            if _UNSAFE_EMBEDDED.search(value):
                raise LakeloomError(
                    "validation_error",
                    f"{key} contains characters this source cannot accept (quote, backslash "
                    "or control character)",
                    hint="use the exact name returned by the list tools",
                )
            return value

        return _PLACEHOLDER.sub(substitute, template)
    if isinstance(template, Mapping):
        out: dict[str, object] = {}
        for key, value in cast("Mapping[str, object]", template).items():
            rendered = _render(value, values)
            if rendered is not _ABSENT:
                out[key] = rendered
        return out
    if isinstance(template, list):
        return [
            r for v in cast("list[object]", template) if (r := _render(v, values)) is not _ABSENT
        ]
    return template


def render_arguments(
    template: Mapping[str, Any], values: Mapping[str, str | None]
) -> dict[str, Any]:
    """Fill an argument template; unknown placeholder raises ``ConfigError``."""
    return cast("dict[str, Any]", _render(template, values))
