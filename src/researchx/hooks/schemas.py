"""Hook configuration schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class HookPolicy(BaseModel):
    semantics: Literal["effect", "observer"] = "effect"
    max_payload_bytes: int = Field(default=65536, ge=256, le=1048576)
    max_output_bytes: int = Field(default=65536, ge=256, le=1048576)

    @model_validator(mode="after")
    def observer_cannot_block(self) -> HookPolicy:
        if self.semantics == "observer" and getattr(self, "block_on_failure", False):
            raise ValueError("Observer hooks cannot block core execution")
        return self


class PolicyHookDefinition(BaseModel):
    """Deterministic policy only; no command, HTTP or model execution."""

    type: Literal["policy"] = "policy"
    semantics: Literal["policy"] = "policy"
    denied_tools: list[str] = Field(default_factory=list)
    matcher: str | None = None
    priority: int = 0
    block_on_failure: Literal[True] = True


class CommandHookDefinition(HookPolicy):
    """A hook that executes a shell command."""

    type: Literal["command"] = "command"
    command: str
    env_allowlist: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    matcher: str | None = None
    block_on_failure: bool = False
    priority: int = Field(default=0)
    """Higher priority runs first within an event; ties keep registration order."""


class PromptHookDefinition(HookPolicy):
    """A hook that asks the model to validate a condition."""

    type: Literal["prompt"] = "prompt"
    prompt: str
    model: str | None = None
    context_window_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    matcher: str | None = None
    block_on_failure: bool = True
    priority: int = Field(default=0)
    """Higher priority runs first within an event; ties keep registration order."""


class HttpHookDefinition(HookPolicy):
    """A hook that POSTs the event payload to an HTTP endpoint."""

    type: Literal["http"] = "http"
    url: str
    trusted_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_trust(self) -> HttpHookDefinition:
        from urllib.parse import urlparse
        from researchx.security.network_guard import validate_http_url

        for origin in self.trusted_origins:
            validate_http_url(origin)
            parsed = urlparse(origin)
            if parsed.path or parsed.params or parsed.query or parsed.fragment or "*" in origin:
                raise ValueError("Trusted origins must be exact scheme://host[:port] values")
        return self

    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    matcher: str | None = None
    block_on_failure: bool = False
    priority: int = Field(default=0)
    """Higher priority runs first within an event; ties keep registration order."""


class AgentHookDefinition(HookPolicy):
    """A hook that performs a deeper model-based validation."""

    type: Literal["agent"] = "agent"
    prompt: str
    model: str | None = None
    context_window_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: int = Field(default=60, ge=1, le=1200)
    matcher: str | None = None
    block_on_failure: bool = True
    priority: int = Field(default=0)
    """Higher priority runs first within an event; ties keep registration order."""


HookDefinition = (
    CommandHookDefinition
    | PromptHookDefinition
    | HttpHookDefinition
    | AgentHookDefinition
    | PolicyHookDefinition
)
