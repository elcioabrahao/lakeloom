"""Unity Catalog connector over the UC REST API (FR-SRC-02).

Speaks plain HTTP to ``{url}/api/2.1/unity-catalog`` with httpx, so it works against Unity
Catalog OSS and Databricks-hosted UC without an SDK (ADR 0008). Endpoints used:
``GET /catalogs``, ``GET /schemas?catalog_name=``, ``GET /tables?catalog_name=&schema_name=``
and ``GET /tables/{full_name}``; list endpoints follow ``next_page_token``.

UC OSS has no tag or constraint API, so the ``lakeloom.*`` table-property conventions of the
Delta connector are honoured here too (``lakeloom.tags``, ``lakeloom.owners``,
``lakeloom.primaryKey``, ``lakeloom.foreignKey.<column>`` and
``lakeloom.columnTags.<column>``). Databricks-hosted UC reports ``table_constraints``,
which are mapped as well. Comments are untrusted text (NFR-SEC-14).

With ``read_storage=True``, table versions and profiles come from the table's Delta
``storage_location`` through :class:`DeltaStorageSource`; profiles are therefore computed
where the data lives (FR-SRC-08). The bearer token is read from ``os.environ[token_env]``
and never logged or placed in errors.
"""

import os
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.base import ProfilePolicy, SourceCapabilities
from lakeloom.sources.config import DeltaSourceConfig, DeltaTableEntry, UnityCatalogSourceConfig
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.sources.models import (
    ColumnInfo,
    Constraint,
    TableDescription,
    TableProfile,
    TableRef,
    TableVersion,
)
from lakeloom.untrusted import UntrustedText

__all__ = ["UnityCatalogSource", "build_source"]

