"""Shell command execution tool."""

from __future__ import annotations
from researchx.workspace.session_files import FileManifest
from typing import TYPE_CHECKING
from researchx.engine.metadata import ExecutionLease

if TYPE_CHECKING:
    from researchx.state.runtime import ResearchAgentRuntime

import asyncio
import os
import json
from uuid import uuid4
from typing import Iterable

from pydantic import BaseModel, Field

from researchx.sandbox import SandboxUnavailableError
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult
from researchx.services.execution.shell import create_shell_subprocess, terminate_shell_process
from researchx.sandbox.policy import (
    ExecutionOwner,
    report_settings,
    report_environment,
    agent_shell_settings,
)


_READ_REMAINING_OUTPUT_TIMEOUT_SECONDS = 2.0


class BashToolInput(BaseModel):
    """Arguments for the bash tool."""

    command: str = Field(description="要执行的 Shell 命令")
    cwd: str | None = Field(default=None, description="覆盖默认工作目录")
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
    description = "在研究工作区执行 Shell 命令。"
    input_model = BashToolInput

    async def execute(self, arguments: BashToolInput, context: ToolExecutionContext) -> ToolResult:
        from researchx.state.errors import ResearchError

        try:
            cwd = await context.resolve_path(arguments.cwd)
        except (ResearchError, OSError, ValueError) as exc:
            return ToolResult(
                output=str(exc), is_error=True, no_effect=True, error_code="workspace_boundary"
            )
        preflight_error = _preflight_interactive_command(arguments.command)
        if preflight_error is not None:
            return ToolResult(
                output=preflight_error,
                is_error=True,
                metadata={"interactive_required": True},
            )
        runtime = await context.workspace_runtime()
        settings = context.settings
        owner = None
        execution = context.metadata.get("research_execution")
        if runtime:
            from researchx.config import Settings
            from researchx.state.errors import ResearchError

            project = (await runtime.store.load()).project
            assert project is not None
            workspace = await runtime.resolve_workspace(project.id)
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
            from researchx.config import Settings

            settings = settings or Settings()
            trusted_host = (
                context.capabilities.allow_trusted_host
                and settings.sandbox.allow_trusted_host
                and not settings.sandbox.enabled
            )
            if not trusted_host:
                if not cwd.is_relative_to(context.cwd.resolve()):
                    return ToolResult(
                        output="Sandbox cwd must remain within the approved workspace",
                        is_error=True,
                        no_effect=True,
                        error_code="workspace_boundary",
                    )
                try:
                    settings = agent_shell_settings(settings, cwd)
                except (SandboxUnavailableError, OSError) as exc:
                    return ToolResult(
                        output=str(exc),
                        is_error=True,
                        metadata={"no_effect": True, "safety_level": "sandbox_required"},
                    )
                owner = ExecutionOwner(context.runtime_id or "direct", "general", uuid4().hex, cwd)
                env = report_environment(cwd)
            else:
                env = dict(os.environ)
            for key in (
                "RESEARCHX_RESEARCH_SESSION_DIR",
                "RESEARCHX_RESEARCH_TASK_ID",
                "RESEARCHX_RESEARCH_EXECUTION",
            ):
                env.pop(key, None)
            store = context.metadata.get("research_store")
            if store is not None:
                env["RESEARCHX_RESEARCH_SESSION_DIR"] = str(store.directory)
                task_id = (await store.load()).research_state.current_task_id
                if task_id:
                    env["RESEARCHX_RESEARCH_TASK_ID"] = task_id
        legacy_store = context.metadata.get("research_store") if runtime is None else None
        legacy_baseline = None
        if legacy_store is not None:
            memory = await legacy_store.load()
            if owner is not None and settings is not None:
                settings.sandbox.filesystem.deny_read.append(str(legacy_store.directory.resolve()))
                settings.sandbox.filesystem.deny_write.append(str(legacy_store.directory.resolve()))
            legacy_baseline = (
                memory.current_context_id,
                memory.research_state.current_plan_id,
                memory.research_state.current_task_id,
            )
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
                    await asyncio.wait_for(
                        asyncio.shield(reader), _READ_REMAINING_OUTPUT_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    await terminate_shell_process(process, force=True)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if getattr(process, "pid", None) is not None:
                await terminate_shell_process(process, force=True)
            if owner and settings and settings.sandbox.backend == "docker":
                from researchx.sandbox.session import stop_docker_sandbox

                await asyncio.shield((stop_docker_sandbox(owner.key)))
            metadata: dict[str, object] = {
                "returncode": process.returncode,
                "safety_level": "sandbox" if owner else "trusted_host",
            }
            if runtime and execution and process.returncode == 0:
                try:
                    metadata["exported_files"] = await _register_exports(
                        output_buffer, context, runtime, execution
                    )
                except (ResearchError, ValueError, OSError) as exc:
                    return ToolResult(output=f"Export registration rejected: {exc}", is_error=True)
            if legacy_store is not None and process.returncode == 0:
                try:
                    metadata["exported_files"] = await _register_legacy_exports(
                        output_buffer, context, legacy_store, legacy_baseline
                    )
                except (ValueError, OSError) as exc:
                    return ToolResult(output=f"Export registration rejected: {exc}", is_error=True)
            return ToolResult(
                output=_format_output(output_buffer),
                is_error=process.returncode != 0,
                metadata=metadata,
            )
        except SandboxUnavailableError as exc:
            return ToolResult(
                output=str(exc),
                is_error=True,
                metadata={
                    "no_effect": True,
                    "error_code": "sandbox_unavailable",
                    "safety_level": "sandbox_required",
                },
            )
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
            if process is not None:
                if process.returncode is None:
                    await terminate_shell_process(process, force=True)
                transport = getattr(process, "_transport", None)
                if transport is not None:
                    transport.close()
                    await asyncio.sleep(0)
            if owner and settings and settings.sandbox.backend == "docker":
                from researchx.sandbox.session import stop_docker_sandbox

                await asyncio.shield((stop_docker_sandbox(owner.key)))


async def _terminate_process(process: asyncio.subprocess.Process, *, force: bool) -> None:
    await terminate_shell_process(process, force=force)


async def _register_exports(
    output: bytearray,
    context: ToolExecutionContext,
    runtime: ResearchAgentRuntime,
    execution: ExecutionLease,
) -> list[FileManifest]:
    """Import declarations on the host, only while the execution lease remains valid."""
    from researchx.state.errors import ResearchError
    from researchx.workspace.session_files import SessionFiles

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
                path = await context.resolve_path(value)
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
    async with runtime.store.transaction():
        memory = await runtime.store._load()
        active = memory.executions.get(execution["id"])
        if active is None or not runtime.repository.execution_valid(memory, active):
            raise ResearchError("Export execution was revoked")
        return [
            (
                await SessionFiles(runtime.store).register(
                    path, task_id=active.task_id, status=status, kind="export", execution=execution
                )
            )
            for path, status in declarations
        ]


async def _read_remaining_output(process: asyncio.subprocess.Process) -> bytearray:
    output_buffer = bytearray()
    if process.stdout is not None:
        try:
            remaining = await asyncio.wait_for(
                (process.stdout.read()),
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
            chunk = await asyncio.wait_for((stream.read(65536)), timeout=read_timeout)
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


async def _register_legacy_exports(
    output: bytearray, context: ToolExecutionContext, store: object, baseline: object
) -> list[FileManifest]:
    """Legacy scripts declare files; only the host imports them, never sandbox state writes."""
    from researchx.state.store import ResearchStore
    from researchx.workspace.session_files import SessionFiles
    from researchx.config.paths import get_config_dir, get_data_dir, get_logs_dir

    assert isinstance(store, ResearchStore)
    exports = []
    private = [
        path.resolve()
        for path in (get_config_dir(), get_data_dir(), get_logs_dir(), store.directory)
    ]
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            packet = json.loads(line)
        except ValueError:
            continue
        if not isinstance(packet, dict) or not isinstance(packet.get("files"), list):
            continue
        for value in packet["files"]:
            if not isinstance(value, str):
                raise ValueError("Export paths must be strings")
            path = await context.resolve_path(value)
            if not path.is_relative_to(context.cwd.resolve()) or any(
                path.is_relative_to(root) for root in private
            ):
                raise ValueError("Export must be a non-private file within the current workspace")
            if (
                not path.is_file()
                or path.stat().st_size > 30 * 1024 * 1024
                or path.stat().st_nlink != 1
            ):
                raise ValueError("Export file is missing, too large or hardlinked")
            query = context.metadata.get("query_context")
            if (
                query
                and not query.permission_checker.evaluate(
                    "read_file", is_read_only=True, file_path=str(path)
                ).allowed
            ):
                raise ValueError("Export path is denied by the read policy")
            exports.append((path, str(packet.get("status", "candidate"))))
            if len(exports) > 50:
                raise ValueError("Too many exported files")
    if not exports:
        return []
    async with store.transaction():
        memory = await store._load()
        state = memory.research_state
        if (
            memory.current_context_id,
            state.current_plan_id,
            state.current_task_id,
        ) != baseline or state.replan_required:
            raise ValueError("Legacy research scope changed; exports were not imported")
        return [
            (
                await SessionFiles(store).register(
                    path, task_id=state.current_task_id, status=status, kind=path.suffix[1:]
                )
            )
            for path, status in exports
        ]
