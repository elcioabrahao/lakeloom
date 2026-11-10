"""MCP resources for models and asset context (FR-PUB-01).

The URIs are ``lakeloom://models``, ``lakeloom://models/{slug}/ontology.{ext}`` (``ttl``,
``jsonld``, ``owl``, ``nt``), ``lakeloom://models/{slug}/context.jsonld`` and
``lakeloom://assets/{fqn}/context.md`` (FQN percent-encoded), each optionally pinned with
``?version=``. Only ``latest`` exists until releases arrive in M3.

A read builds the same :class:`~lakeloom.publish.artifacts.Artifact` as the HTTP API and returns
its body unchanged, so the bytes equal the HTTP response for that representation. The server
routes the URIs itself (rather than through the SDK's template matcher) because the model list
is dynamic, ``?version=`` must reach the builders and the FQN is percent-decoded here.
Failures are MCP errors whose ``data`` is ``{"error": {"code", "message", "hint"}}``.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import parse_qs, unquote

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, Resource, ResourceTemplate
from pydantic import AnyUrl

from lakeloom.errors import LakeloomError
from lakeloom.metrics import RESOURCE_READS
from lakeloom.publish.artifacts import (
    Artifact,
    asset_context_artifact,
    model_artifact,
    model_context_artifact,
    models_list_artifact,
)
from lakeloom.publish.negotiation import MediaType
from lakeloom.server.tools_common import READ_SCOPE, current_principal

if TYPE_CHECKING:
    from lakeloom.server.http_app import AppDeps

__all__ = ["MAX_LISTED_MODELS", "LakeloomMCP"]

SCHEME: Final = "lakeloom://"
MAX_LISTED_MODELS: Final = 100
MEDIA_BY_EXT: Final[dict[str, MediaType]] = {
    "ttl": "text/turtle",
    "jsonld": "application/ld+json",
    "owl": "application/rdf+xml",
    "nt": "application/n-triples",
}
_log = logging.getLogger("lakeloom.mcp")

TEMPLATES: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "lakeloom://models/{slug}/ontology.{ext}",
        "model_ontology",
        "The model as an ontology; ext is ttl, jsonld, owl or nt. Add ?version=latest to pin.",
        "text/turtle",
    ),
    (
        "lakeloom://models/{slug}/context.jsonld",
        "model_context",
        "The JSON-LD context of the model (minimal until M3).",
        "application/ld+json",
    ),
    (
        "lakeloom://assets/{fqn}/context.md",
        "asset_context",
        "Everything known about an asset as Markdown; percent-encode the table FQN.",
        "text/markdown",
    ),
)


def _mcp_error(error: LakeloomError) -> MCPError:
    code = INVALID_PARAMS if error.code in ("not_found", "validation_error") else INTERNAL_ERROR
    return MCPError(code=code, message=error.message, data=error.to_dict())


def _check_version(version: str | None) -> None:
    if version not in (None, "latest"):
        raise LakeloomError(
            "not_found",
            f"Unknown version {version!r}",
            "releases are published from M3; use version=latest",
        )


Kind = Literal["models", "ontology", "context", "asset"]


class LakeloomMCP(MCPServer[Any]):
    """:class:`MCPServer` that serves Lakeloom's publication artefacts as resources."""

    def __init__(self, deps: "AppDeps", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._deps = deps

    async def list_resources(self) -> list[Resource]:
        """``lakeloom://models`` and the concrete URIs of up to 100 models."""
        out = [
            Resource(
                uri="lakeloom://models",
                name="models",
                description="The semantic models (glossaries) Lakeloom publishes.",
                mime_type="application/json",
            )
        ]
        try:
            page = await self._deps.models.list_models(None, MAX_LISTED_MODELS)
        except LakeloomError as exc:  # OM down: still list what needs no lookup
            _log.warning("resources/list: model lookup failed (%s)", exc.code)
            return out
        for info in page.items[:MAX_LISTED_MODELS]:
            for ext, media in MEDIA_BY_EXT.items():
                out.append(
                    Resource(
                        uri=f"lakeloom://models/{info.slug}/ontology.{ext}",
                        name=f"{info.slug} ontology ({ext})",
                        mime_type=media,
                    )
                )
            out.append(
                Resource(
                    uri=f"lakeloom://models/{info.slug}/context.jsonld",
                    name=f"{info.slug} JSON-LD context",
                    mime_type="application/ld+json",
                )
            )
        return out

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        return [
            ResourceTemplate(uri_template=uri, name=name, description=description, mime_type=mime)
            for uri, name, description, mime in TEMPLATES
        ]

    async def read_resource(
        self, uri: AnyUrl | str, context: Context[Any, Any] | None = None
    ) -> Iterable[ReadResourceContents]:
        text = str(uri)
        kind: Kind | None = None
        try:
            kind, build = self._route(text)
            principal = None if context is None else current_principal(context)
            if principal is None or READ_SCOPE not in principal.scopes:
                raise LakeloomError(
                    "forbidden",
                    f"reading resources requires the {READ_SCOPE} scope",
                    f"token lacks {READ_SCOPE}",
                )
            artifact = await build()
        except LakeloomError as exc:
            RESOURCE_READS.labels(resource=kind or "unknown", outcome="error").inc()
            raise _mcp_error(exc) from exc
        except MCPError:
            raise
        except Exception as exc:
            RESOURCE_READS.labels(resource=kind or "unknown", outcome="error").inc()
            # Only the type: the text may carry upstream detail (NFR-SEC-11).
            _log.error("resource %s failed unexpectedly (%s)", text, type(exc).__name__)
            raise _mcp_error(
                LakeloomError(
                    "upstream_unavailable",
                    f"reading the resource failed unexpectedly ({type(exc).__name__})",
                    "retry; if it keeps failing, check the Lakeloom server logs",
                )
            ) from exc
        RESOURCE_READS.labels(resource=kind, outcome="ok").inc()
        return [ReadResourceContents(content=artifact.body.decode(), mime_type=artifact.media_type)]

    def _route(self, uri: str) -> tuple[Kind, Callable[[], Awaitable[Artifact]]]:
        if not uri.startswith(SCHEME):
            raise _not_found(uri)
        rest, _, query = uri[len(SCHEME) :].partition("?")
        query = query.partition("#")[0]
        rest = rest.partition("#")[0]
        versions = parse_qs(query).get("version")
        version = versions[0] if versions else None
        deps = self._deps
        head, _, tail = rest.partition("/")
        if head == "models" and not tail:
            return "models", lambda: _models(deps, version)
        if head == "models":
            slug_enc, _, name = tail.partition("/")
            slug = unquote(slug_enc)
            if name.startswith("ontology."):
                ext = name[len("ontology.") :]
                media = MEDIA_BY_EXT.get(ext)
                if media is None:
                    raise LakeloomError(
                        "validation_error",
                        f"unknown ontology format {ext!r}",
                        "use one of: " + ", ".join(MEDIA_BY_EXT),
                    )
                return "ontology", lambda: model_artifact(deps.models, slug, media, version=version)
            if name == "context.jsonld":
                return "context", lambda: model_context_artifact(deps.models, slug, version=version)
        if head == "assets" and tail.endswith("/context.md"):
            fqn = unquote(tail[: -len("/context.md")])
            return "asset", lambda: _asset(deps, fqn, version)
        raise _not_found(uri)


def _not_found(uri: str) -> LakeloomError:
    return LakeloomError(
        "not_found",
        f"Unknown resource {uri!r}",
        "list resources and resource templates to see the available URIs",
    )


async def _models(deps: "AppDeps", version: str | None) -> Artifact:
    _check_version(version)
    return await models_list_artifact(deps.models)


async def _asset(deps: "AppDeps", fqn: str, version: str | None) -> Artifact:
    _check_version(version)
    return await asset_context_artifact(deps.models, deps.om, deps.sources, fqn, "text/markdown")
