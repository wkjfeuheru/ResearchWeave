"""Conflict lifecycle and atomic imports from isolated investigations."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, overload
from researchx.state.models import ResearchMemory, ResearchOperation
import hashlib
import json
from researchx.state.errors import ResearchError
from researchx.state.models import (
    AddConclusion,
    AddConflict,
    ArbitrationDecision,
    ArbitrationRecord,
    ConflictRecord,
    ReopenConflict,
    ResolveConflict,
    SubmitConflictReport,
    new_id,
    now,
)

if TYPE_CHECKING:
    from researchx.state.store import ResearchStore
ArbitrationStatus = Literal[
    "running", "completed", "timeout", "budget_exhausted", "interrupted", "failed", "stale"
]


@overload
def remap_references(value: dict[str, object], mapping: dict[str, str]) -> dict[str, object]: ...
@overload
def remap_references(value: object, mapping: dict[str, str]) -> object: ...


def remap_references(value: object, mapping: dict[str, str]) -> object:
    """Map exact ID values, never substitute IDs inside prose or original snapshots."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [remap_references(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: remap_references(item, mapping) for key, item in value.items()}
    return value


class ConflictStoreMixin:
    # The concrete store supplies this existing persistence/provenance contract.
    directory: Path
    lock: Path
    if TYPE_CHECKING:
        from contextlib import AbstractAsyncContextManager
        from sqlalchemy.ext.asyncio import AsyncSession

        def transaction(self) -> AbstractAsyncContextManager[AsyncSession]: ...
        async def write_snapshot(self, content: str) -> tuple[str, str]: ...

        async def _apply(
            self, memory: ResearchMemory, operation: ResearchOperation
        ) -> dict[str, object]: ...
        async def _load(self) -> ResearchMemory: ...
        async def _save(
            self, memory: ResearchMemory, action: str, data: dict[str, object]
        ) -> None: ...
        @staticmethod
        def _require(ids: list[str], records: Mapping[str, object], label: str) -> None: ...
        @staticmethod
        def _step_evidence(memory: ResearchMemory, ids: list[str]) -> set[str]: ...
        def _ensure_scope(
            self, memory: ResearchMemory, evidence_ids: list[str], step_ids: list[str]
        ) -> None: ...

    def _conflict_inputs(
        self, memory: ResearchMemory, conflict: ConflictRecord
    ) -> tuple[set[str], set[str], set[str]]:
        evidence_ids = set(conflict.additional_evidence_ids)
        steps = set()
        claims = set()
        for side in conflict.sides:
            evidence_ids.update(side.evidence_ids)
            steps.update(side.step_ids)
            claims.update(side.conclusion_ids)
        for arbitration in memory.arbitrations.values():
            if arbitration.conflict_id == conflict.id:
                claims.update(arbitration.output_conclusion_ids)
                for decision in (arbitration.report, arbitration.decision):
                    if decision:
                        evidence_ids.update(decision.evidence_ids)
                        evidence_ids.update(item.evidence_id for item in decision.assessments)
                        steps.update(decision.step_ids)
        for key in claims:
            evidence_ids.update(memory.conclusions[key].evidence_ids)
            steps.update(memory.conclusions[key].step_ids)
        # Include full provenance, reasoning predecessors and subsequent evidence versions.
        while True:
            previous = (len(evidence_ids), len(steps))
            for key in list(steps):
                step = memory.reasoning_chain[key]
                evidence_ids.update(step.evidence_ids)
                steps.update(step.prior_step_ids)
            for key in list(evidence_ids):
                evidence = memory.evidence_pool[key]
                evidence_ids.update(evidence.input_evidence_ids + evidence.supporting_evidence_ids)
                steps.update(
                    item
                    for item in (evidence.calculation_step_id, evidence.verification_step_id)
                    if item
                )
            evidence_ids.update(
                item.id for item in memory.evidence_pool.values() if item.supersedes in evidence_ids
            )
            if previous == (len(evidence_ids), len(steps)):
                break
        return evidence_ids, steps, claims

    def conflict_fingerprint(self, memory: ResearchMemory, conflict: ConflictRecord) -> str:
        evidence, steps, claims = self._conflict_inputs(memory, conflict)
        inputs = {
            "context_id": conflict.context_id,
            "plan_id": conflict.plan_id,
            "sides": [side.model_dump(mode="json") for side in conflict.sides],
            "evidence": [
                memory.evidence_pool[key].model_dump(mode="json") for key in sorted(evidence)
            ],
            "steps": [memory.reasoning_chain[key].model_dump(mode="json") for key in sorted(steps)],
            # Review flags are lifecycle bookkeeping rather than changed substantive inputs.
            "claims": [
                memory.conclusions[key].model_dump(mode="json", exclude={"needs_review"})
                for key in sorted(claims)
            ],
            "sources": {
                memory.evidence_pool[key].source_id: memory.sources[
                    memory.evidence_pool[key].source_id
                ].content_hash
                for key in evidence
            },
        }
        return hashlib.sha256(
            json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _current_conflict(self, memory: ResearchMemory, conflict_id: str) -> ConflictRecord:
        self._require([conflict_id], memory.conflicts, "conflict")
        conflict = memory.conflicts[conflict_id]
        if (
            conflict.plan_id != memory.research_state.current_plan_id
            or conflict.context_id != memory.current_context_id
            or memory.research_state.replan_required
        ):
            raise ResearchError("Conflict does not belong to the current research scope")
        return conflict

    def _validate_decision(
        self, memory: ResearchMemory, conflict: ConflictRecord, decision: ArbitrationDecision
    ) -> None:
        self._ensure_scope(memory, decision.evidence_ids, decision.step_ids)
        self._require(
            [item.evidence_id for item in decision.assessments],
            memory.evidence_pool,
            "assessed evidence",
        )
        if not set(decision.evidence_ids) <= self._step_evidence(memory, decision.step_ids):
            raise ResearchError("Arbitration evidence must be used by its reasoning steps")
        if not {item.evidence_id for item in decision.assessments} <= self._step_evidence(
            memory, decision.step_ids
        ):
            raise ResearchError("Assessed evidence must be used by arbitration reasoning steps")
        if decision.preferred_side is not None and decision.preferred_side >= len(conflict.sides):
            raise ResearchError("Preferred side is outside the conflict")
        self._ensure_scope(memory, [item.evidence_id for item in decision.assessments], [])
        # Both sides must be examined, including evidence ultimately rejected.
        examined = {item.evidence_id for item in decision.assessments}
        for side in conflict.sides:
            descendants = set(side.evidence_ids)
            while True:
                expanded = descendants | {
                    item.id
                    for item in memory.evidence_pool.values()
                    if item.supersedes in descendants
                }
                if expanded == descendants:
                    break
                descendants = expanded
            if not examined & descendants:
                raise ResearchError("Arbitration must assess evidence from every side")

    def _validate_conflicts(self, memory: ResearchMemory) -> None:
        for conflict in memory.conflicts.values():
            self._require([conflict.plan_id], memory.plans, "conflict plan")
            if conflict.context_id != memory.plans[conflict.plan_id].context_id:
                raise ResearchError("Conflict context does not match its plan")
            self._require(
                conflict.additional_evidence_ids, memory.evidence_pool, "conflict evidence"
            )
            for side in conflict.sides:
                self._require(side.evidence_ids, memory.evidence_pool, "conflict evidence")
                self._require(side.step_ids, memory.reasoning_chain, "conflict steps")
                self._require(side.conclusion_ids, memory.conclusions, "conflict conclusions")
            if conflict.current_arbitration_id:
                self._require([conflict.current_arbitration_id], memory.arbitrations, "arbitration")
                if memory.arbitrations[conflict.current_arbitration_id].conflict_id != conflict.id:
                    raise ResearchError("Arbitration does not match its conflict")
        for arbitration in memory.arbitrations.values():
            self._require([arbitration.conflict_id], memory.conflicts, "arbitration conflict")
            self._require(arbitration.evidence_versions, memory.evidence_pool, "arbitration inputs")
            self._require(
                arbitration.replaced_conclusion_ids + arbitration.output_conclusion_ids,
                memory.conclusions,
                "arbitration conclusions",
            )
            if arbitration.status == "completed" and arbitration.report is None:
                raise ResearchError("Completed investigations require a report")
            if arbitration.decision and (
                arbitration.status != "completed" or not arbitration.output_conclusion_ids
            ):
                raise ResearchError(
                    "Committed decisions require a completed investigation and successor conclusions"
                )
            for report in (arbitration.report, arbitration.decision):
                if report:
                    self._require(
                        report.evidence_ids + [item.evidence_id for item in report.assessments],
                        memory.evidence_pool,
                        "arbitration evidence",
                    )
                    self._require(report.step_ids, memory.reasoning_chain, "arbitration steps")

    def _mark_conflict_review(self, memory: ResearchMemory, conflict: ConflictRecord) -> None:
        evidence, _, claims = self._conflict_inputs(memory, conflict)
        for claim in memory.conclusions.values():
            if claim.id in claims or (
                claim.plan_id == conflict.plan_id
                and (set(claim.evidence_ids) | self._step_evidence(memory, claim.step_ids))
                & evidence
            ):
                claim.needs_review = True

    def _reopen_record(self, memory: ResearchMemory, conflict: ConflictRecord, reason: str) -> None:
        conflict.status, conflict.review_note, conflict.updated_at = "open", reason, now()
        (self._mark_conflict_review(memory, conflict))

    def _reopen_affected_conflicts(self, memory: ResearchMemory, affected: set[str]) -> None:
        for conflict in memory.conflicts.values():
            inputs, _, _ = self._conflict_inputs(memory, conflict)
            if inputs & affected:
                (self._reopen_record(memory, conflict, "相关证据已修订或撤回，裁决需要复核"))

    async def _apply_conflict(
        self, memory: ResearchMemory, operation: ResearchOperation
    ) -> dict[str, object] | None:
        if isinstance(operation, AddConflict):
            state = memory.research_state
            if not state.current_plan_id or state.replan_required:
                raise ResearchError("Create a current research plan before registering a conflict")
            for side in operation.sides:
                self._ensure_scope(memory, side.evidence_ids, side.step_ids)
                self._require(side.conclusion_ids, memory.conclusions, "conflict conclusions")
                if any(
                    memory.conclusions[key].plan_id != state.current_plan_id
                    for key in side.conclusion_ids
                ):
                    raise ResearchError("Conflict conclusions must belong to the current plan")
            conflict = ConflictRecord(
                **operation.model_dump(exclude={"action", "operation_id", "expected_revision"}),
                plan_id=state.current_plan_id,
                context_id=memory.current_context_id or "",
            )
            # Semantic detection is performed by the main agent; avoid exact duplicate tickets.
            for existing in memory.conflicts.values():
                if (
                    existing.plan_id == conflict.plan_id
                    and existing.question == conflict.question
                    and existing.kind == conflict.kind
                    and existing.sides == conflict.sides
                ):
                    if conflict.core and not existing.core:
                        existing.core = True
                        existing.updated_at = now()
                        if existing.status != "resolved":
                            (self._mark_conflict_review(memory, existing))
                    return {"conflict_id": existing.id}
            memory.conflicts[conflict.id] = conflict
            if conflict.core:
                (self._mark_conflict_review(memory, conflict))
            return {"conflict_id": conflict.id}
        if isinstance(operation, ReopenConflict):
            conflict = self._current_conflict(memory, operation.conflict_id)
            self._ensure_scope(memory, operation.evidence_ids, [])
            conflict.additional_evidence_ids = list(
                dict.fromkeys(conflict.additional_evidence_ids + operation.evidence_ids)
            )
            (self._reopen_record(memory, conflict, operation.reason))
            return {"conflict_id": conflict.id}
        if isinstance(operation, SubmitConflictReport):
            # The main loop has already collected and registered both sides. Submit only a
            # report here; resolve_conflict remains a separate, reviewed authority change.
            conflict = self._current_conflict(memory, operation.conflict_id)
            if any(item.status == "running" for item in memory.arbitrations.values()):
                raise ResearchError(
                    "An investigation is still running; settle it before submitting a report"
                )
            if conflict.status in {"resolved", "awaiting_review", "unresolved"}:
                raise ResearchError(
                    "Review the existing report or reopen the conflict with a reason"
                )
            self._validate_decision(memory, conflict, operation.report)
            arbitration = ArbitrationRecord(
                conflict_id=conflict.id,
                input_fingerprint=(self.conflict_fingerprint(memory, conflict)),
                evidence_versions=sorted((self._conflict_inputs(memory, conflict))[0]),
                report=operation.report,
                status="completed",
                finished_at=now(),
                note="Report submitted by the main agent for separate review",
            )
            memory.arbitrations[arbitration.id] = arbitration
            conflict.current_arbitration_id = arbitration.id
            conflict.status, conflict.updated_at = "awaiting_review", now()
            (self._mark_conflict_review(memory, conflict))
            arbitration.review_fingerprint = self.conflict_fingerprint(memory, conflict)
            conflict.last_attempt_fingerprint = arbitration.review_fingerprint
            return {"conflict_id": conflict.id, "arbitration_id": arbitration.id}
        if isinstance(operation, ResolveConflict):
            conflict = self._current_conflict(memory, operation.conflict_id)
            self._require([operation.arbitration_id], memory.arbitrations, "arbitration")
            arbitration = memory.arbitrations[operation.arbitration_id]
            if (
                conflict.current_arbitration_id != arbitration.id
                or arbitration.status != "completed"
                or arbitration.decision is not None
                or conflict.status != "awaiting_review"
            ):
                raise ResearchError("Review the latest completed investigation before resolving")
            if arbitration.review_fingerprint != (self.conflict_fingerprint(memory, conflict)):
                raise ResearchError(
                    "Arbitration inputs changed; investigate again before resolving"
                )
            decision = operation.decision
            self._validate_decision(memory, conflict, decision)
            if decision.outcome == "unresolved" and operation.conclusion_status == "verified":
                raise ResearchError("Unresolved conflicts cannot yield verified conclusions")
            replaced = [key for side in conflict.sides for key in side.conclusion_ids]
            for previous in memory.arbitrations.values():
                if previous.conflict_id == conflict.id:
                    replaced.extend(previous.output_conclusion_ids)
            replaced = list(dict.fromkeys(replaced))
            # Preserve single-parent history; the arbitration records the full replacement set.
            parent = next(
                (
                    key
                    for key in reversed(replaced)
                    if not any(item.supersedes == key for item in memory.conclusions.values())
                ),
                None,
            )
            conditions = "；".join(decision.conditions)
            statement = decision.statement + (f"（适用条件：{conditions}）" if conditions else "")
            conflict.status = "unresolved" if decision.outcome == "unresolved" else "resolved"
            result = await self._apply(
                memory,
                AddConclusion(
                    action="add_conclusion",
                    operation_id=operation.operation_id,
                    expected_revision=operation.expected_revision,
                    statement=statement,
                    status="conflicted"
                    if decision.outcome == "unresolved"
                    else operation.conclusion_status,
                    evidence_ids=decision.evidence_ids,
                    step_ids=decision.step_ids,
                    supersedes=parent,
                ),
            )
            arbitration.decision = decision
            arbitration.replaced_conclusion_ids = replaced
            arbitration.output_conclusion_ids = [str(result["conclusion_id"])]
            conflict.review_note, conflict.updated_at = decision.rationale, now()
            output = memory.conclusions[str(result["conclusion_id"])]
            output_needs_review = output.needs_review
            (self._mark_conflict_review(memory, conflict))
            output.needs_review = output_needs_review or decision.outcome == "unresolved"
            # Account for the new conclusion in the no-repeat key.
            conflict.last_attempt_fingerprint = self.conflict_fingerprint(memory, conflict)
            return {"conflict_id": conflict.id, "arbitration_id": arbitration.id, **result}
        return None

    async def begin_investigation(
        self, conflict_id: str, *, retry: bool = False
    ) -> tuple[ArbitrationRecord, ResearchMemory]:
        async with self.transaction():
            memory = await self._load()
            conflict = self._current_conflict(memory, conflict_id)
            if any(item.status == "running" for item in memory.arbitrations.values()):
                raise ResearchError("Only one investigation may run in this session")
            fingerprint = self.conflict_fingerprint(memory, conflict)
            if not retry and fingerprint == conflict.last_attempt_fingerprint:
                raise ResearchError(
                    "These evidence versions were already investigated; retry only on explicit user request or new evidence"
                )
            evidence, _, _ = self._conflict_inputs(memory, conflict)
            arbitration = ArbitrationRecord(
                conflict_id=conflict.id,
                input_fingerprint=fingerprint,
                evidence_versions=sorted(evidence),
            )
            memory.arbitrations[arbitration.id] = arbitration
            conflict.current_arbitration_id = arbitration.id
            conflict.last_attempt_fingerprint = fingerprint
            conflict.status, conflict.updated_at = "investigating", now()
            (self._mark_conflict_review(memory, conflict))
            await self._save(
                memory,
                "begin_investigation",
                {"conflict_id": conflict.id, "arbitration_id": arbitration.id},
            )
            return arbitration, memory

    async def finish_investigation(
        self,
        arbitration_id: str,
        *,
        staging: ResearchStore | None = None,
        baseline: ResearchMemory | None = None,
        report: ArbitrationDecision | None = None,
        status: ArbitrationStatus = "completed",
        note: str = "",
        usage: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Import complete records atomically; never merge results into a changed scope."""
        stage_memory = (await staging.load()) if staging else None
        async with self.transaction():
            memory = await self._load()
            self._require([arbitration_id], memory.arbitrations, "arbitration")
            arbitration = memory.arbitrations[arbitration_id]
            if arbitration.status != "running":
                return arbitration.model_dump(mode="json")
            conflict = memory.conflicts[arbitration.conflict_id]
            current = (
                conflict.plan_id == memory.research_state.current_plan_id
                and conflict.context_id == memory.current_context_id
                and not memory.research_state.replan_required
                and arbitration.input_fingerprint == (self.conflict_fingerprint(memory, conflict))
            )
            if memory.project and baseline and baseline.project:
                current = current and (
                    memory.project.execution_epoch == baseline.project.execution_epoch
                    and memory.project.objective_revision == baseline.project.objective_revision
                )
            if not current:
                status, note, report = (
                    "stale",
                    "研究范围或输入证据变化，调查结果保留在暂存记录中",
                    None,
                )
            if status == "completed" and report is None:
                raise ResearchError("Completed investigations require a report")
            mapping = {}
            if stage_memory and current:
                for bucket, prefix in (
                    ("sources", "src"),
                    ("evidence_pool", "ev"),
                    ("reasoning_chain", "step"),
                ):
                    mapping.update(
                        {
                            key: new_id(prefix)
                            for key in getattr(stage_memory, bucket)
                            if key not in getattr(baseline, bucket)
                        }
                    )
                for bucket in ("sources", "evidence_pool", "reasoning_chain"):
                    for key, record in getattr(stage_memory, bucket).items():
                        if key not in mapping:
                            continue
                        fields = remap_references(record.model_dump(mode="json"), mapping)
                        fields["plan_id"] = conflict.plan_id
                        fields["task_id"] = memory.research_state.current_task_id
                        if bucket == "sources":
                            fields["origin_id"] = f"{arbitration.id}:{fields['origin_id']}"
                            assert staging is not None
                            content = await staging.read_source(record)
                            await self.write_snapshot(content)
                        if (
                            bucket == "evidence_pool"
                            and record.supersedes
                            and record.supersedes not in mapping
                        ):
                            # A child may verify an input locally, but cannot revise the parent's evidence.
                            fields["supersedes"] = None
                        imported = type(record).model_validate(fields)
                        getattr(memory, bucket)[imported.id] = imported
                arbitration.imported_ids = list(mapping.values())
            if report and current:
                report = ArbitrationDecision.model_validate(
                    remap_references(report.model_dump(mode="json"), mapping)
                )
                self._validate_decision(memory, conflict, report)
                arbitration.report = report
            arbitration.status, arbitration.note = status, note
            arbitration.usage, arbitration.finished_at = usage or {}, now()
            if current:
                conflict.status = (
                    "awaiting_review" if status == "completed" and report else "interrupted"
                )
                conflict.review_note, conflict.updated_at = note, now()
            elif conflict.status == "investigating":
                (self._reopen_record(memory, conflict, note))
            if current:
                arbitration.review_fingerprint = self.conflict_fingerprint(memory, conflict)
                conflict.last_attempt_fingerprint = arbitration.review_fingerprint
            await self._save(
                memory, "finish_investigation", {"arbitration_id": arbitration.id, "status": status}
            )
            return arbitration.model_dump(mode="json")

    async def recover_investigations(self) -> None:
        """Only call when the host has established that this session has no live run."""
        async with self.transaction():
            memory = await self._load()
            changed = False
            for arbitration in memory.arbitrations.values():
                if arbitration.status == "running":
                    arbitration.status, arbitration.finished_at = "interrupted", now()
                    arbitration.note = "执行已中断，可按需重新核查"
                    conflict = memory.conflicts[arbitration.conflict_id]
                    conflict.status, conflict.review_note = "interrupted", arbitration.note
                    (self._mark_conflict_review(memory, conflict))
                    changed = True
            if changed:
                await self._save(memory, "recover_investigations", {})
