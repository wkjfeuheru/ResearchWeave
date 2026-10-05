# 金融投研 Web 工作台

Web 工作台使用 FastAPI 和 React，支持模型配置、流式对话与 SkillHub。
当前为本机单用户版本，服务只监听回环地址，无需登录。

## 当前工作区直接启动

当前工作区已有 Python 虚拟环境和前端构建产物，可以直接启动：

```bash
cd /home/jason/pythonproject/OpenHarness
uv run --extra web oh web
```

浏览器打开 <http://127.0.0.1:8000>。保持这个终端运行；按 `Ctrl+C` 停止服务。
重启后点击历史记录恢复对话，服务不会自动继续上次中断的模型执行。
裸 `oh` 只展示帮助，投研对话使用 `oh web`。

如 8000 端口已被占用，可以改为：

```bash
uv run --extra web oh web --port 8001
```

此时打开 <http://127.0.0.1:8001>。服务固定监听 `127.0.0.1`，没有 `--host` 参数。

## 首次安装或更新源码后启动

需要 Python 3.10+，建议使用 Node.js 20 或更新版本。
在项目根目录安装 Python 依赖，并构建前端：

```bash
cd /home/jason/pythonproject/OpenHarness
uv sync --extra web
cd frontend/web
npm ci
npm run build
cd ../..
uv run --extra web oh web
```

修改前端源码后需要重新构建并重启服务，才能在 8000 端口看到更新。
如项目位于其他位置，将上面的绝对路径换成实际项目目录。
默认研究工具在启动时的工作目录执行；可用 `--cwd` 指定独立研究工作目录：

```bash
mkdir -p /path/to/research-workspace
uv run --extra web oh web --cwd /path/to/research-workspace
```

工作目录参与会话存储隔离。恢复原有会话时，应使用原来的工作目录。
不使用 uv 时，在 Python 虚拟环境中运行 `pip install -e ".[web]"`，随后运行 `oh web`。
`oh web` 不依赖 Node.js 运行；Node.js 只用于构建和开发前端。

首次进入后，在“模型配置”选择已有模型或添加接口并测试连接，然后在主对话页发送研究问题。
缺少 Web 依赖时重新执行 `uv sync --extra web`；页面提示“请先构建前端”时执行上面的前端构建步骤。
安装或同步依赖本身不会启动服务。

## 使用流程

1. 在“模型配置”添加 OpenAI-compatible 或 Anthropic-compatible 接口，填写服务商提供的模型 ID、API Key 和可选 Base URL。留空地址使用该接口类型的官方地址。
2. 保存后点击“测试连接”。测试会发送一次简短模型请求，可能产生服务商计费。将配置设为默认，或在对话页选择该配置。
3. 在 SkillHub 分别启停“财报穿透解析”“舆情与公告监控”“深度投研报告撰写”“研报精读与摘要”，四项默认启用；技能创建与维护插件继续保留。详情中的“在对话中试用”只填入示例命令，由用户点击发送。
4. 在主对话页发送研究问题。复杂研究先提交简短计划后自动执行，任务概要与进度在主面板实时更新。回复支持 Markdown、表格与末尾编号来源；中间过程按轮次收进“执行过程”：运行时默认展开，完成后收起，手动切换后保留当前选择。展开后按时间顺序展示进度文字和工具操作摘要；点击摘要可查看操作目标及成功、失败或中断状态。最终引用提交前会检查证据 ID；仅在发现无效引用时提示模型进行有限修正，仍无法修正则标明不可核验。完整工具参数和原始返回不展示，必要的工具权限请求、文件编辑确认和信息补充仍在浏览器中处理。

