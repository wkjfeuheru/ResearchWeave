"""Validated policy for a single model request, independent of run/cost budgets."""

from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

ContextComponent = Literal[
    "system_prompt", "conversation_history", "memory", "dynamic_context", "user_message", "other"
]
COMPONENT_KEYS: tuple[ContextComponent, ...] = (
    "system_prompt",
    "conversation_history",
    "memory",
    "dynamic_context",
    "user_message",
    "other",
)
DEFAULT_TARGET_SHARES = dict(zip(COMPONENT_KEYS, (15, 30, 10, 20, 15, 10)))
DEFAULT_MAX_SHARES = dict(zip(COMPONENT_KEYS, (30, 60, 20, 40, 100, 100)))
Percent = Annotated[int, Field(strict=True, ge=0, le=100)]
TokenLimit = Annotated[int, Field(strict=True, ge=0)]


class ContextComponentsSettings(BaseModel):
    """Soft construction targets and hard per-component input limits."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    target_shares: dict[ContextComponent, Percent] = Field(
        default_factory=lambda: dict(DEFAULT_TARGET_SHARES)
    )
    max_shares: dict[ContextComponent, Percent] = Field(
        default_factory=lambda: dict(DEFAULT_MAX_SHARES)
    )
    max_tokens: dict[ContextComponent, TokenLimit] = Field(default_factory=dict)

    @field_validator("target_shares", "max_shares")
    @classmethod
    def complete_keys(cls, value: dict[ContextComponent, int]) -> dict[ContextComponent, int]:
        if set(value) != set(COMPONENT_KEYS):
            raise ValueError("组件份额必须完整包含六分类键")
        return value

    @field_validator("target_shares")
    @classmethod
    def sum_to_one_hundred(cls, value: dict[ContextComponent, int]) -> dict[ContextComponent, int]:
        if sum(value.values()) != 100:
            raise ValueError("组件目标份额总和必须为 100")
        return value

    def limits(self, input_limit: int) -> tuple[dict[str, int], dict[str, int]]:
        targets: dict[str, int] = {
            key: input_limit * self.target_shares[key] // 100 for key in COMPONENT_KEYS
        }
        maxima: dict[str, int] = {
            key: min(
                input_limit, self.max_tokens.get(key, input_limit * self.max_shares[key] // 100)
            )
            for key in COMPONENT_KEYS
        }
        return targets, maxima
