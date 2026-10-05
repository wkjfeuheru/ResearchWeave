"""The investment research tool registry."""

from openharness.tools.ask_user_question_tool import AskUserQuestionTool
from openharness.tools.bash_tool import BashTool
from openharness.tools.config_tool import ConfigTool
from openharness.tools.file_edit_tool import FileEditTool
from openharness.tools.file_read_tool import FileReadTool
from openharness.tools.file_write_tool import FileWriteTool
from openharness.tools.glob_tool import GlobTool
from openharness.tools.grep_tool import GrepTool
from openharness.tools.image_generation_tool import ImageGenerationTool
from openharness.tools.image_to_text_tool import ImageToTextTool
from openharness.tools.mcp_auth_tool import McpAuthTool
from openharness.tools.notebook_edit_tool import NotebookEditTool
from openharness.tools.skill_tool import SkillTool
from openharness.tools.sleep_tool import SleepTool
from openharness.tools.tool_search_tool import ToolSearchTool
from openharness.tools.web_fetch_tool import WebFetchTool
from openharness.tools.web_search_tool import WebSearchTool
from openharness.tools.research_memory_tool import ResearchMemoryTool
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult
from openharness.tools.list_mcp_resources_tool import ListMcpResourcesTool
from openharness.tools.read_mcp_resource_tool import ReadMcpResourceTool
from openharness.tools.mcp_tool import McpToolAdapter


def create_research_tool_registry(mcp_manager=None) -> ToolRegistry:
    """Register only shared research capabilities and configured MCP tools."""
    registry = ToolRegistry()
    for tool in (
        AskUserQuestionTool(),
        BashTool(),
        ConfigTool(),
        FileEditTool(),
        FileReadTool(),
        FileWriteTool(),
        GlobTool(),
        GrepTool(),
        ImageGenerationTool(),
        ImageToTextTool(),
        McpAuthTool(),
        NotebookEditTool(),
        ResearchMemoryTool(),
        SkillTool(),
        SleepTool(),
        ToolSearchTool(),
        WebFetchTool(),
        WebSearchTool(),
    ):
        registry.register(tool)
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
