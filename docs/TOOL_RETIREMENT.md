# Retired tools and retained research workflows

The product exposes one research ToolRegistry. `mode="general"` remains an accepted
compatibility argument and uses the same retained tool surface. It does not load old
tool implementations. `RESEARCH_EXCLUDED_TOOLS` aliases `RETIRED_TOOL_NAMES` for existing
callers. Plugins cannot register a retired exact name, even with `replace=True`.
Runtime filters those names while preserving the plugin's hooks and other services.
Configured MCP tools remain namespaced; their contracts and permissions still apply.

| Retired tool | Retained path |
| --- | --- |
| `config` | Host `Settings`, `load_settings` / `save_settings`, configuration UI / CLI |
| `mcp_auth` | Host MCP server headers/env configuration and `McpClientManager.update_server_config` / `reconnect_all` |
| `notebook_edit` | Ordinary `read_file`, `write_file`, `edit_file` on JSON notebooks; no automatic cell management |
| `image_generation` | Legacy configuration stays readable/redacted; no built-in generation capability |
| `sleep` | Host scheduling or bounded execution timeouts; no model-callable sleep |
| `investigate_conflict` | Main Agent evidence/argument collection, `research_memory.submit_conflict_report`, then separate review and `resolve_conflict` |

Existing session history can still contain retired tool calls. New invocations return
Unknown tool without executing hooks, transports or side effects. No alias silently
reinterprets old arguments as a new operation. Main/child tool schemas and search omit
retired names. The hand-written loop, Planner/Replanner tools, ResearchTask,
CompletionPolicy and candidate-only dispatch architecture remain in place.

## Main-agent conflict reports

The main Agent reads the original material from both sides, registers additional
sources/evidence/reasoning if needed, and uses the current revision:

```json
{
  "operation": {
    "action": "submit_conflict_report",
    "operation_id": "unique-main-report-operation",
    "expected_revision": 12,
    "conflict_id": "conflict_existing_id",
    "report": {
      "outcome": "prefer_side",
      "preferred_side": 1,
      "statement": "Use the corrected disclosure",
      "rationale": "Both disclosures and their correction relationship were checked",
      "evidence_ids": ["ev_original", "ev_correction"],
      "step_ids": ["step_comparison"],
      "assessments": [
        {
          "evidence_id": "ev_original",
          "originality": "Original disclosure",
          "directness": "Directly states the metric",
          "scope_match": "Same company and period",
          "timing_and_corrections": "Earlier, subsequently corrected version",
          "independence": "Same issuer; do not count as independent confirmation",
          "reproducibility": "Original table and correction can be compared"
        },
        {
          "evidence_id": "ev_correction",
          "originality": "Issuer's correction",
          "directness": "Directly corrects the metric",
          "scope_match": "Same company and period",
          "timing_and_corrections": "Correction explicitly supersedes the earlier figure",
          "independence": "Same issuer",
          "reproducibility": "Correction text identifies the original table"
        }
      ],
      "rejected_reasons": ["The original value was corrected"]
    }
  }
}
```

The example is schema-valid; replace its references with actual IDs in the current
store and its revision with the latest revision. The provider receives the exact
Pydantic operation schema. `ArbitrationDecision` keeps its originality, relevance,
scope, timing/correction, independence and reproducibility requirements.

`submit_conflict_report` runs inside the existing ResearchStore mutation lock and
revision/idempotency checks. It validates every side's evidence and supporting
reasoning, current research scope and decision structure. It creates a completed
arbitration report and sets the conflict to `awaiting_review`. It does **not** replace
conclusions, complete a task, update a plan, mutate MEMORY.md or mark facts verified.
The returned `arbitration_id` is used by the main Agent's separate `resolve_conflict`
operation after reading/reviewing the report. Existing stale-fingerprint checks,
successor conclusions and evidence verification rules continue to apply.

Completed/pending-review/unresolved reports cannot be silently resubmitted with a new
operation ID. Reopen with a reason and, when applicable, new evidence before another
report. Unresolved decisions remain uncertain. A legacy running investigation blocks
submission until the host establishes that execution has stopped and calls existing
recovery; historical arbitration records remain loadable.

Generic dispatch children can only return candidates and read authorized research
memory. They cannot submit reports or authoritative mutations. Their budget,
cancellation, source files, workspace isolation and late-result rejection remain
covered by the existing dispatch tests. Main-run completion still invokes the
CompletionPolicy: resolving a conflict does not complete an unfinished report task.

## MCP delivery and recovery

`McpServerNotConnectedError.request_not_sent` defaults to **False**. Only the real
manager's pre-dispatch missing-session check sets it to True. A transport error,
remote error or timeout after dispatch does not prove that the remote write failed.
The adapter forwards the proven case as `no_effect=True`; its receipt is failed with
one attempt. The MCP contract still defaults to `retry_mode=never`.

After dispatch, failures remain uncertain, retain their receipt, and block replay or
conflicting operations under a different call ID after restart. An MCP tool's schema,
name or connection error alone is not an external state-query adapter. No generic
status lookup or automatic remote POST retry is introduced.

A host/operator may settle uncertain/partial work only with external verification
and reconciliation evidence. Marking a receipt succeeded is insufficient to reuse
its old error artifact. A reusable artifact must be a valid `ToolResultBlock` with
`is_error=False`, `result_metadata.status="success"`, and the same `operation_id`.
Missing, mismatched or unsuccessful artifacts return `artifact_unavailable` and do
not execute the tool. Restore a verified success artifact via the host's atomic,
private artifact-writing path; do not instruct the model to forge one or expose raw
credentials. Successful receipts remain terminal.

## Test migration

The previously uncollectable files were migrated, not skipped or deleted:

- Core notebook operations now verify JSON through the retained file tools, including
  edit/read round trips and escape-path rejection.
- Configuration and MCP credentials test host persistence and reconnect behavior,
  and assert that model-callable configuration/auth tools stay absent.
- Image generation tests verify that legacy credentials/provider selection cannot
  cause generation or file/network side effects; environment parsing stays covered.
- Dedicated-investigator tests now cover main-loop report/review/idempotency,
  invalid/stale inputs, legacy recovery, bounded transport usage/cancellation and
  candidate-only child restrictions. Dispatch integration covers child execution.

No `--ignore`, new skip or provider-schema weakening is needed for the full suite.
See [this stage's validation record](testing/harness-next-validation.md).
