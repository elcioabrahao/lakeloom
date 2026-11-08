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
