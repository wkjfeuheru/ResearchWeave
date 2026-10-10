"""Explicit project lifecycle commands; all writes go through the Runtime repository."""

from __future__ import annotations
from researchx.tools.base import ToolExecutionContext

import json
from typing import Annotated, Literal

from pydantic import Field

from researchx.state.errors import ResearchError
from researchx.state.models import (
    ArtifactSubmission,
    Mutation,
    Record,
    ResearchObjective,
    TaskStatus,
)
from researchx.state.runtime import ResearchAgentRuntime
from researchx.tools.base import BaseTool, ToolResult


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
        "启动并管理研报生产项目。read 返回版本、目标、计划、产物和执行 ID。"
        "start 需要已持久化的用户来源和明确的研报目标，然后调用 planner。claim_task 为 READY 任务获取租约。"
        "submit_artifact 将不可变工作绑定到成功的 execution_id 和验收标准；财务产物需要单位、币种和期间，"
        "模型需要复现方法和假设证据，初稿需要章节和 [E:ev_ID] 引用。"
        "complete_task 和 finalize 运行确定性的 CompletionPolicy；工具成功或最终文字都不能单独完成项目。"
        "feedback 修改目标并需要 replanner。resume 恢复暂停的工作。cancel 终止项目。"
        "使用当前 expected_revision 和唯一 operation_id；每轮只执行一次修改。"
    )
    input_model = ResearchProjectInput

    def is_read_only(self, arguments: ResearchProjectInput) -> bool:
        # Like research_memory, writes are confined to the session's internal records/files.
        return True

    def execution_contract(self, arguments: ResearchProjectInput | None = None) -> object:
        if arguments is not None and arguments.operation.action == "read":
            return {
                **self.contract,
                "effect": "read_only",
                "resources_write": (),
                "resources_read": ("research.control",),
            }
        return self.contract

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
                from researchx.state.dispatch_audit import dispatch_summaries

                memory = await store.load()
                receipt = {
                    "dispatches": (await dispatch_summaries(store, memory.project.id))
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
                receipt = await runtime.start_project(
                    operation.objective,
                    operation.user_source_ids,
                    operation.expected_revision,
                    operation_id=operation.operation_id,
                )
                if context.runtime_id:
                    from researchx.sandbox.session import stop_docker_sandbox

                    await stop_docker_sandbox(context.runtime_id)
            elif operation.action == "submit_artifact":
                receipt = await repository.submit_artifact(
                    operation.artifact.model_dump(mode="json"),
                    operation.expected_revision,
                    operation.operation_id,
                )
            else:
                project = repository._project((await store.load()))
                if operation.action in {"claim_task", "transition_task"}:
                    receipt = await repository.transition_task(
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
                    receipt = await repository.request_task_completion(
                        project.id,
                        operation.task_id,
                        operation.expected_revision,
                        operation.task_revision,
                        operation_id=operation.operation_id,
                    )
                elif operation.action == "finalize":
                    receipt = await repository.finalize(
                        project.id, operation.expected_revision, operation_id=operation.operation_id
                    )
                elif operation.action == "feedback":
                    receipt = await repository.submit_feedback(
                        project.id,
                        operation.feedback,
                        operation.expected_revision,
                        operation_id=operation.operation_id,
                    )
                elif operation.action == "resume":
                    receipt = await runtime.resume_project(
                        project.id,
                        expected_revision=operation.expected_revision,
                        operation_id=operation.operation_id,
                    )
                elif operation.action == "cancel":
                    receipt = await repository.cancel(
                        project.id,
                        operation.reason,
                        operation.expected_revision,
                        operation_id=operation.operation_id,
                    )
            return ToolResult(
                output=json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                metadata={
                    "context_component": "dynamic_context",
                    "research_progress": (await store.progress()),
                    "research_source_specs": [],
                },
            )
        except (ResearchError, ValueError, OSError, StopIteration) as exc:
            return ToolResult(
                output=str(exc), is_error=True, metadata={"research_source_specs": []}
            )
