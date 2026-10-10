"""Frozen external-tool adapters and evaluation-only workspace boundaries."""

from __future__ import annotations

from typing import TypeVar, TYPE_CHECKING, cast
from pydantic import BaseModel
from researchx.tools.base import ToolExecutionContext
from researchx.tools.bash_tool import BashToolInput
from researchx.tools.glob_tool import GlobToolInput
from researchx.tools.grep_tool import GrepToolInput
from researchx.tools.web_fetch_tool import WebFetchToolInput
from researchx.evaluation.models import SourceAsset, Fault, EvalCase
import asyncio
import ast
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from researchx.tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from researchx.runtime import RuntimeBundle
InputT = TypeVar("InputT", bound=BaseModel)


def denied(message: str) -> ToolResult:
    # 评测边界拒绝：未执行任何副作用，必须声明 no_effect=True。
    # 否则 bash 等 effect=unknown 的契约会被 tool_execution 记为 uncertain 副作用操作，
    # 其 write=["*"] 资源会与该会话后续所有操作冲突，导致整个任务被锁死。
    return ToolResult(
        output=message,
        is_error=True,
        no_effect=True,
        metadata={"research_source_specs": [], "outcome": "evaluation_boundary", "no_effect": True},
    )


class FrozenExternalTool(BaseTool[InputT]):
    def __init__(
        self, original: BaseTool[InputT], assets: list[SourceAsset], workspace: Path
    ) -> None:
        self.name, self.description, self.input_model = (
            original.name,
            original.description,
            original.input_model,
        )
        self.assets, self.workspace = assets, workspace

    def is_read_only(self, arguments: InputT) -> bool:
        return True

    async def execute(self, arguments: InputT, context: ToolExecutionContext) -> ToolResult:
        assets = [
            a for a in self.assets if (self.workspace / "materials" / Path(a.path).name).is_file()
        ]
        if self.name == "web_search":
            limit = getattr(arguments, "max_results", 5)
            rows = [
                {"title": a.title, "url": a.locator, "published_at": a.published_at}
                for a in assets[:limit]
            ]
            return ToolResult(
                output=json.dumps({"results": rows}, ensure_ascii=False),
                metadata={"research_source_specs": []},
            )
        if self.name in {"list_mcp_resources", "tool_search"}:
            return ToolResult(
                output="固定任务的资料目录：" + "\n".join(a.locator for a in assets),
                metadata={"research_source_specs": []},
            )
        locator = getattr(arguments, "url", None) or getattr(arguments, "uri", None)
        matches = [a for a in assets if a.locator == locator]
        if not matches:
            return denied("固定环境只允许读取本任务已登记的资料；请选择附件或冻结资料目录。")
        asset = matches[0]
        path = self.workspace / "materials" / Path(asset.path).name
        if path.suffix.lower() == ".pdf":
            from researchx.workspace.documents import document_text, parse_document

            text = document_text(parse_document(path))
        else:
            text = path.read_text(encoding="utf-8")
        if self.name == "web_fetch":
            text = text[: WebFetchToolInput.model_validate(arguments.model_dump()).max_chars]
        return ToolResult(
            output=text,
            metadata={
                "research_source_specs": [
                    {
                        "content": text,
                        "title": asset.title,
                        "locator": asset.locator,
                        "published_at": asset.published_at,
                        "kind": "web",
                        "fragment": True,
                    }
                ]
            },
        )


