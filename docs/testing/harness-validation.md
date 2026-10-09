# Harness 改造：实施与验证记录

日期：2026-10-09。工作区 `/home/jason/pythonproject/OpenHarness`。

## 工作区与基线

开始和结束的 HEAD 均为 `987802f8ed43538ed4823b7d16f5f72e9ccffecb`，分支 `main...origin/main`。开始时已有 312 条 Git 状态项，包含已暂存改造、未暂存上下文预算改造和旧模块删除。未执行 reset、clean、restore、checkout 覆盖、提交或暂存操作。

阶段内留存的 staged patch 与结束时 `git diff --cached` 的 SHA-256 一致：`5a92cb75ea64f8388822ac81c13f459ba7dd43632cefbade749ca70f4ad7ee69`。保留原有 staged 工作，增量修改位于工作树。源码盘点未发现适用的 AGENTS.md。最终状态见 [workspace-status.txt](harness-logs/workspace-status.txt)。

开发前实际基线：目标 Python 回归 **376 通过、1 失败**，失败为 `test_h_compaction_failure_retains_original_when_hard_cap_rejects`；Ruff 当时有 6 个上下文预算测试导入/未使用符号错误；Web build 通过。首次 E2E 因指定 Chromium 不存在而无法启动。这些都是本轮实测，未沿用旧 CI 结论。

## 最终验证摘要

- Python 3.11.17、3.10.22：最终目标组各 **472 passed**，包含 API、权限、Hooks、引擎、沙箱、Web、上下文服务、来源保留及研究集成。
- 独立 Harness 契约/安全测试最后运行 **41 passed**；与上项重叠，不累计成独立覆盖总数。
- 最后两版广泛套件各 **1176 passed / 8 failed**。其中路径诊断回归随后修复，定向 **39 passed**，并包含于最终 472 项通过组。剩余 **7 项**与开始时已移除的旧工具有关，列于下文。不能将广泛套件宣称全绿。
- 完整 pytest 收集仍有 **4 个模块导入错误**；广泛套件的 `--ignore` 仅用于让其余文件运行，完整收集错误另外保留，未删除测试、增加 skip 或修改原断言来隐藏缺失模块。
- Web 最后执行 **17/17 passed**；最终 `npm run build` 通过。
- `ruff check .`、`ruff format --check src tests scripts`、`scripts/check_types.py`、`git diff --check` 最后通过。格式化 49 个文件，前后 AST 对比全部相同；见 [format-complete.log](harness-logs/format-complete.log)。
- 最终上述目标组及广泛组无 pytest skip 统计；不可收集的四个模块属于未运行，不属于通过或跳过。

原生最新 Playwright 安装器不支持本机 Ubuntu 20.04；通过独立安装 Playwright 1.48.2 的 Chromium 1140，并设置 `OPENHARNESS_TEST_BROWSER` 运行现有测试，未修改项目 Playwright 版本。桌面 Secret Service 在自动化中等待解锁；验证时用 `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` 走已有文件凭据回退，未修改生产凭据策略。中间 E2E 曾有 4 失败 / 1 中断 / 12 未运行；最终重跑 17 项均通过。

Python 3.10 临时环境最初由非锁定安装引入 Pydantic 2.14.0，出现额外 6 个静态 schema 比较失败。使用 `UV_PROJECT_ENVIRONMENT=/tmp/harness-py310 uv sync --frozen --all-extras --dev --python 3.10` 对齐仓库锁定的 Pydantic 2.13.5 后，相关 46 项通过。未更改 uv.lock 或弱化 schema 比较。

## 主要修改与职责

