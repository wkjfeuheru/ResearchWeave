"""The real tool loop runs a bounded child and imports only validated artifacts."""

import asyncio
import json

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.query import QueryContext
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import StatusEvent
from openharness.permissions.checker import PermissionChecker
from openharness.research.models import Record
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult
from openharness.tools.investigate_conflict_tool import (
    InvestigateConflictInput,
    InvestigateConflictTool,
    InvestigationMemoryTool,
    InvestigationClient,
)
from openharness.tools.research_memory_tool import ResearchMemoryInput
from tests.test_research.test_conflicts import apply, conflict, decision, make_research


@pytest.fixture
def research(tmp_path):
    return make_research(tmp_path)


class FetchInput(Record):
    url: str


class OriginalFetch(BaseTool):
    name = "web_fetch"
    description = "Read original material"
    input_model = FetchInput

    def is_read_only(self, arguments):
        return True

    async def execute(self, arguments, context):
        return ToolResult(
            output="更正公告：营收10亿元",
            metadata={
                "research_source_specs": [
                    {
                        "kind": "web",
                        "locator": arguments.url,
                        "title": "更正公告",
                        "content": "更正公告：营收10亿元",
                    }
                ]
            },
        )


def last_result(request):
    return next(
        block
        for message in reversed(request.messages)
        for block in reversed(message.content)
        if isinstance(block, ToolResultBlock)
    )


class InvestigatorModel:
    def __init__(self, research, mode="complete", on_wait=None):
        self.research, self.mode, self.on_wait = research, mode, on_wait
        self.calls, self.requests = 0, []
        self.waiting = asyncio.Event()

    async def stream_message(self, request):
        self.calls += 1
        self.requests.append(request)
        store, evs, steps, _ = self.research
        if self.mode in {"timeout", "cancel", "budget"}:
            if self.calls == 1:
                name, data = "web_fetch", {"url": "https://original.example/correction"}
            else:
                self.waiting.set()
                if self.on_wait:
                    self.on_wait()
                await asyncio.sleep(30)
                name, data = "web_fetch", {"url": "https://original.example/correction"}
        elif self.calls == 1:
            name, data = (
                "research_memory",
                {
                    "operation": {
                        "action": "read",
                        "ids": [store.load().evidence_pool[key].source_id for key in evs]
                        + evs
                        + steps,
                        "include_content": True,
                    }
                },
            )
        elif self.calls == 2:
            read = json.loads(last_result(request).content)
            name, data = (
                "research_memory",
                {
                    "operation": {
                        "action": "add_reasoning",
                        "operation_id": "child-synthesis",
                        "expected_revision": read["revision"],
                        "evidence_ids": evs,
                        "method": "比较原始公告及更正关系",
                        "result": "两种数字来自不同版本",
                        "output": "应采用更正后的10亿元",
                    }
                },
            )
        else:
            result = json.loads(last_result(request).content)
            report = decision(evs, [result["step_id"]])
            name, data = "submit_arbitration_report", report.model_dump(mode="json")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                reasoning_content="private-model-replay",
                content=[ToolUseBlock(id=f"child-{self.calls}", name=name, input=data)],
            ),
            usage=UsageSnapshot(
                input_tokens=7,
                output_tokens=3,
                cache_read_input_tokens=2,
                cache_observed_input_tokens=7,
            ),
        )


def context_for(research, tmp_path, model, **metadata):
    store, _, _, _ = research
    registry = ToolRegistry()
    registry.register(OriginalFetch())
    registry.register(InvestigateConflictTool())
    usage = []
    query = QueryContext(
        api_client=model,
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="current-model",
        context_window_tokens=200_000,
        system_prompt="parent",
        max_tokens=1000,
        tool_metadata={"research_store": store},
        effort="high",
    )
    return ToolExecutionContext(
        cwd=tmp_path,
        metadata={
            "research_store": store,
            "query_context": query,
            "account_subagent_usage": usage.append,
            **metadata,
        },
    ), usage


async def run_tool(research, tmp_path, model, **metadata):
    cid = conflict(research)
    context, usage = context_for(research, tmp_path, model, **metadata)
    result = await InvestigateConflictTool().execute(
        InvestigateConflictInput(conflict_id=cid), context
    )
    return cid, json.loads(result.output), usage


