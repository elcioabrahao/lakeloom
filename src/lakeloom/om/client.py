"""Async OpenMetadata client for the M0 surface (FR-OM-01, FR-OM-06, NFR-SEC-11).

Paths are keys of the committed spec (``servers[0].url`` is ``/api``), verified by
``tests/unit/om/test_spec_paths.py``. The bot token is only ever placed in the request
header; it is never part of an exception message, log line or repr.
"""

import re
import time
from collections.abc import Sequence
from typing import ClassVar, Literal
from urllib.parse import quote

import httpx
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import SecretStr

from lakeloom.metrics import OM_REQUEST_SECONDS
from lakeloom.om.models import (
    OMCapabilities,
    OMEntityRef,
    OMGlossary,
    OMGlossaryTerm,
    OMLineage,
    OMPage,
    OMTable,
    OMVersion,
    RdfStatus,
)
from lakeloom.telemetry import record_om_request, tracer

API_PREFIX = "/api"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
GLOSSARY_FIELDS = ("owners", "tags", "domains", "termCount")
TERM_FIELDS = ("parent", "synonyms", "relatedTerms", "conceptMappings", "owners", "tags", "iri")
TABLE_FIELDS = (
    "columns",
    "tags",
    "owners",
    "domains",
    "dataProducts",
    "tableConstraints",
)
ExportFormat = Literal["turtle", "rdfxml", "ntriples", "jsonld"]
_EXPORT_MIME: dict[str, str] = {
    "turtle": "text/turtle",
    "rdfxml": "application/rdf+xml",
    "ntriples": "application/n-triples",
    "jsonld": "application/ld+json",
}