class WorkspaceTool(BaseTool[InputT]):
    """Limit data reads to staged inputs and skill resources; never expose gold."""

    def __init__(
        self, original: BaseTool[InputT], workspace: Path, resource_roots: list[Path]
    ) -> None:
        self.original = original
        self.name, self.description, self.input_model = (
            original.name,
            original.description,
            original.input_model,
        )
        self.workspace = workspace.resolve()
        self.resource_roots = [Path(p).resolve() for p in resource_roots]

    def is_read_only(self, arguments: InputT) -> bool:
        return self.original.is_read_only(arguments)

    def allowed(self, value: str | Path, *, write: bool = False) -> bool:
        path = Path(value).expanduser()
        path = (self.workspace / path).resolve() if not path.is_absolute() else path.resolve()
        if write and (
            path.is_relative_to(self.workspace / "materials")
            or path.is_relative_to(self.workspace / ".researchx")
            or path.is_relative_to(self.workspace / "session-state")
            and (
                any(
                    p in {"sources", "content"}
                    for p in path.relative_to(self.workspace / "session-state").parts
                )
                or path.name in {"memory.json", "state.json"}
            )
        ):
            return False
        return path.is_relative_to(self.workspace) or (
            not write and any(path.is_relative_to(root) for root in self.resource_roots)
        )

    async def execute(self, arguments: InputT, context: ToolExecutionContext) -> ToolResult:
        if self.name == "glob":
            from researchx.tools.glob_tool import _resolve_glob_request

            root, _ = _resolve_glob_request(
                self.workspace,
                GlobToolInput.model_validate(arguments.model_dump()).root,
                GlobToolInput.model_validate(arguments.model_dump()).pattern,
            )
            if not self.allowed(str(root)):
                return denied("评测检索范围不能越过任务工作区与技能资源。")
        if self.name == "grep" and (
            Path(GrepToolInput.model_validate(arguments.model_dump()).file_glob).is_absolute()
            or ".." in Path(GrepToolInput.model_validate(arguments.model_dump()).file_glob).parts
        ):
            return denied("评测检索范围不能越过任务工作区与技能资源。")
        for field in ("path", "file_path", "notebook_path", "cwd", "root"):
            value = getattr(arguments, field, None)
            if value and not self.allowed(
                value, write=self.name in {"write_file", "edit_file", "notebook_edit"}
            ):
                return denied("评测工具只能访问任务工作区与已启用技能资源。")
        return await self.original.execute(arguments, context)


