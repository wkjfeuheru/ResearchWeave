# Agent 核心架构集成测试与剩余问题修复报告

验证日期：2026-10-09（Asia/Shanghai）。对象为当前 OpenHarness / ResearchWeave 工作树。继前一轮 E2E-01–16 后，本轮落实已批准的强制报告隔离、全生产 Python strict 类型检查与子代理进程崩溃恢复方案。

## 结论与适用范围

原手写 Agent Loop、QueryEngine/run_query、Runtime、Tool Registry、Research Store 与 CompletionPolicy 保留。报告 Shell/Python 经过真实 SRT 0.0.79 或 Docker，后端不可用时拒绝宿主执行；普通会话保持原配置。独立派发进程 SIGKILL 后保留成功/部分文件，恢复幂等且不误吊销活跃任务；只能由 Main Agent 显式重试失败项，不自动采纳候选或重放执行。

确定性研究旅程经过 `build_runtime → QueryEngine → run_query → Registry → ResearchAgentRuntime/Repository`，项目/计划/任务/证据/文件/政策均为真实实现。Fake LLM 与 Mock Web 只替换外部响应，不把完成政策或计划提交 Mock 成恒真。全部研究测试使用临时目录，不调用商业账号、不修改真实研究资料。

当前全部 196 个生产 Python 文件均在 strict 下检查：192 个 src 文件、2 个外层 evals 脚本、构建 hook 与检查入口。8 组当前 bundled 资源通过临时规范包视图检查，原路径未改变。没有关闭 strict、全局忽略或批量 Any/ignore。已在 Python 3.10/3.11 进行检查和实际回归。

功能验收与供应链审计分开记录：固定 SRT 的 node-forge 依赖仍有上游 high 公告，npm audit 为 FAIL；不宣称完整宿主、内核、插件/MCP 隔离或真实模型研究质量。详见[风险清单](agent-core-risks.md)。

## 第一阶段：真实架构检查

