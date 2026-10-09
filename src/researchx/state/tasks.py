"""Task DAG, revision validation and deterministic scheduling (no model calls)."""

from __future__ import annotations

from researchx.state.errors import ResearchError
from researchx.state.models import (
    PlanPatch,
    ResearchPlan,
    ResearchTask,
    ResearchArtifact,
    TaskStatus,
    new_id,
    now,
)

TERMINAL_TASKS = {"completed", "failed", "cancelled"}
TRANSITIONS = {
    "pending": {"ready", "blocked", "cancelled"},
    "ready": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"validating", "blocked", "failed", "cancelled"},
    "validating": {"completed", "in_progress", "blocked", "failed", "cancelled"},
    "blocked": {"ready", "failed", "cancelled"},
}


def validate_dag(
    tasks: list[ResearchTask], *, proposal: bool = False, require_executable: bool = False
) -> None:
    if not tasks or len(tasks) > 30:
        raise ResearchError("A plan requires 1..30 tasks")
    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ResearchError("Duplicate task IDs")
    visiting, visited = set(), set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ResearchError("Task dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        task = by_id[key]
        if len(set(task.dependencies)) != len(task.dependencies):
            raise ResearchError("Duplicate task dependencies")
        for dependency in task.dependencies:
            if dependency not in by_id:
                raise ResearchError(f"Missing task dependency: {dependency}")
            if task.status != "cancelled" and by_id[dependency].status == "cancelled":
                raise ResearchError("An active task cannot depend on a cancelled task")
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for task in tasks:
        visit(task.id)
        if proposal:
            validate_new_task(task)
    if (proposal or require_executable) and not any(task.status != "cancelled" for task in tasks):
        raise ResearchError("No executable tasks remain")


def validate_new_task(task: ResearchTask) -> None:
    if (
        task.status != "pending"
        or task.task_revision != 1
        or task.lease_id
        or task.artifact_ids
        or task.finding_ids
        or task.criterion_results
        or task.completed_at
        or task.started_at
        or task.completion_note
    ):
        raise ResearchError("Proposed tasks must be pending and contain no execution results")
    if not task.acceptance_criteria or not task.required_artifact_kinds:
        raise ResearchError(
            "Every proposed task needs acceptance criteria and required artifact kinds"
        )
    if len(set(task.acceptance_criteria)) != len(task.acceptance_criteria):
        raise ResearchError("Duplicate acceptance criteria")
    if not set(task.required_artifact_kinds) <= {
        "note",
        "dataset",
        "model",
        "chart",
        "report_draft",
    }:
        raise ResearchError("Unknown required artifact kind")


class TaskManager:
    @staticmethod
    def refresh(plan: ResearchPlan) -> None:
        by_id = {task.id: task for task in plan.tasks}
        for task in plan.tasks:
            task.dependency_revisions = {key: by_id[key].task_revision for key in task.dependencies}
            if task.status not in {"pending", "ready"}:
                continue
            satisfied = all(by_id[key].status == "completed" for key in task.dependencies)
            task.status = "ready" if satisfied else "pending"

    @staticmethod
    def get(plan: ResearchPlan, task_id: str) -> ResearchTask:
        task = next((task for task in plan.tasks if task.id == task_id), None)
        if task is None:
            raise ResearchError("Task does not belong to the current plan")
        return task

    @staticmethod
    def transition(task: ResearchTask, from_status: TaskStatus, to_status: TaskStatus) -> None:
        if task.status != from_status or to_status not in TRANSITIONS.get(from_status, set()):
            raise ResearchError(f"Invalid task transition: {task.status} -> {to_status}")
        task.status, task.updated_at = to_status, now()
        if to_status == "in_progress":
            task.lease_id = new_id("lease")
            task.started_at = task.started_at or now()
            task.blocker = ""
        if to_status in TERMINAL_TASKS | {"blocked"}:
            task.lease_id = None
        if to_status == "completed":
            task.completed_at = now()


class PlanPatchValidator:
    """Produce a new snapshot; completed tasks and old artifacts are never overwritten."""

    def apply(
        self, plan: ResearchPlan, patch: PlanPatch, artifacts: dict[str, ResearchArtifact]
    ) -> tuple[list[ResearchTask], set[str]]:
        if patch.base_plan_revision != plan.revision:
            raise ResearchError("Plan revision conflict")
        tasks = {task.id: task.model_copy(deep=True) for task in plan.tasks}
        revise_ids = [revision.task_id for revision in patch.revise_tasks]
        add_ids = [task.id for task in patch.add_tasks]
        cancel_ids = patch.cancel_task_ids
        if len(set(revise_ids + add_ids + cancel_ids)) != len(revise_ids + add_ids + cancel_ids):
            raise ResearchError("Patch operations must use distinct task IDs")
        if set(add_ids) & tasks.keys():
            raise ResearchError("Added task ID already exists")
        if not set(revise_ids + cancel_ids) <= tasks.keys():
            raise ResearchError("Unknown patched task")
        if not set(patch.invalidate_artifact_ids) <= artifacts.keys():
            raise ResearchError("Unknown invalidated artifact")
        affected = set(revise_ids + cancel_ids)
        for key in patch.invalidate_artifact_ids:
            artifact = artifacts[key]
            if (
                artifact.task_id not in tasks
                or artifact.task_revision != tasks[artifact.task_id].task_revision
            ):
                raise ResearchError("Invalidate only artifacts of a current task revision")
            affected.add(artifact.task_id)
        for revision in patch.revise_tasks:
            if revision.expected_revision != tasks[revision.task_id].task_revision:
                raise ResearchError("Task revision conflict")
            if revision.replacement.id != revision.task_id:
                raise ResearchError("Replacement must preserve logical task ID")
            validate_new_task(revision.replacement)
        while True:
            expanded = affected | {
                task.id for task in tasks.values() if set(task.dependencies) & affected
            }
            if expanded == affected:
                break
            affected = expanded
        replacements = {item.task_id: item.replacement for item in patch.revise_tasks}
        for key in affected:
            old = tasks[key]
            task = replacements.get(key, old).model_copy(deep=True)
            task.task_revision = old.task_revision + 1
            task.status = "cancelled" if key in cancel_ids else "pending"
            task.artifact_ids, task.finding_ids, task.criterion_results = [], [], {}
            task.lease_id, task.started_at, task.completed_at = None, None, None
            task.completion_note, task.blocker, task.defects = "", "", []
            task.updated_at = now()
            tasks[key] = task
        for task in patch.add_tasks:
            validate_new_task(task)
            tasks[task.id] = task.model_copy(deep=True)
        for task in tasks.values():
            task.assigned_plan_revision = plan.revision + 1
            if task.status in {"in_progress", "validating"}:
                task.status, task.lease_id = "ready", None
            if task.status == "blocked" and task.blocker in {
                "Objective revised",
                "Plan revision changed",
                "Replanning started",
                "Execution interrupted",
                "执行已停止",
                "Evidence version superseded",
            }:
                task.status, task.blocker = "pending", ""
        validate_dag(list(tasks.values()), require_executable=True)
        return list(tasks.values()), affected
