# Backend stabilization and operating limits

This extends the existing ToolRegistry, handwritten loop, ResearchStore, Skills and SQLite receipts. Planner/Replanner remain Agent-as-Tool; no workflow engine or separate workspace manager is added. See [validation evidence](testing/backend-stability-validation.md) for the actual checkout and commands.

## Imports and product compatibility

Tavily remains supported by current configuration and credential storage. The previous local work already restored its transport; `utils/search_types.py` now owns the shared `SearchBatch` DTO. HTML search imports independently of the paid transport. `utils.tavily_search.SearchBatch` and `search_tavily` remain compatible. Credentials still pass through existing storage, network guard and redaction.

Retired tool registration and old-session diagnostics remain as documented in [TOOL_RETIREMENT.md](TOOL_RETIREMENT.md). Dead conflict-investigation execution branches and runtime image-generation configuration have been removed. Host configuration services and MCP OAuth remain available; deprecated configuration fields still load without reactivating retired tools.

## Private persistence

On POSIX, dedicated config/data/session/research-state/upload/artifact directories use `0700`; sensitive snapshots, credentials, settings, state, locks, results and SQLite files use `0600`. Existing files are hardened at their storage entry points. SQLite WAL/SHM files are checked when connecting; a concurrent checkpoint removing a sidecar is harmless and does not recreate it. Explicit mode failure aborts an atomic replacement rather than publishing a permissive file.

The helpers reject direct symlinks and use no-follow descriptors when hardening POSIX files/directories. They do not chmod existing ancestors or ordinary workspace files. An explicitly configured application-storage directory is reserved private storage and is hardened; choose it accordingly. `MEMORY.md` and user report/export files retain their existing workspace semantics. Exporting a conversation to a user-selected file remains an explicit user action, rather than a private snapshot.

The current backend does not configure a file logging handler. Its reserved logs directory is private; any future persistent handler must explicitly create sensitive files with `0600`. Windows/non-POSIX chmod does not provide equivalent ACL guarantees. Failures are logged there; deployments must configure OS ACLs. This is not a guarantee against a hostile same-UID process replacing path components concurrently.

## Admission, waiting and shell execution

Permission order stays: hard deny → normalized resource/path/command policy → mode (including PLAN write rejection) → limited allow/confirmation. A generic `local_write` tool cannot become automatically admitted by reporting `is_read_only=True`. The compatibility exception is limited to exact built-in research-memory/project bookkeeping tools and host-owned capabilities; subclasses or metadata cannot obtain it. Effectful PRE hooks still run only after parameter validation and admission. POST failures do not undo a committed operation.

`ToolExecutionService(claim_wait_seconds=30.0)` bounds admission waiting independently of body timeout. Values must be finite and within 0–300 seconds. Polling starts at 20 ms, multiplies by 1.5 and caps at 500 ms. Deadline expiry yields blocked `operation_in_progress` for a live duplicate call, or `resource_conflict` for other conflicts. Unresolved writes yield `reconciliation_required`. No waiting executor changes another owner's state or runs hooks/body. Cancellation propagates. SQLite hot-operation busy timeout is 250 ms; cold schema initialization is separately bounded at 5 seconds.

Agent-generated shell and Skill calculations default to strict SRT (pinned 0.0.79) or configured Docker, including when ordinary `sandbox.enabled` is false. Missing isolation fails closed. Report execution keeps its stricter pre-existing policy. Process groups and pipes are cleaned on completion, timeout and cancellation.

An administrator can explicitly select trusted host execution using `sandbox.allow_trusted_host=true` with sandbox disabled. It requires a frozen main-agent capability snapshot as well as trusted settings, and emits a safety status before execution. Reports and subagents cannot inherit this grant; mutable tool metadata cannot enable it. Host mode has no sandbox isolation. Shell working-directory overrides remain inside the admitted cwd/project boundary.

