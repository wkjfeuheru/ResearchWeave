"""Coverage, incompatible judges and unfinished human calibration stay visible."""

import json

from openharness.evaluation.calibration import review_report
from openharness.evaluation.models import MetricResult
from openharness.evaluation.report import compare_runs, write_report
from tests.test_evaluation.test_scoring import run


def test_report_marks_partial_experiment_and_different_judges_incompatible(tmp_path):
    artifact = run()
    artifact.scores = [MetricResult(name="task_success", value=0)]
    (tmp_path / "experiment.json").write_text(
        json.dumps(
            {
                "cases": [artifact.case_id, "pending"],
                "repetitions": 1,
            }
        )
    )
    result = write_report(tmp_path, [artifact])
    assert result["execution_coverage"] == 0.5 and not result["execution_complete"]
    assert result["pending_executions"] == [("pending", 1)]
    other = artifact.model_copy(deep=True)
    other.provenance["judge_prompt_hash"] = "different"
    assert compare_runs([artifact], [other])["paired_count"] == 0


def test_calibration_requires_matching_trace_and_valid_human_scores(tmp_path):
    artifact = run()
    artifact.judge_results = {"claims_complete": True}
    artifact.provenance["judge_prompt_hash"] = "judge"
    (tmp_path / "dataset").mkdir()
    (tmp_path / "dataset/calibration.jsonl").write_text(
        json.dumps(
            {
                "case_id": artifact.case_id,
                "status": "pending",
            }
        )
        + "\n"
    )
    review = {
        "case_id": artifact.case_id,
        "trace_id": artifact.trace_id,
        "dataset_version": artifact.dataset_version,
        "judge_prompt_hash": "judge",
        "status": "reviewed",
        "reviewer": "human",
        "basis": ["原文第1页"],
        "human_scores": {
            "task_success": 1,
            "path_correct": 1,
            "unsupported_statement_rate": 0,
            "citation_correctness": 1,
            "citation_coverage": 1,
        },
    }
    path = tmp_path / "calibration-reviews.jsonl"
    path.write_text(json.dumps(review) + "\n")
    result = review_report(tmp_path, [artifact])
    assert result["reviewed"] == 1 and not result["complete"]
    review["trace_id"] = "wrong"
    path.write_text(json.dumps(review) + "\n")
    assert review_report(tmp_path, [artifact])["reviewed"] == 0
    review["trace_id"] = artifact.trace_id
    review["human_scores"]["task_success"] = 4
    path.write_text(json.dumps(review) + "\n")
    assert review_report(tmp_path, [artifact])["reviewed"] == 0
