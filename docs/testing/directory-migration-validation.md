# 目录迁移实际验收记录

## 版本、环境与工作区保护

开始及结束 HEAD 均为 `5a5643427def5b748228a975043824fbd5980aba`，分支为 `main`。
起始状态为 `main...origin/main`，132 项 unstaged 修改、474 项删除、6 项未跟踪
状态条目（目录折叠显示；展开的 untracked 清单另行保存）。结束 status 为 127 项修改、
479 项删除、14 项未跟踪状态条目。暂存区为空，结束仍为空。
用户已有 `researchx` 包名、`state/` 研究状态目录、SSE 实现，以及脚本和旧源码删除，
均作为实际基线保留。本轮没有提交、推送、reset、checkout、restore 或 clean。

保护证据位于本机 `/tmp/researchx-directory-migration/`：初始 status/log、staged 与
unstaged binary diff、untracked/ignored 清单、活动文件哈希和迁移前源码 tar 备份。
需要移除的旧包标记、helpers 原文件及 ignored bytecode 通过移动保存在该目录，
未清空用户目录。初始和结束 staged patch 比较相同。pyproject.toml、uv.lock、
hatch_build.py、web/events.py、前端 useConversation.ts、Vite 配置和 E2E 文件与
源码备份逐字节相同。目录迁移的实现与完整映射见 [设计记录](../DIRECTORY_MIGRATION.md)，
实际调用文件清单见 [changed-files.json](directory-migration/changed-files.json)。

结束核对时观察到原有 web-workspace.md、sse-transport.md、sse-validation.md 已删除，
这些删除不属于本轮迁移操作。保留当前删除状态，未擅自恢复，开始时原内容仍在源码
备份中。清单以 other_workspace_deletions_during_task 单列；README 的相关 Web 链接
已指向本轮新文档。

环境为 Linux，Python 3.11.17、Python 3.10.22、Node 24.21.0、npm 11.19.0、
uv 0.12.22。主环境使用当前 uv.lock；独立 3.10 环境通过 frozen sync 安装 dev/web/eval。
无桌面测试设置 `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring`，避免系统 keyring
交互等待，仍保留应用的文件后端行为。运行时设置 `PYTHONDONTWRITEBYTECODE=1`。
Shell/浏览器/安装验收使用仓库 `tools/sandbox/node_modules/.bin` 中 SRT 0.0.79。
没有读取真实模型凭据或发起付费模型评测。

## 阶段验证

以下命令在项目根目录运行，除注明 frontend 工作目录外。Python 测试带上述测试环境，
需要真实 sandbox 的命令另将仓库 sandbox Node bin 加入 PATH。

| 阶段/实际命令 | 结果 |
|---|---|
| 迁移前 `uv run --frozen pytest --collect-only -q` | 1308 项，完整收集成功 |
| 迁移前 `uv run --frozen pytest -q` | **1305 passed / 3 failed**，389.70s；[日志](directory-migration/tests-before.log) |
| 迁移前 `uv run --frozen ruff check .` | 通过 |
| 迁移前 `uv run --frozen ruff format --check .` | 失败：已有 test_cli.py、test_retry_hooks.py 两处格式问题 |
| 迁移前 `MYPYPATH=src uv run --frozen mypy --strict --explicit-package-bases src/researchx --exclude plugins/bundled` | 162 个生产文件通过；另执行下述完整 Skill 检查器，通过 |
| 迁移前 `npm run build`（frontend/web） | tsc 与 Vite 通过 |
| B 首次 `uv run --frozen pytest -q tests/test_research/test_planning_tools.py tests/test_research/test_runtime.py tests/test_harness/test_contracts.py` | 命令失败，所选旧文件名不存在，0 项执行；[日志](directory-migration/phase-b-first-command.log) |
| B 修正为 `uv run --frozen pytest -q tests/test_research/test_planning_tools.py tests/test_research/test_report_runtime.py tests/test_harness/test_execution.py` | **62 passed**；[日志](directory-migration/phase-b-corrected.log) |
| C1 `uv run --frozen pytest -q tests/test_utils/test_fs.py tests/test_config tests/test_auth tests/test_services/test_session_storage.py` | **106 passed**（当时尚未迁移测试目录）；[日志](directory-migration/phase-c-low.log) |
| C2 `uv run --frozen pytest -q tests/test_research/test_skill_workflows.py tests/test_tools/test_tavily_search.py tests/test_tools/test_research_search.py tests/test_backend_stability/test_safety.py` | **85 passed**；[日志](directory-migration/phase-c-domain.log) |
| C3 `uv run --frozen pytest -q tests/test_utils tests/test_hooks tests/test_research/test_workspace_memory.py tests/test_web` | **181 passed**（当时尚未迁移测试目录）；[日志](directory-migration/phase-c-final.log) |
| C/D `uv run --frozen ruff check .` 及同上生产 mypy 命令 | 通过，分别检查 164/167 个生产文件 |
| D `uv run --frozen pytest -q tests/test_web` | **83 passed**；[日志](directory-migration/phase-d.log) |
| E `uv run --frozen pytest -q tests/test_services tests/test_harness tests/test_hooks tests/test_research/test_planning_tools.py` | **197 passed**；[日志](directory-migration/phase-e.log) |
| F `uv run --frozen pytest -q tests/test_directory_migration.py tests/test_storage tests/test_security tests/test_workspace tests/test_services/test_message_chunks.py tests/test_config/test_paths.py` | **44 passed**；[日志](directory-migration/phase-f-layout.log) |

