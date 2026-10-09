# Harness execution contracts and recovery

This change extends the existing hand-written Agent Loop and ToolRegistry. Planner/Replanner remain tools; ResearchStore, evidence capture, workspace memory, context budgeting, approval queues and session JSON retain their existing roles. No additional workflow engine or plugin registry is introduced.

## Contracts and compatibility

`tools/contracts.py::resolve_contract(tool, arguments)` returns a validated, frozen `ToolContract`. Registration validates declared contracts; execution resolves argument-dependent legacy read-only behavior.

| Field | Default / meaning |
| --- | --- |
| `name`, `version`, `description`, `source` | Registry identity; version `1`; source is descriptive, not an authorization grant |
| `input_schema` | Derived from the tool's Pydantic `input_model`; conflicting declarations rejected |
| `output_schema`, `output_model` | Optional descriptive schema; optional Pydantic model validates successful JSON output. Text tools need neither |
| `required_capabilities` | Empty for legacy tools; checked against a host-owned immutable capability context |
| `effect` | `unknown`; explicit read-only/local-write/external-write/model-call/mixed classifications |
| `resources_read`, `resources_write` | Resource scopes; `path` resolves the input path against the workspace |
| `parallelism` | `serial`. `resources` requires explicit resource declarations; read-only alone never implies safe parallelism |
| `retry_mode`, `max_attempts` | `never`, `1`; explicit retries capped at 3 total tool attempts |
| `idempotency_key_supported` | False; key retry requires an adapter override that actually transmits the stable host key to a supporting remote service |
| `timeout_seconds`, `cancellable` | 600 seconds; cancellation capability declaration. Cancellation always propagates; inability to undo a write becomes uncertain |
| `max_output_chars` | 30,000; larger results are stored as private artifacts and the model receives a preview/reference |
| `result_statuses` | Declared result states: success, failed, partial, uncertain, cancelled, denied and blocked; successful receipts use `succeeded` |
| `result_semantics`, `observability` | Conservative side-effect classification; input digest only in receipts |

Legacy tools remain executable. `is_read_only()` may imply read-only effect, but never idempotency or parallelism. Unknown, mixed and model-call contracts cannot enable business retries. Tool schemas sent to model APIs remain the original name/description/input-schema format; internal contracts are not transmitted.

Duplicate registration raises an error. `register(..., replace=True)` can explicitly replace a non-builtin registration, but cannot replace a builtin. Trusted test/evaluation adapters explicitly remove the original before installing a replacement. Normal plugin loading never uses this substitution path. Retired plugin tool names are filtered before registration.

## Execution boundary

`engine/query.py::_execute_tool_call_impl()` delegates to `services/tool_execution.py::ToolExecutionService`; source capture and state integration were moved with the implementation, not duplicated.

1. Resolve the tool, validate input and resolve the contract.
2. Check host capabilities and normalized path/command permissions; await any required approval.
3. Prepare a SQLite operation receipt and atomically claim conflicting resources. A successful receipt returns its saved result without invoking the tool or its hooks again.
4. Apply existing research-state admission checks. Run PRE hooks only after tool admission, with separate hook capability/permission checks.
5. Execute with timeout/cancellation; perform only contract-authorized, bounded retries after verified absence of effects.
6. Normalize status/error metadata, preserve ResearchStore sources and citations, and offload oversized output.
7. Persist the result artifact and settle the core operation **before** POST hooks.
8. Report POST-hook failures as diagnostics without changing core success or replaying the tool.

`ToolResult` retains `output`, `is_error` and `metadata`, with optional `status`/`error_code`, conservative `retryable=False` and `no_effect=None`. Existing `ToolResultBlock` and events carry normalized fields in `result_metadata`. `ToolExecutionContext.operation_id` and `.idempotency_key` are assigned by the host, not read from model arguments.

PRE hooks with possible effects make the receipt's effect conservative (`mixed`). A failing PRE effect hook can leave `partial` work even when the core tool never ran. Invalid arguments and rejected tool permission never run PRE hooks.

## Permissions and hooks

Permission precedence is deterministic: sensitive paths and explicit tool deny → path deny and command deny → permission mode → limited tool allow / confirmation. `allowed_tools` does not override hard denials or PLAN restrictions. Paths are resolved to absolute paths, including symlink resolution; existing ResearchAgentRuntime checks enforce project workspace boundaries. Shell confinement remains the existing sandbox's responsibility; required unavailable sandboxes fail closed.

PLAN also rejects an explicitly declared local write even if a legacy research-control tool reports `is_read_only()`. DEFAULT preserves automatic admission only for exact built-in research-memory/project bookkeeping tools with host capabilities. General local writes, subclasses and external/unknown/mixed effects do not receive that compatibility exception.

`CapabilityContext` is a frozen host field, inherited by child queries. `restrict()` rejects expansion. A tool receives only its declared capability set. Mutable tool metadata cannot grant capabilities.

HookRegistry, settings loading, plugin hooks, matching and priority remain in place:

- `policy`: deterministic denied-tool rules; no shell, HTTP or model execution. Currently runs at the same admitted PRE boundary, rather than needing an earlier side-effect-free pass.
- `effect`: existing command/HTTP/prompt/agent hooks; independently admitted at execution.
- `observer`: cannot set `block_on_failure`; failure is diagnostic.
- Command hooks use a fixed safe environment allowlist (optionally narrowed by configuration), no login profile, bounded combined stdout/stderr, process-group termination and pipe draining on cancellation/timeout.
- HTTP hooks reject non-public destinations by default, pin a validated IP while preserving Host/TLS SNI, disable environment proxies and redirects, redact sensitive payload fields and bound request/response bytes. Internal services require an exact `trusted_origins` entry; wildcards and embedded credentials are rejected.
- Prompt/Agent hooks use existing request-budget checks and usage accounting. Bounded subagent transports retain their shared model-call cap and avoid double usage accounting. Invalid JSON is a failed hook, not implicit approval.
- Exceptions, timeout, command nonzero exit, HTTP errors and malformed model output honor `block_on_failure`. Cancellation always propagates after cleanup.