第一版同时运行一个生成任务。生成中可浏览模型与技能页面；返回对话即可查看进度或回复审批。
模型及技能修改从下一轮开始生效。当前对话的模型需先停止生成才能切换。
停止生成或关闭对话连接会取消当前任务，并保存已收到的文字；断线不会自动重发消息。
点击历史记录可恢复会话，新轮次的过程记录也会恢复。历史过程默认收起。
历史记录右侧的删除按钮在悬停、聚焦或触屏时显示；确认后永久删除该会话、研究记忆和资料快照，工具访问过的原始工作区文件保留。运行中或等待审批的会话需先停止；删除当前会话后回到新对话欢迎页。
一个会话同时只能在一个窗口连接，重复连接会被拒绝。

生成期间可以输入修改要求并点击“打断并修改”：先保存要求、取消并收束旧执行，再归档旧计划，重新规划后自动继续。
“停止生成”只停止当前执行。同一对话换题也会归档旧计划；引用旧计划资料时需要在新计划中显式复用证据。

## 研究工作记忆

每个会话独立保存以下四类记录，刷新、切换模型和重启服务后均可恢复：

- **Task Context**：用户授权的目标、研究对象、范围、时间范围、约束与交付物，修改保留版本。
- **Research State**：计划、任务进度、有效结论、阻塞项、未解决问题和下一步。
- **Evidence Pool**：证据陈述、来源 ID、原文定位、资料期间、采集时间、已知发布日期与核验状态。
- **Reasoning Chain**：显式研究步骤的证据输入、方法、假设、计算或验证结果与结论摘要。模型内部思维不采集。

程序在对话压缩和工具输出截断前登记来源；原始返回或工具提供的片段保存为不可变快照，记录 UTC 采集时间与 SHA-256。
网页读取记录最终地址和可识别的结构化发布日期，搜索摘要和用户转述默认待核验。来源不可达后仍可追溯已采集快照。
工具执行成功和任务完成均不代表事实核验成功。计算证据必须关联输入证据与计算步骤，证据撤回或修订会使依赖结论待复核。

Agent 通过 `research_memory` 工具显式维护状态，不增加每轮自动提取模型调用。工具支持读取或按 ID 查询、更新上下文、创建/替换计划、
更新任务、登记/修订证据、追加论证和登记/修订结论。写入使用唯一操作 ID 和预期版本号，程序验证当前会话引用后原子提交。
回答以 `[E:证据ID]` 引用已登记证据，后端按实际引用生成末尾简短来源，回答保存引用版本；无效标记显示“来源不可核验”。

研究注入预算默认为 6,000 tokens，只选择完整条目，完整资料与旧计划仍可按 ID 读取。可以调整预算或关闭研究记忆：

```bash
oh config set research_memory.injection_budget_tokens 8000
oh config set research_memory.enabled false
```

Web 不注入旧项目记忆、CLAUDE.md 或编码专用上下文，也不执行旧的跨会话记忆提取与整理。
旧 Web 会话保留原消息并建立空研究结构；旧摘要不会自动成为证据。状态损坏会明确报错并保留文件。

## 本地存储与兼容性

- 模型配置和插件开关复用 `~/.openharness/settings.json`，与 CLI 共用。
- API Key 使用现有凭据存储，后端接口只返回是否已配置，不返回密钥；编辑时留空保留原值，清除密钥是独立操作。环境变量提供的凭据不能通过网页清除。
- Web 创建的配置有独立凭据槽。Codex、Claude、Copilot 订阅配置可选择和测试连接，认证与编辑继续通过 CLI 管理；网页不提供订阅登录。
- Web 会话位于 `~/.openharness/data/web/sessions/<工作目录哈希>/`，旧 CLI 会话文件保留在原目录，新 Web 会话独立保存；文件保留原始模型上下文和用于界面展示的消息。
- 研究记忆位于 `~/.openharness/data/research/<工作目录哈希>/<会话ID>/state.json`；`content/` 保存资料快照。不同会话不自动共享资料与结论。
- 支持既有 `OPENHARNESS_CONFIG_DIR` 和 `OPENHARNESS_DATA_DIR` 环境变量。
- 默认配置、内置配置或正在被会话引用的配置不能删除。切换默认配置及有关会话的模型后，可删除自定义配置。
- Web 会话使用投研助手提示；没有新增行情数据源、文件上传、账户系统或交易接口。
- MCP 依赖限定在 v1 系列，与项目已有 `FastMCP` 接口兼容。

