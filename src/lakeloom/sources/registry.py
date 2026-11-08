"""Build configured sources: built-ins by ``type``, others via entry points (FR-SRC-06)."""

import importlib
import importlib.metadata
from collections.abc import Callable
from typing import Any

from lakeloom.config import ConfigError, Settings
from lakeloom.sources.base import LakeSource
from lakeloom.sources.config import PluginSourceConfig, SourcesConfig

__all__ = ["ENTRY_POINT_GROUP", "build_sources"]

ENTRY_POINT_GROUP = "lakeloom.sources"

# Built-in connector modules; each exposes ``build_source(config, settings)``.
_BUILTIN_MODULES = {
    "delta": "lakeloom.sources.delta_storage",
    "unity_catalog": "lakeloom.sources.unity_catalog",
    "mcp": "lakeloom.sources.mcp_adapter",
}


def _failed(kind: str, exc: BaseException) -> ConfigError:
    return ConfigError(f"source {kind!r} failed to load: {exc}")


def _builtin_factory(kind: str) -> Callable[[Any, Settings], LakeSource]:
    try:
        module = importlib.import_module(_BUILTIN_MODULES[kind])
        return module.build_source
    except ImportError as exc:
        raise ConfigError(f"source type {kind!r} is not available: {exc}") from None
    except Exception as exc:
        raise _failed(kind, exc) from None


def _plugin_factory(kind: str) -> Callable[[PluginSourceConfig, Settings], LakeSource]:
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name == kind:
            try:
                return entry_point.load()
            except Exception as exc:
                raise _failed(kind, exc) from None
    raise ConfigError(
        f"unknown source type {kind!r}: not built in and no '{ENTRY_POINT_GROUP}' "
        "entry point provides it"
    )


def build_sources(config: SourcesConfig, settings: Settings) -> dict[str, LakeSource]:
    """Instantiate every configured source, keyed by its unique name."""
    built: dict[str, LakeSource] = {}
    for source in config.sources:
        factory: Callable[[Any, Settings], LakeSource] = (
            _plugin_factory(source.type)
            if isinstance(source, PluginSourceConfig)
            else _builtin_factory(source.type)
        )
        try:
            built[source.name] = factory(source, settings)
        except ConfigError:
            raise
        except Exception as exc:
            raise _failed(source.type, exc) from None
    return built
