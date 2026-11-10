"""Seed the fixture ontology (glossary ``retail``) and its realizations into OpenMetadata.

Usage::

    uv run python scripts/seed_om_concepts.py --om-url http://localhost:8585 \\
        --token-file /secrets/om_bot_token

Runs after ``seed_om_fixture.py``. Creates the glossary ``retail``, its terms (synonyms,
parent, explicit ``iri``, SKOS concept mappings), the custom relationship types
``placedBy`` / ``billsFor`` / ``contains``, the term-to-term relations, and glossary-term
tags on the fixture tables and columns (the concepts' realizations). Everything is
create-or-update (PUT) or check-then-add, so a second run changes nothing and keeps the
same ids. Existing classification tags (``PII.Sensitive``) are preserved. The bot token is
read from the token file and only ever sent in the Authorization header.

This seeds OM directly (ADR 0009): FR-OM-02 governs Lakeloom's own writes, from M2.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

GLOSSARY = "retail"
BASE_IRI = "http://localhost:8000/ontology/retail/"
TABLE_PREFIX = "lakeloom_fixture.retail.sales"

# name, parent, synonyms, definition, concept mappings (type, iri)
TERMS: list[dict[str, Any]] = [
    {
        "name": "Customer",
        "description": "A person or organisation that buys goods from the retailer.",
        "synonyms": ["Client", "Buyer"],
        "mappings": [("CLOSE_MATCH", "https://schema.org/Person")],
    },
    {
        "name": "Order",
        "description": "A customer's request to purchase one or more products.",
        "synonyms": ["Purchase"],
        "mappings": [("EXACT_MATCH", "https://schema.org/Order")],
    },
    {"name": "Product", "description": "An item the retailer sells."},
    {"name": "Invoice", "description": "A bill issued to a customer for an order."},
    {
        "name": "OrderStatus",
        "parent": "Order",
        "description": "The stage an order has reached, from placed to delivered or cancelled.",
    },
    {
        "name": "CustomerIdentifier",
        "description": "A value that uniquely identifies a customer.",
    },
    {"name": "OrderIdentifier", "description": "A value that uniquely identifies an order."},
    {"name": "EmailAddress", "description": "An electronic mail address of a person."},
    {
        "name": "MonetaryAmount",
        "description": "A sum of money in a given currency.",
        "mappings": [("EXACT_MATCH", "https://schema.org/MonetaryAmount")],
    },
    {"name": "OrderDate", "description": "The calendar date on which an order was placed."},
]

RELATION_TYPES = {
    "placedBy": ("placed by", "The subject was placed by the object."),
    "billsFor": ("bills for", "The subject bills for the object."),
    "contains": ("contains", "The subject contains the object."),
}
# subject, relation type, object
RELATIONS = [
    ("Order", "placedBy", "Customer"),
    ("Invoice", "billsFor", "Order"),
    ("Order", "contains", "Product"),
]

# table -> (table term, {column: term})
REALIZATIONS: dict[str, tuple[str | None, dict[str, str]]] = {
    "orders": (
        "Order",
        {
            "order_id": "OrderIdentifier",
            "customer_id": "CustomerIdentifier",
            "status": "OrderStatus",
            "total_amount": "MonetaryAmount",
            "order_date": "OrderDate",
        },
    ),
    "customers": ("Customer", {"email": "EmailAddress", "customer_id": "CustomerIdentifier"}),
    "products": ("Product", {}),
    "invoices": ("Invoice", {}),
}


PARENTS = {t["name"]: t["parent"] for t in TERMS if "parent" in t}


def term_fqn(name: str) -> str:
    """OM nests a child term's FQN under its parent: ``retail.Order.OrderStatus``."""
    parent = PARENTS.get(name)
    return f"{term_fqn(parent)}.{name}" if parent else f"{GLOSSARY}.{name}"


def term_payload(spec: dict[str, Any]) -> dict[str, Any]:
    name: str = spec["name"]
    payload: dict[str, Any] = {
        "name": name,
        "displayName": name,
        "glossary": GLOSSARY,
        "description": spec["description"],
        "synonyms": spec.get("synonyms", []),
        "iri": f"{BASE_IRI}{name}",
        "conceptMappings": [
            {"conceptIri": iri, "mappingType": kind} for kind, iri in spec.get("mappings", [])
        ],
    }
    if "parent" in spec:
        payload["parent"] = term_fqn(spec["parent"])
    return payload


