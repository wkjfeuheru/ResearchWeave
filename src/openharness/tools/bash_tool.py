"""Shell command execution tool."""

from __future__ import annotations
from openharness.utils.session_files import FileManifest
from typing import TYPE_CHECKING
from openharness.engine.metadata import ExecutionLease

if TYPE_CHECKING:
    from openharness.research.runtime import ResearchAgentRuntime

import asyncio
import os
import json
from uuid import uuid4
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

from openharness.sandbox import SandboxUnavailableError
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.shell import create_shell_subprocess, terminate_shell_process
from openharness.sandbox.policy import ExecutionOwner, report_settings, report_environment


_READ_REMAINING_OUTPUT_TIMEOUT_SECONDS = 2.0


class BashToolInput(BaseModel):
    """Arguments for the bash tool."""

    command: str = Field(description="Shell command to execute")
    cwd: str | None = Field(default=None, description="Working directory override")
    timeout_seconds: int = Field(default=600, ge=1, le=600)


class BashTool(BaseTool[BashToolInput]):
    """Execute a shell command with stdout/stderr capture."""

    name = "bash"
    contract = {
        "name": "bash",
        "source": "builtin",
        "effect": "unknown",
        "required_capabilities": ("shell.execute",),
        "resources_write": ("*",),
    }
    description = "Run a shell command in the research workspace."
    input_model = BashToolInput

    async def execute(self, arguments: BashToolInput, context: ToolExecutionContext) -> ToolResult:
        cwd = Path(arguments.cwd).expanduser() if arguments.cwd else context.cwd
        if context.workspace_runtime():
            from openharness.research.errors import ResearchError

            try:
                cwd = context.resolve_path(arguments.cwd)
            except (ResearchError, OSError) as exc:
                return ToolResult(output=str(exc), is_error=True)
        preflight_error = _preflight_interactive_command(arguments.command)
        if preflight_error is not None:
            return ToolResult(
                output=preflight_error,
                is_error=True,
                metadata={"interactive_required": True},
            )
        runtime = context.workspace_runtime()
        settings = context.settings
        owner = None
        execution = context.metadata.get("research_execution")
        if runtime:
            from openharness.config import Settings
            from openharness.research.errors import ResearchError

            project = runtime.store.load().project
            assert project is not None
            workspace = runtime.resolve_workspace(project.id)
            # Direct tool callers without trusted configuration also fail closed.
            try:
                settings = report_settings(settings or Settings(), workspace)
            except (SandboxUnavailableError, OSError) as exc:
                return ToolResult(output=str(exc), is_error=True)
            owner = ExecutionOwner(
                context.runtime_id or "direct",
                project.id,
                execution["id"] if execution else uuid4().hex,
                workspace,
            )
            env = report_environment(workspace)
        else:
            env = dict(os.environ)
            for key in (
                "OPENHARNESS_RESEARCH_SESSION_DIR",
                "OPENHARNESS_RESEARCH_TASK_ID",
                "OPENHARNESS_RESEARCH_EXECUTION",
            ):
                env.pop(key, None)
            store = context.metadata.get("research_store")
            if store is not None:
                env["OPENHARNESS_RESEARCH_SESSION_DIR"] = str(store.directory)
                task_id = store.load().research_state.current_task_id
                if task_id:
                    env["OPENHARNESS_RESEARCH_TASK_ID"] = task_id
        process: asyncio.subprocess.Process | None = None
        output_buffer = bytearray()
        try:
            process = await create_shell_subprocess(
                arguments.command,
                cwd=cwd,
                settings=settings,
                owner=owner,
                env=env,
                prefer_pty=owner is None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async def capture() -> None:
                assert process is not None
                if process.stdout:
                    while chunk := await process.stdout.read(65536):
                        # Drain continuously even after reaching the display bound.
                        if len(output_buffer) < 1024 * 1024:
                            output_buffer.extend(chunk)

            reader = asyncio.create_task(capture())
            try:
                await asyncio.wait_for(process.wait(), timeout=arguments.timeout_seconds)
                try:
                    await asyncio.wait_for(reader, _READ_REMAINING_OUTPUT_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    pass
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if owner and settings and settings.sandbox.backend == "docker":
                from openharness.sandbox.session import stop_docker_sandbox

                await asyncio.shield(stop_docker_sandbox(owner.key))
            metadata: dict[str, object] = {"returncode": process.returncode}
            if runtime and execution and process.returncode == 0:
                try:
                    metadata["exported_files"] = _register_exports(
                        output_buffer, context, runtime, execution
                    )
                except (ResearchError, ValueError, OSError) as exc:
                    return ToolResult(output=f"Export registration rejected: {exc}", is_error=True)
            return ToolResult(
                output=_format_output(output_buffer),
                is_error=process.returncode != 0,
                metadata=metadata,
            )
        except SandboxUnavailableError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except asyncio.TimeoutError:
            if process:
                await terminate_shell_process(process, force=True)
            return ToolResult(
                output=_format_timeout_output(
                    output_buffer,
                    command=arguments.command,
                    timeout_seconds=arguments.timeout_seconds,
                ),
                is_error=True,
                metadata={"returncode": process.returncode if process else None, "timed_out": True},
            )
        except asyncio.CancelledError:
            if process:
                await terminate_shell_process(process)
            raise
        finally:
            if owner and settings and settings.sandbox.backend == "docker":
                from openharness.sandbox.session import stop_docker_sandbox

                await asyncio.shield(stop_docker_sandbox(owner.key))


async def _terminate_process(process: asyncio.subprocess.Process, *, force: bool) -> None:
    await terminate_shell_process(process, force=force)


def _register_exports(
    output: bytearray,
    context: ToolExecutionContext,
    runtime: ResearchAgentRuntime,
    execution: ExecutionLease,
) -> list[FileManifest]:
    """Import declarations on the host, only while the execution lease remains valid."""
    from openharness.research.errors import ResearchError
    from openharness.utils.file_lock import exclusive_file_lock
    from openharness.utils.session_files import SessionFiles

    declarations = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            packet = json.loads(line)
        except ValueError:
            continue
        if isinstance(packet, dict) and isinstance(packet.get("files"), list):
            for value in packet["files"]:
                if not isinstance(value, str):
                    raise ValueError("Export paths must be strings")
                path = context.resolve_path(value)
                if not any(
                    path.is_relative_to(context.cwd / directory)
                    for directory in ("reports", "artifacts")
                ):
                    raise ResearchError("Export must belong to current workspace reports/artifacts")
                if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
                    raise ResearchError("Export file missing or too large")
                declarations.append((path, str(packet.get("status", "candidate"))))
    if len(declarations) > 50:
        raise ValueError("Too many exported files")
    with exclusive_file_lock(runtime.store.lock):
        memory = runtime.store._load()
        active = memory.executions.get(execution["id"])
        if active is None or not runtime.repository.execution_valid(memory, active):
            raise ResearchError("Export execution was revoked")
        return [
            SessionFiles(runtime.store.directory).register(
                path, task_id=active.task_id, status=status, kind="export", execution=execution
            )
            for path, status in declarations
        ]


async def _read_remaining_output(process: asyncio.subprocess.Process) -> bytearray:
    output_buffer = bytearray()
    if process.stdout is not None:
        try:
            remaining = await asyncio.wait_for(
                process.stdout.read(),
                timeout=_READ_REMAINING_OUTPUT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            remaining = b""
        output_buffer.extend(remaining)
    return output_buffer


async def _drain_available_output(
    stream: asyncio.StreamReader | None,
    *,
    read_timeout: float = 0.05,
) -> bytearray:
    output_buffer = bytearray()
    if stream is None:
        return output_buffer
    while True:
        try:
            chunk = await asyncio.wait_for(stream.read(65536), timeout=read_timeout)
        except asyncio.TimeoutError:
            return output_buffer
        if not chunk:
            return output_buffer
        output_buffer.extend(chunk)


def _format_output(output_buffer: bytearray) -> str:
    text = output_buffer.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    if not text:
        return "(no output)"
    if len(text) > 12000:
        return f"{text[:12000]}\n...[truncated]..."
    return text


def _format_timeout_output(output_buffer: bytearray, *, command: str, timeout_seconds: int) -> str:
    parts = [f"Command timed out after {timeout_seconds} seconds."]
    text = _format_output(output_buffer)
    if text != "(no output)":
        parts.extend(["", "Partial output:", text])
    hint = _interactive_command_hint(command=command, output=text)
    if hint:
        parts.extend(["", hint])
    return "\n".join(parts)


def _preflight_interactive_command(command: str) -> str | None:
    lowered_command = command.lower()
    if not _looks_like_interactive_scaffold(lowered_command):
        return None
    return (
        "This command appears to require interactive input before it can continue. "
        "The bash tool is non-interactive, so it cannot answer installer/scaffold prompts live. "
        "Prefer non-interactive flags (for example --yes, -y, --skip-install, --defaults, --non-interactive), "
        "or run the scaffolding step once in an external terminal before asking the agent to continue."
    )


def _interactive_command_hint(*, command: str, output: str) -> str | None:
    lowered_command = command.lower()
    if _looks_like_interactive_scaffold(lowered_command) or _looks_like_prompt(output):
        return (
            "This command appears to require interactive input. "
            "The bash tool is non-interactive, so prefer non-interactive flags "
            "(for example --yes, -y, --skip-install, or similar) or run the "
            "scaffolding step once in an external terminal before continuing."
        )
    return None


def _looks_like_interactive_scaffold(lowered_command: str) -> bool:
    scaffold_markers: tuple[str, ...] = (
        "create-next-app",
        "npm create ",
        "pnpm create ",
        "yarn create ",
        "bun create ",
        "pnpm dlx ",
        "npm init ",
        "pnpm init ",
        "yarn init ",
        "bunx create-",
        "npx create-",
    )
    non_interactive_markers: tuple[str, ...] = (
        "--yes",
        " -y",
        "--skip-install",
        "--defaults",
        "--non-interactive",
        "--ci",
    )
    return any(marker in lowered_command for marker in scaffold_markers) and not any(
        marker in lowered_command for marker in non_interactive_markers
    )


def _looks_like_prompt(output: str) -> bool:
    if not output:
        return False
    prompt_markers: Iterable[str] = (
        "would you like",
        "ok to proceed",
        "select an option",
        "which",
        "press enter to continue",
        "?",
    )
    lowered_output = output.lower()
    return any(marker in lowered_output for marker in prompt_markers)
