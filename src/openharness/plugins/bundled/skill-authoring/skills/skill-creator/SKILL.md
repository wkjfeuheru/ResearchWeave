---
name: skill-creator
description: Create, improve, and verify OpenHarness skills. Use when a user asks
  to turn a research workflow into a skill, update a SKILL.md, design trigger behavior
  or check that a skill loads correctly.
skill_id: skill-creator
version: 0.2.0
owner: OpenHarness
permissions:
- file_read
- file_write
- shell_execution
required_tools:
- read_file
- write_file
optional_tools:
- bash
compatible_models:
- text
- tool_calling
scope: current_conversation
status: active
published_at: '2026-10-05'
deprecation: null
---

# 创建和维护技能插件

## 触发条件

用户要求创建、修改、组织或验证 OpenHarness 技能时使用。先读取用户目标和已有技能。

## 主流程与资源导航

1. 确定授权范围、技能名称、触发条件和交付物。选择插件位置时查阅
   [插件规范](references/standards.md)。现有插件直接修改，不另建重复技能。
2. 按正文、参考资料、脚本、模板四层组织。分层职责查阅
   [分层背景](references/background.md)，对照
   [资源引用示例](references/examples.md)；将细节直接链接到使用它的步骤。
3. 新建技能时复制 [入口骨架](templates/skill/SKILL.md) 及同目录的资源骨架，
   替换元信息和必要占位。需要插件入口时使用 [manifest 模板](templates/plugin.json)。
4. 按用户要求填写或保留骨架。仅搭骨架时不发明业务方法、计算脚本、报告字段或表格结构；
   有实际重复、确定性操作时才添加按职责命名的脚本，业务函数和专属模型放在该插件的 `scripts/`。
5. 按 [插件规范](references/standards.md) 验证发现、启停和资源定位，报告保存位置与验证结果。

## 关键决策

正文仅保留触发条件、主流程、关键决策、完成标准和资源导航。
参考资料承载大型规范、背景知识与典型案例；脚本承载可重复的确定性校验与转换；
模板承载输出结构。仅在当前步骤需要时读取或执行资源，不默认加载全部文件。
跨技能共用解析、文件写入等基础能力可复用公共 `utils`；不要用单个 `run.py` 代理集中存放的业务实现。
用户明确要求先搭骨架时，资源仅说明用途、保留待填位置。

## 完成标准

技能通过插件 manifest 被发现，启用后可调用、禁用后不可调用；入口和直接引用的资源路径有效。
正文可一次读懂任务骨架，资源不重复正文，无深层链式引用。
若只交付骨架，明确说明尚未实现业务内容，不能宣称可完成正式研究。

## 适用与禁用场景、输入要求及失败处理

用于创建或维护技能插件；需用户目标或已有插件位置。不代替实际业务数据采集。
新增技能使用完整元数据和独立测试；参阅 [元数据与生命周期](references/metadata.md)。
资源定位或依赖缺失时说明问题并修复可定位部分，不把占位骨架标为正式技能。
