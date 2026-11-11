"""Async OpenMetadata client (FR-OM-01, FR-OM-02, FR-OM-06, NFR-SEC-11).

Reads cover the M0/M1 surface; M2a adds the write side: ontology change sets (drafts,
operations, submit, discard; never apply), glossary creation and axiom validation. OM
behaviour behind the write methods is recorded in ADR 0011.

Paths are keys of the committed spec (``servers[0].url`` is ``/api``), verified by
``tests/unit/om/test_spec_paths.py``. The bot token is only ever placed in the request
header; it is never part of an exception message, log line or repr.
"""

import asyncio
import re
import time
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any, ClassVar, Literal, cast
from urllib.parse import quote

import httpx
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import SecretStr

from lakeloom.metrics import OM_REQUEST_SECONDS
from lakeloom.om.models import (
    OMAxiomValidation,
    OMCapabilities,
    OMChangeSet,
    OMEditLease,
    OMEntityRef,
    OMGlossary,
    OMGlossaryTerm,
    OMLineage,
    OMNativeExport,
    OMOntology,
    OMOntologyState,
    OMPage,
    OMTable,
    OMVersion,
    RdfStatus,
)
from lakeloom.om.native_rdf import build_graph
from lakeloom.telemetry import record_om_request, start_span

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


LEASE_SECONDS = 60
CHANGE_SET_RESOURCE = "ontologyChangeSet"
# A glossary operation whose baseVersion is stale fails at apply with HTTP 400 and this message.
_STALE_BASE = re.compile(r"expected version \S+ but found \S+")
_UUID_IN_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_DETAIL_MAX = 500


ConflictKind = Literal["stale_base", "lease", "exists", "stale_revision"]
_MEMBER_PAGE = 100
_EXPORT_ATTEMPTS = 3
_MAX_MEMBER_PAGES = 100


