"""Create a semantic model: an OM glossary plus its paired native ontology (ADR 0011).

A Lakeloom model is the glossary (concepts as terms, relations, mappings) and a native ontology
of the same name (classes, attributes and axioms; hybrid model, Amendment A). Both are created
through OM's entity APIs, not change sets: a model is not one of the FR-OM-02 change-set-only
kinds. The base IRI follows the M1b rule ``{public_base_url}/ontology/{slug}/``; OM 2.1.0-rc1
requires the ontology ``namespace`` to be an absolute IRI, so it is the base IRI too.

Catalog-changing, so FR-AGT-03 applies: a call without ``confirm_token`` is a dry run that
writes nothing and returns a :class:`ModelPreview` with a token bound to the arguments and to
the previewed OM state (whether the glossary and the ontology exist, the glossary's id and
update time). Once the model exists that state is gone, so a replayed token fails as
``conflict``. ``pair_existing=True`` creates only the missing ontology of an existing glossary
(models created before the hybrid model, e.g. the seeded ``retail``).

If the ontology cannot be created after the glossary was, the new glossary is hard-deleted
again (never recursively) and the error is reported; if even that fails, the hint names the
glossary id and tells how to finish the pairing.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable
from typing import Literal

from pydantic import BaseModel

from lakeloom.config import Settings
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.models import ModelService
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMConflict, OMError, OMUnavailable
from lakeloom.om.models import OMGlossary, OMOntology
from lakeloom.untrusted import code_span, quote_untrusted

__all__ = [
    "CREATE_MODEL_TOOL",
    "SLUG_PATTERN",
    "CreatedModel",
    "ModelPreview",
    "create_model",
    "validate_slug",
]

CREATE_MODEL_TOOL = "create_model"
SLUG_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
MAX_DISPLAY_NAME = 256
MAX_DESCRIPTION = 4000
PAIR_HINT = "call create_model with pair_existing=true"
_SLUG = re.compile(SLUG_PATTERN)
_log = logging.getLogger(__name__)

Part = Literal["glossary", "ontology"]


class ModelPreview(BaseModel):
    """Dry run: what would be created, and the token that confirms it."""

    name: str
    base_iri: str
    creates: list[Part]
    summary: str
    confirm_token: str
    expires_at: int


class CreatedModel(BaseModel):
    name: str
    base_iri: str
    glossary_id: str
    ontology_id: str
    created: list[Part]
    summary: str


def validate_slug(name: str) -> None:
    """A model slug: lower-case letter first, then lower-case letters, digits, ``_`` or ``-``
    (at most 64 characters). It is the glossary name, the ontology name and part of IRIs."""
    if not _SLUG.match(name):
        raise LakeloomError(
            "validation_error",
            f"model name {code_span(name)} is not a valid slug",
            "use 1-64 characters: a lower-case letter, then lower-case letters, digits, "
            "'_' or '-' (for example retail_auto)",
        )


def _om_failure(exc: OMError, what: str) -> LakeloomError:
    if isinstance(exc, OMConflict):
        return LakeloomError(
            "conflict",
            f"OpenMetadata refused {what}: the name is already taken",
            "choose another model name (a soft-deleted entity can still hold the name)",
        )
    if isinstance(exc, OMUnavailable) or exc.status is None or exc.status >= 500:
        return LakeloomError(
            "upstream_unavailable",
            f"OpenMetadata is unavailable while {what}",
            "retry shortly; check that OpenMetadata is running",
        )
    detail = f": {code_span(exc.detail)}" if exc.detail else ""
    return LakeloomError(
        "validation_error",
        f"OpenMetadata rejected {what} (HTTP {exc.status}){detail}",
        "check the name, display name and description",
    )


async def _om[T](what: str, call: Awaitable[T]) -> T:
    try:
        return await call
    except OMError as exc:
        raise _om_failure(exc, what) from None


def _digest(name: str, base_iri: str, g: OMGlossary | None, o: OMOntology | None) -> str:
    state = {
        "name": name,
        "base_iri": base_iri,
        "glossary": None if g is None else [g.id, g.updated_at],
        "ontology": None if o is None else o.id,
    }
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan(
    name: str, g: OMGlossary | None, o: OMOntology | None, *, pair_existing: bool
) -> list[Part]:
    if pair_existing:
        if g is None:
            raise LakeloomError(
                "not_found",
                f"there is no glossary {code_span(name)} to pair",
                "call create_model without pair_existing to create the glossary and its ontology",
            )
        if o is not None:
            raise LakeloomError(
                "validation_error",
                f"model {code_span(name)} is already paired with a native ontology",
                "nothing to do; propose into it with propose_from_scan or propose_change",
            )
        return ["ontology"]
    if g is not None:
        if o is None:
            raise LakeloomError(
                "conflict", f"model {code_span(name)} exists without a native ontology", PAIR_HINT
            )
        raise LakeloomError(
            "conflict",
            f"model {code_span(name)} already exists",
            "choose another name; call list_models to see the existing models",
        )
    if o is not None:
        raise LakeloomError(
            "conflict",
            f"a native ontology {code_span(name)} exists without a glossary",
            "choose another model name, or ask an OpenMetadata admin to remove that ontology",
        )
    return ["glossary", "ontology"]


def _summary(
    name: str,
    base_iri: str,
    parts: list[Part],
    display_name: str | None,
    description: str | None,
    *,
    done: bool,
) -> str:
    verb = "Created" if done else "Will create"
    lines = [f"# {'Created' if done else 'Create'} model {code_span(name)}", ""]
    if "glossary" in parts:
        shown = f" (display name {code_span(display_name)})" if display_name else ""
        lines.append(f"- {verb} OpenMetadata glossary {code_span(name)}{shown}")
    else:
        lines.append(f"- Glossary {code_span(name)} exists already (kept as is)")
    lines.append(
        f"- {verb} the paired native ontology {code_span(name)} with IRI and namespace "
        f"{code_span(base_iri)}"
    )
    if description:
        lines += ["", "Description:", quote_untrusted(description, source="create_model input")]
    lines.append("")
    if done:
        lines.append(
            "Concepts, attributes, relations and mappings are added through proposals "
            "(propose_from_scan, propose_change); nothing is applied until a human approves."
        )
    else:
        lines.append(
            "Side effect on confirmation: these entities are created in OpenMetadata directly "
            "(a model is not a change set). Show this to the user; to proceed, call "
            "create_model again with the same arguments and confirm_token."
        )
    return "\n".join(lines) + "\n"


async def create_model(
    om: OMClient,
    models: ModelService,
    confirm: ConfirmTokens,
    settings: Settings,
    name: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
    pair_existing: bool = False,
    confirm_token: str | None = None,
) -> ModelPreview | CreatedModel:
    """Preview (no token) or create (with the preview's token) a model; see the module doc."""
    validate_slug(name)
    if display_name is not None and len(display_name) > MAX_DISPLAY_NAME:
        raise LakeloomError(
            "validation_error",
            f"display_name is longer than {MAX_DISPLAY_NAME} characters",
            "shorten the display name",
        )
    if description is not None and len(description) > MAX_DESCRIPTION:
        raise LakeloomError(
            "validation_error",
            f"description is longer than {MAX_DESCRIPTION} characters",
            "shorten the description",
        )
    if settings.public_base_url is None:
        raise LakeloomError(
            "validation_error",
            "model IRIs cannot be derived: no public URL is configured",
            "configure LAKELOOM_PUBLIC_URL",
        )
    base_iri = f"{settings.public_base_url}/ontology/{name}/"
    args = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "pair_existing": pair_existing,
    }
    g, o = await asyncio.gather(
        _om(f"looking up glossary {name!r}", om.get_glossary_by_name(name)),
        _om(f"looking up ontology {name!r}", om.get_ontology_by_name(name)),
    )
    digest = _digest(name, base_iri, g, o)
    if confirm_token is not None:
        confirm.verify(confirm_token, CREATE_MODEL_TOOL, args, digest)
    parts = _plan(name, g, o, pair_existing=pair_existing)
    if confirm_token is None:
        token, expires = confirm.issue(CREATE_MODEL_TOOL, args, digest)
        return ModelPreview(
            name=name,
            base_iri=base_iri,
            creates=parts,
            summary=_summary(name, base_iri, parts, display_name, description, done=False),
            confirm_token=token,
            expires_at=expires,
        )
    created = await _create(om, name, base_iri, g, display_name, description)
    models.invalidate(name)
    glossary, ontology = created
    return CreatedModel(
        name=name,
        base_iri=base_iri,
        glossary_id=glossary.id,
        ontology_id=ontology.id,
        created=parts,
        summary=_summary(name, base_iri, parts, display_name, description, done=True),
    )


