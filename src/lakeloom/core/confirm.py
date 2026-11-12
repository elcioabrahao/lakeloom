"""Stateless HMAC confirm tokens for destructive tools (FR-AGT-03).

A dry-run call returns a token bound to the tool, its canonical arguments and a digest of
the previewed state; the real call must present it. Format:
``base64url(json payload).base64url(HMAC-SHA256(secret, payload part))`` with payload
``{"v":1,"t":tool,"a":sha256(canonical args),"s":state_digest,"e":exp_epoch_s,"n":nonce}``.

There is no table, so any replica can verify. Replay protection comes from the state digest:
tools put the target's current version into it, so once the action has run, the same token
fails as "previewed state changed".
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any

from lakeloom.config import ConfigError, Settings
from lakeloom.errors import LakeloomError

__all__ = [
    "MIN_SECRET_BYTES",
    "ConfirmTokens",
    "confirm_tokens_from_settings",
    "require_confirm_secret",
]

MIN_SECRET_BYTES = 32
_VERSION = 1
MAX_TOKEN_LENGTH = 2048
_log = logging.getLogger("lakeloom.confirm")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(
        text.encode("ascii") + b"=" * (-len(text) % 4), altchars=b"-_", validate=True
    )


def args_digest(args: Mapping[str, Any]) -> str:
    bound = {k: v for k, v in args.items() if k != "confirm_token"}
    try:
        canonical = json.dumps(bound, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise LakeloomError(
            "validation_error",
            "arguments cannot be bound to a confirm token",
            "Pass only JSON values (no NaN/Infinity or unsupported types).",
        ) from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid() -> LakeloomError:
    return LakeloomError(
        "validation_error",
        "confirm token invalid",
        "Pass the token unchanged, or call the tool again without confirm_token to get a "
        "fresh preview and token.",
    )


class ConfirmTokens:
    def __init__(self, secret: bytes, ttl: int, clock: Callable[[], float] = time.time) -> None:
        if len(secret) < MIN_SECRET_BYTES:
            raise ConfigError(
                f"LAKELOOM_CONFIRM_SECRET must be at least {MIN_SECRET_BYTES} bytes long"
            )
        self._secret = secret
        self._ttl = ttl
        self._clock = clock

    def _mac(self, payload_part: str) -> bytes:
        return hmac.new(self._secret, payload_part.encode("ascii"), hashlib.sha256).digest()

    def issue(self, tool: str, args: Mapping[str, Any], state_digest: str) -> tuple[str, int]:
        """A token for ``tool(args)`` against ``state_digest``, and its expiry (epoch seconds)."""
        expires_at = int(self._clock()) + self._ttl
        payload = {
            "v": _VERSION,
            "t": tool,
            "a": args_digest(args),
            "s": state_digest,
            "e": expires_at,
            "n": secrets.token_hex(8),
        }
        part = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return f"{part}.{_b64(self._mac(part))}", expires_at

    def verify(self, token: str, tool: str, args: Mapping[str, Any], state_digest: str) -> None:
        """Raise a typed error unless ``token`` was issued for exactly this call and state."""
        payload = self._parse(token)
        if payload.get("t") != tool or payload.get("a") != args_digest(args):
            raise LakeloomError(
                "validation_error",
                "confirm token does not match this call",
                "The token is bound to one tool, one caller and one set of arguments; "
                "call the tool again without confirm_token, with the arguments you intend to "
                "apply, to get a fresh preview and token.",
            )
        expires = payload.get("e")
        if not isinstance(expires, int) or self._clock() >= expires:
            raise LakeloomError(
                "validation_error",
                "confirm token expired",
                f"Tokens last {self._ttl} seconds; "
                "call the tool again without confirm_token to get a fresh preview and token.",
            )
        if payload.get("s") != state_digest:
            raise LakeloomError(
                "conflict",
                "previewed state changed since the confirm token was issued",
                "The target changed or the action already ran; review it, then "
                "call the tool again without confirm_token to get a fresh preview and token.",
            )

    def _parse(self, token: str) -> dict[str, Any]:
        if len(token) > MAX_TOKEN_LENGTH:
            raise _invalid()
        try:
            part, mac_part = token.split(".")
            if not hmac.compare_digest(self._mac(part), _unb64(mac_part)):
                raise _invalid()
            payload = json.loads(_unb64(part))
        except LakeloomError:
            raise
        except (ValueError, UnicodeError, binascii.Error):
            raise _invalid() from None
        if not isinstance(payload, dict) or payload.get("v") != _VERSION:  # pyright: ignore[reportUnknownMemberType]
            raise _invalid()
        return payload  # pyright: ignore[reportUnknownVariableType]


def require_confirm_secret(settings: Settings) -> None:
    """Production must configure the secret (ConfigError otherwise); development may omit it."""
    if settings.env == "production" and settings.confirm_secret is None:
        raise ConfigError(
            "missing required configuration: set LAKELOOM_CONFIRM_SECRET "
            "(or LAKELOOM_CONFIRM_SECRET_FILE)"
        )


def confirm_tokens_from_settings(settings: Settings) -> ConfirmTokens:
    """Production requires ``LAKELOOM_CONFIRM_SECRET``; development falls back to a random one."""
    ttl = settings.confirm_ttl_seconds
    if settings.confirm_secret is not None:
        return ConfirmTokens(settings.confirm_secret.get_secret_value().encode("utf-8"), ttl)
    require_confirm_secret(settings)
    _log.warning(
        "no confirm-token key configured: using a random per-process key; "
        "confirm tokens will not survive restarts or work across replicas"
    )
    return ConfirmTokens(secrets.token_bytes(MIN_SECRET_BYTES), ttl)
