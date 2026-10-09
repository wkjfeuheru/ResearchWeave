"""Nested/concurrent traces, cancellation, redaction and provider usage."""

import asyncio

import pytest

from researchx.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from researchx.api.usage import UsageSnapshot, usage_from_provider
from researchx.engine.messages import ConversationMessage
from researchx.engine.observer import NULL_OBSERVER
from researchx.evaluation.models import Budget
from researchx.evaluation.observer import (
    EvaluationBudgetExceeded,
    ObservedClient,
    RecordingObserver,
)


@pytest.mark.asyncio
async def test_parallel_spans_keep_parents_and_close_on_cancel():
    observer = RecordingObserver(secrets=["private-key"])

    async def work(name, cancel=False):
        with observer.span(
            name, kind="tool", input={"api_key": "private-key", "text": "private-key"}
        ):
            with observer.span(name + " child"):
                if cancel:
                    raise asyncio.CancelledError()
                await asyncio.sleep(0.001)

    with observer.span("root"):
        results = await asyncio.gather(work("a"), work("b", True), return_exceptions=True)
    root = observer.observations[0]
    by_name = {o.name: o for o in observer.observations}
    assert by_name["a"].parent_id == root.id and by_name["b"].parent_id == root.id
    assert by_name["a child"].parent_id == by_name["a"].id
    assert by_name["b child"].status == "cancelled"
    assert all(o.status != "running" for o in observer.observations)
    assert "private-key" not in str([o.model_dump() for o in observer.observations])
    assert isinstance(results[1], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_model_usage_counts_once_and_wrapper_preserves_transport():
    class Provider:
        custom = "preserved"

        async def stream_message(self, request):
            for _ in range(2):
                yield ApiMessageCompleteEvent(
                    message=ConversationMessage.from_user_text("ok"),
                    usage=UsageSnapshot(input_tokens=100, output_tokens=20, usage_reported=True),
                )

    observer = RecordingObserver()
    client = ObservedClient(Provider(), observer)
    assert client.custom == "preserved"
    _ = [
        event async for event in client.stream_message(ApiMessageRequest(model="test", messages=[]))
    ]
    assert len(observer.observations) == 1 and observer.total_tokens() == 120


def test_null_observer_has_no_export_and_hard_budgets_are_finite():
    with NULL_OBSERVER.span("no network") as span:
        span.update(status="ok")
    observer = RecordingObserver(budget=Budget(model_calls=1))
    with observer.span("first", kind="generation"):
        pass
    with pytest.raises(EvaluationBudgetExceeded):
        with observer.span("second", kind="generation"):
            pass
    assert usage_from_provider(None, "openai").usage_reported is False
    assert (
        usage_from_provider({"prompt_tokens": 0, "completion_tokens": 0}, "openai").usage_reported
        is True
    )
