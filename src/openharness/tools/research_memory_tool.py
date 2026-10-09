"""Structured updates to the current conversation's research memory."""

from __future__ import annotations
from pydantic import ModelWrapValidatorHandler

import json

from pydantic import Field, ValidationError, model_validator

from openharness.research.models import Record, ResearchOperation
from openharness.research.errors import ResearchError
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

LEGACY_STATE_MIGRATION = {
    "set_context": "Use research_project.feedback to revise the current objective; research_project.start creates new projects.",
    "create_plan": "Call planner for the initial PlanProposal or replanner for a PlanPatch; Runtime validates and commits it.",
    "update_task": "Use research_project.claim_task/transition_task and complete_task; TaskManager and CompletionPolicy validate transitions.",
}


class ResearchMemoryInput(Record):
    operation: ResearchOperation = Field(
        description="Read the current revision before mutations. Use unique operation_id and expected_revision. All IDs must belong to this conversation."
    )

    @model_validator(mode="wrap")
    @classmethod
    def remove_empty_extra_operation_fields(
        cls, value: object, handler: ModelWrapValidatorHandler[ResearchMemoryInput]
    ) -> ResearchMemoryInput:
        """Tolerate empty, unsupported decoration without weakening record validation."""
        try:
            return handler(value)
        except ValidationError as exc:
            if not isinstance(value, dict) or not isinstance(value.get("operation"), dict):
                raise
            operation = value["operation"]
            errors = exc.errors()
            if not all(
                error["type"] == "extra_forbidden"
                and len(error["loc"]) == 3
                and error["loc"][0] == "operation"
                and error["loc"][1] == operation.get("action")
                and error.get("input") in ("", None)
                for error in errors
            ):
                raise
            # Leave the original tool call intact for hooks and audit history.
            normalized = dict(operation)
            for error in errors:
                normalized.pop(error["loc"][-1])
            return handler({**value, "operation": normalized})


class ResearchMemoryTool(BaseTool[ResearchMemoryInput]):
    name = "research_memory"
    contract = {
        "name": "research_memory",
        "source": "builtin",
        "effect": "local_write",
        "required_capabilities": ("research.write",),
        "resources_write": ("research.control",),
        "parallelism": "resources",
    }
    description = (
        "Read research state and maintain this conversation's evidence, provenance, conclusions, conflicts and "
        "auditable Reasoning Chain. read returns the current view; read(ids, include_content) retrieves "
        "archived records and source snapshots. Report objectives/plans/tasks are authoritative in ResearchAgentRuntime; "
        "use research_project, planner/replanner and CompletionPolicy for them. "
        "set_context/create_plan/update_task are legacy-session compatibility operations only and are rejected in report projects. "
        "MEMORY.md is background maintained by the main agent's file tools. add_evidence must use "
        "a program-issued source_id. verify_evidence creates an audited successor after source, cross-source, or "
        "calculation verification. Use locator for original-text locations and verification_note for verification notes; "
        "source_id_note is not a supported field. Use only schema-defined fields. "
        "add_reasoning records methods/results, never private chain of thought; "
        "add_conclusion links evidence and reasoning. Revise evidence/conclusions using supersedes. "
        "add_conflict records both sides and marks core conclusions for review. The main agent inspects original "
        "sources, registers reasoning and submits submit_conflict_report; optional dispatch_subagents returns "
        "candidates only. resolve_conflict atomically commits the main agent's separately reviewed decision and "
        "successor conclusion; reopen_conflict records changed evidence or a requested review. "
        "Cite evidence in answers as [E:ev_ID]. Writes are internal session bookkeeping."
    )
    input_model = ResearchMemoryInput

    def validation_error_message(self, error: Exception) -> str:
        if not isinstance(error, ValidationError):
            return super().validation_error_message(error)
        details = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        schema = self.input_model.model_json_schema()
        actions = schema["properties"]["operation"]["discriminator"]["mapping"]
        allowed = []
        for item in error.errors():
            location = item["loc"]
            if len(location) > 1 and location[1] in actions:
                definition = actions[location[1]].rsplit("/", 1)[-1]
                fields = ", ".join(schema["$defs"][definition]["properties"])
                hint = f"{location[1]} accepts: {fields}."
                if hint not in allowed:
                    allowed.append(hint)
        return (
            f"Invalid input for research_memory: {details}. "
            + " ".join(allowed)
            + " No write was committed. Read the current revision, correct the fields, and retry. "
            "source_id must remain a program-issued source ID; use verification_note for verification notes."
        )

    async def execute(
        self, arguments: ResearchMemoryInput, context: ToolExecutionContext
    ) -> ToolResult:
        store = context.metadata.get("research_store")
        if store is None:
            return ToolResult(
                output="Research memory is not enabled for this session", is_error=True
            )
        try:
            if (
                store.load().project is not None
                and arguments.operation.action in LEGACY_STATE_MIGRATION
            ):
                return ToolResult(
                    output="Legacy plan/task writes cannot bypass Runtime. "
                    + LEGACY_STATE_MIGRATION[arguments.operation.action],
                    is_error=True,
                )
            receipt = store.apply(
                arguments.operation.model_dump(mode="json"),
                budget=int(context.metadata.get("research_injection_budget", 6000)),
                model=getattr(context.metadata.get("query_context"), "model", ""),
            )
        except (ResearchError, ValueError) as exc:
            return ToolResult(output=str(exc), is_error=True)
        progress = receipt["progress"] if "progress" in receipt else store.progress()
        detail = {
            "add_conflict": "已登记双方证据与争议",
            "submit_conflict_report": "核查报告已保存，等待主代理审查裁决",
            "reopen_conflict": "相关判断需要重新核查",
        }.get(arguments.operation.action)
        if arguments.operation.action == "resolve_conflict":
            detail = (
                "争议仍未决，证据缺口已保留"
                if arguments.operation.decision.outcome == "unresolved"
                else "裁决已提交，适用条件与证据已保存"
            )
        return ToolResult(
            output=json.dumps(receipt, ensure_ascii=False),
            metadata={
                "context_component": "memory"
                if arguments.operation.action == "read"
                else "dynamic_context",
                "research_progress": progress,
                **({"detail": detail} if detail else {}),
            },
        )

    def is_read_only(self, arguments: ResearchMemoryInput) -> bool:
        # Session bookkeeping is authorized by the research runtime; it cannot write arbitrary files.
        return True
