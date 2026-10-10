"""String-based file editing tool."""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult
from researchx.state.errors import ResearchError
from researchx.storage.filesystem import atomic_write_text


class FileEditToolInput(BaseModel):
    """Arguments for the file edit tool."""

    path: str = Field(description="要编辑的文件路径")
    old_str: str = Field(description="要替换的现有文本")
    new_str: str = Field(description="替换后的文本")
    replace_all: bool = Field(default=False)


class FileEditTool(BaseTool[FileEditToolInput]):
    """Replace text in an existing file."""

    name = "edit_file"
    contract = {
        "name": "edit_file",
        "source": "builtin",
        "effect": "local_write",
        "required_capabilities": ("filesystem.write",),
        "resources_write": ("path",),
    }
    description = "通过替换文本编辑已有文件。"
    input_model = FileEditToolInput

    async def execute(
        self,
        arguments: FileEditToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            return await self._edit(arguments, context)
        except OSError as exc:
            # Atomic replacement or directory creation may have started before the error.
            return ToolResult(
                output=str(exc), is_error=True, status="uncertain", error_code="filesystem_error"
            )
        except (ResearchError, UnicodeError) as exc:
            return ToolResult(output=str(exc), is_error=True)

    async def _edit(
        self, arguments: FileEditToolInput, context: ToolExecutionContext
    ) -> ToolResult:
        path = await context.resolve_path(arguments.path, write=True)

        from researchx.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active():
            from researchx.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(
                    output=f"Sandbox: {reason}", is_error=True, metadata={"no_effect": True}
                )

        if not path.exists():
            return ToolResult(
                output=f"File not found: {path}", is_error=True, metadata={"no_effect": True}
            )

        async with context.file_lock(arguments.path) as path:
            original = path.read_text(encoding="utf-8")
        if arguments.old_str not in original:
            return ToolResult(
                output="old_str was not found in the file",
                is_error=True,
                metadata={"no_effect": True},
            )

        if arguments.replace_all:
            updated = original.replace(arguments.old_str, arguments.new_str)
        else:
            updated = original.replace(arguments.old_str, arguments.new_str, 1)

        approval_prompt = context.metadata.get("edit_approval_prompt") if context.metadata else None
        stats = ""
        if approval_prompt is not None:
            diff_text, added, removed = _compute_diff(str(path), original, updated)
            reply = await approval_prompt(str(path), diff_text, added, removed)
            if reply == "reject":
                return ToolResult(
                    output=f"Edit rejected by user: {path}",
                    is_error=True,
                    metadata={"no_effect": True},
                )
            stats = f"  ({_ANSI_GREEN}+{added}{_ANSI_RESET} {_ANSI_RED}-{removed}{_ANSI_RESET})"
        async with context.file_lock(arguments.path, write=True) as path:
            current = path.read_text(encoding="utf-8")
            if approval_prompt is not None and current != original:
                return ToolResult(
                    output="File changed during edit approval; read it again",
                    is_error=True,
                    metadata={"no_effect": True},
                )
            if arguments.old_str not in current:
                return ToolResult(
                    output="old_str was not found in the latest file",
                    is_error=True,
                    metadata={"no_effect": True},
                )
            # Reapply to the latest contents inside the lock, preserving concurrent unrelated edits.
            updated = current.replace(
                arguments.old_str, arguments.new_str, -1 if arguments.replace_all else 1
            )
            atomic_write_text(path, updated)
        return ToolResult(output=f"Updated {path}{stats}")


def _resolve_path(base: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _compute_diff(filename: str, original: str, updated: str) -> tuple[str, int, int]:
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=filename,
            tofile=filename,
            lineterm="",
        )
    )
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return "".join(diff_lines), added, removed


_ANSI_GREEN = "\033[32m"
_ANSI_RED = "\033[31m"
_ANSI_RESET = "\033[0m"