| 组件 | 已实现及真实源码 | 调用关系与权威职责 |
| --- | --- | --- |
| 主模型循环 | `src/openharness/engine/query_engine.py`、`engine/query.py` | Web/共享 Runtime 构造 QueryEngine；submit_message/continue_pending 调用唯一 `run_query` 实现。Runtime 不另开主模型循环。 |
| ResearchAgentRuntime | `src/openharness/research/runtime.py` | QueryEngine 持有并传入 QueryContext；主工具派发前 guard、规划工具返回后 commit、模型结束时 evaluate_stop/finalize。协调原 Repository 与 CompletionPolicy。 |
| ResearchTask / ResearchPlan | `src/openharness/research/models.py` | 保存在原 Store state.json；任务包含 revision、依赖版本、lease、criterion_results、artifact_ids，计划保留历史版本。没有第二套权威任务数据库。 |
| TaskManager / PlanPatchValidator | `src/openharness/research/tasks.py` | Repository 在计划提交/补丁/领取/完成后调用；刷新 READY 和依赖钉住版本，拒绝非法状态转移、循环依赖及旧补丁。 |
| Planner Agent as Tool | `src/openharness/tools/research/planner.py`、`planning.py` | Registry 名称是 `planner`，Python 函数为 `planner_tool`。模型只提交 PlanProposal；原循环预留预算，Runtime 校验并交给 Repository 提交。 |
| Replanner Agent as Tool | `src/openharness/tools/research/replanner.py`、`planning.py` | Registry 名称是 `replanner`，Python 函数为 `replanner_tool`。使用当前目标/计划/反馈，返回 PlanPatch；Runtime/Repository 校验版本、DAG 和影响范围后保存新计划。 |
| 项目与任务工具 | `src/openharness/tools/research/project.py` | `research_project.start` 绑定项目；claim_task 经 Repository/TaskManager 获取租约；submit_artifact 绑定真实执行；complete_task/finalize 运行完成策略。模型不能直接转移到 validating/completed。 |
| CompletionPolicy | `src/openharness/research/completion.py` | Repository 在真正的 VALIDATING 状态调用。检查任务依赖、标准、产物、执行与证据；项目检查全部任务、目标覆盖、草稿、引用、冲突及实际交付文件。Markdown 声明不参与判定。 |
| 通用派发 | `src/openharness/tools/dispatch_subagents_tool.py` | Main Agent 调用 `dispatch_subagents`；单/多任务共用同一主 Query 的限流器，结果按委托 ID/输入顺序返回，父级执行收据由原派发器保存。 |
| 子代理执行器 | `src/openharness/engine/subagents.py` | 通用派发与 investigate_conflict 共用 bounded client、原 run_query、超时、用量及取消/转录逻辑；每个 child 使用独立 QueryContext、历史与受限 Registry。子循环不是第二套顶层 Runtime。 |
| 冲突调查 | `src/openharness/tools/investigate_conflict_tool.py`、`research/conflicts.py` | 旧入口保留 staging 与结构化裁决报告，主 Agent 审查后提交。通用派发可以整理带现存证据的冲突候选，不能代替正式裁决接口。 |
| Research Memory / Store | `src/openharness/tools/research_memory_tool.py`、`research/store.py` | 原锁、CAS、幂等收据、不可变来源快照、证据/论证/结论/引用/冲突。报告项目拒绝旧 set_context/create_plan/update_task；兼容会话保留。证据变更引发的吊销与反馈/停止使用同一 Repository 控制函数。 |
| Workspace | Runtime、`tools/base.py`、现有文件工具 | 项目持久绑定独立目录；路径以 Store 身份与 project_id 摘要命名，避免模型 ID 成为路径。工具相对 cwd 由绑定决定；各 child 只能写自己的 subagents 子目录。没有 WorkspaceService/专用工具。 |
| MEMORY.md | Runtime.build_research_context、QueryEngine._current_runtime_context、run_query | 每次模型请求前读最新 UTF-8 文件，清除旧 workspace_memory 块再注入低信任快照；已有文件工具自主维护，内容不修改权威任务状态。 |
| Registry / Search / 权限 | `tools/__init__.py`、`runtime.py`、`tools/tool_search_tool.py`、`permissions/checker.py` | 真实注册与 API Schema/发现一致；先 Runtime admission，再 Hooks/权限，再实际执行/结果提交。权限与状态约束分别复用。 |
| 中断与恢复 | QueryEngine、Repository.recover/resume、Store.interrupt/stopped、`web/app.py` | Web 初始化时恢复遗留执行/任务租约；取消收束 children、修补消息并持久化暂停；反馈先保存再重规划。恢复读取原绑定和最新 MEMORY；dispatch_audit 生命周期锁识别活跃批次并恢复已落盘候选，需显式继续/重试，不自动重放旧执行。 |

检查未发现第二套顶层 Agent Runtime、替换 Provider、重复 Registry 或独立 Workspace 管理器。Runtime、QueryEngine 与 Store 的适配方法有不同入口，但使用同一 state.json、锁及 Repository 控制语义。规划助手保留专用结构化模型提交协议，研究子代理共用原工具循环；没有强行统一这两种契约。

## 第二阶段：测试基础设施与分层

新增 `tests/test_research/agent_core_support.py`：

- `Lab`：每例创建临时配置、数据、Research Store 和 Workspace；调用真实 build_runtime，MCP 连接关闭，保留 MCP manager 与资源工具。HTTP 请求在测试 fixture 中被硬性拒绝。
- `OfflineFetch`：仅替换现有 web_fetch 的网络 I/O，沿用输入契约及生产来源 metadata，提供虚构公司/竞争/财务/海外披露。没有新增业务专用 Agent Tool。
- `AlphaModel`：识别主 Agent、Planner、Replanner 和 child 的真实 Schema；所有权威写入均通过 Registry 调用。记录请求、用量、执行峰值和取消；同步屏障验证并发，Event 精确控制反馈、迟到和中断时机。
- `ScriptModel`：让主循环执行少量对抗性工具调用；用于权限、幂等、虚假 Markdown 声明等组件集成。提前停止脚本不会用于宣称整个项目交付成功。

