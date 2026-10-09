# OpenHarness 投研工作台

OpenHarness 是本地运行的投研 Web 产品。每个对话独立保存任务上下文、研究状态、证据池和可审计的论证摘要；资料保存时间戳、不可变快照及稳定 ID，历史回答保留当时引用的版本。

[English](README.md) · [Web 工作区](docs/web-workspace.md) · [清理与验收报告](docs/research-product-cleanup.md)

## 安装与启动

需要 Python 3.10 或更新版本：

```bash
pip install 'openharness-ai[web]'
oh setup
oh web
```

打开启动命令显示的本地网址。服务检查本地主机和浏览器来源；凭据由后端保存，浏览器输出会脱敏。

源码安装需要 Node.js 20 或更新版本来构建前端：

```bash
uv sync --extra dev --extra web
cd frontend/web
npm ci
npm run build
cd ../..
uv run oh web
```

`oh`、`openharness`、`openh` 使用相同管理入口。PowerShell 请用 `openh web` 或 `oh.exe web`，避免内置 `oh` 别名。

## 研究与恢复

核心架构已有通过真实 Agent Loop 的确定性集成测试；参见 [E2E-01 至 E2E-16 执行报告与覆盖率](docs/testing/agent-core-e2e-report.md)及[剩余问题与验证限制](docs/testing/agent-core-risks.md)。

报告项目强制使用 SRT 0.0.79 或配置的 Docker 后端，不可用时拒绝宿主 Shell 执行。[安装、脚本导出与崩溃恢复说明](docs/SANDBOX_EXECUTION.md)列出兼容变化；`uv run python scripts/check_types.py` 严格检查全部生产 Python，包含 bundled 脚本。

复杂研究先展示简短任务概要，再自动执行；简单问答无需强制计划。核心工具提供文件读写、搜索、网页检索、金融 MCP、Shell 计算与图片读取。财务分析、估值、图表和报告导出由后续 Skills 复用这些能力。修改文件和执行 Shell 继续遵守权限及沙箱检查。

Research Store 保留证据、引用、论证、结论和冲突管理。报告项目的目标、计划和任务状态由 ResearchAgentRuntime、TaskManager 和 CompletionPolicy 统一控制；旧记忆规划操作仅用于非报告项目会话。工具调用成功不代表事实已核验；搜索摘要、用户转述和暂定结论有明确状态。更正资料保留历史版本，并将依赖结论标记为待复核。

Planner/Replanner 继续作为注册工具返回提案，由 Runtime 验证并提交。`dispatch_subagents` 支持单任务及多任务并行，每个子代理使用独立历史和输出目录，结果由主 Agent 复核。项目工作区包含 `MEMORY.md`、`artifacts/`、`reports/` 和 `subagents/`；子代理不能修改主记忆或权威任务状态。默认研究注册表移除 `notebook_edit`、`config`、`mcp_auth`、`image_generation`、`sleep`，保留底层服务及显式通用注册表。参见[核心工具实现与兼容说明](docs/AGENT_CORE_TOOLS.md)及[工作区与 Markdown 记忆](docs/WORKSPACE_MEMORY.md)。

“停止生成”只取消执行；“打断并修改”保存修改要求、等待旧执行收束，再提交新计划。刷新或重启恢复已提交进度和来源，不自动启动新的模型调用。新对话不自动继承其他对话资料。

## 模型、认证与扩展

保留现有 Anthropic／OpenAI 兼容接口，以及 Codex、Claude、Copilot 订阅认证。API 配置可在网页编辑；订阅配置可在网页选择和测试，认证通过 CLI 管理：

```bash
oh auth login
oh auth codex-login
oh auth claude-login
oh auth copilot-login
oh auth status
oh provider list
oh provider use PROFILE
```

金融资料来自配置的 MCP 与实际检索来源，本次没有新增内置行情接口或交易服务。

```bash
oh mcp list
oh mcp add --help
oh plugin list
oh plugin install PATH
oh config show
oh config set research_memory.enabled false
```

保留技能、插件工具、MCP 和通用 Hooks，继续兼容 `.agents/skills`、`.claude/skills` 等发现目录。旧插件 manifest 的 Agent 和终端命令声明可读取，但不执行；`oh plugin list` 显示诊断。

网页搜索默认限定投研来源，按官方、行业专业、财经媒体排序，扩大到全网需显式选择。技能采用插件分发，业务技能分为 `analysis-modeling` 和 `report-generation` 两包，支持包与单项启停、仅元数据发现以及按需加载。详见 [Skill 模块与渐进式加载](docs/skill-modules.md)。

研究记忆默认启用，注入预算为 6,000 tokens，可独立关闭；关闭后不会启用旧记忆。废弃配置字段允许加载但忽略。旧 `memory` 上下文预算仅在顶层和当前模型均未指定预算时迁移。不会自动删除已有用户数据、凭据或自装扩展。

请求发送前会检查完整上下文预算；未知模型需显式设置 `context_window_tokens`。压缩先保存原文，验收通过后才替换历史，详见[请求预算与可恢复压缩](docs/context-budget.md)。

`oh eval` 提供 200 条版本化投研任务、真实运行轨迹、独立裁判及四维报告；Langfuse 为可选评测依赖，只在显式运行时连接。配置、试跑、重评分和补传见[Agent 评测](docs/agent-evaluation.md)。

个人助手产品、消息平台、编码编排、终端 UI、旧定时服务和长期／向量记忆已退役，投研 Web 是唯一对话入口。

## 验证

```bash
uv run pytest -q
uv run ruff check src tests scripts
cd frontend/web
npm run build
npx playwright install chromium
npm run test:e2e
```

真实模型测试独立执行，使用已有模型配置及临时研究工作区。`tests/test_web/real_research_eval.py --help` 提供规划、资料采集、计算、带来源回答、浏览器检查和重启追问的两轮验收入口。

Harness 开发说明见[工具契约、权限、重试与恢复](docs/HARNESS_EXECUTION.md)，本轮实际测试与限制见[验收记录](docs/testing/harness-validation.md)。
