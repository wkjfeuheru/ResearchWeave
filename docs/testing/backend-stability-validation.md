# 后端稳定性修复验收记录

日期：2026-10-09。这是本轮实际执行结果，不是远端 CI 状态声明。

## 实际版本和工作区保护

- 开始/结束 HEAD：`027bf24bfdb6e503c34741a628df754adac354e0`，`main...origin/main`。最近历史：`027bf24 feat:version 2`、`987802f feat:version 1`、`c857680 Initial commit`。没有提交或暂存。
- 开始有 23 个 tracked unstaged 文件和 6 个 untracked 状态条目，共 29 项；结束为 53 个 tracked unstaged 文件和 11 个 untracked 状态条目，共 64 项；staged 始终为空。Tavily transport、退休工具迁移、MCP 取消、Web race 修复等已有实现已经在此工作区，未用旧 SHA 覆盖。
- 先记录 `git status --short --branch`、`git rev-parse HEAD`、`git log -5 --oneline --decorate`、staged/unstaged diff、untracked 和 ignored 清单。没有适用的 AGENTS.md/CONTRIBUTING；阅读了 README、现有后端文档和本次规范。
- 原始 staged patch SHA256：`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`；原始 unstaged patch：`00a98169756efed3db720491c114838c18565712a52f26e5f87d5c0250dcf4ff`。原始补丁留在 `/tmp/backend-stability-start-{staged,unstaged}.patch`，不把可能含用户实现细节的补丁发布为测试日志。
- 18 个既有 tracked diff 与开始时逐字节相同；其余 5 个是原有 `docs/HARNESS_EXECUTION.md`、`engine/query.py`、`research/conflicts.py`、`services/tool_execution.py`、`tools/base.py`，在原实现上增量扩展。既有 untracked 文档/测试/退休模块未删除，Tavily 模块仅提取共享 DTO 并保持兼容导出。
- 开始记录的 23,404 个 ignored 路径全部仍存在。没有清理 `.venv`、`node_modules`、用户数据或缓存。前端旧 dist/tsbuildinfo 先备份到 `/tmp/openharness-stability-start-web-build`；重建后原 dist 文件内容相同。
- **工作区保护遗漏：** E2E 重生成了 ignored 的 `frontend/web/test-results/` 四张截图和 `.last-run.json`。它们没有开始时的单独备份/内容哈希，未找到可验证的旧副本，不能声称旧内容保留。最终输出另存于 `/tmp/openharness-stability-final-test-results`，但这不能恢复原内容。源码补丁和既有未跟踪实现保留的证据不适用于这些测试输出。
- `uv.lock` SHA256 开始/结束均为 `5dab9a7803fc60423eee7e350b6c3ca9b2dc025ec2939bad626e283c92bd5d68`，锁和依赖声明未修改。使用两个独立临时 venv，不替换原项目环境。没有 reset/clean/checkout/restore。

## 环境及真实基线

Ubuntu 20.04 / Linux 5.15 x86_64；Python 3.10.22、3.11.17；uv 0.12.22；Node 24.21.0 / npm 11.19.0。锁定依赖包含项目 dev/web/eval extras。

可用的真实后端：SRT 0.0.79、bubblewrap、socat、Docker 26.1.3。使用已有 `openharness-sandbox:latest` / `openharness-sandbox-e2e:latest` 镜像（同一 ID `sha256:15e18b796a3f9b11e84b996eab4635f42666537a3aaecda046a848816e62812b`）；Dockerfile 本轮未修改，没有声称重新构建镜像。

前端保留既有 node_modules，`npm ls --depth=0` 核对已安装版本与锁一致。Playwright 1.63 使用本机可运行的 Chromium 1140：`/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome`。这是 Ubuntu 20.04 下的浏览器替代验证，不等同于 ubuntu-latest 上新下载 Chromium 的验收。未运行 `npm ci` 覆盖原 node_modules。

基线命令（仓库根目录，开发前）：

| 命令 | 原始结果 | 日志 |
| --- | --- | --- |
| `uv run --frozen python -c 'import openharness.tools.web_search_tool'` | 通过 | `stability-baseline-import.log` |
| `uv run --frozen pytest --collect-only -q` | 1240 项，0 收集错误 | `stability-baseline-collect.log` |
| `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run --frozen pytest -q --tb=short` | 1240 passed，269.53s | `stability-baseline-tests.log` |

