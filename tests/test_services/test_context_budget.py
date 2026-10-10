"""Request-boundary and transactional-compaction regression tests."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from researchx.config.context_components import ContextComponentsSettings, COMPONENT_KEYS
from researchx.config.settings import Settings, save_settings, load_settings
from researchx.engine.messages import ContextSpan
from researchx.services.context.sources import ContextSnapshot, runtime_fragments

from researchx.api.client import ApiMessageRequest, ApiMessageCompleteEvent, AnthropicApiClient
from researchx.api.openai_client import OpenAICompatibleClient
from researchx.api.usage import UsageSnapshot
from researchx.engine.messages import (
    ConversationMessage as Message,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ImageBlock,
)
from researchx.engine.query import QueryContext, run_query
from researchx.engine.stream_events import ErrorEvent, AssistantTurnComplete
from researchx.permissions.checker import PermissionChecker
from researchx.config.settings import PermissionSettings
from researchx.tools.base import ToolRegistry
from researchx.services.context.budget import (
    ContextBudgetError,
    prepare_request,
    request_budget,
    budget_limits,
)
from researchx.services.context.token_estimation import estimate_tokens
from researchx.services.compact import (
    AutoCompactState,
    auto_compact_if_needed,
    compact_conversation,
    microcompact_messages,
    build_post_compact_messages,
)

MODEL = "claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def isolated_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))


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
            usage=UsageSnapshot(),
            stop_reason=self.stop_reason,
        )


def history():
    return [
        Message.from_user_text("old detail " * 2500),
        Message(role="assistant", content=[TextBlock(text="old answer")]),
        Message.from_user_text("latest request"),
    ]


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


@pytest.mark.parametrize(
    "model",
    ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"],
)
def test_deepseek_official_windows_and_explicit_override(model):
    window, safety, available, trigger, target = budget_limits(model, 16_384)
    assert window == 1_000_000
    assert safety == 50_000
    assert available == 933_616
    assert 0 < target < trigger < available
    # A gateway or deployment with a lower limit must retain its explicit setting.
    assert budget_limits(model, 512, 32_000)[0] == 32_000


def test_unregistered_deepseek_alias_requires_explicit_window():
    with pytest.raises(ContextBudgetError, match="context_window_tokens"):
        budget_limits("deepseek-flash-custom", 512)


def test_manual_threshold_is_clamped_to_hard_limit():
    _, _, available, trigger, target = budget_limits(MODEL, 512, 4000, 999999)
    assert trigger == available
    assert target <= trigger * 0.75
    assert budget_limits(MODEL, 512, 4000, 1000)[3:] == (1000, 750)


def test_unicode_and_tokenizer_counting():
    assert estimate_tokens("abc中", "unknown-model") == 4
    assert estimate_tokens("中文", "unknown-model") == 6
    import tiktoken

    text = '中文 JSON {"hello": "world"} <|endoftext|>'
    assert estimate_tokens(text, "gpt-4o") == len(
        tiktoken.encoding_for_model("gpt-4o").encode(text, disallowed_special=())
    )


def test_complete_provider_input_is_counted_and_frozen():
    client = OpenAICompatibleClient(api_key="unused")
    messages = [
        Message.from_user_text("question"),
        Message(
            role="assistant",
            reasoning_content="reason " * 200,
            content=[ToolUseBlock(id="call1", name="search", input={"query": "test"})],
        ),
        Message(
            role="user",
            runtime_context="current research " * 200,
            content=[
                ToolResultBlock(tool_use_id="call1", content="result"),
                ImageBlock(media_type="image/png", data="A" * 100000),
            ],
        ),
    ]
    request = ApiMessageRequest(
        model="gpt-4o",
        messages=messages,
        system_prompt="system " * 200,
        tools=[
            {"name": "search", "description": "tool " * 200, "input_schema": {"type": "object"}}
        ],
    )
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
    request = prepare_request(
        client, ApiMessageRequest(model=MODEL, messages=[], system_prompt="system")
    )
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
        messages += [
            Message(
                role="assistant", content=[ToolUseBlock(id=f"t{i}", name="read_file", input={})]
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id=f"t{i}",
                        content=f"original {i} " * 1000,
                        is_error=i == 0,
                        result_metadata={"research_sources": ["src_123"], "custom": i},
                    )
                ],
            ),
        ]
    messages.append(Message.from_user_text("new request"))
    return messages


async def test_microcompact_preserves_original_metadata_and_readable_artifacts():
    messages = tool_history()
    original = [m.model_dump() for m in messages]
    candidate, saved = (await microcompact_messages(messages))
    assert saved > 0
    block = candidate[1].content[0]
    assert block.is_error and block.tool_use_id == "t0"
    assert block.result_metadata["custom"] == 0
    assert "src_123" in block.content
    assert (
        Path(block.result_metadata["context_artifact"]).read_text()
        == messages[1].content[0].content
    )
    assert [m.model_dump() for m in messages] == original


@pytest.mark.asyncio
async def test_failed_micro_candidate_uses_original_summary_input():
    messages = tool_history()
    client = Client()
    _, changed = await auto_compact_if_needed(
        messages,
        api_client=client,
        model=MODEL,
        state=AutoCompactState(),
        preserve_recent=1,
        auto_compact_threshold_tokens=5000,
    )
    assert changed
    summary_content = "\n".join(
        b.content
        for m in client.requests[0].messages
        for b in m.content
        if isinstance(b, ToolResultBlock)
    )
    assert "original 0 " * 100 in summary_content
    assert "[Archived tool result]" not in summary_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,stop",
    [
        ("", "stop"),
        ("<summary>unfinished</summary>", "length"),
        ("<summary>[E:ev_invented]</summary>", "stop"),
        ("<summary>" + "giant " * 10000 + "</summary>", "stop"),
        (RuntimeError("prompt too long"), "stop"),
    ],
)
async def test_rejected_summary_never_commits(response, stop):
    messages = history()
    original = [m.model_dump() for m in messages]
    state = AutoCompactState()
    client = Client(response, stop)
    candidate, changed = await auto_compact_if_needed(
        messages,
        api_client=client,
        model=MODEL,
        state=state,
        preserve_recent=1,
        auto_compact_threshold_tokens=5000,
    )
    assert not changed and state.last_status == "failed"
    assert candidate is messages
    assert [m.model_dump() for m in messages] == original
    if isinstance(response, Exception):
        assert len(client.requests) == 1  # no head-truncation retries


@pytest.mark.asyncio
async def test_snapshot_failure_prevents_summary_call(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("researchx.services.compact.save_context_snapshot", fail)
    client = Client()
    messages = history()
    result, changed = await auto_compact_if_needed(
        messages, api_client=client, model=MODEL, state=AutoCompactState(), force=True
    )
    assert not changed and result is messages and not client.requests


@pytest.mark.asyncio
async def test_summary_request_too_large_keeps_all_original_rounds():
    client = Client()
    messages = history()
    result, changed = await auto_compact_if_needed(
        messages,
        api_client=client,
        model=MODEL,
        state=AutoCompactState(),
        preserve_recent=1,
        context_window_tokens=4000,
        max_tokens=512,
        force=True,
    )
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
    task = asyncio.create_task(
        auto_compact_if_needed(
            messages,
            api_client=Slow(),
            model=MODEL,
            state=AutoCompactState(),
            preserve_recent=1,
            force=True,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert [m.model_dump() for m in messages] == original


def context(tmp_path, client, **kwargs):
    return QueryContext(
        api_client=client,
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model=MODEL,
        system_prompt="",
        max_tokens=512,
        context_window_tokens=4000,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_latest_request_and_runtime_overflow_never_reach_provider(tmp_path):
    for prompt, runtime in [("中" * 2000, ""), ("hello", "中" * 2000)]:
        client = Client()
        messages = [Message.from_user_text(prompt)]
        events = [
            e
            async for e, _ in run_query(
                context(tmp_path, client, runtime_context_provider=lambda: runtime), messages
            )
        ]
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
async def test_deepseek_query_without_explicit_window_preserves_user_input(tmp_path):
    client = Client("normal answer")
    ctx = context(tmp_path, client)
    ctx.model, ctx.context_window_tokens, ctx.max_tokens = "deepseek-flash", None, 16_384
    prompt = "请分析公司的最新财务表现。"
    messages = [Message.from_user_text(prompt)]
    events = [e async for e, _ in run_query(ctx, messages)]
    assert isinstance(events[-1], AssistantTurnComplete)
    assert len(client.requests) == 1
    assert client.requests[0].model == "deepseek-flash"
    assert client.requests[0].messages[0].text == prompt
    assert request_budget(client.requests[0]).window == 1_000_000


@pytest.mark.asyncio
async def test_latest_user_round_keeps_entire_tool_chain():
    messages = history()
    for i in range(8):
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[ToolUseBlock(id=f"new{i}", name="read_file", input={})],
                ),
                Message(
                    role="user",
                    content=[ToolResultBlock(tool_use_id=f"new{i}", content="important result")],
                ),
            ]
        )
    protected = [m.model_dump() for m in messages[2:]]
    result = await compact_conversation(
        messages, api_client=Client(), model=MODEL, preserve_recent=6
    )
    assert [m.model_dump() for m in result.messages_to_keep] == protected


@pytest.mark.asyncio
async def test_summary_output_reservation_scales_with_window():
    client = Client()
    await compact_conversation(
        history(),
        api_client=client,
        model=MODEL,
        context_window_tokens=32000,
        max_tokens=512,
        preserve_recent=1,
    )
    assert client.requests[0].max_tokens == 3200
    assert client.requests[0].context_window_tokens == 32000
    assert request_budget(client.requests[0]).fits


@pytest.mark.asyncio
async def test_timeout_rolls_back_after_real_summary_attempt(monkeypatch):
    monkeypatch.setattr("researchx.services.compact.COMPACT_TIMEOUT_SECONDS", 0.01)

    class Slow(Client):
        async def stream_message(self, request):
            self.requests.append(request)
            await asyncio.sleep(10)
            yield

    messages = history()
    original = [m.model_dump() for m in messages]
    client = Slow()
    result, changed = await auto_compact_if_needed(
        messages,
        api_client=client,
        model=MODEL,
        state=AutoCompactState(),
        preserve_recent=1,
        force=True,
    )
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

    monkeypatch.setattr("researchx.engine.query._preprocess_images_in_messages", convert)
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
    candidate, changed = await auto_compact_if_needed(
        messages,
        api_client=client,
        model=MODEL,
        state=AutoCompactState(),
        preserve_recent=1,
        auto_compact_threshold_tokens=5000,
        runtime_context_provider=lambda: "研究状态" * 2000,
    )
    assert client.requests and not changed and candidate is messages
    assert [m.model_dump() for m in messages] == original


async def test_research_prompt_uses_requested_model_counter(tmp_path, monkeypatch):
    from researchx.state.store import ResearchStore
    from researchx.state.errors import ResearchError

    store = ResearchStore(tmp_path, "a" * 12)
    seen = []

    def count(text, model=""):
        seen.append(model)
        return 10_000

    monkeypatch.setattr("researchx.state.store.estimate_tokens", count)
    with pytest.raises(ResearchError, match="预算"):
        (await store.prompt(6000, model="requested-model"))
    assert seen == ["requested-model"]


def test_image_shaped_tool_arguments_are_counted_as_text():
    image_argument = {"type": "image", "source": {"type": "base64", "data": "x" * 12000}}
    request = ApiMessageRequest(
        model=MODEL,
        messages=[
            Message(
                role="assistant",
                content=[ToolUseBlock(id="image-argument", name="custom", input=image_argument)],
            )
        ],
    )
    result = request_budget(prepare_request(Client(), request))
    assert result.components["images"] == 0
    assert result.components["messages"] > 4000


# Six logical categories supplement, rather than replace, the wire statistics.


def component_request(messages=None, *, client=None, policy=None, **kwargs):
    return prepare_request(
        client or Client(),
        ApiMessageRequest(
            model=kwargs.pop("model", MODEL),
            messages=messages or [Message.from_user_text("hello")],
            context_components=policy,
            **kwargs,
        ),
    )


def test_default_component_policy_and_old_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    settings = Settings.model_validate(
        {
            "research_memory": {"injection_budget_tokens": 777},
            "memory": {"context_window_tokens": 32000},
        }
    )
    assert list(settings.context_components.target_shares.values()) == [15, 30, 10, 20, 15, 10]
    assert sum(settings.context_components.target_shares.values()) == 100
    assert list(settings.context_components.max_shares.values()) == [30, 60, 20, 40, 100, 100]
    assert (
        settings.context_window_tokens == 32000
        and settings.research_memory.injection_budget_tokens == 777
    )
    save_settings(settings)
    assert load_settings().context_components == settings.context_components


@pytest.mark.parametrize(
    "field,change",
    [
        ("target_shares", {"memory": -1}),
        ("target_shares", {"memory": 11}),
        ("target_shares", {"memory": 10.0}),
        ("target_shares", {"memory": True}),
        ("max_shares", {"system_prompt": 101}),
        ("max_shares", {"other": -1}),
        ("max_tokens", {"other": -1}),
        ("max_tokens", {"other": 1.5}),
        ("max_tokens", {"invalid_key": 1}),
    ],
)
def test_invalid_component_policy_rejected(field, change):
    values = ContextComponentsSettings().model_dump()
    values[field].update(change)
    with pytest.raises(ValidationError):
        ContextComponentsSettings.model_validate(values)


@pytest.mark.parametrize("field", ["target_shares", "max_shares"])
def test_component_share_keys_must_be_complete(field):
    values = ContextComponentsSettings().model_dump()
    del values[field]["other"]
    with pytest.raises(ValidationError):
        ContextComponentsSettings.model_validate(values)


def test_custom_targets_absolute_overrides_and_rounding():
    policy = ContextComponentsSettings(
        target_shares=dict(zip(COMPONENT_KEYS, [20, 25, 5, 25, 15, 10])),
        max_tokens={"memory": 42, "user_message": 999999},
    )
    result = request_budget(
        component_request(policy=policy, context_window_tokens=4000, max_tokens=512)
    )
    assert result.component_targets["system_prompt"] == result.input_limit * 20 // 100
    assert sum(result.component_targets.values()) <= result.input_limit
    assert result.component_max_tokens["memory"] == 42
    assert result.component_max_tokens["user_message"] == result.input_limit


@pytest.mark.parametrize("provider", ["anthropic", "openai", "responses"])
def test_schemas_and_provider_system_attribution_are_s_and_wire_unchanged(provider):
    if provider == "anthropic":
        client = AnthropicApiClient(api_key="SECRET", claude_oauth=True)
    elif provider == "openai":
        client = OpenAICompatibleClient(api_key="SECRET")
    else:
        from researchx.api.codex_client import CodexApiClient

        client = CodexApiClient(auth_token="SECRET")
    req = component_request(
        client=client,
        model="gpt-4o",
        system_prompt="system rules\nSkill catalog",
        tools=[{"name": "fixture", "description": "tool", "input_schema": {"type": "object"}}],
    )
    result = request_budget(req)
    assert result.component_tokens["system_prompt"] >= result.components["tools"] + estimate_tokens(
        "system rules\nSkill catalog", req.model
    )
    assert req.prepared_payload["tools"] and "SECRET" not in json.dumps(result.__dict__)
    assert result.wire_input_tokens == sum(result.components.values())
    assert result.input_tokens >= result.wire_input_tokens
    assert sum(result.component_tokens.values()) == result.input_tokens


def test_current_user_identity_and_runtime_memory_dynamic_sources():
    snapshot = ContextSnapshot.join(
        [
            ("facts and evidence", "memory", "research_store", False),
            ("task plan", "dynamic_context", "tasks", False),
        ]
    )
    current = Message.from_user_text("new question").model_copy(
        update={
            "message_id": "actual-user",
            "runtime_context": snapshot.text,
            "runtime_context_manifest": snapshot.manifest,
        }
    )
    messages = [
        Message.from_user_text("old question"),
        Message(role="assistant", content=[TextBlock(text="prior answer")]),
        current,
        Message(
            role="user",
            context_origin="continuation",
            content=[TextBlock(text="continue internally")],
        ),
        Message(role="user", runtime_context="legacy runtime"),
    ]
    req = component_request(messages, current_user_message_id="actual-user")
    result = request_budget(req)
    assert result.component_tokens["user_message"] == estimate_tokens("new question", MODEL)
    assert result.component_tokens["memory"] == estimate_tokens("facts and evidence", MODEL)
    assert result.component_tokens["conversation_history"] > 0
    assert result.component_tokens["dynamic_context"] > 0 and result.attribution_fallback_count == 1
    payload = json.dumps(req.prepared_payload)
    assert (
        "context_origin" not in payload
        and "runtime_context_manifest" not in payload
        and "context_component" not in payload
    )
    assert payload.count("facts and evidence") == 1


def test_tool_call_is_h_and_marked_result_is_d_while_plain_result_is_h():
    messages = [
        Message.from_user_text("use skills"),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(id="skill1", name="skill", input={"name": "fixture"}),
                ToolUseBlock(id="plain", name="fixture", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="skill1",
                    content="full skill instructions",
                    result_metadata={"context_component": "dynamic_context"},
                ),
                ToolResultBlock(tool_use_id="plain", content="ordinary result"),
            ],
        ),
    ]
    result = request_budget(component_request(messages))
    assert result.component_tokens["dynamic_context"] == estimate_tokens(
        "full skill instructions", MODEL
    )
    assert result.component_tokens["conversation_history"] > estimate_tokens(
        "ordinary result", MODEL
    )
    assert result.component_tokens["user_message"] == estimate_tokens("use skills", MODEL)


def test_legacy_json_runtime_fallback_counts_exact_fragments_once():
    text = '<research_memory>{"plan":{"task":"check"},"evidence_pool":[{"statement":"事实"}]}</research_memory>'
    legacy = Message.model_validate(
        {"role": "user", "content": [{"type": "text", "text": "question"}], "runtime_context": text}
    )
    pieces, fallback = runtime_fragments(text, None)
    assert "".join(piece[0] for piece in pieces) == text and fallback > 0
    result = request_budget(component_request([legacy]))
    expected = estimate_tokens('[{"statement":"事实"}]', MODEL)
    assert result.component_tokens["memory"] == expected
    assert result.attribution_fallback_count >= 2
    assert Message.model_validate(legacy.model_dump()).runtime_context_manifest is None
    unknown = request_budget(
        component_request([Message(role="user", runtime_context="unclassified old text")])
    )
    assert unknown.component_tokens["memory"] == 0 and unknown.component_tokens["user_message"] == 0
    assert unknown.component_tokens["dynamic_context"] == estimate_tokens(
        "unclassified old text", MODEL
    )


def test_images_are_o_text_is_u_and_metadata_is_not_counted():
    messages = [
        Message.from_user_content(
            [
                TextBlock(text="describe the image"),
                ImageBlock(media_type="image/png", data="A" * 200000),
            ]
        )
    ]
    result = request_budget(component_request(messages))
    assert result.component_tokens["user_message"] == estimate_tokens("describe the image", MODEL)
    assert (
        result.component_tokens["other"]
        >= result.components["images"] + result.components["protocol"]
    )
    assert result.input_tokens < 10000


def test_prepared_payload_and_attribution_are_both_frozen():
    message = Message.from_user_text("original")
    req = component_request([message])
    original = request_budget(req)
    message.content[0].text = "MUTATED" * 500
    assert request_budget(req) == original and "MUTATED" not in json.dumps(req.prepared_payload)


def test_soft_u_s_overflow_can_borrow_without_editing_input_or_tools():
    policy = ContextComponentsSettings(
        max_shares={**ContextComponentsSettings().max_shares, "system_prompt": 100}
    )
    message = Message.from_user_text("question " * 1600)
    tools = [{"name": "large", "input_schema": {"type": "object", "description": "schema " * 1200}}]
    request = component_request(
        [message],
        policy=policy,
        model="gpt-4o",
        context_window_tokens=6000,
        max_tokens=512,
        tools=tools,
    )
    result = request_budget(request)
    assert result.component_overflows["user_message"]["target_exceeded"]
    assert result.component_overflows["system_prompt"]["target_exceeded"]
    result.require_fit()
    assert request.messages[0].text == message.text and request.tools == tools


@pytest.mark.parametrize("key", ["system_prompt", "user_message"])
def test_component_hard_limit_blocks_without_truncating(key):
    policy = ContextComponentsSettings(max_tokens={key: 1})
    message = Message.from_user_text("user question is preserved")
    req = component_request(
        [message],
        policy=policy,
        system_prompt="system rules preserved",
        tools=[{"name": "tool", "input_schema": {"type": "object"}}],
    )
    result = request_budget(req)
    assert result.fits and not result.components_fit
    with pytest.raises(ContextBudgetError, match=key):
        result.require_fit()
    assert message.text == req.messages[0].text and req.tools


def test_disabled_component_policy_keeps_global_hard_check():
    policy = ContextComponentsSettings(
        enabled=False, max_tokens={"system_prompt": 0, "user_message": 0}
    )
    fitting = request_budget(component_request(policy=policy, system_prompt="rules"))
    fitting.require_fit()
    large = request_budget(
        component_request(
            [Message.from_user_text("中文" * 10000)],
            policy=policy,
            context_window_tokens=4000,
            max_tokens=512,
        )
    )
    with pytest.raises(ContextBudgetError, match="可用输入"):
        large.require_fit()


@pytest.mark.asyncio
async def test_h_target_triggers_existing_compaction_and_recalculates(tmp_path):
    policy = ContextComponentsSettings(
        target_shares=dict(zip(COMPONENT_KEYS, [15, 1, 10, 49, 15, 10]))
    )
    messages = history() + [
        Message(role="assistant", content=[TextBlock(text="answer")]),
        *[Message.from_user_text("recent " + str(i)) for i in range(7)],
    ]
    original_count = len(messages)
    client = Client("<summary>Prior work condensed.</summary>")
    ctx = context(tmp_path, client, context_components=policy, tool_metadata={})
    ctx.context_window_tokens = 200000
    initial = request_budget(
        component_request(messages, policy=policy, context_window_tokens=200000, max_tokens=512)
    )
    assert initial.input_tokens < initial.trigger_tokens
    events = [event async for event, _ in run_query(ctx, messages)]
    assert isinstance(events[-1], AssistantTurnComplete)
    assert len(client.requests) == 2 and len(messages) < original_count
    report = ctx.tool_metadata["context_budget"]
    assert (
        report["component_tokens"]["conversation_history"]
        < initial.component_tokens["conversation_history"]
    )
    assert sum(report["component_tokens"].values()) == report["input_tokens"]


@pytest.mark.asyncio
async def test_h_compaction_failure_retains_original_when_hard_cap_rejects(tmp_path):
    policy = ContextComponentsSettings(max_tokens={"conversation_history": 0})
    messages = history() + [Message.from_user_text("recent " + str(i)) for i in range(7)]
    original = [message.model_dump() for message in messages]
    client = Client(RuntimeError("summary failed"))
    ctx = context(tmp_path, client, context_components=policy)
    ctx.context_window_tokens = 200000
    events = [event async for event, _ in run_query(ctx, messages)]
    assert isinstance(events[-1], ErrorEvent)
    assert [message.model_dump() for message in messages] == original
    assert client.requests and all(
        request.current_user_message_id == "" for request in client.requests
    )


@pytest.mark.asyncio
async def test_optional_dynamic_deferral_is_copy_only_and_keeps_required_evidence(tmp_path):
    snapshot = ContextSnapshot.join(
        [
            ("optional environment " * 400, "dynamic_context", "environment", True),
            ("required evidence", "dynamic_context", "reference", False),
        ]
    )
    message = Message.from_user_text("keep my question").model_copy(
        update={"runtime_context": snapshot.text, "runtime_context_manifest": snapshot.manifest}
    )
    policy = ContextComponentsSettings(
        target_shares=dict(zip(COMPONENT_KEYS, [15, 30, 10, 1, 34, 10]))
    )
    ctx = context(tmp_path, Client("done"), context_components=policy, tool_metadata={})
    messages = [message]
    events = [event async for event, _ in run_query(ctx, messages)]
    assert isinstance(events[-1], AssistantTurnComplete)
    sent = ctx.api_client.requests[-1].messages[0]
    assert (
        "optional environment" not in sent.runtime_context
        and "required evidence" in sent.runtime_context
    )
    assert message.runtime_context == snapshot.text and messages[0].runtime_context == snapshot.text
    assert ctx.tool_metadata["context_budget"]["deferred_dynamic_fragments"] == 1


@pytest.mark.asyncio
async def test_memory_disabled_does_not_replay_old_injected_memory(tmp_path):
    message = Message.from_user_text("user text").model_copy(
        update={
            "runtime_context": '<research_memory>{"evidence_pool":["old facts"]}</research_memory>'
        }
    )
    client = Client("answer")
    ctx = context(tmp_path, client, research_memory_enabled=False, tool_metadata={})
    events = [event async for event, _ in run_query(ctx, [message])]
    assert isinstance(events[-1], AssistantTurnComplete)
    assert "old facts" not in json.dumps(client.requests[0].prepared_payload)
    assert ctx.tool_metadata["context_budget"]["component_tokens"]["memory"] == 0
    assert "old facts" in message.runtime_context


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_disabled_memory_rejects_stale_recall_from_runtime_provider(tmp_path, structured):
    from researchx.services.context.sources import tagged_snapshot

    text = '<research_memory>{"evidence_pool":["stale recall"]}</research_memory>'
    client = Client("answer")
    ctx = context(tmp_path, client, research_memory_enabled=False, tool_metadata={})
    if structured:
        ctx.runtime_snapshot_provider = lambda: tagged_snapshot(text, "research_store")
    else:
        ctx.runtime_context_provider = lambda: text
    message = Message.from_user_text("actual question").model_copy(update={"runtime_context": text})
    events = [event async for event, _ in run_query(ctx, [message])]
    assert isinstance(events[-1], AssistantTurnComplete)
    assert "stale recall" not in json.dumps(client.requests[0].prepared_payload)
    assert ctx.tool_metadata["context_budget"]["component_tokens"]["memory"] == 0
    assert message.runtime_context == text


def test_tokenizer_import_failure_keeps_conservative_capacity_check(monkeypatch):
    import builtins
    from researchx.services.context.token_estimation import _encoding, counting_method

    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("tokenizer unavailable")
        return original(name, *args, **kwargs)

    _encoding.cache_clear()
    monkeypatch.setattr(builtins, "__import__", unavailable)
    try:
        assert estimate_tokens("abc中", "gpt-4o") == 4
        assert counting_method("gpt-4o") == "conservative-estimate"
        result = request_budget(component_request(model="gpt-4o"))
        result.require_fit()
        assert result.input_tokens >= result.wire_input_tokens
    finally:
        _encoding.cache_clear()


@pytest.mark.parametrize(
    "spans",
    [
        [ContextSpan(start=0, end=10000, component="memory")],
        [
            ContextSpan(start=0, end=10, component="memory"),
            ContextSpan(start=5, end=15, component="dynamic_context"),
        ],
    ],
)
def test_invalid_or_overlapping_manifests_fall_back_without_double_counting(spans):
    text = "plain runtime context"
    message = Message(role="user", runtime_context=text, runtime_context_manifest=spans)
    result = request_budget(component_request([message]))
    assert result.component_tokens["memory"] == 0
    assert result.component_tokens["dynamic_context"] == estimate_tokens(text, MODEL)
    assert result.attribution_fallback_count > 0
    assert sum(result.component_tokens.values()) == result.input_tokens


async def test_new_snapshot_manifest_round_trip_does_not_change_provider_input():
    from researchx.services.context.snapshots import save_context_snapshot

    snapshot = ContextSnapshot.join(
        [
            ("memory fact", "memory", "store", False),
            ("task state", "dynamic_context", "tasks", False),
        ]
    )
    message = Message.from_user_text("actual user").model_copy(
        update={"runtime_context": snapshot.text, "runtime_context_manifest": snapshot.manifest}
    )
    path = (await save_context_snapshot([message]))
    restored = Message.model_validate(json.loads(path.read_text())["messages"][0])
    assert restored.runtime_context_manifest == message.runtime_context_manifest
    assert restored.to_api_param() == message.to_api_param()
    assert "runtime_context_manifest" not in json.dumps(restored.to_api_param())


def test_segment_boundary_positive_difference_is_other_and_never_lowers_wire(monkeypatch):
    # Force a boundary estimator that penalizes separate text pieces to exercise
    # the conservative branch independently of whichever tokenizer is installed.
    monkeypatch.setattr(
        "researchx.services.context.budget.estimate_tokens",
        lambda text, model="": len(text) + 100 if text else 0,
    )
    snapshot = ContextSnapshot.join(
        [("a", "memory", "store", False), ("b", "dynamic_context", "tasks", False)]
    )
    message = Message.from_user_text("u").model_copy(
        update={"runtime_context": snapshot.text, "runtime_context_manifest": snapshot.manifest}
    )
    result = request_budget(component_request([message], context_window_tokens=200000))
    assert sum(result.component_tokens.values()) == result.input_tokens
    assert result.input_tokens >= result.wire_input_tokens
    assert result.component_tokens["other"] >= result.components["protocol"]


def test_current_user_attachment_body_keeps_explicit_dynamic_source():
    message = Message.from_user_content(
        [
            TextBlock(text="my instruction"),
            TextBlock(text="loaded file body", context_component="dynamic_context"),
        ]
    ).model_copy(update={"message_id": "actual"})
    result = request_budget(component_request([message], current_user_message_id="actual"))
    assert result.component_tokens["user_message"] == estimate_tokens("my instruction", MODEL)
    assert result.component_tokens["dynamic_context"] == estimate_tokens("loaded file body", MODEL)


def test_current_user_id_selects_latest_matching_submission():
    messages = [
        Message.from_user_text("previous text").model_copy(update={"message_id": "same"}),
        Message.from_user_text("current instruction").model_copy(update={"message_id": "same"}),
    ]
    result = request_budget(component_request(messages, current_user_message_id="same"))
    assert result.component_tokens["user_message"] == estimate_tokens("current instruction", MODEL)
    assert result.component_tokens["conversation_history"] == estimate_tokens(
        "previous text", MODEL
    )


async def test_runtime_rules_stay_s_when_permission_mode_changes(tmp_path):
    from researchx.prompts.context import build_runtime_prompt
    from researchx.engine.query_engine import QueryEngine

    settings = Settings(fast_mode=True)
    prompt = build_runtime_prompt(settings, cwd=tmp_path)
    engine = QueryEngine(
        api_client=Client(),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings.permission),
        cwd=tmp_path,
        model=MODEL,
        system_prompt=prompt,
        settings=settings,
        tool_metadata={"permission_mode": "plan"},
    )
    snapshot = (await engine._current_runtime_snapshot())
    s = "".join(
        snapshot.text[span.start : span.end]
        for span in snapshot.manifest
        if span.component == "system_prompt"
    )
    d = "".join(
        snapshot.text[span.start : span.end]
        for span in snapshot.manifest
        if span.component == "dynamic_context"
    )
    assert "当前已启用计划模式" in s and "当前已启用快速模式" in s and "推理设置" in s
    assert "当前已启用计划模式" not in d
    message = Message.from_user_text("question").model_copy(
        update={"runtime_context": snapshot.text, "runtime_context_manifest": snapshot.manifest}
    )
    result = request_budget(component_request([message], system_prompt=prompt.system_prompt))
    assert result.component_tokens["system_prompt"] > estimate_tokens(prompt.system_prompt, MODEL)
    assert result.attribution_fallback_count == 0


def test_replacing_recall_preserves_other_sources_and_never_duplicates_memory():
    from researchx.services.context.sources import (
        combine_snapshots,
        tagged_snapshot,
        refresh_runtime_messages,
    )

    rules = ContextSnapshot.join([("required rules", "system_prompt", "rules", False)])
    old = combine_snapshots(
        [
            rules,
            tagged_snapshot(
                '<research_memory>{"evidence_pool":["old fact"]}</research_memory>',
                "research_store",
            ),
        ]
    )
    new = tagged_snapshot(
        '<research_memory>{"evidence_pool":["new fact"]}</research_memory>', "research_store"
    )
    message = Message.from_user_text("question").model_copy(
        update={"runtime_context": old.text, "runtime_context_manifest": old.manifest}
    )
    refreshed = refresh_runtime_messages([message], new)
    payload = json.dumps([item.to_api_param() for item in refreshed])
    assert "old fact" not in payload and payload.count("new fact") == 1
    assert refreshed[0].runtime_context == "required rules"
    assert refreshed[0].runtime_context_manifest[0].component == "system_prompt"
    assert "old fact" in message.runtime_context
    result = request_budget(component_request(refreshed))
    assert result.attribution_fallback_count == 0
    assert result.component_tokens["system_prompt"] == estimate_tokens("required rules", MODEL)


@pytest.mark.parametrize("key", COMPONENT_KEYS)
def test_every_component_hard_limit_is_enforced(key):
    snapshot = ContextSnapshot.join(
        [
            ("memory evidence", "memory", "store", False),
            ("dynamic resource", "dynamic_context", "resource", False),
        ]
    )
    current = Message.from_user_text("user input").model_copy(
        update={
            "message_id": "actual",
            "runtime_context": snapshot.text,
            "runtime_context_manifest": snapshot.manifest,
        }
    )
    request = component_request(
        [Message.from_user_text("history text"), current],
        current_user_message_id="actual",
        policy=ContextComponentsSettings(max_tokens={key: 0}),
        system_prompt="system rules",
    )
    result = request_budget(request)
    assert result.component_overflows[key]["hard_limit_exceeded"]
    with pytest.raises(ContextBudgetError, match=key):
        result.require_fit()


@pytest.mark.asyncio
async def test_actual_runtime_tool_result_preserves_dynamic_marker(tmp_path):
    from researchx.tools.base import BaseTool, ToolResult
    from pydantic import BaseModel

    class Input(BaseModel):
        pass

    class ResourceTool(BaseTool):
        name, description, input_model = "load_resource", "Load an explicit resource", Input

        def is_read_only(self, arguments):
            return True

        async def execute(self, arguments, context):
            return ToolResult(
                output="RESOURCE_BODY", metadata={"context_component": "dynamic_context"}
            )

    class ToolClient(Client):
        async def stream_message(self, request):
            self.requests.append(request)
            yield ApiMessageCompleteEvent(
                message=Message(
                    role="assistant",
                    content=[ToolUseBlock(id="resource", name="load_resource", input={})],
                )
                if len(self.requests) == 1
                else Message(role="assistant", content=[TextBlock(text="done")]),
                usage=UsageSnapshot(),
            )

    client = ToolClient()
    ctx = context(tmp_path, client, tool_metadata={})
    ctx.tool_registry.register(ResourceTool())
    messages = [Message.from_user_text("load resource")]
    events = [event async for event, _ in run_query(ctx, messages)]
    assert isinstance(events[-1], AssistantTurnComplete) and len(client.requests) == 2
    results = [
        block
        for message in client.requests[-1].messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert results[0].result_metadata["context_component"] == "dynamic_context"
    assert ctx.tool_metadata["context_budget"]["component_tokens"][
        "dynamic_context"
    ] == estimate_tokens("RESOURCE_BODY", MODEL)


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["system_prompt", "user_message", "other"])
async def test_immutable_component_rejection_preserves_input_without_summary_call(
    tmp_path, component
):
    client = Client()
    ctx = context(
        tmp_path,
        client,
        context_components=ContextComponentsSettings(max_tokens={component: 0}),
        tool_metadata={},
    )
    ctx.system_prompt = "mandatory system rules"
    messages = history()
    original = [item.model_dump() for item in messages]
    events = [event async for event, _ in run_query(ctx, messages)]
    assert isinstance(events[-1], ErrorEvent) and component in events[-1].message
    assert not client.requests
    assert [item.model_dump() for item in messages] == original
    assert ctx.tool_metadata["context_budget"]["component_overflows"][component][
        "hard_limit_exceeded"
    ]
