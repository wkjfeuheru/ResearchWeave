"""Count the prepared provider input and reserve output before model calls.

Text tokenization can be exact for a known encoding; protocol and vision costs
remain estimates. No credentials, HTTP headers or image base64 are tokenized.
"""

from __future__ import annotations
from openharness.engine.messages import ConversationMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openharness.api.client import SupportsStreamingMessages
from openharness.api.client import ApiMessageRequest

from dataclasses import dataclass, field, replace
from copy import deepcopy
import json
import math
import os
from typing import Any

from openharness.services.token_estimation import counting_method, estimate_tokens
from openharness.config.context_components import COMPONENT_KEYS, ContextComponentsSettings

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
    component_tokens: dict[str, int] = field(default_factory=dict)
    component_targets: dict[str, int] = field(default_factory=dict)
    component_max_tokens: dict[str, int] = field(default_factory=dict)
    component_overflows: dict[str, dict[str, int | bool]] = field(default_factory=dict)
    attribution_fallback_count: int = 0
    wire_input_tokens: int = 0
    component_policy_enabled: bool = True
    model: str = ""

    @property
    def fits(self) -> bool:
        return self.input_tokens <= self.input_limit

    def require_fit(self) -> None:
        exceeded = [
            key
            for key, value in self.component_tokens.items()
            if self.component_policy_enabled and value > self.component_max_tokens[key]
        ]
        details = "; ".join(
            f"{key}: {self.component_tokens[key]} > {self.component_max_tokens[key]} "
            f"(target={self.component_targets[key]})"
            for key in exceeded
        )
        if not self.fits:
            raise ContextBudgetError(
                f"上下文预算不足：预计输入 {self.input_tokens}，可用输入 {self.input_limit}，"
                f"输出预留 {self.output_tokens}，窗口 {self.window}。"
                "原始内容已保留；请核对 context_window_tokens、调整 max_tokens 或拆分输入。"
                + (f"组件硬上限明细：{details}" if details else "")
            )
        if self.component_policy_enabled:
            if exceeded:
                raise ContextBudgetError(
                    f"上下文组件超过硬上限：{details}；总输入 {self.input_tokens}/{self.input_limit}。"
                    "原始内容已保留；不截断用户文本、系统规则或工具 Schema。请调整组件预算或拆分输入。"
                )

    @property
    def components_fit(self) -> bool:
        return not self.component_policy_enabled or all(
            value <= self.component_max_tokens[key] for key, value in self.component_tokens.items()
        )


def budget_limits(
    model: str, output_tokens: int, window: int | None = None, threshold: int | None = None
) -> tuple[int, int, int, int, int]:
    capacity = get_context_window(model, context_window_tokens=window)
    safety = math.ceil(capacity * 0.05)
    available = capacity - output_tokens - safety
    if output_tokens <= 0 or available <= 0:
        raise ContextBudgetError(
            "上下文没有可用输入预算；请调整 context_window_tokens 或 max_tokens。原始内容已保留。"
        )
    if threshold is not None and threshold <= 0:
        raise ContextBudgetError("auto_compact_threshold_tokens 必须为正整数")
    trigger = min(threshold, available) if threshold is not None else max(1, int(available * 0.8))
    target = min(int(available * 0.6), int(trigger * 0.75))
    return capacity, safety, available, trigger, target


def prepare_request(
    client: SupportsStreamingMessages, request: ApiMessageRequest
) -> ApiMessageRequest:
    """Freeze the same input representation that the transport will send."""
    if request.prepared_payload is not None:
        return request
    prepare = getattr(client, "prepare_request", None)
    if prepare is not None:
        prepared = prepare(request)
        if not isinstance(prepared, ApiMessageRequest):
            raise TypeError("Provider prepare_request must return ApiMessageRequest")
        return replace(
            prepared,
            prepared_payload=deepcopy(prepared.prepared_payload),
            messages=[message.model_copy(deep=True) for message in prepared.messages],
            context_components=deepcopy(prepared.context_components),
        )
    # Test/custom transports can opt into prepare_request; default is Anthropic shape.
    payload = {
        "model": request.model,
        "system": request.system_prompt or "",
        "messages": [m.to_api_param() for m in request.messages],
        "tools": request.tools,
    }
    return replace(
        request,
        prepared_payload=deepcopy(payload),
        messages=[message.model_copy(deep=True) for message in request.messages],
        context_components=deepcopy(request.context_components),
    )


def _without_images(
    value: Any, *, messages: bool = False, content: bool = False
) -> tuple[Any, int]:
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
        list_result, images = [], 0
        for item in value:
            cleaned, count = _without_images(item, messages=messages, content=content)
            list_result.append(cleaned)
            images += count
        return list_result, images
    return value, 0