async def test_child_reports_and_parent_commits_after_review(research, tmp_path):
    store, _, _, claims = research
    model = InvestigatorModel(research)
    cid, result, usage = await run_tool(research, tmp_path, model)
    assert result["status"] == "completed"
    assert result["report"] and not result["decision"]
    assert set(store.load().conclusions) == set(claims)
    assert len(result["imported_ids"]) == 1  # The child synthesis, not a formal conclusion.
    assert len(usage) == 3 and result["usage"]["input_tokens"] == 21
    assert all(
        request.model == "current-model" and request.effort == "high" for request in model.requests
    )
    tools = {tool["name"] for tool in model.requests[0].tools}
    assert "investigate_conflict" not in tools and "write_file" not in tools
    assert "submit_arbitration_report" in tools and "web_fetch" in tools
    saved = next((store.directory / "investigations").glob("*/*/messages.json")).read_text()
    assert "private-model-replay" not in saved and "reasoning_content" not in saved
    apply(
        store,
        "resolve_conflict",
        conflict_id=cid,
        arbitration_id=result["id"],
        decision=result["report"],
    )
    assert store.load().conflicts[cid].status == "resolved"


@pytest.mark.parametrize(
    "mode,settings,status",
    [
        ("timeout", {"conflict_timeout_seconds": 0.5}, "timeout"),
        ("budget", {"conflict_max_turns": 1}, "budget_exhausted"),
    ],
)
async def test_limits_preserve_collected_sources_and_usage(
    research, tmp_path, mode, settings, status
):
    store, _, _, claims = research
    cid, result, usage = await run_tool(
        research, tmp_path, InvestigatorModel(research, mode), **settings
    )
    assert result["status"] == status and not result["report"]
    assert len(result["imported_ids"]) == 1
    assert result["usage"]["input_tokens"] == 7 and len(usage) == 1
    assert store.load().conflicts[cid].status == "interrupted"
    assert set(store.load().conclusions) == set(claims)


async def test_cancellation_settles_child_and_preserves_material(research, tmp_path):
    store, _, _, _ = research
    cid = conflict(research)
    model = InvestigatorModel(research, "cancel")
    context, usage = context_for(research, tmp_path, model)
    task = asyncio.create_task(
        InvestigateConflictTool().execute(InvestigateConflictInput(conflict_id=cid), context)
    )
    await asyncio.wait_for(model.waiting.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    arbitration = next(iter(store.load().arbitrations.values()))
    assert arbitration.status == "interrupted" and len(arbitration.imported_ids) == 1
    assert len(usage) == 1
    assert all(source.title != "investigate_conflict" for source in store.load().sources.values())


async def test_changed_plan_does_not_receive_old_child_material(research, tmp_path):
    store, evs, _, _ = research
    model = InvestigatorModel(
        research,
        "timeout",
        on_wait=lambda: apply(
            store, "create_plan", title="新的范围", tasks=["新研究"], reused_evidence_ids=evs
        ),
    )
    _, result, _ = await run_tool(research, tmp_path, model, conflict_timeout_seconds=0.5)
    assert model.waiting.is_set()
    assert result["status"] == "stale" and not result["imported_ids"]
    assert not any(
        source.locator == "https://original.example/correction"
        for source in store.load().sources.values()
    )
    assert list((store.directory / "investigations").glob("*/*/content/*.txt"))


async def test_investigation_cannot_write_parent_conclusions_or_plan(research, tmp_path):
    store, evs, steps, _ = research
    revision = store.load().revision
    input_data = ResearchMemoryInput(
        operation={
            "action": "add_conclusion",
            "operation_id": "forbidden",
            "expected_revision": revision,
            "statement": "child conclusion",
            "evidence_ids": evs,
            "step_ids": steps,
        }
    )
    result = await InvestigationMemoryTool().execute(
        input_data, ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store})
    )
    assert result.is_error and store.load().revision == revision


async def test_model_budget_and_accounting_include_compaction_calls():
    from openharness.api.client import ApiMessageRequest
    from openharness.engine.cost_tracker import CostTracker
    from openharness.research.store import ResearchError

    class Client:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text="summary")]),
                usage=UsageSnapshot(input_tokens=10, output_tokens=2),
            )

    client, tracker, account = Client(), CostTracker(), []
    bounded = InvestigationClient(client, 2, tracker, account.append)
    # Compaction and tool-aware requests use the same bounded transport.
    for tools in ([], [{"name": "research_memory"}]):
        _ = [
            event
            async for event in bounded.stream_message(
                ApiMessageRequest(model="current", messages=[], tools=tools)
            )
        ]
    with pytest.raises(ResearchError, match="budget exhausted"):
        _ = [
            event
            async for event in bounded.stream_message(
                ApiMessageRequest(model="current", messages=[])
            )
        ]
    assert client.calls == 2 and bounded.exhausted
    assert tracker.total.input_tokens == 20 and len(account) == 2


