"""Prefixed ULID identifiers shared by the state repositories."""

import secrets
import time

__all__ = ["new_ulid_id"]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid_id(prefix: str, now_ms: int | None = None) -> str:
    """``prefix`` + ULID: 48-bit millisecond timestamp then 80 random bits, 26 chars."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | secrets.randbits(80)
    chars = [_CROCKFORD[(value >> shift) & 0x1F] for shift in range(125, -1, -5)]
    return prefix + "".join(chars)
