"""Pure completion checks over committed research records and immutable snapshots."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from researchx.state.store import ResearchStore
from researchx.state.models import ResearchArtifact, ResearchTask, ResearchPlan

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from researchx.state.models import CheckResult, CompletionResult, ResearchMemory
from researchx.state.errors import ResearchError


@dataclass(frozen=True)
class ResearchContext:
    memory: ResearchMemory
    store: ResearchStore


class CompletionPolicy:
    @staticmethod
    def result(checks: list[CheckResult]) -> CompletionResult:
        missing = [check.message for check in checks if check.blocking and not check.passed]
        return CompletionResult(
            passed=not missing,
            checks=checks,
            missing_requirements=missing,
            recommended_actions=[
                "Repair the listed defects, commit revised artifacts, then validate again"
            ]
            if missing
            else [],
        )

    def evidence_valid(self, key: str, memory: ResearchMemory) -> bool:
        evidence = memory.evidence_pool.get(key)
        return bool(
            evidence
            and evidence.status in {"source_checked", "verified"}
            and not evidence.needs_review
            and not any(item.supersedes == key for item in memory.evidence_pool.values())
        )

    def artifact_checks(
        self, artifact: ResearchArtifact, context: ResearchContext
    ) -> list[CheckResult]:
        memory = context.memory
        checks = []

        def check(name: str, passed: object, message: str) -> None:
            checks.append(
                CheckResult(
                    check_id=f"artifact:{artifact.id}:{name}",
                    passed=bool(passed),
                    message=message,
                    affected_task_ids=[artifact.task_id],
                )
            )

        content = ""
        try:
            path = context.store.directory / artifact.snapshot
            if artifact.snapshot != f"content/{artifact.content_hash}.txt":
                raise ValueError("Invalid snapshot")
            content = path.read_text(encoding="utf-8")
            intact = hashlib.sha256(content.encode()).hexdigest() == artifact.content_hash
        except (OSError, ValueError):
            intact = False
        check(
            "snapshot",
            intact and content.strip(),
            "Artifact must have an intact, nonempty snapshot",
        )
        check("fresh", not artifact.stale, "Artifact must not be stale")
        execution = memory.executions.get(artifact.execution_id)
        check(
            "execution",
            execution
            and execution.status == "committed"
            and (execution.task_id, execution.task_revision, execution.plan_revision)
            == (artifact.task_id, artifact.task_revision, artifact.plan_revision),
            "Artifact requires a committed execution with matching versions",
        )
        check(
            "inputs",
            all(
                key in memory.artifacts and not memory.artifacts[key].stale
                for key in artifact.input_artifact_ids
            ),
            "Artifact inputs must exist and be current",
        )
        check(
            "evidence",
            all(
                self.evidence_valid(key, memory)
                for key in artifact.evidence_ids + artifact.assumption_evidence_ids
            ),
            "Artifact evidence and assumptions must reference checked, current evidence",
        )
        evidence_ids = (
            artifact.evidence_ids
            + artifact.assumption_evidence_ids
            + [
                key
                for finding_id in artifact.finding_ids
                if finding_id in memory.conclusions
                for key in memory.conclusions[finding_id].evidence_ids
            ]
        )
        check(
            "source_snapshots",
            all(self.source_intact(key, context) for key in evidence_ids),
            "Evidence source snapshots must be intact and nonempty",
        )
        if artifact.kind in {"dataset", "model", "chart"}:
            check(
                "dimensions",
                artifact.unit.strip() and artifact.currency.strip() and artifact.period.strip(),
                "Financial artifacts require unit, currency and period",
            )
            inputs = [
                memory.artifacts[key]
                for key in artifact.input_artifact_ids
                if key in memory.artifacts
            ]
            check(
                "consistency",
                all(
                    (item.unit, item.currency, item.period)
                    == (artifact.unit, artifact.currency, artifact.period)
                    for item in inputs
                    if item.kind in {"dataset", "model", "chart"}
                ),
                "Financial input dimensions must agree; normalize data in a separate artifact first",
            )
        if artifact.kind == "model":
            check(
                "reproducible",
                artifact.reproduction.strip()
                and artifact.input_artifact_ids
                and artifact.assumption_evidence_ids,
                "Model requires reproducible method, inputs and sourced assumptions",
            )
        if artifact.kind == "report_draft":
            delivery_intact = False
            try:
                from researchx.workspace.session_files import SessionFiles

                manifest, download = SessionFiles(context.store.directory).artifact(
                    artifact.file_id or ""
                )
                delivery_intact = (
                    manifest.get("execution_id") == artifact.execution_id
                    and hashlib.sha256(download.read_bytes()).hexdigest() == artifact.content_hash
                )
                if memory.project and memory.project.workspace_path:
                    from researchx.state.runtime import ResearchAgentRuntime

                    root = Path(memory.project.workspace_path)
                    report = ResearchAgentRuntime._check_path(
                        root, Path("reports") / f"{artifact.id}.md"
                    )
                    delivery_intact = (
                        delivery_intact
                        and report.is_file()
                        and hashlib.sha256(report.read_bytes()).hexdigest() == artifact.content_hash
                    )
            except (OSError, ValueError, TypeError, KeyError, ResearchError):
                delivery_intact = False
            check(
                "delivery",
                delivery_intact,
                "Report delivery files must exist, remain inside the project workspace and match the committed snapshot",
            )
            citations = re.findall(r"\[E:([^\]\s]+)\]", content)
            check(
                "citations",
                citations
                and set(citations) <= set(artifact.evidence_ids)
                and all(self.evidence_valid(key, memory) for key in citations),
                "Draft citations must resolve to bound, checked evidence",
            )
            check(
                "finding_support",
                all(
                    key in memory.conclusions
                    and set(memory.conclusions[key].evidence_ids) <= set(citations)
                    for key in artifact.finding_ids
                ),
                "Draft must cite the evidence supporting its bound findings",
            )
            check(
                "sections",
                all(
                    re.search(r"^#{1,6}\s+" + re.escape(section) + r"\s*$", content, re.M)
                    for section in artifact.sections
                ),
                "Declared draft sections must exist as headings",
            )
        return checks

    @staticmethod
    def source_intact(evidence_id: str, context: ResearchContext) -> bool:
        try:
            source = context.memory.sources[context.memory.evidence_pool[evidence_id].source_id]
            return bool(context.store.read_source(source).strip())
        except (OSError, KeyError, ResearchError):
            return False

    def inspect_task(self, task: ResearchTask, context: ResearchContext) -> CompletionResult:
        memory = context.memory
        checks = []

        def check(name: str, passed: object, message: str, blocking: bool = True) -> None:
            checks.append(
                CheckResult(
                    check_id=f"task:{task.id}:{name}",
                    passed=bool(passed),
                    blocking=blocking,
                    message=message,
                    affected_task_ids=[task.id],
                )
            )

        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        by_id = {item.id: item for item in plan.tasks} if plan else {}
        check(
            "dependencies",
            all(
                key in by_id
                and by_id[key].status == "completed"
                and task.dependency_revisions.get(key) == by_id[key].task_revision
                for key in task.dependencies
            ),
            "Dependencies must complete at their pinned revisions",
        )
        artifacts = [memory.artifacts[key] for key in task.artifact_ids if key in memory.artifacts]
        check(
            "artifacts",
            artifacts
            and len(artifacts) == len(task.artifact_ids)
            and all(
                item.task_id == task.id and item.task_revision == task.task_revision
                for item in artifacts
            ),
            "Task requires artifacts bound to this task revision",
        )
        check(
            "kinds",
            set(task.required_artifact_kinds) <= {item.kind for item in artifacts},
            "Task is missing required artifact kinds",
        )
        check(
            "criteria",
            task.acceptance_criteria
            and all(
                task.criterion_results.get(criterion)
                and set(task.criterion_results[criterion]) <= set(task.artifact_ids)
                for criterion in task.acceptance_criteria
            ),
            "Every acceptance criterion requires committed artifact references",
        )
        evidence_ids = {key for item in artifacts for key in item.evidence_ids}
        check(
            "evidence",
            not task.requires_evidence
            or evidence_ids
            and all(self.evidence_valid(key, memory) for key in evidence_ids),
            "Task requires checked evidence; tool success is insufficient",
        )
        for key in task.finding_ids + [key for item in artifacts for key in item.finding_ids]:
            finding = memory.conclusions.get(key)
            check(
                f"finding:{key}",
                finding
                and finding.status not in {"conflicted", "retracted"}
                and not finding.needs_review
                and all(self.evidence_valid(item, memory) for item in finding.evidence_ids),
                "Finding requires current evidence and an auditable reasoning chain",
            )
            check(
                f"finding_sources:{key}",
                finding and all(self.source_intact(item, context) for item in finding.evidence_ids),
                "Finding source snapshots must remain intact",
            )
            if finding and finding.status == "tentative":
                check(
                    f"tentative:{key}",
                    False,
                    "Tentative finding needs disclosure or researcher review",
                    False,
                )
        for artifact in artifacts:
            checks.extend(self.artifact_checks(artifact, context))
        return self.result(checks)

    def inspect_project(self, plan: ResearchPlan, context: ResearchContext) -> CompletionResult:
        memory = context.memory
        project = memory.project
        assert project is not None
        objective = memory.objectives[project.objective_revision]
        checks = []

        def check(name: str, passed: object, message: str) -> None:
            checks.append(
                CheckResult(check_id=f"project:{name}", passed=bool(passed), message=message)
            )

        check(
            "plan",
            not memory.research_state.replan_required
            and plan.objective_revision == objective.revision,
            "Plan must address the current objective",
        )
        active = [task for task in plan.tasks if task.status != "cancelled"]
        check(
            "tasks",
            active and all(task.status == "completed" for task in active),
            "All required tasks must complete; blocked tasks are unfinished",
        )
        criteria = {criterion for task in active for criterion in task.acceptance_criteria}
        check(
            "requirements",
            set(objective.requirements) <= criteria,
            "Objective requirements must be covered by task criteria",
        )
        ids = {key for task in active for key in task.artifact_ids}
        artifacts = [memory.artifacts[key] for key in ids if key in memory.artifacts]
        check(
            "deliverables",
            all(
                any(value in {item.kind, item.title} for item in artifacts)
                for value in objective.deliverables
            ),
            "Objective deliverables are missing",
        )
        drafts = [item for item in artifacts if item.kind == "report_draft" and not item.stale]
        check("draft", drafts, "Project requires a report draft")
        check(
            "sections",
            any(set(objective.required_sections) <= set(item.sections) for item in drafts),
            "Report draft must cover required sections",
        )
        check(
            "conflicts",
            not any(
                item.core and item.status != "resolved" and item.plan_id == plan.id
                for item in memory.conflicts.values()
            ),
            "Core research conflicts must be resolved",
        )
        for task in active:
            checks.extend(self.inspect_task(task, context).checks)
        return self.result(checks)

    async def check_task(self, task: ResearchTask, context: ResearchContext) -> CompletionResult:
        return self.inspect_task(task, context)

    async def check_project(self, plan: ResearchPlan, context: ResearchContext) -> CompletionResult:
        return self.inspect_project(plan, context)
