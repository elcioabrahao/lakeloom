import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from rdflib import RDF, BNode, Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import SKOS, XSD

from lakeloom.errors import LakeloomError
from lakeloom.rdf.canonical import (
    EXTENSIONS,
    MEDIA_TYPES,
    ExportFormat,
    InputFormat,
    parse_rdf,
    serialize,
)

SRC = Path(__file__).resolve().parents[3] / "src"
EX = Namespace("https://example.org/m/")
ALL: tuple[ExportFormat, ...] = get_args(ExportFormat)
PARSEABLE: tuple[InputFormat, ...] = get_args(InputFormat)


def _triples() -> list[tuple]:
    b1, b2 = BNode(), BNode()
    return [
        (EX.a, RDF.type, SKOS.Concept),
        (EX.a, SKOS.prefLabel, Literal("Cliente", lang="pt")),
        (EX.a, SKOS.prefLabel, Literal("Customer", lang="en")),
        (EX.a, SKOS.definition, Literal("Ação — café ☕")),
        (EX.a, EX["count"], Literal(3, datatype=XSD.integer)),
        (EX.a, EX.restriction, b1),
        (b1, RDF.type, EX.Restriction),
        (b1, EX.onProperty, EX.p),
        (b1, EX.next, b2),
        (b2, EX.value, Literal("x")),
        (EX.b, SKOS.broader, EX.a),
    ]


def _graph(triples: list[tuple]) -> Graph:
    g = Graph()
    for t in triples:
        g.add(t)
    return g


def test_fr_git_03_same_graph_same_bytes_all_formats() -> None:
    t1 = _triples()
    t2 = list(reversed(t1))
    g1, g2 = _graph(t1), _graph(t2)
    g2.bind("zzz", EX)
    for fmt in ALL:
        assert serialize(g1, fmt, prefixes={"ex": str(EX)}) == serialize(
            g2, fmt, prefixes={"ex": str(EX)}
        )


def test_lf_and_trailing_newline() -> None:
    for fmt in ALL:
        out = serialize(_graph(_triples()), fmt)
        assert b"\r" not in out
        assert out.endswith(b"\n") and not out.endswith(b"\n\n")
        out.decode("utf-8")


def test_constants() -> None:
    assert MEDIA_TYPES["skos"] == "text/turtle"
    assert EXTENSIONS == {
        "turtle": "ttl",
        "rdfxml": "owl",
        "ntriples": "nt",
        "jsonld": "jsonld",
        "skos": "skos.ttl",
    }


@pytest.mark.parametrize(
    ("fmt", "data", "prefix"),
    [
        ("turtle", "@prefix ex: <https://e.org/> .\n\nex:a ex:b .\nex:c ex:d .\n", "line 3"),
        ("turtle", "ex:a ex:b ex:c .\n", "line 1"),
        ("rdfxml", "<?xml version='1.0'?>\n<a>\n\n  <b>\n</a>", "line 5, column 2"),
        ("ntriples", "<urn:a> <urn:b> <urn:c> .\n\n<urn:a> bad .\n", "line 3"),
        ("jsonld", '{\n "a": ,\n}', "line 2, column 7"),
    ],
)
def test_parse_error_reports_line(fmt: InputFormat, data: str, prefix: str) -> None:
    with pytest.raises(LakeloomError) as ei:
        parse_rdf(data, fmt)
    assert ei.value.code == "validation_error"
    assert ei.value.message.startswith(f"Invalid {fmt} input at {prefix}:"), ei.value.message
    assert ei.value.hint


def test_rdfxml_rejects_non_ncname_predicate() -> None:
    g = Graph()
    g.add((EX.a, URIRef("https://e.org/123"), Literal("x")))
    with pytest.raises(LakeloomError) as ei:
        serialize(g, "rdfxml")
    assert ei.value.code == "validation_error" and ei.value.hint
    serialize(g, "turtle")  # other formats still work


def test_user_prefix_named_like_auto_prefix() -> None:
    g = Graph()
    g.add((EX.a, URIRef("https://one.org/p"), Literal("x")))
    g.add((EX.a, URIRef("https://two.org/q"), Literal("y")))
    out = serialize(g, "rdfxml", prefixes={"ns1": "https://user.org/"})
    assert isomorphic(parse_rdf(out, "rdfxml"), g)
    assert out == serialize(g, "rdfxml", prefixes={"ns1": "https://user.org/"})
    assert isomorphic(
        parse_rdf(serialize(g, "turtle", prefixes={"ns1": "https://u.org/"}), "turtle"), g
    )


