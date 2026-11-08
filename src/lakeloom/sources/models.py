"""Source-neutral metadata models (FR-SRC-02, FR-SRC-03, FR-SRC-04). All frozen."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lakeloom.untrusted import UntrustedText

__all__ = [
    "ColumnInfo",
    "ColumnProfile",
    "Constraint",
    "Page",
    "TableDescription",
    "TableProfile",
    "TableRef",
    "TableVersion",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)


class TableRef(_Frozen):
    """A table address. ``schema`` is exposed as ``schema_`` (alias ``schema``)."""

    catalog: str | None = None
    schema_: str = Field(alias="schema")
    name: str

    @property
    def full_name(self) -> str:
        parts = (
            [self.catalog, self.schema_, self.name] if self.catalog else [self.schema_, self.name]
        )
        return ".".join(parts)


class ColumnInfo(_Frozen):
    name: str
    data_type: str
    nullable: bool
    comment: UntrustedText | None = None
    tags: list[str] = []
    position: int


class Constraint(_Frozen):
    kind: Literal["primary_key", "foreign_key"]
    columns: list[str]
    ref_table: TableRef | None = None
    ref_columns: list[str] = []


class TableVersion(_Frozen):
    version: int | None = None
    committed_at: datetime | None = None


class TableDescription(_Frozen):
    ref: TableRef
    comment: UntrustedText | None = None
    columns: list[ColumnInfo]
    partition_columns: list[str] = []
    constraints: list[Constraint] = []
    tags: list[str] = []
    owners: list[str] = []
    properties: dict[str, str] = {}
    storage_location: str | None = None
    version: TableVersion


class ColumnProfile(_Frozen):
    name: str
    null_rate: float | None = None
    distinct_count: int | None = None
    min: str | None = None
    max: str | None = None
    top_k: list[tuple[str, int]] | None = None
    values_withheld: Literal["sensitive", "samples_disabled"] | None = None


class TableProfile(_Frozen):
    ref: TableRef
    row_count: int
    columns: list[ColumnProfile]


class Page[T](_Frozen):
    """A cursor-paginated list (FR-AGT-02)."""

    items: list[T]
    next_cursor: str | None = None