| 文件/范围 | 实际目的 |
| --- | --- |
| `tools/contracts.py`、`tools/base.py` | 强类型内部契约、旧工具 resolver、显式结果/操作上下文、注册冲突拒绝 |
| `services/tool_execution.py`、`engine/query.py` | 单一执行边界；完整来源采集后截断模型可见输出；权限/Hook/收据/取消编排 |
| `services/operations.py` | SQLite Run/Step/Operation/Attempt；状态验证、资源冲突、去重、人工核验恢复 |
| `permissions/checker.py`、`permissions/capabilities.py` | deny 优先、规范化路径、PLAN 写限制、不可从 metadata 扩权的能力上下文 |
| `hooks/{schemas,executor,safety}.py`、`utils/{shell,network_guard}.py` | Hook 语义、独立准入、环境最小化、HTTP IP 固定及重定向阻止、输出限制和进程组清理 |
| `api/{retry,client,openai_client,codex_client}.py` | 统一重试分类、关闭 SDK 内层重试、流式尝试暂存、逐 attempt 持久 usage 状态 |
| `engine/{query_engine,subagents}.py`、`runtime.py` | 恢复接入、保留来源、Hook usage、子代理上下文继承、插件注册失败清理 transport |
| bash、文件、MCP、HTTP、模型、研究工具 | 增量声明契约；研究控制与调度使用明确资源范围，保留 Runtime 对晚到结果的拒绝 |
| `evaluation/fixtures.py`、相关测试夹具 | 用显式宿主替换适配严格注册规则；新逻辑调用使用新的 call ID |
| 现有 context budget/source/compact/store 文件 | 补齐类型注解，集成既有 Token 预算工作，不替换其实现 |
| `tests/test_harness/` | 权限前无副作用、取消/崩溃、成功复用、并发去重、重试上限、来源与 Hook 安全测试 |
| README、`docs/HARNESS_EXECUTION.md` | 开发接口、默认值、重试矩阵、恢复流程与限制 |

完整本轮实现清单见 [implementation-files.txt](harness-logs/implementation-files.txt)，纯格式化清单见 [format-only-files.txt](harness-logs/format-only-files.txt)。这些清单不把工作区原有的大量 staged 改动算成本轮新实现。

## 契约与恢复落地

字段默认值、执行顺序、权限优先级、Hook 语义与 Retry/Receipt 状态机详见 [HARNESS_EXECUTION.md](../HARNESS_EXECUTION.md)。关键原则为：校验/准入先于 PRE 效果 Hook；副作用前准备并 claim 收据；核心结果先持久结算再跑 POST；成功操作复用产物，uncertain 不自动重放；同一逻辑操作的业务重试和显式恢复共享总尝试上限与幂等键。

现有手写 Loop、Planner/Replanner Agent as Tool、ResearchTask、CompletionPolicy、ResearchStore、工具调用 ID 配对、JSON 会话及上下文预算保留。研究来源在取消时仍可读取；收据中的取消/不确定状态不会被来源可用性覆盖。流式 API 采用尝试内暂存后提交，避免失败 partial text 进入 Web redaction buffer；代价是首字延迟。

## 剩余失败与未运行

完整收集失败文件：

1. `tests/test_research/test_investigation.py`：缺失 `investigate_conflict_tool`。
2. `tests/test_tools/test_core_tools.py`：缺失 `config_tool`（并引用 notebook 工具）。
3. `tests/test_tools/test_image_generation_tool.py`：缺失 `image_generation_tool`。
4. `tests/test_tools/test_mcp_auth_tool.py`：缺失 `mcp_auth_tool`。

七个仍存在的旧产品表面失败：

1. `test_registry_contains_only_research_tools`：仍要求 `investigate_conflict`。
2. `test_removed_tools_are_uncallable_and_undiscoverable_in_research_mode`：仍要求 general registry 包含已删除工具。
3. `test_report_conflict_investigation_uses_common_loop_and_preserves_authority`：导入已删除模块。
4. `test_existing_tools_reject_escape_paths[notebook_edit-values8]`。
5. `test_existing_tools_reject_escape_paths[image_generation-values11]`。
6. `test_skill_and_config_flow_across_registry`。
7. `test_notebook_flow_across_registry`。

这些工具模块/注册项在开始时已被移除。本轮保留移除，不恢复旧基线、不删除测试来取绿。相关断言需要与产品移除范围另行统一。最新广泛套件日志还包含已修复的 `test_e2e_06_project_switch_files_context_and_child_boundaries` 路径错误消息回归；末次目标组已覆盖并通过，未把旧广泛日志改写成成功。

真实供应商的计费和外部写入未做在线端到端核验。外部写操作只能在具体系统支持状态查询时自动 reconciliation；默认阻塞并要求人工核验。SQLite 为单机方案，PID 存活判断保守；活进程丢失执行协程或 PID 复用可能需要人工处理。API usage 缺失保持 unknown。POST Hook 诊断没有另建独立工作流。其他 Python 版本、操作系统和最新 Chromium 未在本轮运行。