def request_budget(request: Any, *, threshold: int | None = None) -> RequestBudget:
    capacity, safety, available, trigger, target = budget_limits(
        request.model, request.max_tokens, request.context_window_tokens, threshold
    )
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
            text = (
                clean
                if isinstance(clean, str)
                else json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
            )
            components[name] = estimate_tokens(text, request.model)
    components["images"] = images * image_token_estimate()
    items = payload.get("messages", payload.get("input", []))
    components["protocol"] = 16 + 8 * len(items)
    policy = getattr(request, "context_components", None) or ContextComponentsSettings()
    targets, maxima = policy.limits(available)
    logical, fallback = _logical_components(request, components)
    wire_total = sum(components.values())
    # JSON framing, protocol, images, and positive tokenizer boundary differences
    # are O. Never lower the conservative pre-existing payload estimate.
    logical["other"] += components["images"] + components["protocol"]
    logical["other"] += max(0, wire_total - sum(logical.values()))
    overflows: dict[str, dict[str, int | bool]] = {
        key: {
            "target_overflow": max(0, logical[key] - targets[key]),
            "hard_overflow": max(0, logical[key] - maxima[key]),
            "target_exceeded": logical[key] > targets[key],
            "hard_limit_exceeded": logical[key] > maxima[key],
        }
        for key in COMPONENT_KEYS
    }
    return RequestBudget(
        capacity,
        request.max_tokens,
        safety,
        sum(logical.values()),
        available,
        trigger,
        target,
        components,
        counting_method(request.model),
        logical,
        targets,
        maxima,
        overflows,
        fallback,
        wire_total,
        policy.enabled,
        request.model,
    )


def _logical_components(
    request: ApiMessageRequest, wire: dict[str, int]
) -> tuple[dict[str, int], int]:
    from openharness.engine.messages import TextBlock, ToolUseBlock, ToolResultBlock
    from openharness.services.context_sources import latest_user_index, runtime_fragments

    counts: dict[str, int] = dict.fromkeys(COMPONENT_KEYS, 0)
    payload = request.prepared_payload or {}
    counts["system_prompt"] = (
        wire.get("system", 0) + wire.get("instructions", 0) + wire.get("tools", 0)
    )
    for item in payload.get("messages", []):
        if item.get("role") in {"system", "developer"}:
            value = item.get("content", "")
            if isinstance(value, str):
                counts["system_prompt"] += estimate_tokens(value, request.model)
            elif isinstance(value, list):
                counts["system_prompt"] += sum(
                    estimate_tokens(block.get("text", ""), request.model)
                    for block in value
                    if isinstance(block, dict)
                )
        if item.get("reasoning_content"):
            counts["conversation_history"] += estimate_tokens(
                item["reasoning_content"], request.model
            )
    current, fallback = latest_user_index(
        request.messages, getattr(request, "current_user_message_id", None)
    )
    for index, message in enumerate(request.messages):
        content = message.api_content()
        if message.runtime_context and message.role == "user":
            # api_content appends one envelope. Count its content exactly once via
            # spans; wire framing is reconciled to O, not duplicated as U/H.
            content = content[:-1]
            fragments, missed = runtime_fragments(
                message.runtime_context, message.runtime_context_manifest
            )
            fallback += missed
            for text, component, _ in fragments:
                counts[component] += estimate_tokens(text, request.model)
        for block in content:
            if isinstance(block, TextBlock):
                key = block.context_component or (
                    "user_message" if index == current else "conversation_history"
                )
                if key == "user_message" and index != current:
                    key = "conversation_history"
                counts[key] += estimate_tokens(block.text, request.model)
            elif isinstance(block, ToolResultBlock):
                key = block.result_metadata.get("context_component", "conversation_history")
                if key not in COMPONENT_KEYS or key == "user_message":
                    key = "conversation_history"
                    fallback += 1
                counts[key] += estimate_tokens(block.content, request.model)
            elif isinstance(block, ToolUseBlock):
                counts["conversation_history"] += estimate_tokens(
                    json.dumps(
                        {"id": block.id, "name": block.name, "input": block.input},
                        ensure_ascii=False,
                    ),
                    request.model,
                )
    return counts, fallback


def defer_optional_dynamic(
    messages: list[ConversationMessage], budget: RequestBudget
) -> tuple[list[ConversationMessage], int]:
    """Drop only explicitly deferrable D ranges on request copies, never history/U."""
    from openharness.services.context_sources import runtime_fragments
    from openharness.engine.messages import ContextSpan

    if (
        not budget.component_policy_enabled
        or not budget.component_overflows["dynamic_context"]["target_exceeded"]
    ):
        return list(messages), 0
    result, deferred = [], 0
    for message in messages:
        if not message.runtime_context or not message.runtime_context_manifest:
            result.append(message)
            continue
        fragments, fallback = runtime_fragments(
            message.runtime_context, message.runtime_context_manifest
        )
        if fallback:
            result.append(message)
            continue
        text, spans = "", []
        for body, key, optional in fragments:
            if key == "dynamic_context" and optional:
                deferred += 1
                continue
            start = len(text)
            text += body
            spans.append(ContextSpan(start=start, end=len(text), component=key))
        result.append(
            message.model_copy(
                update={"runtime_context": text or None, "runtime_context_manifest": spans or None}
            )
        )
    return result, deferred


def checked_request(client: Any, request: Any) -> Any:
    prepared = prepare_request(client, request)
    request_budget(prepared).require_fit()
    return prepared