| 层次 | 实际测试 | 证明范围 |
| --- | --- | --- |
| 组件契约 | 原 `test_planning_tools.py`、`test_dispatch_subagents.py`、`test_memory_compatibility.py`、`test_report_runtime.py` | 严格规划输入/输出、提案只读、递归拒绝、候选 ID/路径、Memory 迁移、政策缺失项与版本约束。 |
| 组件集成 | 新 `test_agent_core_integration.py` | 通过真实主模型派发或可信宿主接口制造版本、文件、完成状态与恢复竞争；不把直接函数调用包装成完整 E2E。 |
| 完整研究链 | 新 `test_agent_core_e2e.py` | 从用户输入，经项目启动、规划、并行研究、主级核查、文件/记忆、产物/验收到下载；包含反馈重规划及中断重载。 |

`test_full_journey_parallel_memory_replan_interrupt_restore_and_deliver` 将并行研究、长期记忆、增量重规划、领取海外任务后取消、从磁盘重载、保留已完成研究、最终报告与政策验收放在同一条用户旅程内。恢复后的模型没有重新执行已完成的主营/竞争/财务核查或再次规划。

## 第三阶段：场景结果矩阵

路径前缀均为 `tests/test_research/`。PASS 表示自动化断言通过；缺陷修复前的失败另列在下节，不将跳过项计入 PASS。