async def test_child_hooks_share_budget_and_preserve_parent_context(research, tmp_path):
    from openharness.hooks import HookEvent, HookExecutionContext, HookExecutor
    from openharness.hooks.loader import HookRegistry
    from openharness.hooks.schemas import PromptHookDefinition

    class Model(InvestigatorModel):
        async def stream_message(self, request):
            if "hook condition" in request.system_prompt:
                yield ApiMessageCompleteEvent(
                    message=ConversationMessage.from_user_text('{"ok": true}'),
                    usage=UsageSnapshot(input_tokens=2, output_tokens=1),
                )
            else:
                async for event in super().stream_message(request):
                    yield event

    cid = conflict(research)
    model = Model(research)
    context, usage = context_for(research, tmp_path, model, conflict_max_turns=3)
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE, PromptHookDefinition(prompt="check", matcher="research_memory")
    )
    parent_hooks = HookExecutor(
        registry, HookExecutionContext(cwd=tmp_path, api_client=model, default_model="parent")
    )
    context.metadata["query_context"].hook_executor = parent_hooks
    result = await InvestigateConflictTool().execute(
        InvestigateConflictInput(conflict_id=cid), context
    )
    saved = json.loads(result.output)
    assert saved["status"] == "budget_exhausted"
    assert len(usage) == 3 and saved["usage"]["input_tokens"] == 16
    assert (
        parent_hooks._context.api_client is model
        and parent_hooks._context.default_model == "parent"
    )


class ParentAndChildModel:
    def __init__(self, research):
        self.research = research
        self.child = InvestigatorModel(research)
        self.parent_calls = 0

    async def stream_message(self, request):
        if "独立的结论冲突调查代理" in request.system_prompt:
            async for event in self.child.stream_message(request):
                yield event
            return
        self.parent_calls += 1
        store, evs, _, _ = self.research
        cid = next(iter(store.load().conflicts))
        if self.parent_calls == 1:
            # Premature draft must be repaired by the final-answer conflict guard.
            blocks = [TextBlock(text="营收确定为12亿元")]
        elif self.parent_calls == 2:
            blocks = [
                ToolUseBlock(
                    id="parent-investigate", name="investigate_conflict", input={"conflict_id": cid}
                )
            ]
        elif self.parent_calls == 3:
            arb = next(iter(store.load().arbitrations.values()))
            blocks = [
                ToolUseBlock(
                    id="parent-resolve",
                    name="research_memory",
                    input={
                        "operation": {
                            "action": "resolve_conflict",
                            "operation_id": "parent-resolve-op",
                            "expected_revision": store.load().revision,
                            "conflict_id": cid,
                            "arbitration_id": arb.id,
                            "decision": arb.report.model_dump(mode="json"),
                        }
                    },
                )
            ]
        else:
            blocks = [TextBlock(text=f"原公告已经更正，营收为10亿元[E:{evs[1]}]；采用更正版本。")]
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=blocks),
            usage=UsageSnapshot(input_tokens=10, output_tokens=5),
        )


async def test_parent_engine_repairs_draft_and_counts_child_usage(research, tmp_path):
    store, _, _, _ = research
    cid = conflict(research)
    registry = ToolRegistry()
    registry.register(InvestigateConflictTool())
    # This is the parent's unrestricted memory tool.
    from openharness.tools.research_memory_tool import ResearchMemoryTool

    registry.register(ResearchMemoryTool())
    model = ParentAndChildModel(research)
    engine = QueryEngine(
        api_client=model,
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="current-model",
        context_window_tokens=200_000,
        system_prompt="parent",
        max_turns=8,
        tool_metadata={"research_store": store},
    )
    events = [event async for event in engine.submit_message("核查争议")]
    assert store.load().conflicts[cid].status == "resolved"
    assert engine.total_usage.input_tokens == 40 + 21
    assert engine.total_usage.output_tokens == 20 + 9
    assert any(isinstance(event, StatusEvent) and "核心结论" in event.message for event in events)
    assert "营收确定为12亿元" not in engine.messages[-1].text
