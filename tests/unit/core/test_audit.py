"""Audit records: JSON line + state store (NFR-SEC-12)."""

import asyncio
import io
import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.core.audit import AuditRecord, current_audit, record
from lakeloom.errors import LakeloomError
from lakeloom.logging import configure_logging
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.server.tools_common import guarded, local_principal
from lakeloom.state.audit import AuditRepository
from lakeloom.state.db import create_engine, upgrade

FIELDS = {
    "at", "principal_subject", "principal_type", "client", "tool", "model",
    "proposal_id", "change_set_id", "result", "error_code",
}  # fmt: skip


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


def _rec(at: datetime | None = None, tool: str = "create_proposal") -> AuditRecord:
    return AuditRecord(
        at=at or datetime(2026, 1, 1, tzinfo=UTC),
        principal_subject="agent-1",
        principal_type="agent",
        client="pytest",
        tool=tool,
        model="retail",
        proposal_id="prop_1",
        change_set_id=None,
        result="ok",
        error_code=None,
    )


def _principal(scopes: str = "lakeloom:propose") -> Principal:
    return Principal(
        subject="agent-1",
        type=PrincipalType.AGENT,
        client_id="c",
        username=None,
        scopes=frozenset(scopes.split()),
        groups=frozenset(),
        token=SecretStr("tok-secret"),
    )


def _ctx() -> Context:
    return cast(Context, SimpleNamespace(request_context=SimpleNamespace(request=None)))


def _ok() -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text="ok")])


async def test_nfr_sec_12_record_writes_json_line_and_row(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    repo = AuditRepository(engine)
    await record(repo, _rec())
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x]
    line = next(x for x in lines if x["logger"] == "lakeloom.audit")
    assert FIELDS <= set(line)
    assert set(line) - FIELDS <= {"ts", "level", "logger", "message", "request_id"}
    assert line["tool"] == "create_proposal" and line["model"] == "retail"
    page = await repo.list(None, None, 10)
    assert page.items == [_rec()]


async def test_audit_repo_list_since_and_cursor(engine: AsyncEngine) -> None:
    repo = AuditRepository(engine)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        await repo.add(_rec(base + timedelta(minutes=i), tool=f"t{i}"))
    p1 = await repo.list(base + timedelta(minutes=1), None, 2)
    assert [r.tool for r in p1.items] == ["t1", "t2"]
    assert p1.next_cursor is not None
    p2 = await repo.list(base + timedelta(minutes=1), p1.next_cursor, 2)
    assert [r.tool for r in p2.items] == ["t3", "t4"]
    assert p2.next_cursor is None


async def test_record_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(AuditRepository):
        def __init__(self) -> None: ...

        async def add(self, record: AuditRecord) -> None:
            raise RuntimeError("secret-dsn://x")

    with caplog.at_level(logging.ERROR):
        await record(Broken(), _rec())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and "secret-dsn" not in text


async def test_guarded_audits_ok_with_context_never_args(engine: AsyncEngine) -> None:
    repo = AuditRepository(engine)
    local_principal.set(_principal())

    async def body(_p: Principal) -> CallToolResult:
        a = current_audit()
        a.model, a.proposal_id, a.change_set_id = "retail", "prop_9", "cs_1"
        return _ok()

    await guarded("create_proposal", _ctx(), "lakeloom:propose", body, audited=True, audit=repo)
    [r] = (await repo.list(None, None, 10)).items
    assert (r.tool, r.result, r.error_code) == ("create_proposal", "ok", None)
    assert (r.model, r.proposal_id, r.change_set_id) == ("retail", "prop_9", "cs_1")
    assert (r.principal_subject, r.principal_type) == ("agent-1", "agent")
    dumped = json.dumps(r.model_dump(mode="json"))
    assert "tok-secret" not in dumped


async def test_guarded_audits_error_and_forbidden(engine: AsyncEngine) -> None:
    repo = AuditRepository(engine)
    local_principal.set(_principal())

    async def bad(_p: Principal) -> CallToolResult:
        raise LakeloomError("conflict", "x", "y")

    await guarded("t", _ctx(), "lakeloom:propose", bad, audited=True, audit=repo)
    await guarded("t", _ctx(), "lakeloom:admin", bad, audited=True, audit=repo)
    items = (await repo.list(None, None, 10)).items
    assert [(r.result, r.error_code) for r in items] == [
        ("error", "conflict"),
        ("forbidden", "forbidden"),
    ]


