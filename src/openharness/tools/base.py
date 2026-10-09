"""Tool abstractions."""

from __future__ import annotations

from typing import Protocol, Iterable, Iterator
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast, Generic, TypeVar
from typing import TYPE_CHECKING
from pydantic import BaseModel
from openharness.permissions.capabilities import CapabilityContext
from openharness.engine.metadata import ExecutionMetadata


class CwdContext(Protocol):
    cwd: Path


if TYPE_CHECKING:
    from openharness.config import Settings
    from openharness.hooks.executor import HookExecutor
    from openharness.research.runtime import ResearchAgentRuntime


@dataclass
class ToolExecutionContext:
    """Shared execution context for tool invocations."""

    cwd: Path
    metadata: ExecutionMetadata = field(default_factory=ExecutionMetadata)
    hook_executor: HookExecutor | None = None
    settings: Settings | None = None
    runtime_id: str | None = None
    capabilities: CapabilityContext = field(default_factory=CapabilityContext)
    operation_id: str | None = None
    idempotency_key: str | None = None

    @classmethod
    def from_context(cls, context: CwdContext) -> ToolExecutionContext:
        """Keep compatibility with callers supplying the original cwd-only context protocol."""
        return (
            context
            if isinstance(context, cls)
            else cls(
                cwd=context.cwd,
                metadata=cast(ExecutionMetadata, getattr(context, "metadata", {})),
                hook_executor=getattr(context, "hook_executor", None),
            )
        )

    def workspace_runtime(self) -> ResearchAgentRuntime | None:
        runtime = self.metadata.get("research_workspace_runtime") or self.metadata.get(
            "research_runtime"
        )
        store = self.metadata.get("research_store")
        if runtime is None and store is not None and store.load().project is not None:
            from openharness.research.runtime import ResearchAgentRuntime

            runtime = ResearchAgentRuntime(
                store,
                workspace_root=self.metadata.get("research_workspace_root"),
                memory_auto_inject_max_chars=self.metadata.get(
                    "memory_auto_inject_max_chars", 12000
                ),
            )
            self.metadata["research_runtime"] = runtime
        return runtime if runtime and runtime.store.load().project is not None else None

    def resolve_path(self, candidate: str | Path | None = None, *, write: bool = False) -> Path:
        runtime = self.workspace_runtime()
        output_dir = self.metadata.get("subagent_output_dir")
        if output_dir:
            from openharness.research.errors import ResearchError
            from openharness.research.runtime import ResearchAgentRuntime

            root = Path(self.metadata["subagent_parent_workspace"])
            path = Path(candidate or ".").expanduser()
            path = path if path.is_absolute() else self.cwd / path
            path = ResearchAgentRuntime._check_path(root, path)
            own = Path(output_dir)
            if write and not path.is_relative_to(own):
                raise ResearchError(
                    "Subagent writes are confined to its own output directory; main MEMORY.md is read-only"
                )
            if path.is_relative_to(root / "subagents") and not path.is_relative_to(own):
                raise ResearchError("Other subagent output directories are not authorized")
            return path
        if runtime:
            return runtime.resolve_tool_path(
                runtime.repository._project(runtime.store.load()).id, candidate or "."
            )
        path = Path(candidate or ".").expanduser()
        return (path if path.is_absolute() else self.cwd / path).resolve()

    def validate_search_pattern(self, pattern: str) -> None:
        if self.workspace_runtime() and ".." in Path(pattern).parts:
            from openharness.research.errors import ResearchError

            raise ResearchError("Workspace search path traversal is forbidden")

    def safe_search_paths(self, paths: Iterable[Path]) -> Iterator[Path]:
        from openharness.research.errors import ResearchError

        runtime = self.workspace_runtime()
        root = (
            runtime.resolve_workspace(runtime.repository._project(runtime.store.load()).id)
            if runtime
            else None
        )
        for path in paths:
            try:
                if self.metadata.get("subagent_output_dir"):
                    self.resolve_path(path)
                elif runtime and root is not None:
                    runtime._check_path(root, path)
            except ResearchError:
                continue
            yield path

    @contextmanager
    def file_lock(self, candidate: str | Path, *, write: bool = False) -> Iterator[Path]:
        """Use the existing process lock for workspace read-modify-write operations."""
        runtime = self.workspace_runtime()
        if self.metadata.get("subagent_output_dir"):
            candidate = self.resolve_path(candidate, write=write)
        if runtime:
            project_id = runtime.repository._project(runtime.store.load()).id
            with runtime.workspace_file_lock(project_id, candidate) as path:
                if write:
                    from openharness.research.errors import ResearchError
                    from openharness.utils.file_lock import exclusive_file_lock

                    # The state lock orders a write against interruption/lease revocation.
                    with exclusive_file_lock(runtime.store.lock):
                        current = runtime.store._load()
                        project = runtime.repository._project(current)
                        execution = self.metadata.get("research_execution")
                        if project.status not in {"planning", "replanning", "running"}:
                            raise ResearchError(
                                "Workspace write requires an active research project"
                            )
                        if execution and (
                            current.executions[execution["id"]].status != "running"
                            or project.execution_epoch != execution["epoch"]
                        ):
                            raise ResearchError(
                                "Workspace write belongs to a revoked research execution"
                            )
                        baseline = self.metadata.get("subagent_baseline")
                        if (
                            baseline
                            and (
                                project.execution_epoch,
                                project.objective_revision,
                                project.plan_revision,
                            )
                            != baseline
                        ):
                            raise ResearchError(
                                "Subagent workspace write belongs to a revoked parent scope"
                            )
                        yield path
                else:
                    yield path
        else:
            yield self.resolve_path(candidate, write=write)


