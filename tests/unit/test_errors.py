from typing import get_args

from lakeloom.errors import ErrorCode, LakeloomError


def test_fr_agt_05_error_to_dict() -> None:
    err = LakeloomError("not_found", "no such table", "list tables first")
    assert err.to_dict() == {
        "error": {"code": "not_found", "message": "no such table", "hint": "list tables first"}
    }
    assert str(err) == "no such table"
    assert err.code == "not_found"
    assert err.hint == "list tables first"


def test_error_codes_complete() -> None:
    assert set(get_args(ErrorCode)) == {
        "not_found",
        "forbidden",
        "conflict",
        "validation_error",
        "upstream_unavailable",
        "rate_limited",
    }
