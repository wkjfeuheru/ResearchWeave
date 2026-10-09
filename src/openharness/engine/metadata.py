"""Typed host-owned execution context. Persisted JSON contains only a safe subset."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, Callable, Awaitable
import asyncio
from pathlib import Path

if TYPE_CHECKING:
    from openharness.api.usage import UsageSnapshot
    from openharness.config import Settings
    from openharness.engine.query import QueryContext
    from openharness.engine.observer import Observer
    from openharness.research.store import ResearchStore
    from openharness.research.runtime import ResearchAgentRuntime
    from openharness.mcp.client import McpClientManager
    from openharness.tools.base import ToolRegistry


class ExecutionLease(TypedDict):
    id: str
    task_id: str
    task_revision: int
    plan_revision: int
    objective_revision: int
    epoch: int
    lease_id: str


class ExecutionMetadata(TypedDict, total=False):
    research_store: ResearchStore
    research_runtime: ResearchAgentRuntime
    research_workspace_runtime: ResearchAgentRuntime
    research_workspace_root: str | Path | None
    research_injection_budget: int
    memory_auto_inject_max_chars: int
    conflict_max_turns: int
    conflict_timeout_seconds: float
    conflict_investigator: bool
    planning_child: bool
    planning_max_calls: int
    planning_timeout_seconds: float
    planning_token_budget: int
    planning_tokens: int
    planning_baseline: dict[str, int]
    research_execution: ExecutionLease
    subagent_child: bool
    subagent_baseline: tuple[int, int, int]
    subagent_parent_workspace: Path
    subagent_output_dir: Path
    subagent_max_calls: int
    subagent_max_concurrency: int
    subagent_timeout_seconds: float
    subagent_semaphore: asyncio.Semaphore
    query_context: QueryContext
    tool_registry: ToolRegistry
    trusted_settings: Settings
    runtime_id: str
    session_id: str
    mcp_manager: McpClientManager
    account_subagent_usage: Callable[[UsageSnapshot], None]
    ask_user_prompt: Callable[[str], Awaitable[str]] | None
    edit_approval_prompt: Callable[[str, str, int, int], Awaitable[str]] | None
    vision_model_config: dict[str, str]
    image_generation_config: dict[str, str]
    extra_skill_dirs: tuple[str, ...]
    extra_plugin_roots: tuple[str, ...]
    observer: Observer | None
    permission_mode: str
    session_approvals: dict[str, list[str]]
    invoked_skills: list[str]
    skill_settings: Settings
    selected_skill_roots: dict[str, str]
    context_budget: dict[str, object]
    latest_user_source_id: str
    compact_last: dict[str, object]
    compact_checkpoints: list[dict[str, object]]
