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

复杂研究先展示简短任务概要，再自动执行；简单问答无需强制计划。保留文件读取、搜索、网页检索、金融 MCP、计算、文件编辑、Notebook、Shell 与图像能力。修改文件和执行 Shell 继续遵守权限及沙箱检查。

研究记忆显式登记目标、计划、证据、论证和结论。工具调用成功不代表事实已核验；搜索摘要、用户转述和暂定结论有明确状态。更正资料保留历史版本，并将依赖结论标记为待复核。

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

网页搜索默认限定投研来源，按官方、行业专业、财经媒体排序，扩大到全网需显式选择。技能采用插件分发，原 `skill-creator` 已迁入可启停的 `skill-authoring` 插件，并提供四层资源骨架。配置与目录说明见 [投研搜索与技能插件](docs/research-search-and-skills.md)。

研究记忆默认启用，注入预算为 6,000 tokens，可独立关闭；关闭后不会启用旧记忆。废弃配置字段允许加载但忽略。旧 `memory` 上下文预算仅在顶层和当前模型均未指定预算时迁移。不会自动删除已有用户数据、凭据或自装扩展。

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
