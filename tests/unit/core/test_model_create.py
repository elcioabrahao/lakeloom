"""create_model: a glossary plus its paired native ontology, behind a confirm token (FR-AGT-03).

OM is mocked with respx; the confirm tokens are real.
"""

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.model_create import (
    CREATE_MODEL_TOOL,
    CreatedModel,
    ModelPreview,
    create_model,
    validate_slug,
)
from lakeloom.core.models import ModelService
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient

BASE = "http://om.test:8585"
API = f"{BASE}/api/v1"
GID = "0b6f1f9e-6c39-4e57-9d5e-0f7b2b4bd2a1"
OID = "f390dbf9-9645-4bda-8f33-45c8cb4d0180"
IRI = "https://lakeloom.test/ontology/shop/"


def glossary(name: str = "shop", updated: int = 1) -> dict[str, Any]:
    return {"id": GID, "name": name, "fullyQualifiedName": name, "updatedAt": updated}


def ontology(name: str = "shop") -> dict[str, Any]:
    return {"id": OID, "name": name, "iri": IRI, "namespace": IRI}


class Env:
    def __init__(self) -> None:
        self.settings = Settings(public_url="https://lakeloom.test")  # pyright: ignore[reportCallIssue]
        self.om = OMClient(BASE, SecretStr("t"))
        self.models = ModelService(self.om, self.settings)
        self.confirm = ConfirmTokens(b"k" * 32, 600)
        self.invalidated: list[str | None] = []
        self.models.on_invalidate(self.invalidated.append)

    async def call(self, name: str = "shop", **kw: Any) -> CreatedModel | ModelPreview:
        kw.setdefault("principal", "admin-1")
        return await create_model(self.om, self.models, self.confirm, self.settings, name, **kw)


@pytest.fixture
async def env() -> Any:
    e = Env()
    yield e
    await e.om.aclose()


def missing(path: str) -> respx.Route:
    return respx.get(f"{API}/{path}").respond(404, json={"code": 404, "message": "missing"})


@pytest.mark.parametrize("name", ["shop", "retail_auto_1a2b", "ll-prop-9", "a"])
def test_valid_slugs(name: str) -> None:
    validate_slug(name)


@pytest.mark.parametrize("name", ["", "Shop", "1shop", "shop.x", "sh op", "a" * 65, "shop/x"])
def test_invalid_slugs_are_validation_errors(name: str) -> None:
    with pytest.raises(LakeloomError) as info:
        validate_slug(name)
    assert info.value.code == "validation_error"


@respx.mock
async def test_fr_agt_03_preview_then_confirm_creates_glossary_and_ontology(env: Env) -> None:
    get_g = missing("glossaries/name/shop")
    missing("ontologies/name/shop")
    post_g = respx.post(f"{API}/glossaries").respond(201, json=glossary())
    post_o = respx.post(f"{API}/ontologies").respond(201, json=ontology())

    preview = await env.call(display_name="Shop", description="Things we sell")
    assert isinstance(preview, ModelPreview)
    assert preview.base_iri == IRI
    assert preview.creates == ["glossary", "ontology"]
    assert "`shop`" in preview.summary and IRI in preview.summary
    assert not post_g.called and not post_o.called  # a dry run writes nothing

    # the glossary is read again at confirm time, still absent
    done = await env.call(
        display_name="Shop", description="Things we sell", confirm_token=preview.confirm_token
    )
    assert isinstance(done, CreatedModel)
    assert (done.glossary_id, done.ontology_id, done.created) == (
        GID,
        OID,
        ["glossary", "ontology"],
    )
    assert json.loads(post_g.calls[0].request.content) == {
        "name": "shop",
        "displayName": "Shop",
        "description": "Things we sell",
    }
    assert json.loads(post_o.calls[0].request.content)["namespace"] == IRI  # absolute IRI
    assert json.loads(post_o.calls[0].request.content)["iri"] == IRI
    assert env.invalidated == ["shop"]
    assert get_g.call_count == 2


@respx.mock
async def test_token_bound_to_arguments_and_tool(env: Env) -> None:
    missing("glossaries/name/shop")
    missing("ontologies/name/shop")
    post = respx.post(f"{API}/glossaries").respond(201, json=glossary())
    preview = await env.call(description="a")
    assert isinstance(preview, ModelPreview)
    with pytest.raises(LakeloomError) as info:
        await env.call(description="b", confirm_token=preview.confirm_token)
    assert info.value.code == "validation_error"
    other, _ = env.confirm.issue("discard_proposal", {"name": "shop"}, "x")
    with pytest.raises(LakeloomError) as info:
        await env.call(description="a", confirm_token=other)
    assert info.value.code == "validation_error"
    assert not post.called
    assert CREATE_MODEL_TOOL == "create_model"


@respx.mock
async def test_replayed_token_is_conflict(env: Env) -> None:
    state = {"exists": False}

    def get_glossary(_: httpx.Request) -> httpx.Response:
        if state["exists"]:
            return httpx.Response(200, json=glossary())
        return httpx.Response(404, json={"code": 404, "message": "x"})

    respx.get(f"{API}/glossaries/name/shop").mock(side_effect=get_glossary)
    missing("ontologies/name/shop")

    def post_glossary(_: httpx.Request) -> httpx.Response:
        state["exists"] = True
        return httpx.Response(201, json=glossary())

    post = respx.post(f"{API}/glossaries").mock(side_effect=post_glossary)
    respx.post(f"{API}/ontologies").respond(201, json=ontology())
    preview = await env.call()
    assert isinstance(preview, ModelPreview)
    await env.call(confirm_token=preview.confirm_token)
    with pytest.raises(LakeloomError) as info:
        await env.call(confirm_token=preview.confirm_token)
    assert info.value.code == "conflict"
    assert post.call_count == 1


