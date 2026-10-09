"""The investment research tool registry."""

from typing import cast
from pydantic import BaseModel
from openharness.mcp.client import McpClientManager

from openharness.tools.ask_user_question_tool import AskUserQuestionTool
from openharness.tools.bash_tool import BashTool
from openharness.tools.file_edit_tool import FileEditTool
from openharness.tools.file_read_tool import FileReadTool
from openharness.tools.file_write_tool import FileWriteTool
from openharness.tools.glob_tool import GlobTool
from openharness.tools.grep_tool import GrepTool
from openharness.tools.image_to_text_tool import ImageToTextTool
from openharness.tools.skill_tool import SkillTool
from openharness.tools.tool_search_tool import ToolSearchTool
from openharness.tools.web_fetch_tool import WebFetchTool
from openharness.tools.web_search_tool import WebSearchTool
from openharness.tools.research_memory_tool import ResearchMemoryTool
from openharness.tools.research.planner import PlannerTool
from openharness.tools.research.replanner import ReplannerTool
from openharness.tools.research.project import ResearchProjectTool
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult
from openharness.tools.list_mcp_resources_tool import ListMcpResourcesTool
from openharness.tools.read_mcp_resource_tool import ReadMcpResourceTool
from openharness.tools.mcp_tool import McpToolAdapter
from openharness.tools.dispatch_subagents_tool import DispatchSubagentsTool

RESEARCH_EXCLUDED_TOOLS = frozenset(
    {"notebook_edit", "config", "mcp_auth", "image_generation", "sleep"}
)


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
