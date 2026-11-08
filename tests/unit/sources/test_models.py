import pytest
from pydantic import ValidationError

from lakeloom.sources.models import Page, TableRef, TableVersion


def test_table_ref_full_name() -> None:
    assert TableRef(catalog="c", schema="s", name="t").full_name == "c.s.t"
    assert TableRef(catalog=None, schema="s", name="t").full_name == "s.t"
    assert TableRef.model_validate({"schema_": "s", "name": "t"}).schema_ == "s"


def test_models_frozen() -> None:
    ref = TableRef(catalog=None, schema="s", name="t")
    with pytest.raises(ValidationError):
        ref.name = "x"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        TableVersion(version=1, committed_at=None).version = 2  # pyright: ignore[reportAttributeAccessIssue]


def test_page_generic() -> None:
    page = Page[TableRef](items=[TableRef(catalog=None, schema="s", name="t")], next_cursor=None)
    assert page.items[0].full_name == "s.t"
