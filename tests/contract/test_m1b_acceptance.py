"""M1b acceptance against the live stack: semantic read tools over real /mcp (agent token)."""

import hashlib
import math
import time
from typing import Any, get_args

import pytest
from rdflib import Graph
from stack import mcp_session

from lakeloom.rdf.canonical import ExportFormat

pytestmark = pytest.mark.contract

ORDERS = "lakeloom_fixture.retail.sales.orders"
COLUMN_CONCEPTS = {
    "order_id": "retail.OrderIdentifier",
    "customer_id": "retail.CustomerIdentifier",
    "status": "retail.Order.OrderStatus",
    "total_amount": "retail.MonetaryAmount",
    "order_date": "retail.OrderDate",
}
RDFLIB_FORMAT = {
    "turtle": "turtle",
    "rdfxml": "xml",
    "ntriples": "nt",
    "jsonld": "json-ld",
    "skos": "turtle",
}


async def call(lakeloom_url: str, token: str, tool: str, args: dict[str, Any]) -> Any:
    async with mcp_session(lakeloom_url, token) as session:
        result = await session.call_tool(tool, args)
    assert result.is_error is not True, result
    return result


def text_of(result: Any) -> str:
    block = result.content[0]
    assert block.type == "text"
    return str(block.text)


async def test_m1_acceptance_orders_context_lists_column_concepts(
    lakeloom_url: str, agent_token: str
) -> None:
    result = await call(lakeloom_url, agent_token, "get_asset_context", {"asset": ORDERS})

    md = text_of(result)
    data = result.structured_content
    assert "content" not in data
    assert data["truncated"] is False and data["token_estimate"] == math.ceil(len(md) / 4)
    for name, concept in COLUMN_CONCEPTS.items():
        assert f"`{name}`" in md and f"`{concept}`" in md
    assert "`retail.Order`" in md
    as_json = await call(
        lakeloom_url, agent_token, "get_asset_context", {"asset": ORDERS, "format": "json"}
    )
    content = as_json.structured_content["content"]
    assert [c["fqn"] for c in content["table_concepts"]] == ["retail.Order"]
    got = {c["name"]: [r["fqn"] for r in c["concepts"]] for c in content["columns"]}
    assert got == {k: [v] for k, v in COLUMN_CONCEPTS.items()}


async def test_m1_acceptance_exports_five_formats_parse_and_are_stable(
    lakeloom_url: str, agent_token: str
) -> None:
    async with mcp_session(lakeloom_url, agent_token) as session:
        for fmt in get_args(ExportFormat):
            first = await session.call_tool("export_model", {"model": "retail", "format": fmt})
            second = await session.call_tool("export_model", {"model": "retail", "format": fmt})
            assert first.is_error is not True, (fmt, first)
            a, b = first.structured_content, second.structured_content
            assert a["sha256"] == b["sha256"], fmt
            body = text_of(first)
            assert a["sha256"] == hashlib.sha256(body.encode()).hexdigest()
            assert a["bytes"] == len(body.encode())
            graph = Graph().parse(data=body, format=RDFLIB_FORMAT[fmt])
            assert len(graph) > 0, fmt


async def test_find_concepts_client_finds_customer(lakeloom_url: str, agent_token: str) -> None:
    result = await call(lakeloom_url, agent_token, "find_concepts", {"query": "client"})

    fqns = [m["concept"]["fqn"] for m in result.structured_content["items"]]
    assert "retail.Customer" in fqns


async def test_explain_concept_order(lakeloom_url: str, agent_token: str) -> None:
    result = await call(lakeloom_url, agent_token, "explain_concept", {"concept": "retail.Order"})

    data = result.structured_content
    assert data["concept"]["fqn"] == "retail.Order"
    assert data["definition"]["untrusted"] is True
    assert "# `Order`" in text_of(result)


@pytest.mark.parametrize(
    ("name", "fqn"), [("Order", "retail.Order"), ("customer", "retail.Customer")]
)
async def test_fr_ctx_03_concept_tools_accept_plain_names(
    lakeloom_url: str, agent_token: str, name: str, fqn: str
) -> None:
    explained = await call(lakeloom_url, agent_token, "explain_concept", {"concept": name})
    assert explained.structured_content["concept"]["fqn"] == fqn
    assets = await call(lakeloom_url, agent_token, "find_assets_for_concept", {"concept": name})
    by_fqn = await call(lakeloom_url, agent_token, "find_assets_for_concept", {"concept": fqn})
    assert assets.structured_content["items"] == by_fqn.structured_content["items"]
    assert assets.structured_content["items"]


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


async def test_nfr_perf_01_warm_read_tools_p95(
    lakeloom_url: str, agent_token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, dict[str, Any]] = {
        "get_asset_context": {"asset": ORDERS},
        "explain_concept": {"concept": "retail.Order"},
        "find_concepts": {"query": "customer"},
    }
    async with mcp_session(lakeloom_url, agent_token) as session:
        for tool, args in calls.items():  # warm the caches
            assert (await session.call_tool(tool, args)).is_error is not True
        timings: dict[str, list[float]] = {t: [] for t in calls}
        for _ in range(20):
            for tool, args in calls.items():
                started = time.perf_counter()
                result = await session.call_tool(tool, args)
                timings[tool].append(time.perf_counter() - started)
                assert result.is_error is not True, (tool, result)
    with capsys.disabled():
        print("\np95 (s):", {t: round(p95(v), 3) for t, v in timings.items()})
    for tool, samples in timings.items():
        assert p95(samples) < 1.5, (tool, p95(samples))