因此评审 SHA 的历史硬导入/退休工具收集故障在**已有本地工作**中已经修复。本轮继续解耦 HTML import、清除失效执行分支并验证，未把旧 CI 失败当成本轮失败。

## 最终执行命令和结果

`UV_PROJECT_ENVIRONMENT` 仅指向独立临时 venv。以下日志均在 [backend-stability-logs](backend-stability-logs/)；`manifest.json` 记录大小和 SHA256。阶段失败日志也保留，不被最终通过覆盖。

依赖环境实际准备命令：

```bash
UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 uv sync --frozen --extra dev --extra web --extra eval --python 3.11
UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py310 uv sync --frozen --extra dev --extra web --extra eval --python 3.10
```

最终两个全量测试实际使用临时 HOME，以免写入真实账户配置；没有通过全局忽略模块或改变测试的路径断言规避失败。以下是实际命令记录：

```bash
HOME=/tmp/openharness-stability-home311 UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 PATH="$PWD/tools/sandbox/node_modules/.bin:$PATH" PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run --frozen pytest -q -W error::pytest.PytestUnraisableExceptionWarning --tb=short
HOME=/tmp/openharness-stability-home310 UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py310 PATH="$PWD/tools/sandbox/node_modules/.bin:$PATH" PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring uv run --frozen pytest -q -W error::pytest.PytestUnraisableExceptionWarning --tb=short
HOME=/tmp/openharness-stability-quality-home UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 uv run --frozen pytest --collect-only -q
UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 uv run --frozen python scripts/check_types.py
UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 uv run --frozen ruff check src tests scripts evals hatch_build.py
UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 uv run --frozen ruff format --check src evals scripts/check_types.py hatch_build.py
UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 uv run --frozen python -c 'import openharness.tools.web_search_tool; print("PASS: web_search_tool import")'
git diff --check
```

| 最终检查 | 通过 | 失败 | 跳过 | 日志 |
| --- | --- | --- | --- | --- |
| Python 3.11 完整 pytest | 1287，360.28s | 0 | 0 | `stability-verified311.log` |
| Python 3.10 完整 pytest | 1287，394.67s | 0 | 0 | `stability-verified310.log` |
| 完整收集 | 1287，2.24s | 0 collection errors | 无 ignore | `stability-collect-verified.log` |
| 严格生产类型检查 | 全部分组通过 | 0 | 0 | `stability-types-verified2.log` |
| Ruff 完整范围 | 通过 | 0 | 0 | `stability-ruff-final.log` |
| CI 的 production format check | 240 文件 | 0 | 0 | `stability-format-final.log` |
| source import / diff check | 通过 | 0 | 0 | `stability-import-final.log` / `stability-diff-final.log` |

真实 sandbox acceptance 实际执行两次，相同 CI 选例；设置 `OPENHARNESS_REQUIRE_SANDBOX=1`，PATH 包含本地 SRT，headless keyring；使用 Python 3.11 锁定环境：

```bash
OPENHARNESS_REQUIRE_SANDBOX=1 PATH="$PWD/tools/sandbox/node_modules/.bin:$PATH" PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring UV_PROJECT_ENVIRONMENT=/tmp/openharness-stability-py311 uv run --frozen pytest -q tests/test_research/test_report_sandbox.py tests/test_research/test_dispatch_recovery.py scripts/test_docker_sandbox_e2e.py
```

结果：39 passed / 0 failed / 0 skipped；第一次 142.46s，最终验收 176.01s，日志 `stability-sandbox.log`、`stability-sandbox-final.log`。不是用 mock 代替真实隔离。

Web 实际命令（cwd 为 `frontend/web`）：

```bash
npm run build
PATH=/home/jason/pythonproject/OpenHarness/tools/sandbox/node_modules/.bin:$PATH PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring OPENHARNESS_TEST_BROWSER=/home/jason/.cache/ms-playwright/chromium-1140/chrome-linux/chrome npm run test:e2e
```

Build 通过，8.01s。E2E 首轮 16 passed / 2 failed；修复后两项定向重跑 2 passed / 0 failed，27.4s；最终完整 18 passed / 0 failed / 0 skipped，2.2m。日志 `stability-web-build.log`、`stability-e2e.log`、`stability-e2e-repaired.log`、`stability-e2e-final.log`。定向选例是 `research progress, source footnotes and interrupt to replan` 与 `attachments, real script artifacts, download, reload and isolation`，保留原断言和时间上限。

