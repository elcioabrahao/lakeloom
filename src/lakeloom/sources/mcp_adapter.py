"""Upstream MCP adapter: Lakeloom as an MCP client of another MCP server (FR-SRC-05).

The operator configures the upstream (Streamable HTTP ``url`` or stdio ``command``) and a
mapping YAML (:mod:`lakeloom.sources.mcp_mapping`); agents cannot add upstreams
(NFR-SEC-13). :meth:`McpUpstreamSource.start` connects, runs ``tools/list`` and checks
that every mapped tool exists; tool names are never hard-coded and never taken from tool
*results*. If the upstream is down or a tool is missing the source stays registered with
``available=False`` and every operation raises ``last_error``.

Everything the upstream returns is untrusted (NFR-SEC-14): comments become
``UntrustedText(source="mcp:<source name>")``, other strings are used as data only and
bounded in size, and payloads are size-capped before JSON decoding. A result that does
not match the mapping is ``upstream_unavailable`` naming the operation and JMESPath.
"""

import asyncio
import json
import math
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import cast

import httpx2
import jmespath.exceptions
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, PaginatedRequestParams, TextContent
from pydantic import SecretStr, TypeAdapter, ValidationError

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.base import ProfilePolicy, SourceCapabilities
from lakeloom.sources.config import McpSourceConfig
from lakeloom.sources.mcp_mapping import (
    McpMapping,
    OperationMapping,
    OperationName,
    evaluate,
    load_mapping,
    render_arguments,
)
from lakeloom.sources.models import (
    ColumnInfo,
    ColumnProfile,
    Constraint,
    TableDescription,
    TableProfile,
    TableRef,
    TableVersion,
)
from lakeloom.untrusted import UntrustedText

__all__ = ["McpUpstreamSource", "SessionFactory", "build_source", "default_session_factory"]

SessionFactory = Callable[[McpSourceConfig, str | None], AbstractAsyncContextManager[ClientSession]]

CONNECT_TIMEOUT = 10.0
CALL_TIMEOUT = 30.0
MAX_PAYLOAD_CHARS = 8 * 1024 * 1024  # text content decoded at most this long
MAX_PAGES = 100
MAX_ITEMS = 10_000
MAX_NAME = 1024  # identifiers, types, tags, cursors
COLUMNS_TRUNCATED = "lakeloom.columns_truncated"
MAX_TEXT = 64 * 1024  # comments (quote_untrusted cuts further when rendering)
_HINT = "check that the upstream MCP server is running and matches the mapping, then retry"


def _eval(operation: str, expression: str, data: object) -> object:
    """Evaluate a mapping JMESPath; a runtime type error on odd upstream data is a mismatch."""
    try:
        return evaluate(expression, data)
    except jmespath.exceptions.JMESPathError as exc:
        raise _unavailable(
            f"upstream result for {operation} does not match the mapping: JMESPath "
            f"{expression!r} failed ({type(exc).__name__})",
            hint="update the mapping to the upstream's result shape",
        ) from None


def _reject_constant(token: str) -> object:
    """``json.loads`` hook: NaN / Infinity / -Infinity are not JSON; refuse them."""
    raise ValueError(f"non-finite number {token}")


def _kind(exc: BaseException) -> str:
    """The type name of the innermost cause, looking through anyio's exception groups."""
    current: BaseException = exc
    for _ in range(32):
        leaves: tuple[BaseException, ...] = getattr(current, "exceptions", ())
        if not isinstance(current, BaseExceptionGroup) or not leaves:
            break
        current = leaves[0]
    return type(cast("object", current)).__name__


def _unavailable(message: str, hint: str = _HINT) -> LakeloomError:
    return LakeloomError("upstream_unavailable", message, hint=hint)


@asynccontextmanager
async def default_session_factory(
    config: McpSourceConfig, token: str | None
) -> AsyncGenerator[ClientSession]:
    """Connect over Streamable HTTP (bearer ``token``) or stdio; yields an uninitialised session."""
    async with AsyncExitStack() as stack:
        if config.transport == "streamable_http":
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=headers,
                    timeout=httpx2.Timeout(CALL_TIMEOUT, connect=CONNECT_TIMEOUT),
                )
            )
            read, write = await stack.enter_async_context(
                streamable_http_client(str(config.url), http_client=http)
            )
        else:
            command = config.command or []
            params = StdioServerParameters(command=command[0], args=command[1:])
            read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=CALL_TIMEOUT)
        )
        yield session