class OMError(Exception):
    """OpenMetadata answered with an error (or could not be used)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OMUnavailable(OMError):
    """OpenMetadata is down, too slow, or answered 5xx."""


class OMClient:
    PATHS: ClassVar[dict[str, tuple[str, str]]] = {
        "get_version": ("GET", "/v1/system/version"),
        "get_rdf_status": ("GET", "/v1/rdf/status"),
        "get_table_by_name": ("GET", "/v1/tables/name/{fqn}"),
        "list_glossaries": ("GET", "/v1/glossaries"),
        "get_glossary_by_name": ("GET", "/v1/glossaries/name/{name}"),
        "list_terms": ("GET", "/v1/glossaryTerms"),
        "get_term_by_id": ("GET", "/v1/glossaryTerms/{id}"),
        "get_term_by_name": ("GET", "/v1/glossaryTerms/name/{fqn}"),
        "search_terms": ("GET", "/v1/glossaryTerms/search"),
        "get_term_assets": ("GET", "/v1/glossaryTerms/{id}/assets"),
        "export_ontology": ("GET", "/v1/glossaries/{id}/exportOntology"),
        "get_lineage": ("GET", "/v1/lineage/getLineage"),
    }

    def __init__(
        self,
        base_url: str,
        token: SecretStr | None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=timeout)

    def __repr__(self) -> str:
        return f"OMClient(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @staticmethod
    def _observe(span: Span, name: str, status: str, started: float, *, ok: bool = False) -> None:
        if not ok:
            span.set_status(Status(StatusCode.ERROR))
        record_om_request(name, status, time.perf_counter() - started)

    async def _request(
        self,
        name: str,
        *,
        path_args: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        method, path = self.PATHS[name]
        if path_args:
            # Every path segment is percent-encoded, including "/" (FQNs may hold quotes).
            path = path.format(**{k: quote(v, safe="") for k, v in path_args.items()})
        headers = {"Accept": accept}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        url = f"{self._base_url}{API_PREFIX}{path}"
        started = time.perf_counter()
        # Span attributes: the operation name and status only, never the URL or headers.
        with tracer().start_as_current_span(
            f"om.{name}", attributes={"lakeloom.om.operation": name, "http.method": method}
        ) as span:
            try:
                with OM_REQUEST_SECONDS.labels(operation=name).time():
                    response = await self._http.request(method, url, headers=headers, params=params)
            except httpx.TimeoutException as exc:
                self._observe(span, name, "error", started)
                raise OMUnavailable(f"OpenMetadata timed out ({type(exc).__name__})") from None
            except httpx.TransportError as exc:
                self._observe(span, name, "error", started)
                raise OMUnavailable(
                    f"cannot connect to OpenMetadata ({type(exc).__name__})"
                ) from None
            status = response.status_code
            span.set_attribute("http.status_code", status)
            self._observe(span, name, str(status), started, ok=status < 400)
        if status >= 500:
            raise OMUnavailable(f"OpenMetadata returned HTTP {status}", status)
        if status >= 400:
            raise OMError(f"OpenMetadata returned HTTP {status}", status)
        return response

    async def get_version(self) -> OMVersion:
        return OMVersion.model_validate(_json(await self._request("get_version")))

    async def get_rdf_status(self) -> RdfStatus:
        return RdfStatus.model_validate(_json(await self._request("get_rdf_status")))

    async def get_table_by_fqn(
        self,
        fqn: str,
        *,
        fields: Sequence[str] = TABLE_FIELDS,
    ) -> OMTable | None:
        """Look a table up by FQN; ``None`` when OM has no such table (FR-SRC-07)."""
        params = {"fields": ",".join(fields)} if fields else None
        try:
            response = await self._request(
                "get_table_by_name", path_args={"fqn": fqn}, params=params
            )
        except OMError as exc:
            if exc.status == 404:
                return None
            raise
        return OMTable.model_validate(_json(response))

    async def _get_or_none(
        self,
        name: str,
        *,
        path_args: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        try:
            return await self._request(name, path_args=path_args, params=params)
        except OMError as exc:
            if exc.status == 404:
                return None
            raise

    async def list_glossaries(
        self, *, after: str | None = None, limit: int = 50
    ) -> OMPage[OMGlossary]:
        params = {"limit": str(limit), "fields": ",".join(GLOSSARY_FIELDS)}
        if after:
            params["after"] = after
        response = await self._request("list_glossaries", params=params)
        return OMPage[OMGlossary].model_validate(_json(response))

    async def get_glossary_by_name(
        self, name: str, fields: Sequence[str] = GLOSSARY_FIELDS
    ) -> OMGlossary | None:
        params = {"fields": ",".join(fields)} if fields else None
        response = await self._get_or_none(
            "get_glossary_by_name", path_args={"name": name}, params=params
        )
        return None if response is None else OMGlossary.model_validate(_json(response))

    async def list_terms(
        self,
        glossary_fqn: str,
        *,
        after: str | None = None,
        limit: int = 100,
        fields: Sequence[str] = TERM_FIELDS,
    ) -> OMPage[OMGlossaryTerm]:
        """Terms of one glossary. OM filters by glossary *id*, so a name is resolved first.

        Raises ``OMError`` (status 404) when the glossary does not exist.
        """
        glossary_id = glossary_fqn
        if not _UUID.match(glossary_fqn):
            glossary = await self.get_glossary_by_name(glossary_fqn, fields=())
            if glossary is None:
                raise OMError(f"glossary {glossary_fqn!r} not found", 404)
            glossary_id = glossary.id
        params = {"glossary": glossary_id, "limit": str(limit)}
        if fields:
            params["fields"] = ",".join(fields)
        if after:
            params["after"] = after
        response = await self._request("list_terms", params=params)
        return OMPage[OMGlossaryTerm].model_validate(_json(response))

    async def get_term(
        self, id_or_fqn: str, fields: Sequence[str] = TERM_FIELDS
    ) -> OMGlossaryTerm | None:
        params = {"fields": ",".join(fields)} if fields else None
        if _UUID.match(id_or_fqn):
            response = await self._get_or_none(
                "get_term_by_id", path_args={"id": id_or_fqn}, params=params
            )
        else:
            response = await self._get_or_none(
                "get_term_by_name", path_args={"fqn": id_or_fqn}, params=params
            )
        return None if response is None else OMGlossaryTerm.model_validate(_json(response))

    async def search_terms(
        self, q: str, *, glossary_fqn: str | None = None, limit: int = 20
    ) -> list[OMGlossaryTerm]:
        params = {"q": q, "limit": str(limit)}
        if glossary_fqn:
            params["glossaryFqn"] = glossary_fqn
        response = await self._request("search_terms", params=params)
        return OMPage[OMGlossaryTerm].model_validate(_json(response)).data

    async def get_term_assets(
        self, term_id: str, *, offset: int = 0, limit: int = 50
    ) -> OMPage[OMEntityRef]:
        """Assets tagged with a term. The spec types the body as one ``EntityReference``;
        the live server returns a ``{"data": [...], "paging": {"total": n}}`` list."""
        params = {"offset": str(offset), "limit": str(limit)}
        response = await self._request("get_term_assets", path_args={"id": term_id}, params=params)
        return OMPage[OMEntityRef].model_validate(_json(response))

    async def export_ontology(
        self, glossary_id: str, fmt: ExportFormat = "turtle", *, include_relations: bool = True
    ) -> str:
        """Export a glossary as RDF text (needs RDF enabled in OM).

        The live server answers with the serialisation itself (``text/turtle``,
        ``application/rdf+xml``, ``application/n-triples`` or ``application/ld+json``), not
        a JSON wrapper, so the response body is returned verbatim.
        """
        params = {"format": fmt, "includeRelations": "true" if include_relations else "false"}
        response = await self._request(
            "export_ontology",
            path_args={"id": glossary_id},
            params=params,
            accept=_EXPORT_MIME[fmt],
        )
        return response.text

    async def get_lineage(
        self,
        entity_type: str,
        fqn: str,
        *,
        upstream_depth: int = 1,
        downstream_depth: int = 1,
    ) -> OMLineage:
        params = {
            "fqn": fqn,
            "type": entity_type,
            "upstreamDepth": str(upstream_depth),
            "downstreamDepth": str(downstream_depth),
        }
        response = await self._request("get_lineage", params=params)
        return OMLineage.model_validate(_json(response))

    async def get_capabilities(self) -> OMCapabilities:
        """Probe version and RDF status; never raises (FR-OM-06)."""
        try:
            version = await self.get_version()
        except OMError as exc:
            return OMCapabilities(reachable=False, error=str(exc))
        except Exception as exc:
            return OMCapabilities(reachable=False, error=f"unexpected error: {type(exc).__name__}")
        try:
            rdf = await self.get_rdf_status()
        except OMError as exc:
            if exc.status == 404:
                return OMCapabilities(reachable=True, version=version.version)
            return OMCapabilities(reachable=True, version=version.version, error=str(exc))
        except Exception as exc:
            error = f"unexpected error: {type(exc).__name__}"
            return OMCapabilities(reachable=True, version=version.version, error=error)
        return OMCapabilities(
            reachable=True, version=version.version, rdf_enabled=rdf.enabled, rdf=rdf
        )


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        raise OMError("OpenMetadata returned a non-JSON body", response.status_code) from None
