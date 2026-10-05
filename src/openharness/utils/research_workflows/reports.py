"""Broker report comparability and explicit-assumption three-year forecasts."""

from collections import defaultdict
from decimal import Decimal
from .financial import amount_value
from .models import DeepResult, DigestResult

REQUIRED_ASSUMPTIONS = {
    "growth",
    "gross_margin",
    "selling_rate",
    "admin_rate",
    "rd_rate",
    "finance_rate",
    "tax_surcharge_rate",
    "other_operating_net",
    "non_operating_net",
    "tax_rate",
    "parent_share",
}
SECTION_KEYS = {
    "summary",
    "business",
    "industry",
    "financial_quality",
    "events",
    "forecast",
    "risks",
    "sources",
}


def normalize_digest(result: DigestResult) -> DigestResult:
    groups = defaultdict(list)
    for report in result.reports:
        if not report.institution or not report.published_at:
            report.gaps.append("机构或发布日期未知")
        if report.summary_only:
            report.gaps.append("仅取得摘要，未核验完整报告")
        if not report.views or not report.arguments or not report.risks:
            report.gaps.append("观点、论据或风险提示字段不完整")
        if not report.predictions:
            report.gaps.append("未取得盈利预测")
        if not report.authors:
            report.gaps.append("作者未知")
        if report.target_price is None:
            report.gaps.append("原文未提供或未取得目标价")
        elif not report.target_currency:
            report.gaps.append("目标价币种未知")
        if report.rating.current is None:
            report.gaps.append("原文未提供或未取得评级")
        if report.rating_change and not report.rating.change_explicit:
            # Comparing prior reports requires same institution and earlier date.
            matched = any(
                report.institution
                and other is not report
                and other.institution == report.institution
                and other.published_at
                and report.published_at
                and other.published_at < report.published_at
                and other.rating.current == report.rating.previous
                and report.rating.previous is not None
                for other in result.reports
            )
            if not matched:
                report.rating_change = None
                report.gaps.append("缺少同机构前次评级依据，评级变动未知")
        for prediction in report.predictions:
            if not prediction.currency or prediction.amount.value is None:
                report.gaps.append(
                    f"{prediction.year}/{prediction.metric}: 数值或币种未知，不作跨篇比较"
                )
                continue
            value = amount_value(prediction.amount)
            groups[
                (prediction.year, prediction.metric, prediction.currency, prediction.basis)
            ].append(
                {
                    "report": report.title,
                    "institution": report.institution,
                    "published_at": str(report.published_at) if report.published_at else None,
                    "value_yuan": value,
                    "references": [ref.model_dump() for ref in prediction.amount.references],
                }
            )
        result.gaps.extend(f"{report.title}: {gap}" for gap in report.gaps)
    result.comparison = [
        {
            "year": key[0],
            "metric": key[1],
            "currency": key[2],
            "basis": key[3],
            "predictions": values,
        }
        for key, values in groups.items()
    ]
    result.gaps = list(dict.fromkeys(result.gaps))
    result.status = "partial" if result.gaps else "complete"
    return result


def project_year(revenue: Decimal, values: dict) -> dict:
    revenue = revenue * (1 + values["growth"])
    gross_profit = revenue * values["gross_margin"]
    expenses = revenue * sum(
        values[name]
        for name in ("selling_rate", "admin_rate", "rd_rate", "finance_rate", "tax_surcharge_rate")
    )
    operating_profit = gross_profit - expenses + values["other_operating_net"]
    pretax = operating_profit + values["non_operating_net"]
    net_profit = pretax * (1 - values["tax_rate"])
    parent_profit = net_profit * values["parent_share"]
    shares = values.get("shares")
    return {
        "revenue": revenue,
        "gross_profit": gross_profit,
        "operating_profit": operating_profit,
        "pretax_profit": pretax,
        "net_profit": net_profit,
        "parent_net_profit": parent_profit,
        "eps": parent_profit / shares if shares is not None and shares > 0 else None,
    }