| 测试编号 | 场景 | 状态 | 测试文件 / 可检索测试名 | 备注 |
| --- | --- | --- | --- | --- |
| E2E-01 | 正常研究闭环 | PASS | `test_agent_core_e2e.py::test_e2e_01_complete_research_and_13_delivery` | 用户请求启动项目，真实 Planner 提交 DAG、领取、证据核查、产物、完成及下载；工具 ID 无重复、用量无重复计入。 |
| E2E-02 | 并行子 Agent | PASS | `test_agent_core_e2e.py::test_e2e_02_parallel_candidates_context_and_authority` | 三个实际并发、独立历史和目录、正确目标、主记忆写入与权威任务变更被拒绝；主 Agent 整合候选。 |
| E2E-03 | 并行失败隔离 | PASS | `test_agent_core_e2e.py::test_e2e_03_failed_child_preserves_siblings_and_main_continues` | B 异常，A/C 成功文件和摘要保留；主级补查原文后完成。 |
| E2E-04 | 并发上限 | PASS | `test_agent_core_e2e.py::test_e2e_04_observable_concurrency_limit_and_queue` | 五项委托，观测峰值恰为 2；等待任务全部启动并归集。 |
| E2E-05 | 长期记忆更新 | PASS | `test_agent_core_e2e.py::test_e2e_05_fresh_memory_and_07_structured_provenance` | 真实 edit_file 后下一请求含新发现/索引；只有一个 Markdown 块，旧请求保留原快照。 |
| E2E-06 | 工作区隔离 | PASS | `test_agent_core_integration.py::test_e2e_06_project_switch_files_context_and_child_boundaries` | 两个实际项目、跨项目文件/child 读取与遍历拒绝，B 记忆不进入 A 新上下文；应用层及新增真实 OS 沙箱验收共同覆盖；见隔离专项矩阵。 |
| E2E-07 | 结构化证据与 Markdown | PASS | `test_agent_core_e2e.py::test_e2e_05_fresh_memory_and_07_structured_provenance`；`test_agent_core_integration.py::test_e2e_14_markdown_completion_claim_cannot_change_authority` | Evidence → Source/公开论证 → Conclusion 可追溯；Markdown 引用 ID，编辑不改变原证据，不能替代核验。 |
| E2E-08 | 用户修改方向 | PASS | `test_agent_core_e2e.py::test_e2e_08_user_feedback_replanner_preserves_completed_task` | 最新目标/反馈进入 Replanner；新计划 v2、草稿 revision2/依赖更新，已完成研究产物保留且不重跑。 |
| E2E-09 | 过期 PlanPatch | PASS | `test_agent_core_integration.py::test_e2e_09_stale_patch_rejected_then_main_replans` | 当前计划 v2 时提交基于旧 v1 的补丁，被明确拒绝且 v2 未覆盖；主 Agent resume 并重提有效 v3 补丁。 |
| E2E-10 | 旧 child 延迟返回 | PASS | `test_agent_core_integration.py::test_e2e_10_late_children_cannot_pollute_revised_plan` | 同一主上下文中宿主规划调用与迟到 provider 竞争；旧执行 rejected、未注册来源、候选 failed/版本可审计，v2 正常完成。 |
| E2E-11 | 拒绝提前完成 | PASS | `test_agent_core_integration.py::test_e2e_11_rejects_premature_task_then_main_repairs` | 缺产物时返回 missing/recommended actions；主 Agent 补齐后通过。Spy 观察到真实 VALIDATING，未改变政策结果。 |
| E2E-12 | 任务完成 | PASS | `test_agent_core_integration.py::test_e2e_12_completion_idempotency_and_dependency_readiness` | 真实政策通过后 completed，下游 ready；同 operation_id 重放返回原通过收据，新请求重复完成明确拒绝，状态不损坏。 |
| E2E-13 | 项目完成与交付完整性 | PASS | `test_agent_core_e2e.py::test_e2e_01_complete_research_and_13_delivery`；`test_e2e_13_rejects_missing_or_corrupt_workspace_delivery` | 正常完成；报告缺失/篡改/链接、下载副本缺失/篡改五种情况均拒绝 finalize。 |
| E2E-14 | Markdown 与实际状态冲突 | PASS | `test_agent_core_integration.py::test_e2e_14_markdown_completion_claim_cannot_change_authority` | Markdown 宣称全部完成，draft 仍 in_progress；政策拒绝，权威证据/状态保留。 |
| E2E-15 | 运行中断 | PASS | `test_agent_core_e2e.py::test_e2e_15_interrupt_and_16_restore_without_repeating_valid_work`；`test_e2e_15_continuation_cancellation_settles_children_and_state` | 新消息与续跑取消都持久化 suspended；children 收到取消并收束，已提交产物与完整 MEMORY 保留，消息有明确中断收据。 |
| E2E-16 | 恢复执行 | PASS | 上述恢复测试；`test_full_journey_parallel_memory_replan_interrupt_restore_and_deliver`；`test_agent_core_integration.py::test_e2e_16_process_recovery_revokes_lease_between_tool_executions` | 磁盘重载计划/任务/记忆；主级已完成工作不重复；无 running execution 的遗留 lease 也能恢复且 recovery 幂等。 |

补充回归：`test_tool_registry_discovery_and_execution_compatibility` 验证实际 Schema、发现和调用；五个精简工具不在研究表，MCP 资源接口可执行。`test_generic_conflict_candidates_bind_existing_evidence` 验证通用派发整理冲突候选、引用已登记 Evidence，并保留主结论/冲突/任务控制。原冲突调查完整裁决回归继续使用 `test_memory_compatibility.py`、`test_investigation.py`、`test_conflicts.py`。

最终核心基础工具仍为：bash/read_file/write_file/edit_file/glob/grep；web_search/web_fetch/image_to_text；research_memory/investigate_conflict；planner/replanner/research_project/dispatch_subagents；skill/tool_search/ask_user_question；MCP manager 存在时 list_mcp_resources/read_mcp_resource，以及已配置动态 MCP/插件。Python 函数 planner_tool/replanner_tool 的注册名是 planner/replanner，没有新增重复别名。

