"""Conflict review in the retained main loop, plus the shared bounded child transport.

The dedicated investigator was retired. Reports reference registered material and are
submitted/reviewed through research_memory; dispatch isolation is covered separately.
"""

import asyncio
import json

import pytest

from researchx.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from researchx.api.usage import UsageSnapshot
from researchx.config.settings import PermissionSettings
from researchx.engine.cost_tracker import CostTracker
from researchx.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from researchx.engine.query_engine import QueryEngine
from researchx.engine.stream_events import StatusEvent
from researchx.engine.subagents import BoundedSubagentClient
from researchx.permissions.checker import PermissionChecker
from researchx.state.errors import ResearchError
from researchx.tools.base import ToolExecutionContext, ToolRegistry
from researchx.tools.research_memory_tool import ResearchMemoryInput, ResearchMemoryTool
from tests.test_research.test_conflicts import apply, conflict, decision, make_research


@pytest.fixture
def research(tmp_path):
    return make_research(tmp_path)


def report_operation(research, cid, **fields):
    store, evs, steps, _ = research
    return {
        "action": "submit_conflict_report",
        "operation_id": "main-report",
        "expected_revision": store.load().revision,
        "conflict_id": cid,
        "report": decision(evs, steps).model_dump(mode="json"),
        **fields,
    }


async def test_main_reports_and_separately_commits_after_review(research, tmp_path):
    store, _, _, claims = research
    cid = conflict(research)
    operation = report_operation(research, cid)
    tool = ResearchMemoryTool()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store})
    result = await tool.execute(ResearchMemoryInput(operation=operation), context)
    saved = json.loads(result.output)
    assert not result.is_error
    arbitration = store.load().arbitrations[saved["arbitration_id"]]
    assert arbitration.status == "completed" and arbitration.report and not arbitration.decision
    assert store.load().conflicts[cid].status == "awaiting_review"
    assert set(store.load().conclusions) == set(claims)
    assert not arbitration.imported_ids
    apply(
        store,
        "resolve_conflict",
        conflict_id=cid,
        arbitration_id=arbitration.id,
        decision=arbitration.report.model_dump(mode="json"),
    )
    assert store.load().conflicts[cid].status == "resolved"
    assert len(store.load().conclusions) == len(claims) + 1


@pytest.mark.parametrize(
    "invalid", ["missing_side", "unknown_evidence", "unused_evidence", "bad_preferred_side"]
)
async def test_invalid_reports_are_atomic_and_do_not_clear_review(research, tmp_path, invalid):
    store, evs, steps, _ = research
    cid = conflict(research)
    operation = report_operation(research, cid)
    report = operation["report"]
    if invalid == "missing_side":
        report["assessments"] = report["assessments"][:1]
    elif invalid == "unknown_evidence":
        report["evidence_ids"] = ["ev_not_registered"]
    elif invalid == "unused_evidence":
        report["step_ids"] = steps[:1]
    else:
        report["preferred_side"] = 4
    before = store.load().model_dump()
    result = await ResearchMemoryTool().execute(
        ResearchMemoryInput(operation=operation),
        ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store}),
    )
    assert result.is_error
    assert store.load().model_dump() == before
    assert all(item.needs_review for item in store.load().conclusions.values())


def test_report_idempotency_and_no_automatic_repeat(research):
    store, _, _, _ = research
    cid = conflict(research)
    op = report_operation(research, cid)
    first = store.apply(op)
    revision = store.load().revision
    replay = store.apply(op)
    assert first["arbitration_id"] == replay["arbitration_id"]
    assert store.load().revision == revision and len(store.load().arbitrations) == 1
    with pytest.raises(ResearchError, match="Review the existing report"):
        store.apply(report_operation(research, cid, operation_id="duplicate-report"))
    apply(store, "reopen_conflict", conflict_id=cid, reason="用户要求按新口径复核")
    second = store.apply(report_operation(research, cid, operation_id="review-again"))
    assert second["arbitration_id"] != first["arbitration_id"]
    assert len(store.load().arbitrations) == 2


def test_revision_or_scope_change_rejects_old_report(research):
    store, evs, _, _ = research
    cid = conflict(research)
    op = report_operation(research, cid)
    apply(store, "reopen_conflict", conflict_id=cid, reason="补充核查")
    with pytest.raises(ResearchError, match="Revision conflict"):
        store.apply(op)
    apply(store, "create_plan", title="新的范围", tasks=["新研究"], reused_evidence_ids=evs)
    with pytest.raises(ResearchError, match="current research scope"):
        store.apply(report_operation(research, cid))
    assert not store.load().arbitrations


def test_running_legacy_investigation_blocks_new_report_until_host_recovery(research):
    store, _, _, _ = research
    cid = conflict(research)
    arb, _ = store.begin_investigation(cid)
    with pytest.raises(ResearchError, match="still running"):
        store.apply(report_operation(research, cid))
    store.recover_investigations()
    assert store.load().arbitrations[arb.id].status == "interrupted"
    store.apply(report_operation(research, cid))
    assert store.load().conflicts[cid].status == "awaiting_review"


