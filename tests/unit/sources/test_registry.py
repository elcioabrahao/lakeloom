import importlib.metadata
from typing import Any

import pytest

from lakeloom.config import ConfigError, Settings
from lakeloom.sources.config import PluginSourceConfig, SourcesConfig
from lakeloom.sources.registry import build_sources


class FakeEntryPoint:
    name = "fake"

    def load(self) -> Any:
        def factory(cfg: PluginSourceConfig, settings: Settings) -> Any:
            return ("fake-source", cfg.name)

        return factory


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_entry_points(**kwargs: Any) -> list[FakeEntryPoint]:
        assert kwargs.get("group") == "lakeloom.sources"
        return [FakeEntryPoint()]

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


def test_fr_src_06_entry_point_plugin_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    cfg = SourcesConfig(sources=[PluginSourceConfig(type="fake", name="mine", options={})])
    built = build_sources(cfg, Settings())
    assert built == {"mine": ("fake-source", "mine")}


def test_fr_src_06_unknown_type_names_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    cfg = SourcesConfig(sources=[PluginSourceConfig(type="fake2", name="x", options={})])
    with pytest.raises(ConfigError, match="fake2"):
        build_sources(cfg, Settings())
