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


class BrokenLoadEntryPoint:
    name = "boom"

    def load(self) -> Any:
        raise ImportError("no module named secret_dep")


class BrokenFactoryEntryPoint:
    name = "boom2"

    def load(self) -> Any:
        def factory(cfg: PluginSourceConfig, settings: Settings) -> Any:
            raise RuntimeError("bad options")

        return factory


def test_fr_src_06_plugin_load_failure_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: [BrokenLoadEntryPoint()])
    cfg = SourcesConfig(sources=[PluginSourceConfig(type="boom", name="x", options={})])
    with pytest.raises(ConfigError, match=r"'boom' failed to load.*secret_dep"):
        build_sources(cfg, Settings())


def test_fr_src_06_plugin_factory_failure_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: [BrokenFactoryEntryPoint()])
    cfg = SourcesConfig(sources=[PluginSourceConfig(type="boom2", name="x", options={})])
    with pytest.raises(ConfigError, match=r"'boom2' failed to load.*bad options"):
        build_sources(cfg, Settings())


def test_builtin_without_build_source_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    monkeypatch.setattr(importlib, "import_module", lambda _name: types.ModuleType("m"))
    from lakeloom.sources.config import DeltaSourceConfig

    cfg = SourcesConfig(sources=[DeltaSourceConfig(type="delta", name="d", tables=[])])
    with pytest.raises(ConfigError, match="'delta' failed to load"):
        build_sources(cfg, Settings())


def test_fr_src_06_missing_delta_extra_shows_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    from lakeloom.sources.config import DeltaSourceConfig

    real_import = builtins.__import__

    def no_deltalake(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "deltalake" or name.startswith("deltalake."):
            raise ImportError("No module named 'deltalake'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_deltalake)
    cfg = SourcesConfig(sources=[DeltaSourceConfig(type="delta", name="d", tables=[])])
    with pytest.raises(ConfigError) as info:
        build_sources(cfg, Settings())
    assert "needs the 'deltalake' package" in str(info.value)
    assert "pip install 'lakeloom[delta]'" in str(info.value)
