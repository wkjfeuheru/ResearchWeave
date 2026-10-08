"""Adversarial scoring contracts, not implementation snapshots."""

from openharness.evaluation.dataset import load_cases
from openharness.evaluation.judge import (
    CitationReview,
    ClaimReview,
    JudgeResult,
    PathReview,
    RequirementReview,
)
from openharness.evaluation.models import Observation, RunArtifact
from openharness.evaluation.observer import timestamp
from openharness.evaluation.report import compare_runs, create_report
from openharness.evaluation.scoring import numeric_match, path_checks, score_case


def run(answer="毛利率为40%[1]。"):
    return RunArtifact(
        run_id="run",
        case_id="financial-syn-01",
        dataset_version="v1",
        trace_id="0" * 32,
        started_at=timestamp(),
        status="completed",
        answer=answer,
        elapsed_ms=100,
        research_state={
            "sources": {"src1": {"id": "src1", "locator": "report.txt"}},
            "evidence_pool": {
                "ev1": {
                    "id": "ev1",
                    "source_id": "src1",
                    "status": "source_checked",
                    "needs_review": False,
                }
            },
            "answers": {
                "a": {
                    "rendered": answer,
                    "citations": {
                        "ev1": {
                            "number": 1,
                            "evidence": {
                                "id": "ev1",
                                "source_id": "src1",
                                "status": "source_checked",
                            },
                            "source": {"id": "src1", "locator": "report.txt"},
                        }
                    },
                }
            },
        },
        sources={"src1": "营业收入100，营业成本60，毛利率40%。"},
    )


def judgment(case, *, supported=True, cited_source="src1", quote="毛利率为40%[1]。"):
    return JudgeResult(
        requirements=[
            RequirementReview(
                id=r.id,
                score=4,
                quote=quote,
                observed_value="40" if r.check == "numeric" else None,
                unit=r.unit,
                currency=r.currency,
                period=r.period,
                scope=r.scope,
                explanation="检查正文",
                source_ids=["src1"],
            )
            for r in case.requirements
        ],
        path=PathReview(
            tool_selection=4, skill_use=4, dependencies=4, conflict=None, explanation="等价路径"
        ),
        claims=[
            ClaimReview(
                text="毛利率40%",
                quote=quote,
                kind="fact",
                support="supported" if supported else "unsupported",
                requires_citation=True,
                source_ids=["src1"] if supported else [],
                citations=[
                    CitationReview(
                        marker="[1]",
                        source_id=cited_source,
                        supported=supported,
                        quote="毛利率40%",
                        explanation="检查引用",
                    )
                ],
                explanation="逐句核对",
            )
        ],
        claims_complete=True,
        overall_explanation="独立评分",
    )


def values(scores):
    return {score.name: score.value for score in scores}


def test_correct_number_with_wrong_citation_cannot_pass():
    case = load_cases()[0]
    artifact = run()
    result = values(score_case(case, artifact, judgment(case, cited_source="wrong")))
    assert result["citation_correctness"] == 0
    assert result["task_success"] == 0
    assert result["requirement_numeric"] == 1


def test_existing_id_does_not_prove_source_support():
    case = load_cases()[0]
    result = values(score_case(case, run(), judgment(case, supported=False)))
    assert result["unsupported_statement_rate"] == 1
    assert result["citation_correctness"] == 0


def test_missing_judge_and_missing_usage_are_unknown():
    artifact = run()
    artifact.observations = [
        Observation(
            id="g1",
            name="model",
            kind="generation",
            started_at=timestamp(),
            status="ok",
            usage={"input_tokens": 0, "output_tokens": 0, "reported": False},
        )
    ]
    result = values(score_case(load_cases()[0], artifact))
    assert result["task_success"] is None
    assert result["requirement_coverage"] is None
    assert result["total_tokens"] is None
    assert result["unsupported_statement_rate"] is None
    assert result["token_reporting_coverage"] == 0


def test_cached_tokens_are_not_counted_twice():
    artifact = run()
    artifact.observations = [
        Observation(
            id="g1",
            name="model",
            kind="generation",
            started_at=timestamp(),
            status="ok",
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 0,
                "cache_observed_input_tokens": 100,
                "reported": True,
            },
        )
    ]
    result = values(score_case(load_cases()[0], artifact))
    assert result["total_tokens"] == 120
    assert result["cache_hit_rate"] == 0.8


def test_empty_answer_is_failure_not_zero_hallucinations():
    artifact = run("")
    result = values(score_case(load_cases()[0], artifact))
    assert result["task_success"] == 0 and result["unsupported_statement_rate"] is None


def test_numeric_quote_unit_and_period_are_checked():
    case = load_cases()[0]
    requirement = next(r for r in case.requirements if r.check == "numeric")
    review = judgment(case).requirements[-1]
    assert numeric_match(requirement, review)
    assert not numeric_match(requirement, review.model_copy(update={"observed_value": "41"}))
    assert not numeric_match(requirement, review.model_copy(update={"period": "2024"}))
    assert not numeric_match(requirement, review.model_copy(update={"unit": "倍"}))


def test_loading_skill_and_echoing_script_do_not_count_as_execution():
    case = load_cases()[0]
    artifact = run()
    artifact.observations = [
        Observation(
            id="t1",
            name="skill",
            kind="tool",
            started_at=timestamp(),
            status="ok",
            input={"name": "financial-statement-analysis"},
        ),
        Observation(
            id="t2",
            name="bash",
            kind="tool",
            started_at=timestamp(),
            status="ok",
            input={"command": "echo analyze_statements.py"},
        ),
    ]
    checks = path_checks(case, artifact)
    assert checks["skill_use"] == [True, False]
    artifact.observations[-1].input = {
        "command": "python /resources/scripts/analyze_statements.py --input input.json --output result.json"
    }
    assert path_checks(case, artifact)["skill_use"] == [True, True]


def test_superseded_evidence_invalidates_path_and_equivalent_tools_work():
    case = load_cases()[0]
    artifact = run()
    artifact.observations = [
        Observation(id="t1", name="read_file", kind="tool", started_at=timestamp(), status="ok")
    ]
    assert all(path_checks(case, artifact)["tool_selection"])
    artifact.research_state["evidence_pool"]["ev2"] = {
        "id": "ev2",
        "source_id": "src1",
        "supersedes": "ev1",
        "status": "source_checked",
    }
    assert not all(path_checks(case, artifact)["dependencies"])


def test_report_coverage_success_filter_and_compatible_pairs():
    a = run()
    a.provenance = {"environment": "fixed", "category": "financial"}
    a.scores = score_case(load_cases()[0], a)
    result = create_report([a])
    assert result["summary"]["task_success"]["coverage"] == 0
    assert not result["calibrated"] and result["composite_score"] is None
    b = a.model_copy(deep=True)
    b.dataset_version = "other"
    assert compare_runs([a], [b])["paired_count"] == 0
