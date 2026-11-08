"""Delta-on-storage connector (FR-SRC-03, FR-SRC-04).

Reads Delta tables straight from ``file://``, ``s3://``, ``abfss://`` or ``gs://`` URIs with
delta-rs. Tables are listed explicitly by the operator (NFR-SEC-13). Metadata conventions
are documented in ADR 0008: description, field ``comment`` / ``lakeloom.tags`` metadata and
the ``lakeloom.*`` table configuration keys.

``deltalake`` is an optional extra and is imported lazily, so the base install can import
this module (and the registry) without it.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.base import ProfilePolicy, SourceCapabilities
from lakeloom.sources.config import DeltaSourceConfig, DeltaTableEntry
from lakeloom.sources.models import (
    ColumnInfo,
    Constraint,
    TableDescription,
    TableProfile,
    TableRef,
    TableVersion,
)
from lakeloom.untrusted import UntrustedText

__all__ = ["DeltaStorageSource", "build_source"]

_NOT_FOUND_HINT = "check the table URI in LAKELOOM_CONFIG_FILE"
_SPARK_TYPES = {
    "long": "bigint",
    "integer": "int",
    "short": "smallint",
    "byte": "tinyint",
    "timestamp_ntz": "timestamp_ntz",
}
_NOT_FOUND_MARKERS = ("not a delta table", "no such file", "does not exist", "not found")


def _require_deltalake() -> Any:
    try:
        import deltalake
    except ImportError:
        raise LakeloomError(
            "validation_error",
            "the Delta connector needs the 'deltalake' package",
            hint="pip install 'lakeloom[delta]'",
        ) from None
    return deltalake


def _spark_type(delta_type: Any) -> str:
    """Render a delta-rs type as a Spark-style string (``bigint``, ``decimal(12,2)``, ...)."""
    primitive = getattr(delta_type, "type", None)
    if isinstance(primitive, str):
        return _SPARK_TYPES.get(primitive, primitive)
    return str(delta_type)


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _wrap(exc: BaseException, entry: DeltaTableEntry) -> LakeloomError:
    text = str(exc).lower()
    if type(exc).__name__ == "TableNotFoundError" or isinstance(exc, FileNotFoundError):
        not_found = True
    else:
        not_found = any(marker in text for marker in _NOT_FOUND_MARKERS)
    if not_found:
        return LakeloomError(
            "not_found",
            f"no Delta table at {entry.uri}",
            hint=_NOT_FOUND_HINT,
        )
    return LakeloomError(
        "upstream_unavailable",
        f"cannot read Delta table at {entry.uri}: {type(exc).__name__}",
        hint="check storage credentials and connectivity for the table URI",
    )


def _read_table(entry: DeltaTableEntry, ref: TableRef, source_name: str) -> TableDescription:
    deltalake = _require_deltalake()
    try:
        table = deltalake.DeltaTable(entry.uri, storage_options=entry.storage_options or None)
        schema = table.schema()
        meta = table.metadata()
        version = table.version()
        history = table.history(1)
    except Exception as exc:
        raise _wrap(exc, entry) from None
    conf: dict[str, str] = dict(meta.configuration or {})
    source = f"delta:{entry.uri}"
    config_source = f"config:{source_name}"
    known = {field.name for field in schema.fields}
    for overlay_column in (
        *entry.foreign_keys,
        *entry.column_tags,
        *entry.column_comments,
        *(entry.primary_key or []),
    ):
        if overlay_column not in known:
            raise LakeloomError(
                "validation_error",
                f"overlay for {ref.full_name!r} names unknown column {overlay_column!r}",
                hint=f"fix or remove column {overlay_column!r} in the source config "
                f"(LAKELOOM_CONFIG_FILE); columns are {sorted(known)}",
            )
    columns: list[ColumnInfo] = []
    for position, field in enumerate(schema.fields):
        field_meta: dict[str, Any] = dict(field.metadata or {})
        comment: UntrustedText | None = None
        if field.name in entry.column_comments:
            comment = UntrustedText(text=entry.column_comments[field.name], source=config_source)
        elif field_meta.get("comment"):
            comment = UntrustedText(text=str(field_meta["comment"]), source=source)
        tags = entry.column_tags.get(field.name)
        columns.append(
            ColumnInfo(
                name=field.name,
                data_type=_spark_type(field.type),
                nullable=field.nullable,
                comment=comment,
                tags=list(tags) if tags is not None else _split(field_meta.get("lakeloom.tags")),
                position=position,
            )
        )
    constraints: list[Constraint] = []
    pk = (
        entry.primary_key
        if entry.primary_key is not None
        else _split(conf.get("lakeloom.primaryKey"))
    )
    if pk:
        constraints.append(Constraint(kind="primary_key", columns=list(pk)))
    prefix = "lakeloom.foreignKey."
    fks = {k[len(prefix) :]: v for k, v in conf.items() if k.startswith(prefix)}
    fks.update(entry.foreign_keys)
    for fk_column, target in sorted(fks.items()):
        parts = target.split(".")
        if len(parts) != 3:
            continue  # malformed convention value: ignore rather than fail the table
        schema_name, table_name, column = parts
        constraints.append(
            Constraint(
                kind="foreign_key",
                columns=[fk_column],
                ref_table=TableRef(catalog=ref.catalog, schema=schema_name, name=table_name),
                ref_columns=[column],
            )
        )
    committed_at: datetime | None = None
    if history and isinstance(history[0].get("timestamp"), int | float):
        committed_at = datetime.fromtimestamp(history[0]["timestamp"] / 1000, tz=UTC)
    description: UntrustedText | None = None
    if entry.description:
        description = UntrustedText(text=entry.description, source=config_source)
    elif meta.description:
        description = UntrustedText(text=meta.description, source=source)
    return TableDescription(
        ref=ref,
        comment=description,
        columns=columns,
        partition_columns=list(meta.partition_columns),
        constraints=constraints,
        tags=list(entry.tags) if entry.tags is not None else _split(conf.get("lakeloom.tags")),
        owners=(
            list(entry.owners) if entry.owners is not None else _split(conf.get("lakeloom.owners"))
        ),
        properties={k: v for k, v in conf.items() if not k.startswith("lakeloom.")},
        storage_location=entry.uri,
        version=TableVersion(version=version, committed_at=committed_at),
    )


class DeltaStorageSource:
    """A set of operator-configured Delta tables on object storage or local disk."""

    kind = "delta"

    def __init__(self, config: DeltaSourceConfig) -> None:
        _require_deltalake()
        self.name = config.name
        self.capabilities = SourceCapabilities(profiling=True, versions=True, constraints=True)
        self._entries: dict[tuple[str | None, str, str], DeltaTableEntry] = {}
        for entry in config.tables:
            name = entry.name or entry.uri.rstrip("/").rsplit("/", 1)[-1]
            self._entries[(entry.catalog, entry.schema_, name)] = entry

    def _entry(self, ref: TableRef) -> DeltaTableEntry:
        entry = self._entries.get((ref.catalog, ref.schema_, ref.name))
        if entry is None:
            raise LakeloomError(
                "not_found",
                f"table {ref.full_name!r} is not configured in source {self.name!r}",
                hint="call list_tables to see the configured tables",
            )
        return entry

    async def list_catalogs(self) -> list[str]:
        return sorted({c for c, _, _ in self._entries if c})

    async def list_schemas(self, catalog: str | None) -> list[str]:
        return sorted({s for c, s, _ in self._entries if c == catalog})

    async def list_tables(self, catalog: str | None, schema: str) -> list[TableRef]:
        return [
            TableRef(catalog=c, schema=s, name=n)
            for c, s, n in sorted(self._entries, key=lambda k: (k[0] or "", k[1], k[2]))
            if c == catalog and s == schema
        ]

    async def describe_table(self, ref: TableRef) -> TableDescription:
        entry = self._entry(ref)
        return await asyncio.to_thread(_read_table, entry, ref, self.name)

    async def get_table_version(self, ref: TableRef) -> TableVersion:
        return (await self.describe_table(ref)).version

    async def profile_table(self, ref: TableRef, policy: ProfilePolicy) -> TableProfile:
        raise LakeloomError(
            "validation_error",
            "profiling not implemented yet",
            hint="profiling arrives with the profiling module; scan without profile for now",
        )

    async def aclose(self) -> None:
        return None


def build_source(config: DeltaSourceConfig, settings: Settings) -> DeltaStorageSource:
    """Registry factory (FR-SRC-06)."""
    return DeltaStorageSource(config)