class OMError(Exception):
    """OpenMetadata answered with an error (or could not be used).

    ``detail`` is OM's own ``message`` (untrusted text, capped), kept out of ``str(exc)``.
    """

    def __init__(self, message: str, status: int | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class OMUnavailable(OMError):
    """OpenMetadata is down, too slow, or answered 5xx."""


class OMConflict(OMError):
    """OM refused a write because of concurrent state (ADR 0011).

    HTTP 409 (name taken, edit lease held or lost), 412 (native ontology draft older than the
    working revision) and HTTP 400 "expected version X but found Y" (an operation's
    ``baseVersion`` is stale). ``entity_ids`` holds the ids OM named in its message: the
    operation id for a stale base, the resource id for a lease conflict. ``kind`` says which:
    ``exists`` (409 name taken), ``lease`` (any other 409), ``stale_base`` (400 expected
    version) or ``stale_revision`` (412).
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        detail: str | None = None,
        entity_ids: tuple[str, ...] = (),
        kind: ConflictKind = "lease",
    ) -> None:
        super().__init__(message, status, detail)
        self.entity_ids = entity_ids
        self.kind: ConflictKind = kind


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
        # M2a writes (FR-OM-02, ADR 0011)
        "create_glossary": ("POST", "/v1/glossaries"),
        "get_glossary_by_id": ("GET", "/v1/glossaries/{id}"),
        "create_change_set": ("POST", "/v1/ontologyChangeSets"),
        "get_change_set": ("GET", "/v1/ontologyChangeSets/{id}"),
        "replace_operations": ("PUT", "/v1/ontologyChangeSets/{id}/operations"),
        "submit_change_set": ("POST", "/v1/ontologyChangeSets/{id}/submit"),
        "discard_change_set": ("POST", "/v1/ontologyChangeSets/{id}/discard"),
        "acquire_edit_lease": ("POST", "/v1/ontologyEditLocks/acquire"),
        "release_edit_lease": ("DELETE", "/v1/ontologyEditLocks/{resourceType}/{resourceId}"),
        "validate_axiom": ("POST", "/v1/ontologyAxioms/validate"),
        # Native ontologies (hybrid model, Amendment A; ADR 0011)
        "create_ontology": ("POST", "/v1/ontologies"),
        "get_ontology_by_name": ("GET", "/v1/ontologies/name/{fqn}"),
        "get_ontology_state": ("GET", "/v1/ontologies/{id}/state"),
        "list_ontology_concepts": ("GET", "/v1/ontologyConcepts"),
        "list_ontology_properties": ("GET", "/v1/ontologyProperties"),
        "list_ontology_axioms": ("GET", "/v1/ontologyAxioms"),
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
        # Error status only for 5xx and transport failures: a 404 lookup is a normal answer.
        if not ok:
            span.set_status(Status(StatusCode.ERROR, status))
        record_om_request(name, status, time.perf_counter() - started)

    async def _request(
        self,
        name: str,
        *,
        path_args: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        accept: str = "application/json",
        json: object | None = None,
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
        with start_span(
            f"om.{name}", attributes={"lakeloom.om.operation": name, "http.method": method}
        ) as span:
            try:
                with OM_REQUEST_SECONDS.labels(operation=name).time():
                    response = await self._http.request(
                        method, url, headers=headers, params=params, json=json
                    )
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
            self._observe(span, name, str(status), started, ok=status < 500)
        if status >= 500:
            raise OMUnavailable(f"OpenMetadata returned HTTP {status}", status)
        if status >= 400:
            raise _client_error(response)
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

    # --- Writes (M2a): glossary creation, change sets, axiom validation -------------------

    async def create_glossary(
        self, *, name: str, display_name: str | None, description: str
    ) -> OMGlossary:
        """Create a glossary (an ontology model, ADR 0007). Direct API, not a change set:
        a model is not one of the FR-OM-02 change-set-only kinds (ADR 0011).

        Raises ``OMConflict`` when the name is taken.
        """
        payload: dict[str, object] = {"name": name, "description": description}
        if display_name is not None:
            payload["displayName"] = display_name
        response = await self._request("create_glossary", json=payload)
        return OMGlossary.model_validate(_json(response))

    async def _glossary_name(self, glossary: str) -> str:
        """rc1 change sets reference glossaries by name; resolve an id to its FQN."""
        if not _UUID.match(glossary):
            return glossary
        response = await self._request("get_glossary_by_id", path_args={"id": glossary})
        return OMGlossary.model_validate(_json(response)).fully_qualified_name

    async def create_change_set(
        self,
        *,
        name: str,
        glossary_id: str | None,
        description: str,
        operations: Sequence[Mapping[str, Any]],
        display_name: str | None = None,
        ontology: str | None = None,
        base_working_revision: int | None = None,
    ) -> OMChangeSet:
        """Create a Draft change set (FR-OM-02).

        Exactly one target: a glossary (id or FQN; terms, relationships, mappings) or a native
        OM ontology (name, with its current ``workingRevision``; concepts, properties,
        axioms). rc1 rejects mixing the two (ADR 0011). Operations get a fresh ``id`` and
        ``state: ACTIVE`` unless given. Raises ``OMConflict`` when the name is taken.
        """
        if (glossary_id is None) == (ontology is None):
            raise ValueError("a change set needs exactly one of glossary_id or ontology")
        if ontology is not None and base_working_revision is None:
            raise ValueError("an ontology change set needs base_working_revision")
        payload: dict[str, object] = {
            "name": name,
            "displayName": display_name if display_name is not None else name,
            "description": description,
            "operations": [_prepare_operation(op) for op in operations],
        }
        if glossary_id is not None:
            payload["glossaries"] = [await self._glossary_name(glossary_id)]
        else:
            payload["ontology"] = ontology
            payload["baseWorkingRevision"] = base_working_revision
        response = await self._request("create_change_set", json=payload)
        return OMChangeSet.model_validate(_json(response))

    async def get_change_set(self, id: str) -> OMChangeSet | None:
        response = await self._get_or_none("get_change_set", path_args={"id": id})
        return None if response is None else OMChangeSet.model_validate(_json(response))

    @asynccontextmanager
    async def _lease(self, change_set_id: str) -> AsyncGenerator[OMEditLease]:
        """Hold rc1's authoring lease on a change set for one command, then release it.

        Every operations/submit/discard call needs one. A lease held by another session
        raises ``OMConflict`` (409). Release is best effort: an unreleased lease expires
        after ``LEASE_SECONDS``.
        """
        payload = {
            "resourceType": CHANGE_SET_RESOURCE,
            "resourceId": change_set_id,
            "sessionId": f"lakeloom-{uuid.uuid4()}",
            "leaseSeconds": LEASE_SECONDS,
        }
        response = await self._request("acquire_edit_lease", json=payload)
        lease = OMEditLease.model_validate(_json(response))
        try:
            yield lease
        finally:
            # Shielded: a cancelled caller must not leave the lease held until it expires.
            release = asyncio.ensure_future(self._release(change_set_id, lease.session_id))
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await release
                raise

    async def _release(self, change_set_id: str, session_id: str) -> None:
        with suppress(OMError):
            await self._request(
                "release_edit_lease",
                path_args={"resourceType": CHANGE_SET_RESOURCE, "resourceId": change_set_id},
                params={"sessionId": session_id},
            )

    async def add_operations(
        self, change_set_id: str, operations: Sequence[Mapping[str, Any]]
    ) -> OMChangeSet:
        """Append operations to a Draft.

        rc1 only offers ``PUT .../operations``, which *replaces* the list, so the current
        active operations (those before ``undoCursor``) are read under the lease and sent
        back with the new ones; undone operations are dropped, as a new edit after undo
        would. Raises ``OMError`` 404 when the change set does not exist.
        """
        async with self._lease(change_set_id) as lease:
            response = await self._request("get_change_set", path_args={"id": change_set_id})
            current = OMChangeSet.model_validate(_json(response))
            cursor = len(current.operations) if current.undo_cursor is None else current.undo_cursor
            kept = [op.model_dump(by_alias=True, exclude_none=True) for op in current.operations]
            ops = kept[:cursor] + [_prepare_operation(op) for op in operations]
            payload = {"lease": lease.token(), "operations": ops, "undoCursor": len(ops)}
            response = await self._request(
                "replace_operations", path_args={"id": change_set_id}, json=payload
            )
        return OMChangeSet.model_validate(_json(response))

    async def _command(self, name: str, change_set_id: str) -> OMChangeSet:
        async with self._lease(change_set_id) as lease:
            response = await self._request(
                name, path_args={"id": change_set_id}, json={"lease": lease.token()}
            )
        return OMChangeSet.model_validate(_json(response))

    async def submit_change_set(self, id: str) -> OMChangeSet:
        """Draft -> Submitted. rc1 checks no base versions here (ADR 0011)."""
        return await self._command("submit_change_set", id)

    async def discard_change_set(self, id: str) -> OMChangeSet:
        """Draft or Submitted -> Discarded."""
        return await self._command("discard_change_set", id)

    async def validate_axiom(self, axiom: Mapping[str, Any]) -> OMAxiomValidation:
        """Validate a ``CreateOntologyAxiom`` against the *applied* declarations of the
        native ontology it names (``ontology``); 404 when that ontology does not exist."""
        response = await self._request("validate_axiom", json=dict(axiom))
        return OMAxiomValidation.model_validate(_json(response))

    # --- Native ontologies (hybrid model; ADR 0011) ---------------------------------------

    async def create_ontology(self, *, name: str, iri: str, description: str) -> OMOntology:
        """Create the native ontology paired with a glossary (``name`` = the model slug).

        rc1 requires ``namespace`` to be an absolute IRI; the model base IRI is used for
        both. Raises ``OMConflict`` (``exists``) when the name is taken.
        """
        payload = {"name": name, "iri": iri, "namespace": iri, "description": description}
        response = await self._request("create_ontology", json=payload)
        return OMOntology.model_validate(_json(response))

    async def get_ontology_by_name(self, name: str) -> OMOntology | None:
        response = await self._get_or_none("get_ontology_by_name", path_args={"fqn": name})
        return None if response is None else OMOntology.model_validate(_json(response))

    async def get_ontology_state(self, ontology_id: str) -> OMOntologyState:
        response = await self._request("get_ontology_state", path_args={"id": ontology_id})
        return OMOntologyState.model_validate(_json(response))

    async def _members(self, name: str, ontology: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_MEMBER_PAGES):
            params = {"ontology": ontology, "limit": str(_MEMBER_PAGE), "fields": "*"}
            if after:
                params["after"] = after
            body = _json(await self._request(name, params=params))
            if not isinstance(body, dict):
                raise OMError("OpenMetadata returned an unexpected ontology member page")
            page = cast("dict[str, Any]", body)
            data = page.get("data")
            out += (
                [m for m in cast("list[object]", data) if isinstance(m, dict)]
                if isinstance(data, list)
                else []
            )
            paging = page.get("paging")
            cursor = (
                cast("dict[str, Any]", paging).get("after") if isinstance(paging, dict) else None
            )
            if not isinstance(cursor, str) or not cursor:
                return out
            if cursor in seen:
                raise OMError("OpenMetadata repeated a paging cursor for ontology members")
            seen.add(cursor)
            after = cursor
        raise OMError("OpenMetadata returned too many ontology member pages")

    async def export_native_ontology(self, name: str) -> OMNativeExport | None:
        """The native ontology called ``name`` as an RDF graph, or ``None`` without a pair.

        rc1 only exports sealed releases (sealing is a write), so the working members are
        read and turned into RDF (``om.native_rdf``) off the event loop. The
        ``workingRevision`` is read before and after; when it moved, the read is repeated
        (3 attempts; the last one is labelled with the ``before`` revision). Malformed
        members are skipped; any other failure while building raises ``OMError``.
        """
        ontology = await self.get_ontology_by_name(name)
        if ontology is None:
            return None
        attempt = 0
        while True:
            attempt += 1
            before = (await self.get_ontology_state(ontology.id)).working_revision
            concepts, properties, axioms = await asyncio.gather(
                self._members("list_ontology_concepts", name),
                self._members("list_ontology_properties", name),
                self._members("list_ontology_axioms", name),
            )
            after = (await self.get_ontology_state(ontology.id)).working_revision
            if before == after or attempt >= _EXPORT_ATTEMPTS:
                break
        try:
            graph = await asyncio.to_thread(build_graph, concepts, properties, axioms)
        except Exception as exc:
            raise OMError(
                f"cannot express native ontology {name!r} as RDF ({type(exc).__name__})"
            ) from None
        return OMNativeExport(working_revision=before, graph=graph)

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


def _detail(response: httpx.Response) -> str | None:
    """OM's error ``message`` (``{"code": n, "message": "..."}``), capped; never the request."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        message = cast("dict[str, object]", payload).get("message")
        if isinstance(message, str):
            return message[:_DETAIL_MAX]
    return None


def _client_error(response: httpx.Response) -> OMError:
    status = response.status_code
    detail = _detail(response)
    message = f"OpenMetadata returned HTTP {status}"
    stale = status == 400 and detail is not None and _STALE_BASE.search(detail) is not None
    if status in (409, 412) or stale:
        ids = tuple(dict.fromkeys(m.lower() for m in _UUID_IN_TEXT.findall(detail or "")))
        kind: ConflictKind
        if stale:
            kind = "stale_base"
        elif status == 412:
            kind = "stale_revision"
        elif "already exists" in (detail or "").lower():
            kind = "exists"
        else:
            kind = "lease"
        return OMConflict(f"{message} (conflict)", status, detail, ids, kind)
    return OMError(message, status, detail)


def _prepare_operation(op: Mapping[str, Any]) -> dict[str, Any]:
    """Fill the fields rc1 requires on every operation but that carry no meaning for callers:
    a fresh ``id`` and ``state: ACTIVE``. The caller's mapping is not modified."""
    out = dict(op)
    out.setdefault("id", str(uuid.uuid4()))
    out.setdefault("state", "ACTIVE")
    return out


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        raise OMError("OpenMetadata returned a non-JSON body", response.status_code) from None