## 本轮问题与根因修复

| 问题 | 根因及修复 | 主要文件 | 验证 |
| --- | --- | --- | --- |
| 报告 Python 绝对路径可越界 | cwd 不能限制任意代码；从受信配置及持久化绑定派生强制隔离，屏蔽宿主环境，禁止回退 | sandbox/policy.py、utils/shell.py、tools/bash_tool.py、runtime.py、tools/base.py | `test_actual_python_path_and_write_isolation` 两后端、主循环 Shell 兼容测试，PASS |
| SRT 默认读取范围宽；工作区 allowRead 覆盖可写 mount | 显式 denyRead 根、只允许运行依赖/项目；三个输出目录使用可写绑定，MEMORY 只读 | sandbox/policy.py、sandbox/adapter.py | 真实退出码 0/1/7、读写隔离、并发两项目，PASS |
| 宿主硬链接可通过项目可写 bind 改写外部 inode | 两后端都能复现预存 hardlink 修改宿主文件；执行前拒绝多链接文件，与现有文件工具规则一致 | sandbox/policy.py、tools/bash_tool.py | `test_report_rejects_preexisting_cross_project_hardlinks` 修复前 2 FAIL、修复后 PASS |
| Docker 全局活动会话与取消归属 | 使用所属会话字典、context-local 兼容选择和 ExecutionOwner；报告只挂当前工作区，超时/取消强制移除所属容器 | sandbox/session.py、docker_backend.py、runtime.py、tools/research/project.py | 并发项目、keeper 容器不被另一执行取消/超时移除，PASS |
| Shell 输出管道及后代进程收束 | 连续 drain 输出而非先等退出；独立进程组收束，报告不读取宿主登录 profile | utils/shell.py、tools/bash_tool.py | 原超时/输出回归和真实后代取消，PASS |
| 隔离脚本直接操作 Store/下载 manifest | 清理 Store 环境；脚本只声明文件；宿主停所属容器后验证路径/执行租约再用原 SessionFiles 注册 | utils/research_exports.py、tools/bash_tool.py | 真实两后端 md/json/docx/xlsx 导出、拒绝链接与 revoked lease，PASS |
| 子代理硬崩溃 running 审计无人恢复 | 从首检查点前持生命周期锁；恢复合并逐子任务 durable result，原子保存 interrupted，保留候选和部分文件 | research/dispatch_audit.py、repository.py、dispatch_subagents_tool.py | 实际 SIGKILL、活跃批次、幂等、旧审计及未知版本保护，PASS |
| 无显式失败项重试入口 | 增加 retry_dispatch_id，按原任务规格/父任务 revision/目标计划版本验证，只产生新批次 | tools/dispatch_subagents_tool.py、tools/research/project.py | 不重跑成功项、拒绝已完成项/改变方向，PASS |
| 严格类型失败与同名 scripts 冲突 | 修正流式 Provider 协议、工具泛型、执行 metadata/Repository 模型、Web DTO、可选 SDK 类型、全部资源脚本；临时包视图覆盖实际生产路径 | api/、engine/、tools/、research/、web/、evaluation/、plugins/、skills/、utils/；scripts/check_types.py | 原复核 1140 errors/103 files → 全部生产文件零错误，PASS |
| Hook 将 retry 事件视为 text 事件 | 仅处理 ApiTextDeltaEvent 文本，不读取 ApiRetryEvent.text | hooks/executor.py | `test_prompt_hook_ignores_retry_notifications`，PASS |
| Python 3.10 无 asyncio.timeout 且 TimeoutError 类型不同 | 复用小型 timeout 兼容函数，3.10 使用有类型的 async-timeout；catch asyncio.TimeoutError | utils/async_timeout.py、mcp/client.py、utils/tavily_search.py、web/app.py、pyproject.toml | 双版本 actual MCP/Web/全量回归及 strict，PASS |
| 安装版 forecast 直接执行相对导入失败 | 在相对 import 前初始化原 script_package | bundled/analysis-modeling/.../forecast.py | 独立目录 CLI 回归、干净 wheel smoke，PASS |
| 测试仍期待 Docker bridge；浏览器在 Runtime 初始化前检查 UI | 按原禁网约束断言 route table；等待 session 创建/首流事件及最终答案，不依赖 click 后的短暂 busy 状态 | scripts/test_docker_sandbox_e2e.py、frontend/web/e2e/workspace.spec.ts | Docker 19 项、浏览器 17 项，PASS |
| 外层评测生成脚本未覆盖 | 纳入 evals 包根；用明确配方/合成数据/枚举类型及不同阶段变量，不改变冻结数据 | evals/research_v1/build_dataset.py、recipes.py、scripts/check_types.py | 两脚本 strict；80 合成配方与 CLI/数据集 5 项回归，PASS |