## 开发

终端一在项目根目录启动 `uv run --extra web oh web`。终端二启动前端：

```bash
cd frontend/web
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 将 `/api` HTTP 和 WebSocket 请求代理到本地 8000 端口。
代理端口修改时同步调整 `vite.config.ts`。服务校验本机 Host 和浏览器 Origin，开发 Origin 固定允许 5173 端口。

前端构建产物存在时，Python 构建钩子会将其打包进 wheel。未构建前端也能使用管理命令；网页启动后会提示先构建前端。
OpenHarness 终端编码对话及仓库 autopilot CLI 已移除，裸 `oh` 展示帮助。终端组件、个人助手、后台 worker、消息平台、旧定时与长期记忆已删除。投研 Web 是唯一对话入口。

## 验证

在项目根目录运行后端测试：

```bash
uv sync --extra dev --extra web
uv run pytest -q tests/test_research tests/test_web
```

浏览器测试先构建前端，再安装 Chromium：

```bash
cd frontend/web
npm ci
npm run build
npx playwright install chromium
npm run test:e2e
```

浏览器测试自行启动 8765 端口的隔离测试服务，配置和会话使用临时目录，只将模型传输替换成可控测试模型。不会读取个人凭据或调用真实模型。
可通过 `OPENHARNESS_TEST_BROWSER=/path/to/chromium` 指定本地兼容的浏览器可执行文件。

生产前端、后端测试和浏览器测试已加入 CI。真实模型冒烟单独执行，使用已配置的 API profile 和独立临时工作区：

```bash
git clone --depth 1 https://github.com/pypa/sampleproject.git /tmp/research-eval-workspace
uv run python tests/test_research/real_smoke.py --profile YOUR_PROFILE_ID --workspace /tmp/research-eval-workspace
```

该脚本会发送真实模型请求，产生服务商计费。在临时工作区写入明确标为虚构的报告，验证两轮研究、工具执行、保存后恢复、任务完成与证据引用；
会话数据写入新的临时目录，不修改个人配置。冒烟只向模型提供本地读取和研究记忆工具；MCP 连接及清理使用真实运行时。

复现开放式投研问题，可使用 `tests/test_web/real_research_eval.py` 的 `--question '分析一下当前光伏行业的景气度' --with-mcp`。
该模式保留所选配置的网页连接设置和 MCP 服务，记录每次模型请求与工具调用耗时，随后重启服务并复核资料日期。
测试驱动会批准已配置的 ftshare、fuyao 数据读取提示，拒绝配置修改等其他操作；状态与快照仅写入指定的测试目录。
MCP 服务配置支持 `request_timeout`（秒，默认 60，须大于零），约束单次工具调用和资源读取。
超时结果不构成事实证据；慢请求不会阻止已完成工具反馈进度，但下一次模型请求仍等待该批调用收束。
模拟测试不代表真实 API 验证。

投研场景的真实端到端测试走 HTTP/WebSocket、真实模型、官方网页采集和会话持久化，使用微软与苹果历史财报问题：

```bash
uv run python tests/test_web/real_research_eval.py --profile YOUR_PROFILE_ID --output /tmp/research-e2e
```

覆盖财年盈利质量、服务重启后的情景计算、用户转述冲突、换题、新对话隔离、搜索回退及采集后的打断重规划。
输出目录保留 `report.html`、`report.json` 和独立会话数据；个人配置不修改，临时凭据在测试结束后删除。
设置 `OPENHARNESS_TEST_BROWSER` 或传入 `--browser /path/to/chromium`，可同时检查真实回答在生产前端的来源展示和刷新恢复。
搜索不可用时会验证直接读取官方页面的回退，这不代表搜索服务验证通过。
