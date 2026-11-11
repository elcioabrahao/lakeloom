"""Stateless HMAC confirm tokens (FR-AGT-03)."""

import base64
import json
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from lakeloom.config import ConfigError, Settings
from lakeloom.core.confirm import ConfirmTokens, confirm_tokens_from_settings
from lakeloom.errors import LakeloomError

SECRET = b"s" * 32
ARGS = {"model": "retail", "name": "Customer", "confirm_token": None}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _tokens(clock: Clock, ttl: int = 600) -> ConfirmTokens:
    return ConfirmTokens(SECRET, ttl, clock)


def _fail(tokens: ConfirmTokens, token: str, **kw: object) -> LakeloomError:
    call: dict[str, object] = {"tool": "t", "args": ARGS, "state_digest": "d1"} | kw
    with pytest.raises(LakeloomError) as exc:
        tokens.verify(token, call["tool"], call["args"], call["state_digest"])  # type: ignore[arg-type]
    return exc.value


def test_fr_agt_03_round_trip_and_expiry_returned() -> None:
    clock = Clock()
    tokens = _tokens(clock)
    token, expires = tokens.issue("t", ARGS, "d1")
    assert expires == 1_000_600
    # confirm_token itself is excluded from the bound args
    tokens.verify(token, "t", {"model": "retail", "name": "Customer", "confirm_token": token}, "d1")
    tokens.verify(token, "t", {"model": "retail", "name": "Customer"}, "d1")


def test_tampered_token_is_invalid() -> None:
    tokens = _tokens(Clock())
    token, _ = tokens.issue("t", ARGS, "d1")
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    err = _fail(tokens, flipped)
    assert err.code == "validation_error" and err.message == "confirm token invalid"
    payload, mac = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=="))
    body["t"] = "other"
    forged = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    assert _fail(tokens, f"{forged}.{mac}").message == "confirm token invalid"


@pytest.mark.parametrize("junk", ["", "abc", "a.b", "!!!.???", "a.b.c", "é.é"])
def test_malformed_tokens_are_invalid(junk: str) -> None:
    err = _fail(_tokens(Clock()), junk)
    assert err.code == "validation_error" and err.message == "confirm token invalid"
    assert err.hint


def test_other_secret_is_invalid() -> None:
    token, _ = ConfirmTokens(b"x" * 32, 600, Clock()).issue("t", ARGS, "d1")
    assert _fail(_tokens(Clock()), token).message == "confirm token invalid"


def test_other_tool_or_args_does_not_match() -> None:
    tokens = _tokens(Clock())
    token, _ = tokens.issue("t", ARGS, "d1")
    for err in (
        _fail(tokens, token, tool="other"),
        _fail(tokens, token, args={"model": "retail", "name": "Order"}),
    ):
        assert err.code == "validation_error"
        assert "does not match this call" in err.message


def test_expired_token() -> None:
    clock = Clock()
    tokens = _tokens(clock)
    token, _ = tokens.issue("t", ARGS, "d1")
    clock.now += 601
    err = _fail(tokens, token)
    assert err.code == "validation_error" and "expired" in err.message


def test_state_change_is_conflict_and_replay_fails() -> None:
    tokens = _tokens(Clock())
    token, _ = tokens.issue("t", ARGS, "d1")
    err = _fail(tokens, token, state_digest="d2")  # state moved on after the token was used
    assert err.code == "conflict" and "previewed state changed" in err.message
    assert err.hint


def test_nonce_makes_tokens_unique() -> None:
    tokens = _tokens(Clock())
    assert tokens.issue("t", ARGS, "d1")[0] != tokens.issue("t", ARGS, "d1")[0]


def test_short_secret_rejected() -> None:
    with pytest.raises(ConfigError):
        ConfirmTokens(b"short", 600, Clock())


def test_settings_secret_min_length_and_ttl_bounds() -> None:
    with pytest.raises(ConfigError):
        confirm_tokens_from_settings(Settings(confirm_secret=SecretStr("short")))
    with pytest.raises(ValueError):
        Settings(confirm_ttl_seconds=5)
    assert Settings().confirm_ttl_seconds == 600


def test_production_without_secret_is_config_error() -> None:
    with pytest.raises(ConfigError, match="LAKELOOM_CONFIRM_SECRET"):
        confirm_tokens_from_settings(Settings(env="production"))


def test_development_without_secret_random_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        a = confirm_tokens_from_settings(Settings())
        b = confirm_tokens_from_settings(Settings())
    warnings = [r for r in caplog.records if "will not survive restarts" in r.getMessage()]
    assert len(warnings) == 2
    token, _ = a.issue("t", ARGS, "d")
    with pytest.raises(LakeloomError):
        b.verify(token, "t", ARGS, "d")


def test_secret_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "secret"
    f.write_text("k" * 40 + "\n")
    monkeypatch.setenv("LAKELOOM_CONFIRM_SECRET_FILE", str(f))
    settings = Settings(env="production")
    assert settings.confirm_secret is not None
    assert settings.confirm_secret.get_secret_value() == "k" * 40
    assert "k" * 40 not in repr(settings)
    token, _ = confirm_tokens_from_settings(settings).issue("t", ARGS, "d")
    confirm_tokens_from_settings(settings).verify(token, "t", ARGS, "d")


def test_overlong_token_is_invalid_before_parsing() -> None:
    err = _fail(_tokens(Clock()), "a" * 2049)
    assert err.message == "confirm token invalid"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object(), {1, 2}])
def test_unbindable_args_are_validation_error(bad: object) -> None:
    with pytest.raises(LakeloomError) as exc:
        _tokens(Clock()).issue("t", {"x": bad}, "d")
    assert exc.value.code == "validation_error"