前轮已修复的交付文件完整性、continue_pending 取消持久化、遗留任务租约恢复继续由 E2E-13/15/16 覆盖。修复前失败与最终原始输出见 [本轮验证证据](agent-core-remediation-evidence.txt)，前轮失败记录保留在 agent-core-failure-evidence.txt。

## 新增真实专项验收

| 编号 | 测试入口 | 状态 | 断言边界 |
| --- | --- | --- | --- |
| ISO-01 | test_report_sandbox.py::test_actual_srt_exit_codes | PASS | 真实 SRT 退出码保留 |
| ISO-02 | test_actual_python_path_and_write_isolation[srt/docker] | PASS | Shell 内 Python 的绝对路径、遍历、符号链接；项目可写，其他项目/Store/凭证不可读，Store/MEMORY 不可写 |
| ISO-03 | test_actual_backends_own_concurrent_project_mounts[srt/docker] | PASS | 两项目同时运行，各自 cwd/记忆/输出，互不读取 |
| ISO-04 | test_report_backend_missing_fails_closed、test_nonreport_host_compatibility | PASS | 缺 SRT 故障注入时报告拒绝；非报告原宿主行为仍可用 |
| ISO-05 | test_report_timeout_and_cancel_remove_descendants、test_actual_docker_cancel_removes_only_owned_execution | PASS | 真实超时、取消、后代文件无迟到写入；其他所属容器仍运行 |
| ISO-06 | test_actual_isolated_exports_registered_only_by_host[srt/docker]、test_export_registration_rejects_revoked_lease_and_link | PASS | 脚本无宿主 manifest 写入；停命令后 host 验证注册，过期/链接拒绝；不自动提交研究产物 |
| ISO-07 | test_report_rejects_preexisting_cross_project_hardlinks[srt/docker] | PASS | 含外部 inode 的预存多链接文件拒绝执行 |
| REC-01 | test_killed_process_preserves_candidates_and_explicit_retry | PASS | 独立进程实际 SIGKILL；完成和部分文件保留、中断、重复恢复幂等、显式失败项重试 |
| REC-02 | test_recovery_does_not_revoke_live_dispatch、test_live_owner_before_initial_checkpoint_is_not_recovered | PASS | 首 checkpoint 前及运行中活跃锁不被恢复吊销 |
| REC-03 | test_retry_rejects_completed_and_changed_direction | PASS | 不重跑成功项，方向改变要求新派发 |
| REC-04 | test_legacy_audit_is_recovered_and_visible_through_project_tool、test_unknown_audit_schema_is_not_rewritten | PASS | 原格式、真实 project.read 摘要；未知版本拒绝覆盖 |

专项文件位于 `tests/test_research/`。真实后端验收缺依赖失败，不使用 skip 判 PASS。

## 本地执行与覆盖率

