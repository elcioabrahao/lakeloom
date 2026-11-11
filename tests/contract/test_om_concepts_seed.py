"""Fixture concepts and realizations seeded into the real OpenMetadata (FR-CTX-01)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL
from stack import REPO

from lakeloom.om.client import OMClient

pytestmark = pytest.mark.contract

TABLES = "lakeloom_fixture.retail.sales"
BASE_IRI = "http://localhost:8000/ontology/retail/"
TERM_NAMES = {
    "Customer",
    "Order",
    "Product",
    "Invoice",
    "OrderStatus",
    "CustomerIdentifier",
    "OrderIdentifier",
    "EmailAddress",
    "MonetaryAmount",
    "OrderDate",
}


def _run_seed(om_url: str, token: str, tmp_path: Path) -> dict[str, str]:
    token_file = tmp_path / "om_bot_token"
    token_file.write_text(token, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(REPO / "src"), env.get("PYTHONPATH", "")) if p
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            str(REPO / "scripts/seed_om_concepts.py"),
            "--om-url",
            om_url,
            "--token-file",
            str(token_file),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert token not in proc.stdout + proc.stderr
    return {line.split()[0]: line.split()[1] for line in proc.stdout.splitlines()}


@pytest.fixture
def client(om_url: str, om_bot_token: str) -> OMClient:
    return OMClient(om_url, SecretStr(om_bot_token))


async def test_seed_creates_retail_glossary_terms(
    client: OMClient, om_url: str, om_bot_token: str, tmp_path: Path
) -> None:
    _run_seed(om_url, om_bot_token, tmp_path)
    try:
        page = await client.list_terms("retail", limit=100)
        terms = {t.name: t for t in page.data}
        assert set(terms) == TERM_NAMES
        assert set(terms["Customer"].synonyms) == {"Client", "Buyer"}
        assert terms["Order"].synonyms == ["Purchase"]
        parent = terms["OrderStatus"].parent
        assert parent is not None
        assert parent.id == terms["Order"].id
        assert terms["Customer"].iri == f"{BASE_IRI}Customer"
        mapping = {(m.mapping_type, m.concept_iri) for m in terms["Customer"].concept_mappings}
        assert mapping == {("CLOSE_MATCH", "https://schema.org/Person")}
        order_relations = {(r.relation_type, r.term.name) for r in terms["Order"].related_terms}
        # OM rc1 stores a relation on both terms and keeps no direction: Order also lists
        # billsFor -> Invoice, which was seeded as Invoice billsFor Order (ADR 0009).
        assert order_relations == {
            ("placedBy", "Customer"),
            ("contains", "Product"),
            ("billsFor", "Invoice"),
        }
        invoice_relations = {(r.relation_type, r.term.name) for r in terms["Invoice"].related_terms}
        assert invoice_relations == {("billsFor", "Order")}
    finally:
        await client.aclose()


async def test_seed_realizations_on_orders(
    client: OMClient, om_url: str, om_bot_token: str, tmp_path: Path
) -> None:
    _run_seed(om_url, om_bot_token, tmp_path)
    try:
        table = await client.get_table_by_fqn(f"{TABLES}.orders")
        assert table is not None
        assert "retail.Order" in {t.tag_fqn for t in table.tags}
        expected = {
            "order_id": "retail.OrderIdentifier",
            "customer_id": "retail.CustomerIdentifier",
            "status": "retail.Order.OrderStatus",  # a child term's FQN nests under its parent
            "total_amount": "retail.MonetaryAmount",
            "order_date": "retail.OrderDate",
        }
        for column in table.columns:
            own = {t.tag_fqn for t in column.tags if t.source == "Glossary"}
            assert expected[column.name] in own, column.name
        customers = await client.get_table_by_fqn(f"{TABLES}.customers")
        assert customers is not None
        email = next(c for c in customers.columns if c.name == "email")
        fqns = {t.tag_fqn for t in email.tags}
        assert {"PII.Sensitive", "retail.EmailAddress"} <= fqns
    finally:
        await client.aclose()


async def test_concept_seed_idempotent(
    client: OMClient, om_url: str, om_bot_token: str, tmp_path: Path
) -> None:
    first = _run_seed(om_url, om_bot_token, tmp_path)
    second = _run_seed(om_url, om_bot_token, tmp_path)
    assert first == second
    assert len(first) == 10
    try:
        table = await client.get_table_by_fqn(f"{TABLES}.orders")
        assert table is not None
        for column in table.columns:
            tags = [t.tag_fqn for t in column.tags]
            assert len(tags) == len(set(tags)), (column.name, tags)
        order = await client.get_term("retail.Order")
        assert order is not None
        keys = [(r.relation_type, r.term.id) for r in order.related_terms]
        assert len(keys) == len(set(keys)) == 3
    finally:
        await client.aclose()


async def test_export_ontology_contains_terms(
    client: OMClient, om_url: str, om_bot_token: str, tmp_path: Path
) -> None:
    _run_seed(om_url, om_bot_token, tmp_path)
    try:
        glossary = await client.get_glossary_by_name("retail", fields=())
        assert glossary is not None
        turtle = await client.export_ontology(glossary.id, "turtle")
    finally:
        await client.aclose()
    graph = Graph().parse(data=turtle, format="turtle")
    subjects = {str(s) for s in graph.subjects() if isinstance(s, URIRef)}
    for name in TERM_NAMES:
        assert f"{BASE_IRI}{name}" in subjects, name


async def test_model_service_export_retail_all_formats_deterministic(
    client: OMClient, om_url: str, om_bot_token: str, tmp_path: Path
) -> None:
    from rdflib import Graph as RdfGraph

    from lakeloom.config import Settings
    from lakeloom.core.models import ModelService
    from lakeloom.rdf.canonical import ExportFormat

    _run_seed(om_url, om_bot_token, tmp_path)
    settings = Settings(public_url="http://localhost:8000")  # pyright: ignore[reportArgumentType]
    service = ModelService(client, settings)
    rdflib_format = {"skos": "turtle", "rdfxml": "xml", "ntriples": "nt", "jsonld": "json-ld"}
    formats: list[ExportFormat] = ["turtle", "rdfxml", "ntriples", "jsonld", "skos"]
    try:
        for fmt in formats:
            first = await service.export("retail", fmt)
            RdfGraph().parse(data=first, format=rdflib_format.get(fmt, fmt))
            service.clear_cache()
            assert await service.export("retail", fmt) == first
        info = await service.get_model("retail")
        assert info.term_count == len(TERM_NAMES)
    finally:
        await client.aclose()


async def test_seed_pairs_retail_with_native_ontology_idempotently(
    client: OMClient, om_url: str, om_bot_token: str, tmp_path: Path
) -> None:
    _run_seed(om_url, om_bot_token, tmp_path)
    try:
        first = await client.export_native_ontology("retail")
        assert first is not None
        _run_seed(om_url, om_bot_token, tmp_path)
        second = await client.export_native_ontology("retail")
        assert second is not None
        assert second.working_revision == first.working_revision  # re-run changed nothing
        g = Graph().parse(data=first.turtle, format="turtle")
        classes = {str(s) for s in g.subjects(RDF.type, OWL.Class)}
        assert classes == {f"{BASE_IRI}{n}" for n in TERM_NAMES}
        assert not list(g.subjects(RDF.type, OWL.DatatypeProperty))  # no attributes yet
    finally:
        await client.aclose()