B/C/最终均重新导出并比较 Registry 快照：research/general 两模式的 20 个工具完整
Schema、描述与 Contract 均一致；基线 JSON 已入库作为自动回归 fixture。
Capability 集合按排序规范化后比较，不改变工具语义。
迁移前 163 个源模块可导入，迁移后 171 个安装模块可导入。
控制器方法（除构造函数类型注解）、两个通信路由的 AST 和 events.py 源码也与基线一致。

## 最终验收命令

| 实际命令 | 准确结果 |
|---|---|
| `uv run --frozen pytest --collect-only -q` | **1313 项，完整收集成功**；[日志](directory-migration/collect-after.log) |
| `uv run --frozen pytest -q`（3.11） | **1310 passed / 3 failed / 0 skipped**，428.25s；[日志](directory-migration/tests-py311-after.log) |
| `UV_PROJECT_ENVIRONMENT=/tmp/researchx-directory-migration/py310 uv sync --frozen --python /tmp/openharness-stability-py310/bin/python --extra dev --extra web --extra eval` | 3.10.22 锁定环境成功安装 |
| `/tmp/researchx-directory-migration/py310/bin/python -m pytest -q`（另设 PYTHONPATH=项目 src） | **1310 passed / 3 failed / 0 skipped**，448.87s；[日志](directory-migration/tests-py310-after.log) |
| `uv run --frozen ruff check .` | **通过**；[日志](directory-migration/ruff-final.log) |
| `uv run --frozen ruff format --check .` | **通过**；[日志](directory-migration/format-final.log) |
| `bash docs/testing/directory-migration/reproduce-types.sh` 对应的 Python 检查器 | **严格检查 12 组、208 个文件通过**，含全部 bundled Python 资源及 evals/build hook；[日志](directory-migration/types-all-after.log) |
| `git diff --check` | **通过** |
| `npm run build`（frontend/web） | **tsc/Vite 通过**；[日志](directory-migration/frontend-after.log) |
| `npm run test:e2e -- --output=/tmp/researchx-directory-migration/playwright` | 首轮并发验收 **16 passed / 2 failed**，均为 5 秒完成等待超时；[日志](directory-migration/e2e-after.log) |
| `npm run test:e2e -- --output=/tmp/researchx-directory-migration/playwright-serial` | 同代码/断言/timeout 单独重跑 **18 passed**，1.8m；[日志](directory-migration/e2e-serial.log) |
| `RESEARCHX_REQUIRE_SANDBOX=1 uv run --frozen pytest -q tests/test_research/test_report_sandbox.py tests/test_research/test_dispatch_recovery.py` | **20 passed / 0 skipped**，28.96s；SRT/Docker 路径隔离、超时取消、宿主产物登记、进程崩溃恢复；[日志](directory-migration/sandbox-acceptance.log) |
| `uv build --wheel --out-dir /tmp/researchx-directory-migration/wheel` | **成功**，researchx_ai-0.1.9-py3-none-any.whl；[日志](directory-migration/wheel-build.log) |
| `uv venv --python .venv/bin/python /tmp/researchx-directory-migration/wheel-env` | 新建干净 3.11 环境 |
| `uv export --frozen --no-dev --extra web --no-emit-project --no-hashes --format requirements-txt -o /tmp/researchx-directory-migration/wheel-constraints.txt` | 锁定安装约束导出成功 |
| `uv pip install --python /tmp/researchx-directory-migration/wheel-env/bin/python --constraint /tmp/researchx-directory-migration/wheel-constraints.txt '/tmp/researchx-directory-migration/wheel/researchx_ai-0.1.9-py3-none-any.whl[web]'` | 干净 wheel 安装成功 |
| 新环境 Python 执行 `tests/test_install/wheel_smoke.py`，cwd=/tmp、无源码 PYTHONPATH | **通过**：171 安装模块、8 个 Skill 脚本/产物、新文档 CLI、Shell、Web 和持久化；[日志](directory-migration/wheel-smoke.log) |
| 新环境 `rx --help`、`oh --help`、`openh --help` | **3/3 通过**；[日志](directory-migration/cli-smoke.log) |
| 编译并 import 两个 `tests/test_web/real_*_eval.py` | **通过**；没有运行模型评测；[日志](directory-migration/eval-import.log) |

