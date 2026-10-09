"""Keep the opt-in real-run verdict honest when records contain revisions."""

import asyncio
import json

import pytest

from researchx.state.models import Evidence
from tests.test_web.real_research_eval import Evaluation


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    instance = Evaluation(tmp_path, 8767, None)
    yield instance
    asyncio.run(instance.client.aclose())


def financial_record(evaluation, statement):
    store = evaluation.memory_store("a" * 12)
    rows = [
        {
            "stock_name": "样本公司A",
            "stock_code": "000001",
            "year": year,
            "report_type": "q2",
            "report_form_type": form,
            "parcomp_n_profit": value,
        }
        for year, form, value in [(2026, "合并未调整", -51.19e8), (2025, "合并调整", -49.55e8)]
    ]
    source = store.capture(
        origin_id="finance-a",
        kind="mcp",
        title="ft_v1_finance_income",
        content=json.dumps({"data": rows}),
    )
    memory = store.load()
    evidence = Evidence(source_id=source.id, statement=statement, collected_at=source.collected_at)
    memory.evidence_pool[evidence.id] = evidence
    return store, memory, evidence


@pytest.mark.parametrize(
    "statement,passed",
    [
        ("样本公司A归母亏损同比收窄", False),
        ("样本公司A归母亏损绝对额扩大1.64亿元，不能据接口字段判断亏损收窄", True),
        ("样本公司A归母亏损同比收窄，不能据同比字段判断亏损扩大", False),
        ("样本公司A归母亏损绝对额扩大3.66亿元（约+7.4%）", False),
        ("样本公司A归母亏损绝对额扩大1.64亿元、幅度3.31%", True),
        (
            "样本公司A归母净利-51.19亿vs-49.55亿，亏损绝对额扩大1.64亿、幅度3.31%。"
            "原记录「归母亏损扩大3.66亿元（约+7.4%）」已撤回。",
            True,
        ),
    ],
)
def test_financial_audit_uses_current_claim_and_restatement_group(evaluation, statement, passed):
    _, memory, _ = financial_record(evaluation, statement)
    result = {
        "session_id": memory.session_id,
        "after": memory.model_dump(mode="json"),
        "checks": [],
    }
    evaluation.audit_loss_directions(result)
    assert result["checks"][0]["passed"] is passed
    assert len(result["financial_comparisons"]) == 1


def test_multiple_company_claims_need_multiple_input_sources(evaluation):
    store, memory, evidence = financial_record(evaluation, "样本公司A、样本公司B均亏损")
    source = store.capture(
        origin_id="finance-b",
        kind="mcp",
        title="ft_v1_finance_income",
        content=json.dumps({"data": [{"stock_name": "样本公司B"}]}),
    )
    memory.sources[source.id] = source
    result = {
        "session_id": memory.session_id,
        "after": memory.model_dump(mode="json"),
        "checks": [],
    }
    evaluation.audit_loss_directions(result)
    assert result["financial_provenance_issues"] == [evidence.id]


@pytest.mark.parametrize(
    "period,raw,passed",
    [
        ("2026年9月24日—9月30日", "每公斤40-43元人民币", True),
        ("2026-09-30", "报价40.0元/kg", True),
        ("2024年底-2026年上半年", "全球现货12美元/公斤", False),
    ],
)
def test_spot_audit_accepts_original_unit_order_but_rejects_old_quotes(
    evaluation, period, raw, passed
):
    store = evaluation.memory_store("a" * 12)
    source = store.capture(origin_id="quote", kind="web", content=raw)
    memory = store.load()
    memory.sources[source.id].collected_at = "2026-10-04T00:00:00Z"
    evidence = Evidence(
        source_id=source.id,
        statement="多晶硅现货40元/kg",
        period=period,
        collected_at=source.collected_at,
    )
    memory.evidence_pool[evidence.id] = evidence
    result = {
        "session_id": memory.session_id,
        "after": memory.model_dump(mode="json"),
        "checks": [],
    }
    evaluation.audit_recent_spot_prices(result)
    assert result["checks"][0]["passed"] is passed
