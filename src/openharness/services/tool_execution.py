"""Single tool execution boundary used by the existing handwritten loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4
from dataclasses import replace

from openharness.engine.messages import ToolResultBlock
from openharness.permissions.modes import PermissionMode
from openharness.hooks import HookEvent
from openharness.hooks.safety import redact
from openharness.tools.base import ToolExecutionContext, ToolResult
from openharness.tools.contracts import resolve_contract
from openharness.research.source_specs import SourceSpec
from openharness.services.operations import OperationStore
from openharness.utils.fs import atomic_write_text
from openharness.config.paths import get_data_dir

if TYPE_CHECKING:
    from openharness.engine.query import QueryContext

log = logging.getLogger(__name__)


async def _execute_impl(
    service: ToolExecutionService,
    context: QueryContext,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
) -> ToolResultBlock:
    from openharness.engine.query import (
        _resolve_permission_file_path,
        _extract_permission_command,
        _offload_tool_output_if_needed,
    )

    store = (context.tool_metadata or {}).get("research_store")
    runtime = (context.tool_metadata or {}).get("research_runtime")
    project_mode = store is not None and store.load().project is not None
    if project_mode:
        assert store is not None
        if runtime is None:
            from openharness.research.runtime import ResearchAgentRuntime

            runtime = ResearchAgentRuntime(
                store,
                permission_checker=context.permission_checker,
                workspace_root=(context.tool_metadata or {}).get("research_workspace_root"),
                memory_auto_inject_max_chars=(context.tool_metadata or {}).get(
                    "memory_auto_inject_max_chars", 12000
                ),
            )
    log.debug("tool_call start: %s id=%s", tool_name, tool_use_id)

    tool = context.tool_registry.get(tool_name)
    if tool is None:
        log.warning("unknown tool: %s", tool_name)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"Unknown tool: {tool_name}",
            is_error=True,
            result_metadata={"status": "failed", "error_code": "unknown_tool"},
        )

    try:
        parsed_input = tool.input_model.model_validate(tool_input)
    except Exception as exc:
        log.warning("invalid input for %s: %s", tool_name, exc)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=tool.validation_error_message(exc),
            is_error=True,
            result_metadata={"status": "failed", "error_code": "invalid_arguments"},
        )

    service.contract = resolve_contract(tool, parsed_input)
    if not context.capabilities.permits(service.contract.required_capabilities):
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            is_error=True,
            content="Required tool capabilities are not granted.",
            result_metadata={"status": "denied", "error_code": "capability_denied"},
        )

    # Normalize common tool inputs before permission checks so path rules apply
    # consistently across built-in tools that use `file_path`, `path`, or
    # directory-scoped roots such as `glob`/`grep`.
    tool_cwd = context.cwd
    if project_mode:
        assert runtime is not None and store is not None
        tool_cwd = runtime._workspace_path(runtime.repository._project(store.load()))
    _file_path = _resolve_permission_file_path(tool_cwd, tool_input, parsed_input)
    if _file_path and (project_mode or (context.tool_metadata or {}).get("subagent_child")):
        boundary = ToolExecutionContext(
            cwd=tool_cwd,
            metadata={
                **(context.tool_metadata or {}),
                **({"research_runtime": runtime} if runtime else {}),
            },
        )
        try:
            raw_path = next(
                (
                    getattr(parsed_input, key)
                    for key in ("file_path", "path", "root", "image_path")
                    if isinstance(getattr(parsed_input, key, None), str)
                    and getattr(parsed_input, key)
                ),
                _file_path,
            )
            boundary.resolve_path(raw_path, write=not tool.is_read_only(parsed_input))
        except (ValueError, OSError, RuntimeError) as exc:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                is_error=True,
                content=str(exc),
                result_metadata={"status": "denied", "error_code": "workspace_boundary"},
            )
    _command = _extract_permission_command(tool_input, parsed_input)
    log.debug(
        "permission check: %s read_only=%s path=%s cmd=%s",
        tool_name,
        tool.is_read_only(parsed_input),
        _file_path,
        _command and _command[:80],
    )
    decision = context.permission_checker.evaluate(
        tool_name,
        is_read_only=(
            tool.is_read_only(parsed_input)
            and service.contract.effect not in {"external_write", "unknown", "mixed"}
            and not (
                context.permission_checker.mode == PermissionMode.PLAN
                and service.contract.effect == "local_write"
            )
        ),
        file_path=_file_path,
        command=_command,
    )
    if not decision.allowed:
        if decision.requires_confirmation and context.permission_prompt is not None:
            log.debug("permission prompt for %s: %s", tool_name, decision.reason)
            if context.hook_executor is not None:
                await context.hook_executor.execute(
                    HookEvent.NOTIFICATION,
                    {
                        "event": HookEvent.NOTIFICATION.value,
                        "notification_type": "permission_prompt",
                        "tool_name": tool_name,
                        "reason": decision.reason,
                    },
                )
            confirmed = await context.permission_prompt(tool_name, decision.reason)
            if not confirmed:
                log.debug("permission denied by user for %s", tool_name)
                return ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=decision.reason or f"Permission denied for {tool_name}",
                    is_error=True,
                    result_metadata={
                        "outcome": "denied",
                        "status": "denied",
                        "error_code": "permission_denied",
                    },
                )
        else:
            log.debug("permission blocked for %s: %s", tool_name, decision.reason)
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=decision.reason or f"Permission denied for {tool_name}",
                is_error=True,
                result_metadata={
                    "outcome": "denied",
                    "status": "denied",
                    "error_code": "permission_denied",
                },
            )

    if context.hook_executor is not None:
        settings = (context.tool_metadata or {}).get("trusted_settings")
        owner = None
        if project_mode:
            from openharness.config import Settings
            from openharness.sandbox.policy import ExecutionOwner, report_settings

            assert runtime is not None and store is not None
            project = runtime.repository._project(store.load())
            settings = report_settings(settings or Settings(), tool_cwd)
            owner = ExecutionOwner(
                runtime_id=(context.tool_metadata or {}).get("runtime_id")
                or context.execution_session_id,
                project_id=project.id,
                execution_id=tool_use_id,
                workspace=tool_cwd,
            )
        context = replace(
            context,
            hook_executor=context.hook_executor.scoped(
                cwd=tool_cwd,
                settings=settings,
                checker=context.permission_checker,
                capabilities=context.capabilities,
                prompt=context.permission_prompt,
                owner=owner,
            ),
        )
    replay = await service.begin(context, tool, parsed_input, tool_use_id, tool_cwd)
    if replay is not None:
        return replay
    if project_mode:
        assert runtime is not None
        defect = runtime.guard_tool(tool_name, tool_input)
        if defect:
            return ToolResultBlock(tool_use_id=tool_use_id, content=defect, is_error=True)
    if (
        store is not None
        and not project_mode
        and store.load().research_state.replan_required
        and tool_name
        not in {"research_memory", "ask_user_question", "skill", "investigate_conflict"}
    ):
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content="Research requires a new plan. Use research_memory to update context and create_plan before executing more tools.",
            is_error=True,
        )
    if (
        store is not None
        and not project_mode
        and tool_name
        not in {"research_memory", "ask_user_question", "skill", "investigate_conflict"}
    ):
        memory = store.load()
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        if plan and not memory.research_state.current_task_id:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                is_error=True,
                content="Start the current research task before collecting or calculating: use research_memory.update_task "
                "with the actual task ID and status in_progress, then execute its work and update its result. "
                "Do not collect the whole plan while leaving every task pending.",
            )
    if context.hook_executor is not None:
        service.started_hooks = context.hook_executor.has_effects(HookEvent.PRE_TOOL_USE, tool_name)
        pre_hooks = await context.hook_executor.execute(
            HookEvent.PRE_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "event": HookEvent.PRE_TOOL_USE.value,
            },
        )
        if pre_hooks.blocked:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=pre_hooks.reason or f"pre_tool_use hook blocked {tool_name}",
                is_error=True,
                result_metadata={
                    "status": "partial" if service.started_hooks else "denied",
                    "error_code": "pre_hook_blocked",
                },
            )

    log.debug("executing %s ...", tool_name)
    t0 = time.monotonic()
    execution = None
    baseline = None
    if project_mode:
        assert runtime is not None and store is not None
        from openharness.research.runtime import CONTROL_TOOLS, PLANNING_TOOLS
        from openharness.research.errors import ResearchError

        try:
            runtime.resolve_workspace(runtime.repository._project(store.load()).id)
            if tool_name in PLANNING_TOOLS:
                baseline = runtime.repository.reserve_planning(tool_name)
            elif tool_name not in CONTROL_TOOLS and store.load().research_state.current_task_id:
                execution = runtime.repository.begin_execution(tool_name, tool_use_id)
        except ResearchError as exc:
            return ToolResultBlock(tool_use_id=tool_use_id, content=str(exc), is_error=True)
    try:
        result = await service.invoke(
            tool,
            parsed_input,
            ToolExecutionContext(
                cwd=tool_cwd,
                capabilities=context.capabilities.restrict(service.contract.required_capabilities),
                settings=(context.tool_metadata or {}).get("trusted_settings"),
                runtime_id=(context.tool_metadata or {}).get("runtime_id"),
                metadata={
                    "tool_registry": context.tool_registry,
                    "ask_user_prompt": context.ask_user_prompt,
                    **(context.tool_metadata or {}),
                    "query_context": context,
                    **({"research_runtime": runtime} if runtime else {}),
                    **({"planning_baseline": baseline} if baseline else {}),
                    **({"research_execution": execution} if execution else {}),
                },
                hook_executor=context.hook_executor,
            ),
        )
    except BaseException:
        if execution:
            assert runtime is not None
            runtime.repository.cancel_execution(execution["id"])
        if baseline:
            assert runtime is not None
            runtime.repository.suspend("Planning execution interrupted")
        raise
    if baseline:
        assert runtime is not None
        result = await runtime.commit_planning_result(tool_name, result, baseline)
    elapsed = time.monotonic() - t0
    if store is not None and (
        execution
        or tool_name
        not in {
            "research_memory",
            "investigate_conflict",
            "planner",
            "replanner",
            "research_project",
        }
    ):
        source_specs: list[SourceSpec] | None = result.metadata.get("research_source_specs")
        if tool_name == "investigate_conflict":
            source_specs = []  # The investigator imports its own validated evidence with provenance.
        if source_specs is None:
            source_specs = [
                {
                    "content": result.output,
                    "kind": "calculation"
                    if tool_name == "bash"
                    else "user"
                    if tool_name == "ask_user_question"
                    else "tool",
                    "title": tool_name,
                    "locator": f"tool:{tool_name}:{tool_use_id}",
                    "fragment": True,
                }
            ]
        sources: list[str] = []
        if execution:
            assert runtime is not None
            from openharness.research.errors import ResearchError

            rejection = "Late tool result discarded: execution/plan/task revision was revoked"
            try:
                receipt = runtime.repository.commit_execution(
                    execution["id"], source_specs, is_error=result.is_error
                )
            except (ResearchError, ValueError, KeyError, TypeError) as exc:
                rejection = (
                    f"Tool result rejected: invalid provenance or schema ({type(exc).__name__})"
                )
                runtime.repository.cancel_execution(execution["id"], f"Invalid tool result: {exc}")
                receipt = {
                    "accepted": False,
                    "execution_id": execution["id"],
                    "research_sources": [],
                    "task_revision": execution["task_revision"],
                    "plan_revision": execution["plan_revision"],
                }
            sources = cast(list[str], receipt["research_sources"])

            result = replace(result, metadata={**result.metadata, **receipt})
            if not receipt["accepted"]:
                result = replace(result, output=rejection, is_error=True)
        else:
            for index, spec in enumerate(source_specs):
                sources.append(
                    store.capture(
                        origin_id=tool_use_id,
                        index=index,
                        content=spec["content"],
                        kind=spec.get("kind", "tool"),
                        title=spec.get("title", tool_name),
                        locator=spec.get("locator", f"tool:{tool_name}:{tool_use_id}"),
                        fragment=spec.get("fragment", False),
                        published_at=spec.get("published_at"),
                        is_error=result.is_error,
                    ).id
                )

        result = replace(
            result,
            metadata={
                **{
                    key: value
                    for key, value in result.metadata.items()
                    if key != "research_source_specs"
                },
                "research_sources": sources,
            },
        )
    result = service.bound_output(result)
    log.debug(
        "executed %s in %.2fs err=%s output_len=%d",
        tool_name,
        elapsed,
        result.is_error,
        len(result.output or ""),
    )
    inline_output, artifact_path = _offload_tool_output_if_needed(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        output=result.output,
    )
    tool_result = ToolResultBlock(
        tool_use_id=tool_use_id,
        content=inline_output,
        is_error=result.is_error,
        result_metadata={
            key: value
            for key, value in (result.metadata or {}).items()
            if key != "research_source_specs"
        },
    )
    if artifact_path is not None:
        tool_result.result_metadata["tool_output_artifact"] = str(artifact_path)
    if store is not None and result.metadata.get("research_sources"):
        tool_result.content += "\n\nresearch_sources: " + json.dumps(
            result.metadata["research_sources"]
        )
        tool_result.content += f"\nresearch_revision: {store.load().revision}"
    if tool_name == "skill" and not tool_result.is_error:
        skill_name = str(tool_input.get("name", "")).strip()
        if skill_name and context.tool_metadata is not None:
            skills = context.tool_metadata.setdefault("invoked_skills", [])
            if skill_name not in skills:
                skills.append(skill_name)
    service.finish(tool_result)
    if context.hook_executor is not None:
        post = await context.hook_executor.execute(
            HookEvent.POST_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": tool_result.content,
                "tool_is_error": tool_result.is_error,
                "event": HookEvent.POST_TOOL_USE.value,
            },
        )
        failures = [item.hook_type for item in post.results if not item.success]
        if failures:
            tool_result.result_metadata["post_hook_failures"] = failures
    return tool_result


class ToolExecutionService:
    """One invocation adapter; state survives instances through the SQLite ledger."""

    def __init__(self) -> None:
        self.owner = uuid4().hex
        self.ledger: Any = None
        self.operation: Any = None
        self.contract: Any = None
        self.settled = False
        self.started_hooks = False
        self.started_body = False
        self.claimed = False

    async def execute(
        self, context: QueryContext, name: str, call: str, arguments: dict[str, object]
    ) -> ToolResultBlock:
        try:
            result = await _execute_impl(self, context, name, call, arguments)
            if self.claimed and not self.settled:
                self.finish(result, blocked=not self.started_body and not self.started_hooks)
            result.result_metadata.setdefault("status", "failed" if result.is_error else "success")
            if result.is_error:
                result.result_metadata.setdefault("error_code", "admission_rejected")
            return result
        except asyncio.CancelledError:
            if self.claimed and not self.settled:
                status = (
                    "uncertain"
                    if (
                        self.started_hooks
                        or (self.started_body and self.contract.effect != "read_only")
                    )
                    else "cancelled"
                )
                self.ledger.settle(
                    self.operation["operation_id"],
                    status,
                    owner=self.owner,
                    error_code="cancelled_after_start" if self.started_body else "cancelled",
                )
            raise
        except Exception as exc:
            status = (
                "uncertain"
                if (
                    self.started_hooks
                    or (self.started_body and self.contract.effect != "read_only")
                )
                else "failed"
            )
            if self.settled:
                # A post-processing exception cannot erase an already durable success.
                result = self.load_result(call)
                result.result_metadata["post_hook_failures"] = [type(exc).__name__]
                return result
            result = ToolResultBlock(
                tool_use_id=call,
                content=f"Tool execution {status}: {type(exc).__name__}: {redact(str(exc))}",
                is_error=True,
                result_metadata={
                    "status": status,
                    "error_code": "timeout"
                    if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                    else "execution_error",
                },
            )
            if self.claimed:
                self.finish(result, blocked=not self.started_body and not self.started_hooks)
            return result

    async def begin(
        self, context: QueryContext, tool: Any, arguments: Any, call: str, cwd: Path
    ) -> ToolResultBlock | None:
        # Retain the exact contract already admitted before any approval await.
        assert self.contract is not None
        metadata = context.tool_metadata or {}
        store = metadata.get("research_store")
        session = metadata.get("session_id") or (
            store.session_id if store else context.execution_session_id
        )
        scope = str(Path(cwd).resolve())
        root = store.directory if store else get_data_dir() / "executions"
        self.ledger = OperationStore(root / "operations.sqlite3")

        # Unknown resources serialize. Explicit file resources participate in read/write conflicts.
        def resources(items: tuple[str, ...]) -> list[str]:
            return [
                str((Path(cwd) / getattr(arguments, "path")).resolve())
                if item == "path" and hasattr(arguments, "path")
                else item
                for item in items
            ]

        declared = {
            "read": resources(self.contract.resources_read),
            "write": resources(self.contract.resources_write),
        }
        if self.contract.parallelism == "serial":
            declared = {"read": [], "write": ["*"]}
        # Orchestration must not hold its children's resource lock while awaiting them.
        if metadata.get("subagent_child"):
            scope += ":child:" + str(metadata.get("subagent_output_dir", ""))
        self.ledger.recover(session=session, scope=scope)
        digest = hashlib.sha256(
            json.dumps(arguments.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        run = hashlib.sha256(
            f"{session}:{context.current_user_message_id or context.execution_session_id}".encode()
        ).hexdigest()
        self.operation = self.ledger.prepare(
            session=session,
            scope=scope,
            run=run,
            call=call,
            tool=tool.name,
            version=self.contract.version,
            digest=digest,
            effect="mixed"
            if context.hook_executor
            and context.hook_executor.has_effects(HookEvent.PRE_TOOL_USE, tool.name)
            else self.contract.effect,
            resources=declared,
        )
        operation_id = self.operation["operation_id"]
        while True:
            current = self.ledger.get(operation_id, session=session, scope=scope)
            if current["status"] == "succeeded":
                self.operation = current
                self.settled = True
                return self.load_result(call)
            if current["status"] not in {"prepared", "running"}:
                self.settled = True
                return ToolResultBlock(
                    tool_use_id=call,
                    is_error=True,
                    content=f"Operation {operation_id} is {current['status']}; reconciliation or explicit recovery is required. Do not repeat the side effect.",
                    result_metadata={
                        "status": current["status"],
                        "error_code": "recovery_required",
                        "operation_id": operation_id,
                    },
                )
            conflicts = self.ledger.unresolved_conflicts(operation_id)
            if conflicts:
                if current["status"] == "prepared":
                    self.ledger.settle(
                        operation_id, "blocked", error_code="reconciliation_required"
                    )
                self.settled = True
                return ToolResultBlock(
                    tool_use_id=call,
                    is_error=True,
                    content="Unresolved side effects block this resource: " + ", ".join(conflicts),
                    result_metadata={"status": "blocked", "error_code": "reconciliation_required"},
                )
            attempt = self.ledger.claim(operation_id, self.owner)
            if attempt:
                self.claimed = True
                self.operation = self.ledger.get(operation_id, session=session, scope=scope)
                return None
            await asyncio.sleep(0.02)

    async def invoke(self, tool: Any, arguments: Any, context: ToolExecutionContext) -> Any:
        self.started_body = True
        # Stable across all attempts, never exposed as a model-controlled argument.
        context.operation_id = self.operation["operation_id"]
        context.idempotency_key = self.operation["idempotency_key"]
        remaining = self.contract.max_attempts - self.operation["attempts"] + 1
        for attempt in range(1, remaining + 1):
            result = await asyncio.wait_for(
                tool.execute(arguments, context), self.contract.timeout_seconds
            )
            if not result.is_error or attempt >= remaining or self.contract.retry_mode == "never":
                break
            no_effect = (
                self.contract.effect == "read_only" or result.metadata.get("no_effect") is True
            )
            if self.contract.retry_mode == "reconcile_before_retry":
                no_effect = await asyncio.wait_for(
                    tool.reconcile_no_effect(arguments, context), self.contract.timeout_seconds
                )
            if not no_effect:
                break
            self.ledger.next_attempt(context.operation_id, owner=self.owner)
            await asyncio.sleep(min(0.1 * 2 ** (attempt - 1), 1.0))
        if self.contract.output_model is not None and not result.is_error:
            self.contract.output_model.model_validate_json(result.output)
        status = result.status or ("failed" if result.is_error else "success")
        code = result.error_code or result.metadata.get("error_code")
        if (
            result.is_error
            and self.contract.effect in {"external_write", "unknown", "mixed"}
            and not result.metadata.get("no_effect")
        ):
            status = "uncertain"
        metadata = {
            **result.metadata,
            "status": status,
            "error_code": code,
            "operation_id": context.operation_id,
            "contract_version": self.contract.version,
        }
        return replace(result, is_error=status != "success", metadata=metadata)

    def bound_output(self, result: ToolResult) -> ToolResult:
        """Bound model-visible output only after ResearchStore captured the full source."""
        if len(result.output) <= self.contract.max_output_chars:
            return result
        path = (
            self.ledger.path.parent
            / "operation_artifacts"
            / f"{self.operation['operation_id']}.txt"
        )
        atomic_write_text(path, result.output, mode=0o600)
        return replace(
            result,
            output=result.output[: self.contract.max_output_chars] + f"\n[Full output: {path}]",
            metadata={**result.metadata, "tool_output_artifact": str(path)},
        )

    def finish(self, result: ToolResultBlock, *, blocked: bool = False) -> None:
        if not self.operation or self.settled:
            return
        operation_id = self.operation["operation_id"]
        status = (
            "blocked"
            if blocked
            else result.result_metadata.get("status", "failed" if result.is_error else "success")
        )
        state = "succeeded" if status == "success" else status
        if state == "denied":
            state = "blocked"
        result.result_metadata.update(operation_id=operation_id, status=status)
        path = self.ledger.path.parent / "operation_artifacts" / f"{operation_id}.json"
        atomic_write_text(path, result.model_dump_json(), mode=0o600)
        self.ledger.settle(
            operation_id,
            state,
            owner=self.owner,
            result_ref=str(path),
            error_code=result.result_metadata.get("error_code"),
            external_request_id=result.result_metadata.get("external_request_id"),
        )
        self.operation["result_ref"] = str(path)
        self.settled = True

    def load_result(self, call: str) -> ToolResultBlock:
        result = _receipt_result(self.operation, call)
        result.tool_use_id = call
        result.result_metadata["replayed_receipt"] = True
        return result


def recover_messages(messages: list[Any], metadata: Any, cwd: Path, session_id: str) -> list[Any]:
    """Attach durable outcomes to unmatched calls without replaying a write."""
    from openharness.engine.messages import ConversationMessage

    store = metadata.get("research_store")
    runtime = metadata.get("research_runtime")
    session = metadata.get("session_id") or (store.session_id if store else session_id)
    root = store.directory if store else get_data_dir() / "executions"
    if store and runtime and store.load().project:
        cwd = runtime._workspace_path(runtime.repository._project(store.load()))
    scope = str(cwd.resolve())
    path = root / "operations.sqlite3"
    if not path.exists():
        return messages
    ledger = OperationStore(path)
    ledger.recover(session=session, scope=scope)
    if not messages or not messages[-1].tool_uses:
        return messages
    results = []
    found_receipt = False
    for call in messages[-1].tool_uses:
        identity = hashlib.sha256(json.dumps([session, scope, call.id]).encode()).hexdigest()
        try:
            receipt = ledger.get(identity, session=session, scope=scope)
        except ValueError:
            results.append(
                ToolResultBlock(
                    tool_use_id=call.id,
                    is_error=True,
                    content="No durable receipt exists. Inspect the operation before explicitly resubmitting.",
                )
            )
            continue
        found_receipt = True
        if receipt["status"] == "succeeded" and receipt["result_ref"]:
            result = _receipt_result(receipt, call.id)
            result.result_metadata["replayed_receipt"] = True
        else:
            result = ToolResultBlock(
                tool_use_id=call.id,
                is_error=True,
                content=f"Execution interrupted. Recovered operation {identity}: {receipt['status']}. Reconciliation is required before repeating side effects.",
                result_metadata={
                    "operation_id": identity,
                    "status": receipt["status"],
                    "error_code": "recovery_required",
                },
            )
        if store is not None:
            sources = [
                source for source in store.load().sources.values() if source.origin_id == call.id
            ]
            if sources:
                source_ids = [source.id for source in sources]
                result.content += (
                    f"\nRetained research sources: {source_ids}; read them before continuing."
                )
                result.result_metadata["research_sources"] = source_ids
                if receipt["effect"] == "read_only" and receipt["status"] != "succeeded":
                    result.is_error = all(source.is_error for source in sources)
        results.append(result)
    return (
        [*messages, ConversationMessage(role="user", content=[item for item in results])]
        if found_receipt
        else messages
    )


def _receipt_result(receipt: dict[str, Any], call: str) -> ToolResultBlock:
    try:
        return ToolResultBlock.model_validate_json(
            Path(receipt["result_ref"]).read_text()
        ).model_copy(update={"tool_use_id": call})
    except (OSError, ValueError, TypeError):
        return ToolResultBlock(
            tool_use_id=call,
            is_error=True,
            content="Operation already succeeded but its saved result is unavailable. Restore the artifact; do not repeat the side effect.",
            result_metadata={
                "status": "blocked",
                "operation_status": "succeeded",
                "error_code": "artifact_unavailable",
                "operation_id": receipt["operation_id"],
            },
        )
