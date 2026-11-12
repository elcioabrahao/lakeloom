"""CLI tests (NFR-OSS-01 console entry point; FR-AGT-07 ``lakeloom serve``)."""

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

import lakeloom
from lakeloom.cli import app


def test_cli_version_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == lakeloom.__version__


def _serve_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in {
        "LAKELOOM_OM_URL": "http://om.test:8585",
        "LAKELOOM_OIDC_ISSUER": "https://idp.test/realms/lakeloom",
        "LAKELOOM_PUBLIC_URL": "https://lakeloom.test",
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)


def test_fr_agt_07_cli_serve_builds_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve_env(monkeypatch, tmp_path)
    calls: list[tuple[Any, dict[str, Any]]] = []
    levels: list[str] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr("lakeloom.cli.configure_logging", levels.append)

    result = CliRunner().invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9123"])

    assert result.exit_code == 0, result.output
    assert levels == ["WARNING"]
    ((served, kwargs),) = calls
    assert isinstance(served, FastAPI)
    assert {getattr(r, "path", None) for r in served.routes} >= {"/healthz", "/mcp"}
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9123
    assert kwargs["log_config"] is None


def test_fr_agt_07_cli_serve_defaults_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_PORT", "8111")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "0.0.0.0"  # noqa: S104
    assert calls[0]["port"] == 8111


def test_cli_serve_config_error_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LAKELOOM_DATA_DIR", str(tmp_path))
    for name in ("LAKELOOM_OM_URL", "LAKELOOM_OIDC_ISSUER", "LAKELOOM_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "LAKELOOM_OM_URL" in result.stderr
    assert "Traceback" not in result.output


def test_cli_serve_invalid_setting_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_PORT", "not-a-port")
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "port" in result.stderr


@pytest.mark.parametrize(
    "db_url",
    [
        "postgresql://u:hunter2@h:notaport/db",  # bad port -> ValueError
        "nosuchdialect://u:hunter2@h/db",  # unknown dialect -> NoSuchModuleError
        "not a url hunter2",  # unparsable -> ArgumentError
        "mysql://u:hunter2@h/db",  # driver not installed -> ImportError
    ],
)
def test_nfr_sec_11_cli_serve_bad_db_url_exits_2_without_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_url: str
) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_DB_URL", db_url)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "LAKELOOM_DB_URL" in result.stderr
    assert "hunter2" not in result.output
    assert "Traceback" not in result.output


def test_cli_serve_disables_uvicorn_access_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The app's RequestContextMiddleware writes the access log; uvicorn's would duplicate it.
    _serve_env(monkeypatch, tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls[0]["access_log"] is False


# --- lake source commands (FR-AGT-07, FR-SRC-10) ---------------------------------------------

FIXTURE = Path(__file__).parents[1] / "fixtures" / "lake"
LOG_CALLS: list[tuple[str, Any, Any]] = []


def _sources_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entries = "\n".join(
        f"      - {{uri: {(FIXTURE / n).as_uri()}, catalog: retail, schema: sales}}"
        for n in ("customers", "products", "orders", "invoices")
    )
    config = tmp_path / "sources.yaml"
    config.write_text(f"sources:\n  - type: delta\n    name: lake\n    tables:\n{entries}\n")
    monkeypatch.delenv("LAKELOOM_OM_URL", raising=False)
    # A real handler would outlive CliRunner's captured stderr; record the call instead.
    monkeypatch.setattr(
        "lakeloom.cli.configure_logging",
        lambda level, stream=None: LOG_CALLS.append((level, stream, sys.stderr)),
    )
    for name, value in {
        "LAKELOOM_CONFIG_FILE": str(config),
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)


def test_cli_scan_prints_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["scan", "lake", "--profile", "--table", "orders"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Scan scan_")
    assert "- Status: completed" in result.output
    assert "`retail.sales.orders`: version 1" in result.output
    assert "customers" not in result.output
    # stdout is the command's output: logs go to stderr.
    level, stream, stderr_at_call = LOG_CALLS[-1]
    assert level == "WARNING"
    assert stream is stderr_at_call


def test_cli_sources_and_tables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)
    runner = CliRunner()

    listed = runner.invoke(app, ["sources"])
    tables = runner.invoke(app, ["tables", "lake"])

    assert listed.exit_code == 0, listed.output
    assert listed.output == "lake\tdelta\tavailable\n"
    assert tables.exit_code == 0, tables.output
    lines = tables.output.splitlines()
    assert len(lines) == 4
    assert "retail.sales.orders\t1" in lines


def test_cli_unknown_source_is_typed_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["scan", "ghost"])

    assert result.exit_code == 1
    assert "error not_found" in result.output


