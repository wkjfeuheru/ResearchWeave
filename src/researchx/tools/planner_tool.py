"""Planner Agent as Tool: generate a proposal; the parent Runtime commits it."""

from __future__ import annotations
from researchx.tools.base import ToolExecutionContext

import json

from pydantic import Field

from researchx.state.models import PlanProposal, Record
from researchx.tools.base import BaseTool, ToolResult
from researchx.engine.planning_support import generate_plan, planning_packet


class PlannerInput(Record):
    objective: str = Field(min_length=1, description="已持久化研究目标中的规划重点")


async def planner_tool(objective: str, context: ToolExecutionContext) -> dict[str, object]:
    packet = await planning_packet(context)
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
    description = "使用受约束的只读规划 Agent 生成初始研究任务 DAG。先调用 research_project。Runtime 会验证并提交提案；此工具不能直接持久化计划。"
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
