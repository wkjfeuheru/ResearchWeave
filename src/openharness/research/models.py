"""Validated, auditable research records. All IDs are local to one session."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskContext(Record):
    id: str = Field(default_factory=lambda: new_id("ctx"))
    goal: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)
    scope: str = ""
    time_range: str = ""
    constraints: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    user_source_ids: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    created_at: str = Field(default_factory=now)


TaskStatus = Literal["pending", "in_progress", "completed", "blocked", "cancelled"]


class ResearchTask(Record):
    id: str = Field(default_factory=lambda: new_id("task"))
    title: str = Field(min_length=1, max_length=120)
    status: TaskStatus = "pending"
    blocker: str = ""
    updated_at: str = Field(default_factory=now)


class ResearchPlan(Record):
    id: str = Field(default_factory=lambda: new_id("plan"))
    context_id: str
    title: str = Field(min_length=1, max_length=120)
    tasks: list[ResearchTask] = Field(min_length=1)
    reused_evidence_ids: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    archived: bool = False
    created_at: str = Field(default_factory=now)


class SourceRecord(Record):
    id: str
    plan_id: str | None = None
    kind: Literal["web", "search", "file", "mcp", "user", "tool", "calculation"]
    title: str
    locator: str
    origin_id: str
    collected_at: str
    published_at: str | None = None
    content_hash: str
    snapshot: str
    fragment: bool = False
    is_error: bool = False

    @field_validator("collected_at")
    @classmethod
    def utc_collection_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("Source collection timestamp must be UTC")
        return value


class Evidence(Record):
    id: str = Field(default_factory=lambda: new_id("ev"))
    plan_id: str | None = None
    statement: str = Field(min_length=1)
    source_id: str
    locator: str = ""
    period: str = ""
    collected_at: str
    published_at: str | None = None
    status: Literal["pending", "verified", "retracted"] = "pending"
    needs_review: bool = False
    verification_step_id: str | None = None
    verification_note: str = ""
    input_evidence_ids: list[str] = Field(default_factory=list)
    calculation_step_id: str | None = None
    supersedes: str | None = None
    created_at: str = Field(default_factory=now)


class ReasoningStep(Record):
    id: str = Field(default_factory=lambda: new_id("step"))
    plan_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    prior_step_ids: list[str] = Field(default_factory=list)
    method: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    result: str = Field(min_length=1)
    output: str = Field(min_length=1)
    uncertainty: str = ""
    verification: bool = False
    created_at: str = Field(default_factory=now)


class Conclusion(Record):
    id: str = Field(default_factory=lambda: new_id("claim"))
    plan_id: str | None = None
    statement: str = Field(min_length=1)
    status: Literal["tentative", "verified", "conflicted", "retracted"] = "tentative"
    evidence_ids: list[str] = Field(min_length=1)
    step_ids: list[str] = Field(min_length=1)
    needs_review: bool = False
    supersedes: str | None = None
    created_at: str = Field(default_factory=now)


class ResearchState(Record):
    current_plan_id: str | None = None
    current_task_id: str | None = None
    unresolved: list[str] = Field(default_factory=list)
    next_step: str = ""
    replan_required: bool = False


class ResearchMemory(Record):
    schema_version: Literal[1] = 1
    session_id: str
    revision: int = Field(default=0, ge=0)
    current_context_id: str | None = None
    task_context: list[TaskContext] = Field(default_factory=list)
    research_state: ResearchState = Field(default_factory=ResearchState)
    plans: dict[str, ResearchPlan] = Field(default_factory=dict)
    sources: dict[str, SourceRecord] = Field(default_factory=dict)
    evidence_pool: dict[str, Evidence] = Field(default_factory=dict)
    reasoning_chain: dict[str, ReasoningStep] = Field(default_factory=dict)
    conclusions: dict[str, Conclusion] = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    operations: dict[str, dict] = Field(default_factory=dict)
    answers: dict[str, dict] = Field(default_factory=dict)
    pending_steers: dict[str, dict] = Field(default_factory=dict)


class ReadMemory(Record):
    action: Literal["read"]
    ids: list[str] = Field(default_factory=list)
    include_content: bool = False


class Mutation(Record):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class SetContext(Mutation):
    action: Literal["set_context"]
    goal: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)
    scope: str = ""
    time_range: str = ""
    constraints: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    user_source_ids: list[str] = Field(min_length=1)


class CreatePlan(Mutation):
    action: Literal["create_plan"]
    title: str = Field(min_length=1, max_length=120)
    tasks: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(min_length=1, max_length=30)
    reused_evidence_ids: list[str] = Field(default_factory=list)


class UpdateTask(Mutation):
    action: Literal["update_task"]
    task_id: str
    status: TaskStatus
    blocker: str = ""
    next_step: str = ""
    unresolved: list[str] | None = None


class AddEvidence(Mutation):
    action: Literal["add_evidence"]
    statement: str = Field(min_length=1)
    source_id: str
    locator: str = ""
    period: str = ""
    status: Literal["pending", "verified", "retracted"] = "pending"
    verification_step_id: str | None = None
    verification_note: str = ""
    input_evidence_ids: list[str] = Field(default_factory=list)
    calculation_step_id: str | None = None
    supersedes: str | None = None


class AddReasoning(Mutation):
    action: Literal["add_reasoning"]
    evidence_ids: list[str] = Field(default_factory=list)
    prior_step_ids: list[str] = Field(default_factory=list)
    method: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    result: str = Field(min_length=1)
    output: str = Field(min_length=1)
    uncertainty: str = ""
    verification: bool = False

    @model_validator(mode="after")
    def has_inputs(self):
        if not self.evidence_ids and not self.prior_step_ids:
            raise ValueError("A reasoning step needs evidence or a prior step")
        return self


class AddConclusion(Mutation):
    action: Literal["add_conclusion"]
    statement: str = Field(min_length=1)
    status: Literal["tentative", "verified", "conflicted", "retracted"] = "tentative"
    evidence_ids: list[str] = Field(min_length=1)
    step_ids: list[str] = Field(min_length=1)
    supersedes: str | None = None


ResearchOperation = Annotated[
    ReadMemory | SetContext | CreatePlan | UpdateTask | AddEvidence | AddReasoning | AddConclusion,
    Field(discriminator="action"),
]
