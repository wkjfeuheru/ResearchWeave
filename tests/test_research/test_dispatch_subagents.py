"""Finite child execution, shared concurrency, authority and workspace boundaries."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings, ResearchMemorySettings
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.query import QueryContext, _execute_tool_call
from openharness.permissions.checker import PermissionChecker
from openharness.research.errors import ResearchError
from openharness.research.models import PlanProposal, ResearchObjective
from openharness.research.runtime import ResearchAgentRuntime
from openharness.research.store import ResearchStore
from openharness.tools import RESEARCH_EXCLUDED_TOOLS, create_research_tool_registry
from openharness.tools.base import ToolExecutionContext
from openharness.tools.dispatch_subagents_tool import (
    DispatchInput,
    SubagentTask,
    dispatch_subagents,
)
from tests.test_research.test_report_runtime import checked_evidence, task


@pytest.fixture
async def project(tmp_path):
    store = ResearchStore(tmp_path, "e" * 12, root=tmp_path / "memory")
    store.capture(origin_id="user", kind="user", content="独立研究")
    runtime = ResearchAgentRuntime(store, workspace_root=tmp_path / "workspaces")
    await runtime.start(
        ResearchObjective(
            project_id="P",
            subject="公司 P",
            report_type="earnings",
            requirements=["parent"],
            deliverables=["note"],
        )
    )
    runtime.repository.commit_plan(
        "P",
        PlanProposal(objective_revision=1, tasks=[task("parent")], rationale="研究"),
        store.load().revision,
    )
    runtime.repository.transition_task(
        "parent", "ready", "in_progress", store.load().revision, task_revision=1
    )
    return runtime


def latest_result(request):
    return next(
        block
        for message in reversed(request.messages)
        for block in reversed(message.content)
        if isinstance(block, ToolResultBlock)
    )


class ChildModel:
    def __init__(
        self, *, fail=(), wait=(), initial=None, evidence_refs=None, outputs=None, delay=0.02
    ):
        self.fail, self.wait, self.initial = set(fail), set(wait), initial or {}
        self.evidence_refs, self.outputs = evidence_refs or [], outputs
        self.delay = delay
        self.calls, self.requests = {}, []
        self.active, self.peak = 0, 0
        self.waiting, self.completed, self.cancelled = asyncio.Event(), asyncio.Event(), []

    async def stream_message(self, request):
        self.requests.append(request)
        packet = json.loads(request.messages[0].text)
        key = packet["task_id"]
        call = self.calls.get(key, 0) + 1
        self.calls[key] = call
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if key in self.fail:
                raise RuntimeError(f"fixture failure: {key}")
            if key in self.wait and call >= 2:
                self.waiting.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.append(key)
                    raise
            if call == 1:
                name, arguments = self.initial.get(
                    key, ("write_file", {"path": "note.md", "content": f"candidate_{key}"})
                )
            else:
                name, arguments = (
                    "submit_subagent_result",
                    {
                        "summary": f"summary_{key}; source pending verification",
                        "evidence_refs": self.evidence_refs,
                        "output_paths": self.outputs
                        if self.outputs is not None
                        else (["note.md"] if key not in self.initial else []),
                    },
                )
                if key not in self.wait:
                    self.completed.set()
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    reasoning_content="private-replay",
                    content=[ToolUseBlock(id=f"{key}-{call}", name=name, input=arguments)],
                ),
                usage=UsageSnapshot(input_tokens=5, output_tokens=3),
            )
        finally:
            self.active -= 1


def context(runtime, model, *, checker=None, **metadata):
    registry = create_research_tool_registry()
    checker = checker or PermissionChecker(
        PermissionSettings(allowed_tools=["write_file", "edit_file"])
    )
    usage = []
    values = {
        "research_store": runtime.store,
        "research_runtime": runtime,
        "account_subagent_usage": usage.append,
        **metadata,
    }
    query = QueryContext(
        api_client=model,
        tool_registry=registry,
        permission_checker=checker,
        cwd=runtime.store.directory.parent,
        model="fixture",
        system_prompt="main",
        max_tokens=4096,
        context_window_tokens=200000,
        tool_metadata=values,
    )
    return ToolExecutionContext(
        cwd=runtime.resolve_workspace("P"), metadata={**values, "query_context": query}
    ), usage


def assignments(*keys):
    return [
        SubagentTask(task_id=key, instruction=f"independent_{key}", context=f"context_{key}")
        for key in keys
    ]


async def test_single_child_returns_real_paths_and_does_not_mutate_parent(project):
    model = ChildModel()
    ctx, usage = context(project, model)
    before = project.store.load().model_dump()
    memory = project.load_workspace_memory("P")
    result = await dispatch_subagents(assignments("one"), ctx)
    child = result.results[0]
    assert child.status == "completed" and child.summary.startswith("summary_one")
    assert len(child.output_paths) == 1
    path = project.resolve_workspace("P") / child.output_paths[0]
    assert path.read_text() == "candidate_one" and not Path(child.output_paths[0]).is_absolute()
    assert (
        project.store.load().model_dump() == before and project.load_workspace_memory("P") == memory
    )
    assert len(usage) == 2
    transcript = (
        project.store.directory / "dispatches" / result.dispatch_id / "one/messages.json"
    ).read_text()
    assert "private-replay" not in transcript and "reasoning_content" not in transcript
    child_tools = {tool["name"] for tool in model.requests[0].tools}
    assert {
        "planner",
        "replanner",
        "research_project",
        "dispatch_subagents",
        "investigate_conflict",
        "bash",
    }.isdisjoint(child_tools)
    schema = next(
        tool["input_schema"]
        for tool in model.requests[0].tools
        if tool["name"] == "research_memory"
    )
    assert schema["$defs"]["ReadMemory"]["properties"]["action"]["const"] == "read"


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_three_children_obey_config_and_have_independent_histories(project, limit):
    model = ChildModel(delay=0.04)
    ctx, _ = context(project, model, subagent_max_concurrency=limit)
    result = await dispatch_subagents(assignments("industry", "financial", "storage"), ctx)
    assert [child.task_id for child in result.results] == ["industry", "financial", "storage"]
    assert all(child.status == "completed" for child in result.results)
    assert model.peak == limit
    assert len({child.output_paths[0] for child in result.results}) == 3
    for request in model.requests:
        key = json.loads(request.messages[0].text)["task_id"]
        text = " ".join(message.text for message in request.messages)
        for other in {"industry", "financial", "storage"} - {key}:
            assert f"independent_{other}" not in text and f"candidate_{other}" not in text


async def test_concurrency_limit_is_shared_across_dispatch_calls(project):
    model = ChildModel(delay=0.04)
    ctx, _ = context(project, model, subagent_max_concurrency=2)
    first, second = await asyncio.gather(
        dispatch_subagents(assignments("a", "b"), ctx),
        dispatch_subagents(assignments("c", "d"), ctx),
    )
    assert model.peak == 2 and first.dispatch_id != second.dispatch_id
    assert all(item.status == "completed" for item in first.results + second.results)


async def test_failure_isolated_and_results_keep_input_order(project):
    model = ChildModel(fail=["bad"])
    ctx, _ = context(project, model)
    result = await dispatch_subagents(assignments("good", "bad", "also_good"), ctx)
    assert [child.status for child in result.results] == ["completed", "failed", "completed"]
    assert "fixture failure" in result.results[1].errors[0]
    assert result.results[0].output_paths and result.results[2].output_paths


async def test_parent_cancellation_settles_active_and_queued_children(project):
    model = ChildModel(wait=["active"])
    ctx, _ = context(project, model, subagent_max_concurrency=1)
    pending = asyncio.create_task(dispatch_subagents(assignments("active", "queued"), ctx))
    await asyncio.wait_for(model.waiting.wait(), timeout=3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert model.cancelled == ["active"] and model.active == 0 and "queued" not in model.calls
    audit = json.loads(
        next((project.store.directory / "dispatches").glob("*/batch.json")).read_text()
    )
    assert audit["status"] == "cancelled" and [item["status"] for item in audit["results"]] == [
        "cancelled",
        "cancelled",
    ]
    path = project.resolve_workspace("P") / audit["results"][0]["output_paths"][0]
    assert path.read_text() == "candidate_active"


async def test_completed_sibling_is_preserved_when_parent_is_cancelled(project):
    model = ChildModel(wait=["slow"])
    ctx, _ = context(project, model)
    pending = asyncio.create_task(dispatch_subagents(assignments("fast", "slow"), ctx))
    await asyncio.wait_for(model.waiting.wait(), timeout=3)
    await asyncio.sleep(0.05)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    audit = json.loads(
        next((project.store.directory / "dispatches").glob("*/batch.json")).read_text()
    )
    assert [item["status"] for item in audit["results"]] == ["completed", "cancelled"]
    assert (project.resolve_workspace("P") / audit["results"][0]["output_paths"][0]).is_file()


@pytest.mark.parametrize(
    "settings,error",
    [
        ({"subagent_timeout_seconds": 0.08}, "TimeoutError"),
        ({"subagent_max_calls": 1}, "MaxTurnsExceeded"),
    ],
)
async def test_limits_fail_only_the_child_and_preserve_files(project, settings, error):
    model = ChildModel(wait=["one"])
    ctx, _ = context(project, model, **settings)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and error in result.results[0].errors[0]
    assert result.results[0].output_paths and model.active == 0


@pytest.mark.parametrize("key", ["../escape", "/tmp/absolute", "x/y", "x.y", "", "a" * 65])
def test_task_paths_are_not_model_controlled(key):
    with pytest.raises(ValidationError):
        DispatchInput(tasks=[{"task_id": key, "instruction": "research"}])


def test_batch_size_and_duplicate_ids_are_rejected():
    for values in [[], assignments("a", "a"), assignments(*(f"x{i}" for i in range(17)))]:
        with pytest.raises(ValidationError):
            DispatchInput(tasks=values)


@pytest.mark.parametrize(
    "tool", ["planner", "replanner", "research_project", "dispatch_subagents", "bash"]
)
async def test_child_cannot_invoke_authoritative_or_recursive_tools(project, tool):
    model = ChildModel(initial={"one": (tool, {})})
    ctx, _ = context(project, model)
    before = project.store.load().model_dump()
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "completed"
    call = latest_result(model.requests[1])
    assert call.is_error and "Unknown tool" in call.content
    assert project.store.load().model_dump() == before


@pytest.mark.parametrize(
    "action", ["set_context", "create_plan", "update_task", "add_evidence", "add_conclusion"]
)
async def test_child_memory_schema_exposes_only_read(project, action):
    model = ChildModel(initial={"one": ("research_memory", {"operation": {"action": action}})})
    ctx, _ = context(project, model)
    before = project.store.load().revision
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "completed" and latest_result(model.requests[1]).is_error
    assert project.store.load().revision == before


@pytest.mark.parametrize(
    "kind", ["main_memory", "parent_artifact", "outside", "traversal", "symlink"]
)
async def test_child_file_writes_are_confined_to_own_directory(project, tmp_path, kind):
    workspace = project.resolve_workspace("P")
    before = project.load_workspace_memory("P")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "artifacts/link.txt").symlink_to(outside)
    candidate = {
        "main_memory": str(workspace / "MEMORY.md"),
        "parent_artifact": str(workspace / "artifacts/main.txt"),
        "outside": str(outside),
        "traversal": "../escaped.txt",
        "symlink": str(workspace / "artifacts/link.txt"),
    }[kind]
    model = ChildModel(initial={"one": ("write_file", {"path": candidate, "content": "overwrite"})})
    ctx, _ = context(project, model)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "completed" and latest_result(model.requests[1]).is_error
    assert project.load_workspace_memory("P") == before and outside.read_text() == "outside"
    assert not (workspace / "artifacts/main.txt").exists()


async def test_child_can_read_main_memory_and_read_only_evidence(project):
    _, evidence = checked_evidence(project.repository, "parent")
    for name, values in [
        ("read_file", {"path": str(project.resolve_workspace("P") / "MEMORY.md")}),
        (
            "research_memory",
            {"operation": {"action": "read", "ids": [evidence], "include_content": True}},
        ),
    ]:
        model = ChildModel(initial={"one": (name, values)}, evidence_refs=[evidence])
        ctx, _ = context(project, model)
        result = await dispatch_subagents(assignments("one"), ctx)
        assert result.results[0].status == "completed" and result.results[0].evidence_refs == [
            evidence
        ]
        assert not latest_result(model.requests[1]).is_error


@pytest.mark.parametrize(
    "outputs,refs,error",
    [
        (["missing.md"], [], "does not exist"),
        (["../other.md"], [], "traversal"),
        (["/tmp/other.md"], [], "must be relative"),
        ([], ["ev_fabricated"], "Unverifiable"),
    ],
)
async def test_fabricated_paths_and_evidence_are_not_returned_as_success(
    project, outputs, refs, error
):
    model = ChildModel(evidence_refs=refs, outputs=outputs)
    ctx, _ = context(project, model, subagent_max_calls=2)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and not result.results[0].evidence_refs
    audit = (
        project.store.directory / "dispatches" / result.dispatch_id / "one/messages.json"
    ).read_text()
    # Failed submission is present in the public transcript even when the turn cap stops repair.
    assert error in audit


async def test_repeated_dispatches_never_overwrite_previous_outputs(project):
    ctx, _ = context(project, ChildModel())
    first = await dispatch_subagents(assignments("one"), ctx)
    ctx.metadata["query_context"].api_client = ChildModel()
    second = await dispatch_subagents(assignments("one"), ctx)
    assert first.dispatch_id != second.dispatch_id
    assert first.results[0].output_paths != second.results[0].output_paths
    assert (
        project.resolve_workspace("P") / first.results[0].output_paths[0]
    ).read_text() == "candidate_one"


async def test_recursion_is_rejected_even_with_direct_function_call(project):
    ctx, _ = context(project, ChildModel(), subagent_child=True)
    with pytest.raises(ResearchError, match="Recursive"):
        await dispatch_subagents(assignments("one"), ctx)


async def test_parent_permission_remains_effective(project):
    model = ChildModel()
    checker = PermissionChecker(PermissionSettings(denied_tools=["write_file"]))
    ctx, _ = context(project, model, checker=checker)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert (
        result.results[0].status == "failed" and "禁止" in latest_result(model.requests[1]).content
    )
    assert not list(project.resolve_workspace("P").glob("subagents/*/one/note.md"))


async def test_real_tool_loop_binds_dispatch_to_parent_execution(project):
    model = ChildModel()
    ctx, _ = context(project, model)
    query = ctx.metadata["query_context"]
    result = await _execute_tool_call(
        query,
        "dispatch_subagents",
        "parent-dispatch",
        {"tasks": [task.model_dump(mode="json") for task in assignments("one")]},
    )
    assert not result.is_error
    execution = project.store.load().executions[result.result_metadata["execution_id"]]
    assert execution.status == "committed" and execution.task_id == "parent"
    assert project.repository._plan(project.store.load()).tasks[0].status == "in_progress"


async def test_removed_tools_are_uncallable_and_undiscoverable_in_research_mode(project):
    ctx, _ = context(project, ChildModel())
    registry = ctx.metadata["query_context"].tool_registry
    search = registry.get("tool_search")
    for name in RESEARCH_EXCLUDED_TOOLS:
        assert registry.get(name) is None and name not in {
            tool["name"] for tool in registry.to_api_schema()
        }
        result = await search.execute(
            search.input_model(query=name),
            ToolExecutionContext(cwd=ctx.cwd, metadata={"tool_registry": registry}),
        )
        assert name not in {line.split(":", 1)[0] for line in result.output.splitlines()}
        result = await _execute_tool_call(
            ctx.metadata["query_context"], name, f"removed-{name}", {}
        )
        assert result.is_error and "Unknown tool" in result.content
    assert {"planner", "replanner", "dispatch_subagents"} <= {
        tool.name for tool in registry.list_tools()
    }
    general = create_research_tool_registry(mode="general")
    assert RESEARCH_EXCLUDED_TOOLS.isdisjoint(tool.name for tool in general.list_tools())
    assert {tool.name for tool in general.list_tools()} == {
        tool.name for tool in registry.list_tools()
    }


def test_subagent_config_is_small_and_bounded():
    config = ResearchMemorySettings()
    assert config.subagent_max_concurrency == 3 and config.subagent_max_calls == 12
    with pytest.raises(ValidationError):
        ResearchMemorySettings(subagent_max_concurrency=0)


@pytest.mark.parametrize("kind", ["sibling", "other_project", "symlink", "traversal"])
async def test_child_cannot_read_unauthorized_files(project, tmp_path, kind):
    workspace = project.resolve_workspace("P")
    sibling = workspace / "subagents/old-dispatch/sibling/private.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("SIBLING_PRIVATE", encoding="utf-8")
    other = tmp_path / "workspaces/other_project/MEMORY.md"
    other.parent.mkdir(parents=True)
    other.write_text("OTHER_PROJECT_PRIVATE", encoding="utf-8")
    link = workspace / "artifacts/link.md"
    link.symlink_to(other)
    path = {
        "sibling": str(sibling),
        "other_project": str(other),
        "symlink": str(link),
        "traversal": "../private.md",
    }[kind]
    model = ChildModel(initial={"one": ("read_file", {"path": path})})
    ctx, _ = context(project, model)
    result = await dispatch_subagents(assignments("one"), ctx)
    receipt = latest_result(model.requests[1])
    assert result.results[0].status == "completed" and receipt.is_error
    assert (
        "SIBLING_PRIVATE" not in receipt.content and "OTHER_PROJECT_PRIVATE" not in receipt.content
    )


@pytest.mark.parametrize("name", ["glob", "grep"])
async def test_child_search_filters_sibling_and_symlink_content(project, tmp_path, name):
    workspace = project.resolve_workspace("P")
    visible = workspace / "artifacts/shared.md"
    visible.write_text("SEARCH_VISIBLE", encoding="utf-8")
    sibling = workspace / "subagents/old-dispatch/sibling/private.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("SEARCH_SIBLING_PRIVATE", encoding="utf-8")
    private = tmp_path / "private.md"
    private.write_text("SEARCH_LINK_PRIVATE", encoding="utf-8")
    (workspace / "artifacts/link.md").symlink_to(private)
    arguments = {"root": str(workspace), "pattern": "**/*.md" if name == "glob" else "SEARCH_"}
    model = ChildModel(initial={"one": (name, arguments)})
    ctx, _ = context(project, model)
    result = await dispatch_subagents(assignments("one"), ctx)
    receipt = latest_result(model.requests[1])
    assert result.results[0].status == "completed" and not receipt.is_error
    assert (
        "shared.md" in receipt.content
        and "private.md" not in receipt.content
        and "link.md" not in receipt.content
    )
    assert (
        "SEARCH_SIBLING_PRIVATE" not in receipt.content
        and "SEARCH_LINK_PRIVATE" not in receipt.content
    )


@pytest.mark.parametrize("change", ["corrupt", "supersede"])
async def test_candidate_evidence_checks_hash_and_current_version(project, change):
    from tests.test_research.test_conflicts import apply

    _, evidence_id = checked_evidence(project.repository, "parent")
    evidence = project.store.load().evidence_pool[evidence_id]
    if change == "corrupt":
        source = project.store.load().sources[evidence.source_id]
        (project.store.directory / source.snapshot).write_text("tampered", encoding="utf-8")
    else:
        apply(
            project.store,
            "add_evidence",
            source_id=evidence.source_id,
            statement="新版待核实",
            supersedes=evidence_id,
        )
    model = ChildModel(evidence_refs=[evidence_id])
    ctx, _ = context(project, model, subagent_max_calls=2)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and not result.results[0].evidence_refs


async def test_plain_answer_is_not_accepted_as_structured_success(project):
    class PlainModel:
        async def stream_message(self, request):
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text="done")]),
                usage=UsageSnapshot(input_tokens=2, output_tokens=1),
            )

    ctx, _ = context(project, PlainModel())
    result = await dispatch_subagents(assignments("one"), ctx)
    assert (
        result.results[0].status == "failed"
        and "without a validated" in result.results[0].errors[0]
    )


async def test_child_prompt_hooks_share_budget_and_keep_parent_context(project):
    from openharness.hooks import HookEvent, HookExecutionContext, HookExecutor
    from openharness.hooks.loader import HookRegistry
    from openharness.hooks.schemas import PromptHookDefinition

    class HookModel(ChildModel):
        async def stream_message(self, request):
            if "hook condition" in request.system_prompt:
                yield ApiMessageCompleteEvent(
                    message=ConversationMessage.from_user_text('{"ok": true}'),
                    usage=UsageSnapshot(input_tokens=2, output_tokens=1),
                )
            else:
                async for event in super().stream_message(request):
                    yield event

    model = HookModel()
    ctx, usage = context(project, model, subagent_max_calls=2)
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE, PromptHookDefinition(prompt="check", matcher="write_file")
    )
    hooks = HookExecutor(
        registry, HookExecutionContext(cwd=ctx.cwd, api_client=model, default_model="parent")
    )
    ctx.metadata["query_context"].hook_executor = hooks
    result = await dispatch_subagents(assignments("one"), ctx)
    assert (
        result.results[0].status == "failed" and "MaxTurnsExceeded" in result.results[0].errors[0]
    )
    assert len(usage) == 2 and sum(item.input_tokens for item in usage) == 7
    assert hooks._context.api_client is model and hooks._context.default_model == "parent"


async def test_scope_change_during_edit_approval_revokes_child_write(project):
    async def approve(path, diff, added, removed):
        await project.submit_feedback("P", "请调整研究目标并重新规划")
        return "accept"

    model = ChildModel()
    ctx, _ = context(project, model, edit_approval_prompt=approve, subagent_max_calls=2)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and not result.results[0].output_paths
    assert (
        latest_result(model.requests[1]).is_error
        and "revoked parent scope" in latest_result(model.requests[1]).content
    )
    assert project.store.load().project.objective_revision == 2
    assert not list(project.resolve_workspace("P").glob("subagents/*/one/note.md"))


async def test_generic_child_cannot_call_planning_helper_directly(project):
    from openharness.tools.research.planner import planner_tool

    ctx, _ = context(project, ChildModel(), subagent_child=True)
    with pytest.raises(ResearchError, match="child recursion"):
        await planner_tool("replace", ctx)


async def test_cancelled_parent_execution_restores_workspace_and_partial_candidates(
    project, tmp_path
):
    model = ChildModel(wait=["one"])
    ctx, _ = context(project, model)
    pending = asyncio.create_task(
        _execute_tool_call(
            ctx.metadata["query_context"],
            "dispatch_subagents",
            "interrupted",
            {"tasks": [item.model_dump(mode="json") for item in assignments("one")]},
        )
    )
    await asyncio.wait_for(model.waiting.wait(), timeout=3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await project.interrupt("P", "Execution interrupted")
    saved_workspace = project.resolve_workspace("P")
    memory_before = project.load_workspace_memory("P")
    restored_store = ResearchStore(
        tmp_path, project.store.session_id, root=project.store.directory.parent
    )
    restored = ResearchAgentRuntime(restored_store, workspace_root=tmp_path / "changed-default")
    await restored.resume("P")
    assert restored.resolve_workspace("P") == saved_workspace
    assert restored.load_workspace_memory("P") == memory_before
    assert restored.repository._plan(restored.store.load()).tasks[0].status == "ready"
    execution = next(
        item
        for item in restored.store.load().executions.values()
        if item.tool_use_id == "interrupted"
    )
    assert execution.status == "cancelled"
    assert not any(
        source.title == "dispatch_subagents" for source in restored.store.load().sources.values()
    )
    batch = json.loads(
        next((restored.store.directory / "dispatches").glob("*/batch.json")).read_text()
    )
    assert (
        batch["status"] == "cancelled"
        and (saved_workspace / batch["results"][0]["output_paths"][0]).is_file()
    )
    restored.repository.transition_task(
        "parent", "ready", "in_progress", restored.store.load().revision, task_revision=1
    )
    ctx, _ = context(restored, ChildModel())
    retry = await dispatch_subagents(assignments("one"), ctx)
    assert retry.results[0].status == "completed" and retry.dispatch_id != batch["dispatch_id"]
