# 目录与职责迁移

> 本文记录目录迁移阶段的边界。后续版本已将业务持久化切换到 PostgreSQL；本文提及的会话 JSON 是历史实现，当前部署和恢复请参阅 [PostgreSQL 说明](POSTGRESQL.md)。

本次以本地 `researchx` 包和已完成的 FastAPI SSE 实现为基线。不会恢复旧的
`openharness` 包名。研究任务、权威研究状态及 Runtime 继续位于现有 `state/`；
原暂存的 `research/` 已在后续按职责拆分到 `contracts/`、`workspace/`、`engine/`、`config/`
（见下方「research/ 职责拆分」）。

## 最终职责

```text
src/researchx/
├── api/                  # 模型与外部检索适配，含 search_types、tavily_search
├── config/               # paths、settings、sites（来源目录）
├── contracts/            # models（研究数据契约）、values（金额口径）
├── engine/               # 查询引擎、消息、子代理与 planning_support（规划适配器）
├── plugins/              # 插件加载与 research_script_support
│   └── bundled/          # 原插件与 Skill 资源位置、ID 保留
├── security/             # network_guard、redaction
├── state/                # 原研究状态、任务、存储、CompletionPolicy 与 Runtime
├── storage/              # filesystem、file_lock
├── workspace/            # session_files、paths、documents、exports；不依赖 FastAPI/SSE
├── services/
│   ├── context/          # budget、snapshots、sources、token_estimation
│   ├── execution/        # tool_execution、operations、outputs、async_timeout、shell
│   ├── sessions/storage.py
│   ├── message_chunks.py
│   └── compact/          # 保留现有压缩实现
├── tools/                # 扁平工具实现 *_tool.py，另有 __init__、base、contracts
└── web/                  # app、events、runtime、workspace、session_view、redaction 等
```

## research/ 职责拆分

原先暂存的 `research/` 目录已按真实职责拆出，未保留兼容 shim：

| 原 research/ 文件 | 现位置 |
|---|---|
| contracts.py | contracts/models.py |
| values.py | contracts/values.py |
| documents.py | workspace/documents.py |
| exports.py | workspace/exports.py |
| planning_support.py | engine/planning_support.py |
| sites.py | config/sites.py |
| __init__.py | 随目录一并移除 |

拆分只移动位置并更新导入，未修改工具名称、Schema、权限、报告输出或存储协议。
文档解析命令相应改为 `python -m researchx.workspace.documents`；
`tests/test_directory_migration.py` 断言 `research/` 包不再存在。

## 工具迁移记录

以下旧路径仅用于审计迁移，不作为兼容入口：

| 原 tools 文件 | 当前文件 |
|---|---|
| research/planner.py | planner_tool.py |
| research/replanner.py | replanner_tool.py |
| research/project.py | research_project_tool.py |
| research/planning.py | ../engine/planning_support.py（共享适配器，不是工具） |
| retired.py | contracts.py 内的 RETIRED_TOOL_NAMES |
| research/__init__.py | 移除冗余包标记 |

其他具体工具已经符合 `*_tool.py`，保留文件名。迁移没有修改工具公开名称、描述、
输入 Schema、Contract、权限或业务重试语义。迁移前的完整注册快照保存在
`tests/fixtures/directory_migration/tool_registry.json`；测试逐项比较两个模式下的
17 个内置工具，以及 MCP 资源工具和一个模拟动态 MCP 工具，共 20 项。

## 原 utils 的完整归属

此表记录已移除的旧目录；没有兼容包或导入 shim。