## Retry policy

All three providers use `api/retry.py`, with SDK retries disabled where SDKs have an independent retry layer. Four **total** API attempts are allowed for transport failures, rate limiting and transient unavailable statuses. Exponential jitter and bounded Retry-After are shared.

| Condition | Behavior |
| --- | --- |
| Authentication / authorization / invalid request | No retry |
| Explicit quota exhaustion | No retry, including quota reported as HTTP 429 |
| Prompt too long / context length | No transport retry; existing context-compaction path handles it |
| User cancellation | Propagate; no retry |
| Partial stream then transient failure | Discard that attempt's buffered events; retry within cap |
| Tool error with unknown external effect | `uncertain`; no immediate replay, even under a new call ID when resources conflict |
| Tool error with proven no effect | Retry only if contract allows and total attempts remain |
| `reconcile_before_retry` | Call `BaseTool.reconcile_no_effect()` first; default is False |
| Tool timeout after possible write | `uncertain`, not an automatic retry |
| POST hook failure | Core result stays succeeded |

API attempts are buffered until a complete response, then committed to the existing event stream. This avoids changing Web redaction/reset protocols and prevents failed partial text reaching either QueryEngine or the UI. The tradeoff is delayed first-token display. The buffer has byte and event limits.

Each provider attempt gets an independent UUID and a stable logical request ID, with a durable running record written before the call. Settlement records classification, wait duration and reported/unknown usage. Missing usage is `null`/unknown, never invented zero. These records supplement, rather than replace, existing usage totals.

## SQLite receipts and recovery

`services/operations.py::OperationStore` uses SQLite WAL, `BEGIN IMMEDIATE`, unique operation/attempt keys and validated state edges. Research operations use the existing ResearchStore directory; other operations and API-attempt audit use the configured data directory's `executions/operations.sqlite3`.

A run is a user-message execution; the minimal step is one tool call. Tables store runs, steps, operations, tool attempts and API attempts. Operation identity hashes session + workspace scope + call ID, and binds tool/contract version/input digest. Receipts include attempt count, owner/PID, timestamps, resources, error code, private result/artifact references, optional external request ID, stable idempotency key and reconciliation evidence. Raw tool arguments are not stored in receipts. Result artifacts use mode 0600 and atomic replacement.

```text
prepared -> running -> succeeded | failed | partial | uncertain | cancelled | blocked
prepared -> blocked | cancelled
partial/uncertain -- verified reconciliation --> succeeded | failed
failed -- retry_failed(contract, verified_no_effect), within total cap --> prepared
```

Successful settlement is terminal/idempotent. Duplicate operation IDs with changed input/tool/version are rejected. Concurrent owners cannot claim the same operation. Explicit resources permit non-conflicting concurrency; legacy unknown resource scopes serialize. Child orchestration uses child scopes to avoid a parent holding its own children's lock.

On process loss, a running write becomes uncertain and a read-only attempt becomes failed. Expired lease timestamps alone never steal work from a live PID. Unresolved writes block conflicting operations. This is intentionally conservative and can require operator intervention after PID reuse or a lost coroutine in a still-live process.

`recover_messages()` attaches durable results to unmatched chat tool calls on session load and interruption. A succeeded operation reuses its artifact. Other states produce an explicit interrupted/recovery-required result. Old histories with no matching receipt retain their existing sanitization behavior. `continue_pending()` does not execute a persisted write by itself; chat JSON is not the source of execution truth.

Host recovery APIs:

- `recover(session=..., scope=...)`: classify dead-owner operations.
- `settle(operation, "succeeded"|"failed", evidence=...)`: resolve partial/uncertain only after external verification; supply a result artifact when marking success.
- `retry_failed(operation, contract, verified_no_effect=...)`: explicitly authorize a failed, non-effectful operation within the contract's remaining attempt cap.

No generic rollback is claimed. External POST/MCP reconciliation requires a provider-specific adapter or human verification. The default adapter refuses to certify absence of effects. A new model call ID cannot bypass an unresolved resource conflict.

## Operational limits and follow-up

- Exactly-once local receipt handling does not make a remote system exactly-once. The crash window between external success and local settlement remains uncertain.
- SQLite locking targets a single host. PID liveness is conservative; distributed workers are outside scope.
- HTTP pinning intentionally uses direct networking. Internal proxies require an explicit future trusted transport integration, not disabling validation.
- Legacy contracts default to serial execution, which can reduce concurrency until tools declare trustworthy resource scopes.
- PRE hooks and tools share one operation receipt. POST diagnostics do not constitute a separate durable hook workflow.
- Output schema-only declarations are descriptive; runtime structured validation requires `output_model`.
- API usage may remain unknown after stream failure or cancellation. An operator should reconcile billing externally if exact cost is required.

See [backend stabilization](BACKEND_STABILIZATION.md) for the private persistence, strict shell policy, bounded claim wait, retry adapter, schema version 2 and retention details, and [its validation record](testing/backend-stability-validation.md) for this run's commands and results. Earlier implementation evidence remains in [harness-next-validation.md](testing/harness-next-validation.md).

Retired tool migration, main-agent conflict reports and verified result-artifact recovery are described in [TOOL_RETIREMENT.md](TOOL_RETIREMENT.md).
