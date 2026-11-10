"""The known OAuth scopes (shared by config and the server; imports nothing from either)."""

from typing import Final

__all__ = ["SCOPES"]

SCOPES: Final = (
    "lakeloom:read",
    "lakeloom:propose",
    "lakeloom:review",
    "lakeloom:publish",
    "lakeloom:admin",
)
