"""JSON logging (NFR-OPS-03)."""

import json
import logging

import pytest

from lakeloom.logging import configure_logging, request_id_var


def test_nfr_ops_03_json_log_line_has_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    token = request_id_var.set("req-123")
    try:
        logging.getLogger("lakeloom.test").info("hello", extra={"tool": "x"})
    finally:
        request_id_var.reset(token)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(line)
    assert data["message"] == "hello"
    assert data["level"] == "INFO"
    assert data["logger"] == "lakeloom.test"
    assert data["request_id"] == "req-123"
    assert data["tool"] == "x"
    assert "ts" in data


def test_configure_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")
    logging.getLogger("lakeloom.test").info("once")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "once" in ln]
    assert len(lines) == 1
