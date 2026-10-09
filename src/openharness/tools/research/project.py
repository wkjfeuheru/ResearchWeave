"""Explicit project lifecycle commands; all writes go through the Runtime repository."""

from __future__ import annotations
from openharness.tools.base import ToolExecutionContext

import json
from typing import Annotated, Literal

from pydantic import Field

from openharness.research.errors import ResearchError
from openharness.research.models import (
    ArtifactSubmission,
    Mutation,
    Record,
    ResearchObjective,
    TaskStatus,
)
from openharness.research.runtime import ResearchAgentRuntime
from openharness.tools.base import BaseTool, ToolResult


class ReadProject(Record):
    action: Literal["read"]


class StartProject(Mutation):
    action: Literal["start"]
    objective: ResearchObjective
    user_source_ids: list[str] = Field(min_length=1)


class TaskMutation(Mutation):
    task_id: str
    task_revision: int = Field(ge=1)


class ClaimTask(TaskMutation):
    action: Literal["claim_task"]


class TransitionTask(TaskMutation):
    action: Literal["transition_task"]
    from_status: TaskStatus
    to_status: TaskStatus
    blocker: str = ""


class SubmitArtifact(Mutation):
    action: Literal["submit_artifact"]
    artifact: ArtifactSubmission


class CompleteTask(TaskMutation):
    action: Literal["complete_task"]


class FinalizeProject(Mutation):
    action: Literal["finalize"]


class ResumeProject(Mutation):
    action: Literal["resume"]


class FeedbackProject(Mutation):
    action: Literal["feedback"]
    feedback: str = Field(min_length=1)


class CancelProject(Mutation):
    action: Literal["cancel"]
    reason: str = Field(min_length=1)


class ResearchProjectInput(Record):
    operation: Annotated[
        ReadProject
        | StartProject
        | ClaimTask
        | TransitionTask
        | SubmitArtifact
        | CompleteTask
        | FinalizeProject
        | ResumeProject
        | FeedbackProject
        | CancelProject,
        Field(discriminator="action"),
    ]


class ResearchProjectTool(BaseTool[ResearchProjectInput]):
    name = "research_project"
    contract = {
        "name": "research_project",
        "source": "builtin",
        "effect": "local_write",
        "required_capabilities": ("research.write",),
        "resources_write": ("research.control",),
        "parallelism": "resources",
    }
    description = (
        "Start and manage a report production project. read returns revision, objective, plan, artifacts and execution IDs. "
        "start requires a persisted user source and explicit report objective; then call planner. claim_task leases a READY task. "
        "submit_artifact binds immutable work to a successful execution_id and acceptance criteria; financial artifacts need unit/currency/period, "
        "models need reproduction and assumption evidence, drafts need sections and [E:ev_ID] citations. "
        "complete_task and finalize run deterministic CompletionPolicy; successful tools or final prose do not complete a project. "
        "feedback revises objective and requires replanner. resume restores suspended work. cancel terminates the project. "
        "Use current expected_revision and unique operation_id; issue one mutation per turn."
    )
    input_model = ResearchProjectInput

    def is_read_only(self, arguments: ResearchProjectInput) -> bool:
        # Like research_memory, writes are confined to the session's internal records/files.
        return True

    async def execute(
        self, arguments: ResearchProjectInput, context: ToolExecutionContext
    ) -> ToolResult:
        store = context.metadata.get("research_store")
        if store is None:
            return ToolResult(output="Research memory is disabled", is_error=True)
        runtime = context.metadata.get("research_runtime") or ResearchAgentRuntime(store)
        repository = runtime.repository
        receipt: object
        operation = arguments.operation
        try:
            if operation.action == "read":
                from openharness.research.dispatch_audit import dispatch_summaries

                memory = store.load()
                receipt = {
                    "dispatches": dispatch_summaries(store.directory, memory.project.id)
                    if memory.project
                    else [],
                    "revision": memory.revision,
                    "project": memory.project.model_dump(mode="json") if memory.project else None,
                    "objectives": {
                        key: value.model_dump(mode="json")
                        for key, value in memory.objectives.items()
                    },
                    "plan": store.view(memory)["plan"],
                    "artifacts": {
                        key: value.model_dump(mode="json")
                        for key, value in memory.artifacts.items()
                    },
                    "executions": {
                        key: value.model_dump(mode="json")
                        for key, value in memory.executions.items()
                    },
                }
            elif operation.action == "start":
                receipt = runtime.start_project(
                    operation.objective,
                    operation.user_source_ids,
                    operation.expected_revision,
                    operation_id=operation.operation_id,
                )
                if context.runtime_id:
                    from openharness.sandbox.session import stop_docker_sandbox

                    await stop_docker_sandbox(context.runtime_id)
            elif operation.action == "submit_artifact":
                receipt = repository.submit_artifact(
                    operation.artifact.model_dump(mode="json"),
                    operation.expected_revision,
                    operation.operation_id,
                )
            else:
                project = repository._project(store.load())
                if operation.action in {"claim_task", "transition_task"}:
                    receipt = repository.transition_task(
                        operation.task_id,
                        operation.from_status if operation.action == "transition_task" else "ready",
                        operation.to_status
                        if operation.action == "transition_task"
                        else "in_progress",
                        operation.expected_revision,
                        task_revision=operation.task_revision,
                        blocker=operation.blocker if operation.action == "transition_task" else "",
                        operation_id=operation.operation_id,
                    )
                elif operation.action == "complete_task":
                    receipt = repository.request_task_completion(
                        project.id,
                        operation.task_id,
                        operation.expected_revision,
                        operation.task_revision,
                        operation_id=operation.operation_id,
                    )
                elif operation.action == "finalize":
                    receipt = repository.finalize(
                        project.id, operation.expected_revision, operation_id=operation.operation_id
                    )
                elif operation.action == "feedback":
                    receipt = repository.submit_feedback(
                        project.id,
                        operation.feedback,
                        operation.expected_revision,
                        operation_id=operation.operation_id,
                    )
                elif operation.action == "resume":
                    receipt = runtime.resume_project(
                        project.id,
                        expected_revision=operation.expected_revision,
                        operation_id=operation.operation_id,
                    )
                elif operation.action == "cancel":
                    receipt = repository.cancel(
                        project.id,
                        operation.reason,
                        operation.expected_revision,
                        operation_id=operation.operation_id,
                    )
            return ToolResult(
                output=json.dumps(receipt, ensure_ascii=False),
                metadata={
                    "context_component": "dynamic_context",
                    "research_progress": store.progress(),
                    "research_source_specs": [],
                },
            )
        except (ResearchError, ValueError, OSError, StopIteration) as exc:
            return ToolResult(
                output=str(exc), is_error=True, metadata={"research_source_specs": []}
            )
