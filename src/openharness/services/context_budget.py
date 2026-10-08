"""Count the prepared provider input and reserve output before model calls.

Text tokenization can be exact for a known encoding; protocol and vision costs
remain estimates. No credentials, HTTP headers or image base64 are tokenized.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
import json
import math
import os
from typing import Any

from openharness.services.token_estimation import counting_method, estimate_tokens

# Explicit conservative operating capacities, not substring/family inference.
# Claude subscription/gateway limits may be lower than the public API's 1M:
# https://platform.claude.com/docs/en/models/sonnet-4-6/overview
# https://developers.openai.com/api/docs/models/gpt-5.4
MODEL_WINDOWS = {
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-5-20250929": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4o-2024-08-06": 128_000,
    "gpt-4o-mini-2024-07-18": 128_000,
    "gpt-5.4": 1_050_000,
}


class ContextBudgetError(ValueError):
    """The configured or estimated request cannot safely be sent."""


def get_context_window(model: str, *, context_window_tokens: int | None = None) -> int:
    if context_window_tokens is not None:
        if context_window_tokens <= 0:
            raise ContextBudgetError("context_window_tokens 必须为正整数")
        return context_window_tokens
    if model in MODEL_WINDOWS:
        return MODEL_WINDOWS[model]
    raise ContextBudgetError(
        f"无法确定模型 {model!r} 的上下文窗口，请配置 context_window_tokens 后重试。原始内容已保留。"
    )


def image_token_estimate() -> int:
    try:
        return max(64, int(os.environ.get("OPENHARNESS_IMAGE_TOKEN_ESTIMATE", "3072")))
    except ValueError:
        return 3072


@dataclass(frozen=True)
class RequestBudget:
    window: int
    output_tokens: int
    safety_tokens: int
    input_tokens: int
    input_limit: int
    trigger_tokens: int
    target_tokens: int
    components: dict[str, int]
    counting_method: str

    @property
    def fits(self) -> bool:
        return self.input_tokens <= self.input_limit

    def require_fit(self) -> None:
        if not self.fits:
            raise ContextBudgetError(
                f"上下文预算不足：预计输入 {self.input_tokens}，可用输入 {self.input_limit}，"
                f"输出预留 {self.output_tokens}，窗口 {self.window}。"
                "原始内容已保留；请核对 context_window_tokens、调整 max_tokens 或拆分输入。"
            )


def budget_limits(model: str, output_tokens: int, window: int | None = None,
                  threshold: int | None = None) -> tuple[int, int, int, int, int]:
    capacity = get_context_window(model, context_window_tokens=window)
    safety = math.ceil(capacity * .05)
    available = capacity - output_tokens - safety
    if output_tokens <= 0 or available <= 0:
        raise ContextBudgetError(
            "上下文没有可用输入预算；请调整 context_window_tokens 或 max_tokens。原始内容已保留。"
        )
    if threshold is not None and threshold <= 0:
        raise ContextBudgetError("auto_compact_threshold_tokens 必须为正整数")
    trigger = min(threshold, available) if threshold is not None else max(1, int(available * .8))
    target = min(int(available * .6), int(trigger * .75))
    return capacity, safety, available, trigger, target


def prepare_request(client: Any, request: Any) -> Any:
    """Freeze the same input representation that the transport will send."""
    if request.prepared_payload is not None:
        return request
    prepare = getattr(client, "prepare_request", None)
    if prepare is not None:
        prepared = prepare(request)
        return replace(prepared, prepared_payload=deepcopy(prepared.prepared_payload))
    # Test/custom transports can opt into prepare_request; default is Anthropic shape.
    payload = {"model": request.model, "system": request.system_prompt or "",
               "messages": [m.to_api_param() for m in request.messages], "tools": request.tools}
    return replace(request, prepared_payload=deepcopy(payload))


def _without_images(value: Any, *, messages: bool = False, content: bool = False) -> tuple[Any, int]:
    if isinstance(value, dict):
        if content and value.get("type") in {"image", "image_url", "input_image"}:
            return {"type": value["type"], "image": "[image]"}, 1
        result, images = {}, 0
        for key, item in value.items():
            cleaned, count = _without_images(item, content=messages and key == "content")
            result[key] = cleaned
            images += count
        return result, images
    if isinstance(value, list):
        result, images = [], 0
        for item in value:
            cleaned, count = _without_images(item, messages=messages, content=content)
            result.append(cleaned)
            images += count
        return result, images
    return value, 0


def request_budget(request: Any, *, threshold: int | None = None) -> RequestBudget:
    capacity, safety, available, trigger, target = budget_limits(
        request.model, request.max_tokens, request.context_window_tokens, threshold)
    payload = request.prepared_payload
    if payload is None:
        raise ValueError("Prepare the provider request before counting it")
    components: dict[str, int] = {}
    images = 0
    # Only model input, never authentication or transport metadata.
    for name in ("system", "instructions", "messages", "input", "tools"):
        value = payload.get(name)
        if value:
            clean, count = _without_images(value, messages=name in {"messages", "input"})
            images += count
            text = clean if isinstance(clean, str) else json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
            components[name] = estimate_tokens(text, request.model)
    components["images"] = images * image_token_estimate()
    items = payload.get("messages", payload.get("input", []))
    components["protocol"] = 16 + 8 * len(items)
    return RequestBudget(capacity, request.max_tokens, safety, sum(components.values()),
                         available, trigger, target, components, counting_method(request.model))


def checked_request(client: Any, request: Any) -> Any:
    prepared = prepare_request(client, request)
    request_budget(prepared).require_fit()
    return prepared
