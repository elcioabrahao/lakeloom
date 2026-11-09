# 0008. Lake source design

- Status: accepted
- Date: 2026-11-08
- Requirements: FR-SRC-01, FR-SRC-05, FR-SRC-06, FR-SRC-08, FR-SRC-09, FR-AGT-05, NFR-SEC-13, NFR-SEC-14

## Context

M1a adds read-only lake connectors (Delta, Unity Catalog, upstream MCP, plugins) that feed
scans of table metadata and profiles.

## Decision

- **Protocol.** Connectors implement `lakeloom.sources.base.LakeSource` (async, read-only)
  and declare `SourceCapabilities` (profiling, versions, constraints). Models are frozen
  pydantic classes; lake-provided comments are `UntrustedText` and rendered with
  `quote_untrusted` (NFR-SEC-14).
- **Configuration.** Operators write a YAML file (`sources:` list discriminated by `type`:
  `delta`, `unity_catalog`, `mcp`; any other type is a plugin). Agents never supply sources
  or URLs (NFR-SEC-13). Credentials are referenced by environment-variable name
  (`token_env`) and resolved at build time, never stored in the model.
- **Plugins.** Unknown types are looked up in the `lakeloom.sources` entry-point group;
  the object is `Callable[[PluginSourceConfig, Settings], LakeSource]`. Built-ins live in
  modules exposing `build_source(config, settings)`. Unknown types raise `ConfigError`.
- **Unity Catalog over REST.** The connector uses the UC REST API through httpx rather than
  `databricks-sdk`, which keeps the base install small and works against Unity Catalog OSS.
  Cloud SDKs stay optional; `deltalake` + `pyarrow` live in the `delta` extra.
- **Errors.** `lakeloom.errors.LakeloomError` carries the six FR-AGT-05 codes with a hint;
  `to_dict()` is the tool error shape.
- **New settings.**

  | Variable | Default |
  |---|---|
    | `LAKELOOM_PROFILE_TOPK` | 10 |
    | `LAKELOOM_PROFILE_LOW_CARDINALITY` | 50 (distinct values) |
    | `LAKELOOM_PROFILE_VALUE_MAX_LENGTH` | 64 (characters) |
    | `LAKELOOM_SCAN_CONCURRENCY` | 8 |

- **Delta tags, owners and constraints.** Delta has no native tags, owners or constraints.
  Lakeloom resolves them from two mechanisms, in precedence order:

  1. **Operator overlay** in the source YAML, per table entry: `description`, `tags`,
     `owners`, `primary_key`, `foreign_keys` (column to `<schema>.<table>.<column>`),
     `column_tags`, `column_comments`. It needs no write access to the lake, so it works
     for read-only lakes, delta-rs-only pipelines and third-party tables, and stays
     operator-controlled (NFR-SEC-13). Overlay column names are validated against the table
     schema at describe time (unknown column gives `validation_error`). Overlay text is
     still `UntrustedText` (source `config:<source>`).
  2. **Conventions in the Delta log** (used when the overlay does not set the value):

  | Concept | Where | Format |
  |---|---|---|
  | Table description | Delta metadata `description` | free text |
  | Column comment | field metadata key `comment` | free text |
  | Column tags | field metadata key `lakeloom.tags` | comma-separated, e.g. `PII.Email` |
  | Table tags | table configuration `lakeloom.tags` | comma-separated |
  | Primary key | configuration `lakeloom.primaryKey` | comma-separated columns |
  | Foreign key | configuration `lakeloom.foreignKey.<column>` | `<schema>.<table>.<column>`, resolved in the same catalog |
  | Owners | configuration `lakeloom.owners` | comma-separated |

  The log conventions are settable by Spark/Databricks (`ALTER TABLE ... SET TBLPROPERTIES`
  for table keys; field metadata needs a schema rewrite). delta-rs 1.6 (delta-kernel)
  cannot write `lakeloom.*` table properties (it rejects unknown properties) although it
  reads them, so delta-rs-only pipelines should use the overlay. The fixture generator
  patches the keys into commit 0 of the log to exercise the log path.

  Comments and descriptions are `UntrustedText` (NFR-SEC-14). Malformed foreign key values
  are ignored rather than failing the table. Delta types are rendered Spark-style (`long`
  becomes `bigint`, `integer` becomes `int`).

