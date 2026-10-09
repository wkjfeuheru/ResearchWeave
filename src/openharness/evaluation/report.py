"""Four-dimensional reports with explicit denominators, coverage and run slices."""

from __future__ import annotations

from typing_extensions import TypedDict
import json
import math
from collections import defaultdict
from pathlib import Path
from openharness.evaluation.models import RunArtifact
from openharness.utils.fs import atomic_write_text


class MetricSummary(TypedDict, total=False):
    count: int
    total: int
    coverage: float | None
    not_applicable: int
    unjudged: int
    mean: float | None
    p50: float | None
    p90: float | None
    p95: float | None
    numerator: float
    denominator: float
    micro: float | None


class FailureSummary(TypedDict):
    case_id: str
    trace_id: str
    status: str
    error: str | None
    judge_error: object
    record: str
    failed_checks: list[str]
    unjudged: list[str]


class EvaluationReport(TypedDict, total=False):
    task_count: int
    code_changed_during_experiment: bool
    calibrated: bool
    warning: str
    summary: dict[str, MetricSummary]
    groups: dict[str, dict[str, MetricSummary]]
    failures: list[FailureSummary]
    composite_score: None
    calibration: dict[str, object]
    planned_executions: int
    execution_coverage: float | None
    pending_executions: list[tuple[str, int]]
    execution_complete: bool


def read_results(directory: str | Path) -> list[RunArtifact]:
    return [
        RunArtifact.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted((Path(directory) / "results").glob("*.json"))
    ]


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * percent
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summary(artifacts: list[RunArtifact]) -> dict[str, MetricSummary]:
    names = {s.name for a in artifacts for s in a.scores}
    result: dict[str, MetricSummary] = {}
    for name in sorted(names):
        scores = [s for a in artifacts for s in a.scores if s.name == name]
        known = [s for s in scores if s.value is not None and s.status == "scored"]
        values = [s.value for s in known if s.value is not None]
        result[name] = {
            "count": len(known),
            "total": len(artifacts),
            "coverage": len(known) / len(artifacts) if artifacts else None,
            "not_applicable": sum(s.status == "not_applicable" for s in scores),
            "unjudged": sum(s.status == "unjudged" for s in scores),
            "mean": sum(values) / len(values) if values else None,
            "p50": percentile(values, 0.5),
            "p90": percentile(values, 0.9),
            "p95": percentile(values, 0.95),
            "numerator": sum(s.numerator or 0 for s in known),
            "denominator": sum(s.denominator or 0 for s in known),
        }
        if known and all(s.denominator is not None for s in known):
            den = result[name]["denominator"]
            result[name]["micro"] = result[name]["numerator"] / den if den else None
    return result


def create_report(artifacts: list[RunArtifact]) -> EvaluationReport:
    groups: dict[str, list[RunArtifact]] = defaultdict(list)
    failures: list[FailureSummary] = []
    for artifact in artifacts:
        groups[f"environment:{artifact.provenance.get('environment', 'unknown')}"].append(artifact)
        for dimension in ("category", "difficulty", "split", "material", "cache_condition"):
            groups[f"{dimension}:{artifact.provenance.get(dimension, 'unknown')}"].append(artifact)
        success = next((s.value for s in artifact.scores if s.name == "task_success"), None)
        groups["success_only" if success == 1 else "not_success"].append(artifact)
        if success == 1:
            groups[
                f"environment:{artifact.provenance.get('environment', 'unknown')}:success"
            ].append(artifact)
        if success != 1:
            failures.append(
                {
                    "case_id": artifact.case_id,
                    "trace_id": artifact.trace_id,
                    "status": artifact.status,
                    "error": artifact.error,
                    "judge_error": artifact.provenance.get("judge_error"),
                    "record": f"results/{artifact.case_id}-r{artifact.repetition}-{artifact.run_id}.json",
                    "failed_checks": [
                        s.name
                        for s in artifact.scores
                        if (
                            s.name.startswith(("requirement_", "path_"))
                            and s.status == "scored"
                            and s.value == 0
                        )
                    ],
                    "unjudged": [s.name for s in artifact.scores if s.status == "unjudged"],
                }
            )
    return {
        "task_count": len(artifacts),
        "code_changed_during_experiment": any(
            a.provenance.get("code_changed_during_experiment", False) for a in artifacts
        ),
        "calibrated": bool(artifacts) and all(a.calibrated for a in artifacts),
        "warning": "独立模型评分未经人工校准" if not all(a.calibrated for a in artifacts) else "",
        "summary": summary(artifacts),
        "groups": {k: summary(v) for k, v in groups.items()},
        "failures": failures,
        "composite_score": None,
    }


