# PostgreSQL 切换验收记录

本轮基于 `main` 的 `7fd45814cb26b926ef5c3641dca8b2d73fec8323`。开始时 staged、unstaged、untracked 均为空；没有提交、切换分支、重置或清理用户工作区。当前源码包名是 `researchx`，研究领域位于 `state/`。SSE/HTTP 接口、手写 Agent Loop 和工具公开名称保持原路径与语义。

实施期间，另一个工作区操作将研究契约移到 `contracts/`、规划适配器移到 `engine/`、文档和导出移到 `workspace/`，并删除 `.github/`。这些 staged/unstaged 变更已保留；收到用户明确指示后仅恢复 `.github/workflows/ci.yml` 的 PostgreSQL CI 配置，未恢复 Issue/PR 模板，未改写暂存区。工作流使用当前 `tools/check_types.py` 和测试路径，包含 PostgreSQL 16 service、显式 Alembic 迁移、Python 3.10/3.11、Web E2E、wheel 和实际沙箱检查。

原始基线日志、阶段测试和最终检查日志位于 `/tmp/researchx-postgres-cutover/`。该目录是本轮专用临时目录，未纳入版本库；部署前应按需要保存日志。下列状态只表示本机实际运行，不代表远端 CI 已执行。

## 环境

- Linux；Python 3.11.17（项目 `.venv`）和 3.10.22（独立锁定依赖环境）。
- `uv.lock`：SQLAlchemy 2.1.4 / Python 3.11；SQLAlchemy 2.0.54 / Python 3.10；asyncpg、Alembic。
- PostgreSQL 16，独立 Docker 测试容器 `researchx-postgres-cutover`，仅监听本机 55432；没有使用 SQLite 替代集成数据库。
- Node 24.21、npm 11.19、Playwright 1.63.0、本机 Chromium 130.0.6723.31。
- 测试设置 `RESEARCHX_TEST_DATABASE_URL`；应用/迁移和浏览器子进程设置 `RESEARCHX_DATABASE_URL`。密钥存储使用 headless 测试 backend；SRT 加入本轮命令的 PATH。

## Schema 与存储边界

30 张业务表覆盖工作区、研究领域实体、历史/回执、会话消息、内容引用、子代理调度和执行账本。Alembic 版本为 `0001_postgresql` → `0002_scoped_relations` → `0003_child_sessions`。已测试空库迁移、含数据的版本升级以及 metadata/schema 一致性；日志分别为 `fresh-migrations.log`、`populated-upgrade.log`、`schema-parity.log`。

源码中正式研究状态、Web/CLI 会话和操作账本不再读写 JSON/SQLite 权威后端。`storage/legacy_import.py` 是明确保留的旧数据读取入口；压缩 JSON/Markdown 导出是备份或投影。配置、凭据、插件清单和离线评测数据集不是业务会话后端，继续使用文件。`MEMORY.md` 保留工作区文件语义。

内容先写入并校验，再在事务中登记引用。来源、附件、产物、历史中的报告正文，以及会话图片/大消息块使用 ContentStore。不会在新引用提交前删除旧对象；没有自动内容 GC。

## 实际历史数据扫描

执行了只读 dry-run，没有向测试库或生产库导入用户历史：

```bash
uv run --frozen python -m researchx.storage.legacy_import \
  --source /home/jason/.researchx/data \
  --workspace /home/jason/pythonproject/OpenHarness \
  --report /tmp/researchx-postgres-cutover/legacy-dry-run-final.json
```

| 分类 | 发现 | 导入 | 跳过 | 失败 |
| --- | ---: | ---: | ---: | ---: |
| Web 会话 | 3 | 0 | 0 | 0 |
| CLI 会话 | 128 | 0 | 0 | 0 |
| 研究状态 | 3 | 0 | 0 | 0 |
| 内容 manifest / dispatch / 子代理 transcript | 0 | 0 | 0 | 0 |
| 压缩上下文归档 | 965 | 0 | 0 | 0 |
| 工具内容归档 | 191 | 0 | 0 | 0 |
| SQLite 工具账本 | 1 | 0 | 0 | 1 |

发现 66 个工作区、1033 个会话身份，输入文件含 8651 条消息。dry-run 以非零状态报告旧账本的正文引用越出批准的源目录；没有扩大读取边界或忽略该错误。源文件未删除、覆盖或自动整理。实际生产导入仍须确定目标数据库，核实该引用并重新 dry-run。

合成历史夹具已覆盖 dry-run、首次导入、重复跳过、变更源拒绝、损坏/缺失引用、事务回滚、SQLite 未完成写操作转 uncertain、未知 usage 保留、正文 hash 校验、压缩/工具内容及源文件原样保留。它们的导入成功不等于上表中的用户历史已迁移。

## 检查记录

原始 `uv run pytest --collect-only -q`：1313 项完整收集。原始全量测试：1310 通过、3 失败（旧目录断言和缺失 Windows 安装脚本），详见 `collect-before.log`、`tests-before.log`。本轮修复这些确定的基线问题；Windows 安装脚本只做静态检查，未在 Windows 运行。

中间回归曾发现遗漏 await、旧文件后端断言、异步测试前置条件，以及多套重检查同时运行造成的超时。`python311-full3.log` 记录该阶段 1237 通过、10 失败；它不是最终验收。较早的 Python 3.10/3.11 运行被主动中断以改为顺序执行，也不算全量通过。

