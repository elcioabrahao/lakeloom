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

## Consequences

New connectors need no core changes. The base install runs without the `delta` extra; the
Delta connector reports a clear "install lakeloom[delta]" error instead.