Bundled script outputs are imported through host-side `SessionFiles` after success. Legacy `exports` declarations are bounded to 50 files/30 MiB each, scoped to cwd and rechecked against permission/private-storage policy; symlink escapes and hardlinks are rejected. The agent script cannot write the authoritative private store directly. User-installed Skill scripts outside readable sandbox roots may need copying into the approved workspace; approval alone does not broaden sandbox filesystem access.

Skill entry frontmatter is checked against its original approved root before reading, and the root is retained for progressive loading. Explicit host-approved root aliases are normalized once for compatibility. Automatically discovered external directory aliases and plugin child symlinks are rejected. Markdown traversal does not follow directory symlinks.

## Business and API retries

`ToolResult` adds compatible `retryable: bool = False` and `no_effect: bool | None = None`. Legacy explicit metadata is accepted. Domain errors (auth, authorization, invalid request, quota, context length, cancel) and partial/uncertain results never cause ordinary business retries.

| Contract mode | Required behavior |
| --- | --- |
| `never` | One body attempt, regardless of `retryable` |
| `idempotent` | Explicit semantically idempotent contract plus transient `retryable`; this implementation conservatively also requires read-only effect or proven `no_effect` |
| `idempotency_key` | `idempotency_key_supported=True` and an override of `execute_with_idempotency_key(arguments, context, *, idempotency_key)`; adapter must send the stable host key to a remote system that supports it. Still requires transient error and known absence of effects |
| `reconcile_before_retry` | Transient error followed by real `reconcile_no_effect()` returning True; default False proves nothing |

Tool attempts are capped at 3 total. Exceptions/timeouts are not blindly replayed. External writes, unknown and mixed effects with unverified failure become `uncertain`. No currently shipped HTTP/MCP write adapter claims remote idempotency support merely because a context key exists. A declared adapter's actual remote guarantee must be verified when integrating it.

Provider transport attempts are separately capped at 4, with SDK retries disabled. Structured error codes take precedence; ordinary rate limits/transport/unavailable errors can retry, while auth, invalid request, quota exhausted, context length and cancel cannot. Retry-After and jitter are bounded to 30 seconds. Default write contracts prevent multiplicative business/transport replay.

Each API attempt buffers complete serialized events, capped at 16 MiB and 200,000 events, and commits only after a complete message. Failed text/tool/image events never reach Web/history. This intentionally delays visible output. Missing usage remains unknown/null; legacy nonzero usage without provider provenance is estimated. A failed audit settlement on cancellation is logged and cancellation still propagates; its running record needs recovery/verification rather than pretending success.

## SQLite migration, recovery and retention

Schema version 2 transactionally adds nullable API-attempt status/time columns and indexes for scope/status, session/status/scope, run/status, operation-attempt status and API status/time. Existing records remain readable, including rows whose new fields are NULL. Newer unsupported schemas are rejected. Initialization caches PID + absolute path + device/inode under a bounded thread lock, with an LRU limit of 256; fork resets the cache and lock. Cross-process initialization is protected by SQLite transactions. Each operation still uses independent connections/transactions.

Receipts keep the existing prepared/running/succeeded/failed/partial/uncertain/cancelled/blocked state machine, unique call/operation/attempt IDs and stable key. Successful results are reused; dead-owner writes become uncertain, and reconciliation evidence is required before replay. Chat JSON is not a write-recovery protocol. A live process is not displaced merely because its lease timestamp expired.

`OperationStore.prune_audit(before=<epoch>, limit=1000)` is explicit host maintenance, never agent-triggered automatic deletion. Limit is 1–10,000 total rows. It removes only old terminal API attempt details with **reported** usage and old non-running tool-attempt details attached to terminal succeeded/failed/cancelled operations. Unknown/estimated usage, legacy NULL rows and prepared/running/partial/uncertain/blocked operations are retained. Stable operation identities, run/step records and result artifacts are retained to prevent duplicate execution after chat recovery. Deployments should monitor growth; complete archival requires a separate retained identity/tombstone policy before removing receipts or referenced artifacts.

This is a single-host SQLite design. An external success followed by process loss or failed receipt settlement still requires provider-specific reconciliation or manual verification. No exactly-once remote effect or rollback is promised. Do not retry a write simply because audit storage failed.