- **Unity Catalog REST contract (FR-SRC-02).** Verified against Unity Catalog OSS v0.7.0
  (`unitycatalog/unitycatalog:v0.7.0`, OpenAPI at
  `https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/api/all.yaml`, and by running
  the image). Base path `/api/2.1/unity-catalog`: `GET /catalogs`,
  `GET /schemas?catalog_name=`, `GET /tables?catalog_name=&schema_name=`,
  `GET /tables/{full_name}`; lists return `next_page_token` (followed with `page_token`);
  a missing object is HTTP 404 with `{"error_code": "TABLE_NOT_FOUND", ...}`. Recorded
  responses live in `tests/fixtures/uc/`. Mapping:
  - `columns[].type_text` -> data type, `partition_index` -> partition columns,
    `comment` -> `UntrustedText(source="unity_catalog:<source name>")`, `owner` -> owners,
    `storage_location` kept as is.
  - OSS v0.7.0 has no tag or constraint API and no `table_constraints`. The connector
    therefore reads the same `lakeloom.*` conventions as the Delta connector from table
    `properties`: `lakeloom.tags`, `lakeloom.owners`, `lakeloom.primaryKey`,
    `lakeloom.foreignKey.<column>` (`<schema>.<table>.<column>`) and
    `lakeloom.columnTags.<column>` (comma-separated). Declared `table_constraints`
    (Databricks-hosted UC) take precedence over the properties.
  - `read_storage: true` makes `get_table_version` / `profile_table` delegate to
    `DeltaStorageSource` for DELTA tables at `file://`, `s3://`, `abfss://` or `gs://`
    locations (UC column tags are passed on as overlay so the sensitive-tag rule holds).
    Anything else yields `TableVersion(None, None)` and a `validation_error` on profiling.
    `storage_root_map` (`{"file:///lake": "/host/path"}`, prefix rewrite) lets Lakeloom
    reach data at a different path than UC reports; the contract tests use it.
  - The bearer token is `os.environ[token_env]` when set, sent only in the Authorization
    header, never logged or put in errors. 404 is `not_found`; 401/403 `forbidden` (hint
    names `token_env`); 5xx, timeouts and connect errors `upstream_unavailable`.

- **Profiling and privacy (FR-SRC-08, FR-SRC-09).** Profiles stream record batches once
  (`profile_arrow_dataset`); the table is never materialised. Rules:
  - `null_rate` = nulls / rows (`None` for an empty table).
  - Distinct values are tracked exactly up to `low_cardinality + 1` per column; past that
    the column reports `distinct_count=None` meaning "more than `low_cardinality`" and no
    `top_k`. This bounds memory.
  - A column is sensitive when any tag matches a `sensitive_tags` glob (`fnmatch`).
    Sensitive columns report no `top_k` and no min/max of any type (a numeric min/max of
    a PII column such as a birth year is still a sample); `values_withheld="sensitive"`.
  - With `samples` off (default) `top_k` is `None` and string min/max are withheld
    (`values_withheld="samples_disabled"`). Numeric, date and timestamp min/max of
    non-sensitive columns are aggregates and are always reported.
  - `top_k` orders by count desc then value asc, excludes nulls, renders values as strings
    (ISO for dates/timestamps) truncated to `value_max_length` plus `…`.
  - Settings: `LAKELOOM_PROFILE_TOPK` (10), `LAKELOOM_PROFILE_LOW_CARDINALITY` (50),
    `LAKELOOM_PROFILE_VALUE_MAX_LENGTH` (64).

