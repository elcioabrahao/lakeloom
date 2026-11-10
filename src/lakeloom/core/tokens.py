"""Token estimate and whole-section truncation for Markdown output (FR-CTX-06)."""

import math
from collections.abc import Sequence

__all__ = ["TRUNCATION_NOTE", "token_estimate", "truncate_markdown"]

TRUNCATION_NOTE = "> _truncated: further sections omitted_"
_SEP = "\n\n"


def token_estimate(text: str) -> int:
    """``ceil(len(text) / 4)`` (ADR 0009)."""
    return math.ceil(len(text) / 4)


def truncate_markdown(sections: Sequence[str], max_tokens: int | None) -> tuple[str, bool]:
    """Join whole sections in order, dropping trailing ones that exceed ``max_tokens``.

    A section is never cut. The first section (the document title) is always kept, so the
    output stays a well-formed document even for tiny budgets. When anything is dropped
    the result ends with a truncation note and the flag is ``True``.
    """
    full = _SEP.join(sections)
    if max_tokens is None or token_estimate(full) <= max_tokens:
        return full, False
    kept: list[str] = []
    for section in sections:
        candidate = _SEP.join([*kept, section, TRUNCATION_NOTE])
        if kept and token_estimate(candidate) > max_tokens:
            break
        kept.append(section)
    return _SEP.join([*kept, TRUNCATION_NOTE]), True
