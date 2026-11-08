"""Quoting of untrusted text (NFR-SEC-14).

Lake comments, upstream MCP output and imported files are data, never instructions.
Markdown output renders them as an escaped blockquote under an explicit header; structured
output flags them with :class:`UntrustedText`.
"""

from typing import Literal

from pydantic import BaseModel

__all__ = ["MAX_QUOTED_CHARS", "UntrustedText", "quote_untrusted"]

MAX_QUOTED_CHARS = 2000


class UntrustedText(BaseModel):
    """Text from outside Lakeloom's trust boundary, flagged for structured output."""

    text: str
    source: str
    untrusted: Literal[True] = True


def _escape(text: str) -> str:
    return text.replace("`", "\\`").replace("<", "&lt;").replace(">", "&gt;")


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
    lines.extend(f"> {line}" for line in _escape(text.replace("\r\n", "\n")).split("\n"))
    return "\n".join(lines)
