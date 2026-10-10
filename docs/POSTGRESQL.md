# PostgreSQL 持久化与迁移

本版本以 PostgreSQL 为结构化业务数据的唯一权威存储。应用不会自动建表、升级 Schema、读取旧 JSON 或回退 SQLite。旧版安装必须先停止旧进程、备份、迁移并导入，再启动新版本。`MEMORY.md`、配置和密钥文件仍遵循原有文件权限与工作区规则。

## 数据流和表边界

| 入口 | 权威数据 | 内容/投影 |
| --- | --- | --- |
| ResearchStore / ResearchRepository | research_sessions 行锁与 revision；context、source、plan/task、evidence、reasoning、conclusion、conflict/arbitration、project/objective、artifact、execution、answer、history、receipt 独立行 | 来源正文和报告存 ContentStore |
| WebSessionBackend / CLI snapshots | conversation_sessions + 按序 conversation_messages；usage、模型、过滤后的 tool metadata | Markdown 导出为显式文件导出 |
| 子代理与压缩 | 同一会话表，subagent/context_archive channel；父子会话外键 | 压缩 JSON 是可删除的备份投影；恢复权威数据来自数据库 |
| OperationStore | tool_runs/steps/operations/attempts/api_attempts | 大结果收据正文存 ContentStore |
| SessionFiles | session_files 元数据和 content_objects 引用 | 附件、报告、解析结果和下载缓存 |
| Dispatch audit | research_dispatches 批次及结果 | 文件锁仅协调当前宿主进程生命周期 |

所有实体使用 workspace/session 复合身份；会话头保留轻量状态，复杂扩展字段是单条实体的 JSONB。数据库没有整个 ResearchMemory 大对象。history 中的报告正文也写入 ContentStore，仅存不可变引用；读取时重建原有 history 序列化内容。加载在一致事务内重建现有领域模型，并运行原有验证。

一次研究修改在同一 PostgreSQL 事务内锁定会话头、校验 operation fingerprint 和 expected_revision、更新实体、revision、history 及 receipt。重复 operation 返回原回执，不同 fingerprint 拒绝。事务上下文仅在同一 asyncio Task 内复用，子任务获得独立事务。工具账本通过短事务的工作区锁串行化资源准入，执行本体不长期持有数据库锁。

## 本地启动

安装锁定依赖：`uv sync --frozen --extra dev --extra web --extra eval`。

```bash
export RESEARCHX_POSTGRES_PASSWORD='<本地开发口令>'
docker compose -f compose.postgres.yml up -d
export RESEARCHX_DATABASE_URL='postgresql+asyncpg://researchx:<URL编码口令>@127.0.0.1:5432/researchx_dev'
uv run python -m researchx.storage.migrate
uv run rx web
```

不要把连接串写入仓库或提交 `.env`。生产使用独立最小权限运行账号；迁移账号才需要 DDL 权限。HTTP 健康检查验证数据库连接与 Schema 版本，失败返回 503。启动缺少连接或 Schema 时快速报错，不创建本地业务数据库。

连接池：`RESEARCHX_DB_POOL_SIZE` 默认 5，范围 1..100；`RESEARCHX_DB_MAX_OVERFLOW` 默认 5，范围 0..100；池等待和命令超时 30 秒，连接超时 10 秒。请求取消传播并回滚事务；应用关闭先收束控制器再释放池。

迁移是 Alembic `0001_postgresql` → `0002_scoped_relations` → `0003_child_sessions`。第一版完整建表；第二版补充关系、查询列、索引和约束；第三版加入父子会话外键。迁移命令以 PostgreSQL advisory transaction lock 防止多个部署并发升级。禁止自动降级删除生产数据；回退使用已验证备份和对应应用版本。

## ContentStore

默认 `RESEARCHX_CONTENT_BACKEND=local`，根目录为数据目录下 `objects/`，可由 `RESEARCHX_CONTENT_ROOT` 显式指定。键由工作区和 SHA-256 组成；目录 0700、文件 0600，拒绝越界与符号链接，原子写入并校验 hash。文件先写完整、校验，再在数据库事务中登记引用。事务失败可能留下无引用对象，不会提交缺失正文的引用，也不删除旧对象。

会话中的图片编码与超过 64 KiB 的 text/tool-result block 使用 ContentStore 引用，加载时校验作用域和 hash 并还原原有序列化内容；消息顺序与其他元数据仍由 PostgreSQL 管理。

下载、解析及大工具输出的可读路径是校验后的缓存；不能将缓存或旧 state/session JSON 用作数据库故障时的恢复后端。

S3 兼容部署安装 `uv sync --frozen --extra web --extra s3`，设置：

```bash
export RESEARCHX_CONTENT_BACKEND=s3
export RESEARCHX_S3_BUCKET=researchx-private
export RESEARCHX_S3_PREFIX=objects
# 使用 AWS 标准凭据链 / 工作负载身份，勿将密钥写入配置或日志。
# 私有 MinIO 可显式设置 RESEARCHX_S3_ENDPOINT；AWS 可省略。
```

支持 `AWS_DEFAULT_REGION`；条件 PUT 避免覆盖，重复内容读取校验。桶应为私有并限制运行身份访问前缀。网络/S3 失败直接失败，没有本地回退。S3 已有离线 adapter 测试，真实服务的 IAM、TLS、条件 PUT 兼容性需部署环境验证。

PostgreSQL 和共享对象存储并不把本地 MEMORY.md、文件工具、SSE 活动连接变成分布式系统：当前工作台仍使用一个 Web 控制进程；多实例必须共享真实工作区并把同一工作区路由到同一控制进程。不要仅增加 uvicorn workers。此改造不引入分布式调度或第二套 Agent Loop。

