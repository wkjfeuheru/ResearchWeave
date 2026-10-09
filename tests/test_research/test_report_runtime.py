"""Report lifecycle invariants, optimistic concurrency and version isolation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from openharness.research.completion import CompletionPolicy, ResearchContext
from openharness.research.errors import ResearchError
from openharness.research.models import (
    PlanPatch,
    PlanProposal,
    ResearchObjective,
    ResearchTask,
    TaskRevision,
    new_id,
)
from openharness.research.repository import ResearchRepository
from openharness.research.runtime import ResearchAgentRuntime
from openharness.research.store import ResearchStore
from openharness.research.tasks import validate_dag
from openharness.utils.session_files import SessionFiles
from tests.test_research.test_store import apply, reasoning


def task(key="sources", *, dependencies=None, kind="note", criterion=None):
    return ResearchTask(
        id=key,
        title=key,
        dependencies=dependencies or [],
        acceptance_criteria=[criterion or key],
        required_artifact_kinds=[kind],
    )


@pytest.fixture
def project(tmp_path):
    store = ResearchStore(tmp_path, "c" * 12, root=tmp_path / "memory")
    user = store.capture(origin_id="user", kind="user", content="生成公司财报点评")
    repo = ResearchRepository(store)
    repo.start(
        ResearchObjective(
            project_id="project_A",
            subject="公司 A",
            report_type="earnings_commentary",
            requirements=["sources", "draft"],
            deliverables=["report_draft"],
            required_sections=["摘要"],
        ),
        [user.id],
        store.load().revision,
    )
    return repo


def commit(repo, tasks=None):
    tasks = tasks or [task(), task("draft", dependencies=["sources"], kind="report_draft")]
    proposal = PlanProposal(objective_revision=1, tasks=tasks, rationale="来源先于初稿")
    return repo.commit_plan("project_A", proposal, repo.store.load().revision)


def claim(repo, key):
    memory = repo.store.load()
    selected = repo._plan(memory)
    item = next(item for item in selected.tasks if item.id == key)
    return repo.transition_task(
        key, "ready", "in_progress", memory.revision, task_revision=item.task_revision
    )


def checked_evidence(repo, key="sources"):
    execution = repo.begin_execution("fixture_source", new_id("tool"))
    receipt = repo.commit_execution(
        execution["id"],
        [{"content": "营收120百万元人民币，FY2025", "kind": "file", "locator": "fixture:earnings"}],
    )
    ev = apply(
        repo.store,
        "add_evidence",
        statement="营收120百万元人民币，FY2025",
        source_id=receipt["research_sources"][0],
    )["evidence_id"]
    step = reasoning(repo.store, [ev], verification=True)
    checked = apply(
        repo.store,
        "verify_evidence",
        evidence_id=ev,
        level="source_checked",
        method="source",
        verification_step_id=step,
        verification_note="核对离线完整披露 fixture",
    )["evidence_id"]
    return execution, checked


def artifact(repo, execution, ev, *, key="sources", kind="note", **fields):
    data = {
        "task_id": key,
        "task_revision": execution["task_revision"],
        "plan_revision": execution["plan_revision"],
        "execution_id": execution["id"],
        "kind": kind,
        "title": key,
        "content": "核对完整披露",
        "criteria": [key],
        "evidence_ids": [ev],
        **fields,
    }
    return repo.submit_artifact(data, repo.store.load().revision, new_id("op"))["artifact_id"]


def finish_sources(repo):
    claim(repo, "sources")
    execution, ev = checked_evidence(repo)
    art = artifact(repo, execution, ev)
    result = repo.request_task_completion("project_A", "sources", repo.store.load().revision, 1)
    assert result["passed"]
    return ev, art


def test_initial_plan_dag_and_legacy_file_loading(project):
    repo = project
    commit(repo)
    memory = repo.store.load()
    plan = repo._plan(memory)
    assert [item.status for item in plan.tasks] == ["ready", "pending"]
    assert plan.tasks[1].dependency_revisions == {"sources": 1}
    restored = ResearchStore(
        repo.store.directory.parent, repo.store.session_id, root=repo.store.directory.parent
    )
    assert restored.load().project.id == "project_A"
    with pytest.raises(ResearchError, match="transition|dependencies"):
        repo.transition_task("draft", "pending", "in_progress", memory.revision, task_revision=1)
    with pytest.raises(ResearchError, match="bypass"):
        apply(repo.store, "create_plan", title="绕过", tasks=["任意完成"])


@pytest.mark.parametrize(
    "tasks,match",
    [
        ([task("a", dependencies=["b"]), task("b", dependencies=["a"])], "cycle"),
        ([task("a", dependencies=["missing"])], "Missing"),
        ([task("a"), task("a")], "Duplicate"),
    ],
)
def test_bad_dags_rejected_without_commit(project, tasks, match):
    before = project.store.load().revision
    with pytest.raises(ResearchError, match=match):
        commit(project, tasks)
    assert project.store.load().revision == before
    validate_dag([task("a"), task("b", dependencies=["a"])], proposal=True)


def test_claim_is_exclusive_and_repeated_transition_rejected(project):
    commit(project, [task(), task("independent")])
    claim(project, "sources")
    with pytest.raises(ResearchError, match="active task"):
        claim(project, "independent")
    with pytest.raises(ResearchError, match="active task"):
        claim(project, "sources")


def test_evidence_missing_blocks_completion_and_project(project):
    commit(project)
    claim(project, "sources")
    execution = project.begin_execution("fixture", "missing-evidence")
    project.commit_execution(execution["id"], [])
    artifact(project, execution, ev="unused", evidence_ids=[])
    result = project.request_task_completion(
        "project_A", "sources", project.store.load().revision, 1
    )
    assert not result["passed"]
    assert any("checked evidence" in value for value in result["missing_requirements"])
    assert project._plan(project.store.load()).tasks[0].status == "in_progress"
    final = project.finalize("project_A", project.store.load().revision)
    assert not final["passed"] and project.store.load().project.status == "running"


def test_concurrent_plan_cas_only_one_writer(project):
    proposal = PlanProposal(objective_revision=1, tasks=[task()], rationale="来源")
    revision = project.store.load().revision

    def worker(_):
        try:
            project.commit_plan("project_A", proposal, revision)
            return True
        except ResearchError as exc:
            assert "conflict" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(worker, range(2))) == 1
    assert len(project.store.load().plans) == 1


def test_patch_preserves_completed_facts_and_invalidates_descendants(project):
    commit(project)
    ev, source_artifact = finish_sources(project)
    claim(project, "draft")
    execution, draft_ev = checked_evidence(project, "draft")
    draft = artifact(
        project,
        execution,
        draft_ev,
        key="draft",
        kind="report_draft",
        content=f"# 摘要\n营收增长 [E:{draft_ev}]",
        sections=["摘要"],
        input_artifact_ids=[source_artifact],
    )
    assert project.request_task_completion("project_A", "draft", project.store.load().revision, 1)[
        "passed"
    ]
    old = project._plan(project.store.load())
    project.submit_feedback("project_A", "增加储能增长预测", project.store.load().revision)
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=2,
        add_tasks=[task("storage", criterion="增加储能增长预测")],
        revise_tasks=[
            TaskRevision(
                task_id="draft",
                expected_revision=1,
                replacement=task("draft", dependencies=["sources", "storage"], kind="report_draft"),
            )
        ],
        reason="新增储能研究，修订初稿",
    )
    project.apply_plan_patch("project_A", patch, project.store.load().revision)
    memory = project.store.load()
    current = project._plan(memory)
    assert current.tasks[0].status == "completed" and current.tasks[0].task_revision == 1
    assert current.tasks[1].task_revision == 2 and current.tasks[1].artifact_ids == []
    assert current.tasks[1].dependency_revisions == {"sources": 1, "storage": 1}
    assert memory.plans[old.id].tasks[1].status == "completed"
    assert memory.artifacts[draft].stale and not memory.artifacts[source_artifact].stale
    assert not memory.evidence_pool[ev].needs_review
    with pytest.raises(ResearchError, match="Plan revision conflict"):
        project.apply_plan_patch("project_A", patch, memory.revision)


def test_upstream_revision_propagates_to_downstream(project):
    commit(project)
    _, source_artifact = finish_sources(project)
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=1,
        reason="来源口径改变",
        revise_tasks=[TaskRevision(task_id="sources", expected_revision=1, replacement=task())],
    )
    project.apply_plan_patch("project_A", patch, project.store.load().revision)
    memory = project.store.load()
    assert [item.task_revision for item in project._plan(memory).tasks] == [2, 2]
    assert memory.artifacts[source_artifact].stale
    assert memory.plans[next(iter(memory.plans))].tasks[0].status == "completed"


def test_empty_plan_after_patch_rejected(project):
    commit(project)
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=1,
        reason="取消所有工作",
        cancel_task_ids=["sources", "draft"],
    )
    with pytest.raises(ResearchError, match="No executable"):
        project.apply_plan_patch("project_A", patch, project.store.load().revision)
    assert project.store.load().project.plan_revision == 1


def test_interrupt_preserves_plan_resumes_and_discards_late_results(project):
    commit(project)
    claim(project, "sources")
    execution = project.begin_execution("slow_fetch", "old-call")
    old_plan = project._plan(project.store.load()).id
    project.store.interrupt(request_id="steer", target_request_id="old", text="增加储能")
    project.store.stopped()
    project.store.require_replan("steer")
    memory = project.store.load()
    assert memory.research_state.current_plan_id == old_plan
    assert memory.project.objective_revision == 2 and memory.research_state.replan_required
    late = project.commit_execution(execution["id"], [{"content": "旧执行返回的预测"}])
    assert not late["accepted"] and not late["research_sources"]
    assert not any(item.origin_id == "old-call" for item in project.store.load().sources.values())
    project.store.require_replan("steer")  # Idempotent recovery boundary.
    assert project.store.load().project.objective_revision == 2
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=2,
        reason="新增研究",
        add_tasks=[task("storage", criterion="增加储能")],
    )
    project.apply_plan_patch("project_A", patch, project.store.load().revision)
    project.suspend("Execution interrupted")
    project.resume("project_A")
    assert project.store.load().project.status == "running"


def test_restart_revokes_unfinished_execution_without_rerun(project):
    commit(project)
    claim(project, "sources")
    execution = project.begin_execution("fixture", "before-crash")
    restored = ResearchRepository(project.store)
    restored.recover()
    assert restored.store.load().project.status == "suspended"
    assert restored.store.load().executions[execution["id"]].status == "cancelled"
    restored.resume("project_A")
    assert restored._plan(restored.store.load()).tasks[0].status == "ready"


async def test_runtime_api_and_early_stop(project):
    runtime = ResearchAgentRuntime(project.store)
    assert not (await runtime.evaluate_stop()).passed
    commit(project)
    assert not (await runtime.finalize("project_A")).passed
    await runtime.interrupt("project_A", "Execution interrupted")
    await runtime.resume("project_A")
    await runtime.submit_feedback("project_A", "增加储能")
    assert project.store.load().project.status == "replanning"


@pytest.mark.parametrize(
    "fields,check",
    [
        ({"unit": "", "currency": "CNY", "period": "FY2025"}, "dimensions"),
        (
            {"unit": "million", "currency": "CNY", "period": "FY2025", "reproduction": ""},
            "reproducible",
        ),
    ],
)
def test_financial_artifact_quality_checks(project, fields, check):
    commit(project, [task(kind="model")])
    claim(project, "sources")
    execution, ev = checked_evidence(project)
    identifier = artifact(project, execution, ev, kind="model", **fields)
    memory = project.store.load()
    checks = CompletionPolicy().artifact_checks(
        memory.artifacts[identifier], ResearchContext(memory, project.store)
    )
    assert any(item.check_id.endswith(check) and not item.passed for item in checks)


def test_draft_snapshot_and_stale_citations_block_delivery(project):
    commit(project)
    _, source_artifact = finish_sources(project)
    claim(project, "draft")
    execution, ev = checked_evidence(project, "draft")
    identifier = artifact(
        project,
        execution,
        ev,
        key="draft",
        kind="report_draft",
        content="# 摘要\n没有可核验引用 [E:unknown]",
        sections=["摘要"],
        input_artifact_ids=[source_artifact],
    )
    result = project.request_task_completion("project_A", "draft", project.store.load().revision, 1)
    assert not result["passed"]
    memory = project.store.load()
    (project.store.directory / memory.artifacts[identifier].snapshot).write_text("tampered")
    checks = CompletionPolicy().artifact_checks(
        memory.artifacts[identifier], ResearchContext(memory, project.store)
    )
    assert any(item.check_id.endswith("snapshot") and not item.passed for item in checks)


def test_artifact_submission_is_idempotent_and_rejects_old_lease(project):
    commit(project)
    claim(project, "sources")
    execution, ev = checked_evidence(project)
    data = {
        "task_id": "sources",
        "task_revision": 1,
        "plan_revision": 1,
        "execution_id": execution["id"],
        "kind": "note",
        "title": "note",
        "content": "原文已核对",
        "criteria": ["sources"],
        "evidence_ids": [ev],
    }
    rev = project.store.load().revision
    first = project.submit_artifact(data, rev, "artifact-op")
    assert project.submit_artifact(data, rev, "artifact-op") == first
    project.suspend("Execution interrupted")
    project.resume("project_A")
    claim(project, "sources")
    with pytest.raises(ResearchError, match="active lease"):
        project.submit_artifact(data, project.store.load().revision, "new-artifact-op")


def test_project_cancel_preserves_committed_history(project):
    commit(project)
    _, identifier = finish_sources(project)
    project.cancel("project_A", "用户终止", project.store.load().revision)
    memory = project.store.load()
    assert memory.project.status == "cancelled" and identifier in memory.artifacts
    assert project._plan(memory).tasks[0].status == "completed"


async def test_two_execution_results_can_commit_under_one_task_lease(project):
    commit(project)
    claim(project, "sources")
    first = project.begin_execution("read_file", "one")
    second = project.begin_execution("read_file", "two")
    receipts = await asyncio.gather(
        *[
            asyncio.to_thread(
                project.commit_execution, execution["id"], [{"content": execution["id"]}]
            )
            for execution in (first, second)
        ]
    )
    assert all(item["accepted"] for item in receipts)
    assert first["lease_id"] == second["lease_id"]


def test_export_manifest_retains_execution_versions_and_projects_stale(project, tmp_path):
    commit(project)
    claim(project, "sources")
    execution = project.begin_execution("bash", "export")
    path = tmp_path / "draft.md"
    path.write_text("离线草稿")
    files = SessionFiles(project.store.directory)
    manifest = files.register(
        path, task_id="sources", status="draft", kind="note", execution=execution
    )
    assert files.list("artifacts")[0]["status"] == "pending_execution"
    project.commit_execution(execution["id"], [])
    assert not files.artifact(manifest["id"])[0]["stale"]
    project.apply_plan_patch(
        "project_A",
        PlanPatch(
            base_plan_revision=1,
            objective_revision=1,
            reason="修订来源任务",
            revise_tasks=[TaskRevision(task_id="sources", expected_revision=1, replacement=task())],
        ),
        project.store.load().revision,
    )
    metadata, retained_path = files.artifact(manifest["id"])
    assert (
        metadata["stale"]
        and metadata["task_revision"] == 1
        and metadata["execution_id"] == execution["id"]
    )
    assert retained_path.read_text() == "离线草稿"


def test_evidence_correction_marks_artifacts_stale_and_requires_replan(project):
    commit(project)
    ev, identifier = finish_sources(project)
    claim(project, "draft")
    source = project.store.load().evidence_pool[ev].source_id
    apply(project.store, "add_evidence", source_id=source, statement="纠正数据口径", supersedes=ev)
    memory = project.store.load()
    assert memory.artifacts[identifier].stale and memory.research_state.replan_required
    assert memory.project.status == "replanning"
    project.apply_plan_patch(
        "project_A",
        PlanPatch(base_plan_revision=1, objective_revision=1, reason="重做更正后的来源与初稿"),
        memory.revision,
    )
    assert [item.task_revision for item in project._plan(project.store.load()).tasks] == [2, 2]


def test_active_execution_must_settle_before_task_validation(project):
    commit(project)
    claim(project, "sources")
    execution = project.begin_execution("slow_tool", "active")
    with pytest.raises(ResearchError, match="settle"):
        project.request_task_completion("project_A", "sources", project.store.load().revision, 1)
    assert project.store.load().executions[execution["id"]].status == "running"


def test_planning_budget_exhaustion_requires_explicit_host_budget_change(project):
    baseline = project.reserve_planning("planner")
    project.set_planning_budget(
        "project_A", max_calls=2, max_tokens=64000, expected_revision=baseline["revision"]
    )
    project.reserve_planning("planner")
    with pytest.raises(ResearchError, match="budget exhausted"):
        project.reserve_planning("planner")
    assert project.store.load().project.status == "suspended"
    with pytest.raises(ResearchError, match="budget exhausted"):
        project.resume("project_A")
    project.set_planning_budget(
        "project_A", max_calls=3, max_tokens=64000, expected_revision=project.store.load().revision
    )
    project.resume("project_A")
    assert project.store.load().project.status == "planning"


def test_repeated_execution_commit_is_idempotent_and_conflicting_replay_rejected(project):
    commit(project)
    claim(project, "sources")
    execution = project.begin_execution("fixture", "replay")
    specs = [{"content": "实际资料"}]
    first = project.commit_execution(execution["id"], specs)
    second = project.commit_execution(execution["id"], specs)
    assert first["research_sources"] == second["research_sources"]
    assert len(project.store.load().sources) == 2  # User objective plus one tool source.
    with pytest.raises(ResearchError, match="different content"):
        project.commit_execution(execution["id"], [{"content": "不同资料"}])


def test_tampered_source_snapshot_blocks_task_completion(project):
    commit(project)
    claim(project, "sources")
    execution, ev = checked_evidence(project)
    artifact(project, execution, ev)
    memory = project.store.load()
    source = memory.sources[memory.evidence_pool[ev].source_id]
    (project.store.directory / source.snapshot).write_text("原文内容被修改")
    result = project.request_task_completion("project_A", "sources", memory.revision, 1)
    assert not result["passed"]
    assert any("source snapshots" in message for message in result["missing_requirements"])
    assert project._plan(project.store.load()).tasks[0].status == "in_progress"