@dataclass(frozen=True)
class ToolResult:
    """Normalized tool execution result."""

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    error_code: str | None = None
    retryable: bool = False
    no_effect: bool | None = None


InputT = TypeVar("InputT", bound=BaseModel)


class BaseTool(ABC, Generic[InputT]):
    """Base class for all OpenHarness tools."""

    name: str
    description: str
    input_model: type[InputT]

    @abstractmethod
    async def execute(self, arguments: InputT, context: ToolExecutionContext) -> ToolResult:
        """Execute the tool."""

    def is_read_only(self, arguments: InputT) -> bool:
        """Return whether the invocation is read-only."""
        del arguments
        return False

    async def execute_with_idempotency_key(
        self, arguments: InputT, context: ToolExecutionContext, *, idempotency_key: str
    ) -> ToolResult:
        """Override only when the remote API honors this key; host invokes this path explicitly."""
        raise NotImplementedError("No remote idempotency-key adapter")

    async def reconcile_no_effect(self, arguments: InputT, context: ToolExecutionContext) -> bool:
        """Explicit external verification adapter; False means unknown, never assume no effect."""
        return False

    def validation_error_message(self, error: Exception) -> str:
        """Give the caller a recoverable tool-input error."""
        return f"Invalid input for {self.name}: {error}"

    def to_api_schema(self) -> dict[str, Any]:
        """Return the tool schema expected by the Anthropic Messages API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


class ToolRegistry:
    """Map tool names to implementations."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool[BaseModel]] = {}

    def register(self, tool: BaseTool[InputT], *, replace: bool = False) -> None:
        """Validate contracts and reject implicit or protected tool replacement."""
        from openharness.tools.contracts import resolve_contract

        from openharness.tools.retired import RETIRED_TOOL_NAMES

        if tool.name in RETIRED_TOOL_NAMES:
            raise ValueError(f"Retired tool cannot be registered: {tool.name}")
        resolve_contract(tool)
        if tool.name in self._tools:
            existing = self._tools[tool.name]
            protected = existing.__class__.__module__.startswith("openharness.tools.")
            if not replace or protected:
                raise ValueError(f"Duplicate/protected tool registration: {tool.name}")
        self._tools[tool.name] = cast(BaseTool[BaseModel], tool)

    def get(self, name: str) -> BaseTool[BaseModel] | None:
        """Return a registered tool by name."""
        return self._tools.get(name)

    def unregister(self, name: str) -> None:
        """Remove tools that do not belong to a runtime's product surface."""
        self._tools.pop(name, None)

    def list_tools(self) -> list[BaseTool[BaseModel]]:
        """Return all registered tools."""
        return list(self._tools.values())

    def to_api_schema(self) -> list[dict[str, Any]]:
        """Return all tool schemas in API format."""

        # JSON Schema's required list is a set; Pydantic emits it in property
        # discovery order, which can differ between remote MCP connections.
        def canonical(value: object) -> object:
            if isinstance(value, dict):
                return {
                    key: sorted(item)
                    if key == "required"
                    and isinstance(item, list)
                    and all(isinstance(entry, str) for entry in item)
                    else canonical(item)
                    for key, item in sorted(value.items())
                }
            if isinstance(value, list):
                return [canonical(item) for item in value]
            return value

        return [
            cast(dict[str, Any], canonical(self._tools[name].to_api_schema()))
            for name in sorted(self._tools)
        ]
