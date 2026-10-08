---
name: replace-with-skill-name
description: 待填：技能能力与触发条件；当前仅为骨架。
skill_id: replace-with-skill-id
version: 0.1.0
owner: 待填写
permissions: []
required_tools: []
optional_tools: []
compatible_models:
- text
- tool_calling
scope: current_conversation
status: draft
published_at: null
deprecation: null
---

# 待填：技能名称

## 触发条件

待填：何时使用，以及所需输入。

## 主流程与资源导航

1. 待填：输入确认步骤；需要大型规范时直接读取 [规范](references/standards.md)。
2. 待填：主体任务步骤；需要背景时读取 [背景](references/background.md)，
   需要典型案例时读取 [案例](references/examples.md)。
3. 待填：确定性校验与转换步骤；业务函数和专属模型实现在本插件 `scripts/`，
   在本步骤直接引用按职责命名的脚本；公共基础能力可复用，不以单个运行入口代理业务。
4. 待填：交付步骤；按实际输出选择 [报告模板](templates/report.md)、
   [代码模板](templates/code.md) 或 [表格模板](templates/table.md)。

## 关键决策

待填：影响流程或输出的必要决策。

## 完成标准

待填：可检查的完成条件。本技能业务内容尚未实现，不能用于正式交付。

## 适用与禁用场景

待填：支持边界。

## 输入要求

待填：输入及资料定位要求。

## 失败处理

待填：可恢复错误、资料缺口与停止条件。
