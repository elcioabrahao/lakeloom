"""What this Lakeloom instance is and can do, for ``get_server_info`` (FR-OM-06, §7).

``build_server_info`` has no side effects beyond probing OpenMetadata, and never raises
when OpenMetadata is down: ``openmetadata.reachable`` is then ``False`` with an error.
The caller section names the principal and its scopes but never carries its token
(NFR-SEC-11).
"""

from typing import Literal

from pydantic import BaseModel, Field

import lakeloom
from lakeloom.om.models import OMCapabilities
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.server.http_app import AppDeps

__all__ = ["CallerInfo", "ServerInfo", "build_server_info", "summarize"]


class CallerInfo(BaseModel):
    """The authenticated caller, without credentials."""

    subject: str
    type: PrincipalType
    scopes: list[str]


class ServerInfo(BaseModel):
    """Versions, enabled features, OpenMetadata capabilities, sources and models."""

    name: Literal["lakeloom"] = "lakeloom"
    version: str = Field(description="Lakeloom version.")
    environment: str = Field(description="Deployment environment (development or production).")
    openmetadata: OMCapabilities = Field(description="OpenMetadata version and RDF status.")
    features: dict[str, bool] = Field(
        description="Optional features: triplestore, stdio, token_exchange."
    )
    sources: list[str] = Field(description="Configured lake sources (none in M0).")
    models: list[str] = Field(description="Registered semantic models (none in M0).")
    caller: CallerInfo | None = Field(description="Who is calling, with granted scopes.")


async def build_server_info(deps: AppDeps, principal: Principal | None) -> ServerInfo:
    settings = deps.settings
    caller = (
        None
        if principal is None
        else CallerInfo(
            subject=principal.subject,
            type=principal.type,
            scopes=sorted(principal.scopes),
        )
    )
    return ServerInfo(
        version=lakeloom.__version__,
        environment=settings.env,
        openmetadata=await deps.om.get_capabilities(),
        features={
            "triplestore": settings.triplestore_enabled,
            "stdio": bool(settings.stdio_enabled),
            "token_exchange": settings.oidc_token_exchange,
        },
        sources=[],
        models=[],
        caller=caller,
    )


def summarize(info: ServerInfo) -> str:
    """Short Markdown summary that accompanies the structured content (FR-AGT-02)."""
    om = info.openmetadata
    if om.reachable:
        rdf = {True: "enabled", False: "disabled", None: "unknown"}[om.rdf_enabled]
        om_line = f"OpenMetadata {om.version or 'unknown'} reachable, RDF {rdf}"
        if om.error:
            om_line += f" (warning: {om.error})"
    else:
        om_line = f"OpenMetadata **unreachable**: {om.error or 'unknown error'}"
    enabled = ", ".join(k for k, v in info.features.items() if v) or "none"
    lines = [
        f"**Lakeloom {info.version}** ({info.environment})",
        f"- {om_line}",
        f"- Features enabled: {enabled}",
        f"- Sources: {len(info.sources)}, models: {len(info.models)}",
    ]
    if info.caller is not None:
        scopes = ", ".join(info.caller.scopes) or "none"
        lines.append(f"- Caller: {info.caller.type} `{info.caller.subject}` (scopes: {scopes})")
    return "\n".join(lines)