## 测试命令总表

以下列出本轮各次执行，包含开发中失败和中止的命令；不能把中间结果当作最终状态。pytest 默认工作目录为仓库根目录；日志路径原件为 `/tmp/harness-<名称>.log`，主要验收日志已复制到本目录 `harness-logs/`。

| 日志 | 实际命令 | 当次结果 |
| --- | --- | --- |
| `baseline-pytest` | `uv run pytest -q tests/test_permissions tests/test_hooks tests/test_api tests/test_engine tests/test_sandbox tests/test_web tests/test_services --tb=short` | 1 failed, 376 passed in 48.15s |
| `phase0` | `uv run pytest -q tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox --tb=short` | 147 passed in 6.14s |
| `phase12` | `uv run pytest -q tests/test_permissions tests/test_tools/test_core_tools.py tests/test_engine --tb=short` | 1 error in 3.61s |
| `phase3` | `uv run pytest -q tests/test_hooks tests/test_permissions tests/test_engine --tb=short` | 5 failed, 107 passed in 14.90s |
| `phase4` | `uv run pytest -q tests/test_api --tb=short` | 2 failed, 82 passed in 3.02s |
| `phase4b` | `uv run pytest -q tests/test_api tests/test_hooks tests/test_permissions tests/test_engine --tb=short` | 196 passed in 7.77s |
| `collection` | `uv run pytest --collect-only -q` | 1135 tests collected, 4 errors in 1.53s |
| `phase35` | `uv run pytest -q tests/test_api tests/test_hooks tests/test_permissions tests/test_engine tests/test_research/test_report_runtime.py tests/test_research/test_report_sandbox.py --tb=short` | 1 failed, 234 passed in 32.02s |
| `phase5` | `uv run pytest -q tests/test_harness --tb=short` | 1 failed, 10 passed in 3.50s |
| `phase5b` | `uv run pytest -q tests/test_harness tests/test_engine tests/test_hooks --tb=short` | 3 failed, 74 passed in 6.85s |
| `phase5c` | `uv run pytest -q tests/test_harness --tb=short` | 26 passed in 5.13s |
| `regression` | `uv run pytest -q tests/test_api tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox tests/test_web tests/test_services tests/test_harness --tb=short` | 3 failed, 425 passed in 55.34s |
| `all-available` | `uv run pytest -q --ignore=tests/test_research/test_investigation.py --ignore=tests/test_tools/test_core_tools.py --ignore=tests/test_tools/test_image_generation_tool.py --ignore=tests/test_tools/test_mcp_auth_tool.py --tb=short` | 中止：桌面钥匙环等待或诊断期间的清理等待；未完整执行 |
| `py310` | `/tmp/harness-py310/bin/python -m pytest -q tests/test_api tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox tests/test_web tests/test_services tests/test_harness --tb=short` | 中止：桌面钥匙环等待或诊断期间的清理等待；未完整执行 |
| `research-first` | `uv run pytest -q tests/test_research/test_agent_core_integration.py -x --tb=short` | 1 failed in 11.78s |
| `research-second` | `uv run pytest -q tests/test_research/test_agent_core_integration.py -x --tb=short` | 10 passed in 303.12s (0:05:03) |
| `hook-cleanup` | `uv run pytest -q tests/test_harness/test_retry_hooks.py::test_command_hook_environment_and_output_bound --tb=short` | 1 passed in 2.30s |
| `quick-final` | `uv run pytest -q tests/test_harness tests/test_engine --tb=short` | 80 passed in 6.44s |
| `py310-final` | `/tmp/harness-py310/bin/python -m pytest -q tests/test_api tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox tests/test_web tests/test_services tests/test_harness --tb=short` | 中止：桌面钥匙环等待或诊断期间的清理等待；未完整执行 |
| `web-diagnostic` | `uv run pytest -q tests/test_web -x -o faulthandler_timeout=20 --tb=short` | 中止：桌面钥匙环等待或诊断期间的清理等待；未完整执行 |
| `core-debug` | `uv run pytest -q tests/test_research/test_agent_core_e2e.py -x --log-cli-level=DEBUG` | 1 failed, 12 passed in 117.00s (0:01:57) |
| `regression-final` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_api tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox tests/test_web tests/test_services tests/test_harness --tb=short` | 430 passed in 81.26s (0:01:21) |
| `py310-complete` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q tests/test_api tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox tests/test_web tests/test_services tests/test_harness --tb=short` | 430 passed in 90.65s (0:01:30) |
| `security-final` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness --tb=short` | 33 passed in 14.28s |
| `suite311` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q --ignore=tests/test_research/test_investigation.py --ignore=tests/test_tools/test_core_tools.py --ignore=tests/test_tools/test_image_generation_tool.py --ignore=tests/test_tools/test_mcp_auth_tool.py --tb=short` | 14 failed, 1163 passed in 495.29s (0:08:15) |
| `suite310` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q --ignore=tests/test_research/test_investigation.py --ignore=tests/test_tools/test_core_tools.py --ignore=tests/test_tools/test_image_generation_tool.py --ignore=tests/test_tools/test_mcp_auth_tool.py --tb=short` | 19 failed, 1158 passed in 532.27s (0:08:52) |
| `evaluation` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_evaluation --tb=short` | 2 failed, 38 passed in 12.77s |
| `entrypoints` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_entrypoints --tb=short` | 2 failed, 17 passed in 5.07s |
| `eval-security` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_evaluation tests/test_harness --tb=short` | 73 passed in 16.00s |
| `security-migration` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_entrypoints/test_research_product.py::test_runtime_filters_retired_plugin_tools_but_keeps_services_and_config --tb=short` | 35 passed in 6.48s |
| `security-complete` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness --tb=short` | 36 passed in 5.19s |
| `last-boundary` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_engine tests/test_permissions tests/test_hooks --tb=short` | 149 passed in 9.08s |
| `repaired-regressions` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_entrypoints/test_research_product.py::test_runtime_filters_retired_plugin_tools_but_keeps_services_and_config tests/test_research/test_agent_core_e2e.py::test_e2e_15_continuation_cancellation_settles_children_and_state --tb=short` | 2 passed in 4.53s |
| `final-integration` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_research/test_dispatch_subagents.py tests/test_research/test_dispatch_recovery.py tests/test_research/test_report_runtime.py tests/test_research/test_workspace_memory.py tests/test_engine tests/test_permissions tests/test_hooks tests/test_evaluation --tb=short` | 45 passed in 235.72s (0:03:55)；中断，非完整通过 |
| `source-replan` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_research/test_engine.py tests/test_research/test_agent_core_integration.py::test_e2e_10_late_children_cannot_pollute_revised_plan tests/test_harness --tb=short` | 50 passed in 17.36s |
| `dispatch-diagnostic` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring timeout 45s uv run pytest -vv tests/test_research/test_dispatch_subagents.py -x --tb=short` | 1 failed, 41 passed in 10.99s |
| `py310-locked-skills` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q tests/test_research/test_skill_workflows.py tests/test_skills/test_package_workflows.py --tb=short` | 46 passed in 14.80s |
| `acceptance311` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q --ignore=tests/test_research/test_investigation.py --ignore=tests/test_tools/test_core_tools.py --ignore=tests/test_tools/test_image_generation_tool.py --ignore=tests/test_tools/test_mcp_auth_tool.py --tb=short` | 8 failed, 1176 passed in 295.13s (0:04:55) |
| `acceptance310` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q --ignore=tests/test_research/test_investigation.py --ignore=tests/test_tools/test_core_tools.py --ignore=tests/test_tools/test_image_generation_tool.py --ignore=tests/test_tools/test_mcp_auth_tool.py --tb=short` | 8 failed, 1176 passed in 331.31s (0:05:31) |
| `safety-provenance-final` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_research/test_engine.py tests/test_permissions tests/test_hooks --tb=short` | 108 passed in 5.78s |
| `core-latest` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_research/test_agent_core_integration.py -x --tb=short` | 1 failed in 18.30s |
| `available-collection` | `uv run pytest --collect-only -q --ignore=tests/test_research/test_investigation.py --ignore=tests/test_tools/test_core_tools.py --ignore=tests/test_tools/test_image_generation_tool.py --ignore=tests/test_tools/test_mcp_auth_tool.py` | 1185 tests collected in 1.04s |
| `traversal-final` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_research/test_agent_core_integration.py::test_e2e_06_project_switch_files_context_and_child_boundaries tests/test_harness --tb=short` | 39 passed in 18.29s |
| `retry-source-final` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_research/test_engine.py tests/test_api --tb=short` | 135 passed in 5.94s |
| `final-check311` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness tests/test_api tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox tests/test_web tests/test_services tests/test_research/test_engine.py tests/test_research/test_agent_core_integration.py --tb=short` | 472 passed in 120.90s (0:02:00) |
| `final-check310` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/harness-py310/bin/python -m pytest -q tests/test_harness tests/test_api tests/test_permissions tests/test_hooks tests/test_engine tests/test_sandbox tests/test_web tests/test_services tests/test_research/test_engine.py tests/test_research/test_agent_core_integration.py --tb=short` | 472 passed in 141.93s (0:02:21) |
| `contract-security-verified` | `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run pytest -q tests/test_harness --tb=short` | 41 passed in 8.40s |

