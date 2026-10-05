# 插件与技能规范

技能随插件分发：`<plugin>/plugin.json` 声明插件，
`<plugin>/skills/<name>/SKILL.md` 是技能入口。同一插件可以提供多个技能。
用户插件放在 `~/.openharness/plugins/`；工作区插件放在 `.openharness/plugins/`，
后者仅在 `allow_project_plugins=true` 时加载。包内插件使用相同 manifest 和启停机制。
`enabled_plugins` 按 manifest 中的插件名称控制启停。

兼容的用户技能目录为 `~/.openharness/skills/`、`~/.agents/skills/` 与 `~/.claude/skills/`；
兼容的工作区目录为 `.openharness/skills/`、`.agents/skills/` 与 `.claude/skills/`。
项目技能发现受 `allow_project_skills` 控制。新交付使用插件布局。

入口 YAML 必须包含 `name` 和 `description`，description 描述触发条件。
保留已有元信息；仅在明确要求模型不能自动调用时设置 `disable-model-invocation: true`。
不要在技能文件、参考资料、脚本或模板中放置密钥。

研究资料与执行指令分开，注明资料日期、单位、期间、计算和不确定性。
研究记录属于当前对话，使用研究记忆工具保存可审计的方法摘要，不保存模型内部思维。
不能宣称已配置实际不存在的数据服务。

验证 YAML、技能发现、插件启停，以及从不同工作目录定位并按需读取资源。
业务内容已实现时再验证代表性研究请求；仅搭骨架不声称通过业务验证。
新增脚本需实际验证其确定性操作，但不因为脚本出现在导航中就自动执行。
终端 slash commands 和插件 Agent 声明已停用；使用技能、研究工具、MCP 或通用 hooks。
