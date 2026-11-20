"""MCP tools for stage 3 of the ontology pipeline: the taxonomy (BT/NT hierarchy).

All writes create a draft proposal; nothing is applied until a reviewer approves it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from lakeloom.core.annotations import effective_graph
from lakeloom.core.operations import (
    AddConcept,
    ProposalOperation,
)
from lakeloom.core.taxonomy import CoverageModel
from lakeloom.core.taxonomy import assess_taxonomy_coverage as coverage_report
from lakeloom.errors import LakeloomError
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_authoring import (
    DRAFT_NOTE,
    MAX_TOPICS,
    READ_ONLY,
    SCOPE_PROPOSE_TEXT,
    SCOPE_READ_TEXT,
    WRITE,
    proposal_result,
    resolve_in_model,
    tool_result,
    vocabulary_gap_warning,
)
from lakeloom.server.tools_common import (
    PROPOSE_SCOPE,
    READ_SCOPE,
    client_name,
    guarded,
)
from lakeloom.server.tools_proposals import ProposalResult

__all__ = ["register_taxonomy_tools"]

ASSESS_TAXONOMY_COVERAGE_DESCRIPTION = f"""\
Measure how well a model's taxonomy covers a declared set of topics.  For each topic \
in `topics`, searches the model's concepts by name similarity.  Reports matched topics, \
gaps (topics with no concept), and depth violations (branches deeper than `depth`).

`depth` (default 5): maximum BT/NT hierarchy depth the taxonomy should have.
`granularity`: `"coarse"` (1-3 levels), `"medium"` (3-6), `"fine"` (6+) — affects \
context in the report only.
`exclusions`: topics explicitly out of scope (skipped in gap analysis).

Read-only, no side effects.  Unknown model → `not_found`.

{SCOPE_READ_TEXT}"""

PROPOSE_ADD_HIERARCHY_DESCRIPTION = f"""\
Add a parent concept and its children to a model in one call.  Creates the parent (if it \
does not already exist) and all child concepts, each with skos:broader pointing at the parent.

`parent_name`: name of the parent concept (created if absent; pass its FQN if it exists).
`children`: list of child term names (strings) or TermDraft objects for richer definitions.
`parent_definition`: optional definition for the parent concept.
`note`: free-text rationale recorded in the proposal.

{DRAFT_NOTE}

{SCOPE_PROPOSE_TEXT}"""


class CoverageReportResult(BaseModel):
    model: str
    total_concepts: int
    actual_max_depth: int
    declared_max_depth: int
    coverage_ratio: float
    matched_count: int
    gap_count: int
    depth_violation_count: int
    summary: str


def register_taxonomy_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the taxonomy tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: assess_taxonomy_coverage
    # ------------------------------------------------------------------

    async def assess_taxonomy_coverage(
        model: Annotated[str, Field(description="Model slug.")],
        topics: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=MAX_TOPICS,
                description="Topic names the taxonomy should cover (e.g. ['Customer', 'Order']).",
            ),
        ],
        ctx: Context,
        depth: Annotated[
            int, Field(ge=1, le=20, description="Maximum BT/NT hierarchy depth declared.")
        ] = 5,
        granularity: Annotated[
            Literal["coarse", "medium", "fine"],
            Field(description="Intended taxonomy granularity."),
        ] = "medium",
        exclusions: Annotated[
            list[str] | None,
            Field(max_length=50, description="Topics explicitly out of scope."),
        ] = None,
    ) -> Annotated[CallToolResult, CoverageReportResult]:
        async def body(_: Principal) -> CallToolResult:
            graph = await effective_graph(deps.models, deps.proposal_repo, model)
            coverage_model = CoverageModel.from_lists(
                topics, depth=depth, granularity=granularity, exclusions=list(exclusions or [])
            )
            report = coverage_report(graph, coverage_model)
            data = CoverageReportResult(
                model=model,
                total_concepts=report.total_concepts,
                actual_max_depth=report.actual_max_depth,
                declared_max_depth=report.declared_max_depth,
                coverage_ratio=report.coverage_ratio,
                matched_count=len(report.matched_topics),
                gap_count=len(report.gaps),
                depth_violation_count=len(report.depth_violations),
                summary=report.to_markdown(),
            )
            return tool_result(report.to_markdown(), data)

        return await guarded("assess_taxonomy_coverage", ctx, READ_SCOPE, body)

    mcp.add_tool(
        assess_taxonomy_coverage,
        name="assess_taxonomy_coverage",
        title="Assess taxonomy coverage",
        description=ASSESS_TAXONOMY_COVERAGE_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_add_hierarchy
    # ------------------------------------------------------------------

    async def propose_add_hierarchy(
        model: Annotated[str, Field(description="Target model slug.")],
        parent_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=128,
                description=(
                    "Name of the parent concept.  Created if it does not exist in the model; "
                    "pass the FQN (e.g. `retail.Order`) if it already exists."
                ),
            ),
        ],
        children: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=200,
                description=(
                    "Child concept names.  Each becomes a new concept with skos:broader "
                    "pointing at the parent."
                ),
            ),
        ],
        ctx: Context,
        parent_definition: Annotated[
            str | None,
            Field(max_length=4000, description="Definition for the parent concept (optional)."),
        ] = None,
        note: Annotated[
            str | None,
            Field(max_length=1000, description="Rationale recorded in the proposal."),
        ] = None,
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            from lakeloom.untrusted import UntrustedText

            ops: list[ProposalOperation] = []
            parent = parent_name
            try:  # an existing parent (by FQN, IRI or unique name) is reused, never recreated
                parent = await resolve_in_model(deps, model, parent_name)
                exists = True
            except LakeloomError as exc:
                if exc.code != "not_found" or "." in parent_name:
                    raise
                exists = False
            parent_description = (
                UntrustedText(text=parent_definition, source="user")
                if parent_definition
                else UntrustedText(
                    text=f"Child of {parent_name} in the taxonomy of model {model}.",
                    source="user",
                )
            )
            if not exists:
                ops.append(
                    AddConcept(
                        op="add_concept",
                        name=parent_name,
                        description=parent_description,
                        rule_ids=["R-CV-DIRECT"],
                    )
                )
            for child in children:
                ops.append(
                    AddConcept(
                        op="add_concept",
                        name=child,
                        parent=parent,
                        description=UntrustedText(
                            text=f"Term added as a child of {parent_name} in the taxonomy of "
                            f"model {model}.",
                            source="user",
                        ),
                        rule_ids=["R-CV-DIRECT"],
                    )
                )
            warnings = await vocabulary_gap_warning(deps, model, "building the taxonomy")
            if note:
                warnings.append(f"Author note: {note}")
            p = await deps.proposals.propose_operations(
                model,
                ops,
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_add_hierarchy",
                warnings=warnings,
                recheck_pending=True,
            )
            return proposal_result(p, deps)

        return await guarded(
            "propose_add_hierarchy", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_add_hierarchy,
        name="propose_add_hierarchy",
        title="Propose add hierarchy",
        description=PROPOSE_ADD_HIERARCHY_DESCRIPTION,
        annotations=WRITE,
    )