| 检查 | 状态 | 实际结果 / 证据 |
| --- | --- | --- |
| 全量 Python 3.11 | PASS | 1107 passed，211.95s；包括本轮全部新增回归 |
| 全量 Python 3.10 | PASS | 独立临时 venv；1107 passed，242.73s；包括本轮全部新增回归 |
| 核心分支覆盖率 | PASS | 640 passed；pytest-cov 真实测量 research/主循环/工具/子代理/沙箱 |
| E2E-01–16 | PASS | 上述矩阵均在真实主循环测试中断言；包含完整反馈—重规划—中断—恢复—交付旅程 |
| SRT/Docker 真实报告隔离与导出 | PASS | 14 passed，不依赖 LLM Key；Linux Node24.21.0、SRT0.0.79、Docker26.1.3、bubblewrap/socat |
| 派发崩溃恢复专项 | PASS | 6 passed，包括真正独立进程 SIGKILL |
| 原 Docker E2E | PASS | OPENHARNESS_REQUIRE_SANDBOX=1；19 passed，101.30s |
| 全生产 strict | PASS | Python3.11/3.10 全部 196 文件零错误；统一入口覆盖源码及生产脚本 |
| Ruff / 生产格式 / diff 空白 | PASS | Ruff 全部检查通过；生产格式通过；git diff --check 通过 |
| Web build / 浏览器 | PASS | TypeScript/Vite build 通过；17 passed（2.0m），使用本机已有 Chromium1140；未使用自动 retry |
| Wheel | PASS | uv build 成功；干净 Python3.12 环境导入158模块，Web/CLI/Shell/Store及安装脚本 smoke 通过 |
| npm audit | FAIL | 固定 SDK 的 node-forge 上游公告，2 high；没有把此项写成安全审计通过 |
| GitHub Actions / 非 Linux / 真实外部账号 | NOT RUN | 已配置 CI，未在远端执行；均未计入 PASS |

原始覆盖率保存于 [agent-core-remediation-coverage.json](agent-core-remediation-coverage.json)。范围含 4523 语句、1578 分支，实际语句 4096/4523（90.56%）、分支 1234/1578（78.20%），综合 87.36%。本次是明确目录集合中的 640 个核心/集成用例测量，不是前轮 88.65% 数据；不能直接比较不同分母。前轮原始 agent-core-coverage.json 保留。


| 模块 | 已执行语句 / 总语句 | 已执行分支 / 总分支 | 综合覆盖率 |
| --- | --- | --- | --- |
| ResearchAgentRuntime | 203/220 | 65/80 | 89.33% |
| ResearchRepository | 384/429 | 117/164 | 84.49% |
| CompletionPolicy | 121/125 | 17/18 | 96.50% |
| 派发恢复审计 | 104/126 | 33/44 | 80.59% |
| 报告隔离策略 | 47/47 | 7/8 | 98.18% |
| run_query / 工具派发 | 467/528 | 173/218 | 85.79% |
| QueryEngine | 193/208 | 49/56 | 91.67% |
| 通用派发 | 231/263 | 62/84 | 84.44% |
| Shell / 宿主导出 | 155/189 | 42/62 | 78.49% |

未覆盖的 Windows/平台故障、部分审计 I/O、稀少 Provider/错误分支未据此宣称通过。不为数字编写镜像测试或设置虚假 100% 阈值。

## 修改文件、核心接口与兼容性

本轮主要新增 `sandbox/policy.py`、`engine/metadata.py`、`research/dispatch_audit.py`、`utils/async_timeout.py`、`web/types.py`、`scripts/check_types.py`、`tools/sandbox/package.json/package-lock.json` 及隔离/恢复/外层生成脚本回归。关键修改为 runtime.py、tools/base.py/bash_tool.py/dispatch_subagents_tool.py/research/project.py、sandbox/adapter.py/session.py/docker_backend.py/Dockerfile/docker_image.py、research/repository.py/models.py、utils/shell.py/research_exports.py、pyproject.toml、hatch_build.py 与 CI。

