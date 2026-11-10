"""Byte-deterministic RDF serialisation (FR-GIT-03).

Algorithm
---------
1. ``canonicalize`` runs ``rdflib.compare.to_canonical_graph`` (hash-based canonical blank-node
   identifiers) and then relabels the blank nodes ``b0..bn`` in sorted order of the canonical
   identifier, so labels are short and independent of rdflib internals and of
   ``PYTHONHASHSEED`` (verified by a cross-process test).
2. ``serialize`` copies the canonical graph into a fresh graph with exactly the allowed prefix
   bindings (given prefixes plus dcterms, om, owl, rdf, rdfs, skos, xsd, bound in sorted order) and
   inserts the triples in sorted order, then:
   * turtle: rdflib's Turtle writer (sorted subjects, predicates and objects);
   * ntriples: serialised lines are sorted and de-duplicated;
   * rdfxml: a small flat writer (one ``rdf:Description`` per subject, sorted) because
     rdflib's XML writers order subjects by set iteration and mis-prefix ``xml:lang``;
   * jsonld: a flat expanded writer (nodes sorted by ``@id``, values sorted, sorted keys)
     because rdflib's JSON-LD writer drops blank-node-only cycles;
   * skos: Turtle of the SKOS projection.
   Unbound predicate/type namespaces are named ``ns1..nsN`` in sorted namespace order, skipping
   any ``nsN`` already taken by a caller prefix. Invalid graphs raise ``validation_error``.
   JSON-LD output is a top-level array of expanded nodes (full IRIs, no ``@context``).
3. Output is UTF-8, LF only, with a single trailing newline.
"""

import json
import re
from collections.abc import Mapping
from typing import Any, cast
from typing import Literal as TLiteral
from xml.sax import SAXParseException
from xml.sax.saxutils import escape, quoteattr

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.compare import to_canonical_graph
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, SKOS, XSD, split_uri
from rdflib.term import Node

from lakeloom.errors import LakeloomError
from lakeloom.rdf.skos import skos_projection

__all__ = [
    "EXTENSIONS",
    "MEDIA_TYPES",
    "ExportFormat",
    "InputFormat",
    "canonicalize",
    "parse_rdf",
    "serialize",
]

ExportFormat = TLiteral["turtle", "rdfxml", "ntriples", "jsonld", "skos"]
InputFormat = TLiteral["turtle", "rdfxml", "ntriples", "jsonld"]

MEDIA_TYPES: dict[ExportFormat, str] = {
    "turtle": "text/turtle",
    "rdfxml": "application/rdf+xml",
    "ntriples": "application/n-triples",
    "jsonld": "application/ld+json",
    "skos": "text/turtle",
}
EXTENSIONS: dict[ExportFormat, str] = {
    "turtle": "ttl",
    "rdfxml": "owl",
    "ntriples": "nt",
    "jsonld": "jsonld",
    "skos": "skos.ttl",
}

_RDFLIB_FORMAT: dict[InputFormat, str] = {
    "turtle": "turtle",
    "rdfxml": "xml",
    "ntriples": "nt",
    "jsonld": "json-ld",
}
_DEFAULT_PREFIXES: dict[str, Namespace] = {
    "dcterms": Namespace(str(DCTERMS)),
    # OpenMetadata's own vocabulary, as OM's exportOntology binds it (om:fullyQualifiedName).
    "om": Namespace("https://open-metadata.org/ontology/"),
    "owl": Namespace(str(OWL)),
    "rdf": Namespace(str(RDF)),
    "rdfs": Namespace(str(RDFS)),
    "skos": Namespace(str(SKOS)),
    "xsd": Namespace(str(XSD)),
}
_XML_POS = re.compile(r":(\d+):(\d+):")
_NCNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")


def parse_rdf(data: str | bytes, fmt: InputFormat) -> Graph:
    """Parse ``data``; syntax errors become ``validation_error`` (with line/column if known)."""
    graph = Graph()
    try:
        graph.parse(data=data, format=_RDFLIB_FORMAT[fmt])
    except Exception as exc:
        raise LakeloomError(
            "validation_error",
            f"Invalid {fmt} input{_position(exc, data, fmt)}: {_one_line(exc)}",
            hint=f"Fix the {fmt} syntax at the reported position and retry.",
        ) from exc
    return graph


