"""OpenMetadata fully-qualified-name construction (FR-SRC-07)."""

__all__ = ["build_fqn"]


def build_fqn(*parts: str) -> str:
    """Join ``parts`` with ``.``; a part containing ``.`` or ``"`` is double-quoted.

    Inner double quotes are doubled, matching OpenMetadata's FullyQualifiedName quoting.
    """
    return ".".join('"' + p.replace('"', '""') + '"' if "." in p or '"' in p else p for p in parts)
