# 元数据与生命周期

入口声明skill_id、version、owner、permissions、required_tools、optional_tools、compatible_models、scope、status、published_at、deprecation。
compatible_models描述text/tool_calling等能力，不硬编码供应商。权限只声明需求，不代替运行时检查。scope默认current_conversation。
status为draft/active/deprecated/retired；草稿与退役不参与模型调用，deprecated需声明原因和替代技能。
content_hash由加载器计算入口及references/scripts/templates/assets相对路径与内容的SHA-256，排除缓存、测试、哈希字段自身，不手填。
新技能入口须有名称、Description、适用与禁用场景、输入要求、主流程、完成标准与失败处理。复杂内容直接链接references，确定性操作写scripts，输出结构写templates/assets，测试在测试目录独立维护。
