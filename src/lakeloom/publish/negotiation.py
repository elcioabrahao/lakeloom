"""``Accept`` negotiation (RFC 9110 section 12.5.1) over the media types Lakeloom publishes."""

from collections.abc import Sequence
from typing import Literal

from lakeloom.rdf.canonical import ExportFormat

__all__ = ["FORMAT_BY_MEDIA", "MediaType", "choose"]

MediaType = Literal[
    "text/turtle",
    "application/ld+json",
    "application/rdf+xml",
    "application/n-triples",
    "text/markdown",
    "application/json",
]

FORMAT_BY_MEDIA: dict[MediaType, ExportFormat] = {
    "text/turtle": "turtle",
    "application/ld+json": "jsonld",
    "application/rdf+xml": "rdfxml",
    "application/n-triples": "ntriples",
}


def _parse(accept: str) -> list[tuple[str, float]] | None:
    """``(range, q)`` pairs, or ``None`` when no range is usable (treated as no preference)."""
    ranges: list[tuple[str, float]] = []
    for part in accept.split(","):
        head, *params = (p.strip() for p in part.split(";"))
        rng = head.lower()
        if rng.count("/") != 1 or not all(rng.split("/")):
            continue  # malformed range: ignored, never an error
        q = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = min(max(float(value.strip()), 0.0), 1.0)
                except ValueError:
                    q = 1.0
        ranges.append((rng, q))
    return ranges or None


def _specificity(rng: str, media: str) -> int:
    """3 for an exact match, 2 for ``type/*``, 1 for ``*/*``, 0 for no match."""
    if rng == media:
        return 3
    if rng == "*/*":
        return 1
    if rng.endswith("/*") and media.startswith(rng[:-1]):
        return 2
    return 0


def choose(accept: str | None, offered: Sequence[MediaType]) -> MediaType | None:
    """The offered media type with the highest q; ties keep the order of ``offered``.

    The most specific matching range decides a type's q (so ``text/turtle;q=0, */*`` refuses
    turtle only). Absent, empty or entirely malformed headers select ``offered[0]``; ``None``
    means nothing offered is acceptable (the caller answers 406).
    """
    if not offered:
        return None
    ranges = _parse(accept) if accept and accept.strip() else None
    if ranges is None:
        return offered[0]
    best: MediaType | None = None
    best_q = 0.0
    for media in offered:
        matches = [(_specificity(r, media), q) for r, q in ranges if _specificity(r, media)]
        if not matches:
            continue
        q = max(matches)[1]  # highest specificity wins (max by specificity first)
        if q > best_q:
            best, best_q = media, q
    return best
