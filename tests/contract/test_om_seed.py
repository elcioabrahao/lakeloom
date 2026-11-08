"""Fixture lake seeded into the real OpenMetadata (FR-SRC-07, FR-OM-01)."""

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr
from stack import REPO

from lakeloom.om.client import OMClient

pytestmark = pytest.mark.contract

SERVICE = "lakeloom_fixture.retail.sales"
TABLES = ("customers", "products", "orders", "invoices")


def _run_seed(om_url: str, token: str, tmp_path: Path) -> dict[str, str]:
    token_file = tmp_path / "om_bot_token"
    token_file.write_text(token, encoding="utf-8")
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            str(REPO / "scripts/seed_om_fixture.py"),
            "--om-url",
            om_url,
            "--token-file",
            str(token_file),
            "--lake",
            str(REPO / "tests/fixtures/lake"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert token not in proc.stdout + proc.stderr
    return {line.split()[0]: line.split()[1] for line in proc.stdout.splitlines()}


async def test_fr_src_07_om_seed_created_fixture_tables(om_url: str, om_bot_token: str) -> None:
    client = OMClient(om_url, SecretStr(om_bot_token))
    try:
        for name in TABLES:
            table = await client.get_table_by_fqn(f"{SERVICE}.{name}")
            assert table is not None, name
            assert len(table.columns) >= 5, name
        customers = await client.get_table_by_fqn(f"{SERVICE}.customers")
        assert customers is not None
        email = next(c for c in customers.columns if c.name == "email")
        assert any(t.tag_fqn.startswith("PII.") for t in email.tags)
        assert await client.get_table_by_fqn(f"{SERVICE}.no_such_table") is None
    finally:
        await client.aclose()


def test_fr_src_07_om_seed_idempotent(om_url: str, om_bot_token: str, tmp_path: Path) -> None:
    first = _run_seed(om_url, om_bot_token, tmp_path)
    second = _run_seed(om_url, om_bot_token, tmp_path)

    assert set(first) == {f"{SERVICE}.{t}" for t in TABLES}
    assert first == second
