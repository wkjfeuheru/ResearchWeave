"""File reading tool."""

from __future__ import annotations

from pathlib import Path
import hashlib
from contextlib import nullcontext

from pydantic import BaseModel, Field

from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult
from researchx.state.errors import ResearchError


class FileReadToolInput(BaseModel):
    """Arguments for the file read tool."""

    path: str = Field(description="要读取的文件路径")
    offset: int = Field(default=0, ge=0, description="从零开始计数的起始行号")
    limit: int = Field(default=200, ge=1, le=2000, description="返回的行数")


class FileReadTool(BaseTool[FileReadToolInput]):
    """Read a UTF-8 text file with line numbers."""

    name = "read_file"
    contract = {
        "name": "read_file",
        "source": "builtin",
        "effect": "read_only",
        "required_capabilities": ("filesystem.read",),
        "resources_read": ("path",),
    }
    description = "读取研究工作区中的文本文件。"
    input_model = FileReadToolInput

    def is_read_only(self, arguments: FileReadToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: FileReadToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            return await self._read(arguments, context)
        except (ResearchError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(output=str(exc), is_error=True)

    async def _read(
        self, arguments: FileReadToolInput, context: ToolExecutionContext
    ) -> ToolResult:
        from researchx.skills.resources import validate_skill_file_path, selected_skill_resource

        candidate = Path(arguments.path).expanduser()
        candidate = candidate if candidate.is_absolute() else context.cwd / candidate
        validate_skill_file_path(candidate)
        resource = selected_skill_resource(context, candidate)
        path = resource if resource else (await context.resolve_path(arguments.path))

        from researchx.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active() and resource is None:
            from researchx.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(output=f"Sandbox: {reason}", is_error=True)

        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(output=f"Cannot read directory: {path}", is_error=True)

        async with nullcontext(resource) if resource else context.file_lock(arguments.path) as path:
            raw = path.read_bytes()
        if b"\x00" in raw:
            return ToolResult(output=f"Binary file cannot be read as text: {path}", is_error=True)

        project_mode = (await context.workspace_runtime()) is not None
        runtime = await context.workspace_runtime()
        memory_file = bool(
            runtime
            and path
            == (
                await runtime.resolve_workspace(
                    runtime.repository._project((await runtime.store.load())).id
                )
            )
            / "MEMORY.md"
        )
        text = raw.decode("utf-8", errors="strict" if project_mode else "replace")
        lines = text.splitlines()
        selected = lines[arguments.offset : arguments.offset + arguments.limit]
        numbered = [
            f"{arguments.offset + index + 1:>6}\t{line}" for index, line in enumerate(selected)
        ]
        digest = hashlib.sha256(raw).hexdigest()
        output = "\n".join(numbered) if numbered else f"(no content in selected range for {path})"
        if project_mode:
            output += f"\n\nexpected_sha256: {digest} (whole file; use for write_file overwrite)"
        return ToolResult(
            output=output,
            metadata={
                **(
                    {"context_component": "memory"}
                    if memory_file
                    else {"context_component": "dynamic_context"}
                    if resource
                    else {}
                ),
                "content_sha256": digest,
                "research_source_specs": [
                    {
                        "kind": "file",
                        "title": path.name,
                        "locator": f"{path}:{arguments.offset + 1}-{arguments.offset + len(selected)}",
                        "content": "\n".join(numbered),
                        "fragment": (project_mode and path.name == "MEMORY.md")
                        or arguments.offset > 0
                        or arguments.offset + len(selected) < len(lines),
                    }
                ],
            },
        )


def _resolve_path(base: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