| 原 utils 文件 | 当前归属 |
|---|---|
| fs.py | storage/filesystem.py |
| file_lock.py | storage/file_lock.py |
| async_timeout.py | services/execution/async_timeout.py |
| redaction.py | security/redaction.py |
| network_guard.py | security/network_guard.py |
| session_files.py | workspace/session_files.py |
| research_documents.py | workspace/documents.py |
| research_exports.py | workspace/exports.py |
| research_script_support.py | plugins/research_script_support.py |
| research_sites.py | config/sites.py |
| research_types.py | contracts/models.py |
| research_values.py | contracts/values.py |
| search_types.py | api/search_types.py |
| tavily_search.py | api/tavily_search.py |
| shell.py | services/execution/shell.py |
| helpers.py | get_data_path → config/paths.py；safe_filename → workspace/paths.py；split_message → services/message_chunks.py |
| __init__.py | 移除空包标记 |

`contracts/models.py`（原 `research/contracts.py`）是 Skill 计算模型的共享契约，未合并进权威状态模型，避免改变
存储协议和产生导入循环。helpers 的函数按真实职责拆分，保留原实现和行为，未创建
新的杂物目录。对应文件锁、网络、shell、文件名和文本分块测试也按职责重新归组，
原有断言完整保留。

## 工作区与 Web 边界

`workspace/session_files.py` 管理附件解析、文件清单、产物登记和路径隔离。
`workspace/paths.py` 提供文件名规范化。该层不持有浏览器连接和 HTTP 状态。

`web/workspace.py::WebWorkspace` 保留 Web 会话连接表、执行锁、命令去重、删除标记、
文件操作状态及恢复投影。它使用通用工作区资源，未搬入通用 workspace。
`web/session_view.py` 从持久化消息生成前端视图，`web/redaction.py` 负责浏览器输出及
跨 chunk 脱敏，共享凭据辅助函数位于 `security/redaction.py`。

`web/app.py` 继续装配 FastAPI 路由、middleware、Host/Origin 检查及生命周期；
`web/events.py` 保留 SSE 编码、有界通道、心跳和断连清理；`web/runtime.py` 保留原执行
控制器及 Agent 事件转换。`GET /api/sessions/{session_id}/events` 使用 `data: <JSON>\n\n`
的 UTF-8 SSE，先发送含持久化快照及 connection_id 的 ready；
`POST /api/sessions/{session_id}/commands` 使用 `X-ResearchX-Connection` 令牌，受理返回
202，验证/冲突返回 4xx。连接令牌、submit/cancel/steer/
response、202 ack、事件字段和排序均未改动。断连仍取消运行、保存部分内容并清理；
重开会话恢复快照，不重放消息；不承诺事件 replay。重规划等待旧任务收束，权限提示
经 response 命令回答。实际接口实现在 [FastAPI 路由](../src/researchx/web/app.py)。

## 动态入口与兼容性

当前文档解析命令为：

```bash
uv run python -m researchx.workspace.documents --input report.pdf --output-dir parsed
```

项目自带 SKILL.md、Python 脚本、评测 fixture 的模块白名单、CLI Tavily 凭据入口、
测试 monkeypatch 和 wheel smoke 已同步新模块位置。插件和 Skill 的资源目录、ID、
模板及业务脚本算法未更改。安装包继续提供 `rx`、`oh`、`openh` 三个入口。

当前沙箱使用包根的文件系统读取规则，未发现单独硬编码旧 utils 名称的模块白名单；
迁入的新模块仍位于同一包根。该策略保持原样，由实际 SRT/Docker 执行验收验证。

外部自定义脚本若直接导入原私有模块，需要按照上表修改。旧 utils 导入明确不可用；
没有隐式动态重定向。Web runtime 使用拆出的 redaction/session_view 函数，仍只有一份
实现。上下文、执行和会话服务迁入子包后，原私有导入路径同样需更新。

不引入新 Agent Loop、Registry 或工作流框架。压缩模块和现有 oauth 包保留位置，
不为目录外观进行额外重命名。原会话 JSON、ResearchStore、回执、MEMORY.md 和恢复
语义保持不变。验证命令、结果及既有失败见 [本轮验证记录](testing/directory-migration-validation.md)。
