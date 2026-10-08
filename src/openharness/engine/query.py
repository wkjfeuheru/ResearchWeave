"""Core tool-aware query loop."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable
from uuid import uuid4

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiTextDeltaEvent,
    SupportsStreamingMessages,
)
from openharness.api.provider import is_model_multimodal
from openharness.api.usage import UsageSnapshot
from openharness.config.paths import get_data_dir
from openharness.engine.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
)
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    ResearchProgressEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.hooks import HookEvent, HookExecutor
from openharness.permissions.checker import PermissionChecker
from openharness.services.context_budget import ContextBudgetError, prepare_request, request_budget
from openharness.services.tool_outputs import tool_output_inline_chars, tool_output_preview_chars
from openharness.tools.base import ToolExecutionContext
from openharness.tools.base import ToolRegistry

AUTO_COMPACT_STATUS_MESSAGE = "Auto-compacting conversation memory to keep things fast and focused."
REACTIVE_COMPACT_STATUS_MESSAGE = "Prompt too long; compacting conversation memory and retrying."
MAX_SAFE_COMPLETION_TOKENS = 128_000

log = logging.getLogger(__name__)


PermissionPrompt = Callable[[str, str], Awaitable[bool]]
AskUserPrompt = Callable[[str], Awaitable[str]]


def _is_prompt_too_long_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "prompt too long",
            "context_length_exceeded",
            "context length",
            "maximum context",
            "context window",
            "input tokens exceed",
            "messages resulted in",
            "reduce the length of the messages",
            "configured limit",
            "too many tokens",
            "too large for the model",
            "maximum context length",
            "exceed_context",
            "exceeds the available context size",
            "available context size",
        )
    )


def _bounded_completion_tokens(max_tokens: int, context_window_tokens: int | None = None) -> int:
    """Return a conservative per-request output token cap.

    Some OpenAI-compatible providers reject very large ``max_tokens`` before
    the request reaches model-side context management.  Keep oversized user
    config from making every turn fail while preserving normal defaults.
    """
    limit = MAX_SAFE_COMPLETION_TOKENS
    return max(1, min(int(max_tokens), limit))


def _extract_completion_token_limit(exc: Exception) -> int | None:
    """Parse provider errors such as "supports at most 128000 completion tokens"."""
    text = str(exc).lower().replace(",", "")
    patterns = (
        r"supports at most\s+(\d+)\s+completion tokens",
        r"at most\s+(\d+)\s+completion tokens",
        r"max(?:imum)?(?:_completion)?[_\s-]tokens.*?(?:<=|less than or equal to|at most)\s+(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return max(1, int(match.group(1)))
            except ValueError:
                return None
    return None


def _is_completion_token_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("max_tokens" in text or "max_completion_tokens" in text) and (
        "too large" in text or "at most" in text or "completion tokens" in text
    )


class MaxTurnsExceeded(RuntimeError):
    """Raised when the agent exceeds the configured max_turns for one user prompt."""

    def __init__(self, max_turns: int) -> None:
        super().__init__(f"Exceeded maximum turn limit ({max_turns})")
        self.max_turns = max_turns


@dataclass
class QueryContext:
    """Context shared across a query run."""

    api_client: SupportsStreamingMessages
    tool_registry: ToolRegistry
    permission_checker: PermissionChecker
    cwd: Path
    model: str
    system_prompt: str
    max_tokens: int
    effort: str | None = None
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    permission_prompt: PermissionPrompt | None = None
    ask_user_prompt: AskUserPrompt | None = None
    max_turns: int | None = 200
    hook_executor: HookExecutor | None = None
    tool_metadata: dict[str, object] | None = None
    runtime_context_provider: Callable[[], str | None] | None = None


def _tool_artifact_dir() -> Path:
    artifact_dir = get_data_dir() / "tool_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _safe_tool_artifact_name(tool_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name.strip())
    return (normalized or "tool")[:80]


def _offload_tool_output_if_needed(
    *,
    tool_name: str,
    tool_use_id: str,
    output: str,
) -> tuple[str, Path | None]:
    inline_limit = tool_output_inline_chars()
    if len(output) <= inline_limit:
        return output, None

    artifact_path = (
        _tool_artifact_dir()
        / f"{time.strftime('%Y%m%d-%H%M%S')}-{_safe_tool_artifact_name(tool_name)}-{uuid4().hex[:12]}.txt"
    )
    from openharness.utils.fs import atomic_write_text
    atomic_write_text(artifact_path, output, mode=0o600)
    preview = output[: tool_output_preview_chars()]
    omitted = max(0, len(output) - len(preview))
    inline = (
        "[Tool output truncated]\n"
        f"Tool: {tool_name}\n"
        f"Tool use id: {tool_use_id}\n"
        f"Original size: {len(output)} chars\n"
        f"Full output saved to: {artifact_path}\n"
        f"Inline preview: first {len(preview)} chars"
    )
    if omitted:
        inline += f" ({omitted} chars omitted)"
    if preview:
        inline += f"\n\nPreview:\n{preview}"
    return inline, artifact_path


# ---------------------------------------------------------------------------
# Image preprocessing — convert ImageBlocks to text for non-multimodal models
# ---------------------------------------------------------------------------

_IMAGE_PREPROCESS_STATUS = "Converting image to text description via vision model…"


async def _preprocess_images_in_messages(
    messages: list[ConversationMessage],
    context: QueryContext,
) -> AsyncIterator[StreamEvent]:
    """Scan messages for ImageBlocks and convert them to text if the active
    model does not support multimodal input.

    Yields status events during conversion so the UI stays responsive.
    """
    if is_model_multimodal(context.model):
        return

    vision_config = (context.tool_metadata or {}).get("vision_model_config")
    if not vision_config:
        # No vision model configured — skip preprocessing.
        return

    # Collect all ImageBlocks with their parent message index and block index
    pending: list[tuple[int, int, ImageBlock]] = []
    for msg_idx, msg in enumerate(messages):
        if msg.role != "user":
            continue
        for blk_idx, block in enumerate(msg.content):
            if isinstance(block, ImageBlock):
                pending.append((msg_idx, blk_idx, block))

    if not pending:
        return

    yield StatusEvent(message=_IMAGE_PREPROCESS_STATUS)

    # Process images in parallel
    async def _describe(msg_idx: int, blk_idx: int, block: ImageBlock) -> tuple[int, int, str]:
        tool = context.tool_registry.get("image_to_text")
        if tool is None:
            return (
                msg_idx,
                blk_idx,
                "[Image: could not describe — image_to_text tool not available]",
            )

        # Build tool input
        tool_input_data: dict[str, object] = {
            "image_data": block.data,
            "media_type": block.media_type,
            "prompt": "Describe this image in detail, including any text, "
            "UI elements, code, diagrams, or visual information present.",
        }

        try:
            parsed = tool.input_model.model_validate(tool_input_data)
        except Exception:
            return msg_idx, blk_idx, "[Image: could not parse image data]"

        exec_context = ToolExecutionContext(
            cwd=context.cwd,
            metadata={
                "vision_model_config": vision_config,
                **(context.tool_metadata or {}),
            },
        )
        result = await tool.execute(parsed, exec_context)
        if result.is_error:
            return msg_idx, blk_idx, f"[Image description failed: {result.output}]"
        return msg_idx, blk_idx, result.output

    results = await asyncio.gather(*[_describe(mi, bi, blk) for mi, bi, blk in pending])

    # Replace ImageBlocks with TextBlocks in-place
    for msg_idx, blk_idx, description in results:
        msg = messages[msg_idx]
        msg.content[blk_idx] = TextBlock(text=description)


async def run_query(
    context: QueryContext,
    messages: list[ConversationMessage],
) -> AsyncIterator[tuple[StreamEvent, UsageSnapshot | None]]:
    """Run the conversation loop until the model stops requesting tools.

    Auto-compaction is checked at the start of each turn.  When the
    estimated token count exceeds the model's auto-compact threshold,
    the engine first tries a cheap microcompact (clearing old tool result
    content) and, if that is not enough, performs a full LLM-based
    summarization of older messages.
    """
    from openharness.services.compact import (
        AutoCompactState,
        auto_compact_if_needed,
    )

    compact_state = AutoCompactState()
    reactive_compact_attempted = False
    last_compaction_result: tuple[list[ConversationMessage], bool] = (messages, False)
    effective_max_tokens = _bounded_completion_tokens(
        context.max_tokens,
        context.context_window_tokens,
    )
    reported_token_clamp = False
    citation_repairs = 0
    verification_repairs = 0
    conflict_repairs = 0

    async def _stream_compaction(
        *,
        trigger: str,
        force: bool = False,
    ) -> AsyncIterator[tuple[StreamEvent, UsageSnapshot | None]]:
        nonlocal last_compaction_result
        progress_queue: asyncio.Queue[CompactProgressEvent] = asyncio.Queue()

        async def _progress(event: CompactProgressEvent) -> None:
            await progress_queue.put(event)

        async def observed_compaction():
            from openharness.engine.observer import observer_for

            with observer_for(context.tool_metadata).span("compaction", metadata={"trigger": trigger}):
                return await auto_compact_if_needed(
                messages,
                api_client=context.api_client,
                model=context.model,
                system_prompt=context.system_prompt,
                state=compact_state,
                progress_callback=_progress,
                force=force,
                trigger=trigger,
                hook_executor=context.hook_executor,
                carryover_metadata=context.tool_metadata,
                context_window_tokens=context.context_window_tokens,
                auto_compact_threshold_tokens=context.auto_compact_threshold_tokens,
                max_tokens=effective_max_tokens,
                tools=request_tools,
                runtime_context_provider=context.runtime_context_provider,
                )

        task = asyncio.create_task(observed_compaction())
        pending_event = None
        try:
            while not task.done() or not progress_queue.empty():
                if not progress_queue.empty():
                    yield progress_queue.get_nowait(), None
                    continue
                pending_event = asyncio.create_task(progress_queue.get())
                # wait_for(queue.get()) can swallow cancellation when the queue
                # future completes concurrently on supported Python versions.
                done, _ = await asyncio.wait((task, pending_event), return_when=asyncio.FIRST_COMPLETED)
                if pending_event in done:
                    yield pending_event.result(), None
                else:
                    pending_event.cancel()
                    await asyncio.gather(pending_event, return_exceptions=True)
                pending_event = None
            last_compaction_result = await task
        finally:
            children = [task] + ([pending_event] if pending_event is not None else [])
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
        return

    turn_count = 0
    while context.max_turns is None or turn_count < context.max_turns:
        turn_count += 1
        if effective_max_tokens != context.max_tokens and not reported_token_clamp:
            reported_token_clamp = True
            yield (
                StatusEvent(
                    message=(
                        "Requested max_tokens="
                        f"{context.max_tokens} exceeds the safe per-request output cap; "
                        f"using {effective_max_tokens}."
                    )
                ),
                None,
            )
        # Reattach current context after compaction or a permission change.
        # Append a new user item only after all tool results are present; never
        # mutate a snapshot that has already been sent to the provider.
        if context.runtime_context_provider is not None:
            current = context.runtime_context_provider()
            previous = next(
                (m.runtime_context for m in reversed(messages) if m.runtime_context), None
            )
            if current and current != previous:
                messages.append(ConversationMessage(role="user", runtime_context=current))
        # ---------------------------------------------------------------

        # --- image preprocessing: convert ImageBlocks to text for non-vision models ---
        async for event in _preprocess_images_in_messages(messages, context):
            yield event, None
        # -----------------------------------------------------------------------------

        request_tools = context.tool_registry.to_api_schema()

        def build_request(candidate=None):
            return prepare_request(context.api_client, ApiMessageRequest(
                model=context.model, messages=messages if candidate is None else candidate, system_prompt=context.system_prompt,
                max_tokens=effective_max_tokens, tools=request_tools, effort=context.effort,
                context_window_tokens=context.context_window_tokens))

        try:
            # Validate configuration before attempting compaction or any main model call.
            request_budget(build_request(), threshold=context.auto_compact_threshold_tokens)
            async for event, usage in _stream_compaction(trigger="auto"):
                yield event, usage
            compacted_messages, was_compacted = last_compaction_result
            request = build_request(compacted_messages if was_compacted else messages)
            budget = request_budget(request, threshold=context.auto_compact_threshold_tokens)
            if context.tool_metadata is not None:
                from dataclasses import asdict
                context.tool_metadata["context_budget"] = asdict(budget)
            budget.require_fit()
            if was_compacted:
                messages[:] = compacted_messages
        except ContextBudgetError as exc:
            yield ErrorEvent(message=str(exc)), None
            return

        final_message: ConversationMessage | None = None
        usage = UsageSnapshot()

        try:
            async for event in context.api_client.stream_message(request):
                if isinstance(event, ApiTextDeltaEvent):
                    yield AssistantTextDelta(text=event.text), None
                    continue
                if isinstance(event, ApiRetryEvent):
                    yield (
                        StatusEvent(
                            message=(
                                f"Request failed; retrying in {event.delay_seconds:.1f}s "
                                f"(attempt {event.attempt + 1} of {event.max_attempts}): {event.message}"
                            )
                        ),
                        None,
                    )
                    continue

                if isinstance(event, ApiMessageCompleteEvent):
                    final_message = event.message
                    usage = event.usage
        except Exception as exc:
            error_msg = str(exc)
            if _is_completion_token_limit_error(exc):
                supported_limit = _extract_completion_token_limit(exc)
                if supported_limit is not None and effective_max_tokens > supported_limit:
                    previous_max_tokens = effective_max_tokens
                    effective_max_tokens = supported_limit
                    yield (
                        StatusEvent(
                            message=(
                                f"Model rejected max_tokens={previous_max_tokens}; "
                                f"retrying with provider limit {effective_max_tokens}."
                            )
                        ),
                        None,
                    )
                    turn_count = max(0, turn_count - 1)
                    continue
            if not reactive_compact_attempted and _is_prompt_too_long_error(exc):
                reactive_compact_attempted = True
                yield StatusEvent(message=REACTIVE_COMPACT_STATUS_MESSAGE), None
                async for event, usage in _stream_compaction(trigger="reactive", force=True):
                    yield event, usage
                compacted_messages, was_compacted = last_compaction_result
                if was_compacted:
                    retry_budget = request_budget(build_request(compacted_messages))
                    if retry_budget.fits and retry_budget.input_tokens < budget.input_tokens:
                        messages[:] = compacted_messages
                        continue
            if (
                "connect" in error_msg.lower()
                or "timeout" in error_msg.lower()
                or "network" in error_msg.lower()
            ):
                yield (
                    ErrorEvent(
                        message=f"Network error: {error_msg}. Check your internet connection and try again."
                    ),
                    None,
                )
            else:
                yield ErrorEvent(message=f"API error: {error_msg}"), None
            return

        if final_message is None:
            raise RuntimeError("Model stream finished without a final message")

        if final_message.role == "assistant" and final_message.is_effectively_empty():
            log.warning("dropping empty assistant message from provider response")
            yield (
                ErrorEvent(
                    message=(
                        "Model returned an empty assistant message. "
                        "The turn was ignored to keep the session healthy."
                    )
                ),
                usage,
            )
            return

        store = (context.tool_metadata or {}).get("research_store")
        if store is not None and not final_message.tool_uses and not (context.tool_metadata or {}).get("conflict_investigator"):
            state = store.load()
            actionable = [item for item in state.conflicts.values()
                          if item.core and item.plan_id == state.research_state.current_plan_id
                          and (item.status == "awaiting_review" or (item.status == "open"
                               and item.last_attempt_fingerprint != store.conflict_fingerprint(state, item)))]
            if (actionable and conflict_repairs < 2
                    and (context.max_turns is None or turn_count < context.max_turns)):
                conflict_repairs += 1
                messages.append(ConversationMessage(role="user", runtime_context="<research_conflict_check>\n"
                    + json.dumps({"conflict_ids": [item.id for item in actionable],
                                  "draft": final_message.text,
                                  "required_action": "Investigate open core conflicts using investigate_conflict. "
                                  "Read completed reports, review original evidence, and submit resolve_conflict. "
                                  "Allow conditional or unresolved decisions. Do not present disputed claims as certain. "
                                  "On timeout or failure retain uncertainty and continue independent work."}, ensure_ascii=False)
                    + "\n</research_conflict_check>"))
                yield StatusEvent(message="正在核查影响核心结论的争议…", discard_draft=True), usage
                continue
            invalid = store.invalid_citations(final_message.text)
            if (
                invalid
                and citation_repairs < 2
                and (context.max_turns is None or turn_count < context.max_turns)
            ):
                citation_repairs += 1
                state = store.load()
                note = {
                    "session_id": state.session_id,
                    "revision": state.revision,
                    "invalid_citations": invalid,
                    "available_evidence_ids": list(state.evidence_pool),
                    "draft": final_message.text,
                    "required_action": "Correct this answer's invalid citations using exact existing evidence IDs, "
                    "including ev_ prefixes. Read research_memory by ID when the supporting evidence is uncertain. "
                    "Do not guess or fabricate a source, silently substitute another source, or collect new data "
                    "just to repair a spelling error. Remove unsupported claims if no existing evidence supports them. "
                    "Return the complete corrected answer using [E:ev_ID] markers; the backend builds the source list.",
                }
                messages.append(
                    ConversationMessage(
                        role="user",
                        runtime_context="<research_answer_check>\n"
                        + json.dumps(note, ensure_ascii=False)
                        + "\n</research_answer_check>",
                    )
                )
                yield StatusEvent(message="正在核对回答中的来源引用…", discard_draft=True), usage
                continue
            state = store.load()
            plan = state.plans.get(state.research_state.current_plan_id or "")
            ready_to_finalize = bool(plan) and all(
                task.status in {"completed", "blocked", "cancelled"} for task in plan.tasks
            )
            candidates = store.verification_candidates(final_message.text) if ready_to_finalize else []
            if (
                candidates
                and verification_repairs < 3
                and (context.max_turns is None or turn_count < context.max_turns)
            ):
                verification_repairs += 1
                note = {
                    "session_id": state.session_id,
                    "revision": state.revision,
                    "draft": final_message.text,
                    "verification_candidates": candidates,
                    "required_action": (
                        "Before publishing, advance only the listed cited evidence using auditable records. "
                        "For source checking, read the evidence and its complete source snapshot, add a verification "
                        "reasoning step, then call verify_evidence(level=source_checked, method=source). "
                        "For cross-source verification, use already source-checked evidence from a distinct source in "
                        "one verification step, then call verify_evidence(level=verified, method=cross_source) with "
                        "supporting_evidence_ids. For deterministic calculations, verify the calculation evidence only "
                        "when all traced inputs are source-checked. Use one research-memory mutation per turn and the "
                        "latest revision. Do not fetch unrelated material merely to change a label. After verification, "
                        "return the complete answer using the successor evidence IDs. If a candidate cannot be advanced, "
                        "keep its current status and return the answer without claiming verification."
                    ),
                }
                messages.append(
                    ConversationMessage(
                        role="user",
                        runtime_context="<research_verification_check>\n"
                        + json.dumps(note, ensure_ascii=False)
                        + "\n</research_verification_check>",
                    )
                )
                yield StatusEvent(message="正在核对引用原文与计算依据…", discard_draft=True), usage
                continue

        messages.append(final_message)
        yield AssistantTurnComplete(message=final_message, usage=usage), usage

        if not final_message.tool_uses:
            if context.hook_executor is not None:
                await context.hook_executor.execute(
                    HookEvent.STOP,
                    {
                        "event": HookEvent.STOP.value,
                        "stop_reason": "tool_uses_empty",
                    },
                )
            return

        tool_calls = final_message.tool_uses

        if len(tool_calls) == 1:
            # Single tool: sequential (stream events immediately)
            tc = tool_calls[0]
            yield (
                ToolExecutionStarted(tool_name=tc.name, tool_input=tc.input, tool_use_id=tc.id),
                None,
            )
            try:
                result = await _execute_tool_call(context, tc.name, tc.id, tc.input)
            except Exception as exc:
                log.exception("tool execution raised: name=%s id=%s", tc.name, tc.id)
                result = ToolResultBlock(
                    tool_use_id=tc.id,
                    content=f"Tool {tc.name} failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                )
            yield (
                ToolExecutionCompleted(
                    tool_name=tc.name,
                    output=result.content,
                    is_error=result.is_error,
                    metadata=result.result_metadata,
                    tool_use_id=result.tool_use_id,
                ),
                None,
            )
            if (context.tool_metadata or {}).get("research_store"):
                progress = (result.result_metadata or {}).get("research_progress")
                yield (
                    ResearchProgressEvent(
                        progress=progress or context.tool_metadata["research_store"].progress()
                    ),
                    None,
                )
            tool_results = [result]
        else:
            # Multiple tools: report each completion while slower siblings run.
            for tc in tool_calls:
                yield (
                    ToolExecutionStarted(tool_name=tc.name, tool_input=tc.input, tool_use_id=tc.id),
                    None,
                )

            async def _run(index, tc):
                try:
                    result = await _execute_tool_call(context, tc.name, tc.id, tc.input)
                except Exception as exc:
                    log.exception("tool execution raised: name=%s id=%s", tc.name, tc.id)
                    result = ToolResultBlock(
                        tool_use_id=tc.id,
                        content=f"Tool {tc.name} failed: {type(exc).__name__}: {exc}",
                        is_error=True,
                    )
                return index, result

            pending_tools = [
                asyncio.create_task(_run(index, tc)) for index, tc in enumerate(tool_calls)
            ]
            ordered_results = {}
            try:
                for completed in asyncio.as_completed(pending_tools):
                    index, result = await completed
                    ordered_results[index] = result
                    yield (
                        ToolExecutionCompleted(
                            tool_name=tool_calls[index].name,
                            output=result.content,
                            is_error=result.is_error,
                            metadata=result.result_metadata,
                            tool_use_id=result.tool_use_id,
                        ),
                        None,
                    )
                    if (context.tool_metadata or {}).get("research_store"):
                        progress = (result.result_metadata or {}).get("research_progress")
                        yield (
                            ResearchProgressEvent(
                                progress=progress or context.tool_metadata["research_store"].progress()
                            ),
                            None,
                        )
            finally:
                # Cancellation must settle all tools before a steering run starts.
                for task in pending_tools:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending_tools, return_exceptions=True)
            tool_results = [ordered_results[index] for index in range(len(tool_calls))]

        messages.append(ConversationMessage(role="user", content=tool_results))

    if context.max_turns is not None:
        raise MaxTurnsExceeded(context.max_turns)
    raise RuntimeError("Query loop exited without a max_turns limit or final response")


async def _execute_tool_call(
    context: QueryContext,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
) -> ToolResultBlock:
    from openharness.engine.observer import observer_for

    with observer_for(context.tool_metadata).span(
        tool_name, kind="tool", input=tool_input,
        metadata={"tool_use_id": tool_use_id,
                  "investigation": bool((context.tool_metadata or {}).get("conflict_investigator"))},
    ) as span:
        result = await _execute_tool_call_impl(context, tool_name, tool_use_id, tool_input)
        outcome = (result.result_metadata or {}).get("outcome")
        span.update(output=result.content, status="denied" if outcome in {"denied", "evaluation_boundary"}
                    else "error" if result.is_error else "ok",
                    metadata={"tool_use_id": tool_use_id,
                              "investigation": bool((context.tool_metadata or {}).get("conflict_investigator")),
                              **(result.result_metadata or {})})
        return result


async def _execute_tool_call_impl(
    context: QueryContext,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
) -> ToolResultBlock:
    store = (context.tool_metadata or {}).get("research_store")
    if (
        store is not None
        and store.load().research_state.replan_required
        and tool_name not in {"research_memory", "ask_user_question", "skill", "investigate_conflict"}
    ):
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content="Research requires a new plan. Use research_memory to update context and create_plan before executing more tools.",
            is_error=True,
        )
    if store is not None and tool_name not in {"research_memory", "ask_user_question", "skill", "investigate_conflict"}:
        memory = store.load()
        plan = memory.plans.get(memory.research_state.current_plan_id)
        if plan and not memory.research_state.current_task_id:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                is_error=True,
                content="Start the current research task before collecting or calculating: use research_memory.update_task "
                "with the actual task ID and status in_progress, then execute its work and update its result. "
                "Do not collect the whole plan while leaving every task pending.",
            )
    if context.hook_executor is not None:
        pre_hooks = await context.hook_executor.execute(
            HookEvent.PRE_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "event": HookEvent.PRE_TOOL_USE.value,
            },
        )
        if pre_hooks.blocked:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=pre_hooks.reason or f"pre_tool_use hook blocked {tool_name}",
                is_error=True,
            )

    log.debug("tool_call start: %s id=%s", tool_name, tool_use_id)

    tool = context.tool_registry.get(tool_name)
    if tool is None:
        log.warning("unknown tool: %s", tool_name)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"Unknown tool: {tool_name}",
            is_error=True,
        )

    try:
        parsed_input = tool.input_model.model_validate(tool_input)
    except Exception as exc:
        log.warning("invalid input for %s: %s", tool_name, exc)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=tool.validation_error_message(exc),
            is_error=True,
        )

    # Normalize common tool inputs before permission checks so path rules apply
    # consistently across built-in tools that use `file_path`, `path`, or
    # directory-scoped roots such as `glob`/`grep`.
    _file_path = _resolve_permission_file_path(context.cwd, tool_input, parsed_input)
    _command = _extract_permission_command(tool_input, parsed_input)
    log.debug(
        "permission check: %s read_only=%s path=%s cmd=%s",
        tool_name,
        tool.is_read_only(parsed_input),
        _file_path,
        _command and _command[:80],
    )
    decision = context.permission_checker.evaluate(
        tool_name,
        is_read_only=tool.is_read_only(parsed_input),
        file_path=_file_path,
        command=_command,
    )
    if not decision.allowed:
        if decision.requires_confirmation and context.permission_prompt is not None:
            log.debug("permission prompt for %s: %s", tool_name, decision.reason)
            if context.hook_executor is not None:
                await context.hook_executor.execute(
                    HookEvent.NOTIFICATION,
                    {
                        "event": HookEvent.NOTIFICATION.value,
                        "notification_type": "permission_prompt",
                        "tool_name": tool_name,
                        "reason": decision.reason,
                    },
                )
            confirmed = await context.permission_prompt(tool_name, decision.reason)
            if not confirmed:
                log.debug("permission denied by user for %s", tool_name)
                return ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=decision.reason or f"Permission denied for {tool_name}",
                    is_error=True,
                    result_metadata={"outcome": "denied"},
                )
        else:
            log.debug("permission blocked for %s: %s", tool_name, decision.reason)
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=decision.reason or f"Permission denied for {tool_name}",
                is_error=True,
                result_metadata={"outcome": "denied"},
            )

    log.debug("executing %s ...", tool_name)
    t0 = time.monotonic()
    result = await tool.execute(
        parsed_input,
        ToolExecutionContext(
            cwd=context.cwd,
            metadata={
                "tool_registry": context.tool_registry,
                "ask_user_prompt": context.ask_user_prompt,
                **(context.tool_metadata or {}),
                "query_context": context,
            },
            hook_executor=context.hook_executor,
        ),
    )
    elapsed = time.monotonic() - t0
    if store is not None and tool_name not in {"research_memory", "investigate_conflict"}:
        source_specs = result.metadata.get("research_source_specs")
        if source_specs is None:
            source_specs = [{
                "content": result.output,
                "kind": "calculation"
                if tool_name == "bash"
                else "user"
                if tool_name == "ask_user_question"
                else "tool",
                "title": tool_name,
                "locator": f"tool:{tool_name}:{tool_use_id}",
                "fragment": True,
            }]
        sources = []
        for index, spec in enumerate(source_specs):
            sources.append(
                store.capture(
                    origin_id=tool_use_id,
                    index=index,
                    content=spec["content"],
                    kind=spec.get("kind", "tool"),
                    title=spec.get("title", tool_name),
                    locator=spec.get("locator", f"tool:{tool_name}:{tool_use_id}"),
                    fragment=spec.get("fragment", False),
                    published_at=spec.get("published_at"),
                    is_error=result.is_error,
                ).id
            )
        from dataclasses import replace

        result = replace(
            result,
            metadata={
                **{
                    key: value
                    for key, value in result.metadata.items()
                    if key != "research_source_specs"
                },
                "research_sources": sources,
            },
        )
    log.debug(
        "executed %s in %.2fs err=%s output_len=%d",
        tool_name,
        elapsed,
        result.is_error,
        len(result.output or ""),
    )
    inline_output, artifact_path = _offload_tool_output_if_needed(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        output=result.output,
    )
    tool_result = ToolResultBlock(
        tool_use_id=tool_use_id,
        content=inline_output,
        is_error=result.is_error,
        result_metadata={
            key: value
            for key, value in (result.metadata or {}).items()
            if key != "research_source_specs"
        },
    )
    if artifact_path is not None:
        tool_result.result_metadata["tool_output_artifact"] = str(artifact_path)
    if store is not None and result.metadata.get("research_sources"):
        tool_result.content += "\n\nresearch_sources: " + json.dumps(
            result.metadata["research_sources"]
        )
        tool_result.content += f"\nresearch_revision: {store.load().revision}"
    if tool_name == "skill" and not tool_result.is_error:
        skill_name = str(tool_input.get("name", "")).strip()
        if skill_name and context.tool_metadata is not None:
            skills = context.tool_metadata.setdefault("invoked_skills", [])
            if skill_name not in skills:
                skills.append(skill_name)
    if context.hook_executor is not None:
        await context.hook_executor.execute(
            HookEvent.POST_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": tool_result.content,
                "tool_is_error": tool_result.is_error,
                "event": HookEvent.POST_TOOL_USE.value,
            },
        )
    return tool_result


def _resolve_permission_file_path(
    cwd: Path,
    raw_input: dict[str, object],
    parsed_input: object,
) -> str | None:
    for key in ("file_path", "path", "root"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = cwd / path
            return str(path.resolve())

    for attr in ("file_path", "path", "root"):
        value = getattr(parsed_input, attr, None)
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = cwd / path
            return str(path.resolve())

    return None


def _extract_permission_command(
    raw_input: dict[str, object],
    parsed_input: object,
) -> str | None:
    value = raw_input.get("command")
    if isinstance(value, str) and value.strip():
        return value

    value = getattr(parsed_input, "command", None)
    if isinstance(value, str) and value.strip():
        return value

    return None
