"""Common versioned research types; skill-specific contracts live in their plugins."""

from __future__ import annotations

from datetime import datetime
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
    def supported(self) -> Company:
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
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of requires a timezone")
        return value


class Amount(Contract):
    value: Decimal | None
    unit: Literal["元", "万元", "亿元", "千元", "百万元", "十亿元"] | None = None
    references: list[Reference] = Field(default_factory=list)
    missing_reason: str = ""

    @model_validator(mode="after")
    def grounded(self) -> Amount:
        if self.value is None and not self.missing_reason:
            raise ValueError("缺失数值须说明原因")
        if self.value is not None and (
            not self.value.is_finite() or not self.references or self.unit is None
        ):
            raise ValueError("数值须为有限值并附原始资料定位")
        return self


class Claim(Contract):
    text: str = Field(min_length=1)
    references: list[Reference] = Field(min_length=1)
