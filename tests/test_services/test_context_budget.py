"""Request-boundary and transactional-compaction regression tests."""
import asyncio
import json
from pathlib import Path

import pytest

from openharness.api.client import ApiMessageRequest, ApiMessageCompleteEvent, AnthropicApiClient
from openharness.api.openai_client import OpenAICompatibleClient
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage as Message, TextBlock, ToolUseBlock, ToolResultBlock, ImageBlock
from openharness.engine.query import QueryContext, run_query
from openharness.engine.stream_events import ErrorEvent, AssistantTurnComplete
from openharness.permissions.checker import PermissionChecker
from openharness.config.settings import PermissionSettings
from openharness.tools.base import ToolRegistry
from openharness.services.context_budget import (
    ContextBudgetError, prepare_request, request_budget, budget_limits,
)
from openharness.services.token_estimation import estimate_tokens
from openharness.services.compact import (
    AutoCompactState, auto_compact_if_needed, compact_conversation,
    microcompact_messages, build_post_compact_messages,
)

MODEL = "claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def isolated_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))


class Client:
    def __init__(self, summary="<summary>Old work complete.</summary>", stop_reason="stop"):
        self.requests = []
        self.summary = summary
        self.stop_reason = stop_reason

    async def stream_message(self, request):
        self.requests.append(request)
        if isinstance(self.summary, Exception):
            raise self.summary
        yield ApiMessageCompleteEvent(
            message=Message(role="assistant", content=[TextBlock(text=self.summary)]),
            usage=UsageSnapshot(), stop_reason=self.stop_reason)


def history():
    return [Message.from_user_text("old detail " * 2500),
            Message(role="assistant", content=[TextBlock(text="old answer")]),
            Message.from_user_text("latest request")]


def budget(**kwargs):
    request = ApiMessageRequest(model=MODEL, messages=[Message.from_user_text("hello")], **kwargs)
    return request_budget(prepare_request(Client(), request))


@pytest.mark.parametrize("window", [4000, 32000, 128000, 200000])
def test_output_aware_budget_and_small_windows(window):
    result = budget(context_window_tokens=window, max_tokens=512)
    assert result.input_limit == window - 512 - (window + 19) // 20
    assert 0 < result.target_tokens < result.trigger_tokens < result.input_limit
    assert result.fits


def test_unknown_window_and_no_input_capacity():
    with pytest.raises(ContextBudgetError, match="context_window_tokens"):
        budget_limits("unknown-model", 512)
    with pytest.raises(ContextBudgetError, match="没有可用输入"):
        budget(context_window_tokens=4000, max_tokens=4096)
    assert budget_limits("unknown-model", 512, 4000)[0] == 4000


def test_manual_threshold_is_clamped_to_hard_limit():
    _, _, available, trigger, target = budget_limits(MODEL, 512, 4000, 999999)
    assert trigger == available
    assert target <= trigger * .75
    assert budget_limits(MODEL, 512, 4000, 1000)[3:] == (1000, 750)


def test_unicode_and_tokenizer_counting():
    assert estimate_tokens("abc中", "unknown-model") == 4
    assert estimate_tokens("中文", "unknown-model") == 6
    import tiktoken
    text = '中文 JSON {"hello": "world"} <|endoftext|>'
    assert estimate_tokens(text, "gpt-4o") == len(tiktoken.encoding_for_model("gpt-4o").encode(text, disallowed_special=()))


