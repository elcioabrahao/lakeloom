"""Mapping YAML for the upstream MCP adapter (FR-SRC-05)."""

from pathlib import Path

import pytest

from lakeloom.config import ConfigError
from lakeloom.errors import LakeloomError
from lakeloom.sources.mcp_mapping import (
    McpMapping,
    evaluate,
    load_mapping,
    parse_mapping,
    render_arguments,
)

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "examples" / "mcp-mappings"

MINIMAL = {
    "version": 1,
    "operations": {
        "list_tables": {
            "tool": "search_tables",
            "arguments": {"schema": "{schema}", "cursor": "{cursor}"},
            "result": {"items": "tables", "fields": {"name": "name"}},
            "pagination": {"cursor": "next"},
        },
        "describe_table": {
            "tool": "get_table",
            "arguments": {"full_name": "{full_name}"},
            "result": {"fields": {"columns": "columns"}},
        },
    },
}


def test_fr_src_05_render_arguments() -> None:
    template = {
        "fqn": "{full_name}",
        "filter": '{"term": {"schema": "{schema}"}}',
        "nested": [{"name": "{name}"}, 5, True],
        "size": 50,
        "cursor": "{cursor}",
    }
    values = {"full_name": "c.s.t", "schema": "sales", "name": "t", "cursor": None}
    rendered = render_arguments(template, values)
    assert rendered == {
        "fqn": "c.s.t",
        "filter": '{"term": {"schema": "sales"}}',
        "nested": [{"name": "t"}, 5, True],
        "size": 50,
    }  # whole-placeholder with no value (first page cursor) is omitted
    assert render_arguments({"cursor": "{cursor}"}, {"cursor": "abc"}) == {"cursor": "abc"}


def test_fr_src_05_render_arguments_unknown_placeholder() -> None:
    with pytest.raises(ConfigError, match=r"unknown placeholder \{table\}"):
        render_arguments({"x": "{table}"}, {"name": "t"})


def test_fr_src_05_render_arguments_rejects_breakout_in_embedded_value() -> None:
    # An agent-supplied schema must not escape a JSON string in the template.
    template = {"filter": '{"term": {"schema": "{schema}"}}'}
    with pytest.raises(LakeloomError) as info:
        render_arguments(template, {"schema": 'x"}},{"match_all":{}'})
    assert info.value.code == "validation_error"
    # Whole-placeholder values are passed as data, unchanged.
    assert render_arguments({"s": "{schema}"}, {"schema": 'a"b'}) == {"s": 'a"b'}


def test_fr_src_05_jmespath_fields() -> None:
    data = {"results": [{"name": "a", "cols": [{"n": "x"}]}, {"name": "b", "cols": []}]}
    assert evaluate("results[].name", data) == ["a", "b"]
    assert evaluate("results[0].cols[].{name: n}", data) == [{"name": "x"}]
    assert evaluate("missing", data) is None


def test_fr_src_05_mapping_yaml_validation_errors(tmp_path: Path) -> None:
    mapping = parse_mapping(MINIMAL, where="t")
    assert set(mapping.operations) == {"list_tables", "describe_table"}

    def bad(change: dict[str, object], match: str) -> None:
        raw = {**MINIMAL, **change}
        with pytest.raises(ConfigError, match=match):
            parse_mapping(raw, where="t")

    bad({"version": 2}, "version")
    bad({"operations": {"drop_table": MINIMAL["operations"]["describe_table"]}}, "drop_table")
    bad(
        {
            "operations": {
                "list_tables": {
                    **MINIMAL["operations"]["list_tables"],
                    "result": {"items": "tables[", "fields": {"name": "name"}},
                }
            }
        },
        "JMESPath",
    )
    bad(
        {
            "operations": {
                "list_catalogs": {
                    "tool": "t",
                    "arguments": {"x": "{schema}"},
                    "result": {"items": "a", "fields": {"name": "n"}},
                }
            }
        },
        r"\{schema\}",
    )
    bad(
        {"operations": {"describe_table": {"tool": "t", "result": {"fields": {"bogus": "x"}}}}},
        "bogus",
    )
    bad(
        {"operations": {"describe_table": {"tool": "t", "result": {"fields": {"comment": "x"}}}}},
        "columns",
    )
    bad(
        {
            "operations": {
                "list_tables": {
                    "tool": "t",
                    "arguments": {"c": "{cursor}"},
                    "result": {"items": "a", "fields": {"name": "n"}},
                }
            }
        },
        "pagination",
    )
    bad(
        {"operations": {"list_tables": {"tool": "t", "result": {"fields": {"name": "n"}}}}}, "items"
    )

    describe = {"tool": "t", "result": {"fields": {"columns": "c"}}}
    bad(
        {"operations": {"describe_table": {**describe, "arguments": {"o": "{column_offset}"}}}},
        "column_pagination",
    )
    bad(
        {"operations": {"describe_table": {**describe, "column_pagination": {"more": "m"}}}},
        "column_offset",
    )
    assert render_arguments(
        {"o": "{column_offset}", "s": "p{column_offset}"}, {"column_offset": 3}
    ) == {"o": 3, "s": "p3"}

    path = tmp_path / "m.yaml"
    path.write_text("version: 1\noperations: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_mapping(path)
    with pytest.raises(ConfigError, match="cannot read"):
        load_mapping(tmp_path / "missing.yaml")


@pytest.mark.parametrize("name", ["databricks.example.yaml", "snowflake.example.yaml"])
def test_fr_src_05_example_mappings_parse(name: str) -> None:
    path = EXAMPLES / name
    assert path.read_text(encoding="utf-8").startswith(
        "# EXAMPLE TO VERIFY — tool names are placeholders; run tools/list against your "
        "server and edit"
    )
    mapping = load_mapping(path)
    assert isinstance(mapping, McpMapping)
    assert {op.tool for op in mapping.operations.values()} == {"<tool-from-tools/list>"}


def test_fr_src_05_openmetadata_mapping_parses() -> None:
    mapping = load_mapping(EXAMPLES / "openmetadata.yaml")
    assert {"list_tables", "describe_table"} <= set(mapping.operations)
