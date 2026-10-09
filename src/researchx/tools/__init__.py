"""The investment research tool registry."""

from typing import cast
from pydantic import BaseModel
from researchx.mcp.client import McpClientManager

from researchx.tools.ask_user_question_tool import AskUserQuestionTool
from researchx.tools.bash_tool import BashTool
from researchx.tools.file_edit_tool import FileEditTool
from researchx.tools.file_read_tool import FileReadTool
from researchx.tools.file_write_tool import FileWriteTool
from researchx.tools.glob_tool import GlobTool
from researchx.tools.grep_tool import GrepTool
from researchx.tools.image_to_text_tool import ImageToTextTool
from researchx.tools.skill_tool import SkillTool
from researchx.tools.tool_search_tool import ToolSearchTool
from researchx.tools.web_fetch_tool import WebFetchTool
from researchx.tools.web_search_tool import WebSearchTool
from researchx.tools.research_memory_tool import ResearchMemoryTool
from researchx.tools.planner_tool import PlannerTool
from researchx.tools.replanner_tool import ReplannerTool
from researchx.tools.research_project_tool import ResearchProjectTool
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult
from researchx.tools.list_mcp_resources_tool import ListMcpResourcesTool
from researchx.tools.read_mcp_resource_tool import ReadMcpResourceTool
from researchx.tools.mcp_tool import McpToolAdapter
from researchx.tools.dispatch_subagents_tool import DispatchSubagentsTool

from researchx.tools.contracts import RETIRED_TOOL_NAMES

# Compatibility import for plugins and existing callers. Both modes retain the same product surface.
RESEARCH_EXCLUDED_TOOLS = RETIRED_TOOL_NAMES


def create_research_tool_registry(
    mcp_manager: McpClientManager | None = None, *, mode: str = "research"
) -> ToolRegistry:
    """Register only shared research capabilities and configured MCP tools."""
    if mode not in {"research", "general"}:
        raise ValueError("Unknown tool registry mode")
    registry = ToolRegistry()
    for tool in (
        AskUserQuestionTool(),
        BashTool(),
        FileEditTool(),
        FileReadTool(),
        FileWriteTool(),
        GlobTool(),
        GrepTool(),
        ImageToTextTool(),
        ResearchMemoryTool(),
        PlannerTool(),
        ReplannerTool(),
        ResearchProjectTool(),
        DispatchSubagentsTool(),
        SkillTool(),
        ToolSearchTool(),
        WebFetchTool(),
        WebSearchTool(),
    ):
        if mode != "research" or tool.name not in RESEARCH_EXCLUDED_TOOLS:
            registry.register(cast(BaseTool[BaseModel], tool))
    if mcp_manager is not None:
        registry.register(ListMcpResourcesTool(mcp_manager))
        registry.register(ReadMcpResourceTool(mcp_manager))
        for info in mcp_manager.list_tools():
            registry.register(McpToolAdapter(mcp_manager, info))
    return registry


__all__ = [
    "BaseTool",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolResult",
    "create_research_tool_registry",
]