- **Upstream MCP adapter (FR-SRC-05).** `lakeloom.sources.mcp_adapter.McpUpstreamSource`
  is an MCP *client* of an operator-configured upstream (`type: mcp`, `transport:
  streamable_http` with `url`, or `stdio` with `command` argv). Bearer token from at most one
  of `token_env` (variable name) or `token_file` (path, read at `start()`; for compose the
  OM bot-token file); never logged or put in errors. A relative `mapping` / `token_file` is
  resolved against the sources YAML file's directory.
  - **Lifecycle.** `start()` connects (10 s timeout), runs `initialize` + `tools/list` and
    checks every mapped tool exists; it never raises. On failure the source stays
    registered with `available=False` and `last_error` (e.g. `upstream_unavailable`
    "upstream tool '<name>' not found", hint "update the mapping or the upstream server");
    every operation then raises that error. The connection lives in a dedicated asyncio
    task that owns the `AsyncExitStack` (the SDK transports are anyio task groups and must
    be exited in the task that entered them); `aclose()` signals it and waits. Tool calls
    time out after 30 s.
  - **Mapping format** (`McpMapping`, validated at load time, errors are `ConfigError`):

    ```yaml
    version: 1
    operations:              # list_catalogs | list_schemas | list_tables | describe_table
      <operation>:           #   | get_table_version | profile_table
        tool: <name from tools/list>
        arguments: {...}     # string leaves may hold placeholders
        result:
          items: <JMESPath>  # required for list operations: the list of items
          fields: {<field>: <JMESPath>}
        pagination: {cursor: <JMESPath>}  # list operations; needs a {cursor} argument
        not_found: <JMESPath>  # optional, evaluated on an isError result -> not_found
        column_pagination: {more: <JMESPath>}  # describe_table only; needs {column_offset}
    ```

    Placeholders: `{catalog}`, `{schema}`, `{name}`, `{full_name}`, `{cursor}` from the
    brief, plus `{om_service}` (the source's configured `om_service`) so one mapping serves
    any OM service, and `{column_offset}` (an integer: the number of columns received so far;
    describe_table with `column_pagination` only). Allowed per operation (checked at load): list_catalogs `om_service,
    cursor`; list_schemas `+catalog`; list_tables `+schema`; table operations `catalog,
    schema, name, full_name, om_service`. A leaf that is exactly one placeholder becomes the
    raw value, or is omitted when the value is absent (first page cursor, no catalog). An
    embedded placeholder is substituted as text and its value must not contain `"`, `\` or
    control characters (`validation_error`), so an agent-chosen name cannot break out of a
    JSON string such as OM's OpenSearch `queryFilter`.
    Fields: list_catalogs/list_schemas `name`; list_tables `name` (+ optional `catalog`,
    `schema`, defaulting to the request); describe_table `columns` (required; JMESPath
    yielding objects with `name`, `data_type`, `nullable`, `comment`, `tags`, `position`),
    `columns_truncated` (truthy on the last page = upstream dropped columns), `comment`, `tags`,
    `owners`, `partition_columns`, `constraints` (objects with `kind`
    `PRIMARY_KEY`/`FOREIGN_KEY` case-insensitive, `columns`, `ref_columns`, optional
    `ref_table`; dotted `ref_columns` like `svc.db.schema.table.col` yield the referenced
    table and column), `properties`, `storage_location`, `version`, `committed_at`;
    get_table_version `version`, `committed_at`; profile_table `row_count`, `columns`
    (`name`, `null_rate`, `distinct_count`). Unmapped operations are capability-off and raise
    `validation_error`.
  - **Column pages.** With `column_pagination`, describe_table repeats the call with
    `{column_offset}` = columns so far while `more` is truthy (the first call omits it).
    The description is never silently short: if the upstream stops early (empty page), the
    100-page / 10 000-column caps are hit, or `columns_truncated` holds on the last page, the
    columns received are returned with property `lakeloom.columns_truncated: "true"`.
  - **Results are untrusted (NFR-SEC-14).** Read from `structuredContent`, else the first
    text content JSON-decoded (at most 8 MiB; non-JSON or too-deep JSON is
    `upstream_unavailable` "upstream returned non-JSON for <operation>"; `NaN`/`Infinity` tokens
    are rejected too). Numbers must be finite, also when they arrive already decoded in
    `structuredContent`; a non-finite or non-integral count is a shape mismatch. Comments become
    `UntrustedText(source="mcp:<source name>")`; names, types and tags are data only,
    bounded to 1024 characters, lists to 10 000 items, pagination to 100 pages (a repeated
    cursor stops). Lakeloom only ever calls tools named in the mapping *and* present in
    `tools/list`; nothing in a result can choose a tool. A field of the wrong shape is
    `upstream_unavailable` naming the operation, field and JMESPath. An upstream error's
    text is never copied into Lakeloom's error message. profile_table never takes min/max or
    top-k from an upstream, so no sample values can leak past FR-SRC-09.
  - **OpenMetadata mapping** (`examples/mcp-mappings/openmetadata.yaml`, the working one).
    OM 2.1.0-rc1's `McpApplication` serves Streamable HTTP at `/mcp` with the bot token as
    bearer; no compose change was needed. `tools/list` on 2026-11-09 returned (`*` =
    required):

    | Tool | Input properties |
    |---|---|
    | `company_context` | `query`, `fqn`, `size`, `from`, `cursor` |
    | `search_metadata` | `query`, `entityType`, `queryFilter`, `from`, `cursor`, `size`, `includeDeleted`, `fields`, `includeAggregations`, `maxAggregationBuckets`, `excludeEntityTypes`, `ignorePersonaScope` |
    | `semantic_search` | `query`*, `filters`, `size`, `k`, `threshold`, `from`, `cursor`, `ignorePersonaScope` |
    | `get_entity_details` | `entityType`, `fqn`, `columnOffset`, `columnLimit`, `include`, `entities`, `query`, `format`, `passages` |
    | `get_persona_context` | `personaName`, `format`, `part` |
    | `get_user_context` | `includeOwnedSummary`, `includeFollowedSummary`, `ownedLimit`, `followedLimit`, `ownedFilter` |
    | `find_context` | `query`*, `size`, `format` |
    | `get_concept_context` | `entityType`*, `fqn`*, `format`, `query` |
    | `create_entity` | `entityType`*, `name`*, `description`, `displayName`, `owners`, `tags`, `domains`, `reviewers`, `extension`, `attributes` |
    | `describe_entity_type` | `entityType`* |
    | `patch_entity` | `entityType`*, `fqn`*, `patch`* |
    | `get_entity_lineage` | `entityType`*, `fqn`*, `upstreamDepth`, `downstreamDepth`, `includeColumnLineage`, `column`, `cursor`, `includeSql` |
    | `create_lineage` | `fromEntity`*, `toEntity`* |
    | `get_test_definitions` | `entityType`*, `testPlatform`, `after`, `cursor`, `limit` |
    | `create_test_case` | `name`*, `fqn`*, `columnName`, `entityType`, `testDefinitionName`*, `description`, `parameterValues`* |
    | `root_cause_analysis` | `fqn`*, `upstreamDepth`, `downstreamDepth`, `queryFilter`, `includeDeleted`, `includeColumnLineage`, `entityType`* |
    | `ontology_describe` | `resource`, `format`, `maxBytes` |

    Mapping: list_catalogs / list_schemas / list_tables use `search_metadata` with an
    OpenSearch `queryFilter` on `entityType`, `service.name.keyword` = `{om_service}`,
    `database.name.keyword` = `{catalog}`, `databaseSchema.name.keyword` = `{schema}`,
    `size: 50`, paginated by `nextCursor` (items `results[?entityType=='table']`, since table
    searches may also return `tableColumn` documents). describe_table uses
    `get_entity_details` with `entityType: table`, `fqn: "{om_service}.{full_name}"`:
    `description`, `tags[].tagFQN`, `owners[].name`, `columns[]` (`dataTypeDisplay`,
    `ordinalPosition`, `constraint != 'NOT_NULL'`), `tableConstraints[]`. OM drops columns
    past its response-size budget unless paged, flagging `columnsTruncated`/`hasMoreColumns`
    (verified live), so the mapping sends `columnOffset: "{column_offset}"`, `columnLimit:
    200` and pages on `hasMoreColumns`. `columnsTruncated` is not mapped, because OM sets it on
    every slice, the last one included; a missing table is
    `isError` with `statusCode: 404` -> `not_found`. OM's catalog = OM database, schema = OM
    databaseSchema. get_table_version and profile_table are not mapped (OM's entity
    `version` is a metadata revision, not a table version).
  - **Examples to verify.** `databricks.example.yaml` and `snowflake.example.yaml` start with
    `# EXAMPLE TO VERIFY — tool names are placeholders; run tools/list against your server
    and edit` and use `<tool-from-tools/list>` as every tool name; they are not tested
    against live servers.

## Consequences

New connectors need no core changes. The base install runs without the `delta` extra; the
Delta connector reports a clear "install lakeloom[delta]" error instead.
