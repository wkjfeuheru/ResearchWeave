"""Malformed model arguments must recover without losing evidence or weakening IDs."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.research.models import AddEvidence
from openharness.research.store import ResearchStore
from openharness.tools.research_memory_tool import ResearchMemoryInput, ResearchMemoryTool
from tests.test_research.test_engine import engine


def payload(**extra):
    return {
        "operation": {
            "action": "add_evidence",
            "operation_id": "evidence-op",
            "expected_revision": 1,
            "source_id": "src_report",
            "statement": "报告披露营业收入",
            **extra,
        }
    }


def test_empty_extra_fields_are_normalized_only_at_the_tool_boundary():
    original = payload(source_id_note="", additional_note=None)
    preserved = deepcopy(original)
    parsed = ResearchMemoryInput.model_validate(original)
    assert parsed.operation.source_id == "src_report"
    assert parsed.operation.operation_id == "evidence-op"
    assert original == preserved
    assert "source_id_note" not in parsed.operation.model_dump()
    with pytest.raises(ValidationError):
        AddEvidence.model_validate(original["operation"])
    schema = ResearchMemoryInput.model_json_schema()["$defs"]["AddEvidence"]
    assert schema["additionalProperties"] is False
    assert "source_id_note" not in schema["properties"]


@pytest.mark.parametrize(
    "extra",
    [
        {"source_id_note": "原文摘录备注"},
        {"session_id": "bbbbbbbbbbbb"},
        {"source_id_note": "", "expected_revision": "invalid"},
        {"source_id_note": "", "source_id": None},
    ],
)
def test_meaningful_extra_fields_and_required_types_remain_strict(extra):
    with pytest.raises(ValidationError):
        ResearchMemoryInput.model_validate(payload(**extra))


def test_missing_source_id_is_not_repaired_by_its_note():
    data = payload(source_id_note="")
    del data["operation"]["source_id"]
    with pytest.raises(ValidationError):
        ResearchMemoryInput.model_validate(data)


@pytest.mark.asyncio
async def test_engine_commits_empty_note_and_recovers_nonempty_note(tmp_path, caplog):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    source = store.capture(origin_id="report", kind="file", content="报告披露营业收入")

    class Model:
        phase = 0
        rejected_revision = None

        async def stream_message(self, request):
            memory = store.load()
            if self.phase == 2:
                results = [
                    block
                    for message in request.messages
                    for block in message.content
                    if isinstance(block, ToolResultBlock)
                ]
                assert results[-1].is_error
                assert "verification_note" in results[-1].content
                assert "accepts:" in results[-1].content
                assert "No write was committed" in results[-1].content
                assert memory.revision == self.rejected_revision
                assert len(memory.evidence_pool) == 1
            if self.phase < 3:
                data = payload()
                data["operation"].update(
                    source_id=source.id,
                    expected_revision=memory.revision,
                    operation_id=f"op-{self.phase}",
                )
                if self.phase == 0:
                    data["operation"]["source_id_note"] = ""
                elif self.phase == 1:
                    data["operation"]["source_id_note"] = "待核验原文"
                    self.rejected_revision = memory.revision
                else:
                    data["operation"]["verification_note"] = "待核验原文"
                message = ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(id=f"call-{self.phase}", name="research_memory", input=data)
                    ],
                )
            else:
                evidence_id = list(memory.evidence_pool)[-1]
                message = ConversationMessage(
                    role="assistant", content=[TextBlock(text=f"研究结果 [E:{evidence_id}]")]
                )
            self.phase += 1
            yield ApiMessageCompleteEvent(
                message=message, usage=UsageSnapshot(input_tokens=1, output_tokens=1)
            )

    model = Model()
    agent = engine(tmp_path, store, ResearchMemoryTool(), model)
    _ = [event async for event in agent.submit_message("分析报告")]
    memory = store.load()
    assert model.phase == 4
    assert len(memory.evidence_pool) == 2
    assert all(record.source_id == source.id for record in memory.evidence_pool.values())
    assert list(memory.evidence_pool.values())[-1].verification_note == "待核验原文"
    assert "op-1" not in memory.operations
    assert "operation.add_evidence.source_id_note" in caplog.text