def calculate_deep(result: DeepResult) -> DeepResult:
    result.forecasts, result.sensitivity = [], []
    section_keys = [section.key for section in result.sections]
    if len(section_keys) != len(set(section_keys)):
        raise ValueError("报告章节不可重复")
    result.gaps.extend(f"缺少报告章节: {key}" for key in sorted(SECTION_KEYS - set(section_keys)))
    result.gaps.extend(gap for section in result.sections for gap in section.gaps)
    invalid_base = result.base_year != result.as_of.year - 1 or not result.base_currency
    if invalid_base:
        result.gaps.append(
            "预测基期须为截止时间最近已结束的完整财年；旧基期或当期累计收入不足以形成未来三年预测"
        )
    for section in result.sections:
        if not section.paragraphs:
            result.gaps.append(f"{section.title}: 章节缺少有依据的内容")
    base_revenue = amount_value(result.base_revenue)
    names = [scenario.name for scenario in result.scenarios]
    if set(names) != {"base", "optimistic", "cautious"} or len(names) != 3:
        result.gaps.append("基准、乐观、谨慎三种情景未全部提供")
    for scenario in result.scenarios:
        expected_years = list(range(result.base_year + 1, result.base_year + 4))
        if [year.year for year in scenario.years] != expected_years:
            raise ValueError("预测必须覆盖基期后的连续三个完整财年")
        revenue = base_revenue
        for year in scenario.years:
            unknown = set(year.assumptions) - REQUIRED_ASSUMPTIONS - {"shares"}
            if unknown:
                raise ValueError(f"未知预测假设: {sorted(unknown)}")
            missing = REQUIRED_ASSUMPTIONS - set(year.assumptions)
            values = {key: item.value for key, item in year.assumptions.items()}
            missing |= {key for key in REQUIRED_ASSUMPTIONS if values.get(key) is None}
            if missing or revenue is None or invalid_base:
                result.gaps.append(
                    f"{scenario.name}/{year.year}: 预测基期或假设缺失 {sorted(missing)}"
                )
                revenue = None
                continue
            if "shares" in values and (
                values["shares"] is None
                or values["shares"] <= 0
                or year.assumptions["shares"].origin == "analyst"
                or not year.assumptions["shares"].references
            ):
                values["shares"] = None
                result.gaps.append(f"{scenario.name}/{year.year}: 股本缺少可靠依据，EPS不计算")
            for key in ("gross_margin", "tax_rate", "parent_share"):
                if not 0 <= values[key] <= 1:
                    raise ValueError(f"{key}必须在0到1之间")
            if values["growth"] < -1:
                raise ValueError("收入增速不可低于-100%")
            for key in ("selling_rate", "admin_rate", "rd_rate", "tax_surcharge_rate"):
                if values[key] < 0:
                    raise ValueError(f"{key}不可为负")
            projection = project_year(revenue, values)
            result.forecasts.append(
                {
                    "scenario": scenario.name,
                    "year": year.year,
                    "currency": result.base_currency,
                    "unit": "元",
                    "previous_revenue": revenue,
                    "base_references": [ref.model_dump() for ref in result.base_revenue.references],
                    "values": projection,
                    "inputs": values,
                    "assumptions": {
                        key: item.model_dump() for key, item in year.assumptions.items()
                    },
                    "formulas": {
                        "revenue": "previous_revenue*(1+growth)",
                        "operating_profit": "revenue*(gross_margin-selling_rate-admin_rate-rd_rate-finance_rate-tax_surcharge_rate)+other_operating_net",
                        "pretax_profit": "operating_profit+non_operating_net",
                        "net_profit": "pretax_profit*(1-tax_rate)",
                        "parent_net_profit": "net_profit*parent_share",
                        "eps": "parent_net_profit/shares (reliable shares only)",
                    },
                    "limitation": "税率为简化有效税率假设，不推演递延所得税或完整三大表",
                }
            )
            if scenario.name == "base":
                for growth_shift in (Decimal("-0.02"), Decimal(0), Decimal("0.02")):
                    for margin_shift in (Decimal("-0.02"), Decimal(0), Decimal("0.02")):
                        changed = {
                            **values,
                            "growth": max(Decimal(-1), values["growth"] + growth_shift),
                            "gross_margin": min(
                                Decimal(1), max(Decimal(0), values["gross_margin"] + margin_shift)
                            ),
                        }
                        sensitivity = project_year(revenue, changed)
                        result.sensitivity.append(
                            {
                                "year": year.year,
                                "growth_shift": growth_shift,
                                "gross_margin_shift": margin_shift,
                                "parent_net_profit": sensitivity["parent_net_profit"],
                                "basis": "单年度冲击，其他输入及上一年收入保持基准情景",
                                "previous_revenue": revenue,
                                "inputs": changed,
                                "base_references": [
                                    ref.model_dump() for ref in result.base_revenue.references
                                ],
                            }
                        )
            revenue = projection["revenue"]
    result.gaps = list(dict.fromkeys(result.gaps))
    result.status = "partial" if result.gaps else "complete"
    return result
