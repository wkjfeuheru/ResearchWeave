# 单次请求的六分类上下文预算

`Settings.context_components` 控制组件预算；旧配置缺少该字段时自动加载默认值。关闭组件策略仅关闭局部限制和额外历史压缩触发，原有全局容量检查仍启用。该功能不累计 Run 的费用、调用次数或 Token。

| key | 来源 | 目标份额 | 局部硬上限份额 |
| --- | --- | ---: | ---: |
| `system_prompt` | 系统规则、权限/模式/推理规则、工具 Schema、轻量 Skill 目录 | 15% | 30% |
| `conversation_history` | 历史对话、工具调用、普通工具结果 | 30% | 60% |
| `memory` | 实际召回的研究事实、证据、推理、结论、冲突，以及工作区记忆正文 | 10% | 20% |
| `dynamic_context` | Skill 正文、按需 references、模板、环境、任务/计划快照、显式动态资源 | 20% | 40% |
| `user_message` | 当前真实用户指令及图片问题/描述文本 | 15% | 100% |
| `other` | 图片估算、协议及序列化开销、正向对账差额 | 10% | 100% |

全局输入上限仍为 `context_window - max_output_tokens - ceil(context_window * 0.05)`。目标值向下取整，可借用空闲目标额度；局部硬上限与全局硬上限同时生效。未知模型仍须显式配置窗口。

配置示例（同现有 settings 文件持久化，不改变 profiles 或 research_memory 的迁移）：

```json
{
  "context_components": {
    "enabled": true,
    "target_shares": {
      "system_prompt": 15,
      "conversation_history": 30,
      "memory": 10,
      "dynamic_context": 20,
      "user_message": 15,
      "other": 10
    },
    "max_shares": {
      "system_prompt": 30,
      "conversation_history": 60,
      "memory": 20,
      "dynamic_context": 40,
      "user_message": 100,
      "other": 100
    },
    "max_tokens": {"memory": 4000}
  }
}
```

两类份额都必须完整包含六个键，使用 0–100 的整数；目标份额总和必须为 100。`max_tokens` 可仅覆盖部分键，使用非负整数，最终仍受全局输入上限约束。

## 来源与对账

`ConversationMessage.context_origin` 和请求中的当前用户消息 ID 区分真实用户输入、内部续跑、隐藏 runtime 与压缩摘要。`TextBlock.context_component` 标记加载附件等文本来源；工具结果的 `result_metadata.context_component` 标记来源，普通结果默认 H。Skill 调用计入 H，成功加载的正文、选中 Skill 根目录内的 reference 读取结果计入 D。研究记忆读取计入 M；计划/任务回执计入 D。

`RuntimePrompt` 与消息的 `runtime_context_manifest` 使用字符区间标记来源，正文只存储一次。这些可选字段可随旧会话/快照反序列化，并且不会进入 Provider payload 或用户可见文本。新研究召回对已知 JSON 字段建立精确区间；记忆使用规则仍为 S，计划/目标/控制字段为 D。旧 runtime 优先解析已知研究/工作区 JSON 包；不能可靠识别的文本仅计入 D，并增加 `attribution_fallback_count`。

Provider 的 `prepare_request()` 仍定义真实发送表示，工具继续使用原有 `tools` 字段。预算冻结 payload、源消息和策略后统计；原有 `components` 低层统计保持原义。新增 `component_tokens`、`component_targets`、`component_max_tokens`、`component_overflows`、`wire_input_tokens` 与 fallback 计数。图片 base64、认证、HTTP headers 不做文本计数；图片和协议归 O，正向分项差额归 O。六分类之和等于最终输入估值，且不会小于原有 wire 估值。

## 超限与事务

- H 超目标（或更低的硬上限）可提前触发现有 microcompact / auto-compaction，继续使用快照、工具配对校验、候选验收与失败回滚。摘要子请求临时关闭局部组件限制，以完整读取待压缩历史；原有全局硬检查仍启用，压缩后的主请求重新统计并检查全部局部硬上限。
- M 自动召回额度为旧 `research_memory.injection_budget_tokens`、M 目标、M 硬上限的最小值。研究记录按现有优先顺序整条选择；工作区 Markdown 召回共享同一额度，放不下时整篇延后，文件不修改。关闭研究记忆时移除旧的自动召回包。
- D 超目标仅在请求副本中延后显式 `deferrable` 资源；当前环境允许延后，完整 Skill/参考资料等必需结果保持来源和正文。渐进式加载仍由现有 Skill / read_file 路径完成，不自动读取全部资源。
- S、U 可以超过软目标，但不会自动删字、删规则或删 Schema；超硬上限给出数字明细并保留原始输入。图片先沿用现有预处理，O 估算及协议开销保留。
- 每次变换重新准备、归因和对账；最终调用 `require_fit()` 后才提交主请求历史。失败不会提交候选变换。`tool_metadata["context_budget"]` 保存纯数字诊断，不包含原文或凭据。