async def _create(
    om: OMClient,
    name: str,
    base_iri: str,
    existing: OMGlossary | None,
    display_name: str | None,
    description: str | None,
) -> tuple[OMGlossary, OMOntology]:
    glossary = existing
    if glossary is None:
        glossary = await _om(
            f"creating glossary {name!r}",
            om.create_glossary(
                name=name,
                display_name=display_name,
                description=description or f"Lakeloom model {name}",
            ),
        )
    try:
        ontology = await _om(
            f"creating the native ontology {name!r}",
            om.create_ontology(
                name=name,
                iri=base_iri,
                description=f"Native ontology paired with the Lakeloom model {name} "
                "(classes, attributes and axioms)",
            ),
        )
    except BaseException as exc:
        if existing is not None:
            raise  # the glossary was there before: never delete it
        undone = await asyncio.shield(_undo(om, glossary))
        if not isinstance(exc, LakeloomError):
            raise
        if undone:
            raise LakeloomError(
                exc.code, f"{exc.message}; the new glossary was removed again", exc.hint
            ) from None
        raise LakeloomError(
            exc.code,
            f"{exc.message}; the new glossary could not be removed",
            f"glossary {glossary.id} exists without its ontology: retry with "
            "create_model pair_existing=true, or delete that glossary in OpenMetadata",
        ) from None
    return glossary, ontology


async def _undo(om: OMClient, glossary: OMGlossary) -> bool:
    try:
        await om.delete_glossary(glossary.id)
    except Exception as exc:
        _log.error(
            "could not delete glossary %s after a failed create_model (%s)",
            glossary.id,
            type(exc).__name__,
        )
        return False
    return True