Wheel smoke 使用独立安装环境，生产依赖受当前锁约束；最终仅重装项目包，未覆盖源码环境：

```bash
uv venv --python 3.11 /tmp/openharness-stability-wheel-env
uv export --frozen --no-dev --extra web --no-emit-project --no-hashes --format requirements-txt -o /tmp/stability-wheel-constraints.txt
uv build --wheel --out-dir /tmp/openharness-stability-wheel
uv pip install --python /tmp/openharness-stability-wheel-env/bin/python --constraint /tmp/stability-wheel-constraints.txt '/tmp/openharness-stability-wheel/openharness_ai-0.1.9-py3-none-any.whl[web]'
env -u PYTHONPATH PATH="$PWD/tools/sandbox/node_modules/.bin:$PATH" PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/openharness-stability-wheel-env/bin/python tests/test_install/wheel_smoke.py
uv build --wheel --out-dir /tmp/openharness-stability-wheel-final
uv pip install --python /tmp/openharness-stability-wheel-env/bin/python --reinstall-package openharness-ai --constraint /tmp/stability-wheel-constraints.txt '/tmp/openharness-stability-wheel-final/openharness_ai-0.1.9-py3-none-any.whl[web]'
env -u PYTHONPATH PATH="$PWD/tools/sandbox/node_modules/.bin:$PATH" PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring /tmp/openharness-stability-wheel-env/bin/python tests/test_install/wheel_smoke.py
```

两次 smoke 均通过：162 个 installed modules，可导入且不含退役资产；Web/CLI/Shell/研究持久化验证成功。日志 `stability-wheel-*.log`。质量命令在修复过程中重复运行；最终 Ruff/format/diff 再跑仍通过。

## 中间失败、根因和修复

这些不是遗留失败，不将它们抹去或报告成一次全绿：

| 检查/日志 | 当时结果 | 根因和处理 |
| --- | --- | --- |
| phase2-shell | 157 passed / 1 failed | 旧 Bash fixture 假定无配置直接宿主执行；保留断言，明确默认严格沙箱和显式宿主两种配置 |
| phase2-final | 189 passed / 3 failed | 新安全 fixture 的可选 metadata 初始化遗漏、fail-closed 错误 metadata 不完整；分别修正 |
| phase3-final | 246 passed / 1 failed | 过度拒绝显式批准的 Skill root alias；恢复根别名兼容，持续限制子路径和自动发现的外部 alias |
| types-initial | 2 errors | 缓冲变量缺少注解，nullable complete event 未缩窄；补齐类型，不放宽配置 |
| safety-verified / target-final / host-debug | 宿主可见性新用例失败 | 假模型没有声明 context window；补 fixture 窗口，不绕过原预算守卫 |
| full311 / full310 | 1280/1279 passed，1/2 failed；3.10 另有 1 warning | 上述 fixture；SQLite checkpoint 删除 sidecar 后 open 的竞态；Bash pipe transport finalizer；针对根因修复，最终以 warning-as-error 验证 |
| final311 / final310 | 1282/1283 passed，5/4 failed | spawn worker 导入整个 SDK 测试模块过慢；越界 cwd 异常未转换 ToolResult；3 项默认路径测试与本轮全局 CONFIG/DATA 覆盖冲突。轻量 worker 保持原 20s 限制、结构化越界错误、环境改为临时用户根；断言不变 |
| e2e / e2e-export / e2e-export-debug | 完整 2 失败；导出定向仍失败 | 同机重负载下 steer progress 暂时超时；fixture 脚本使用 `frontend/web/../../` 路径经过 sandbox masked 子目录。规范化脚本/解释器路径；原 timeout/断言不变，修复后完整通过；诊断代码已撤回 |
| ruff-acceptance / ruff-verified | 1 error | Bash unused Path import；删除该导入，最终全范围通过 |

阶段探索用 `uv run --frozen pytest -q` / `--tb=short` 选择相关 Harness、权限、Hooks、research、Skills、Bash、Web 测试；3.10 的 pipe 回归还使用 `-W error::pytest.PytestUnraisableExceptionWarning`。阶段日志完整保留在目录中并在 manifest 提供实际统计；部分早期子集命令的逐项选例参数未写入日志，不能据统计反推并伪造精确命令。最终全量和 CI 命令如上，覆盖这些选例且无 ignore/额外 skip。

