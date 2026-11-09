from pathlib import Path

import pytest

from lakeloom.config import ConfigError, Settings
from lakeloom.sources.base import ProfilePolicy
from lakeloom.sources.config import (
    DeltaSourceConfig,
    McpSourceConfig,
    PluginSourceConfig,
    UnityCatalogSourceConfig,
    load_sources_config,
)

YAML = """
sources:
  - type: delta
    name: lake
    tables:
      - uri: /data/lake/orders
        schema: sales
  - type: unity_catalog
    name: uc
    url: http://uc:8080
    token_env: UC_TOKEN
  - type: mcp
    name: om
    transport: streamable_http
    url: http://om:8585/mcp
    token_env: OM_TOKEN
    mapping: mappings/om.yaml
  - type: custom
    name: mine
    options: {a: 1}
"""


def test_fr_src_01_sources_config_yaml(tmp_path: Path) -> None:
    p = tmp_path / "sources.yaml"
    p.write_text(YAML)
    cfg = load_sources_config(p)
    kinds = [type(s) for s in cfg.sources]
    assert kinds == [
        DeltaSourceConfig,
        UnityCatalogSourceConfig,
        McpSourceConfig,
        PluginSourceConfig,
    ]
    delta, uc, mcp, plugin = cfg.sources
    assert isinstance(delta, DeltaSourceConfig)
    assert delta.tables[0].name == "orders"
    assert delta.tables[0].schema_ == "sales"
    assert isinstance(uc, UnityCatalogSourceConfig)
    assert uc.token_env == "UC_TOKEN"  # noqa: S105
    assert isinstance(mcp, McpSourceConfig)
    assert mcp.token_env == "OM_TOKEN"  # noqa: S105
    assert isinstance(plugin, PluginSourceConfig)
    assert plugin.type == "custom"
    assert plugin.options == {"a": 1}


def test_sources_config_missing_path_is_empty(tmp_path: Path) -> None:
    assert load_sources_config(None).sources == []
    assert load_sources_config(tmp_path / "nope.yaml").sources == []


def test_sources_config_duplicate_names(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text("sources:\n  - {type: custom, name: a}\n  - {type: custom, name: a}\n")
    with pytest.raises(ConfigError, match="duplicate"):
        load_sources_config(p)


def test_sources_config_yaml_syntax_error_has_line(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text("sources:\n  - type: delta\n   name: [oops\n")
    with pytest.raises(ConfigError, match=r"line \d+"):
        load_sources_config(p)


def test_sources_config_invalid_entry(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text("sources:\n  - {type: unity_catalog, name: uc}\n")
    with pytest.raises(ConfigError, match="url"):
        load_sources_config(p)


def test_profile_policy_from_settings() -> None:
    policy = ProfilePolicy.from_settings(Settings())
    assert policy.top_k == 10
    assert policy.low_cardinality == 50
    assert policy.value_max_length == 64
    assert policy.samples is False
    assert policy.sensitive_tags == ["PII.*"]


def test_new_scan_settings_defaults() -> None:
    assert Settings().scan_concurrency == 8


def test_fr_src_01_compose_sources_config_is_valid() -> None:
    """The dev stack's operator config parses and names the shipped OM mapping."""
    from lakeloom.sources.config import (
        DeltaSourceConfig,
        McpSourceConfig,
        UnityCatalogSourceConfig,
    )

    repo = Path(__file__).parents[3]
    config = load_sources_config(repo / "deploy/compose/lakeloom-sources.yaml")

    by_name = {s.name: s for s in config.sources}
    assert list(by_name) == ["fixture_delta", "fixture_uc", "openmetadata_mcp"]
    delta = by_name["fixture_delta"]
    assert isinstance(delta, DeltaSourceConfig)
    assert [t.uri for t in delta.tables] == [
        f"file:///lake/{n}" for n in ("customers", "products", "orders", "invoices")
    ]
    assert all((t.catalog, t.schema_) == ("retail", "sales") for t in delta.tables)
    uc = by_name["fixture_uc"]
    assert isinstance(uc, UnityCatalogSourceConfig)
    assert uc.read_storage is True
    assert uc.catalogs == ["retail"]  # not UC OSS's built-in `unity` sample catalog
    om = by_name["openmetadata_mcp"]
    assert isinstance(om, McpSourceConfig)
    assert om.token_file == Path("/secrets/om_bot_token")
    assert (repo / "examples/mcp-mappings" / om.mapping.name).is_file()
    assert {delta.om_service, uc.om_service, om.om_service} == {"lakeloom_fixture"}
