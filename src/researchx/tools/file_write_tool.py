"""File writing tool."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from pydantic import BaseModel, Field

from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult
from researchx.state.errors import ResearchError
from researchx.storage.filesystem import atomic_write_text


class FileWriteToolInput(BaseModel):
    """Arguments for the file write tool."""

    path: str = Field(description="要写入的文件路径")
    content: str = Field(description="文件的完整内容")
    create_directories: bool = Field(default=True)
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        description="read_file 返回的哈希；覆盖已有研究项目文件时必填",
    )


class FileWriteTool(BaseTool[FileWriteToolInput]):
    """Write complete file contents."""

    name = "write_file"
    contract = {
        "name": "write_file",
        "source": "builtin",
        "effect": "local_write",
        "required_capabilities": ("filesystem.write",),
        "resources_write": ("path",),
    }
    description = "在研究工作区创建或覆盖文本文件。"
    input_model = FileWriteToolInput

    async def execute(
        self,
        arguments: FileWriteToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            return await self._write(arguments, context)
        except OSError as exc:
            # Atomic replacement or directory creation may have started before the error.
            return ToolResult(
                output=str(exc), is_error=True, status="uncertain", error_code="filesystem_error"
            )
        except (ResearchError, UnicodeError) as exc:
            return ToolResult(output=str(exc), is_error=True)

    async def _write(
        self, arguments: FileWriteToolInput, context: ToolExecutionContext
    ) -> ToolResult:
        path = context.resolve_path(arguments.path, write=True)

        from researchx.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active():
            from researchx.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(
                    output=f"Sandbox: {reason}", is_error=True, metadata={"no_effect": True}
                )

        project_mode = context.workspace_runtime() is not None
        with context.file_lock(arguments.path) as path:
            existed = path.exists()
            original_bytes = path.read_bytes() if existed else b""
            original = original_bytes.decode("utf-8")
            original_hash = hashlib.sha256(original_bytes).hexdigest()
            if project_mode and existed and arguments.expected_sha256 is None:
                return ToolResult(
                    output="Existing research file requires expected_sha256 from read_file; prefer edit_file",
                    is_error=True,
                    metadata={"no_effect": True},
                )
            if arguments.expected_sha256 is not None and (
                not existed or arguments.expected_sha256 != original_hash
            ):
                return ToolResult(
                    output="File changed: expected_sha256 conflict; read it again",
                    is_error=True,
                    metadata={"no_effect": True},
                )

        stats = ""
        approval_prompt = context.metadata.get("edit_approval_prompt") if context.metadata else None
        if approval_prompt is not None:
            diff_text, added, removed = _compute_diff(str(path), original, arguments.content)
            reply = await approval_prompt(str(path), diff_text, added, removed)
            if reply == "reject":
                return ToolResult(
                    output=f"Write rejected by user: {path}",
                    is_error=True,
                    metadata={"no_effect": True},
                )
            stats = f"  ({_ANSI_GREEN}+{added}{_ANSI_RESET} {_ANSI_RED}-{removed}{_ANSI_RESET})"
        with context.file_lock(arguments.path, write=True) as path:
            current = path.read_bytes().decode("utf-8") if path.exists() else ""
            if project_mode and (path.exists() != existed or current != original):
                return ToolResult(
                    output="File changed while preparing write; read it again",
                    is_error=True,
                    metadata={"no_effect": True},
                )
            if arguments.create_directories:
                path.parent.mkdir(parents=True, exist_ok=True)
            elif not path.parent.is_dir():
                raise FileNotFoundError(f"Parent directory does not exist: {path.parent}")
            atomic_write_text(path, arguments.content)
        return ToolResult(output=f"Wrote {path}{stats}")


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
