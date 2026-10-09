"""Independent judge validation, bounded repairs and canonical report bodies."""

import pytest

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.engine.messages import ConversationMessage, TextBlock
from researchx.evaluation.dataset import load_cases
from researchx.evaluation.judge import (
    JudgeOutputError,
    citation_map,
    evaluated_text,
    exact_quote,
    judge_case,
    validate_judgment,
)
from researchx.evaluation.scoring import score_case
from tests.test_evaluation.test_scoring import judgment, run, values


def test_whitespace_quotes_recover_literal_but_paraphrases_fail():
    case = load_cases()[0]
    artifact = run()
    review = judgment(case)
    artifact.sources["src1"] = "毛利率\n40%"
    review.claims[0].citations[0].quote = "毛利率40%"
    validated = validate_judgment(case, artifact, review.model_dump_json())
    assert validated.claims[0].citations[0].quote == "毛利率\n40%"
    assert exact_quote("毛利率是40%", "毛利率40%") == "毛利率是40%"
    review.claims[0].citations[0].quote = "毛利率是40%"
    with pytest.raises(JudgeOutputError):
        validate_judgment(case, artifact, review.model_dump_json())


def test_nonexistent_reference_is_a_scored_error_not_judge_failure():
    case = load_cases()[0]
    artifact = run()
    review = judgment(case)
    citation = review.claims[0].citations[0]
    citation.source_id, citation.supported, citation.quote = None, False, ""
    result = validate_judgment(case, artifact, review.model_dump_json())
    assert values(score_case(case, artifact, result))["citation_correctness"] == 0


def test_line_locations_are_extracted_from_actual_answer_and_source():
    case, artifact = load_cases()[0], run()
    review = judgment(case)
    artifact.sources["src1"] = "营业收入100。\n营业成本60。\n毛利率40%。"
    for item in review.requirements:
        item.quote = ""
        item.answer_line = 1
    review.claims[0].quote = ""
    review.claims[0].answer_line = 1
    citation = review.claims[0].citations[0]
    citation.quote, citation.source_line = "", 3
    result = validate_judgment(case, artifact, review.model_dump_json())
    assert result.claims[0].quote == artifact.answer
    assert result.claims[0].citations[0].quote == "毛利率40%。"
    citation.source_line = 10000
    with pytest.raises(JudgeOutputError):
        validate_judgment(case, artifact, review.model_dump_json())


def test_multiformat_reports_count_once_and_references_are_namespaced():
    artifact = run()
    artifact.artifacts = {
        "a.md": "结论A[R1]。\n- [R1] 年报；report.txt；第1页",
        "a.docx": "结论A[R1]。",
        "b.md": "结论B[R1]。\n- [R1] 研报；other.txt；第2页",
        "intermediate.json": "不应计入最终报告",
    }
    artifact.artifact_metadata = {
        key: {"registered": key != "intermediate.json"} for key in artifact.artifacts
    }
    artifact.research_state["sources"]["src2"] = {"id": "src2", "locator": "other.txt"}
    text = evaluated_text(artifact)
    assert text.count("结论A") == 1 and "不应计入" not in text
    references = citation_map(artifact)
    assert {key for key in references if key.startswith("[report:")}
    assert {"src1", "src2"} <= set(references.values())


@pytest.mark.asyncio
async def test_judge_repairs_only_once_and_accounts_for_both_attempts():
    case, artifact = load_cases()[0], run()
    good = judgment(case)
    invalid = good.model_copy(deep=True)
    invalid.requirements[0].quote = "这是编造的回答片段"

    class Judge:
        requests = []

        async def stream_message(self, request):
            self.requests.append(request)
            output = invalid if len(self.requests) == 1 else good
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant", content=[TextBlock(text=output.model_dump_json())]
                ),
                usage=UsageSnapshot(input_tokens=100, output_tokens=10, usage_reported=True),
            )

    provider = Judge()
    result = await judge_case(case, artifact, provider, "independent-judge")
    assert result.claims_complete
    assert len(provider.requests) == len(artifact.judge_attempts) == len(artifact.judge_usage) == 2
    assert "validation_error" in artifact.judge_attempts[0]
    assert "previous_validation_error" in str(provider.requests[1])


@pytest.mark.asyncio
async def test_interrupted_judge_usage_remains_unknown():
    case, artifact = load_cases()[0], run()

    class BrokenJudge:
        async def stream_message(self, request):
            raise OSError("service unavailable")
            yield

    with pytest.raises(OSError):
        await judge_case(case, artifact, BrokenJudge(), "judge")
    result = values(score_case(case, artifact))
    assert result["judge_total_tokens"] is None
    assert result["judge_model_calls"] == 1 and result["judge_token_reporting_coverage"] == 0