def write_report(directory: str | Path, artifacts: list[RunArtifact]) -> EvaluationReport:
    directory = Path(directory)
    from openharness.evaluation.calibration import review_report

    for a in artifacts:
        a.calibrated = False
    calibration = review_report(directory, artifacts)
    report = create_report(artifacts)
    report["calibration"] = calibration
    manifest_path = directory / "experiment.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        expected = {
            (case, repeat)
            for case in manifest["cases"]
            for repeat in range(1, manifest["repetitions"] + 1)
        }
        observed = {(a.case_id, a.repetition) for a in artifacts}
        report["planned_executions"] = len(expected)
        report["execution_coverage"] = (
            len(observed & expected) / len(expected) if expected else None
        )
        report["pending_executions"] = sorted(expected - observed)
        report["execution_complete"] = not report["pending_executions"]
    atomic_write_text(directory / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    rows = [
        "# Agent 四维评测报告",
        "",
        f"任务执行数：{len(artifacts)}；{report['warning']}",
        "",
        "固定和联网集分别比较；没有加权总分。所有数值来自本次执行，缺失不是零。",
        "",
    ]
    display = (
        "task_success",
        "path_correct",
        "unsupported_statement_rate",
        "citation_correctness",
        "citation_coverage",
        "total_tokens",
        "model_steps",
        "agent_decision_steps",
        "tool_steps",
        "end_to_end_ms",
        "first_content_ms",
    )

    def fmt(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.3f}"

    for group in (
        "environment:fixed",
        "environment:live",
        "environment:fixed:success",
        "environment:live:success",
    ):
        rows.extend(
            [
                "",
                f"## {group}",
                "",
                "| 指标 | 均值 | 判定数量/执行数 | P50 | P90 | P95 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for name in display:
            item = report["groups"].get(group, {}).get(name)
            if item:
                rows.append(
                    f"| {name} | {fmt(item['mean'])} | {item['count']}/{item['total']} | {fmt(item['p50'])} | {fmt(item['p90'])} | {fmt(item['p95'])} |"
                )
    rows.extend(["", "## 失败与未判定任务", ""])
    rows.extend(
        f"- [{f['case_id']}]({f['record']})：{f['status']}；未判定：{', '.join(f['unjudged']) or '无'}；trace {f['trace_id']}"
        for f in report["failures"]
    )
    atomic_write_text(directory / "report.md", "\n".join(rows) + "\n")
    return report


def compare_runs(candidate: list[RunArtifact], baseline: list[RunArtifact]) -> dict[str, object]:
    left = {(a.case_id, a.repetition): a for a in candidate}
    right = {(a.case_id, a.repetition): a for a in baseline}
    shared = sorted(left.keys() & right.keys())
    valid = [
        key
        for key in shared
        if left[key].dataset_version == right[key].dataset_version
        and left[key].provenance.get("environment") == right[key].provenance.get("environment")
        and left[key].provenance.get("judge_model") == right[key].provenance.get("judge_model")
        and left[key].provenance.get("judge_prompt_hash")
        == right[key].provenance.get("judge_prompt_hash")
        and left[key].provenance.get("judge_version") == right[key].provenance.get("judge_version")
        and not left[key].provenance.get("code_changed_during_experiment", False)
        and not right[key].provenance.get("code_changed_during_experiment", False)
    ]
    differences: dict[str, list[float]] = defaultdict(list)
    for key in valid:
        a = {s.name: s.value for s in left[key].scores if s.status == "scored"}
        b = {s.name: s.value for s in right[key].scores if s.status == "scored"}
        for name in a.keys() & b.keys():
            if a[name] is not None and b[name] is not None:
                left_value, right_value = a[name], b[name]
                assert left_value is not None and right_value is not None
                differences[name].append(left_value - right_value)
    return {
        "paired_count": len(valid),
        "excluded_incompatible": len(shared) - len(valid),
        "deltas": {
            k: {"n": len(v), "mean_candidate_minus_baseline": sum(v) / len(v)}
            for k, v in differences.items()
        },
        "note": "耗时/成本下降须结合任务完成、路径和事实质量判定，不自动认定为优化。",
    }