def test_reopened_report_cannot_resolve_stale_decision(research):
    store, _, _, _ = research
    cid = conflict(research)
    receipt = store.apply(report_operation(research, cid))
    apply(store, "reopen_conflict", conflict_id=cid, reason="新的反证需要复核")
    before = store.load().model_dump()
    with pytest.raises(ResearchError, match="latest completed"):
        apply(
            store,
            "resolve_conflict",
            conflict_id=cid,
            arbitration_id=receipt["arbitration_id"],
            decision=report_operation(research, cid)["report"],
        )
    assert store.load().model_dump() == before


async def test_readonly_child_cannot_submit_report_or_parent_mutations(research, tmp_path):
    from researchx.tools.dispatch_subagents_tool import ReadOnlyResearchMemoryTool
    from pydantic import ValidationError

    store, _, _, _ = research
    cid = conflict(research)
    tool = ReadOnlyResearchMemoryTool(store)
    before = store.load().model_dump()
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"operation": report_operation(research, cid)})
    assert store.load().model_dump() == before
    result = await tool.execute(
        tool.input_model(operation={"action": "read"}), ToolExecutionContext(cwd=tmp_path)
    )
    assert not result.is_error


async def test_model_budget_and_accounting_include_compaction_calls():
    class Client:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text="summary")]),
                usage=UsageSnapshot(input_tokens=10, output_tokens=2),
            )

    client, tracker, account = Client(), CostTracker(), []
    bounded = BoundedSubagentClient(client, 2, tracker, account.append)
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


async def test_bounded_child_cancellation_preserves_completed_usage():
    waiting = asyncio.Event()

    class Client:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            if self.calls == 2:
                waiting.set()
                await asyncio.sleep(30)
            yield ApiMessageCompleteEvent(
                message=ConversationMessage.from_user_text("material"),
                usage=UsageSnapshot(input_tokens=7, output_tokens=3),
            )

    tracker, account = CostTracker(), []
    bounded = BoundedSubagentClient(Client(), 3, tracker, account.append)
    request = ApiMessageRequest(model="current", messages=[])
    _ = [event async for event in bounded.stream_message(request)]

    async def consume():
        return [event async for event in bounded.stream_message(request)]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bounded.calls == 2 and len(account) == 1
    assert tracker.total.input_tokens == 7


class MainReviewModel:
    def __init__(self, research):
        self.research = research
        self.calls, self.requests = 0, []

    async def stream_message(self, request):
        self.calls += 1
        self.requests.append(request)
        store, evs, _, _ = self.research
        cid = next(iter(store.load().conflicts))
        if self.calls == 1:
            blocks = [TextBlock(text="营收确定为12亿元")]
        elif self.calls == 2:
            blocks = [
                ToolUseBlock(
                    id="main-report",
                    name="research_memory",
                    input={"operation": report_operation(self.research, cid)},
                )
            ]
        elif self.calls == 3:
            arb = next(iter(store.load().arbitrations.values()))
            blocks = [
                ToolUseBlock(
                    id="main-resolve",
                    name="research_memory",
                    input={
                        "operation": {
                            "action": "resolve_conflict",
                            "operation_id": "main-resolve",
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


async def run_main_review(research, tmp_path, runtime=None):
    store, _, _, _ = research
    registry = ToolRegistry()
    registry.register(ResearchMemoryTool())
    metadata = {"research_store": store}
    if runtime:
        metadata["research_runtime"] = runtime
    model = MainReviewModel(research)
    engine = QueryEngine(
        api_client=model,
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="current-model",
        context_window_tokens=200_000,
        system_prompt="parent",
        max_turns=8,
        tool_metadata=metadata,
    )
    events = [event async for event in engine.submit_message("核查争议")]
    return engine, model, events


async def test_parent_engine_repairs_draft_and_counts_usage(research, tmp_path):
    store, _, _, _ = research
    cid = conflict(research)
    engine, model, events = await run_main_review(research, tmp_path)
    assert store.load().conflicts[cid].status == "resolved"
    assert engine.total_usage.input_tokens == 40 and engine.total_usage.output_tokens == 20
    assert model.calls == 4
    assert any(isinstance(event, StatusEvent) and "核心结论" in event.message for event in events)
    assert "营收确定为12亿元" not in engine.messages[-1].text
    assert all(
        "investigate_conflict" not in {tool["name"] for tool in request.tools}
        for request in model.requests
    )


def test_unresolved_decision_requires_explicit_reopen_before_another_report(research):
    store, evs, steps, _ = research
    cid = conflict(research)
    report = decision(evs, steps, "unresolved").model_dump(mode="json")
    saved = store.apply(report_operation(research, cid, report=report))
    apply(
        store,
        "resolve_conflict",
        conflict_id=cid,
        arbitration_id=saved["arbitration_id"],
        decision=report,
    )
    before = store.load().model_dump()
    assert store.load().conflicts[cid].status == "unresolved"
    with pytest.raises(ResearchError, match="reopen"):
        store.apply(report_operation(research, cid, operation_id="no-new-material"))
    assert store.load().model_dump() == before