class RestrictedPythonTool(WorkspaceTool[BashToolInput]):
    """No shell is launched in fixed evaluations: only validated deterministic Python."""

    def __init__(
        self,
        original: BaseTool[BashToolInput],
        workspace: Path,
        resource_roots: list[Path],
        *,
        allow_network: bool = False,
    ) -> None:
        super().__init__(original, workspace, resource_roots)
        self.allow_network = allow_network

    def _inline_allowed(self, code: str) -> bool:
        tree = ast.parse(code)
        safe_modules = {
            "json",
            "decimal",
            "math",
            "statistics",
            "pathlib",
            "csv",
            "datetime",
            "re",
            "pypdf",
            "docx",
            "openpyxl",
        }
        if self.allow_network:
            safe_modules.update({"httpx", "requests", "urllib"})
        forbidden = {
            "eval",
            "exec",
            "compile",
            "__import__",
            "getattr",
            "setattr",
            "globals",
            "locals",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                n.name.split(".")[0] not in safe_modules for n in node.names
            ):
                return False
            if isinstance(node, ast.ImportFrom) and (
                node.level or (node.module or "").split(".")[0] not in safe_modules
            ):
                return False
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                return False
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden
            ):
                return False
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("/") and not self.allowed(node.value):
                    return False
        return True

    async def execute(self, arguments: BashToolInput, context: ToolExecutionContext) -> ToolResult:
        try:
            tokens = shlex.split(arguments.command)
        except ValueError:
            return denied("请使用单个 Python 调用，不使用 shell 管道或重定向。")
        # Convert a simple heredoc into a Python -c invocation without invoking a shell.
        if "<<" in arguments.command:
            import re

            match = re.fullmatch(
                r"\s*(\S+)\s+-\s*<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\2\s*", arguments.command, re.S
            )
            if match:
                tokens = [match[1], "-c", match[3]]
        if not tokens or Path(tokens[0]).name not in {
            "python",
            "python3",
            "python3.10",
            "python3.11",
            "python3.12",
            "python3.13",
            "python3.14",
        }:
            return denied(
                "固定评测仅允许确定性 Python 计算和技能脚本，不执行任意 shell 或联网命令。"
            )
        args = tokens[1:]
        try:
            cwd = Path(arguments.cwd).resolve() if arguments.cwd else self.workspace
            if not cwd.is_relative_to(self.workspace):
                return denied("脚本工作目录不属于任务。")
            if args[:1] == ["-c"]:
                if len(args) != 2 or not (self._inline_allowed(args[1])):
                    return denied("Python 代码超出固定评测允许的确定性计算范围。")
            elif args[:1] == ["-m"]:
                if len(args) < 2 or args[1] != "researchx.workspace.documents":
                    return denied("只允许内置本地文档解析模块。")
            else:
                script = (
                    (cwd / args[0]).resolve()
                    if args and not Path(args[0]).is_absolute()
                    else (Path(args[0]).resolve() if args else None)
                )
                generated = (
                    script is not None
                    and script.is_relative_to(self.workspace)
                    and script.suffix == ".py"
                    and script.is_file()
                    and (self._inline_allowed(script.read_text()))
                )
                if (
                    script is None
                    or not args
                    or not self.allowed(args[0])
                    or not (
                        generated
                        or any(
                            script.is_relative_to(root / "scripts") for root in self.resource_roots
                        )
                    )
                ):
                    return denied("只允许工作区内的确定性计算脚本与已启用技能脚本。")
            for value in args[2:] if args[:1] in [["-m"], ["-c"]] else args[1:]:
                if ("://" in value and not self.allow_network) or (
                    value.startswith("/") and not self.allowed(value)
                ):
                    return denied("脚本只能读写任务工作区，不能联网或读取评测金标。")
            from researchx.evaluation.python_guard import bootstrap

            code = bootstrap(
                self.workspace, self.resource_roots, args, allow_network=self.allow_network
            )
            if (await context.workspace_runtime()) is not None:
                # Report runs must use the real owned OS sandbox, including fixed evaluations.
                return await self.original.execute(arguments, context)
            # Legacy evaluations retain the deterministic Python audit guard.
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={
                    "PATH": str(Path(sys.executable).parent),
                    "RESEARCHX_RESEARCH_SESSION_DIR": str(
                        context.metadata["research_store"].directory
                        if context.metadata.get("research_store")
                        else self.workspace / "session-state"
                    ),
                    "RESEARCHX_RESEARCH_TASK_ID": str(
                        (
                            (
                                await context.metadata["research_store"].load()
                            ).research_state.current_task_id
                            or ""
                        )
                        if context.metadata.get("research_store")
                        else ""
                    ),
                },
            )
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(), timeout=arguments.timeout_seconds
                )
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            return ToolResult(
                output=output.decode("utf-8", errors="replace"),
                is_error=process.returncode != 0,
                metadata={
                    "returncode": process.returncode,
                    "executed_script": args[0] if args[0] not in {"-c", "-m"} else None,
                },
            )
        except (ValueError, SyntaxError):
            return denied("Python 调用格式无效。")


class FaultTool(BaseTool[InputT]):
    def __init__(self, original: BaseTool[InputT], faults: list[Fault]) -> None:
        self.original, self.faults = original, faults
        self.name, self.description, self.input_model = (
            original.name,
            original.description,
            original.input_model,
        )
        self.calls = 0

    def is_read_only(self, arguments: InputT) -> bool:
        return self.original.is_read_only(arguments)

    async def execute(self, arguments: InputT, context: ToolExecutionContext) -> ToolResult:
        self.calls += 1
        fault = next((f for f in self.faults if f.occurrence == self.calls), None)
        if fault and fault.kind == "investigation_timeout":
            context.metadata["conflict_timeout_seconds"] = 0.001
            result = await self.original.execute(arguments, context)
            return replace(result, metadata={**result.metadata, "injected_fault": fault.kind})
        if fault:
            return ToolResult(
                output="评测注入的暂时超时"
                if fault.kind == "timeout"
                else "评测注入的可恢复工具错误",
                is_error=True,
                metadata={"research_source_specs": [], "injected_fault": fault.kind},
            )
        return await self.original.execute(arguments, context)