async def test_guarded_not_audited_by_default_and_audit_failure_never_fails(
    engine: AsyncEngine,
) -> None:
    repo = AuditRepository(engine)
    local_principal.set(_principal())

    async def body(_p: Principal) -> CallToolResult:
        return _ok()

    await guarded("t", _ctx(), "lakeloom:propose", body, audit=repo)
    assert (await repo.list(None, None, 10)).items == []

    class Broken(AuditRepository):
        def __init__(self) -> None: ...

        async def add(self, record: AuditRecord) -> None:
            raise RuntimeError("down")

    result = await guarded("t", _ctx(), "lakeloom:propose", body, audited=True, audit=Broken())
    assert not result.is_error


def test_current_audit_without_guard_is_detached() -> None:
    assert current_audit().model is None


def _ctx_named(name: str) -> Context:
    params = SimpleNamespace(client_info=SimpleNamespace(name=name))
    return cast(
        Context,
        SimpleNamespace(
            request_context=SimpleNamespace(request=None),
            session=SimpleNamespace(client_params=params),
        ),
    )


def test_oversized_fields_are_truncated_to_column_width() -> None:
    rec = _rec().model_copy(update={})
    big = AuditRecord.model_validate(
        rec.model_dump() | {"client": "c" * 300, "model": "m" * 300, "proposal_id": "p" * 100}
    )
    assert len(big.client or "") == 255 and len(big.model or "") == 255
    assert len(big.proposal_id or "") == 64


async def test_guarded_audit_survives_oversized_client_name(engine: AsyncEngine) -> None:
    repo = AuditRepository(engine)
    local_principal.set(_principal())

    async def body(_p: Principal) -> CallToolResult:
        return _ok()

    await guarded("t", _ctx_named("c" * 300), "lakeloom:propose", body, audited=True, audit=repo)
    [r] = (await repo.list(None, None, 10)).items
    assert r.client == "c" * 255


async def test_audited_without_repository_fails_closed() -> None:
    local_principal.set(_principal())

    async def body(_p: Principal) -> CallToolResult:
        raise AssertionError("must not run")

    with pytest.raises(RuntimeError):
        await guarded("t", _ctx(), "lakeloom:propose", body, audited=True)


def test_audit_line_emitted_at_warning_root_level_once() -> None:
    buf = io.StringIO()
    configure_logging("WARNING", stream=buf)
    try:
        logging.getLogger("lakeloom.audit").info("audit", extra=_rec().model_dump(mode="json"))
        logging.getLogger("lakeloom.other").info("hidden")
    finally:
        configure_logging("INFO")
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert [x["logger"] for x in lines] == ["lakeloom.audit"]


async def test_cancelled_call_is_still_audited(engine: AsyncEngine) -> None:
    repo = AuditRepository(engine)
    local_principal.set(_principal())
    started = asyncio.Event()

    async def body(_p: Principal) -> CallToolResult:
        started.set()
        await asyncio.sleep(10)
        return _ok()

    task = asyncio.create_task(
        guarded("t", _ctx(), "lakeloom:propose", body, audited=True, audit=repo)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [r] = (await repo.list(None, None, 10)).items
    assert (r.result, r.error_code) == ("error", "cancelled")


async def test_audit_record_build_failure_never_fails_call(engine: AsyncEngine) -> None:
    local_principal.set(_principal())
    bad = cast(Context, SimpleNamespace(request_context=SimpleNamespace(request=None), session=1))

    async def body(_p: Principal) -> CallToolResult:
        return _ok()

    result = await guarded(
        "t", bad, "lakeloom:propose", body, audited=True, audit=AuditRepository(engine)
    )
    assert not result.is_error


async def test_body_can_override_the_outcome_of_a_partial_success(engine: AsyncEngine) -> None:
    """A partial result (e.g. part 2 of 3 not created) is returned but audited as an error."""
    repo = AuditRepository(engine)
    local_principal.set(_principal())

    async def body(_p: Principal) -> CallToolResult:
        current_audit().error_code = "upstream_unavailable"
        return _ok()

    result = await guarded(
        "propose_from_scan", _ctx(), "lakeloom:propose", body, audited=True, audit=repo
    )
    assert result.is_error is not True
    [r] = (await repo.list(None, None, 10)).items
    assert (r.result, r.error_code) == ("error", "upstream_unavailable")


async def test_nul_is_stripped_from_audit_strings(engine: AsyncEngine) -> None:
    rec = _rec().model_copy(update={"client": "evil\x00client", "model": "re\x00tail"})
    rec = AuditRecord.model_validate(rec.model_dump())
    assert (rec.client, rec.model) == ("evilclient", "retail")
