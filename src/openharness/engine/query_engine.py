"""High-level conversation engine."""

from __future__ import annotations

import re
from pathlib import Path

from openharness.api.usage import UsageSnapshot
from openharness.prompts.context import RuntimePrompt, _build_permission_mode_section
from openharness.permissions.modes import PermissionMode
from typing import AsyncIterator

from openharness.api.client import SupportsStreamingMessages
from openharness.engine.cost_tracker import CostTracker
from openharness.engine.messages import (
    ConversationMessage,
    ToolResultBlock,
    sanitize_conversation_messages,
    TextBlock,
)
from openharness.engine.query import AskUserPrompt, PermissionPrompt, QueryContext, run_query
from openharness.engine.stream_events import AssistantTurnComplete, StreamEvent
from openharness.config.settings import Settings
from openharness.hooks import HookEvent, HookExecutor
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import ToolRegistry


class QueryEngine:
    """Owns conversation history and the tool-aware model loop."""

    def __init__(
        self,
        *,
        api_client: SupportsStreamingMessages,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        cwd: str | Path,
        model: str,
        system_prompt: str | RuntimePrompt,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
        max_turns: int | None = 8,
        permission_prompt: PermissionPrompt | None = None,
        ask_user_prompt: AskUserPrompt | None = None,
        hook_executor: HookExecutor | None = None,
        tool_metadata: dict[str, object] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._api_client = api_client
        self._tool_registry = tool_registry
        self._permission_checker = permission_checker
        self._cwd = Path(cwd).resolve()
        self._model = model
        self._runtime_context: str | None = None
        self.set_system_prompt(system_prompt)
        self._max_tokens = max_tokens
        self._effort = settings.effort if settings is not None else None
        self._context_window_tokens = context_window_tokens
        self._auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self._max_turns = max_turns
        self._permission_prompt = permission_prompt
        self._ask_user_prompt = ask_user_prompt
        self._hook_executor = hook_executor
        self._tool_metadata = tool_metadata or {}
        self._settings = settings
        self._messages: list[ConversationMessage] = []
        self._cost_tracker = CostTracker()

    @property
    def messages(self) -> list[ConversationMessage]:
        """Return the current conversation history."""
        return list(self._messages)

    @property
    def max_turns(self) -> int | None:
        """Return the maximum number of agentic turns per user input, if capped."""
        return self._max_turns

    @property
    def api_client(self) -> SupportsStreamingMessages:
        """Return the active API client."""
        return self._api_client

    @property
    def model(self) -> str:
        """Return the active model identifier."""
        return self._model

    @property
    def system_prompt(self) -> str:
        """Return the active system prompt."""
        return self._system_prompt

    @property
    def runtime_context(self) -> str | None:
        """Return current hidden runtime state for diagnostics."""
        return self._current_runtime_context()

    @property
    def tool_metadata(self) -> dict[str, object]:
        """Return the mutable tool metadata/carry-over state."""
        return self._tool_metadata

    @property
    def total_usage(self):
        """Return the total usage across all turns."""
        return self._cost_tracker.total

    def clear(self) -> None:
        """Clear the in-memory conversation history."""
        self._messages.clear()
        self._cost_tracker = CostTracker()

    def set_system_prompt(self, prompt: str | RuntimePrompt) -> None:
        """Update the active system prompt for future turns."""
        if isinstance(prompt, RuntimePrompt):
            self._system_prompt = prompt.system_prompt
            self._runtime_context = prompt.runtime_context
        else:
            self._system_prompt = prompt

    def set_model(self, model: str) -> None:
        """Update the active model for future turns."""
        self._model = model

    def set_effort(self, effort: str | None) -> None:
        """Update the active reasoning effort for future turns."""
        self._effort = effort

    def set_api_client(self, api_client: SupportsStreamingMessages) -> None:
        """Update the active API client for future turns."""
        self._api_client = api_client

    def set_max_turns(self, max_turns: int | None) -> None:
        """Update the maximum number of agentic turns per user input."""
        self._max_turns = None if max_turns is None else max(1, int(max_turns))

    def set_permission_checker(self, checker: PermissionChecker) -> None:
        """Update the active permission checker for future turns."""
        self._permission_checker = checker

    def _current_runtime_context(self) -> str | None:
        """Refresh only mutable loop state; query-specific memory stays snapshotted."""
        text = self._runtime_context or ""
        # Restored or externally supplied contexts must not retain an old recall alongside the new one.
        text = re.sub(r"\n*<long_term_memory>.*?</long_term_memory>", "", text, flags=re.DOTALL)
        store = self._tool_metadata.get("research_store")
        if store is not None:
            text = re.sub(r"\n*<research_memory>.*?</research_memory>", "", text, flags=re.S)
            text += "\n\n" + store.prompt(
                int(self._tool_metadata.get("research_injection_budget", 6000))
            )
        mode = self._tool_metadata.get("permission_mode")
        if mode and self._settings is not None:
            settings = self._settings.model_copy(deep=True)
            settings.permission.mode = PermissionMode(mode)
            section = (
                "<permission_mode>\n"
                + _build_permission_mode_section(settings)
                + "\n</permission_mode>"
            )
            text = re.sub(
                r"<permission_mode>.*?</permission_mode>", lambda _: section, text, flags=re.S
            )
        # Coordinator state must never be moved ahead of old assistant/tool turns.
        text = re.sub(r"\n*<coordinator_context>.*?</coordinator_context>", "", text, flags=re.S)
        return text or None

    def restore_usage(self, usage: dict | UsageSnapshot | None) -> None:
        self._cost_tracker = CostTracker(UsageSnapshot.model_validate(usage or {}))

    def load_messages(
        self, messages: list[ConversationMessage], *, preserve_runtime_context: bool = False
    ) -> None:
        """Replace the in-memory conversation history."""
        previous = self._current_runtime_context() if preserve_runtime_context else None
        self._messages = list(messages)
        latest = next((m.runtime_context for m in reversed(messages) if m.runtime_context), None)
        if previous and latest != previous:
            self._messages.append(ConversationMessage(role="user", runtime_context=previous))
        if self._runtime_context is None:
            self._runtime_context = next(
                (m.runtime_context for m in reversed(messages) if m.runtime_context), None
            )

    def has_pending_continuation(self) -> bool:
        """Return True when the conversation ends with tool results awaiting a follow-up model turn."""
        # A state update may follow tool results when cancellation occurs just
        # before the next model response. Hidden context is not a new user turn.
        messages = [
            message for message in self._messages if message.content or not message.runtime_context
        ]
        if not messages:
            return False
        last = messages[-1]
        if last.role != "user":
            return False
        if not any(isinstance(block, ToolResultBlock) for block in last.content):
            return False
        for msg in reversed(messages[:-1]):
            if msg.role != "assistant":
                continue
            return bool(msg.tool_uses)
        return False

    async def submit_message(self, prompt: str | ConversationMessage) -> AsyncIterator[StreamEvent]:
        """Append a user message and execute the query loop."""
        user_message = (
            prompt
            if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        store = self._tool_metadata.get("research_store")
        if store is not None and user_message.text.strip():
            from openharness.research.models import new_id

            origin = user_message.message_id or new_id("msg")
            user_message = user_message.model_copy(update={"message_id": origin})
            source = store.capture(
                origin_id=origin,
                content=user_message.text,
                kind="user",
                title="用户提供资料",
                locator=f"message:{origin}",
            )
            self._tool_metadata["latest_user_source_id"] = source.id
        # Retain the submitted prompt even if its recall is cancelled before the model starts.
        self._messages = sanitize_conversation_messages(self._messages)
        user_message = user_message.model_copy(
            update={"runtime_context": self._current_runtime_context()}
        )
        self._messages.append(user_message)
        if self._hook_executor is not None:
            await self._hook_executor.execute(
                HookEvent.USER_PROMPT_SUBMIT,
                {
                    "event": HookEvent.USER_PROMPT_SUBMIT.value,
                    "prompt": user_message.text,
                },
            )
        context = QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            effort=self._effort,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=self._max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
            runtime_context_provider=self._current_runtime_context,
        )
        query_messages = list(self._messages)
        try:
            async for event, usage in run_query(context, query_messages):
                if isinstance(event, AssistantTurnComplete):
                    if store is not None and not event.message.tool_uses:
                        from openharness.research.models import new_id

                        warning = store.completion_warning()
                        text = event.message.text + (f"\n\n{warning}" if warning else "")
                        rendered, answer = store.render_answer(text, new_id("answer"))
                        updated = event.message.model_copy(
                            update={
                                "content": [TextBlock(text=rendered)],
                                "research_citations": answer,
                            }
                        )
                        query_messages[-1] = updated
                        event = AssistantTurnComplete(message=updated, usage=event.usage)
                    self._messages = list(query_messages)
                if usage is not None:
                    self._cost_tracker.add(usage)
                yield event
        finally:
            if store is not None:
                self._complete_interrupted_research_tools(query_messages, store)
            self._messages = list(query_messages)

    @staticmethod
    def _complete_interrupted_research_tools(messages, store) -> None:
        """Preserve pending tool calls with explicit results across a cancellation boundary."""
        if not messages or not messages[-1].tool_uses:
            return
        memory = store.load()
        results = []
        for call in messages[-1].tool_uses:
            sources = [source for source in memory.sources.values() if source.origin_id == call.id]
            if sources:
                source_ids = [source.id for source in sources]
                results.append(
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content=f"Execution ended before its result was attached. Retained research sources: {source_ids}; read them before continuing.",
                        is_error=all(source.is_error for source in sources),
                        result_metadata={"research_sources": source_ids},
                    )
                )
            else:
                results.append(
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content="Execution interrupted. Inspect current research state before retrying; an operation may already have committed.",
                        is_error=True,
                    )
                )
        messages.append(ConversationMessage(role="user", content=results))

    async def continue_pending(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        """Continue an interrupted tool loop without appending a new user message."""
        self._messages = sanitize_conversation_messages(self._messages)
        context = QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            effort=self._effort,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
            runtime_context_provider=self._current_runtime_context,
        )
        async for event, usage in run_query(context, self._messages):
            if usage is not None:
                self._cost_tracker.add(usage)
            yield event
