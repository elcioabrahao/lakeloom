"""Readiness checks behind ``/readyz`` (NFR-OPS-01).

Each check returns a :class:`CheckResult` and never raises; :func:`run_checks` also
bounds every check with its own timeout. Details are short fixed-shape messages and
never contain credentials.
"""

import asyncio
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.om.client import OMClient
from lakeloom.state.db import ping


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str | None = None


Check = Callable[[], Awaitable[CheckResult]]


def _named(name: str, check: Check) -> Check:
    # run_checks reports a timed-out/crashed check under this name.
    check.__name__ = name  # pyright: ignore[reportFunctionMemberAccess]
    return check


def om_check(client: OMClient) -> Check:
    async def check() -> CheckResult:
        caps = await client.get_capabilities()
        if caps.reachable:
            return CheckResult("openmetadata", True, caps.version)
        return CheckResult("openmetadata", False, caps.error or "unreachable")

    return _named("openmetadata", check)


def state_store_check(
    engine: AsyncEngine, upgrade_pending: Callable[[], bool] | None = None
) -> Check:
    async def check() -> CheckResult:
        if upgrade_pending is not None and upgrade_pending():
            return CheckResult("state_store", False, "schema upgrade pending")
        try:
            await ping(engine)
        except Exception as exc:
            return CheckResult(
                "state_store", False, f"{type(exc).__name__}: state store unreachable"
            )
        return CheckResult("state_store", True, None)

    return _named("state_store", check)


def _probe_writable(path: Path) -> str | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".lakeloom-ready-"):
            pass
    except OSError as exc:
        return exc.strerror
    return None


def git_workspace_check(path: Path) -> Check:
    async def check() -> CheckResult:
        name = "git_workspace"
        error = await asyncio.to_thread(_probe_writable, path)
        if error is not None:
            return CheckResult(name, False, f"{path} not writable: {error}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            code = await proc.wait()
        except OSError:
            return CheckResult(name, False, "git executable not found")
        if code != 0:
            return CheckResult(name, False, f"git --version exited with {code}")
        return CheckResult(name, True, None)

    return _named("git_workspace", check)


async def _run_one(check: Check, timeout: float) -> CheckResult:  # noqa: ASYNC109 - per-check bound
    name = getattr(check, "__name__", "check")
    try:
        async with asyncio.timeout(timeout):
            return await check()
    except TimeoutError:
        return CheckResult(name, False, f"timed out after {timeout:g}s")
    except Exception as exc:
        return CheckResult(name, False, str(exc) or type(exc).__name__)


async def run_checks(checks: Sequence[Check], timeout: float = 3.0) -> list[CheckResult]:  # noqa: ASYNC109
    """Run all checks concurrently, each under its own timeout."""
    return list(await asyncio.gather(*(_run_one(c, timeout) for c in checks)))
