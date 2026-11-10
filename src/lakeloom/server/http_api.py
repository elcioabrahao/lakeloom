"""Read-only publication API under ``/v1`` (FR-PUB-02, FR-PUB-07).

Every route is a ``GET`` that needs the ``lakeloom:read`` scope, negotiates the representation
from ``Accept`` (a type the route does not offer gives ``406``), serves the artefact bytes
unmodified with ``ETag``, ``Last-Modified``, ``Link``, ``Vary: Accept`` and ``Cache-Control``,
and answers ``304`` to a matching ``If-None-Match``. Failures are typed JSON
(``{"error": {"code", "message", "hint"}}``), never HTML or a stack trace.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from email.utils import format_datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from lakeloom.errors import ErrorCode, LakeloomError
from lakeloom.publish.artifacts import (
    Artifact,
    asset_context_artifact,
    concept_artifact,
    etag_for,
    json_bytes,
    matches_if_none_match,
    model_artifact,
    model_context_artifact,
    models_list_artifact,
)
from lakeloom.publish.negotiation import MediaType, choose
from lakeloom.server.auth import Principal

if TYPE_CHECKING:
    from lakeloom.server.http_app import AppDeps

__all__ = [
    "STATUS_BY_CODE",
    "create_router",
    "lakeloom_error_handler",
    "request_validation_handler",
]

READ_SCOPE: Final = "lakeloom:read"
CACHE_CONTROL: Final = "private, max-age=0, must-revalidate"
STATUS_BY_CODE: Final[dict[ErrorCode, int]] = {
    "not_found": 404,
    "forbidden": 403,
    "validation_error": 400,
    "conflict": 409,
    "rate_limited": 429,
    "upstream_unavailable": 503,
}
MODEL_TYPES: Final[tuple[MediaType, ...]] = (
    "text/turtle",
    "application/ld+json",
    "application/rdf+xml",
    "application/n-triples",
)
CONCEPT_TYPES: Final[tuple[MediaType, ...]] = ("text/markdown", "application/json")
ASSET_TYPES: Final[tuple[MediaType, ...]] = (
    "text/markdown",
    "application/json",
    "application/ld+json",
)
JSON_TYPES: Final[tuple[MediaType, ...]] = ("application/json",)
CONTEXT_TYPES: Final[tuple[MediaType, ...]] = ("application/ld+json", "application/json")

_log = logging.getLogger("lakeloom.http")


def error_response(code: ErrorCode, message: str, hint: str, status: int | None = None) -> Response:
    body = LakeloomError(code, message, hint).to_dict()
    return JSONResponse(body, status_code=status or STATUS_BY_CODE[code])


async def lakeloom_error_handler(_: Request, exc: Exception) -> Response:
    err = cast(LakeloomError, exc)
    return error_response(err.code, err.message, err.hint)


async def request_validation_handler(_: Request, exc: Exception) -> Response:
    """A bad query parameter is a typed ``validation_error`` rather than FastAPI's 422."""
    problems = cast(list[dict[str, Any]], cast(RequestValidationError, exc).errors())
    loc = cast(tuple[object, ...], problems[0].get("loc", ())) if problems else ()
    where = ".".join(str(p) for p in loc if p != "query")
    return error_response(
        "validation_error",
        f"invalid parameter {where}" if where else "invalid request",
        "max_tokens must be an integer >= 1; limit an integer from 1 to 200",
    )


def _require_read(request: Request) -> None:
    state: dict[str, Any] = request.scope.get("state") or {}
    principal = state.get("principal")
    if not isinstance(principal, Principal) or READ_SCOPE not in principal.scopes:
        raise LakeloomError(
            "forbidden", f"this route requires the {READ_SCOPE} scope", "token lacks scope"
        )


def _content_type(media: str) -> str:
    textual = media.startswith("text/") or media == "application/json" or media.endswith("+json")
    return f"{media}; charset=utf-8" if textual else media


def _validators(artifact: Artifact) -> dict[str, str]:
    headers = {
        "ETag": artifact.etag,
        "Last-Modified": format_datetime(artifact.last_modified, usegmt=True),
        "Vary": "Accept",
        "Cache-Control": CACHE_CONTROL,
    }
    if artifact.links:
        headers["Link"] = ", ".join(artifact.links)
    return headers


def _respond(request: Request, artifact: Artifact) -> Response:
    headers = _validators(artifact)
    if matches_if_none_match(request.headers.get("if-none-match"), artifact.etag):
        return Response(status_code=304, headers=headers)
    headers["Content-Type"] = _content_type(artifact.media_type)
    return Response(content=artifact.body, headers=headers)  # bytes as hashed for the ETag


def _responses(
    offered: Sequence[MediaType], extra: dict[int | str, Any] | None = None
) -> dict[int | str, Any]:
    headers = {
        name: {"schema": {"type": "string"}, "description": text}
        for name, text in (
            ("ETag", "Strong validator: sha256 of the body."),
            ("Last-Modified", "When the source changed in OpenMetadata."),
            ("Link", 'The OpenMetadata entity the artefact came from (rel="via").'),
        )
    }
    error = {"description": "Typed error", "content": {"application/json": {"schema": _ERROR}}}
    out: dict[int | str, Any] = {
        200: {
            "description": "The artefact in the negotiated media type",
            "content": {m: {} for m in offered},
            "headers": headers,
        },
        304: {"description": "Not modified (If-None-Match matched)"},
        400: error,
        401: {"description": "Missing or invalid bearer token"},
        403: error,
        404: error,
        406: error,
        500: error,
        503: error,
    }
    out.update(extra or {})
    return out