def configure_tools(
    bundle: RuntimeBundle, case: EvalCase, workspace: Path, resource_roots: list[Path]
) -> None:
    registry = bundle.tool_registry
    if case.environment == "fixed":
        from researchx.mcp.types import McpToolInfo
        from researchx.tools.mcp_tool import McpToolAdapter

        class ReplayManager:
            async def call_tool(self, server: str, name: str, arguments: dict[str, object]) -> str:
                from researchx.mcp.client import McpToolReturnedError

                selected = next((a for a in case.assets if a.locator == arguments.get("uri")), None)
                if selected is None:
                    raise McpToolReturnedError("固定MCP未登记此资料")
                path = workspace / "materials" / Path(selected.path).name
                if path.suffix == ".pdf":
                    from researchx.workspace.documents import document_text, parse_document

                    return document_text(parse_document(path))
                return path.read_text(encoding="utf-8")

        registry.register(
            McpToolAdapter(
                ReplayManager(),
                McpToolInfo(
                    server_name="fixture",
                    name="get_company_data",
                    description="读取本任务冻结资料；uri取自本轮提供的原始定位。",
                    input_schema={
                        "type": "object",
                        "properties": {"uri": {"type": "string"}},
                        "required": ["uri"],
                    },
                ),
            )
        )
    for original in registry.list_tools():
        if original.name in {"config", "mcp_auth"}:
            registry.unregister(original.name)
            continue
        if (
            case.environment == "fixed"
            and not original.name.startswith("mcp__")
            and original.name
            not in {
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "bash",
                "notebook_read",
                "notebook_edit",
                "research_memory",
                "skill",
                "ask_user_question",
                "investigate_conflict",
                "web_search",
                "web_fetch",
                "read_mcp_resource",
                "list_mcp_resources",
                "tool_search",
            }
        ):
            registry.unregister(original.name)
            continue
        tool = original
        if original.name == "bash":
            tool = cast(
                BaseTool[BaseModel],
                RestrictedPythonTool(
                    cast(BaseTool[BashToolInput], original),
                    workspace,
                    resource_roots,
                    allow_network=case.environment == "live",
                ),
            )
        elif original.name in {
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "grep",
            "notebook_edit",
            "notebook_read",
        }:
            tool = WorkspaceTool(original, workspace, resource_roots)
        if case.environment == "fixed":
            if original.name in {
                "web_search",
                "web_fetch",
                "read_mcp_resource",
                "list_mcp_resources",
                "tool_search",
            }:
                tool = FrozenExternalTool(original, case.assets, workspace)
            elif (
                original.name.startswith("mcp__")
                and original.name != "mcp__fixture__get_company_data"
            ):
                tool = FrozenExternalTool(original, case.assets, workspace)
            elif original.name == "bash":
                tool = cast(
                    BaseTool[BaseModel],
                    RestrictedPythonTool(
                        cast(BaseTool[BashToolInput], original), workspace, resource_roots
                    ),
                )
            elif original.name in {"image_generate", "image_to_text"}:
                registry.unregister(original.name)
                continue
            elif original.name in {
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "notebook_edit",
                "notebook_read",
            }:
                tool = WorkspaceTool(original, workspace, resource_roots)
        faults = [f for f in case.faults if f.tool == original.name]
        if faults:
            tool = FaultTool(tool, faults)
        if tool is not original:
            # Trusted evaluation adapters are an explicit host replacement, never a plugin override.
            registry.unregister(original.name)
            registry.register(tool)
