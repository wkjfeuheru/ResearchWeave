"""Structured updates to the current conversation's research memory."""

from __future__ import annotations

import json

from pydantic import Field, ValidationError, model_validator

from openharness.research.models import Record, ResearchOperation
from openharness.research.store import ResearchError
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ResearchMemoryInput(Record):
    operation: ResearchOperation = Field(description="Read the current revision before mutations. Use unique operation_id and expected_revision. All IDs must belong to this conversation.")

    @model_validator(mode="wrap")
    @classmethod
    def remove_empty_extra_operation_fields(cls, value, handler):
        """Tolerate empty, unsupported decoration without weakening record validation."""
        try:
            return handler(value)
        except ValidationError as exc:
            if not isinstance(value, dict) or not isinstance(value.get("operation"), dict):
                raise
            operation = value["operation"]
            errors = exc.errors()
            if not all(error["type"] == "extra_forbidden"
                       and len(error["loc"]) == 3 and error["loc"][0] == "operation"
                       and error["loc"][1] == operation.get("action")
                       and error.get("input") in ("", None) for error in errors):
                raise
            # Leave the original tool call intact for hooks and audit history.
            normalized = dict(operation)
            for error in errors:
                normalized.pop(error["loc"][-1])
            return handler({**value, "operation": normalized})


class ResearchMemoryTool(BaseTool):
    name = "research_memory"
    description = (
        "Read and maintain this conversation's Task Context, Research State, Evidence Pool and "
        "auditable Reasoning Chain. read returns the current view; read(ids, include_content) retrieves "
        "archived records and source snapshots. set_context records user-authorized goals; create_plan "
        "archives the previous plan; update_task publishes committed progress. add_evidence must use "
        "a program-issued source_id. Use locator for original-text locations and verification_note for verification notes; "
        "source_id_note is not a supported field. Use only schema-defined fields. "
        "add_reasoning records methods/results, never private chain of thought; "
        "add_conclusion links evidence and reasoning. Revise evidence/conclusions using supersedes. "
        "Cite evidence in answers as [E:ev_ID]. Writes are internal session bookkeeping."
    )
    input_model = ResearchMemoryInput

    def validation_error_message(self, error: Exception) -> str:
        if not isinstance(error, ValidationError):
            return super().validation_error_message(error)
        details = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                            for item in error.errors(include_url=False, include_input=False))
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
        return (f"Invalid input for research_memory: {details}. " + " ".join(allowed)
                + " No write was committed. Read the current revision, correct the fields, and retry. "
                  "source_id must remain a program-issued source ID; use verification_note for verification notes.")

    async def execute(self, arguments: ResearchMemoryInput, context: ToolExecutionContext) -> ToolResult:
        store = context.metadata.get("research_store")
        if store is None:
            return ToolResult(output="Research memory is not enabled for this session", is_error=True)
        try:
            receipt = store.apply(arguments.operation.model_dump(mode="json"),
                                  budget=int(context.metadata.get("research_injection_budget", 6000)))
        except (ResearchError, ValueError) as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=json.dumps(receipt, ensure_ascii=False), metadata={"research_progress": store.progress()})

    def is_read_only(self, arguments) -> bool:
        # Session bookkeeping is authorized by the research runtime; it cannot write arbitrary files.
        return True