def _one_line(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:300] or type(exc).__name__


def _ntriples_bad_line(data: str | bytes) -> int | None:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            Graph().parse(data=line, format="nt")
        except Exception:
            return number
    return None


def _position(exc: Exception, data: str | bytes, fmt: InputFormat) -> str:
    line: int | None = None
    col: int | None = None
    if isinstance(exc, SAXParseException):
        line, col = exc.getLineNumber(), exc.getColumnNumber()
    elif isinstance(exc, json.JSONDecodeError):
        line, col = exc.lineno, exc.colno
    elif isinstance(getattr(exc, "lines", None), int):
        line = exc.lines + 1  # type: ignore[attr-defined]  # rdflib BadSyntax is 0-based
    else:
        m = _XML_POS.search(str(exc))
        if m:
            line, col = int(m.group(1)), int(m.group(2))
    if line is None and fmt == "ntriples":
        line = _ntriples_bad_line(data)
    if line is None:
        return ""
    return f" at line {line}" + (f", column {col}" if col is not None else "")


def canonicalize(graph: Graph) -> Graph:
    """Return an isomorphic graph with canonical, stable blank-node labels ``b0..bn``."""
    try:
        canon = to_canonical_graph(graph)
    except Exception as exc:
        raise LakeloomError(
            "validation_error",
            f"Graph cannot be canonicalised: {_one_line(exc)}",
            hint="Check the graph for invalid IRIs or terms.",
        ) from exc
    mapping = {
        node: BNode(f"b{i}")
        for i, node in enumerate(
            sorted(
                {t for triple in canon for t in triple if isinstance(t, BNode)},
                key=str,
            )
        )
    }
    out = Graph(bind_namespaces="none")
    for prefix, ns in graph.namespaces():
        out.bind(prefix, ns, override=True)
    for s, p, o in canon:
        out.add((mapping.get(s, s), p, mapping.get(o, o)))  # type: ignore[arg-type]
    return out


def _term_key(term: Node) -> tuple[int, str, str, str]:
    if isinstance(term, Literal):
        return (2, str(term), term.language or "", str(term.datatype or ""))
    return (1 if isinstance(term, BNode) else 0, str(term), "", "")


def _split(iri: URIRef) -> tuple[str, str]:
    try:
        ns, local = split_uri(iri)
    except ValueError:
        ns, local = "", ""
    if not ns or not _NCNAME.match(local):
        raise LakeloomError(
            "validation_error",
            f"Predicate IRI cannot be written as an XML name: {iri}",
            hint="Use predicate IRIs ending in a valid XML local name, or export another format.",
        )
    return ns, local


def _sorted_copy(graph: Graph, prefixes: Mapping[str, str] | None) -> Graph:
    allowed: dict[str, str] = {k: str(v) for k, v in _DEFAULT_PREFIXES.items()}
    allowed.update(prefixes or {})
    out = Graph(bind_namespaces="none")
    for prefix in sorted(allowed):
        out.bind(prefix, Namespace(allowed[prefix]), override=True, replace=True)
    triples = sorted(graph, key=lambda t: tuple(_term_key(x) for x in t))
    # Name unbound predicate/type namespaces ns1..nsN in sorted order (never rdflib's
    # iteration-order dependent autonumbering).
    bound = set(allowed.values())
    extra: set[str] = set()
    for _, p, o in triples:
        for iri in (p, o if p == RDF.type else None):
            if isinstance(iri, URIRef):
                try:
                    ns = split_uri(iri)[0]
                except ValueError:
                    continue
                if ns not in bound:
                    extra.add(ns)
    counter = 0
    for ns in sorted(extra):
        counter += 1
        while f"ns{counter}" in allowed:
            counter += 1
        out.bind(f"ns{counter}", Namespace(ns), override=True, replace=True)
    for triple in triples:
        out.add(triple)
    return out


def _finish(text: str) -> bytes:
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return (text + "\n").encode("utf-8")


_XML_ILLEGAL = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml_text(value: str) -> str:
    if _XML_ILLEGAL.search(value):
        raise LakeloomError(
            "validation_error",
            "A literal contains characters that cannot be written in RDF/XML",
            hint="Remove control characters from the literal or export another format.",
        )
    return escape(value).replace("\r", "&#13;")


