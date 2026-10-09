"""Scheme A retains research records while report state has one write authority."""

import json

import pytest

from openharness.research.errors import ResearchError
from openharness.tools.base import ToolExecutionContext
from openharness.tools.research_memory_tool import ResearchMemoryInput, ResearchMemoryTool
from tests.test_research.test_dispatch_subagents import project as project
from tests.test_research.test_conflicts import make_research


@pytest.mark.parametrize(
    "action,fields,hint",
    [
        ("set_context", {"goal": "绕过目标", "user_source_ids": []}, "research_project.feedback"),
        ("create_plan", {"title": "绕过计划", "tasks": ["伪任务"]}, "planner"),
        ("update_task", {"task_id": "parent", "status": "completed"}, "CompletionPolicy"),
    ],
)
async def test_report_legacy_mutations_return_migration_and_do_not_write(
    project, action, fields, hint
):
    if action == "set_context":
        fields = {
            **fields,
            "user_source_ids": [
                key for key, source in project.store.load().sources.items() if source.kind == "user"
            ],
        }
    operation = {
        "action": action,
        "operation_id": f"legacy-{action}",
        "expected_revision": project.store.load().revision,
        **fields,
    }
    before = project.store.load().model_dump()
    result = await ResearchMemoryTool().execute(
        ResearchMemoryInput(operation=operation),
        ToolExecutionContext(
            cwd=project.resolve_workspace("P"), metadata={"research_store": project.store}
        ),
    )
    assert result.is_error and hint in result.output and "cannot bypass Runtime" in result.output
    assert project.store.load().model_dump() == before
    with pytest.raises(ResearchError, match="bypass"):
        project.store.apply(operation)
    assert project.store.load().model_dump() == before


async def test_nonproject_memory_keeps_legacy_plan_and_task_compatibility(tmp_path):
    store, evs, steps, _ = make_research(tmp_path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store})
    current_task = store.load().research_state.current_task_id
    operation = {
        "action": "update_task",
        "operation_id": "legacy-block",
        "expected_revision": store.load().revision,
        "task_id": current_task,
        "status": "blocked",
        "blocker": "等待披露",
    }
    result = await ResearchMemoryTool().execute(ResearchMemoryInput(operation=operation), context)
    assert not result.is_error and store.load().project is None
    operation = {
        "action": "create_plan",
        "operation_id": "legacy-replan",
        "expected_revision": store.load().revision,
        "title": "后续核查",
        "tasks": ["新研究任务"],
        "reused_evidence_ids": evs,
    }
    result = await ResearchMemoryTool().execute(ResearchMemoryInput(operation=operation), context)
    assert not result.is_error and json.loads(result.output)["tasks"]
    assert all(key in store.load().evidence_pool for key in evs)
    assert all(key in store.load().reasoning_chain for key in steps)


async def test_report_conflict_investigation_uses_common_loop_and_preserves_authority(project):
    from openharness.tools.bash_tool import BashTool
    from openharness.tools.investigate_conflict_tool import (
        InvestigateConflictInput,
        InvestigateConflictTool,
    )
    from tests.test_research.test_conflicts import apply, conflict
    from tests.test_research.test_investigation import context_for, InvestigatorModel

    evidence_ids, step_ids, conclusions = [], [], []
    for index, amount in enumerate((12, 10)):
        execution = project.repository.begin_execution("fixture_source", f"source-{index}")
        source = project.repository.commit_execution(
            execution["id"],
            [
                {
                    "content": f"营收{amount}亿元",
                    "kind": "web",
                    "locator": f"https://source{index}.example/report",
                }
            ],
        )["research_sources"][0]
        evidence = apply(
            project.store, "add_evidence", source_id=source, statement=f"营收{amount}亿元"
        )["evidence_id"]
        step = apply(
            project.store,
            "add_reasoning",
            evidence_ids=[evidence],
            method="核对原始披露",
            result=f"原文披露{amount}亿元",
            output=f"营收{amount}亿元",
        )["step_id"]
        conclusion = apply(
            project.store,
            "add_conclusion",
            statement=f"营收{amount}亿元",
            evidence_ids=[evidence],
            step_ids=[step],
        )["conclusion_id"]
        evidence_ids.append(evidence)
        step_ids.append(step)
        conclusions.append(conclusion)
    research = (project.store, evidence_ids, step_ids, conclusions)
    cid = conflict(research)
    model = InvestigatorModel(research)
    context, _ = context_for(
        research, project.resolve_workspace("P"), model, research_runtime=project
    )
    context.metadata["query_context"].tool_registry.register(BashTool())
    tasks_before = project.repository._plan(project.store.load()).model_dump()
    memory_before = project.load_workspace_memory("P")
    result = await InvestigateConflictTool().execute(
        InvestigateConflictInput(conflict_id=cid), context
    )
    saved = json.loads(result.output)
    assert not result.is_error and saved["status"] == "completed" and saved["imported_ids"]
    assert "bash" not in {tool["name"] for tool in model.requests[0].tools}
    assert project.repository._plan(project.store.load()).model_dump() == tasks_before
    assert project.load_workspace_memory("P") == memory_before
    assert (project.resolve_workspace("P") / "subagents" / saved["id"] / "investigator").is_dir()
    assert project.store.load().conflicts[cid].status == "awaiting_review"
    apply(
        project.store,
        "resolve_conflict",
        conflict_id=cid,
        arbitration_id=saved["id"],
        decision=saved["report"],
    )
    assert project.store.load().conflicts[cid].status == "resolved"