测试适配保留业务/安全断言：取消测试先确认兄弟任务持久化完成；超时保留文件测试为真实数据库写入留出 1 秒，同时新增 1 毫秒提前截止测试。浏览器测试等待 SSE `started` 再注入文本或检查执行进度；一次完整重规划的 12 个事务调用使用 15 秒完成等待，整个用例原有 60 秒上限保留。没有增加 skip 或删除失败断言。

最近一次全量 `uv run --frozen pytest -vv --tb=short --durations=10` 为 **1334 通过、5 失败**（`python311-final.log`）。运行中发生上述目录迁移，3 个沙箱/导出失败引用了测试进程先前加载的 `researchx.research.exports`；另有迟到子任务用例的 60 秒超时和 bash 部分输出超时断言失败。该次运行不是最新目录布局的完整验收，后续定向通过也不能替代重新跑全套。Python 3.10 的完整验收尚未完成。

恢复 CI 后实际执行：

| 命令 / 检查 | 结果 | 日志 |
| --- | --- | --- |
| `uv run --frozen pytest --collect-only -q` | 1344 项完整收集 | `collect-restored-ci.log` |
| `uv run --frozen ruff check src tests tools evals hatch_build.py` | 通过，最终复核也通过 | `ruff-restored-ci.log` / `ruff-restored-ci-final.log` |
| `uv run --frozen ruff format --check src evals tools/check_types.py hatch_build.py` | 通过，首次 261、最新 260 个文件（并行目录迁移移除旧包入口） | `format-restored-ci.log` / `format-restored-ci-final.log` |
| `uv run --frozen pytest -q tests/test_storage/test_content_backends.py tests/test_storage/test_postgresql.py tests/test_directory_migration.py` | 25 项通过，退出码 0 | `restored-ci-regression.log` |
| YAML 解析及 CI job/service/迁移/脚本路径断言 | 通过，4 个 jobs | 本轮工具输出 |
| `git diff --check` | 通过 | 本轮工具输出 |

`uv run --frozen python tools/check_types.py` 首次复核发现查询表达式缓存的两处 BindParameter 泛型注解缺失，补齐后复核通过，覆盖主包 182、插件 35、评测 2、构建脚本 2 个源文件；日志为 `types-restored-ci-fixed.log`。

Python 3.10 另外实际执行：

```bash
/tmp/researchx-postgres-cutover/python310/bin/python -m pytest -q \
  tests/test_storage/test_postgresql.py tests/test_storage/test_content_backends.py \
  tests/test_storage/test_legacy_import.py tests/test_directory_migration.py
```

30 项通过（46.15 秒），使用真实 PostgreSQL，日志为 `python310-restored-ci.log`。这是定向兼容检查，并非 Python 3.10 完整套件通过。

最新真实沙箱与恢复复测实际命令（PATH 加入 SRT，设置 `RESEARCHX_REQUIRE_SANDBOX=1` 和测试数据库）：

```bash
uv run --frozen pytest -q \
  tests/test_research/test_agent_core_integration.py::test_e2e_10_late_children_cannot_pollute_revised_plan \
  tests/test_research/test_report_sandbox.py \
  tests/test_research/test_skill_workflows.py::test_isolated_skill_exports_do_not_import_database_stack \
  tests/test_research/test_dispatch_recovery.py \
  tests/test_sandbox/test_docker_backend.py tests/test_sandbox/test_docker_image.py
```

**47 通过、1 失败**（229.68 秒），日志 `current-sandbox-recovery.log`。当前目录的 SRT/Docker 导出、数据库隔离导出、崩溃恢复和沙箱断言通过；迟到子任务场景仍触发原有 60 秒等待上限，尚不能宣称全量验收通过。未放宽其超时或断言。随后单独执行该用例（`uv run --frozen pytest -q tests/test_research/test_agent_core_integration.py::test_e2e_10_late_children_cannot_pollute_revised_plan --tb=short --durations=3`）仍失败，日志为 `late-child-current-alone.log`，因此不能将此问题仅归因于并行测试。

较早的前端构建和干净 wheel 安装通过，日志为 `frontend-build.log`、`wheel-build.log`、`wheel-install.log`、`wheel-smoke2.log`、`wheel-migrations.log`。它们发生在并行目录迁移之前，不能宣称最新 wheel 已验证。最新一次完整浏览器运行 `e2e-progress3.log` 为 16 通过、2 失败；对应两项修复的定向复跑已通过，但仍需最新完整 E2E。磁盘可用空间曾降至约 190 MB，尚未启动额外的大型构建。远端 CI 未执行。

## 部署前仍需处理

- 配置正式 PostgreSQL 与内容存储，备份并解决历史账本的越界引用，完成真实导入和业务核验。
- S3 adapter 已有离线测试；真实 S3/MinIO 的 IAM、TLS、条件 PUT 与网络故障行为未执行。
- 未调用付费模型或真实外部写 API；相关评测脚本仅做可编译验证。
- PostgreSQL 不改变 SSE 控制器、工作区锁和 MEMORY.md 的单控制进程约束；多实例需共享工作区并保持规范路径、数据目录配置和会话路由。
- 未知外部副作用继续 uncertain，需要人工 reconciliation；provider usage 不可获得时保留 unknown。
- 已配置 CI 的 PostgreSQL services，但本轮未推送或执行远端 CI。
