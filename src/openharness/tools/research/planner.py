"""Planner Agent as Tool: generate a proposal; the parent Runtime commits it."""

from __future__ import annotations
from openharness.tools.base import ToolExecutionContext

import json

from pydantic import Field

from openharness.research.models import PlanProposal, Record
from openharness.tools.base import BaseTool, ToolResult
from openharness.tools.research.planning import generate_plan, planning_packet


class PlannerInput(Record):
    objective: str = Field(
        min_length=1, description="Planning focus within the persisted research objective"
    )


async def planner_tool(objective: str, context: ToolExecutionContext) -> dict[str, object]:
    packet = planning_packet(context)
    packet["focus"] = objective
    return await generate_plan(PlanProposal, packet, context, kind="planner")


class PlannerTool(BaseTool[PlannerInput]):
    name = "planner"
    contract = {
        "name": "planner",
        "source": "builtin",
        "effect": "model_call",
        "required_capabilities": ("model.call",),
        "resources_write": ("research.control",),
        "parallelism": "resources",
    }
    description = "Generate the initial research task DAG using a bounded read-only planning agent. Start research_project first. Runtime validates and commits the proposal; this tool cannot persist plans."
    input_model = PlannerInput

    def is_read_only(self, arguments: PlannerInput) -> bool:
        return True

    async def execute(self, arguments: PlannerInput, context: ToolExecutionContext) -> ToolResult:
        try:
            result = await planner_tool(arguments.objective, context)
            return ToolResult(
                output=json.dumps(result, ensure_ascii=False),
                metadata={
                    "context_component": "dynamic_context",
                    "plan_proposal": result["proposal"],
                    "planning_tokens": result["planning_tokens"],
                    "research_source_specs": [],
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Planner failed: {type(exc).__name__}: {exc}",
                is_error=True,
                metadata={
                    "planning_tokens": getattr(exc, "planning_tokens", 0),
                    "research_source_specs": [],
                },
            )
