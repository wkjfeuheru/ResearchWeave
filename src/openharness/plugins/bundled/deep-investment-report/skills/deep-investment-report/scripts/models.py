"""Versioned contracts owned by deep-investment-report."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from pydantic import Field, field_validator, model_validator
from openharness.utils.research_types import Amount, Claim, Contract, Reference, Result
from openharness.utils.research_values import restore_decimal_fields


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

    @field_validator("forecasts", "sensitivity")
    @classmethod
    def computed_numbers(cls, value):
        return restore_decimal_fields(
            value,
            (
                "*.previous_revenue",
                "*.values.*",
                "*.inputs.*",
                "*.assumptions.*.value",
                "*.growth_shift",
                "*.gross_margin_shift",
                "*.parent_net_profit",
            ),
        )
