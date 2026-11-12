"""Pending reviews (FR-REV-05): proposals awaiting a reviewer, grouped by model and data product.

A proposal is pending when it is ``submitted`` or ``apply_failed``. For a caller, only models
whose ``review_groups`` admit them (:func:`~lakeloom.core.model_policy.may`) count, and the
proposals they created themselves are left out (a creator does not review their own work).

The data product of a proposal is the ``catalog.schema`` of its tables' lake full names
(ADR 0012 § Pending reviews: "data product = lake schema"; the provenance holds no OM data
products and this listing makes no OM calls). A proposal over several schemas is grouped under
the first (sorted) and lists all of them; one without tables is grouped under
``(no table)``. Age is the time since the proposal was last stored (its submission, as
submitting stores it), else its creation.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from lakeloom.core.callers import Caller
from lakeloom.core.model_policy import ModelPolicyEntry, access_for, may
from lakeloom.core.proposals import Proposal
from lakeloom.state.proposals import PENDING_STATUSES, ProposalRepository, encode_cursor

__all__ = [
    "NO_TABLE",
    "DataProductGroup",
    "ModelGroup",
    "PendingPage",
    "PendingReview",
    "group_pending",
    "list_pending",
]

NO_TABLE = "(no table)"
_BATCH = 100


class PendingReview(BaseModel):
    id: str
    model: str
    status: str
    data_product: str = Field(description="Lake schema (catalog.schema) of the first table.")
    data_products: list[str] = []
    parts: int
    operations: dict[str, int] = Field(description="Operation counts by type.")
    proposer: str = Field(description="Subject of the principal that proposed it (untrusted).")
    proposer_type: str
    age_seconds: int


class DataProductGroup(BaseModel):
    data_product: str
    proposals: list[PendingReview]


class ModelGroup(BaseModel):
    model: str
    data_products: list[DataProductGroup]


class PendingPage(BaseModel):
    items: list[PendingReview]
    next_cursor: str | None = None


def data_products(p: Proposal) -> list[str]:
    """Distinct ``catalog.schema`` of the proposal's tables, sorted (``[]`` without tables)."""
    names = [t.fqn for t in p.provenance.tables] or [t for part in p.parts for t in part.tables]
    return sorted({".".join(n.split(".")[:2]) for n in names if n})


def _entry(p: Proposal, now: datetime) -> PendingReview:
    products = data_products(p)
    ops = Counter(op.op for op in p.operations())
    return PendingReview(
        id=p.id,
        model=p.model,
        status=p.status,
        data_product=products[0] if products else NO_TABLE,
        data_products=products,
        parts=len(p.parts),
        operations=dict(sorted(ops.items())),
        proposer=p.provenance.principal_subject,
        proposer_type=p.provenance.principal_type,
        age_seconds=max(0, int((now - p.updated_at).total_seconds())),
    )


async def list_pending(
    repo: ProposalRepository,
    policies: Sequence[ModelPolicyEntry],
    principal: Caller,
    *,
    model: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
    exclude_own: bool = True,
) -> PendingPage:
    """One page (oldest first) of the pending proposals ``principal`` may review."""
    now = datetime.now(UTC)
    found: list[Proposal] = []
    after = cursor
    while len(found) <= limit:
        batch, after = await repo.list_by_status(PENDING_STATUSES, model, after, _BATCH)
        for p in batch:
            if exclude_own and p.provenance.principal_subject == principal.subject:
                continue
            if may(principal, access_for(policies, p.model), "review"):
                found.append(p)
        if after is None:
            break
    page = found[:limit]
    more = len(found) > limit
    return PendingPage(
        items=[_entry(p, now) for p in page],
        next_cursor=encode_cursor(page[-1].id) if more else None,
    )


def group_pending(items: Sequence[PendingReview]) -> list[ModelGroup]:
    """Group by model, then data product (both sorted); proposals keep their order."""
    tree: dict[str, dict[str, list[PendingReview]]] = {}
    for e in items:
        tree.setdefault(e.model, {}).setdefault(e.data_product, []).append(e)
    return [
        ModelGroup(
            model=m,
            data_products=[
                DataProductGroup(data_product=d, proposals=tree[m][d]) for d in sorted(tree[m])
            ],
        )
        for m in sorted(tree)
    ]