Tokenization、协议与图片用量是容量估算，不代表账单的精确用量。未添加通用资源相关度排序器；当前 M 沿用研究存储的记录优先顺序，D 只延后明确允许延后的内容。

## 实现文件

| 文件（相对仓库根目录） | 职责 |
| --- | --- |
| `src/openharness/config/context_components.py` | 六分类定义、默认份额、配置校验及局部限制计算 |
| `src/openharness/config/settings.py` | 默认加载、旧配置兼容及持久化入口 |
| `src/openharness/engine/messages.py` | 可持久化来源字段、区间清单、真实用户及隐藏动态消息构造 |
| `src/openharness/api/client.py` | 请求携带策略和当前真实用户 ID，独立于发送 payload |
| `src/openharness/prompts/context.py` | Skill 目录/系统规则为 S，环境快照为 D |
| `src/openharness/services/context_sources.py` | 精确来源区间、旧格式 fallback、召回额度协调及替换旧记忆 |
| `src/openharness/services/context_budget.py` | 冻结 Provider payload、六分类统计、保守对账、局部硬检查及可选 D 延后 |
| `src/openharness/services/token_estimation.py` | 缺少 tokenizer 时维持保守估算 |
| `src/openharness/engine/query_engine.py` | 标记真实用户输入、恢复来源、更新规则/研究召回快照 |
| `src/openharness/engine/query.py` | 请求副本、额外 H 触发、重新计数、诊断报告及最终验收提交 |
| `src/openharness/services/compact/__init__.py` | 复用现有事务压缩，保留来源，候选局部/全局验收 |
| `src/openharness/research/store.py` | 按现有记录优先级整条选择 M，保留旧 prompt 接口 |
| `src/openharness/engine/subagents.py` | 子查询继承组件策略和记忆开关 |
| `src/openharness/tools/skill_tool.py` | Skill 正文结果标记 D |
| `src/openharness/tools/file_read_tool.py` | 选中 Skill 资源标记 D，绑定工作区的 MEMORY.md 标记 M |
| `src/openharness/tools/research_memory_tool.py` | 记忆读取标记 M，操作回执标记 D |
| `src/openharness/tools/research/planning.py`、`planner.py`、`replanner.py`、`project.py` | 规划输入、计划/任务快照标记 D |
| `src/openharness/tools/dispatch_subagents_tool.py` | 子任务输入标记 D、内部续跑来源 |
| `src/openharness/tools/__init__.py` | 清理其他工作区改动已经删除的工具所遗留的导入/注册，保持通用注册器可导入；没有恢复或新增这些工具 |
| `tests/test_services/test_context_budget.py` | 配置、Provider、来源、对账、借用/硬限制、压缩回滚、旧快照和真实工具结果转换 |
| `tests/test_research/test_store.py`、`test_workspace_memory.py` | 整条召回、旧额度及工作区共享 M 额度 |
| `tests/test_skills/test_progressive_loading.py` | 目录 S、加载正文/reference D、普通文件默认 H |

## 本次验证记录

最终相关回归 **425 passed**：

```bash
uv run pytest tests/test_services/test_context_budget.py tests/test_services/test_compact.py tests/test_engine tests/test_prompts tests/test_api tests/test_config tests/test_research/test_store.py tests/test_research/test_planning_tools.py tests/test_skills tests/test_research/test_workspace_memory.py::test_workspace_recall_shares_memory_quota_and_defers_whole_document -q
uv run ruff check src tests scripts
uv run python scripts/check_types.py
git diff --check
```

后三项通过。初始相关基线为 117 passed；最终合并回归扩展了测试范围。

`uv run pytest -q` 在当前工作区以四个收集错误中止：`test_investigation.py`、`test_core_tools.py`、`test_image_generation_tool.py`、`test_mcp_auth_tool.py` 仍引用由其他工作区改动删除的模块。

早一轮诊断命令为：

```bash
uv run pytest -q --ignore=tests/test_research/test_investigation.py --ignore=tests/test_tools/test_core_tools.py --ignore=tests/test_tools/test_image_generation_tool.py --ignore=tests/test_tools/test_mcp_auth_tool.py
```

该轮得到 **1027 passed、37 failed**，在长期没有进展后中断。失败包含独立工具执行重构导致的受保护工具重复注册、已删除工具、Hook 错误等；运行期间工作区继续变化，不能把此结果视为最终全量通过或固定的基线失败数。

包含整个 `tests/test_research/test_workspace_memory.py` 的一轮回归为 **308 passed、2 failed**：失败分别是旧测试调用已删除的 `notebook_edit` 和 `image_generation`。本次新增的工作区记忆额度测试包含在最终 425 项通过结果中。未回滚这些独立删除，也未删掉失败测试。