其他构建/质量/环境验证命令：

| 命令 | 当次结果 / 日志 |
| --- | --- |
| `uv run ruff check src tests scripts` | baseline 6 错误；开发中 13 错误随后修复；末次通过。`baseline-ruff`, `current-ruff`, `lint-acceptance` |
| `uv run ruff check src/openharness/api src/openharness/services/tool_execution.py --fix` | 修复未使用导入；当时仍有一个重复 prepare_request，随后修复 |
| `uv run ruff check .` | 最后通过，`lint-final-verified` |
| `uv run ruff format --check src tests scripts` | 开发中存在格式问题；最后 342 文件全部符合格式，`format-final-verified` |
| `uv run ruff format <本轮修改文件>` | 分阶段格式化本轮实现；完整文件范围见 implementation-files |
| `uv run ruff format src tests scripts` | 49 文件格式化；前后 AST 相同；`format-complete` |
| `uv run python scripts/check_types.py` | 中间发现 80 个类型错误并修复；最后所有分组通过。历次日志 `types`, `types2`, `types3`, `types-final`, `types4`, `types5`, `types-acceptance`, `types-complete`, `types-verified`, `types-final-verified` |
| `git diff --check` | 最后通过，`diff-final-verified` |
| `npm run build`（frontend/web） | 基线和最后均通过，`baseline-web-build`, `web-build-final` |
| `OPENHARNESS_TEST_BROWSER=/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome npm run test:e2e` | 初次 17 个启动失败；安装后一次因钥匙环阻塞 4 失败 / 1 中断 / 12 未运行。`baseline-e2e`, `e2e` |
| `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring OPENHARNESS_TEST_BROWSER=/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome npm run test:e2e` | 两次完整执行均 17 passed。`e2e-final`, `e2e-verified` |
| `npx playwright install chromium` | Ubuntu 20.04 不受当前版本支持，失败，`browser-install` |
| `npx --yes playwright@1.48.2 install chromium` | 成功安装兼容测试浏览器，`browser-compatible-install` |
| `uv venv --python 3.10 /tmp/harness-py310` | 创建独立环境，未替换项目 .venv |
| `uv pip install --python /tmp/harness-py310/bin/python -e '.[dev,web,eval]'` | 初始非锁定环境，记录了 Pydantic 2.14 schema 漂移 |
| `UV_PROJECT_ENVIRONMENT=/tmp/harness-py310 uv sync --frozen --all-extras --dev --python 3.10` | 最终同步现有 uv.lock 成功，`py310-locked-setup` |
| `uv run pytest --collect-only -q tests/test_research/test_dispatch_subagents.py` | 仅用于定位运行位置，非测试通过统计 |

调试期间的 `uvx py-spy dump --pid ...` 被系统 ptrace 权限拒绝；没有提权。随后使用 pytest 的 `faulthandler_timeout=20` 获取到 Secret Service 等待堆栈，保存在 `/tmp/harness-web-diagnostic.log`。

## 下一阶段

优先统一旧工具移除与旧测试的产品边界；为实际外部写接口实现状态查询/幂等适配器，再按其真实语义放开业务重试；逐步为其余旧工具声明准确资源范围；如需低首字延迟，再引入明确的 attempt reset 协议替代当前暂存方式。保留未知 usage 和 uncertain 的保守处理。
