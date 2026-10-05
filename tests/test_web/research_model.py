"""Deterministic research model that drives the real memory tool and Agent loop."""

import asyncio
from uuid import uuid4

from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from openharness.research.store import ResearchStore


class ResearchModel:
    def __init__(self, cwd, session_id, *, slow=False, title="公司 A 研究"):
        self.store = ResearchStore(cwd, session_id)
        self.phase = 0
        self.slow = slow
        self.title = title
        self.requests = []

    async def close(self):
        pass

    async def stream_message(self, request):
        self.requests.append(request)
        memory = self.store.load()
        fields = None
        current_plan = memory.plans.get(memory.research_state.current_plan_id or "")
        source = list(memory.sources.values())[-1]
        if self.phase == 0:
            fields = {"action": "set_context", "goal": self.title,
                      "scope": "年报基本面", "constraints": ["仅使用提供资料"],
                      "user_source_ids": [source.id]}
        elif self.phase == 1:
            fields = {"action": "create_plan", "title": self.title, "tasks": ["收集年报", "核验与分析"]}
        elif self.phase == 2:
            fields = {"action": "update_task", "task_id": current_plan.tasks[0].id, "status": "in_progress"}
        elif self.phase == 3:
            if self.slow:
                yield ApiTextDeltaEvent("旧研究执行中")
                await asyncio.sleep(3600)
            message = ConversationMessage(role="assistant", content=[ToolUseBlock(name="read_file", input={"path": "report.txt"})])
        elif self.phase == 4:
            fields = {"action": "add_evidence", "source_id": source.id, "statement": "资料显示营收增长"}
        elif self.phase == 5:
            fields = {"action": "add_reasoning", "evidence_ids": [list(memory.evidence_pool)[-1]],
                      "method": "按原文比较同口径数据", "result": "原文描述增长", "output": "暂定增长，仍待独立核验"}
        elif self.phase == 6:
            fields = {"action": "add_conclusion", "statement": "资料描述营收增长，待核验",
                      "evidence_ids": [list(memory.evidence_pool)[-1]], "step_ids": [list(memory.reasoning_chain)[-1]]}
        elif self.phase in {7, 8}:
            fields = {"action": "update_task", "task_id": current_plan.tasks[self.phase - 7].id, "status": "completed"}
        else:
            text = f"资料显示营收增长，尚待核验 [E:{list(memory.evidence_pool)[-1]}]。"
            yield ApiTextDeltaEvent(text)
            message = ConversationMessage(role="assistant", content=[TextBlock(text=text)])
        if fields:
            operation = {"operation_id": uuid4().hex, "expected_revision": memory.revision, **fields}
            message = ConversationMessage(role="assistant", content=[ToolUseBlock(name="research_memory", input={"operation": operation})])
        self.phase += 1
        yield ApiMessageCompleteEvent(message=message, usage=UsageSnapshot(input_tokens=10, output_tokens=5))
