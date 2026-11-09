"""Quoting of untrusted text (NFR-SEC-14).

Lake comments, upstream MCP output and imported files are data, never instructions.
Markdown output renders them as an escaped blockquote under an explicit header; structured
output flags them with :class:`UntrustedText`.
"""

from typing import Literal

from pydantic import BaseModel

__all__ = ["MAX_QUOTED_CHARS", "UntrustedText", "code_span", "quote_untrusted", "single_line"]

MAX_QUOTED_CHARS = 2000


class UntrustedText(BaseModel):
    """Text from outside Lakeloom's trust boundary, flagged for structured output."""

    text: str
    source: str
    untrusted: Literal[True] = True


def _escape(text: str) -> str:
    return text.replace("`", "\\`").replace("<", "&lt;").replace(">", "&gt;")


def single_line(text: str) -> str:
    """Collapse every line boundary and whitespace run to one space.

    Uses the same boundaries as :func:`quote_untrusted` (``splitlines``: ``\n``, ``\r``,
    ``\v``, ``\f``, ``\x1c``-``\x1e``, U+0085, U+2028, U+2029) plus ``str.split``'s
    whitespace, so interpolated text can never start a new Markdown line or heading.
    """
    return " ".join(" ".join(text.splitlines()).split())


def code_span(name: str) -> str:
    """An untrusted identifier (table, column, tag, source name) as inert inline code.

    Backticks become ``'`` so the span cannot be closed early, and line boundaries are
    collapsed by :func:`single_line`, so a hostile name stays inside one list item.
    """
    return "`" + single_line(name.replace("`", "'")) + "`"


def quote_untrusted(text: str, *, source: str) -> str:
    """Render ``text`` as a Markdown blockquote that cannot break out of its quote.

    Backticks and angle brackets are escaped (so fences, HTML and a forged quote header
    stay inert), every line is prefixed ``> ``, and the text is cut to 2000 characters
    with an ellipsis.
    """
    if len(text) > MAX_QUOTED_CHARS:
        text = text[: MAX_QUOTED_CHARS - 1] + "…"
    safe_source = _escape(" ".join(source.split()))
    lines = [f"> [untrusted text from {safe_source}]"]
    # splitlines() breaks on every line boundary a renderer might honour (\r, \v, \f,
    # \x1c-\x1e, U+0085, U+2028, U+2029), so no line can start outside the quote.
    body = _escape(text).splitlines() or [""]
    lines.extend(f"> {line}" for line in body)
    return "\n".join(lines)
