"""Replanner Agent as Tool: return an incremental, revision-pinned PlanPatch."""

from __future__ import annotations
from researchx.tools.base import ToolExecutionContext

import json

from pydantic import Field

from researchx.state.models import PlanPatch, Record
from researchx.tools.base import BaseTool, ToolResult
from researchx.engine.planning_support import generate_plan, planning_packet


class ReplannerInput(Record):
    reason: str = Field(min_length=1)


async def replanner_tool(reason: str, context: ToolExecutionContext) -> dict[str, object]:
    packet = await planning_packet(context)
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
    description = "针对当前计划和目标版本提出增量 PlanPatch。保留未受影响的任务和证据；修改受影响的任务并使其产物失效。Runtime 会在提交前验证 DAG 和 CAS。"
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