def _node_attr(term: Node) -> str:
    if isinstance(term, BNode):
        return f"rdf:nodeID={quoteattr(str(term))}"
    return f"rdf:about={quoteattr(str(term))}"


def _rdfxml(graph: Graph) -> str:
    by_subject: dict[Node, list[tuple[URIRef, Node]]] = {}
    for s, p, o in graph:
        by_subject.setdefault(s, []).append((p, o))  # type: ignore[arg-type]
    ns_prefix = {str(ns): prefix for prefix, ns in graph.namespaces()}
    used: dict[str, str] = {str(RDF): "rdf"}
    body: list[str] = []
    for subject in sorted(by_subject, key=_term_key):
        body.append(f"  <rdf:Description {_node_attr(subject)}>")
        for p, o in sorted(
            by_subject[subject], key=lambda po: (_term_key(po[0]), _term_key(po[1]))
        ):
            ns, local = _split(p)
            prefix = "rdf" if ns == str(RDF) else ns_prefix[ns]
            used[ns] = prefix
            tag = f"{prefix}:{local}"
            if isinstance(o, Literal):
                attrs = ""
                if o.language:
                    attrs = f" xml:lang={quoteattr(o.language)}"
                elif o.datatype:
                    attrs = f" rdf:datatype={quoteattr(str(o.datatype))}"
                body.append(f"    <{tag}{attrs}>{_xml_text(str(o))}</{tag}>")
            elif isinstance(o, BNode):
                body.append(f"    <{tag} rdf:nodeID={quoteattr(str(o))}/>")
            else:
                body.append(f"    <{tag} rdf:resource={quoteattr(str(o))}/>")
        body.append("  </rdf:Description>")
    decls = "".join(
        f"\n  xmlns:{prefix}={quoteattr(ns)}"
        for ns, prefix in sorted(used.items(), key=lambda kv: kv[1])
    )
    head = f'<?xml version="1.0" encoding="utf-8"?>\n<rdf:RDF{decls}>'
    return "\n".join([head, *body, "</rdf:RDF>"])


def _json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _jsonld(graph: Graph) -> str:
    nodes: dict[Node, dict[str, Any]] = {}
    for s, p, o in graph:
        node = nodes.setdefault(s, {"@id": f"_:{s}" if isinstance(s, BNode) else str(s)})
        if p == RDF.type and isinstance(o, URIRef):
            node.setdefault("@type", []).append(str(o))
            continue
        value: dict[str, str]
        if isinstance(o, Literal):
            value = {"@value": str(o)}
            if o.language:
                value["@language"] = o.language
            elif o.datatype:
                value["@type"] = str(o.datatype)
        else:
            value = {"@id": f"_:{o}" if isinstance(o, BNode) else str(o)}
        node.setdefault(str(p), []).append(value)
    docs: list[dict[str, Any]] = []
    for key in sorted(nodes, key=_term_key):
        node = nodes[key]
        for k, v in list(node.items()):
            if isinstance(v, list):
                items = cast("list[Any]", v)
                node[k] = sorted(items, key=_json_key)
        docs.append(node)
    return json.dumps(docs, sort_keys=True, indent=2, ensure_ascii=False)


def serialize(
    graph: Graph, fmt: ExportFormat, *, prefixes: Mapping[str, str] | None = None
) -> bytes:
    """Serialise ``graph`` to ``fmt`` as byte-identical output for the same RDF content."""
    if fmt == "skos":
        return serialize(skos_projection(graph), "turtle", prefixes=prefixes)
    ordered = _sorted_copy(canonicalize(graph), prefixes)
    if fmt == "turtle":
        text = ordered.serialize(format="turtle")
    elif fmt == "rdfxml":
        text = _rdfxml(ordered)
    elif fmt == "ntriples":
        text = "\n".join(sorted(set(ordered.serialize(format="nt").splitlines())))
    elif fmt == "jsonld":
        text = _jsonld(ordered)
    else:  # pragma: no cover
        raise LakeloomError(
            "validation_error",
            f"Unknown export format {fmt!r}",
            hint="Use one of turtle, rdfxml, ntriples, jsonld, skos.",
        )
    return _finish(text)