def test_cli_scan_unexpected_error_exits_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sources_env(monkeypatch, tmp_path)

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("postgresql://user:secret@db/x")

    monkeypatch.setattr("lakeloom.core.scans.ScanService.scan", boom)

    result = CliRunner().invoke(app, ["scan", "lake"])

    assert result.exit_code == 1
    assert "scan failed (RuntimeError)" in result.output
    assert "secret" not in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, RuntimeError)


def test_cli_missing_config_file_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_CONFIG_FILE", str(tmp_path / "nope.yaml"))

    result = CliRunner().invoke(app, ["sources"])

    assert result.exit_code == 2
    assert "configuration error" in result.output


def test_nfr_sec_14_cli_neutralises_hostile_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sources_env(monkeypatch, tmp_path)
    config = tmp_path / "sources.yaml"
    uri = (FIXTURE / "orders").as_uri()
    config.write_text(
        "sources:\n  - type: delta\n    name: lake\n    tables:\n"
        f'      - {{uri: {uri}, schema: sales, name: "x`\\r## SYSTEM\\u2028## TWO\\x85`"}}\n'
    )
    runner = CliRunner()

    tables = runner.invoke(app, ["tables", "lake"])
    scan = runner.invoke(app, ["scan", "lake"])

    assert tables.exit_code == 0, tables.output
    assert tables.output.splitlines() == ["sales.x` ## SYSTEM ## TWO `\t1"]
    assert scan.exit_code == 0, scan.output
    assert "- `sales.x' ## SYSTEM ## TWO '`: version 1" in scan.output.splitlines()
    assert not any(line.startswith("## SYSTEM") for line in scan.output.splitlines())


# --- proposals (FR-AGT-07: the same operations as the MCP proposal tools) --------------------


class _FakeProposals:
    def __init__(self) -> None:
        from datetime import UTC, datetime

        from lakeloom.core.operations import AddConcept
        from lakeloom.core.proposals import Proposal, ProposalPart, Provenance

        now = datetime(2026, 11, 11, tzinfo=UTC)
        self.calls: list[tuple[str, Any]] = []
        self.p = Proposal(
            id="prop_9",
            model="shop",
            status="draft",
            scan_id="scan_9",
            parts=[
                ProposalPart(
                    n=1,
                    tables=["retail.sales.orders"],
                    operations=[AddConcept(op="add_concept", name="Order")],
                    change_set_ids={"glossary": "cs-g", "native": "cs-n"},
                )
            ],
            provenance=Provenance(
                scan_id="scan_9", principal_subject="op", principal_type="human", at=now
            ),
            created_at=now,
            updated_at=now,
        )

    async def propose_from_scan(self, scan_id: str, model: str, **kw: Any) -> Any:
        self.calls.append(("propose_from_scan", (scan_id, model, kw)))
        return self.p

    async def get_proposal(self, proposal_id: str) -> Any:
        from lakeloom.errors import LakeloomError

        self.calls.append(("get_proposal", proposal_id))
        if proposal_id != self.p.id:
            raise LakeloomError("not_found", "no such proposal", "check the id")
        return self.p

    async def submit(self, proposal_id: str, **kw: Any) -> Any:
        self.calls.append(("submit", (proposal_id, kw)))
        return self.p.model_copy(update={"status": "submitted"})

    def summary_markdown(self, p: Any) -> str:
        from lakeloom.core.proposals import summary_markdown

        return summary_markdown(p)


