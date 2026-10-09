"""MCP tool adapters."""

from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, Field, create_model

from researchx.mcp.client import McpServerNotConnectedError, McpToolReturnedError
from researchx.mcp.types import McpToolInfo
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult


class McpToolCaller(Protocol):
    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> str: ...


class McpToolAdapter(BaseTool[BaseModel]):
    """Expose one MCP tool as a normal ResearchX tool."""

    def __init__(self, manager: McpToolCaller, tool_info: McpToolInfo) -> None:
        self._manager = manager
        self._tool_info = tool_info
        server_segment = _sanitize_tool_segment(tool_info.server_name)
        tool_segment = _sanitize_tool_segment(tool_info.name)
        self.name = f"mcp__{server_segment}__{tool_segment}"
        self.contract = {
            "name": self.name,
            "source": "mcp",
            "effect": "external_write",
            "required_capabilities": ("mcp.call",),
            "resources_write": ("*",),
        }
        self.description = tool_info.description or f"MCP 工具 {tool_info.name}"
        self.input_model = _input_model_from_schema(self.name, tool_info.input_schema)

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        del context
        try:
            output = await self._manager.call_tool(
                self._tool_info.server_name,
                self._tool_info.name,
                arguments.model_dump(mode="json", exclude_none=True),
            )
        except (McpServerNotConnectedError, McpToolReturnedError) as exc:
            code = getattr(exc, "code", "connection")
            description = {
                "invalid_response": "工具返回异常：数据结构或结果处理失败",
                "timeout": "工具执行超时",
                "connection": "外部服务连接失败",
                "tool_error": "外部服务返回工具错误",
            }.get(code, "外部工具失败")
            detail = f"{self._tool_info.server_name}/{self._tool_info.name}：{description}"
            return ToolResult(
                output=str(exc),
                is_error=True,
                metadata={
                    "outcome": "error",
                    "error_code": code,
                    "detail": detail,
                    "research_source_specs": [],
                    "no_effect": isinstance(exc, McpServerNotConnectedError)
                    and exc.request_not_sent,
                },
            )
        return ToolResult(
            output=output,
            metadata={
                "research_source_specs": [
                    {
                        "kind": "mcp",
                        "title": self._tool_info.name,
                        "locator": f"mcp:{self._tool_info.server_name}/{self._tool_info.name}",
                        "content": output,
                        "fragment": False,
                    }
                ]
            },
        )


_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _input_model_from_schema(tool_name: str, schema: dict[str, object]) -> type[BaseModel]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return create_model(f"{tool_name.title()}Input")

    fields: dict[str, Any] = {}
    required_value = schema.get("required", [])
    required = set(required_value) if isinstance(required_value, list) else set()
    for key in properties:
        prop = properties[key] if isinstance(properties[key], dict) else {}
        py_type = _JSON_TYPE_MAP.get(str(prop.get("type", "")), object)
        if key in required:
            fields[key] = (py_type, Field(default=...))
        else:
            fields[key] = (py_type | None, Field(default=None))
    return create_model(f"{tool_name.title().replace('-', '_')}Input", __base__=BaseModel, **fields)


def _sanitize_tool_segment(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not sanitized:
        return "tool"
    if not sanitized[0].isalpha():
        return f"mcp_{sanitized}"
    return sanitized
