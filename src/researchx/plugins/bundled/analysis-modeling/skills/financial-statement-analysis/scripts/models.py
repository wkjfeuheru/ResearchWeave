"""Versioned contracts owned by financial-statement-analysis."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import Field, field_validator, model_validator
from researchx.research.contracts import Amount, Contract, Reference, Result
from researchx.research.values import restore_decimal_fields


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
    def valid_period(self) -> FinancialPeriod:
        if self.end < self.start:
            raise ValueError("财务期间结束日不能早于开始日")
        from .analyze_statements import CORE_ITEMS

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
    ratios: list[dict[str, object]] = Field(default_factory=list)
    checks: list[dict[str, object]] = Field(default_factory=list)
    changes: list[dict[str, object]] = Field(default_factory=list)

    @field_validator("ratios", "checks", "changes")
    @classmethod
    def computed_numbers(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        return restore_decimal_fields(
            value,
            (
                "*.value",
                "*.inputs_yuan.*",
                "*.residual_yuan",
                "*.tolerance_yuan",
                "*.difference_yuan",
                "*.yoy",
            ),
        )