## 一次性导入

1. 停止旧版本，复制整个旧数据目录（含 SQLite WAL/SHM、来源与附件），保留只读备份；同时备份 PostgreSQL 和内容存储。
2. 初始化目标数据库并确认所有原工作区的规范绝对路径。workspace_id 由规范路径生成，不能随意重命名路径。恢复现有项目时也须保持原数据目录配置，Research Runtime 的工作区目录身份包含原研究目录；更换挂载位置须先做明确的路径映射和工作区恢复。
3. 默认只扫描，未配置数据库也可运行：

```bash
uv run python -m researchx.storage.legacy_import \
  --source /backup/researchx-data --workspace /original/project \
  --report /private/import-plan.json
```

4. 检查报告，每个无法映射工作区、损坏记录、缺失正文或跨源根引用均须处理。支持多个 `--workspace`；无作用域的旧 API/工具内容需唯一显式工作区，无法确认的会话标记 legacy-unattributed，不能猜测用户身份。
5. 使用同一参数加 `--apply` 执行。源文件永远不修改/删除。发现阶段存在错误时不开始任何写入；应用阶段按输入事务提交并报告失败，允许修复后再次运行。

覆盖 research state、Web/CLI session、附件 manifest、dispatch、子代理 transcript、context archive、tool artifacts、SQLite runs/steps/operations/attempts/api_attempts 及其正文。仅对已知旧 schema 答案格式做显式转换，并在报告列出；不把损坏数据替换为空会话。

导入账本以来源路径唯一约束和内容指纹防重复：相同输入跳过，变化的已导入文件拒绝覆盖。每次写入重读核验，研究状态完整比较并检查内容 hash。SQLite 先复制数据库及 sidecars 并验证原文件未变化，再只读打开复制品，避免修改原 SHM。工具操作本地 ID 增加作用域，外部 idempotency key 保持原值；遗留 running 写操作标为 uncertain，需要核验后才能继续。

报告是私有 JSON：分类 discovered/imported/skipped/failed、作用域、消息/实体/状态计数、转换及诊断。不要把报告上传公开系统。CLI 同时输出统计供人工阅读。实际生产导入必须由操作者明确指定目标库；测试库中的合成数据导入不等于用户历史已迁移。

## 恢复、留存和备份

状态仍区分 prepared/running/succeeded/failed/partial/uncertain/cancelled/blocked。成功回执复用正文，不再次执行；只有已确认无副作用且契约允许的失败才能重试。失联外部写入不能当失败重放。未知远程 owner 不会因本机看不到 PID 就被抢占，需人工核验外部状态。数据库不可用不会改变其他 owner 的状态。

对已结束且 usage 已确认的 API attempt 提供有界清理；不自动清理 unresolved、uncertain、partial 操作。研究 history/receipts 不自动删除。内容对象暂不自动 GC：先审计所有表引用并保留备份窗口，才能离线删除无引用对象，禁止按旧文件目录直接清理。

备份顺序：停止写入或取得协调快照，备份 PostgreSQL（例如 `pg_dump --format=custom`），备份完整内容对象和工作区 MEMORY.md，再验证恢复到隔离数据库/内容目录。恢复后执行 Schema 检查与完整性测试；旧 JSON/SQLite 只作手动迁移输入，不能作为运行期 fallback。数据库和对象存储备份需要同一恢复时间窗口。

## 开发验证

测试必须设置 `RESEARCHX_TEST_DATABASE_URL` 指向名称 `test_*` 或 `*_test` 的已迁移专用 PostgreSQL。测试拒绝开发/生产命名数据库；没有 DB 时失败，不 skip 或替换 SQLite。

```bash
RESEARCHX_DATABASE_URL="$RESEARCHX_TEST_DATABASE_URL" uv run python -m researchx.storage.migrate
uv run pytest --collect-only -q
uv run pytest -q
uv run python tools/check_types.py
uv run ruff check src tests tools evals hatch_build.py
uv run ruff format --check src evals tools/check_types.py hatch_build.py
cd frontend/web
npm ci
npm run build
npm run test:e2e
```

CI Python 3.10/3.11、Web E2E、沙箱验收各有真实 PostgreSQL service。E2E 子进程还需 `RESEARCHX_DATABASE_URL`。真实付费模型评测不属于默认验证。安装 smoke 需打包前端资源和迁移文件，并在干净环境验证 CLI、插件/Skill、文档解析与数据库会话读写。

## 嵌入 Runtime 的异步边界

Web lifespan 和评测 CLI 已管理连接池。自定义入口在最外层使用 `async with database_lifespan():`，在此范围内构建、启动及关闭 Runtime；不要在工具内临时创建事件循环。`ResearchStore.load/apply/capture`、Repository 修改、CompletionPolicy 检查、会话读写以及依赖状态的 `ToolExecutionContext.resolve_path/workspace_runtime` 现在需要 `await`。纯 Pydantic 验证保持同步，工具名称与输入 Schema 不变。

研究视图使用单条 SQL 的 MVCC 快照读取；普通上下文刷新不持有写锁。仅缓存固定的参数化 SQL 表达式，不缓存查询结果或研究状态。写入继续显式锁定会话头并提交 CAS、实体、history 和 receipt。内部 `research.control` 资源仅影响对应会话；带副作用 Hook 的 mixed 操作和外部写操作仍保留资源冲突保护。

Skill 沙箱只输出候选文件，宿主使用异步 `export_registered` 或现有 Bash 导出登记路径验证并提交引用。独立脚本不能凭 `--session-dir` 写数据库；直接嵌入的导出调用须提供当前 PostgreSQL ResearchStore。沙箱不接收数据库或 S3 凭据。