@respx.mock
async def test_ontology_failure_deletes_the_new_glossary(env: Env) -> None:
    missing("glossaries/name/shop")
    missing("ontologies/name/shop")
    respx.post(f"{API}/glossaries").respond(201, json=glossary())
    respx.post(f"{API}/ontologies").respond(409, json={"code": 409, "message": "exists"})
    delete = respx.delete(f"{API}/glossaries/{GID}").respond(200, json={"id": GID})
    preview = await env.call()
    assert isinstance(preview, ModelPreview)
    with pytest.raises(LakeloomError) as info:
        await env.call(confirm_token=preview.confirm_token)
    assert info.value.code == "conflict"
    assert "removed again" in info.value.message
    assert delete.called
    assert delete.calls[0].request.url.params["hardDelete"] == "true"


@respx.mock
async def test_failed_compensation_names_the_glossary(env: Env) -> None:
    missing("glossaries/name/shop")
    missing("ontologies/name/shop")
    respx.post(f"{API}/glossaries").respond(201, json=glossary())
    respx.post(f"{API}/ontologies").respond(503)
    respx.delete(f"{API}/glossaries/{GID}").respond(503)
    preview = await env.call()
    assert isinstance(preview, ModelPreview)
    with pytest.raises(LakeloomError) as info:
        await env.call(confirm_token=preview.confirm_token)
    assert info.value.code == "upstream_unavailable"
    assert GID in info.value.hint and "pair_existing=true" in info.value.hint


@respx.mock
async def test_pair_existing_creates_only_the_ontology(env: Env) -> None:
    respx.get(f"{API}/glossaries/name/shop").respond(200, json=glossary())
    missing("ontologies/name/shop")
    post_g = respx.post(f"{API}/glossaries").respond(201, json=glossary())
    post_o = respx.post(f"{API}/ontologies").respond(201, json=ontology())
    preview = await env.call(pair_existing=True)
    assert isinstance(preview, ModelPreview) and preview.creates == ["ontology"]
    done = await env.call(pair_existing=True, confirm_token=preview.confirm_token)
    assert isinstance(done, CreatedModel) and done.created == ["ontology"]
    assert done.glossary_id == GID
    assert not post_g.called and post_o.called


@respx.mock
async def test_pair_existing_on_a_paired_model_is_validation_error(env: Env) -> None:
    respx.get(f"{API}/glossaries/name/shop").respond(200, json=glossary())
    respx.get(f"{API}/ontologies/name/shop").respond(200, json=ontology())
    with pytest.raises(LakeloomError) as info:
        await env.call(pair_existing=True)
    assert info.value.code == "validation_error"
    assert "already paired" in info.value.message


@respx.mock
async def test_pair_existing_without_glossary_is_not_found(env: Env) -> None:
    missing("glossaries/name/shop")
    missing("ontologies/name/shop")
    with pytest.raises(LakeloomError) as info:
        await env.call(pair_existing=True)
    assert info.value.code == "not_found"


@respx.mock
async def test_existing_unpaired_model_hints_pair_existing(env: Env) -> None:
    respx.get(f"{API}/glossaries/name/shop").respond(200, json=glossary())
    missing("ontologies/name/shop")
    with pytest.raises(LakeloomError) as info:
        await env.call()
    assert info.value.code == "conflict"
    assert info.value.hint == "call create_model with pair_existing=true"


@respx.mock
async def test_existing_paired_model_is_conflict(env: Env) -> None:
    respx.get(f"{API}/glossaries/name/shop").respond(200, json=glossary())
    respx.get(f"{API}/ontologies/name/shop").respond(200, json=ontology())
    with pytest.raises(LakeloomError) as info:
        await env.call()
    assert info.value.code == "conflict"


@respx.mock
async def test_orphan_ontology_is_conflict(env: Env) -> None:
    missing("glossaries/name/shop")
    respx.get(f"{API}/ontologies/name/shop").respond(200, json=ontology())
    with pytest.raises(LakeloomError) as info:
        await env.call()
    assert info.value.code == "conflict"


async def test_invalid_name_is_rejected_before_om(env: Env) -> None:
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(LakeloomError) as info:
            await env.call("Bad Name")
        assert info.value.code == "validation_error"
        assert not router.calls


async def test_display_name_and_description_limits(env: Env) -> None:
    with pytest.raises(LakeloomError):
        await env.call(display_name="x" * 300)
    with pytest.raises(LakeloomError):
        await env.call(description="x" * 5000)


async def test_no_public_url_is_validation_error() -> None:
    e = Env()
    e.settings = Settings()  # pyright: ignore[reportCallIssue]
    try:
        with pytest.raises(LakeloomError) as info:
            await e.call()
        assert info.value.code == "validation_error"
    finally:
        await e.om.aclose()


@respx.mock
async def test_token_bound_to_the_principal(env: Env) -> None:
    missing("glossaries/name/shop")
    missing("ontologies/name/shop")
    post = respx.post(f"{API}/glossaries").respond(201, json=glossary())
    preview = await env.call(description="a")
    assert isinstance(preview, ModelPreview)
    with pytest.raises(LakeloomError) as info:
        await env.call(description="a", principal="admin-2", confirm_token=preview.confirm_token)
    assert info.value.code == "validation_error"
    assert not post.called
