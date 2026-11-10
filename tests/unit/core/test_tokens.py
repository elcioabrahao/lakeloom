"""Token estimate and truncation (FR-CTX-06)."""

from lakeloom.core.tokens import TRUNCATION_NOTE, token_estimate, truncate_markdown


def test_token_estimate_is_ceil_len_over_four() -> None:
    assert token_estimate("") == 0
    assert token_estimate("abcd") == 1
    assert token_estimate("abcde") == 2


def test_no_limit_keeps_everything() -> None:
    text, truncated = truncate_markdown(["# a", "b"], None)
    assert (text, truncated) == ("# a\n\nb", False)


def test_fits_budget_is_not_truncated() -> None:
    assert truncate_markdown(["# a", "b"], 100) == ("# a\n\nb", False)


def test_drops_trailing_sections_whole_and_notes_it() -> None:
    sections = ["# title", "## one\n" + "x" * 80, "## two\n" + "y" * 80, "## three\n" + "z" * 80]
    text, truncated = truncate_markdown(sections, 60)
    assert truncated
    assert text.endswith(TRUNCATION_NOTE)
    assert token_estimate(text) <= 60
    body = text.removesuffix("\n\n" + TRUNCATION_NOTE).split("\n\n")
    assert all(part in sections for part in body)
    assert body == sections[: len(body)]


def test_first_section_is_always_kept() -> None:
    text, truncated = truncate_markdown(["# " + "t" * 100, "more"], 5)
    assert truncated
    assert text.startswith("# ttt")
    assert "more" not in text
