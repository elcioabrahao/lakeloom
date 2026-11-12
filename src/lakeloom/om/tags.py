"""Glossary-term tags on data assets (realizations; FR-REV-02, ADR 0012).

Pure merge rules shared by ``OMClient.add_asset_tags``/``remove_asset_tags`` and the dev seed
script ``scripts/seed_om_concepts.py``. OM shows a table's tags on its columns with
``labelType: Derived``; those are not the column's own tags and are never written back.
"""

from collections.abc import Mapping, Sequence
from typing import Any


def glossary_tag(term_fqn: str) -> dict[str, str]:
    """The ``TagLabel`` that realizes the glossary term ``term_fqn`` on an asset."""
    return {"tagFQN": term_fqn, "source": "Glossary", "labelType": "Manual", "state": "Confirmed"}


def _own(existing: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(t) for t in existing if t.get("labelType") != "Derived"]


def merge_tags(existing: Sequence[Mapping[str, Any]], term_fqn: str) -> list[dict[str, Any]] | None:
    """Own tags plus the glossary tag for ``term_fqn``, or ``None`` when it is already there."""
    own = _own(existing)
    if any(t.get("tagFQN") == term_fqn for t in own):
        return None
    return [*own, glossary_tag(term_fqn)]


def remove_tags(
    existing: Sequence[Mapping[str, Any]], term_fqn: str
) -> list[dict[str, Any]] | None:
    """Own tags without ``term_fqn``, or ``None`` when the asset has no own tag for it."""
    own = _own(existing)
    kept = [t for t in own if t.get("tagFQN") != term_fqn]
    return None if len(kept) == len(own) else kept
