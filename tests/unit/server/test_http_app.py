"""HTTP app: health, readiness, metrics, request ids, auth wiring (NFR-OPS-01..03, NFR-SEC-11)."""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.config import ConfigError, Settings
from lakeloom.om.client import OMClient
from lakeloom.server.auth import TokenValidator
from lakeloom.server.health import Check, CheckResult
from lakeloom.server.http_app import AppDeps, build_deps, create_app
from lakeloom.state.db import create_engine

BOT_TOKEN = "super-secret-bot-token-xyz"  # noqa: S105


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        om_url="http://om.test:8585",  # pyright: ignore[reportArgumentType]
        om_bot_token=SecretStr(BOT_TOKEN),
        oidc_issuer="https://idp.test/realms/lakeloom",
        public_url="https://lakeloom.test",  # pyright: ignore[reportArgumentType]
        db_url=f"sqlite:///{tmp_path}/l.db",
        data_dir=tmp_path,
    )


@pytest.fixture
async def deps(tmp_path: Path) -> AsyncIterator[AppDeps]:
    settings = _settings(tmp_path)
    engine: AsyncEngine = create_engine(settings.db_url)
    validator = TokenValidator(
        issuer="https://idp.test/realms/lakeloom",
        audience="lakeloom",
        http=httpx.AsyncClient(),
        jwks_url="https://idp.test/jwks",
    )
    om = OMClient("http://om.test:8585", settings.om_bot_token)
    yield AppDeps(settings=settings, om=om, engine=engine, validator=validator)
    await om.aclose()
    await engine.dispose()


def _check(name: str, ok: bool, detail: str | None = None) -> Check:
    async def check() -> CheckResult:
        return CheckResult(name, ok, detail)

    return check


def _client(deps: AppDeps, checks: Sequence[Check] | None = None) -> httpx.AsyncClient:
    app = create_app(deps, checks=checks)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_nfr_ops_01_healthz_no_auth(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_nfr_ops_01_readyz_all_green(deps: AppDeps) -> None:
    checks = [_check("openmetadata", True), _check("state_store", True)]
    async with _client(deps, checks) as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert [x["name"] for x in body["checks"]] == ["openmetadata", "state_store"]


async def test_nfr_ops_01_readyz_reports_failing_check(deps: AppDeps) -> None:
    checks = [_check("openmetadata", False, "down"), _check("state_store", True)]
    async with _client(deps, checks) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "fail"
    failing = [x for x in body["checks"] if not x["ok"]]
    assert failing[0]["name"] == "openmetadata"
    assert failing[0]["ok"] is False


async def test_nfr_ops_01_default_readyz_check_names(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/readyz")
    assert [x["name"] for x in r.json()["checks"]] == [
        "openmetadata",
        "state_store",
        "git_workspace",
    ]


async def test_nfr_ops_02_metrics_exposes_request_counter(deps: AppDeps) -> None:
    async with _client(deps) as c:
        await c.get("/healthz")
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert 'lakeloom_http_requests_total{method="GET",route="/healthz",status="200"}' in r.text
    assert "lakeloom_om_request_seconds" in r.text
    assert "lakeloom_tool_calls_total" in r.text


async def test_nfr_ops_02_unmatched_route_label(deps: AppDeps) -> None:
    async with _client(deps) as c:
        await c.get("/healthz/../nope/12345", headers={"Authorization": "Bearer x"})
        r = await c.get("/metrics")
    assert "12345" not in r.text


async def test_nfr_ops_03_request_id_echoed(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/healthz", headers={"X-Request-ID": "abc-1"})
        r2 = await c.get("/healthz")
    assert r.headers["x-request-id"] == "abc-1"
    assert len(r2.headers["x-request-id"]) == 36


async def test_protected_route_requires_token(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/something")
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert (
        "https://lakeloom.test/.well-known/oauth-protected-resource"
        in (r.headers["www-authenticate"])
    )


async def test_prm_document_is_public(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    assert r.json()["resource"] == "https://lakeloom.test"


async def test_openapi_json_is_protected_no_cors(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/healthz", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in r.headers


async def test_extra_mounts_are_served_behind_auth(deps: AppDeps) -> None:
    async def sub(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi"})

    app = create_app(deps, extra_mounts=[("/sub", sub)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/sub/x")
    assert r.status_code == 401


async def test_nfr_sec_11_bot_token_not_in_logs(
    deps: AppDeps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client(deps) as c:
        await c.get("/readyz")
        await c.get("/healthz", headers={"Authorization": "Bearer sekrit-bearer"})
        await c.get("/x", headers={"Authorization": "Bearer sekrit-bearer"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert BOT_TOKEN not in text
    assert "sekrit-bearer" not in text


def test_build_deps_requires_config() -> None:
    with pytest.raises(ConfigError) as exc:
        build_deps(Settings())
    msg = str(exc.value)
    for var in ("LAKELOOM_OM_URL", "LAKELOOM_OIDC_ISSUER", "LAKELOOM_PUBLIC_URL"):
        assert var in msg


async def test_build_deps_and_lifespan_close(tmp_path: Path) -> None:
    deps = build_deps(_settings(tmp_path))
    app = create_app(deps, checks=[_check("openmetadata", True)])
    async with app.router.lifespan_context(app):
        pass
    assert deps.validator is not None


async def test_lifespans_run_inside_app_lifespan(deps: AppDeps) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def hook() -> AsyncIterator[None]:
        events.append("enter")
        yield
        events.append("exit")

    app = create_app(deps, lifespans=[hook])
    assert events == []
    async with app.router.lifespan_context(app):
        assert events == ["enter"]
    assert events == ["enter", "exit"]
