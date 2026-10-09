# Harness 下一阶段：产品边界、MCP 恢复和验收

2026-10-09，工作区 `/home/jason/pythonproject/OpenHarness`。

## Checkout 和已有工作

本轮开始 HEAD 为 `987802f8ed43538ed4823b7d16f5f72e9ccffecb`，已有 **360 条状态项**，包含上一阶段 Harness、研究架构、工作区与 Token 预算改造。
开始状态已留存；staged patch SHA-256 为 `5a92cb75ea64f8388822ac81c13f459ba7dd43632cefbade749ca70f4ad7ee69`。
执行中外部提交将 HEAD 更新为 **`027bf24bfdb6e503c34741a628df754adac354e0`**（`feat:version 2`）。本轮没有执行提交、暂存、重置、清理或 checkout/restore 回退。
适用目录未发现 AGENTS.md。本轮增量继续基于新 HEAD，当前状态见 [workspace-status](harness-next-logs/workspace-status.txt)。

外部新提交删除了 `utils/tavily_search.py`，但 web_search、凭据脱敏和测试仍直接依赖它，造成导入失败。
依据本轮开始时留存的最新 staged 源码恢复缺失文件，仅修复当前调用方需要的依赖；没有用旧基线覆盖现有模块。
恢复后 Tavily 离线回归和完整测试均可收集。该依赖修复在文件清单中明确列出。

## 实际落地

- `tools/retired.py` 统一六个退休工具名。Registry 拒绝退休名称，即使显式 replace；research/general 兼容入口保持同一保留工具表。插件 Hook 等能力不受影响。
- `ResearchMemory.submit_conflict_report` 在原 store 的 revision、幂等和文件锁下提交报告，验证双方证据/论证与当前范围。报告等待主 Agent 单独 review/resolve；不创建第二套调查 Loop、不自动提交结论或完成任务。
- 修正研究提示词和最终答复冲突修复指令中对已删除工具的依赖。未决裁决须明确 reopen 后才可提交另一份报告；旧 running 调查保留 host recovery 兼容路径。
- MCP 的实际 manager 区分 pre-dispatch 无会话与 post-dispatch 断连/超时/远端错误。仅 request_not_sent=True 证明 no_effect；默认仍不自动重试。
- 成功收据复用额外校验结果产物的 success 状态和 operation ID。人工核验成功后的旧错误产物、错配产物仍阻塞，并禁止重复外部写入。
- 旧测试迁移到保留的文件工具、宿主配置与通用子代理路径；原来的 4 个收集错误和 7 个产品表面失败不再需要 ignore/skip。
- E2E 暴露并稳定复现首次连接竞态：ready 已到达时，异步 ensureSession 的旧闭包可能把消息滞留。`useConversation` 以当前 socket 的 ready session ref 准入；断线后的不自动重发策略保留。新增确定性 E2E 验证只发送一条用户消息。
- 并发运行下，旧取消测试的 0.3 秒定时子进程可能在 cancel 前已写入。改为显式 release 门闩，检查 PID 已退出和无残留副作用；没有放宽安全断言或新增 skip。

主要文件：`research/{models,conflicts,prompt}.py`、`tools/{base,__init__,retired,research_memory_tool,mcp_tool}.py`、`mcp/client.py`、`engine/query.py`、`services/tool_execution.py`、`utils/tavily_search.py`、`frontend/web/src/useConversation.ts`、Web E2E、研究/工具测试和新增 `test_harness/test_product_recovery.py`。
完整列表见 [implementation-files](harness-next-logs/implementation-files.txt)，接口及迁移规则见 [TOOL_RETIREMENT.md](../TOOL_RETIREMENT.md)。

## 验收及中间失败

起始实测：4 个收集错误；产品目标组 7 failed / 53 passed。随后外部 checkout 变化短暂造成 Tavily 导入失败，已修复。
第一次两版本全量各 1238 passed；补充产物匹配和 unresolved 重复提交用例后可收集 1240 项。
Python 3.11 完整运行 1240 passed，之后的 Harness/冲突目标组 72 passed（含确定性取消测试）。
Python 3.10 中间一次 1239 passed / 1 failed，旧取消测试已改用 release 门闩，最后完整重跑 **1240 passed**。最终两版本均无失败、无 skip、无收集错误；中间失败保留在下表。
Web 初次 16 passed / 1 failed，新增竞态用例在修复前稳定失败；修复后完整 **18 passed**。Web build 通过。
Ruff、格式检查、类型检查和 diff 检查通过。所有全量命令均没有 --ignore；没有新增 skip 或删除测试文件。