_ERROR: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": sorted(STATUS_BY_CODE)},
                "message": {"type": "string"},
                "hint": {"type": "string"},
            },
            "required": ["code", "message", "hint"],
        }
    },
    "required": ["error"],
}


def create_router(deps: "AppDeps") -> APIRouter:
    """The ``/v1`` router; dependencies are read per request (tests swap them)."""
    router = APIRouter(prefix="/v1", tags=["publication"], dependencies=[Depends(_require_read)])

    async def serve(
        request: Request,
        offered: Sequence[MediaType],
        build: Callable[[MediaType], Awaitable[Artifact]],
    ) -> Response:
        accept = ", ".join(request.headers.getlist("accept")) or None
        media = choose(accept, offered)
        if media is None:
            return error_response(
                "validation_error",
                "not acceptable",
                "supported: " + ", ".join(offered),
                status=406,
            )
        try:
            artifact = await build(media)
        except LakeloomError as exc:
            return error_response(exc.code, exc.message, exc.hint)
        except Exception as exc:
            _log.error("http api request failed unexpectedly (%s)", type(exc).__name__)
            return JSONResponse(
                {
                    "error": {
                        "code": "upstream_unavailable",
                        "message": "internal error",
                        "hint": "check the server logs",
                    }
                },
                status_code=500,
            )
        return _respond(request, artifact)

    @router.get(
        "/models",
        summary="List the models",
        response_class=Response,
        responses=_responses(JSON_TYPES),
    )
    async def list_models(
        request: Request,
        cursor: Annotated[str | None, Query(description="next_cursor of a previous page")] = None,
        limit: Annotated[int | None, Query(ge=1, le=200, description="page size")] = None,
    ) -> Response:
        return await serve(
            request,
            JSON_TYPES,
            lambda _: models_list_artifact(deps.models, cursor=cursor, limit=limit),
        )

    @router.get(
        "/models/{slug}",
        summary="The model as an ontology (Turtle, JSON-LD, RDF/XML or N-Triples)",
        response_class=Response,
        responses=_responses(MODEL_TYPES),
    )
    async def get_model(request: Request, slug: str) -> Response:
        return await serve(
            request, MODEL_TYPES, lambda m: model_artifact(deps.models, slug, m, version=None)
        )

    @router.get(
        "/models/{slug}/versions",
        summary="The published versions of a model",
        response_class=Response,
        responses=_responses(JSON_TYPES),
    )
    async def list_versions(request: Request, slug: str) -> Response:
        async def build(_: MediaType) -> Artifact:
            # Resolves the model (404) and supplies its Last-Modified and Link.
            base = await model_artifact(deps.models, slug, "text/turtle", version=None)
            data = [{"version": "latest", "media_types": list(MODEL_TYPES)}]
            body = json_bytes(data)
            return base.model_copy(
                update={"body": body, "media_type": "application/json", "etag": etag_for(body)}
            )

        return await serve(request, JSON_TYPES, build)

    @router.get(
        "/models/{slug}/versions/{version}",
        summary="One version of the model (only latest until releases arrive in M3)",
        response_class=Response,
        responses=_responses(MODEL_TYPES),
    )
    async def get_model_version(request: Request, slug: str, version: str) -> Response:
        return await serve(
            request, MODEL_TYPES, lambda m: model_artifact(deps.models, slug, m, version=version)
        )

    @router.get(
        "/models/{slug}/context.jsonld",
        summary="The JSON-LD context of the model (minimal until M3)",
        response_class=Response,
        responses=_responses(CONTEXT_TYPES),
    )
    async def get_model_context(request: Request, slug: str) -> Response:
        return await serve(
            request,
            CONTEXT_TYPES,
            lambda _: model_context_artifact(deps.models, slug, version=None),
        )

    @router.get(
        "/concepts/{concept:path}",
        summary="A concept by IRI, FQN, id or name",
        response_class=Response,
        responses=_responses(CONCEPT_TYPES),
    )
    async def get_concept(request: Request, concept: str) -> Response:
        def build(media: MediaType) -> Awaitable[Artifact]:
            return concept_artifact(
                deps.models, deps.om, concept, _narrow(media, "text/markdown", "application/json")
            )

        return await serve(request, CONCEPT_TYPES, build)

    @router.get(
        "/assets/{fqn:path}/context",
        summary="Everything known about an asset, for an agent's context window",
        response_class=Response,
        responses=_responses(ASSET_TYPES),
    )
    async def get_asset_context(
        request: Request,
        fqn: str,
        max_tokens: Annotated[
            int | None, Query(ge=1, description="cut the content to about this many tokens")
        ] = None,
    ) -> Response:
        def build(media: MediaType) -> Awaitable[Artifact]:
            return asset_context_artifact(
                deps.models,
                deps.om,
                deps.sources,
                fqn,
                _narrow(media, "text/markdown", "application/json", "application/ld+json"),
                max_tokens=max_tokens,
            )

        return await serve(request, ASSET_TYPES, build)

    return router


def _narrow[T: str](media: MediaType, *allowed: T) -> T:
    """``media`` as one of the ``allowed`` literals (the negotiation offered only those)."""
    for candidate in allowed:
        if candidate == media:
            return candidate
    raise LakeloomError("validation_error", f"{media} is not available here", "use Accept")
