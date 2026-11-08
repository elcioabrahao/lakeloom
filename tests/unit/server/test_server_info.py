"""``build_server_info`` (FR-OM-06, NFR-SEC-11)."""

from kit import OM_BOT_TOKEN, DepsFactory, om_down
from pydantic import SecretStr

import lakeloom
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.server.server_info import ServerInfo, build_server_info

CALLER_TOKEN = "caller-access-token-abc123"  # noqa: S105


def principal(scopes: frozenset[str] = frozenset({"lakeloom:read"})) -> Principal:
    return Principal(
        subject="svc-123",
        type=PrincipalType.AGENT,
        client_id="lakeloom-agent",
        username=None,
        scopes=scopes,
        groups=frozenset(),
        token=SecretStr(CALLER_TOKEN),
    )


async def test_fr_om_06_server_info_reports_om_version_and_rdf(make_deps: DepsFactory) -> None:
    deps = make_deps(triplestore_enabled=True, oidc_token_exchange=False)

    info = await build_server_info(deps, principal())

    assert isinstance(info, ServerInfo)
    assert info.name == "lakeloom"
    assert info.version == lakeloom.__version__
    assert info.environment == "development"
    assert info.openmetadata.reachable is True
    assert info.openmetadata.version == "2.1.0"
    assert info.openmetadata.rdf_enabled is True
    assert info.features == {"triplestore": True, "stdio": True, "token_exchange": False}
    assert info.sources == []
    assert info.models == []
    assert info.caller is not None
    assert info.caller.subject == "svc-123"
    assert info.caller.type == "agent"
    assert info.caller.scopes == ["lakeloom:read"]


async def test_fr_om_06_server_info_when_om_down(make_deps: DepsFactory) -> None:
    deps = make_deps(om_down)

    info = await build_server_info(deps, None)

    assert info.openmetadata.reachable is False
    assert info.openmetadata.error
    assert info.caller is None


async def test_nfr_sec_11_server_info_never_contains_token(make_deps: DepsFactory) -> None:
    deps = make_deps()

    info = await build_server_info(deps, principal())
    dumped = info.model_dump_json()

    assert CALLER_TOKEN not in dumped
    assert OM_BOT_TOKEN not in dumped
    assert info.caller is not None
    assert "token" not in info.caller.model_dump()
