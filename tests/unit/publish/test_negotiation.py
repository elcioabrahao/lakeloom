"""Accept-header negotiation (FR-PUB-02)."""

import pytest

from lakeloom.publish.negotiation import FORMAT_BY_MEDIA, MediaType, choose

ALL: list[MediaType] = [
    "text/turtle",
    "application/ld+json",
    "application/rdf+xml",
    "application/n-triples",
]
MD_JSON: list[MediaType] = ["text/markdown", "application/json"]


@pytest.mark.parametrize(
    ("accept", "offered", "expected"),
    [
        ("text/turtle;q=0.9, application/ld+json", ALL, "application/ld+json"),
        ("*/*;q=0.1", ALL, "text/turtle"),
        ("application/*", ["text/turtle", "application/ld+json"], "application/ld+json"),
        ("text/html", ALL, None),
        ("text/html, application/xml;q=0.5", MD_JSON, None),
        (None, ALL, "text/turtle"),
        ("", ALL, "text/turtle"),
        ("   ", ALL, "text/turtle"),
        ("*/*", MD_JSON, "text/markdown"),
        ("garbage", ALL, "text/turtle"),
        (";;;,,,q=", ALL, "text/turtle"),
        ("text/turtle;q=abc", ALL, "text/turtle"),
        ("application/ld+json;q=nan, text/turtle;q=0.5", ALL, "text/turtle"),
        ("application/ld+json;q=-1, text/turtle;q=0.1", ALL, "text/turtle"),
        ("application/ld+json;q=-1", ["application/ld+json"], None),
        (
            "application/json, text/plain, */*",
            ["text/markdown", "application/json"],
            "application/json",
        ),
        ("application/ld+json, */*", ALL, "application/ld+json"),
        ("application/*, */*", ALL, "application/ld+json"),
        ("text/turtle;q=0.5, application/ld+json;q=0.5, */*;q=0.5", ALL, "text/turtle"),
        ("application/ld+json;q=0", ["application/ld+json"], None),
        ("text/turtle;q=0, */*", ALL, "application/ld+json"),
        ("application/json;q=0.5, text/markdown;q=0.5", MD_JSON, "text/markdown"),
        ("application/json, text/markdown;q=0.8", MD_JSON, "application/json"),
        ("TEXT/Markdown", MD_JSON, "text/markdown"),
        ("text/*;q=0.2, application/json;q=0.9", MD_JSON, "application/json"),
        ("application/ld+json;q=2", ALL, "application/ld+json"),
    ],
)
def test_choose(accept: str | None, offered: list[MediaType], expected: str | None) -> None:
    assert choose(accept, offered) == expected


def test_specific_beats_wildcard_for_same_type() -> None:
    # text/turtle is explicitly refused even though */* would allow it
    assert choose("text/turtle;q=0, */*;q=1", ALL) == "application/ld+json"


def test_format_by_media() -> None:
    assert FORMAT_BY_MEDIA == {
        "text/turtle": "turtle",
        "application/ld+json": "jsonld",
        "application/rdf+xml": "rdfxml",
        "application/n-triples": "ntriples",
    }