def test_invalid_graph_is_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_g: Graph) -> Graph:
        raise Exception("bad term")

    monkeypatch.setattr("lakeloom.rdf.canonical.to_canonical_graph", boom)
    with pytest.raises(LakeloomError) as ei:
        serialize(Graph(), "turtle")
    assert ei.value.code == "validation_error" and ei.value.hint


def test_roundtrip_each_format() -> None:
    g = _graph(_triples())
    for fmt in PARSEABLE:
        assert isomorphic(parse_rdf(serialize(g, fmt), fmt), g)


_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="\ufffe\uffff"),
    max_size=12,
)
_iri = st.sampled_from([URIRef(f"https://example.org/m/{n}") for n in "abcdef"])
_lit = st.one_of(
    _text.map(Literal),
    st.tuples(_text, st.sampled_from(["pt", "en", "pt-BR"])).map(
        lambda t: Literal(t[0], lang=t[1])
    ),
    st.integers(-5, 5).map(lambda i: Literal(i, datatype=XSD.integer)),
    st.text(alphabet="ãéç☕ x\n\r\t\"'<&", max_size=8).map(
        lambda s: Literal(s, datatype=XSD.string)
    ),
)
_bn = st.integers(0, 3)


@st.composite
def _spec(draw: st.DrawFn) -> list[tuple]:
    n = draw(st.integers(1, 12))
    out = []
    for _ in range(n):
        s = draw(st.one_of(_iri, _bn))
        p = draw(_iri)
        o = draw(st.one_of(_iri, _bn, _lit))
        out.append((s, p, o))
    return out


def _realise(spec: list[tuple], seed: int) -> Graph:
    labels: dict[int, BNode] = {}
    g = Graph()
    rng = random.Random(seed)  # noqa: S311

    def conv(x: object) -> object:
        if isinstance(x, int) and not isinstance(x, bool):
            return labels.setdefault(x, BNode(f"n{rng.random()}"))
        return x

    items = list(spec)
    rng.shuffle(items)
    for s, p, o in items:
        g.add((conv(s), p, conv(o)))  # type: ignore[arg-type]
    return g


@settings(max_examples=200, deadline=None)
@given(spec=_spec(), seed=st.integers(0, 10**6))
def test_fr_git_03_property_insertion_order_and_bnodes(spec: list[tuple], seed: int) -> None:
    g1, g2 = _realise(spec, 1), _realise(spec, seed + 2)
    for fmt in ALL:
        a, b = serialize(g1, fmt), serialize(g2, fmt)
        assert a == b, fmt
    for fmt in PARSEABLE:
        assert isomorphic(parse_rdf(serialize(g1, fmt), fmt), g1), fmt


_SCRIPT = textwrap.dedent(
    """
    import hashlib, random
    from rdflib import BNode, Graph, Literal, Namespace, RDF
    from lakeloom.rdf.canonical import serialize
    EX = Namespace("https://example.org/m/")
    g = Graph()
    bs = [BNode() for _ in range(6)]
    for i, b in enumerate(bs):
        g.add((EX.a, EX.p, b))
        g.add((b, RDF.type, EX.R))
        g.add((b, EX.v, Literal(f"ação {i % 2}", lang="pt")))
        g.add((b, EX.next, bs[(i + 1) % 6]))
    h = hashlib.sha256()
    for f in ("turtle", "rdfxml", "ntriples", "jsonld", "skos"):
        h.update(serialize(g, f))
    print(h.hexdigest())
    """
)


def test_cross_process_hash_seed_independence() -> None:
    digests = []
    for seed in ("1", "2", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(SRC)}
        res = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True, check=True
        )
        digests.append(res.stdout.strip())
    assert len(set(digests)) == 1, digests


def test_om_vocabulary_has_its_own_prefix() -> None:
    g = parse_rdf(
        '<http://x/T> <https://open-metadata.org/ontology/fullyQualifiedName> "retail.T" .',
        "turtle",
    )
    out = serialize(g, "turtle").decode()
    assert "@prefix om: <https://open-metadata.org/ontology/> ." in out
    assert 'om:fullyQualifiedName "retail.T"' in out
    assert "ns1" not in out
