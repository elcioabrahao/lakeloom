# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingTypeStubs=false, reportAttributeAccessIssue=false
"""Streaming table profiles with privacy controls (FR-SRC-08, FR-SRC-09).

Profiles are computed where the data lives: the dataset is scanned once, batch by batch
(never materialised as a whole table), and only aggregates leave this module. Privacy
rules (ADR 0008):

* a column is *sensitive* when any of its tags matches a ``policy.sensitive_tags`` glob;
  sensitive columns never expose values: no top-k and no min/max of any type;
* with ``policy.samples`` off (the default) no top-k is produced and string-like columns
  get no min/max; numeric, date and timestamp min/max are aggregates and stay;
* distinct values are tracked exactly up to ``low_cardinality + 1`` per column; past that
  the column reports ``distinct_count=None`` ("more than low_cardinality") and no top-k;
* rendered values are truncated to ``value_max_length`` characters plus ``…``.
"""

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from lakeloom.sources.base import ProfilePolicy
from lakeloom.sources.models import ColumnProfile, TableDescription, TableProfile

__all__ = ["profile_arrow_dataset"]

_ELLIPSIS = "…"


def _is_string(t: pa.DataType) -> bool:
    return pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t)


def _is_orderable(t: pa.DataType) -> bool:
    return (
        pa.types.is_integer(t)
        or pa.types.is_floating(t)
        or pa.types.is_decimal(t)
        or pa.types.is_date(t)
        or pa.types.is_timestamp(t)
        or pa.types.is_time(t)
        or _is_string(t)
    )


def _is_countable(t: pa.DataType) -> bool:
    return _is_orderable(t) or pa.types.is_boolean(t)


def _render(value: Any) -> str:
    iso = getattr(value, "isoformat", None)
    return str(iso()) if callable(iso) else str(value)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _ELLIPSIS


@dataclass
class _ColumnState:
    name: str
    dtype: pa.DataType
    sensitive: bool
    want_minmax: bool
    want_counts: bool
    nulls: int = 0
    min: Any = None
    max: Any = None
    counts: dict[Any, int] | None = field(default_factory=dict)

    def update(self, array: pa.Array | pa.ChunkedArray) -> None:
        self.nulls += array.null_count
        if self.want_minmax and len(array) > array.null_count:
            mm = pc.min_max(array)
            lo, hi = mm["min"].as_py(), mm["max"].as_py()
            if lo is not None and (self.min is None or lo < self.min):
                self.min = lo
            if hi is not None and (self.max is None or hi > self.max):
                self.max = hi

    def update_counts(self, array: pa.Array | pa.ChunkedArray, cap: int) -> None:
        if self.counts is None:
            return
        for item in pc.value_counts(array.drop_null()).to_pylist():
            value = item["values"]
            self.counts[value] = self.counts.get(value, 0) + item["counts"]
        if len(self.counts) > cap:
            self.counts = None  # high cardinality: stop tracking, bounds memory


def _matches(tags: list[str], patterns: list[str]) -> bool:
    return any(fnmatchcase(tag, pattern) for tag in tags for pattern in patterns)


def profile_arrow_dataset(
    dataset: Any,
    description: TableDescription,
    policy: ProfilePolicy,
    *,
    batch_size: int = 65536,
) -> TableProfile:
    """Profile ``dataset`` in one streaming pass. Synchronous: wrap in ``to_thread``."""
    schema = dataset.schema
    cap = policy.low_cardinality + 1
    states: list[_ColumnState] = []
    for info in description.columns:
        if info.name not in schema.names:
            continue
        dtype = schema.field(info.name).type
        sensitive = _matches(info.tags, policy.sensitive_tags)
        minmax_ok = _is_orderable(dtype) and not sensitive
        if _is_string(dtype) and not policy.samples:
            minmax_ok = False
        states.append(
            _ColumnState(
                name=info.name,
                dtype=dtype,
                sensitive=sensitive,
                want_minmax=minmax_ok,
                want_counts=_is_countable(dtype),
                counts={} if _is_countable(dtype) else None,
            )
        )
    columns = [s.name for s in states]
    row_count = 0
    for batch in dataset.to_batches(columns=columns, batch_size=batch_size):
        row_count += batch.num_rows
        for state in states:
            array = batch.column(state.name)
            state.update(array)
            state.update_counts(array, cap)
    out: list[ColumnProfile] = []
    for state in states:
        distinct = len(state.counts) if state.counts is not None else None
        if state.sensitive:
            withheld = "sensitive"
        elif not policy.samples:
            withheld = "samples_disabled"
        else:
            withheld = None
        top_k: list[tuple[str, int]] | None = None
        if withheld is None and state.counts is not None:
            ranked = sorted(
                ((_render(v), c) for v, c in state.counts.items()),
                key=lambda t: (-t[1], t[0]),
            )[: policy.top_k]
            top_k = [(_truncate(v, policy.value_max_length), c) for v, c in ranked]
        lo = _render(state.min) if state.min is not None else None
        hi = _render(state.max) if state.max is not None else None
        if _is_string(state.dtype):
            lo = _truncate(lo, policy.value_max_length) if lo is not None else None
            hi = _truncate(hi, policy.value_max_length) if hi is not None else None
        out.append(
            ColumnProfile(
                name=state.name,
                null_rate=state.nulls / row_count if row_count else None,
                distinct_count=distinct,
                min=lo,
                max=hi,
                top_k=top_k,
                values_withheld=withheld,
            )
        )
    return TableProfile(ref=description.ref, row_count=row_count, columns=out)
