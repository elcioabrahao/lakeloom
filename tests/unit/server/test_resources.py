"""MCP resources for models and asset context (FR-PUB-01), with a fake OM."""

from typing import Any
from urllib.parse import quote

import pytest
from kit import AuthKit, DepsFactory, mcp_session
from mcp.shared.exceptions import MCPError
from mcp.types import TextResourceContents
from rdflib import Graph
from starlette.testclient import TestClient
from test_tools_semantic import COLUMNS, ORDERS, fake_deps

from lakeloom.metrics import REGISTRY
from lakeloom.server.mcp_app import create_server_app

EXTS = {
    "ttl": "text/turtle",
    "jsonld": "application/ld+json",
    "owl": "application/rdf+xml",
    "nt": "application/n-triples",
}


async def read_or_error(
    make_deps: DepsFactory, kit: AuthKit, uri: str, **claims: Any
) -> Any | MCPError:
    app = create_server_app(fake_deps(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        try:
            return (await session.read_resource(uri)).contents[0]
        except MCPError as exc:  # caught in the session: errors escaping it get group-wrapped
            return exc


async def read(make_deps: DepsFactory, kit: AuthKit, uri: str, **claims: Any) -> Any:
    result = await read_or_error(make_deps, kit, uri, **claims)
    assert not isinstance(result, MCPError), result
    return result


async def error_of(make_deps: DepsFactory, kit: AuthKit, uri: str, **claims: Any) -> dict[str, Any]:
    result = await read_or_error(make_deps, kit, uri, **claims)
    assert isinstance(result, MCPError), result
    data = result.data
    assert isinstance(data, dict)
    return data["error"]  # type: ignore[no-any-return]


def http_body(make_deps: DepsFactory, kit: AuthKit, path: str, accept: str) -> bytes:
    with TestClient(create_server_app(fake_deps(make_deps)), base_url="https://lakeloom.test") as c:
        r = c.get(path, headers={"Authorization": f"Bearer {kit.token()}", "Accept": accept})
    assert r.status_code == 200, r.text
    return r.content


async def test_resources_list_has_models_and_per_model_uris(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    app = create_server_app(fake_deps(make_deps))
    async with mcp_session(app, kit.token()) as session:
        listed = await session.list_resources()
        templates = await session.list_resource_templates()
    uris = {str(r.uri) for r in listed.resources}
    assert "lakeloom://models" in uris
    for ext in EXTS:
        assert f"lakeloom://models/retail/ontology.{ext}" in uris
    assert "lakeloom://models/retail/context.jsonld" in uris
    assert {t.uri_template for t in templates.resource_templates} >= {
        "lakeloom://models/{slug}/ontology.{ext}",
        "lakeloom://models/{slug}/context.jsonld",
        "lakeloom://assets/{fqn}/context.md",
    }


@pytest.mark.parametrize("ext", list(EXTS))
async def test_ontology_resource_equals_http_body(
    make_deps: DepsFactory, kit: AuthKit, ext: str
) -> None:
    content = await read(make_deps, kit, f"lakeloom://models/retail/ontology.{ext}")
    assert isinstance(content, TextResourceContents)
    assert content.mime_type == EXTS[ext]
    assert content.text.encode() == http_body(make_deps, kit, "/v1/models/retail", EXTS[ext])


async def test_models_list_and_context_resources(make_deps: DepsFactory, kit: AuthKit) -> None:
    listing = await read(make_deps, kit, "lakeloom://models")
    assert listing.mime_type == "application/json"
    assert listing.text.encode() == http_body(make_deps, kit, "/v1/models", "application/json")
    ctx = await read(make_deps, kit, "lakeloom://models/retail/context.jsonld")
    assert ctx.mime_type == "application/ld+json"
    assert ctx.text.encode() == http_body(
        make_deps, kit, "/v1/models/retail/context.jsonld", "application/ld+json"
    )


async def test_version_latest_accepted_other_not_found(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    ok = await read(make_deps, kit, "lakeloom://models/retail/ontology.ttl?version=latest")
    assert Graph().parse(data=ok.text, format="turtle")
    err = await error_of(make_deps, kit, "lakeloom://models/retail/ontology.ttl?version=1.0.0")
    assert err["code"] == "not_found"
    assert "version=latest" in err["hint"]
    err = await error_of(make_deps, kit, "lakeloom://models/retail/context.jsonld?version=2")
    assert err["code"] == "not_found"
    err = await error_of(make_deps, kit, f"lakeloom://assets/{ORDERS}/context.md?version=2")
    assert err["code"] == "not_found"


async def test_unknown_ext_and_unknown_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    err = await error_of(make_deps, kit, "lakeloom://models/retail/ontology.xyz")
    assert err["code"] == "validation_error"
    assert "ttl, jsonld, owl, nt" in err["message"] + err["hint"]
    err = await error_of(make_deps, kit, "lakeloom://models/nope/ontology.ttl")
    assert err["code"] == "not_found"
    err = await error_of(make_deps, kit, "lakeloom://bogus/thing")
    assert err["code"] == "not_found"


async def test_resource_requires_read_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    err = await error_of(
        make_deps, kit, "lakeloom://models/retail/ontology.ttl", scope="lakeloom:propose"
    )
    assert err["code"] == "forbidden"


async def test_asset_context_resource_percent_encoded_fqn(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    for fqn in (ORDERS, quote(ORDERS, safe=""), quote(ORDERS, safe="")[:-1] + "%73"):
        content = await read(make_deps, kit, f"lakeloom://assets/{fqn}/context.md")
        assert content.mime_type == "text/markdown"
        assert all(concept in content.text for concept in COLUMNS.values())
    expected = http_body(make_deps, kit, f"/v1/assets/{ORDERS}/context", "text/markdown")
    assert content.text.encode() == expected
    err = await error_of(make_deps, kit, "lakeloom://assets/svc.nope.x.y/context.md")
    assert err["code"] == "not_found"


async def test_resource_reads_are_counted(make_deps: DepsFactory, kit: AuthKit) -> None:
    def count(outcome: str) -> float:
        return (
            REGISTRY.get_sample_value(
                "lakeloom_resource_reads_total", {"resource": "ontology", "outcome": outcome}
            )
            or 0.0
        )

    ok, bad = count("ok"), count("error")
    await read(make_deps, kit, "lakeloom://models/retail/ontology.ttl")
    await error_of(make_deps, kit, "lakeloom://models/nope/ontology.ttl")
    assert count("ok") == ok + 1
    assert count("error") == bad + 1