最终格式检查前，新长模块名让 test_shell.py 的 import 行超长，首次检查提示该文件需格式化；
已仅运行 Ruff format 修正。原两个格式问题也仅作格式修正，测试断言未改。
浏览器使用已有 Chromium，经 `RESEARCHX_TEST_BROWSER` 指定。首轮并发时系统负载约为 5.85；
单独重跑完全通过，推断是并发负载下的时间敏感失败，未扩大 timeout 或弱化断言。

`scripts/check_types.py`、`scripts/ci_validation.py` 和 `scripts/test_docker_sandbox_e2e.py`
在本轮开始前已被用户删除，因此没有执行这些缺失文件，也没有恢复它们。类型验收采用
Git 基线原检查器的内存副本，仅替换当前包名并移除不存在检查器自身的检查项，完整保留
bundled scripts 的临时包视图、严格规则及所有生产分组；复现脚本固定基线 SHA，
不会写回源码。CI 原来的缺失脚本命令仍需在单独的开发工具修复任务中处理。

## 已有失败与未运行项

两版本的三个失败与迁移前完全相同，未修改预期、删除断言或增加 skip：

1. `test_package_has_no_retired_imports_or_dynamic_loads` 的 RETIRED 集合包含 `state`，
   与当前用户已有的权威研究状态目录冲突。本轮保留 state 的职责与现有测试。
2. `test_powershell_installer_recommends_openh_for_windows` 读取已删除的 install.ps1。
3. `test_powershell_installer_falls_back_when_openh_exe_missing` 同样依赖该缺失脚本。

全量收集已恢复且没有新增 Python 失败，但不能宣称完整套件或 CI 全绿。
真实付费研究/Skill 评测、远端 provider、外部 MCP 副作用未运行，目录迁移没有要求
启动这些服务。已有自动化验证使用模拟 API；sandbox 验收使用本地真实执行后端。

全仓旧模块路径搜索的有效源码/资源结果为空；wheel 不包含旧 utils 或 tools 子包。
审计文档、负向断言和历史日志仅记录旧路径，不是执行依赖。Web 主路径搜索 WebSocket、
websocket_connect、new WebSocket、routeWebSocket、websockets.connect、/ws、ws: true
无结果；MCP 的独立 ws 配置类型和依赖保留。

外部自定义 Skill 或插件若导入原 utils/services 私有模块，需要按迁移映射更新；
不存在旧 utils 兼容 shim。本轮完成目录阶段 A–F，未重写通信或研究逻辑。
