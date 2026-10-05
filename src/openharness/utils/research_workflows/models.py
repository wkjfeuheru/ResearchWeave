"""Versioned skill interchange contracts. Missing values never become zero."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reference(Contract):
    locator: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_id: str | None = None
    evidence_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    published_at: str | None = None
    excerpt: str = ""


class Company(Contract):
    name: str = Field(min_length=1)
    code: str = Field(pattern=r"^\d{6}$")
    market: Literal["SSE", "SZSE", "BSE"]
    industry: str = Field(min_length=1)
    financial_sector: bool = False
    identity_sources: list[Reference] = Field(min_length=1)

    @model_validator(mode="after")
    def supported(self):
        if self.financial_sector:
            raise ValueError("首版仅支持A股非金融企业；金融企业需要专属科目与比率")
        return self


class Result(Contract):
    schema_version: Literal[1] = 1
    company: Company
    inputs: list[Reference] = Field(min_length=1)
    status: Literal["complete", "partial", "blocked"] = "partial"
    gaps: list[str] = Field(default_factory=list)
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("as_of requires a timezone")
        return value


class Amount(Contract):
    value: Decimal | None
    unit: Literal["元", "万元", "亿元", "千元", "百万元", "十亿元"] | None = None
    references: list[Reference] = Field(default_factory=list)
    missing_reason: str = ""

    @model_validator(mode="after")
    def grounded(self):
        if self.value is None and not self.missing_reason:
            raise ValueError("缺失数值须说明原因")
        if self.value is not None and (
            not self.value.is_finite() or not self.references or self.unit is None
        ):
            raise ValueError("数值须为有限值并附原始资料定位")
        return self


class FinancialPeriod(Contract):
    label: str
    start: date
    end: date
    currency: str | None = None
    scope: Literal["consolidated", "parent"] = "consolidated"
    restated: bool = False
    # Exact aliases are in references/accounting.md; unknown keys fail validation.
    items: dict[str, Amount]
    rounding_unit: Decimal = Field(default=Decimal("1"), gt=0)

    @model_validator(mode="after")
    def valid_period(self):
        if self.end < self.start:
            raise ValueError("财务期间结束日不能早于开始日")
        from .financial import CORE_ITEMS

        if unknown := set(self.items) - set(CORE_ITEMS):
            raise ValueError(f"未知财务科目: {sorted(unknown)}")
        return self


class Finding(Contract):
    topic: str
    fact: str
    interpretation: str
    references: list[Reference] = Field(min_length=1)
    gaps: list[str] = Field(default_factory=list)


class FinancialResult(Result):
    kind: Literal["financial"] = "financial"
    periods: list[FinancialPeriod] = Field(min_length=1)
    notes: list[Finding] = Field(default_factory=list)
    ratios: list[dict] = Field(default_factory=list)
    checks: list[dict] = Field(default_factory=list)
    changes: list[dict] = Field(default_factory=list)


class Score(Contract):
    value: int | None = Field(ge=-2, le=2, strict=True)
    reason: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]


class Event(Contract):
    event_key: str = Field(
        min_length=1, description="Stable company/category/event identity, not article URL"
    )
    title: str
    occurred_at: datetime | None = None
    published_at: datetime | None = None
    category: Literal[
        "merger",
        "shareholding",
        "earnings_guidance",
        "financial_report",
        "financing_dividend",
        "litigation_regulation",
        "operating_contract",
        "management",
        "other",
    ]
    summary: str
    references: list[Reference] = Field(min_length=1)
    sentiment: Score
    impact: Score
    summary_only: bool = False

    @field_validator("occurred_at", "published_at")
    @classmethod
    def timezone_required(cls, value):
        if value and value.tzinfo is None:
            raise ValueError("事件日期须带时区")
        return value


class Channel(Contract):
    name: str
    state: Literal["ok", "failed", "stale", "summary_only"]
    detail: str = ""


class MonitorResult(Result):
    kind: Literal["monitor"] = "monitor"
    window_start: datetime | None = None
    events: list[Event] = Field(default_factory=list)
    undated_events: list[Event] = Field(default_factory=list)
    outside_window: list[Event] = Field(default_factory=list)
    channels: list[Channel] = Field(min_length=1)


class Claim(Contract):
    text: str = Field(min_length=1)
    references: list[Reference] = Field(min_length=1)


class Prediction(Contract):
    year: int = Field(ge=2000, le=2100)
    metric: str
    amount: Amount
    currency: str | None = None
    basis: str = Field(min_length=1)


class Rating(Contract):
    current: str | None = None
    previous: str | None = None
    change_explicit: bool = False
    references: list[Reference] = Field(default_factory=list)

    @model_validator(mode="after")
    def grounded(self):
        if (self.current or self.previous) and not self.references:
            raise ValueError("评级须有原文依据")
        return self


class BrokerReport(Contract):
    title: str
    institution: str | None
    authors: list[str] = Field(default_factory=list)
    published_at: date | None
    references: list[Reference] = Field(min_length=1)
    summary_only: bool = False
    views: list[Claim] = Field(default_factory=list)
    arguments: list[Claim] = Field(default_factory=list)
    assumptions: list[Claim] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    target_price: Amount | None = None
    target_currency: str | None = None
    rating: Rating = Field(default_factory=Rating)
    rating_change: str | None = None
    risks: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class DigestResult(Result):
    kind: Literal["digest"] = "digest"
    reports: list[BrokerReport] = Field(min_length=1)
    comparison: list[dict] = Field(default_factory=list)


class Assumption(Contract):
    value: Decimal | None
    rationale: str = Field(min_length=1)
    origin: Literal["historical", "external", "analyst"]
    references: list[Reference] = Field(default_factory=list)

    @model_validator(mode="after")
    def grounded(self):
        if self.value is not None and not self.value.is_finite():
            raise ValueError("假设须为有限值")
        if self.origin != "analyst" and not self.references:
            raise ValueError("历史或外部假设须附依据")
        return self


class ForecastYear(Contract):
    year: int
    assumptions: dict[str, Assumption]


class Scenario(Contract):
    name: Literal["base", "optimistic", "cautious"]
    years: list[ForecastYear] = Field(min_length=3, max_length=3)


class ReportSection(Contract):
    key: Literal[
        "summary",
        "business",
        "industry",
        "financial_quality",
        "events",
        "forecast",
        "risks",
        "sources",
    ]
    title: str
    paragraphs: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class DeepResult(Result):
    kind: Literal["deep"] = "deep"
    base_year: int
    base_revenue: Amount
    base_currency: str | None = None
    base_scope: Literal["consolidated"] = "consolidated"
    scenarios: list[Scenario] = Field(default_factory=list)
    sections: list[ReportSection] = Field(default_factory=list)
    prior_results: list[Reference] = Field(default_factory=list)
    forecasts: list[dict] = Field(default_factory=list)
    sensitivity: list[dict] = Field(default_factory=list)


RESULT_TYPES = {
    "financial": FinancialResult,
    "monitor": MonitorResult,
    "digest": DigestResult,
    "deep": DeepResult,
}
