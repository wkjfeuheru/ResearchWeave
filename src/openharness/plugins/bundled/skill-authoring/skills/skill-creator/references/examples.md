# 直接引用示例

以下仅演示资源定位，不提供业务内容：

- 确定资料要求的步骤直接引用 `references/standards.md`。
- 需要背景时直接引用 `references/background.md`，对照案例时引用 `references/examples.md`。
- 执行校验或转换的步骤直接引用已有 `scripts/<file>`，说明输入和用途。
- 形成输出的步骤直接引用对应的 `templates/report.md`、`templates/code.md` 或 `templates/table.md`。

文件内使用相对技能入口的路径；执行时根据 skill 工具给出的资源基目录解析为绝对路径。
