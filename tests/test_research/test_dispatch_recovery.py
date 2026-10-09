"""Actual process death, durable recovery and explicit failed-only retry."""

import asyncio
import multiprocessing
import os
import signal
import time
from pathlib import Path

import pytest

from researchx.state.dispatch_audit import dispatch_summaries, read_batch
from researchx.state.repository import ResearchRepository
from researchx.state.runtime import ResearchAgentRuntime
from researchx.state.store import ResearchStore
from researchx.tools.dispatch_subagents_tool import dispatch_subagents
from tests.test_research.test_dispatch_subagents import (
    project as project,
    context,
    assignments,
    ChildModel,
)


def _crashing_dispatch(cwd, store_root, workspace_root):
    async def execute():
        store = ResearchStore(Path(cwd), "e" * 12, root=Path(store_root))
        runtime = ResearchAgentRuntime(store, workspace_root=workspace_root)
        ctx, _ = context(runtime, ChildModel(wait=("unfinished",)))
        await dispatch_subagents(assignments("success", "unfinished"), ctx)

    asyncio.run(execute())


async def test_killed_process_preserves_candidates_and_explicit_retry(project):
    runtime = project
    process = multiprocessing.get_context("spawn").Process(
        target=_crashing_dispatch,
        args=(
            str(runtime.workspace_root.parent),
            str(runtime.store.directory.parent),
            str(runtime.workspace_root),
        ),
    )
    # ResearchStore root is the session collection, inspect concrete path in the child.
    process.start()
    try:
        deadline = time.monotonic() + 30
        batch_file = None
        while time.monotonic() < deadline:
            files = list((runtime.store.directory / "dispatches").glob("*/success/result.json"))
            if files:
                batch_file = files[0].parent.parent / "batch.json"
                break
            if not process.is_alive():
                pytest.fail(f"Worker exited unexpectedly: {process.exitcode}")
            await asyncio.sleep(0.05)
        assert batch_file is not None, "No durable candidate before deadline"
        before = read_batch(batch_file.parent)
        assert before["schema_version"] == 2
        os.kill(process.pid, signal.SIGKILL)
        await asyncio.to_thread(process.join, 5)
        assert not process.is_alive()
        repository = ResearchRepository(runtime.store)
        repository.recover()
        recovered = read_batch(batch_file.parent)
        assert recovered["status"] == "interrupted"
        assert [item["status"] for item in recovered["results"]] == ["completed", "interrupted"]
        assert recovered["results"][1]["output_paths"], "Retain partial files for main-agent review"
        success = recovered["results"][0]
        assert (
            runtime.resolve_workspace("P") / success["output_paths"][0]
        ).read_text() == "candidate_success"
        first_bytes, revision = batch_file.read_bytes(), runtime.store.load().revision
        repository.recover()
        assert batch_file.read_bytes() == first_bytes
        assert runtime.store.load().revision == revision
        await runtime.resume("P")
        repository.transition_task(
            "parent", "ready", "in_progress", runtime.store.load().revision, task_revision=1
        )
        model = ChildModel()
        ctx, _ = context(runtime, model)
        result = await dispatch_subagents([], ctx, before["dispatch_id"])
        assert result.dispatch_id != before["dispatch_id"]
        assert [item.task_id for item in result.results] == ["unfinished"]
        assert result.results[0].status == "completed"
        assert "success" not in model.calls
        assert not runtime.store.load().artifacts
        assert len(dispatch_summaries(runtime.store.directory, "P")) == 2
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 5)


async def test_recovery_does_not_revoke_live_dispatch(project):
    model = ChildModel(wait=("live",))
    ctx, _ = context(project, model)
    task = asyncio.create_task(dispatch_subagents(assignments("live"), ctx))
    try:
        await asyncio.wait_for(model.waiting.wait(), 10)
        before = project.store.load().model_dump()
        project.repository.recover()
        assert project.store.load().model_dump() == before
        summary = dispatch_summaries(project.store.directory, "P")[0]
        assert summary["status"] == "running"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_retry_rejects_completed_and_changed_direction(project):
    from researchx.state.errors import ResearchError

    ctx, _ = context(project, ChildModel(fail=("bad",)))
    result = await dispatch_subagents(assignments("ok", "bad"), ctx)
    with pytest.raises(ResearchError, match="unchanged failed"):
        await dispatch_subagents(assignments("ok"), ctx, result.dispatch_id)
    await project.submit_feedback("P", "new direction")
    with pytest.raises(ResearchError, match="direction changed"):
        await dispatch_subagents([], ctx, result.dispatch_id)


async def test_live_owner_before_initial_checkpoint_is_not_recovered(project):
    from researchx.state.dispatch_audit import lifecycle_lock

    directory = project.store.directory / "dispatches" / ("dispatch_" + "a" * 32)
    before = project.store.load().model_dump()
    with lifecycle_lock(directory) as acquired:
        assert acquired
        project.repository.recover()
        assert project.store.load().model_dump() == before


async def test_legacy_audit_is_recovered_and_visible_through_project_tool(project):
    import json
    from researchx.tools.research_project_tool import ResearchProjectTool, ResearchProjectInput

    identifier = "dispatch_" + "b" * 32
    directory = project.store.directory / "dispatches" / identifier
    child = directory / "old"
    child.mkdir(parents=True)
    # Original audit has no schema, ownership or version fields.
    (directory / "batch.json").write_text(
        json.dumps(
            {
                "dispatch_id": identifier,
                "project_id": "P",
                "parent_task_id": "parent",
                "status": "running",
                "tasks": [{"task_id": "old", "instruction": "old research", "context": None}],
            }
        )
    )
    (child / "result.json").write_text(
        json.dumps(
            {
                "task_id": "old",
                "status": "completed",
                "summary": "old candidate",
                "evidence_refs": [],
                "output_paths": [],
                "errors": [],
            }
        )
    )
    project.repository.recover()
    ctx, _ = context(project, ChildModel())
    result = await ResearchProjectTool().execute(
        ResearchProjectInput.model_validate({"operation": {"action": "read"}}), ctx
    )
    assert not result.is_error, result.output
    summary = json.loads(result.output)["dispatches"][0]
    assert summary["status"] == "interrupted" and summary["results"][0]["status"] == "completed"
    assert summary["candidate_authority"] == "unreviewed" and summary["requires_main_review"]
    assert not project.store.load().artifacts


async def test_unknown_audit_schema_is_not_rewritten(project):
    import json
    from researchx.state.errors import ResearchError

    identifier = "dispatch_" + "c" * 32
    directory = project.store.directory / "dispatches" / identifier
    directory.mkdir(parents=True)
    batch = directory / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "dispatch_id": identifier,
                "project_id": "P",
                "status": "running",
                "schema_version": 999,
            }
        )
    )
    before = batch.read_bytes(), project.store.load().model_dump()
    with pytest.raises(ResearchError, match="Unsupported"):
        project.repository.recover()
    assert (batch.read_bytes(), project.store.load().model_dump()) == before