def test_complete_provider_input_is_counted_and_frozen():
    client = OpenAICompatibleClient(api_key="unused")
    messages = [Message.from_user_text("question"), Message(role="assistant", reasoning_content="reason " * 200,
        content=[ToolUseBlock(id="call1", name="search", input={"query": "test"})]),
        Message(role="user", runtime_context="current research " * 200,
            content=[ToolResultBlock(tool_use_id="call1", content="result"),
                     ImageBlock(media_type="image/png", data="A" * 100000)])]
    request = ApiMessageRequest(model="gpt-4o", messages=messages, system_prompt="system " * 200,
        tools=[{"name": "search", "description": "tool " * 200, "input_schema": {"type": "object"}}])
    prepared = prepare_request(client, request)
    result = request_budget(prepared)
    assert result.components["tools"] > 200
    assert result.components["messages"] > 600
    assert result.components["images"] == 3072
    serialized = json.dumps(prepared.prepared_payload)
    assert "reasoning_content" in serialized and "current research" in serialized
    messages[1].content[0].input["query"] = "mutated"
    assert json.dumps(prepared.prepared_payload) == serialized
    assert result.input_tokens < 10000  # base64 is not counted as text


def test_anthropic_oauth_attribution_is_part_of_counted_payload():
    client = AnthropicApiClient(api_key="unused", claude_oauth=True)
    request = prepare_request(client, ApiMessageRequest(model=MODEL, messages=[], system_prompt="system"))
    assert request.prepared_payload["system"] != "system"
    assert request_budget(request).components["system"] > estimate_tokens("system", MODEL)


@pytest.mark.asyncio
async def test_summary_sees_untruncated_middle_and_snapshot_is_recoverable(tmp_path):
    messages = history()
    constraint = "用户约束：只使用2025年数据"
    messages[0].content = [TextBlock(text="a" * 4000 + constraint + "b" * 4000)]
    original = [m.model_dump(mode="json") for m in messages]
    client = Client(f"<summary>{constraint}</summary>")
    result = await compact_conversation(messages, api_client=client, model=MODEL, preserve_recent=1)
    assert constraint in client.requests[0].messages[0].text
    assert client.requests[0].max_tokens == 4096
    assert constraint in build_post_compact_messages(result)[1].text
    snapshot = json.loads(Path(result.compact_metadata["snapshot_path"]).read_text())
    assert snapshot["messages"] == original
    assert [m.model_dump(mode="json") for m in messages] == original
    assert result.status == "success"


def tool_history():
    messages = []
    for i in range(8):
        messages += [Message(role="assistant", content=[ToolUseBlock(id=f"t{i}", name="read_file", input={})]),
                     Message(role="user", content=[ToolResultBlock(tool_use_id=f"t{i}", content=f"original {i} " * 1000,
                         is_error=i == 0, result_metadata={"research_sources": ["src_123"], "custom": i})])]
    messages.append(Message.from_user_text("new request"))
    return messages


def test_microcompact_preserves_original_metadata_and_readable_artifacts():
    messages = tool_history()
    original = [m.model_dump() for m in messages]
    candidate, saved = microcompact_messages(messages)
    assert saved > 0
    block = candidate[1].content[0]
    assert block.is_error and block.tool_use_id == "t0"
    assert block.result_metadata["custom"] == 0
    assert "src_123" in block.content
    assert Path(block.result_metadata["context_artifact"]).read_text() == messages[1].content[0].content
    assert [m.model_dump() for m in messages] == original


@pytest.mark.asyncio
async def test_failed_micro_candidate_uses_original_summary_input():
    messages = tool_history()
    client = Client()
    _, changed = await auto_compact_if_needed(messages, api_client=client, model=MODEL,
        state=AutoCompactState(), preserve_recent=1, auto_compact_threshold_tokens=5000)
    assert changed
    summary_content = "\n".join(b.content for m in client.requests[0].messages for b in m.content if isinstance(b, ToolResultBlock))
    assert "original 0 " * 100 in summary_content
    assert "[Archived tool result]" not in summary_content


