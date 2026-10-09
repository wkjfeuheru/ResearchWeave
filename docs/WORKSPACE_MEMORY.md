# 项目工作区与 Markdown 长期记忆

本次依据两份工作区设计文档，直接扩展现有 ResearchAgentRuntime。没有新建 Workspace 服务、专用工具、资产数据库、向量检索或 Agent Loop。

## 真实源码审查

| 实际组件 | 可复用能力 | 本次接入 |
| --- | --- | --- |
| ResearchAgentRuntime | 启动、恢复、规划提交、完成检查 | 项目目录绑定、初始化、记忆加载和上下文构造 |
| ResearchStore / Repository | state.json、锁、CAS、幂等收据、执行吊销 | 原项目记录增加可选 workspace_path；启动同一事务绑定；旧记录首次使用时补绑定 |
| QueryEngine / run_query | 动态 runtime context provider、手写工具循环 | 每次请求加载新 MEMORY.md，替换旧 Markdown 块 |
| ToolExecutionContext / 文件工具 | 显式 cwd、读取、字符串编辑、原权限检查 | 用 Runtime 校验项目边界，相对路径基于项目目录 |
| exclusive_file_lock / atomic_write_text | 进程互斥、原子替换 | 并发初始化、局部编辑、覆盖写入和中断时序保护 |
| SessionFiles / CompletionPolicy | 报告下载、不可变快照、确定性验收 | 新草稿置于 reports/，仍注册原下载清单；完成判定不读取 Markdown |

Planner/Replanner 保持 tools/research 下的 Agent as Tool，通过原 Tool Registry 调用。ResearchTask、ResearchPlan 和 CompletionPolicy 的职责不变。

## 配置与目录

现有设置文件的 research_memory 节增加：

```json
{
  "research_memory": {
    "workspace_root": "/server/research-workspaces",
    "memory_auto_inject_max_chars": 12000
  }
}
```

根目录未配置时为 `<ResearchStore.directory>/workspaces/`。目录名为 `project_<32 位摘要>`，摘要来自可信 Store 身份和已登记项目 ID。模型提供的展示 ID 即使包含路径符号，也不会直接拼入路径；不同会话的同名项目使用不同目录。

```text
workspaces/project_<key>/
├── MEMORY.md
├── artifacts/
├── reports/
└── subagents/        # 核心工具整合阶段新增，按派发和子任务隔离
```

ResearchProject.workspace_path 是服务端写入的绝对路径，不在模型工具输入 Schema 中。绑定保存在原 state.json。恢复优先使用已有绑定，即使默认根目录改变，也不搬迁项目。沿用仓库“一会话一个研究项目”的约束；Web 切换项目使用相应会话/Runtime。

新启动经过 Repository 的目标、用户来源和 revision 校验后，Runtime 在同一状态提交内初始化目录。初始化使用原锁和原子写入，不覆盖已有 MEMORY.md。重复 operation_id 返回原收据。旧 checkpoint 的 workspace_path 默认为 null，首次使用时补齐绑定，增加一次审计 revision，保留原 Task 状态和执行版本。

## 接口与调用链

ResearchAgentRuntime 增加以下方法：

- resolve_workspace(project_id) / initialize_workspace(project_id)：只解析当前持久化项目，校验绑定，幂等初始化。
- resolve_tool_path(project_id, candidate) / workspace_file_lock(...)：文件边界和原进程锁。
- load_workspace_memory(project_id, max_chars=None)：无缓存读取，严格 UTF-8，缺失、损坏明确报错。
- build_research_context(project_id)：有界、低信任的背景文本。
- start_project(...) / resume_project(...)：原生命周期工具的同步适配，异步 start/resume 继续可用。

启动链为 `research_project.start → Runtime.start_project → Repository.start → 目录初始化及 state.json 提交`；恢复链为 `research_project.resume → Runtime.resume_project → 原绑定/记忆校验 → Repository.resume`。

原 run_query 每轮调用动态 provider。MEMORY.md 放在 workspace_memory 背景块，关联项目和当前任务。上限内全文注入，超限只展示有界开头及明确的 truncated 标志，提示 read_file 的 offset/limit 按需读取。JSON 转义标签边界；记忆不能变成系统规则、权限或完成声明。没有 LLM 自动摘要和缓存。

循环复制消息并清除旧 workspace_memory 块，再加入当前快照；其他任务和工具历史保留。provider 请求使用独立消息列表，后续刷新不会改写旧请求。恢复消息中的其他项目 Markdown 块也被清除。

## 现有工具与一致性

read_file、write_file、edit_file、glob、grep 通过现有 ToolExecutionContext 获得项目路径。允许工作区内绝对路径，拒绝越界绝对路径、`..`、软链接和已有多重硬链接文件。搜索的 Python 回退在读取前过滤不安全路径；ripgrep 不跟随目录链接，glob 返回结果也经过检查。原权限检查基于同一有效 cwd。普通会话保持原 cwd 语义。Notebook 和图片生成底层工具保留同一边界，但已从默认研究注册表移除；显式 general 注册表仍可使用。

局部编辑在路径锁内重读最新文件并替换，保留并发的其他修改；编辑审批期间内容变化会返回冲突。覆盖已有项目文件的 write_file 需要 expected_sha256，read_file 在正文和 metadata 提供整文件哈希，冲突要求重读。新建文件无需哈希，普通会话不增加该要求。文件采用原子替换，读取只能看到完整旧版或新版。

写入临界区同时持有原状态锁，检查项目活跃状态和执行 epoch/receipt，防止审批等待后已经吊销的写入落盘。read_file 读取 MEMORY.md 时标记来源为 fragment，不能当作核验后的原始披露。Main Agent 专用提示词约定适时维护带来源/状态的发现、假设、相对资产路径、缺口和决策。