API_PREFIX = "/api/2.1/unity-catalog"
_STORAGE_SCHEMES = ("file://", "s3://", "abfss://", "gs://")
_MAX_PAGES = 1000
_COLUMN_TAGS = "lakeloom.columnTags."
_FOREIGN_KEY = "lakeloom.foreignKey."


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _obj(value: object) -> dict[str, Any]:
    """``value`` as a JSON object, or ``{}``."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _objs(value: object) -> list[dict[str, Any]]:
    """The JSON objects in ``value`` when it is a list; anything else yields ``[]``."""
    if not isinstance(value, list):
        return []
    return [cast("dict[str, Any]", v) for v in cast("list[object]", value) if isinstance(v, dict)]


def _strs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in cast("list[object]", value)]


def _unavailable(message: str) -> LakeloomError:
    return LakeloomError(
        "upstream_unavailable",
        message,
        hint="check that Unity Catalog is running and the source url is reachable, then retry",
    )


class UnityCatalogSource:
    """A read-only view of one Unity Catalog server."""

    kind = "unity_catalog"

    def __init__(
        self,
        config: UnityCatalogSourceConfig,
        *,
        http: httpx.AsyncClient | None = None,
        delta_factory: Callable[[DeltaSourceConfig], DeltaStorageSource] = DeltaStorageSource,
    ) -> None:
        self.name = config.name
        self.om_service = config.om_service
        self.capabilities = SourceCapabilities(
            profiling=config.read_storage, versions=config.read_storage, constraints=True
        )
        self._config = config
        self._base = str(config.url).rstrip("/") + API_PREFIX
        self._token_env = config.token_env
        value = os.environ.get(config.token_env) if config.token_env else None
        self._token = SecretStr(value) if value else None
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=30)
        self._delta_factory = delta_factory
        self._source = f"unity_catalog:{config.name}"

    # --- HTTP -------------------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        try:
            response = await self._http.get(f"{self._base}{path}", params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise _unavailable(f"Unity Catalog timed out ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise _unavailable(f"cannot connect to Unity Catalog ({type(exc).__name__})") from None
        status = response.status_code
        if status == 404:
            raise LakeloomError(
                "not_found",
                f"Unity Catalog has no such object: {path}",
                hint="call list_tables / list_schemas to see what exists in this source",
            )
        if status in (401, 403):
            env = self._token_env or "token_env"
            raise LakeloomError(
                "forbidden",
                f"Unity Catalog refused the request (HTTP {status})",
                hint=f"check the token in environment variable {env} and its Unity Catalog grants",
            )
        if status >= 500:
            raise _unavailable(f"Unity Catalog returned HTTP {status}")
        if status >= 400:
            raise LakeloomError(
                "validation_error",
                f"Unity Catalog rejected the request (HTTP {status}) for {path}",
                hint="check catalog, schema and table names",
            )
        try:
            body = response.json()
        except ValueError:
            raise _unavailable("Unity Catalog returned a non-JSON body") from None
        if not isinstance(body, dict):
            raise _unavailable("Unity Catalog returned an unexpected body")
        return _obj(cast("object", body))

    async def _list(
        self, path: str, key: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Collect every page of a list endpoint, following ``next_page_token``."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        token: str | None = None
        for _ in range(_MAX_PAGES):
            page_params = dict(params or {})
            if token:
                page_params["page_token"] = token
            body = await self._get(path, page_params or None)
            items.extend(_objs(body.get(key)))
            next_token = body.get("next_page_token")
            if not next_token:
                return items
            if not isinstance(next_token, str) or next_token in seen:
                raise _unavailable("Unity Catalog pagination did not terminate")
            seen.add(next_token)
            token = next_token
        raise _unavailable("Unity Catalog pagination did not terminate")

    @staticmethod
    def _need_catalog(catalog: str | None) -> str:
        if not catalog:
            raise LakeloomError(
                "validation_error",
                "Unity Catalog tables live in a catalog",
                hint="pass catalog (call list_catalogs to see them)",
            )
        return catalog

    async def _table_json(self, ref: TableRef) -> dict[str, Any]:
        catalog = self._need_catalog(ref.catalog)
        full = ".".join(quote(p, safe="") for p in (catalog, ref.schema_, ref.name))
        return await self._get(f"/tables/{full}")

    # --- LakeSource -------------------------------------------------------------------------

    async def list_catalogs(self) -> list[str]:
        names = [str(c["name"]) for c in await self._list("/catalogs", "catalogs") if "name" in c]
        allowed = self._config.catalogs
        return sorted(n for n in names if allowed is None or n in allowed)

    async def list_schemas(self, catalog: str | None) -> list[str]:
        params = {"catalog_name": self._need_catalog(catalog)}
        items = await self._list("/schemas", "schemas", params)
        return sorted(str(s["name"]) for s in items if "name" in s)

    async def list_tables(self, catalog: str | None, schema: str) -> list[TableRef]:
        params = {"catalog_name": self._need_catalog(catalog), "schema_name": schema}
        items = await self._list("/tables", "tables", params)
        return sorted(
            (
                TableRef(catalog=catalog, schema=schema, name=str(t["name"]))
                for t in items
                if "name" in t
            ),
            key=lambda r: r.name,
        )

    async def describe_table(self, ref: TableRef) -> TableDescription:
        return self._describe(ref, await self._table_json(ref))

    def _describe(self, ref: TableRef, body: dict[str, Any]) -> TableDescription:
        props = {str(k): str(v) for k, v in _obj(body.get("properties")).items()}
        raw_columns = _objs(body.get("columns"))
        columns = [
            ColumnInfo(
                name=str(c["name"]),
                data_type=str(c.get("type_text") or c.get("type_name") or "unknown"),
                nullable=bool(c.get("nullable", True)),
                comment=(
                    UntrustedText(text=str(c["comment"]), source=self._source)
                    if c.get("comment")
                    else None
                ),
                tags=_split(props.get(f"{_COLUMN_TAGS}{c['name']}")),
                position=int(c.get("position", i)),
            )
            for i, c in enumerate(raw_columns)
        ]
        partitions = sorted(
            (int(c["partition_index"]), str(c["name"]))
            for c in raw_columns
            if c.get("partition_index") is not None
        )
        owners = _split(props.get("lakeloom.owners"))
        owner = body.get("owner")
        if isinstance(owner, str) and owner and owner not in owners:
            owners.insert(0, owner)
        comment = body.get("comment")
        return TableDescription(
            ref=ref,
            comment=UntrustedText(text=str(comment), source=self._source) if comment else None,
            columns=columns,
            partition_columns=[name for _, name in partitions],
            constraints=self._constraints(ref, props, body.get("table_constraints")),
            tags=_split(props.get("lakeloom.tags")),
            owners=owners,
            properties={k: v for k, v in props.items() if not k.startswith("lakeloom.")},
            storage_location=(
                str(body["storage_location"]) if body.get("storage_location") else None
            ),
            version=TableVersion(),
        )

    @staticmethod
    def _constraints(ref: TableRef, props: dict[str, str], declared: object) -> list[Constraint]:
        out: list[Constraint] = []
        for item in _objs(declared):
            pk = _obj(item.get("primary_key_constraint"))
            fk = _obj(item.get("foreign_key_constraint"))
            if pk.get("child_columns"):
                out.append(Constraint(kind="primary_key", columns=_strs(pk["child_columns"])))
            elif fk.get("child_columns"):
                parts = str(fk.get("parent_table", "")).split(".")
                target = (
                    TableRef(catalog=parts[0], schema=parts[1], name=parts[2])
                    if len(parts) == 3
                    else None
                )
                out.append(
                    Constraint(
                        kind="foreign_key",
                        columns=_strs(fk["child_columns"]),
                        ref_table=target,
                        ref_columns=_strs(fk.get("parent_columns")),
                    )
                )
        if not out:
            pk_cols = _split(props.get("lakeloom.primaryKey"))
            if pk_cols:
                out.append(Constraint(kind="primary_key", columns=pk_cols))
            for key, value in sorted(props.items()):
                if not key.startswith(_FOREIGN_KEY):
                    continue
                parts = value.split(".")
                if len(parts) != 3:
                    continue  # malformed convention value: ignore rather than fail the table
                out.append(
                    Constraint(
                        kind="foreign_key",
                        columns=[key[len(_FOREIGN_KEY) :]],
                        ref_table=TableRef(catalog=ref.catalog, schema=parts[0], name=parts[1]),
                        ref_columns=[parts[2]],
                    )
                )
        return out

    # --- storage-backed operations ------------------------------------------------------------

    def _storage_uri(self, location: str | None) -> str | None:
        """Apply ``storage_root_map`` and accept only Delta-capable storage locations."""
        if not location:
            return None
        for prefix in sorted(self._config.storage_root_map, key=len, reverse=True):
            if location.startswith(prefix):
                location = self._config.storage_root_map[prefix] + location[len(prefix) :]
                break
        if location.startswith("/") or location.startswith(_STORAGE_SCHEMES):
            return location
        return None

    def _delta_for(
        self, ref: TableRef, body: dict[str, Any], description: TableDescription
    ) -> DeltaStorageSource | None:
        if str(body.get("data_source_format", "DELTA")).upper() != "DELTA":
            return None
        uri = self._storage_uri(description.storage_location)
        if uri is None:
            return None
        entry = DeltaTableEntry(
            uri=uri,
            catalog=ref.catalog,
            schema=ref.schema_,
            name=ref.name,
            column_tags={c.name: c.tags for c in description.columns if c.tags},
        )
        config = DeltaSourceConfig(type="delta", name=f"{self.name}:storage", tables=[entry])
        return self._delta_factory(config)

    async def get_table_version(self, ref: TableRef) -> TableVersion:
        if not self._config.read_storage:
            return TableVersion()
        body = await self._table_json(ref)
        delta = self._delta_for(ref, body, self._describe(ref, body))
        if delta is None:
            return TableVersion()
        return (await delta.describe_table(ref)).version

    async def profile_table(self, ref: TableRef, policy: ProfilePolicy) -> TableProfile:
        if not self._config.read_storage:
            raise LakeloomError(
                "validation_error",
                f"profiling is disabled for Unity Catalog source {self.name!r}",
                hint="set read_storage: true on the source to profile Delta tables in place",
            )
        body = await self._table_json(ref)
        delta = self._delta_for(ref, body, self._describe(ref, body))
        if delta is None:
            raise LakeloomError(
                "validation_error",
                f"table {ref.full_name!r} is not a Delta table on supported storage",
                hint="profiling needs a DELTA table at a file://, s3://, abfss:// or gs:// path",
            )
        return await delta.profile_table(ref, policy)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()


def build_source(config: UnityCatalogSourceConfig, settings: Settings) -> UnityCatalogSource:
    """Registry factory (FR-SRC-06)."""
    return UnityCatalogSource(config)
