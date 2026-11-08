"""FastAPI app: health, readiness, metrics, request ids, bearer auth (NFR-OPS-01..03).

Middleware order (outermost first): request id + metrics + access log, then bearer auth.
CORS is deliberately not added (NFR-SEC-15). Sub-applications sit behind the same auth:
``endpoints`` are served at exactly one path (the MCP endpoint ``/mcp``), ``extra_mounts``
under a path prefix. Startup/shutdown lives in one ``lifespan``; ``lifespans`` (such as
the MCP session manager) are entered inside it and exited before dependencies close.
"""

import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakeloom.config import ConfigError, Settings
from lakeloom.logging import request_id_var
from lakeloom.metrics import HTTP_REQUESTS, REGISTRY
from lakeloom.om.client import OMClient
from lakeloom.server.auth import (
    PRM_PATH,
    PUBLIC_PATHS,
    BearerAuthMiddleware,
    TokenValidator,
    protected_resource_metadata,
)
from lakeloom.server.health import (
    Check,
    git_workspace_check,
    om_check,
    run_checks,
    state_store_check,
)
from lakeloom.state.db import create_engine

_log = logging.getLogger("lakeloom.http")
_IDP_TIMEOUT = 5.0


@dataclass
class AppDeps:
    settings: Settings
    om: OMClient
    engine: AsyncEngine
    validator: TokenValidator
    # Closed by the app lifespan (the validator's shared client).
    idp_http: httpx.AsyncClient | None = None


def build_deps(settings: Settings) -> AppDeps:
    """Wire settings into live dependencies; fail fast when required config is missing."""
    required = {
        "LAKELOOM_OM_URL": settings.om_url,
        "LAKELOOM_OIDC_ISSUER": settings.oidc_issuer,
        "LAKELOOM_PUBLIC_URL": settings.public_url,
    }
    missing = [name for name, value in required.items() if not value]
    if missing or settings.om_url is None or settings.oidc_issuer is None:
        raise ConfigError(f"missing required configuration: set {', '.join(missing)}")
    idp_http = httpx.AsyncClient(timeout=_IDP_TIMEOUT)
    validator = TokenValidator(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        http=idp_http,
        jwks_url=settings.oidc_jwks_url,
        leeway=settings.oidc_clock_skew_seconds,
        agent_claim=settings.oidc_agent_claim,
        scope_group_map=settings.scope_group_map,
    )
    return AppDeps(
        settings=settings,
        om=OMClient(str(settings.om_url), settings.om_bot_token),
        engine=create_engine(settings.db_url),
        validator=validator,
        idp_http=idp_http,
    )


class RequestContextMiddleware:
    """Pure ASGI: request id (accept/generate/echo), request counter, access log.

    Never logs headers. The route label is the route template, the mount prefix for
    mounted sub-applications, or ``unmatched``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next((v for k, v in scope["headers"] if k.lower() == b"x-request-id"), None)
        request_id = _clean_request_id(incoming) or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message["headers"] if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            label = _route_label(scope)
            HTTP_REQUESTS.labels(
                method=scope["method"], route=label or "unmatched", status=str(status)
            ).inc()
            _log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "route": label or "unmatched",
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)


def _route_label(scope: Scope) -> str | None:
    # Starlette sets scope["route"] to the matched route; for a Mount that is the Mount,
    # whose ``path`` is the prefix, unless the mounted app routes again and overwrites it.
    template = getattr(scope.get("route"), "path", None)
    return template if isinstance(template, str) and template else None


def _clean_request_id(raw: bytes | None) -> str | None:
    if not raw:
        return None
    value = raw.decode("latin-1").strip()
    if not value or len(value) > 128 or not value.isprintable():
        return None
    return value


def create_app(
    deps: AppDeps,
    *,
    extra_mounts: Sequence[tuple[str, ASGIApp]] = (),
    endpoints: Sequence[tuple[str, ASGIApp]] = (),
    lifespans: Sequence[Callable[[], AbstractAsyncContextManager[None]]] = (),
    checks: Sequence[Check] | None = None,
) -> FastAPI:
    """Build the app. ``checks`` overrides the default readiness checks (tests).

    ``endpoints`` are ASGI apps served at exactly their path (all methods); a Starlette
    ``Mount`` cannot serve its bare prefix without a redirect, which MCP clients must
    not need to follow. ``lifespans`` run for the lifetime of the app.
    """
    settings = deps.settings
    resource_metadata_url = f"{settings.public_base_url}{PRM_PATH}"
    readiness: Sequence[Check] = (
        checks
        if checks is not None
        else (
            om_check(deps.om),
            state_store_check(deps.engine),
            git_workspace_check(settings.data_dir / "git"),
        )
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            async with AsyncExitStack() as stack:
                for factory in lifespans:
                    await stack.enter_async_context(factory())
                yield
        finally:
            if deps.idp_http is not None:
                await deps.idp_http.aclose()
            await deps.om.aclose()
            await deps.engine.dispose()

    app = FastAPI(title="Lakeloom", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        results = await run_checks(readiness)
        ok = all(r.ok for r in results)
        body = {
            "status": "ok" if ok else "fail",
            "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results],
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.get(PRM_PATH)
    async def prm() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return protected_resource_metadata(settings)

    for path, endpoint in endpoints:
        app.router.routes.append(Route(path, endpoint=endpoint))
    for path, mounted in extra_mounts:
        app.mount(path, mounted)

    app.add_middleware(
        BearerAuthMiddleware,
        validator=deps.validator,
        resource_metadata_url=resource_metadata_url,
        public_paths=PUBLIC_PATHS,
    )
    app.add_middleware(RequestContextMiddleware)
    return app
