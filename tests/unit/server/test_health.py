"""Readiness checks (NFR-OPS-01)."""

import asyncio
import os
from pathlib import Path

import pytest

from lakeloom.server.health import CheckResult, git_workspace_check, run_checks


def _ok(name: str):
    async def check() -> CheckResult:
        return CheckResult(name, True, None)

    return check


async def test_run_checks_collects_results() -> None:
    results = await run_checks([_ok("a"), _ok("b")])
    assert [r.name for r in results] == ["a", "b"]
    assert all(r.ok for r in results)


async def test_check_timeout_marks_failed() -> None:
    async def slow() -> CheckResult:
        await asyncio.sleep(5)
        return CheckResult("slow", True, None)

    results = await run_checks([slow], timeout=0.05)
    assert results[0].ok is False
    assert results[0].name == "slow"
    assert "timed out" in (results[0].detail or "")


async def test_check_exception_marks_failed() -> None:
    async def boom() -> CheckResult:
        raise RuntimeError("kaput")

    results = await run_checks([boom])
    assert results[0].ok is False
    assert "kaput" in (results[0].detail or "")


async def test_git_workspace_check_ok(tmp_path: Path) -> None:
    result = await git_workspace_check(tmp_path / "git")()
    assert result.ok is True, result.detail
    assert result.name == "git_workspace"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_git_workspace_check_unwritable_dir(tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        result = await git_workspace_check(ro)()
    finally:
        ro.chmod(0o700)
    assert result.ok is False
    assert "not writable" in (result.detail or "")
