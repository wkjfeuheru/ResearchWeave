"""Replanner Agent as Tool: return an incremental, revision-pinned PlanPatch."""

from __future__ import annotations
from openharness.tools.base import ToolExecutionContext

import json

from pydantic import Field

from openharness.research.models import PlanPatch, Record
from openharness.tools.base import BaseTool, ToolResult
from openharness.tools.research.planning import generate_plan, planning_packet


class ReplannerInput(Record):
    reason: str = Field(min_length=1)


async def replanner_tool(reason: str, context: ToolExecutionContext) -> dict[str, object]:
    packet = planning_packet(context)
    packet["reason"] = reason
    return await generate_plan(PlanPatch, packet, context, kind="replanner")


class ReplannerTool(BaseTool[ReplannerInput]):
    name = "replanner"
    contract = {
        "name": "replanner",
        "source": "builtin",
        "effect": "model_call",
        "required_capabilities": ("model.call",),
        "resources_write": ("research.control",),
        "parallelism": "resources",
    }
    description = "Propose an incremental PlanPatch for the current plan and objective revision. Preserve unaffected tasks and evidence; revise impacted tasks and invalidate their artifacts. Runtime validates DAG and CAS before committing."
    input_model = ReplannerInput

    def is_read_only(self, arguments: ReplannerInput) -> bool:
        return True

    async def execute(self, arguments: ReplannerInput, context: ToolExecutionContext) -> ToolResult:
        try:
            result = await replanner_tool(arguments.reason, context)
            return ToolResult(
                output=json.dumps(result, ensure_ascii=False),
                metadata={
                    "context_component": "dynamic_context",
                    "plan_patch": result["patch"],
                    "planning_tokens": result["planning_tokens"],
                    "research_source_specs": [],
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Replanner failed: {type(exc).__name__}: {exc}",
                is_error=True,
                metadata={
                    "planning_tokens": getattr(exc, "planning_tokens", 0),
                    "research_source_specs": [],
                },
            )