图片读取/生成也检查路径；图片落盘复用原子写入。bash 绑定项目 cwd，并检查 cwd 参数。冲突调查继续用原独立 staging 状态，但其文件工具保留父项目目录边界。

核心工具整合后，通用子代理复用原 run_query，使用 `subagents/<dispatch_id>/<task_id>/` 作为文件 cwd。允许读取主 MEMORY.md 与工作区资料，只能写自己的目录；禁止读取其他子代理目录。搜索在返回结果和读取内容前过滤这些路径。写入锁同时检查父项目 epoch、目标与计划 revision，审批等待期间主项目被中断或改目标后，旧写入被拒绝。报告模式的冲突调查也移除 Shell/动态 MCP 执行入口，保留原 staging 导入与主代理审查流程。详见 [Agent 核心工具体系](AGENT_CORE_TOOLS.md)。

新 report_draft 在 reports/ 生成 Markdown，继续使用 SessionFiles 下载 ID 和不可变快照。历史报告保持原下载地址。Agent 自主创建的 artifacts/ 文件仍需既有 submit_artifact 才成为可验收的结构化产物。

## 测试、限制与后续

tests/test_research/test_workspace_memory.py 覆盖初始化、同名项目隔离、恶意 ID、原文件工具、哈希冲突、并发编辑、审批竞争、路径和链接、记忆异常、长度截断、动态刷新、恢复、执行吊销、普通模式及旧 checkpoint。离线财报端到端测试继续验证 Planner、Task、CompletionPolicy 和报告下载，并检查 reports/ 路径。实际运行结果见下方验证记录。

本阶段提供应用内文件工具的基础隔离。bash/任意 Python、MCP 或第三方工具仍可能访问宿主绝对路径、直接写文件或修改 state.json；cwd 不构成 OS 沙箱。既有 Docker/本地沙箱配置保持原样，本阶段未完成容器工作区映射。绕过文件工具的外部编辑不参与锁/CAS。静态路径检查不能替代抵御恶意宿主进程竞争替换目录的系统级保护。

后续 Harness 工作包括执行工具的 OS 文件系统边界、容器映射、外部编辑版本协调、跨进程异步等待优化，以及项目归档/移动/删除时的清理。Store 身份参与目录 key，移动整个状态根目录需要显式迁移绑定。记忆内容质量仍需 Agent 和人工审阅。本阶段未新增复杂 Token 预算、权限矩阵、数据库、向量检索或总结流水线。

## 本阶段实际修改文件

以下清单仅记录工作区与 Markdown 阶段，保留上一阶段的研报 Runtime 实现：

| 文件 | 改动 |
| --- | --- |
| src/openharness/research/runtime.py | 工作区/记忆接口、恢复、工具守卫 |
| src/openharness/research/models.py | 可选 workspace_path |
| src/openharness/research/repository.py | 启动初始化回调、reports/ 草稿路径 |
| src/openharness/research/prompt.py | 主 Agent 记忆维护约定 |
| src/openharness/config/settings.py | 根目录和注入字符上限 |
| src/openharness/runtime.py | 服务端配置传入 Runtime |
| src/openharness/engine/query_engine.py | 动态记忆加载、复用已有 Runtime |
| src/openharness/engine/query.py | 显式工具 cwd、旧块替换、请求快照复制、异常报告 |
| src/openharness/tools/base.py | 原工具上下文路径/锁方法、旧 cwd 协议兼容 |
| src/openharness/tools/research/project.py | start/resume 走 Runtime 工作区入口 |
| src/openharness/tools/file_read_tool.py | 路径校验、严格编码、整文件哈希、记忆低信任来源 |
| src/openharness/tools/file_write_tool.py | 哈希覆盖保护、锁、原子替换 |
| src/openharness/tools/file_edit_tool.py | 锁内最新版本编辑、审批竞争检查 |
| src/openharness/tools/glob_tool.py | 模式/根目录检查及结果过滤 |
| src/openharness/tools/grep_tool.py | 根目录检查、回退读取边界、旧上下文兼容 |
| src/openharness/tools/notebook_edit_tool.py | 同一边界、锁及原子写入 |
| src/openharness/tools/bash_tool.py | 项目 cwd 绑定和覆盖参数检查 |
| src/openharness/tools/image_to_text_tool.py | 图片路径边界与错误结果 |
| src/openharness/tools/image_generation_tool.py | 输入/输出路径绑定、原子落盘 |
| src/openharness/tools/investigate_conflict_tool.py | 保留 staging，同时传递父工作区边界 |
| tests/test_research/test_workspace_memory.py | 42 项工作区与兼容性测试 |
| tests/test_research/test_earnings_workflow.py | 验证报告项目的实际工作区和草稿路径 |
| README.md | 使用说明入口 |
| docs/WORKSPACE_MEMORY.md | 源码审查、接口、配置、交付及限制 |

## 实际验证记录（2026-10-08）

- `uv run pytest -q tests/test_research/test_workspace_memory.py tests/test_research/test_earnings_workflow.py --tb=short`：43 passed。
- `uv run pytest -q --tb=short`：965 passed，1 skipped，90.40 秒；覆盖现有工具调用、Planner/Replanner、财报交付、引擎、Web 和普通会话。
- `uv run ruff check src tests scripts`：All checks passed。
- `git diff --check`：通过。

跳过项是既有本地沙箱真实执行测试，环境缺少 srt；并非工作区用例。全部研究测试使用 Mock/离线资料，没有调用真实商业金融 API。本阶段未修改前端，不重复运行上一阶段已通过的前端构建和浏览器测试。