下表保留本轮所有测试运行和中间失败。新日志是本轮结果；上一阶段的报告作为历史保留。
Python 在根目录运行；npm E2E 在 frontend/web。相同节点重复参数那次 pytest 实际仅运行 1 项，未将它宣称为 3 次通过。

| 日志 | 实际命令 | 当次结果 |
| --- | --- | --- |
| [baseline-collection](harness-next-logs/baseline-collection.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_tools/test_core_tools.py tests/test_tools/test_image_generation_tool.py tests/test_tools/test_mcp_auth_tool.py tests/test_research/test_investigation.py --tb=short` | 4 errors in 5.05s |
| [baseline](harness-next-logs/baseline.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_entrypoints/test_research_product.py tests/test_research/test_memory_compatibility.py tests/test_research/test_workspace_memory.py tests/test_tools/test_integration_flows.py tests/test_research/test_dispatch_subagents.py::test_removed_tools_are_uncallable_and_undiscoverable_in_research_mode --tb=short` | 7 failed, 53 passed in 14.02s |
| [product](harness-next-logs/product.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_tools/test_core_tools.py tests/test_tools/test_image_generation_tool.py tests/test_tools/test_mcp_auth_tool.py tests/test_research/test_investigation.py tests/test_research/test_memory_compatibility.py tests/test_research/test_conflicts.py tests/test_entrypoints/test_research_product.py tests/test_research/test_workspace_memory.py tests/test_tools/test_integration_flows.py tests/test_research/test_dispatch_subagents.py::test_removed_tools_are_uncallable_and_undiscoverable_in_research_mode --tb=short` | 1 error in 4.40s |
| [collection](harness-next-logs/collection.log) | `uv run pytest --collect-only -q` | 见日志（未获得完成统计） |
| [product2](harness-next-logs/product2.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_tools/test_core_tools.py tests/test_tools/test_image_generation_tool.py tests/test_tools/test_mcp_auth_tool.py tests/test_research/test_investigation.py tests/test_research/test_memory_compatibility.py tests/test_research/test_conflicts.py tests/test_entrypoints/test_research_product.py tests/test_research/test_workspace_memory.py tests/test_tools/test_integration_flows.py tests/test_tools/test_tavily_search.py tests/test_research/test_dispatch_subagents.py::test_removed_tools_are_uncallable_and_undiscoverable_in_research_mode --tb=short` | 8 failed, 126 passed in 15.71s |
| [review](harness-next-logs/review.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_research/test_investigation.py tests/test_research/test_memory_compatibility.py tests/test_tools/test_image_generation_tool.py --tb=short` | 7 failed, 18 passed in 5.19s |
| [taskdiff](harness-next-logs/taskdiff.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_research/test_memory_compatibility.py::test_report_conflict_investigation_uses_common_loop_and_preserves_authority -vv --tb=short` | 1 failed in 3.76s |
| [recovery](harness-next-logs/recovery.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_mcp tests/test_tools/test_mcp_tool.py tests/test_research/test_investigation.py tests/test_research/test_memory_compatibility.py tests/test_tools/test_image_generation_tool.py --tb=short` | 114 passed in 9.37s |
| [collection2](harness-next-logs/collection2.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest --collect-only -q` | 1238 tests collected in 2.00s |
| [full311](harness-next-logs/full311.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q --tb=short` | 1238 passed in 343.38s (0:05:43) |
| [full310](harness-next-logs/full310.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q --tb=short` | 1238 passed in 377.28s (0:06:17) |
| [final-slice](harness-next-logs/final-slice.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_research/test_investigation.py tests/test_research/test_memory_compatibility.py tests/test_tools/test_core_tools.py tests/test_tools/test_image_generation_tool.py tests/test_tools/test_mcp_auth_tool.py --tb=short` | 97 passed in 10.65s |
| [final311](harness-next-logs/final311.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q --tb=short` | 1240 passed in 318.43s (0:05:18) |
| [final310](harness-next-logs/final310.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q --tb=short` | 1 failed, 1239 passed in 352.87s (0:05:52) |
| [collection-final](harness-next-logs/collection-final.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest --collect-only -q` | 1240 tests collected in 1.98s |
| [cancel311](harness-next-logs/cancel311.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness/test_retry_hooks.py::test_hook_cancel_kills_child_process_group --tb=short` | 1 passed in 7.47s |
| [cancel310](harness-next-logs/cancel310.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q tests/test_harness/test_retry_hooks.py::test_hook_cancel_kills_child_process_group --tb=short` | 1 passed in 2.63s |
| [cancel-repeat](harness-next-logs/cancel-repeat.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q tests/test_harness/test_retry_hooks.py::test_hook_cancel_kills_child_process_group tests/test_harness/test_retry_hooks.py::test_hook_cancel_kills_child_process_group tests/test_harness/test_retry_hooks.py::test_hook_cancel_kills_child_process_group --keep-duplicates --tb=short` | 1 passed in 3.76s |
| [security-final](harness-next-logs/security-final.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_research/test_investigation.py --tb=short` | 72 passed in 6.55s |
| [final310-verified](harness-next-logs/final310-verified.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q --tb=short` | 1240 passed in 316.98s (0:05:16) |
| [e2e](harness-next-logs/e2e.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring OPENHARNESS_TEST_BROWSER=/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome npm run test:e2e` | 1 failed / 16 passed (2.2m) |
| [web-race-before](harness-next-logs/web-race-before.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring OPENHARNESS_TEST_BROWSER=/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome npm run test:e2e -- --grep 'first message is delivered'` | 测试文件路径写入错误；无测试匹配（随后修正并执行） |
| [web-race-before2](harness-next-logs/web-race-before2.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring OPENHARNESS_TEST_BROWSER=/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome npm run test:e2e -- --grep 'first message is delivered'` | 1 failed |
| [e2e-final](harness-next-logs/e2e-final.log) | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring OPENHARNESS_TEST_BROWSER=/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome npm run test:e2e` | 18 passed (1.9m) |
| [web-build](harness-next-logs/web-build.log) | `npm run build (frontend/web)` | ✓ built in 14.84s |
| [web-build-final](harness-next-logs/web-build-final.log) | `npm run build (误在仓库根目录执行)` | 失败：根目录没有 package.json；正确目录随后构建通过 |
| [web-build-final2](harness-next-logs/web-build-final2.log) | `npm run build (frontend/web)` | ✓ built in 4.80s |
| [types](harness-next-logs/types.log) | `uv run python scripts/check_types.py` | 全部类型检查组通过 |
| [types-final](harness-next-logs/types-final.log) | `uv run python scripts/check_types.py` | 全部类型检查组通过 |
| [ruff-final](harness-next-logs/ruff-final.log) | `uv run ruff check .` | All checks passed! |
| [format-final](harness-next-logs/format-final.log) | `uv run ruff format --check src tests scripts` | 344 files already formatted |
| [diff-final](harness-next-logs/diff-final.log) | `git diff --check` | 通过（无输出） |

格式化命令使用 `uv run ruff format` 明确列举本轮修改文件；初次 Ruff 查出两个新增测试的 unused import，使用针对这两个文件的 `ruff check --fix` 修复。没有修改行为断言来修 lint。
文档 JSON 示例使用 `ResearchMemoryInput.model_validate` 实测通过。测试时沿用上一阶段的 fail Keyring 文件凭据回退和兼容 Chromium，没有修改生产配置。

## 剩余范围

任意 MCP 外部写仍没有通用的状态查询/幂等保证。只有具体外部系统提供可信接口时才能开发 reconciliation adapter；当前 uncertain 继续阻塞，host 可以携带外部核验证据和正确成功产物结算。
未使用真实供应商做计费/付费搜索/外部写入端到端核验，usage 不可用时继续 unknown。SQLite 仍是单机方案。
保留工具中尚未声明精确资源范围的工具仍按保守串行契约执行，后续应逐个增加明确的资源声明及冲突测试。
低首字延迟的 attempt reset 协议和多机恢复仍不在本轮范围。