class _Shape(Exception):
    """A result field did not have the shape the mapping promised."""

    def __init__(self, path: str, expected: str) -> None:
        super().__init__(f"{path}: expected {expected}")
        self.path = path
        self.expected = expected


def _name(value: object, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _Shape(path, "a string")
    text = str(value)
    if not text or len(text) > MAX_NAME:
        raise _Shape(path, f"a non-empty string of at most {MAX_NAME} characters")
    return text


def _opt_name(value: object, path: str) -> str | None:
    return None if value is None else _name(value, path)


def _names(value: object, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Shape(path, "a list of strings")
    items = cast("list[object]", value)
    if len(items) > MAX_ITEMS:
        raise _Shape(path, f"at most {MAX_ITEMS} items")
    return [_name(v, path) for v in items]


def _objects(value: object, path: str, *, required: bool) -> list[dict[str, object]]:
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise _Shape(path, "a list of objects")
    items = cast("list[object]", value)
    if len(items) > MAX_ITEMS:
        raise _Shape(path, f"at most {MAX_ITEMS} items")
    if not all(isinstance(v, dict) for v in items):
        raise _Shape(path, "a list of objects")
    return cast("list[dict[str, object]]", items)


def _finite(value: object) -> bool:
    """A real JSON number: not bool, and not NaN/Infinity (which also arrive via
    already-decoded ``structuredContent``)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and (isinstance(value, int) or math.isfinite(value))
    )


def _int(value: object, path: str, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if not _finite(value):
        raise _Shape(path, "an integer")
    number = cast("int | float", value)
    if isinstance(number, float) and not number.is_integer():
        raise _Shape(path, "an integer")
    return int(number)


def _float(value: object, path: str) -> float | None:
    if value is None:
        return None
    if not _finite(value):
        raise _Shape(path, "a finite number")
    return float(cast("int | float", value))


def _bool(value: object, path: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _Shape(path, "a boolean")
    return value


_DATETIME: TypeAdapter[datetime] = TypeAdapter(datetime)


def _datetime(value: object, path: str) -> datetime | None:
    if value is None:
        return None
    try:
        return _DATETIME.validate_python(value)
    except ValidationError:
        raise _Shape(path, "a timestamp") from None


def _strmap(value: object, path: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _Shape(path, "an object")
    items = cast("dict[object, object]", value)
    if len(items) > MAX_ITEMS:
        raise _Shape(path, f"at most {MAX_ITEMS} entries")
    return {_name(k, path): _text(v, path) for k, v in items.items()}


def _text(value: object, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise _Shape(path, "a string")
    return str(value)[:MAX_TEXT]


def _dotted_ref(value: str) -> tuple[TableRef | None, str]:
    """``a.b.c.table.col`` -> (``b.c.table`` as a TableRef, ``col``); unqualified -> (None, col)."""
    parts = value.split(".")
    if len(parts) < 3:
        return None, parts[-1]
    table = parts[-4:-1]
    ref = (
        TableRef(catalog=table[0], schema=table[1], name=table[2])
        if len(table) == 3
        else TableRef(schema=table[0], name=table[1])
    )
    return ref, parts[-1]


class McpUpstreamSource:
    """A LakeSource backed by tools of an upstream MCP server, bound by a mapping."""

    kind = "mcp"

    def __init__(
        self,
        config: McpSourceConfig,
        mapping: McpMapping,
        *,
        session_factory: SessionFactory = default_session_factory,
        connect_timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self.name = config.name
        self.om_service = config.om_service
        ops = mapping.operations
        describe = ops.get("describe_table")
        self.capabilities = SourceCapabilities(
            profiling="profile_table" in ops,
            versions="get_table_version" in ops,
            constraints=describe is not None and "constraints" in describe.result.fields,
        )
        self.available = False
        self.last_error: LakeloomError | None = _unavailable(
            f"upstream MCP source '{config.name}' is not started",
            hint="the server connects upstream MCP sources at startup; check the server logs",
        )
        self._config = config
        self._mapping = mapping
        self._factory = session_factory
        self._connect_timeout = connect_timeout
        self._task: asyncio.Task[None] | None = None
        self._closing: asyncio.Event | None = None
        self._session: ClientSession | None = None
        self._source = f"mcp:{config.name}"

    # --- lifecycle --------------------------------------------------------------------------

    def _token(self) -> SecretStr | None:
        if self._config.token_env:
            value = os.environ.get(self._config.token_env, "").strip()
            if not value:
                raise _unavailable(
                    f"environment variable {self._config.token_env} for upstream MCP source "
                    f"'{self.name}' is not set",
                    hint="set the variable to the upstream bearer token and restart",
                )
            return SecretStr(value)
        if self._config.token_file:
            try:
                value = self._config.token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise _unavailable(
                    f"cannot read token_file for upstream MCP source '{self.name}' "
                    f"({type(exc).__name__})",
                    hint="check the token_file path and its permissions",
                ) from None
            if not value:
                raise _unavailable(
                    f"token_file for upstream MCP source '{self.name}' is empty",
                    hint="write the upstream bearer token into the file",
                )
            return SecretStr(value)
        return None

    async def _connect(
        self, stack: AsyncExitStack, token: SecretStr | None
    ) -> tuple[ClientSession, set[str]]:
        secret = token.get_secret_value() if token else None
        session = await stack.enter_async_context(self._factory(self._config, secret))
        await session.initialize()
        names: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            result = await session.list_tools(
                params=None if cursor is None else PaginatedRequestParams(cursor=cursor)
            )
            names.update(tool.name for tool in result.tools)
            cursor = result.next_cursor
            if not cursor:
                break
        return session, names

    async def _run(
        self,
        token: SecretStr | None,
        ready: "asyncio.Future[tuple[ClientSession, set[str]]]",
        closing: asyncio.Event,
    ) -> None:
        """Own the connection: transports are entered and exited in this one task.

        The SDK's transports are anyio task groups, which must be exited in the task that
        entered them; a dedicated task keeps that true whichever task calls ``start`` and
        ``aclose``, and lets ``start`` cancel a hung connect cleanly.
        """
        try:
            async with AsyncExitStack() as stack:
                connected = await self._connect(stack, token)
                if not ready.done():
                    ready.set_result(connected)
                await closing.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
        if not closing.is_set():  # the transport ended on its own
            self._session = None
            self.available = False
            self.last_error = _unavailable(f"upstream MCP source '{self.name}' disconnected")

    async def start(self) -> None:
        """Connect, run ``tools/list`` and check every mapped tool; never raises.

        On failure the source stays registered with ``available=False`` and ``last_error``.
        """
        await self.aclose()
        try:
            token = self._token()
        except LakeloomError as exc:
            self.last_error = exc
            return
        ready: asyncio.Future[tuple[ClientSession, set[str]]] = (
            asyncio.get_running_loop().create_future()
        )
        closing = asyncio.Event()
        task = asyncio.create_task(self._run(token, ready, closing), name=f"mcp:{self.name}")
        self._task, self._closing = task, closing
        try:
            session, tools = await asyncio.wait_for(
                asyncio.shield(ready), timeout=self._connect_timeout
            )
        except TimeoutError:
            await self._stop(cancel=True)
            self.last_error = _unavailable(
                f"connecting to upstream MCP source '{self.name}' timed out after "
                f"{self._connect_timeout:g} s"
            )
            return
        except Exception as exc:
            await self._stop()
            self.last_error = _unavailable(
                f"cannot connect to upstream MCP source '{self.name}' ({_kind(exc)})"
            )
            return
        missing = [op.tool for op in self._mapping.operations.values() if op.tool not in tools]
        if missing:
            await self._stop()
            self.last_error = _unavailable(
                f"upstream tool '{missing[0]}' not found",
                hint="update the mapping or the upstream server",
            )
            return
        self._session = session
        self.available = True
        self.last_error = None

    async def _stop(self, *, cancel: bool = False) -> None:
        """Ask the connection task to close; cancel it if asked or if it does not finish."""
        task, closing = self._task, self._closing
        self._task = self._closing = None
        self._session = None
        self.available = False
        if task is None or closing is None:
            return
        closing.set()
        if cancel:
            task.cancel()
        try:
            await asyncio.wait_for(task, timeout=self._connect_timeout)
        except BaseException as exc:  # best-effort close; never propagate
            if isinstance(exc, asyncio.CancelledError) and not task.cancelled():
                raise  # our own caller was cancelled
            # wait_for cancelled the hung task on timeout; nothing else to do.

    async def aclose(self) -> None:
        if self.available:
            self.last_error = _unavailable(f"upstream MCP source '{self.name}' is closed")
        await self._stop()

    # --- calls ------------------------------------------------------------------------------

    def _operation(self, name: OperationName) -> OperationMapping:
        if not self.available or self._session is None:
            raise self.last_error or _unavailable(f"upstream MCP source '{self.name}' is down")
        op = self._mapping.operations.get(name)
        if op is None:
            raise LakeloomError(
                "validation_error",
                f"source '{self.name}' does not support {name}",
                hint="its MCP mapping does not bind this operation; see list_sources",
            )
        return op

    async def _call(
        self, name: OperationName, op: OperationMapping, values: dict[str, str | int | None]
    ) -> object:
        assert self._session is not None  # noqa: S101 - checked by _operation
        arguments = render_arguments(op.arguments, values)
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(op.tool, arguments), timeout=CALL_TIMEOUT
            )
        except TimeoutError:
            raise _unavailable(f"upstream tool '{op.tool}' timed out for {name}") from None
        except Exception as exc:
            raise _unavailable(
                f"upstream tool '{op.tool}' failed for {name} ({type(exc).__name__})"
            ) from None
        if result.is_error:
            if op.not_found is not None:
                try:
                    payload = self._payload(name, result)
                except LakeloomError:
                    payload = None
                if payload is not None and _eval(name, op.not_found, payload):
                    raise LakeloomError(
                        "not_found",
                        f"source '{self.name}' has no such object for {name}",
                        hint="call list_tables to see what exists in this source",
                    )
            # The upstream's error text is untrusted and stays out of the message.
            raise _unavailable(f"upstream tool '{op.tool}' reported an error for {name}")
        return self._payload(name, result)

    def _payload(self, name: str, result: CallToolResult) -> object:
        if result.structured_content is not None:
            return result.structured_content
        text = next((c.text for c in result.content if isinstance(c, TextContent)), None)
        if text is None:
            raise _unavailable(f"upstream returned non-JSON for {name}")
        if len(text) > MAX_PAYLOAD_CHARS:
            raise _unavailable(f"upstream result for {name} is too large ({len(text)} chars)")
        try:
            return cast("object", json.loads(text, parse_constant=_reject_constant))
        except (ValueError, RecursionError):
            raise _unavailable(f"upstream returned non-JSON for {name}") from None

    def _shape_error(self, name: str, op: OperationMapping, key: str, exc: _Shape) -> LakeloomError:
        expression = op.result.fields.get(key, op.result.items or "")
        return _unavailable(
            f"upstream result for {name} does not match the mapping: field {key!r} "
            f"(JMESPath {expression!r}) — {exc.path}: expected {exc.expected}",
            hint="update the mapping to the upstream's result shape",
        )

    def _field(self, name: str, op: OperationMapping, key: str, data: object) -> object:
        expression = op.result.fields.get(key)
        return None if expression is None else _eval(name, expression, data)

    async def _list(
        self, name: OperationName, values: dict[str, str | int | None]
    ) -> list[dict[str, object]]:
        op = self._operation(name)
        items: list[dict[str, object]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            payload = await self._call(name, op, {**values, "cursor": cursor})
            page = _eval(name, op.result.items or "@", payload)
            try:
                page_items = _objects(page, "items", required=True)
            except _Shape as exc:
                raise _unavailable(
                    f"upstream result for {name} does not match the mapping: items "
                    f"(JMESPath {op.result.items!r}) — expected {exc.expected}",
                    hint="update the mapping to the upstream's result shape",
                ) from None
            items.extend(page_items)
            if len(items) > MAX_ITEMS:
                raise _unavailable(f"upstream returned more than {MAX_ITEMS} items for {name}")
            if op.pagination is None:
                break
            raw = _eval(name, op.pagination.cursor, payload)
            if raw in (None, ""):
                break
            try:
                cursor = _name(raw, "cursor")
            except _Shape:
                raise _unavailable(
                    f"upstream result for {name} does not match the mapping: pagination "
                    f"cursor (JMESPath {op.pagination.cursor!r}) — expected a string"
                ) from None
            if cursor in seen:
                break  # the upstream is looping; stop rather than spin
            seen.add(cursor)
        return items

    def _list_names(
        self, name: str, op: OperationMapping, items: list[dict[str, object]]
    ) -> list[str]:
        out: list[str] = []
        for item in items:
            try:
                out.append(_name(self._field(name, op, "name", item), "name"))
            except _Shape as exc:
                raise self._shape_error(name, op, "name", exc) from None
        return out

    def _base_values(self) -> dict[str, str | int | None]:
        return {"om_service": self.om_service}

    async def list_catalogs(self) -> list[str]:
        items = await self._list("list_catalogs", self._base_values())
        return self._list_names("list_catalogs", self._mapping.operations["list_catalogs"], items)

    async def list_schemas(self, catalog: str | None) -> list[str]:
        items = await self._list("list_schemas", {**self._base_values(), "catalog": catalog})
        return self._list_names("list_schemas", self._mapping.operations["list_schemas"], items)

    async def list_tables(self, catalog: str | None, schema: str) -> list[TableRef]:
        name: OperationName = "list_tables"
        values = {**self._base_values(), "catalog": catalog, "schema": schema}
        items = await self._list(name, values)
        op = self._mapping.operations[name]
        refs: list[TableRef] = []
        for item in items:
            key = "name"
            try:
                table = _name(self._field(name, op, "name", item), "name")
                key = "catalog"
                cat = _opt_name(self._field(name, op, "catalog", item), "catalog")
                key = "schema"
                sch = _opt_name(self._field(name, op, "schema", item), "schema")
            except _Shape as exc:
                raise self._shape_error(name, op, key, exc) from None
            refs.append(TableRef(catalog=cat or catalog, schema=sch or schema, name=table))
        return refs

    def _table_values(self, ref: TableRef) -> dict[str, str | int | None]:
        return {
            **self._base_values(),
            "catalog": ref.catalog,
            "schema": ref.schema_,
            "name": ref.name,
            "full_name": ref.full_name,
        }

    async def _table_payload(
        self, name: OperationName, ref: TableRef
    ) -> tuple[OperationMapping, object]:
        op = self._operation(name)
        return op, await self._call(name, op, self._table_values(ref))

    async def describe_table(self, ref: TableRef) -> TableDescription:
        name: OperationName = "describe_table"
        op = self._operation(name)
        values: dict[str, str | int | None] = {**self._table_values(ref), "column_offset": None}
        payload = await self._call(name, op, values)
        key = "columns"
        try:
            raw_columns = _objects(self._field(name, op, key, payload), "columns[]", required=True)
            raw_columns, truncated = await self._more_columns(
                name, op, values, payload, raw_columns
            )
            columns = [self._column(c, i) for i, c in enumerate(raw_columns)]
            key = "comment"
            comment = self._untrusted(self._field(name, op, key, payload), "comment")
            key = "constraints"
            constraints = self._constraints(self._field(name, op, key, payload))
            key = "partition_columns"
            partitions = _names(self._field(name, op, key, payload), key)
            key = "tags"
            tags = _names(self._field(name, op, key, payload), key)
            key = "owners"
            owners = _names(self._field(name, op, key, payload), key)
            key = "properties"
            properties = _strmap(self._field(name, op, key, payload), key)
            key = "storage_location"
            location = _opt_name(self._field(name, op, key, payload), key)
            key = "version"
            version = self._version(name, op, payload)
        except _Shape as exc:
            raise self._shape_error(name, op, key, exc) from None
        if truncated:
            # Surfaced, never silent: the agent sees the column list is incomplete.
            properties[COLUMNS_TRUNCATED] = "true"
        return TableDescription(
            ref=ref,
            comment=comment,
            columns=columns,
            partition_columns=partitions,
            constraints=constraints,
            tags=tags,
            owners=owners,
            properties=properties,
            storage_location=location,
            version=version,
        )

    async def _more_columns(
        self,
        name: OperationName,
        op: OperationMapping,
        values: dict[str, str | int | None],
        payload: object,
        columns: list[dict[str, object]],
    ) -> tuple[list[dict[str, object]], bool]:
        """Follow ``column_pagination``; returns the columns and whether the list is cut."""
        paging = op.column_pagination
        last = payload
        if paging is not None:
            pages = 1
            while _eval(name, paging.more, last):
                if pages >= MAX_PAGES or len(columns) >= MAX_ITEMS:
                    return columns, True
                last = await self._call(name, op, {**values, "column_offset": len(columns)})
                page = _objects(self._field(name, op, "columns", last), "columns[]", required=True)
                if not page:
                    return columns, True  # upstream says "more" but sends nothing
                columns = [*columns, *page]
                pages += 1
            if len(columns) > MAX_ITEMS:
                raise _Shape("columns[]", f"at most {MAX_ITEMS} items")
        return columns, bool(self._field(name, op, "columns_truncated", last))

    def _untrusted(self, value: object, path: str) -> UntrustedText | None:
        if value is None or value == "":
            return None
        return UntrustedText(text=_text(value, path), source=self._source)

    def _column(self, raw: dict[str, object], index: int) -> ColumnInfo:
        path = f"columns[{index}]"
        position = _int(raw.get("position"), f"{path}.position")
        return ColumnInfo(
            name=_name(raw.get("name"), f"{path}.name"),
            data_type=_name(raw.get("data_type"), f"{path}.data_type"),
            nullable=_bool(raw.get("nullable"), f"{path}.nullable", True),
            comment=self._untrusted(raw.get("comment"), f"{path}.comment"),
            tags=_names(raw.get("tags"), f"{path}.tags"),
            position=position if position is not None else index + 1,
        )

    def _constraints(self, value: object) -> list[Constraint]:
        out: list[Constraint] = []
        for i, raw in enumerate(_objects(value, "constraints[]", required=False)):
            path = f"constraints[{i}]"
            kind = _name(raw.get("kind"), f"{path}.kind").strip().lower()
            if kind not in ("primary_key", "foreign_key"):
                continue  # UNIQUE, CHECK, ...: not modelled
            columns = _names(raw.get("columns"), f"{path}.columns")
            if kind == "primary_key":
                out.append(Constraint(kind="primary_key", columns=columns))
                continue
            ref_columns: list[str] = []
            ref_table: TableRef | None = None
            for ref in _names(raw.get("ref_columns"), f"{path}.ref_columns"):
                table, column = _dotted_ref(ref)
                ref_table = ref_table or table
                ref_columns.append(column)
            explicit = _opt_name(raw.get("ref_table"), f"{path}.ref_table")
            if explicit is not None:
                ref_table, _ = _dotted_ref(explicit + ".x")
            out.append(
                Constraint(
                    kind="foreign_key",
                    columns=columns,
                    ref_table=ref_table,
                    ref_columns=ref_columns,
                )
            )
        return out

    def _version(self, name: str, op: OperationMapping, payload: object) -> TableVersion:
        return TableVersion(
            version=_int(self._field(name, op, "version", payload), "version"),
            committed_at=_datetime(self._field(name, op, "committed_at", payload), "committed_at"),
        )

    async def get_table_version(self, ref: TableRef) -> TableVersion:
        name: OperationName = "get_table_version"
        op, payload = await self._table_payload(name, ref)
        try:
            return self._version(name, op, payload)
        except _Shape as exc:
            raise self._shape_error(name, op, "version", exc) from None

    async def profile_table(self, ref: TableRef, policy: ProfilePolicy) -> TableProfile:
        """Aggregates only (FR-SRC-09).

        Upstream min/max/top-k are never read, whatever ``policy`` says, so no sample values
        leak through an upstream with weaker privacy rules than Lakeloom's.
        """
        del policy
        name: OperationName = "profile_table"
        op, payload = await self._table_payload(name, ref)
        key = "row_count"
        try:
            row_count = _int(self._field(name, op, key, payload), key, required=True)
            key = "columns"
            columns = [
                ColumnProfile(
                    name=_name(c.get("name"), f"columns[{i}].name"),
                    null_rate=_float(c.get("null_rate"), f"columns[{i}].null_rate"),
                    distinct_count=_int(c.get("distinct_count"), f"columns[{i}].distinct_count"),
                )
                for i, c in enumerate(
                    _objects(self._field(name, op, key, payload), "columns[]", required=True)
                )
            ]
        except _Shape as exc:
            raise self._shape_error(name, op, key, exc) from None
        return TableProfile(ref=ref, row_count=row_count or 0, columns=columns)


def build_source(config: McpSourceConfig, settings: Settings) -> McpUpstreamSource:
    """Registry factory (FR-SRC-06): loads and validates the mapping; ``start()`` connects."""
    return McpUpstreamSource(config, load_mapping(config.mapping))