def glossary_tag(name: str) -> dict[str, str]:
    return {
        "tagFQN": term_fqn(name),
        "source": "Glossary",
        "labelType": "Manual",
        "state": "Confirmed",
    }


def merge_tags(existing: list[dict[str, Any]], term: str) -> list[dict[str, Any]] | None:
    """Own tags plus the glossary tag, or ``None`` when it is already there.

    OM shows a table's tags on its columns with ``labelType: Derived``; those are not the
    column's own tags and must not be written back.
    """
    own = [t for t in existing if t.get("labelType") != "Derived"]
    if any(t.get("tagFQN") == term_fqn(term) for t in own):
        return None
    return [*own, glossary_tag(term)]


def _check(response: httpx.Response, what: str) -> httpx.Response:
    if response.status_code >= 400:
        raise SystemExit(f"{what} failed: HTTP {response.status_code} {response.text[:500]}")
    return response


def _put(http: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _check(http.put(path, json=body), f"PUT {path}").json()


def seed_relation_types(http: httpx.Client) -> None:
    for name, (display, description) in RELATION_TYPES.items():
        _put(
            http,
            "/v1/relationshipTypes",
            {
                "name": name,
                "displayName": display,
                "description": description,
                "category": "CUSTOM",
                "paletteKey": "BLUE",
                "rdfPredicate": f"{BASE_IRI}{name}",
            },
        )


def seed_terms(http: httpx.Client) -> dict[str, str]:
    _put(
        http,
        "/v1/glossaries",
        {
            "name": GLOSSARY,
            "displayName": "Retail Ontology",
            "description": "Fixture retail ontology for Lakeloom development and tests.",
        },
    )
    ids: dict[str, str] = {}
    for spec in TERMS:  # parents precede children in TERMS
        ids[spec["name"]] = str(_put(http, "/v1/glossaryTerms", term_payload(spec))["id"])
    return ids


def seed_relations(http: httpx.Client, ids: dict[str, str]) -> int:
    """POST each relation not yet present (the endpoint is not an upsert); return added."""
    added = 0
    for subject, relation, obj in RELATIONS:
        term = _check(
            http.get(f"/v1/glossaryTerms/{ids[subject]}", params={"fields": "relatedTerms"}),
            f"GET term {subject}",
        ).json()
        present = {(r["relationType"], r["term"]["id"]) for r in term.get("relatedTerms", [])}
        if (relation, ids[obj]) in present:
            continue
        body = {"term": {"id": ids[obj], "type": "glossaryTerm"}, "relationType": relation}
        _check(http.post(f"/v1/glossaryTerms/{ids[subject]}/relations", json=body), "POST relation")
        added += 1
    return added


def seed_realizations(http: httpx.Client) -> int:
    """Tag tables and columns with their glossary terms; return number of PATCHes sent."""
    patched = 0
    for table, (table_term, columns) in REALIZATIONS.items():
        fqn = f"{TABLE_PREFIX}.{table}"
        response = _check(
            http.get(f"/v1/tables/name/{fqn}", params={"fields": "tags,columns"}), f"GET {fqn}"
        )
        entity = response.json()
        ops: list[dict[str, Any]] = []
        if table_term is not None:
            merged = merge_tags(entity.get("tags", []), table_term)
            if merged is not None:
                ops.append({"op": "add", "path": "/tags", "value": merged})
        for index, column in enumerate(entity["columns"]):
            if column["name"] in columns:
                merged = merge_tags(column.get("tags", []), columns[column["name"]])
                if merged is not None:
                    ops.append({"op": "add", "path": f"/columns/{index}/tags", "value": merged})
        if ops:
            # Column indexes come from the GET just above; no JSON Patch `test` op guards them.
            _check(
                http.patch(
                    f"/v1/tables/{entity['id']}",
                    content=json.dumps(ops),
                    headers={"Content-Type": "application/json-patch+json"},
                ),
                f"PATCH {fqn}",
            )
            patched += 1
    return patched


def seed(om_url: str, token: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(base_url=f"{om_url.rstrip('/')}/api", headers=headers, timeout=30) as http:
        seed_relation_types(http)
        ids = seed_terms(http)
        seed_relations(http, ids)
        seed_realizations(http)
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--om-url", required=True)
    parser.add_argument("--token-file", type=Path, default=Path("/secrets/om_bot_token"))
    args = parser.parse_args(argv)
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"token file {args.token_file} is empty")
    for name, term_id in seed(args.om_url, token).items():
        print(f"{term_fqn(name)} {term_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