严格修复还修改 api/provider 和各流式实现、engine/query/query_engine/observer、hooks/executor、MCP、Research Store、Web/CLI/evaluation、配置/权限、插件/Skills、utils 及当前 bundled 脚本的协议与标注。完整生产文件快照见 [文件清单](agent-core-remediation-files.txt)；工作树已有的架构功能、分析/报告包迁移、资源删除等保留，不把它们当成本轮新业务开发。相关测试修改都对应实际接口或行为回归。

核心接口：

- `report_settings(Settings, workspace)`、`report_environment(workspace)`、`ExecutionOwner`：宿主派生强制策略与归属，不暴露给模型。
- `ToolExecutionContext(settings=..., runtime_id=...)`、`create_shell_subprocess(owner=...)`：可选向后兼容扩展，保留原位置参数顺序。
- `recover_dispatches(store_directory, project_id, workspace)`：非阻塞生命周期锁、逐任务 durable 合并、原子/幂等恢复。
- `dispatch_subagents(tasks, context, retry_dispatch_id=None)`：旧调用有效；显式失败项重试创建新批次。
- `research_project.read.dispatches`：新增可审查摘要，不改变原权威 task/plan/evidence 记录。

默认研究 Registry 仍为 18 个：ask_user_question、bash、edit_file、read_file、write_file、glob、grep、image_to_text、research_memory、investigate_conflict、planner、replanner、research_project、dispatch_subagents、skill、tool_search、web_fetch、web_search；运行环境按原 manager 添加必要 MCP 工具。Planner/Replanner 的 Registry 名称为 planner/replanner，Python 入口保持 planner_tool/replanner_tool。notebook_edit/config/mcp_auth/image_generation/sleep 仍不进入研究默认 Registry/Search，底层服务与一般会话兼容保留。

行为变化：报告 Shell 禁止宿主执行、Store 不直接暴露给脚本、主 MEMORY 只能用文件工具维护；旧工作区不搬迁。规划提案仍由 Runtime 校验提交，任务/项目完成仍由 CompletionPolicy 决定。没有增加顶层 Runtime、Workspace 服务、Artifact Registry、框架或专用业务工具。

## 复现命令

安装/镜像/导出兼容说明见 [SANDBOX_EXECUTION.md](../SANDBOX_EXECUTION.md)。

```bash
uv sync --extra dev --extra web --extra eval
uv run python scripts/check_types.py
uv run python scripts/check_types.py --python-version 3.10
uv run pytest -q
OPENHARNESS_REQUIRE_SANDBOX=1 uv run pytest -q tests/test_research/test_report_sandbox.py tests/test_research/test_dispatch_recovery.py scripts/test_docker_sandbox_e2e.py
uv run ruff check src tests scripts evals hatch_build.py
uv run ruff format --check src evals scripts/check_types.py hatch_build.py

# 核心目录集合覆盖率；全部使用测试临时目录
COVERAGE_FILE=/tmp/openharness-remediation.coverage uv run pytest -q \
  tests/test_research tests/test_engine tests/test_sandbox tests/test_tools tests/test_hooks tests/test_entrypoints tests/test_web \
  --cov=openharness.research --cov=openharness.engine.query --cov=openharness.engine.query_engine \
  --cov=openharness.engine.subagents --cov=openharness.tools.research --cov=openharness.tools.dispatch_subagents_tool \
  --cov=openharness.tools.base --cov=openharness.tools.research_memory_tool --cov=openharness.tools.bash_tool \
  --cov=openharness.sandbox --cov-branch --cov-report=json:docs/testing/agent-core-remediation-coverage.json

cd frontend/web
npm run build
npx playwright install chromium   # 或用 OPENHARNESS_TEST_BROWSER 指定已有浏览器
npm run test:e2e
```
