"""Bounded child invocations of the original loop, with no second Runtime."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, TYPE_CHECKING
from openharness.engine.metadata import ExecutionMetadata

if TYPE_CHECKING:
    from openharness.engine.query import QueryContext

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiStreamEvent,
    SupportsStreamingMessages,
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.cost_tracker import CostTracker
from openharness.engine.messages import ConversationMessage
from openharness.engine.stream_events import ErrorEvent
from openharness.research.errors import ResearchError
from openharness.tools.base import ToolRegistry
from openharness.utils.fs import atomic_write_text


class BoundedSubagentClient:
    """Count every child call, including compaction/hooks, using the parent transport."""

    accounts_usage = True

    def __init__(
        self,
        client: SupportsStreamingMessages,
        max_calls: int,
        tracker: CostTracker,
        account: Callable[[UsageSnapshot], None] | None,
        *,
        label: str = "Subagent",
    ) -> None:
        self.client, self.max_calls, self.tracker, self.account = (
            client,
            max_calls,
            tracker,
            account,
        )
        self.calls, self.exhausted, self.label = 0, False, label

    def prepare_request(self, request: ApiMessageRequest) -> ApiMessageRequest:
        from openharness.services.context_budget import prepare_request

        return prepare_request(self.client, request)

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        if self.calls >= self.max_calls:
            self.exhausted = True
            raise ResearchError(f"{self.label} model call budget exhausted")
        self.calls += 1
        async for event in self.client.stream_message(request):
            if isinstance(event, ApiMessageCompleteEvent):
                self.tracker.add(event.usage)
                if self.account:
                    self.account(event.usage)
            yield event


@dataclass
class SubagentRun:
    messages: list[ConversationMessage]
    usage: dict[str, object]


async def execute_subagent(
    parent: QueryContext,
    *,
    registry: ToolRegistry,
    cwd: Path,
    prompt: str,
    messages: list[ConversationMessage],
    metadata: ExecutionMetadata,
    max_calls: int,
    timeout: float,
    transcript_path: Path,
    tracker: CostTracker | None = None,
    account: Callable[[UsageSnapshot], None] | None = None,
    runtime_context_provider: Callable[[], str] | None = None,
    stop_when: Callable[[], bool] | None = None,
    label: str = "Subagent",
) -> SubagentRun:
    """Run one isolated finite child, propagate cancellation and always preserve its transcript."""
    from openharness.engine.query import MaxTurnsExceeded, QueryContext, run_query

    tracker = tracker or CostTracker()
    client = BoundedSubagentClient(parent.api_client, max_calls, tracker, account, label=label)
    hooks = (
        parent.hook_executor.with_api_client(
            client, parent.model, context_window_tokens=parent.context_window_tokens
        )
        if parent.hook_executor
        else None
    )
    child = QueryContext(
        capabilities=parent.capabilities,
        execution_session_id=parent.execution_session_id,
        api_client=client,
        tool_registry=registry,
        permission_checker=parent.permission_checker,
        cwd=cwd,
        model=parent.model,
        system_prompt=prompt,
        max_tokens=parent.max_tokens,
        effort=parent.effort,
        context_window_tokens=parent.context_window_tokens,
        auto_compact_threshold_tokens=parent.auto_compact_threshold_tokens,
        permission_prompt=parent.permission_prompt,
        hook_executor=hooks,
        max_turns=max_calls,
        tool_metadata=metadata,
        runtime_context_provider=runtime_context_provider,
        context_components=parent.context_components,
        research_memory_enabled=parent.research_memory_enabled,
    )

    async def run() -> None:
        stream = run_query(child, messages)
        try:
            async for event, _ in stream:
                if isinstance(event, ErrorEvent):
                    if client.exhausted:
                        raise MaxTurnsExceeded(max_calls)
                    raise ResearchError(event.message)
                if stop_when and stop_when():
                    return
        finally:
            await stream.aclose()

    cancelled = False
    try:
        try:
            await asyncio.wait_for(run(), timeout=timeout)
        except Exception as exc:
            if client.exhausted and not isinstance(exc, asyncio.TimeoutError):
                raise MaxTurnsExceeded(max_calls) from exc
            raise
        return SubagentRun(messages=messages, usage=tracker.total.model_dump())
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # Store only public tool/text history; model replay reasoning is never persisted here.
        try:
            atomic_write_text(
                transcript_path,
                json.dumps(
                    [
                        message.model_dump(mode="json", exclude={"reasoning_content"})
                        for message in messages
                    ],
                    ensure_ascii=False,
                ),
                mode=0o600,
            )
        except OSError:
            if not cancelled:
                raise