## 本轮文件和兼容性变更

详细文件清单见 `backend-stability-logs/implementation-files.txt`。以下不把此前已经存在的退休工具测试、MCP/Web 改动冒充本轮新增。

| 优先级 | 文件组 | 原因 / 实际修复 |
| --- | --- | --- |
| P0 | `utils/search_types.py`、`utils/tavily_search.py`、`tools/web_search_tool.py` | provider-neutral DTO、懒加载 transport；兼容旧接口，HTML 独立导入 |
| P0 | `runtime.py`、`engine/query.py`、`services/tool_execution.py` | 删除已退役 image/conflict 工具的死配置和特殊分支；保留既有冲突报告与通用 subagent 路径 |
| P1 | `utils/fs.py`、`file_lock.py`、`session_files.py`；`config/paths.py`、`settings.py`、`auth/storage.py` | 显式私有权限，既有文件硬化，无全局 umask 新依赖；WAL 删除竞态修复 |
| P1 | `services/session_storage.py`、`web/storage.py`、`research/store.py`、`repository.py`、`conflicts.py`、`dispatch_audit.py`、`engine/query.py`、`subagents.py` | 快照/状态/资料/输出的私有目录及文件，不 chmod 普通工作区文件 |
| P1 | `services/tool_execution.py`、`engine/query_engine.py`、`permissions/capabilities.py`、`config/settings.py` | 30s 有界 claim、限幅退避/取消、精确 bookkeeping 授权、冻结宿主授权快照 |
| P1 | `tools/bash_tool.py`、`sandbox/policy.py` | 生成代码 fail closed、显式可见的宿主选择、子 agent/report 不扩权、进程组/pipe cleanup、受限 host export import |
| P1 | `skills/loader.py`、`resources.py`、`types.py`、`plugins/loader.py` | 入口读取前锚定初始 root，progressive load 再检查，遍历不 followlinks |
| P1/P2 | `tools/base.py`、`contracts.py`、`services/tool_execution.py`、`api/retry.py` | structured transient/no-effect、真实 key adapter、保守核验、完整事件上限、provider structured code/usage |
| P2 | `services/operations.py` | schema v2、跨进程一次初始化缓存、索引 EXPLAIN 用例、有限 terminal audit 清理，保留 operation 身份/结果 |
| 验证/文档 | `tests/test_backend_stability/{test_safety,test_retry_storage,storage_worker}.py`、`tests/test_harness/test_execution.py`、`tests/test_tools/test_bash_tool.py`、`tests/test_web/browser_server.py`、README 中英文、后端手册/本文 | 新增 46 项稳定性选例 + 1 项 Bash 参数化选例，保留有效安全断言和现有预算/审批/恢复行为 |

新增配置 `sandbox.allow_trusted_host=False`；ToolResult 两个默认字段、ToolContract key-support 标志、BaseTool key adapter 和 claim-wait 构造参数均为向后兼容的保守扩展。SQLite v2 是增量迁移。没有引入依赖/engine/queue/Manager，也未改变 Planner/Replanner 或 Workspace 的架构。

## 未运行 / 风险 / 下一步

- 最终要求的本机验收命令没有剩余失败或跳过；Windows/macOS ACL、不同 OS 上最新 Chromium、干净 Docker 镜像重建和远端 GitHub Actions 未运行。浏览器替代与复用 Docker 镜像的边界如上。
- 真实 Tavily/模型 billing、外部 MCP/HTTP 写及远端 idempotency guarantee 未发出生产调用验证；provider usage 仍可能 unknown/estimated。测试覆盖 mocks 和协议语义，不声称外部服务 exactly-once。
- uncertain/partial、审计 settlement 失败后的 running 记录必须人工/provider adapter 核验。单机 SQLite，热操作同步 busy 最长 250ms、cold init 5s；不是分布式租约。保留身份和产物会持续增长，提供有界 audit 清理但不自动删除恢复证据。
- 非 POSIX 文件权限无同等级 ACL 保证；显式 trusted host 无隔离；自装 Skill 脚本在沙箱根之外可能需先 staging 到工作区。固定 root 检查并非对抗同 UID 主动竞态的完整 openat 安全模型。
- 现在 P0、完整收集和安全关键本机验收门槛已满足。下一步先让这些改动跑过远端 CI/生产相同 OS 的浏览器与镜像构建，再独立进行端到端研报工作流验收；本轮没有启动新的财报工作流。
