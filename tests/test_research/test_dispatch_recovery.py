"""Actual process death, durable recovery and explicit failed-only retry."""

import asyncio
import multiprocessing
import os
import signal
import time
from pathlib import Path

import pytest

from researchx.state.dispatch_audit import dispatch_summaries, read_batch, write_batch
from researchx.storage.database import Database, bind_database
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
        async with Database() as database:
            with bind_database(database):
                store = ResearchStore(Path(cwd), "e" * 12, root=Path(store_root))
                runtime = ResearchAgentRuntime(store, workspace_root=workspace_root)
                ctx, _ = (await context(runtime, ChildModel(wait=("unfinished",))))
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
        dispatch_id = None
        while time.monotonic() < deadline:
            summaries = await dispatch_summaries(runtime.store, "P")
            if summaries and any(item["task_id"] == "success" and item["status"] == "completed" for item in (summaries[0]["results"] or [])):
                dispatch_id = summaries[0]["dispatch_id"]
                break
            if not process.is_alive():
                pytest.fail(f"Worker exited unexpectedly: {process.exitcode}")
            await asyncio.sleep(0.05)
        assert dispatch_id is not None, "No durable candidate before deadline"
        before = (await read_batch(runtime.store, dispatch_id))
        assert before["schema_version"] == 2
        os.kill(process.pid, signal.SIGKILL)
        await asyncio.to_thread(process.join, 5)
        assert not process.is_alive()
        repository = ResearchRepository(runtime.store)
        (await repository.recover())
        recovered = (await read_batch(runtime.store, dispatch_id))
        assert recovered["status"] == "interrupted"
        assert [item["status"] for item in recovered["results"]] == ["completed", "interrupted"]
        assert recovered["results"][1]["output_paths"], "Retain partial files for main-agent review"
        success = recovered["results"][0]
        assert (
            (await runtime.resolve_workspace("P")) / success["output_paths"][0]
        ).read_text() == "candidate_success"
        first_records, revision = await read_batch(runtime.store, dispatch_id), (await runtime.store.load()).revision
        (await repository.recover())
        assert await read_batch(runtime.store, dispatch_id) == first_records
        assert (await runtime.store.load()).revision == revision
        await runtime.resume("P")
        (await repository.transition_task(
            "parent", "ready", "in_progress", (await runtime.store.load()).revision, task_revision=1
        ))
        model = ChildModel()
        ctx, _ = (await context(runtime, model))
        result = await dispatch_subagents([], ctx, before["dispatch_id"])
        assert result.dispatch_id != before["dispatch_id"]
        assert [item.task_id for item in result.results] == ["unfinished"]
        assert result.results[0].status == "completed"
        assert "success" not in model.calls
        assert not (await runtime.store.load()).artifacts
        assert len((await dispatch_summaries(runtime.store, "P"))) == 2
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 5)


async def test_recovery_does_not_revoke_live_dispatch(project):
    model = ChildModel(wait=("live",))
    ctx, _ = (await context(project, model))
    task = asyncio.create_task(dispatch_subagents(assignments("live"), ctx))
    try:
        await asyncio.wait_for(model.waiting.wait(), 10)
        before = (await project.store.load()).model_dump()
        (await project.repository.recover())
        assert (await project.store.load()).model_dump() == before
        summary = (await dispatch_summaries(project.store, "P"))[0]
        assert summary["status"] == "running"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_retry_rejects_completed_and_changed_direction(project):
    from researchx.state.errors import ResearchError

    ctx, _ = (await context(project, ChildModel(fail=("bad",))))
    result = await dispatch_subagents(assignments("ok", "bad"), ctx)
    with pytest.raises(ResearchError, match="unchanged failed"):
        await dispatch_subagents(assignments("ok"), ctx, result.dispatch_id)
    await project.submit_feedback("P", "new direction")
    with pytest.raises(ResearchError, match="direction changed"):
        await dispatch_subagents([], ctx, result.dispatch_id)


async def test_live_owner_before_initial_checkpoint_is_not_recovered(project):
    from researchx.state.dispatch_audit import lifecycle_lock

    directory = project.store.directory / "dispatches" / ("dispatch_" + "a" * 32)
    before = (await project.store.load()).model_dump()
    with lifecycle_lock(directory) as acquired:
        assert acquired
        await write_batch(project.store, {"dispatch_id": directory.name, "project_id": "P",
                                          "status": "running", "tasks": [], "results": [], "schema_version": 2})
        (await project.repository.recover())
        assert (await project.store.load()).model_dump() == before


async def test_legacy_audit_is_recovered_and_visible_through_project_tool(project):
    import json
    from researchx.tools.research_project_tool import ResearchProjectTool, ResearchProjectInput

    identifier = "dispatch_" + "b" * 32
    from researchx.storage.legacy_import import import_legacy
    from researchx.storage.database import current_database
    legacy = project.store.directory.parent / "legacy-import"
    directory = legacy / "research" / project.store.workspace_id / project.store.session_id / "dispatches" / identifier
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
    report = await import_legacy(legacy, [Path(project.store.cwd)], current_database())
    assert not report["errors"] and report["counts"]["dispatch"]["imported"] == 1
    (await project.repository.recover())
    ctx, _ = (await context(project, ChildModel()))
    result = await ResearchProjectTool().execute(
        ResearchProjectInput.model_validate({"operation": {"action": "read"}}), ctx
    )
    assert not result.is_error, result.output
    summary = json.loads(result.output)["dispatches"][0]
    assert summary["status"] == "interrupted" and summary["results"][0]["status"] == "completed"
    assert summary["candidate_authority"] == "unreviewed" and summary["requires_main_review"]
    assert not (await project.store.load()).artifacts


async def test_unknown_audit_schema_is_not_rewritten(project):
    from sqlalchemy import insert, select
    from researchx.storage.schema import dispatches
    from researchx.state.errors import ResearchError
    identifier = "dispatch_" + "c" * 32
    payload = {"dispatch_id": identifier, "project_id": "P", "status": "running", "schema_version": 999}
    async with project.store.transaction() as db:
        await db.execute(insert(dispatches).values(workspace_id=project.store.workspace_id,
            session_id=project.store.session_id, dispatch_id=identifier, status="running",
            owner="unknown", payload=payload))
    before = (await project.store.load()).model_dump()
    with pytest.raises(ResearchError, match="Unsupported"):
        await project.repository.recover()
    async with project.store.transaction() as db:
        persisted = await db.scalar(select(dispatches.c.payload).where(
            dispatches.c.workspace_id == project.store.workspace_id, dispatches.c.session_id == project.store.session_id,
            dispatches.c.dispatch_id == identifier))
    assert persisted == payload and (await project.store.load()).model_dump() == before
