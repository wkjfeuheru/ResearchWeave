"""Hook execution engine."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from researchx.api.usage import UsageSnapshot
from researchx.config import Settings
from researchx.permissions.checker import PermissionChecker
from researchx.permissions.capabilities import CapabilityContext
from researchx.sandbox.policy import ExecutionOwner
from typing import Awaitable
from researchx.hooks.safety import redact, post_hook, SAFE_ENV
from researchx.services.execution.shell import terminate_shell_process


from researchx.api.client import (
    ApiTextDeltaEvent,
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    SupportsStreamingMessages,
)
from researchx.engine.messages import ConversationMessage
from researchx.hooks.events import HookEvent
from researchx.hooks.loader import HookRegistry
from researchx.hooks.schemas import (
    AgentHookDefinition,
    CommandHookDefinition,
    HookDefinition,
    HttpHookDefinition,
    PromptHookDefinition,
    PolicyHookDefinition,
)
from researchx.hooks.types import AggregatedHookResult, HookResult
from researchx.sandbox import SandboxUnavailableError
from researchx.services.execution.shell import create_shell_subprocess


@dataclass
class HookExecutionContext:
    """Context passed into hook execution."""

    cwd: Path
    api_client: SupportsStreamingMessages
    default_model: str
    context_window_tokens: int | None = None
    settings: Settings | None = None
    owner: ExecutionOwner | None = None
    permission_checker: PermissionChecker | None = None
    capabilities: CapabilityContext = CapabilityContext()
    permission_prompt: Callable[[str, str], Awaitable[bool]] | None = None
    account_usage: Callable[[UsageSnapshot], None] | None = None


class HookExecutor:
    """Execute hooks for lifecycle events."""

    def __init__(self, registry: HookRegistry, context: HookExecutionContext) -> None:
        self._registry = registry
        self._context = context

    def scoped(
        self,
        *,
        cwd: Path,
        settings: Settings | None,
        checker: PermissionChecker,
        capabilities: CapabilityContext,
        prompt: Callable[[str, str], Awaitable[bool]] | None,
        owner: ExecutionOwner | None = None,
    ) -> HookExecutor:
        return HookExecutor(
            self._registry,
            replace(
                self._context,
                cwd=cwd,
                settings=settings or self._context.settings,
                permission_checker=checker,
                capabilities=capabilities,
                permission_prompt=prompt,
                owner=owner,
            ),
        )

    def has_effects(self, event: HookEvent, tool_name: str) -> bool:
        return any(
            not isinstance(hook, PolicyHookDefinition)
            and _matches_hook(hook, {"tool_name": tool_name})
            for hook in self._registry.get(event)
        )

    def update_registry(self, registry: HookRegistry) -> None:
        """Replace the active hook registry."""
        self._registry = registry

    def with_api_client(
        self,
        api_client: SupportsStreamingMessages,
        default_model: str,
        *,
        context_window_tokens: int | None = None,
    ) -> HookExecutor:
        """Reuse hook policy with a separate model transport and execution context."""
        return HookExecutor(
            self._registry,
            HookExecutionContext(
                cwd=self._context.cwd,
                settings=self._context.settings,
                account_usage=None
                if getattr(api_client, "accounts_usage", False)
                else self._context.account_usage,
                api_client=api_client,
                default_model=default_model,
                context_window_tokens=context_window_tokens
                if context_window_tokens is not None
                else (
                    self._context.context_window_tokens
                    if default_model == self._context.default_model
                    else None
                ),
            ),
        )

    def update_context(
        self,
        *,
        api_client: SupportsStreamingMessages | None = None,
        default_model: str | None = None,
    ) -> None:
        """Update the active hook execution context."""
        if api_client is not None:
            self._context.api_client = api_client
        if default_model is not None:
            self._context.default_model = default_model

    async def execute(self, event: HookEvent, payload: dict[str, Any]) -> AggregatedHookResult:
        """Execute all matching hooks for an event."""
        results: list[HookResult] = []
        for hook in self._registry.get(event):
            if not _matches_hook(hook, payload):
                continue
            clean_payload = redact(payload)
            try:
                if isinstance(hook, PolicyHookDefinition):
                    denied = payload.get("tool_name") in hook.denied_tools
                    result = HookResult(
                        hook_type="policy",
                        success=not denied,
                        blocked=denied,
                        reason="Tool denied by deterministic policy hook" if denied else "",
                    )
                else:
                    capability = (
                        "shell.execute"
                        if isinstance(hook, CommandHookDefinition)
                        else (
                            "network.http" if isinstance(hook, HttpHookDefinition) else "model.call"
                        )
                    )
                    if not self._context.capabilities.permits(frozenset({capability})):
                        raise PermissionError("Hook capability not granted")
                    checker = self._context.permission_checker
                    if checker is not None:
                        decision = checker.evaluate(
                            "hook:" + hook.type,
                            is_read_only=isinstance(
                                hook, (PromptHookDefinition, AgentHookDefinition)
                            ),
                            command=_inject_arguments(
                                hook.command, clean_payload, shell_escape=True
                            )
                            if isinstance(hook, CommandHookDefinition)
                            else None,
                        )
                        if not decision.allowed:
                            prompt = self._context.permission_prompt
                            if not (
                                decision.requires_confirmation
                                and prompt
                                and await prompt("hook:" + hook.type, decision.reason)
                            ):
                                raise PermissionError("Hook permission denied")
                    if len(json.dumps(clean_payload).encode()) > hook.max_payload_bytes:
                        raise ValueError("Hook payload exceeds limit")
                    if isinstance(hook, CommandHookDefinition):
                        pending = self._run_command_hook(hook, event, clean_payload)
                    elif isinstance(hook, HttpHookDefinition):
                        pending = self._run_http_hook(hook, event, clean_payload)
                    else:
                        pending = self._run_prompt_like_hook(
                            hook,
                            event,
                            clean_payload,
                            agent_mode=isinstance(hook, AgentHookDefinition),
                        )
                    result = await asyncio.wait_for(pending, hook.timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = HookResult(
                    hook_type=hook.type,
                    success=False,
                    blocked=hook.block_on_failure,
                    reason=f"Hook failed: {type(exc).__name__}",
                )
            results.append(result)
            if result.blocked:
                break
        return AggregatedHookResult(results=results)

    async def _run_command_hook(
        self,
        hook: CommandHookDefinition,
        event: HookEvent,
        payload: dict[str, Any],
    ) -> HookResult:
        command = _inject_arguments(hook.command, payload, shell_escape=True)
        try:
            process = await create_shell_subprocess(
                command,
                login=False,
                cwd=self._context.cwd,
                settings=self._context.settings,
                owner=self._context.owner,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if key in SAFE_ENV and (not hook.env_allowlist or key in hook.env_allowlist)
                    },
                    "RESEARCHX_HOOK_EVENT": event.value,
                    "RESEARCHX_HOOK_PAYLOAD": json.dumps(payload),
                },
            )
        except SandboxUnavailableError as exc:
            return HookResult(
                hook_type=hook.type,
                success=False,
                blocked=hook.block_on_failure,
                reason=str(exc),
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                _bounded_communicate(process, hook.max_output_bytes),
                timeout=hook.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await asyncio.gather(
                terminate_shell_process(process, force=True), process.communicate()
            )
            return HookResult(
                hook_type=hook.type,
                success=False,
                blocked=hook.block_on_failure,
                reason=f"command hook timed out after {hook.timeout_seconds}s",
            )

        except BaseException:
            await asyncio.gather(
                terminate_shell_process(process, force=True), process.communicate()
            )
            raise

        output = "\n".join(
            part
            for part in (
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
            )
            if part
        )
        output = str(redact(output))
        if len(output.encode()) > hook.max_output_bytes:
            raise ValueError("Command hook output exceeds limit")
        success = process.returncode == 0
        return HookResult(
            hook_type=hook.type,
            success=success,
            output=output,
            blocked=hook.block_on_failure and not success,
            reason=output or f"command hook failed with exit code {process.returncode}",
            metadata={"returncode": process.returncode},
        )

    async def _run_http_hook(
        self,
        hook: HttpHookDefinition,
        event: HookEvent,
        payload: dict[str, Any],
    ) -> HookResult:
        status, output = await post_hook(hook, event.value, payload)
        success = 200 <= status < 300
        return HookResult(
            hook_type=hook.type,
            success=success,
            output=output,
            blocked=hook.block_on_failure and not success,
            reason="" if success else f"HTTP hook returned {status}",
            metadata={"status_code": status},
        )

    async def _run_prompt_like_hook(
        self,
        hook: PromptHookDefinition | AgentHookDefinition,
        event: HookEvent,
        payload: dict[str, Any],
        *,
        agent_mode: bool,
    ) -> HookResult:
        prompt = _inject_arguments(hook.prompt, payload)
        prefix = (
            "你正在判断 ResearchX 中某个 Hook 条件是否通过。"
            '只返回严格 JSON：{"ok": true} 或 {"ok": false, "reason": "..."}。'
        )
        if agent_mode:
            prefix += " 请更仔细地检查载荷后再作决定。"
        request = ApiMessageRequest(
            model=hook.model or self._context.default_model,
            messages=[ConversationMessage.from_user_text(prompt)],
            system_prompt=prefix,
            max_tokens=512,
            context_window_tokens=(
                hook.context_window_tokens
                if hook.context_window_tokens is not None
                else (
                    self._context.context_window_tokens
                    if not hook.model or hook.model == self._context.default_model
                    else None
                )
            ),
        )

        from researchx.services.context.budget import checked_request

        request = checked_request(self._context.api_client, request)
        text_chunks: list[str] = []
        final_event: ApiMessageCompleteEvent | None = None
        async for event_item in self._context.api_client.stream_message(request):
            if isinstance(event_item, ApiMessageCompleteEvent):
                final_event = event_item
                if self._context.account_usage:
                    self._context.account_usage(event_item.usage)
            elif isinstance(event_item, ApiTextDeltaEvent):
                text_chunks.append(event_item.text)

        text = "".join(text_chunks)
        if final_event is not None and final_event.message.text:
            text = final_event.message.text

        if len(text.encode()) > hook.max_output_bytes:
            raise ValueError("Model hook output exceeds limit")
        text = str(redact(text))
        parsed = _parse_hook_json(text)
        if parsed["ok"]:
            return HookResult(hook_type=hook.type, success=True, output=text)
        return HookResult(
            hook_type=hook.type,
            success=False,
            output=text,
            blocked=hook.block_on_failure,
            reason=parsed.get("reason", "hook rejected the event"),
        )


def _matches_hook(hook: HookDefinition, payload: dict[str, Any]) -> bool:
    matcher = getattr(hook, "matcher", None)
    if not matcher:
        return True
    subject = str(payload.get("tool_name") or payload.get("prompt") or payload.get("event") or "")
    return fnmatch.fnmatch(subject, matcher)


def _inject_arguments(template: str, payload: dict[str, Any], *, shell_escape: bool = False) -> str:
    serialized = json.dumps(payload, ensure_ascii=True)
    if shell_escape:
        serialized = shlex.quote(serialized)
    return template.replace("$ARGUMENTS", serialized)


def _parse_hook_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("ok"), bool):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"ok": False, "reason": text.strip() or "hook returned invalid JSON"}


async def _bounded_communicate(
    process: asyncio.subprocess.Process, limit: int
) -> tuple[bytes, bytes]:
    total = 0

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal total
        data = bytearray()
        if stream is None:
            return bytes(data)
        while chunk := await stream.read(8192):
            total += len(chunk)
            if total > limit:
                raise ValueError("Command hook output exceeds limit")
            data.extend(chunk)
        return bytes(data)

    tasks = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
    try:
        stdout, stderr = await asyncio.gather(*tasks)
        await process.wait()
        return stdout, stderr
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