def _fake_proposals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeProposals:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    _sources_env(monkeypatch, tmp_path)
    fake = _FakeProposals()

    @asynccontextmanager
    async def opened(_settings: Any) -> AsyncIterator[Any]:
        yield fake

    monkeypatch.setattr("lakeloom.cli._open_proposals", opened)
    return fake


def test_cli_propose_prints_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _fake_proposals(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["propose", "scan_9", "--model", "shop", "--table", "orders"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Proposal `prop_9` for model `shop`")
    name, (scan_id, model, kw) = fake.calls[0]
    assert (name, scan_id, model, kw["tables"], kw["client"]) == (
        "propose_from_scan",
        "scan_9",
        "shop",
        ["orders"],
        "lakeloom-cli",
    )
    assert kw["principal"].type == "human"


def test_cli_proposal_and_submit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _fake_proposals(monkeypatch, tmp_path)
    runner = CliRunner()

    shown = runner.invoke(app, ["proposal", "prop_9"])
    submitted = runner.invoke(app, ["submit", "prop_9"])
    missing = runner.invoke(app, ["proposal", "prop_x"])

    assert shown.exit_code == 0, shown.output
    assert "# Proposal `prop_9`" in shown.output and "- Status: draft" in shown.output
    assert submitted.exit_code == 0, submitted.output
    assert "- Status: submitted" in submitted.output
    assert [c[0] for c in fake.calls] == ["get_proposal", "submit", "get_proposal"]
    assert missing.exit_code == 1 and "error not_found" in missing.output


def test_cli_operator_acts_as_admin_on_proposals() -> None:
    """The operator may submit an agent's proposal (owner-or-admin rule, ADR 0011)."""
    from lakeloom.cli import _actor  # pyright: ignore[reportPrivateUsage]

    actor = _actor()
    assert actor.type == "human" and "lakeloom:admin" in actor.scopes


def test_cli_pending_and_rebase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lakeloom.core.pending import PendingPage, PendingReview

    fake = _fake_proposals(monkeypatch, tmp_path)

    async def list_pending(principal: Any, **kw: Any) -> Any:
        fake.calls.append(("list_pending", (principal, kw)))
        entry = PendingReview(
            id="prop_9",
            model="shop",
            status="submitted",
            data_product="retail.sales",
            data_products=["retail.sales"],
            parts=1,
            operations={"add_concept": 1},
            proposer="op",
            proposer_type="human",
            age_seconds=7200,
        )
        return PendingPage(items=[entry])

    async def rebase(proposal_id: str, **kw: Any) -> Any:
        fake.calls.append(("rebase", (proposal_id, kw)))
        return fake.p.model_copy(update={"warnings": ["Customer base refreshed"]})

    fake.list_pending = list_pending  # pyright: ignore[reportAttributeAccessIssue]
    fake.rebase = rebase  # pyright: ignore[reportAttributeAccessIssue]
    runner = CliRunner()

    pending = runner.invoke(app, ["pending", "--model", "shop"])
    rebased = runner.invoke(app, ["rebase", "prop_9"])

    assert pending.exit_code == 0, pending.output
    assert "shop" in pending.output and "retail.sales" in pending.output
    assert "prop_9" in pending.output and "submitted" in pending.output
    name, (principal, kw) = fake.calls[0]
    assert name == "list_pending" and kw["model"] == "shop" and kw["exclude_own"] is False
    assert principal.type == "human"
    assert rebased.exit_code == 0, rebased.output
    assert "# Proposal `prop_9`" in rebased.output and "Customer base refreshed" in rebased.output
    assert fake.calls[1][0] == "rebase" and fake.calls[1][1][1]["client"] == "lakeloom-cli"