@pytest.mark.asyncio
@pytest.mark.parametrize("response,stop", [
    ("", "stop"), ("<summary>unfinished</summary>", "length"),
    ("<summary>[E:ev_invented]</summary>", "stop"),
    ("<summary>" + "giant " * 10000 + "</summary>", "stop"),
    (RuntimeError("prompt too long"), "stop"),
])
async def test_rejected_summary_never_commits(response, stop):
    messages = history()
    original = [m.model_dump() for m in messages]
    state = AutoCompactState()
    client = Client(response, stop)
    candidate, changed = await auto_compact_if_needed(messages, api_client=client, model=MODEL,
        state=state, preserve_recent=1, auto_compact_threshold_tokens=5000)
    assert not changed and state.last_status == "failed"
    assert candidate is messages
    assert [m.model_dump() for m in messages] == original
    if isinstance(response, Exception):
        assert len(client.requests) == 1  # no head-truncation retries


@pytest.mark.asyncio
async def test_snapshot_failure_prevents_summary_call(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("openharness.services.compact.save_context_snapshot", fail)
    client = Client()
    messages = history()
    result, changed = await auto_compact_if_needed(messages, api_client=client, model=MODEL,
        state=AutoCompactState(), force=True)
    assert not changed and result is messages and not client.requests


@pytest.mark.asyncio
async def test_summary_request_too_large_keeps_all_original_rounds():
    client = Client()
    messages = history()
    result, changed = await auto_compact_if_needed(messages, api_client=client, model=MODEL,
        state=AutoCompactState(), preserve_recent=1, context_window_tokens=4000, max_tokens=512, force=True)
    assert not changed and result is messages and not client.requests


@pytest.mark.asyncio
async def test_cancel_during_summary_keeps_original_history():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    class Slow(Client):
        async def stream_message(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            yield  # async iterator
    messages = history()
    original = [m.model_dump() for m in messages]
    task = asyncio.create_task(auto_compact_if_needed(messages, api_client=Slow(), model=MODEL,
        state=AutoCompactState(), preserve_recent=1, force=True))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert [m.model_dump() for m in messages] == original


def context(tmp_path, client, **kwargs):
    return QueryContext(api_client=client, tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings()), cwd=tmp_path,
        model=MODEL, system_prompt="", max_tokens=512, context_window_tokens=4000, **kwargs)


@pytest.mark.asyncio
async def test_latest_request_and_runtime_overflow_never_reach_provider(tmp_path):
    for prompt, runtime in [("中" * 2000, ""), ("hello", "中" * 2000)]:
        client = Client()
        messages = [Message.from_user_text(prompt)]
        events = [e async for e, _ in run_query(context(tmp_path, client, runtime_context_provider=lambda: runtime), messages)]
        assert isinstance(events[-1], ErrorEvent)
        assert not client.requests
        assert messages[0].text == prompt


@pytest.mark.asyncio
async def test_failed_soft_compaction_can_send_original_fitting_request(tmp_path):
    client = Client("normal answer")
    ctx = context(tmp_path, client, auto_compact_threshold_tokens=100)
    messages = [Message.from_user_text("latest " * 200)]
    original = messages[0].text
    events = [e async for e, _ in run_query(ctx, messages)]
    assert isinstance(events[-1], AssistantTurnComplete)
    assert len(client.requests) == 1 and client.requests[0].messages[0].text == original


@pytest.mark.asyncio
async def test_unknown_model_query_errors_without_call(tmp_path):
    client = Client()
    ctx = context(tmp_path, client)
    ctx.model, ctx.context_window_tokens = "unregistered", None
    events = [e async for e, _ in run_query(ctx, [Message.from_user_text("hello")])]
    assert isinstance(events[-1], ErrorEvent) and "context_window_tokens" in events[-1].message
    assert not client.requests


@pytest.mark.asyncio
async def test_latest_user_round_keeps_entire_tool_chain():
    messages = history()
    for i in range(8):
        messages.extend([
            Message(role="assistant", content=[ToolUseBlock(id=f"new{i}", name="read_file", input={})]),
            Message(role="user", content=[ToolResultBlock(tool_use_id=f"new{i}", content="important result")]),
        ])
    protected = [m.model_dump() for m in messages[2:]]
    result = await compact_conversation(messages, api_client=Client(), model=MODEL, preserve_recent=6)
    assert [m.model_dump() for m in result.messages_to_keep] == protected


@pytest.mark.asyncio
async def test_summary_output_reservation_scales_with_window():
    client = Client()
    await compact_conversation(history(), api_client=client, model=MODEL,
                               context_window_tokens=32000, max_tokens=512, preserve_recent=1)
    assert client.requests[0].max_tokens == 3200
    assert client.requests[0].context_window_tokens == 32000
    assert request_budget(client.requests[0]).fits


@pytest.mark.asyncio
async def test_timeout_rolls_back_after_real_summary_attempt(monkeypatch):
    monkeypatch.setattr("openharness.services.compact.COMPACT_TIMEOUT_SECONDS", .01)
    class Slow(Client):
        async def stream_message(self, request):
            self.requests.append(request)
            await asyncio.sleep(10)
            yield
    messages = history()
    original = [m.model_dump() for m in messages]
    client = Slow()
    result, changed = await auto_compact_if_needed(messages, api_client=client, model=MODEL,
        state=AutoCompactState(), preserve_recent=1, force=True)
    assert client.requests and not changed and result is messages
    assert [m.model_dump() for m in messages] == original


@pytest.mark.asyncio
async def test_query_cancellation_joins_summary_task(tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()
    class Slow(Client):
        async def stream_message(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            yield
    messages = history()[:2] + [Message.from_user_text("recent") for _ in range(8)]
    original = [m.model_dump() for m in messages]
    ctx = context(tmp_path, Slow(), auto_compact_threshold_tokens=5000)
    ctx.context_window_tokens = 200_000
    async def consume():
        async for _ in run_query(ctx, messages):
            pass
    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cancelled.is_set()
    assert [m.model_dump() for m in messages] == original


@pytest.mark.asyncio
async def test_image_description_is_counted_before_main_call(tmp_path, monkeypatch):
    async def convert(messages, context):
        messages[0] = Message.from_user_text("中文描述" * 1000)
        if False:
            yield
    monkeypatch.setattr("openharness.engine.query._preprocess_images_in_messages", convert)
    client = Client()
    messages = [Message(role="user", content=[ImageBlock(media_type="image/png", data="abc")])]
    events = [e async for e, _ in run_query(context(tmp_path, client), messages)]
    assert isinstance(events[-1], ErrorEvent)
    assert not client.requests


@pytest.mark.asyncio
async def test_runtime_growth_during_summary_rejects_candidate():
    messages = history()
    original = [m.model_dump() for m in messages]
    client = Client()
    candidate, changed = await auto_compact_if_needed(messages, api_client=client, model=MODEL,
        state=AutoCompactState(), preserve_recent=1, auto_compact_threshold_tokens=5000,
        runtime_context_provider=lambda: "研究状态" * 2000)
    assert client.requests and not changed and candidate is messages
    assert [m.model_dump() for m in messages] == original


def test_research_prompt_uses_requested_model_counter(tmp_path, monkeypatch):
    from openharness.research.store import ResearchStore
    from openharness.research.errors import ResearchError
    store = ResearchStore(tmp_path, "a" * 12)
    seen = []
    def count(text, model=""):
        seen.append(model)
        return 10_000
    monkeypatch.setattr("openharness.research.store.estimate_tokens", count)
    with pytest.raises(ResearchError, match="预算"):
        store.prompt(6000, model="requested-model")
    assert seen == ["requested-model"]


def test_image_shaped_tool_arguments_are_counted_as_text():
    image_argument = {"type": "image", "source": {"type": "base64", "data": "x" * 12000}}
    request = ApiMessageRequest(model=MODEL, messages=[Message(role="assistant", content=[
        ToolUseBlock(id="image-argument", name="custom", input=image_argument)])])
    result = request_budget(prepare_request(Client(), request))
    assert result.components["images"] == 0
    assert result.components["messages"] > 4000
