# 投研搜索与技能插件

`web_search` 默认 `scope=curated`，只返回来源目录内的网页。
`category` 可选 `all`（默认）、`policy`、`macro`、`disclosure`、`industry`、`news`。
结果优先官方政策、统计和披露，其次行业专业来源，最后财经媒体；同层保留搜索相关性顺序。
目录内结果不足或检索失败时不会自动扩展；模型可按研究需要使用 `scope=web` 保留原主题补充搜索，
或从官网导航。全网结果中未收录的站点标为目录外来源。
来源目录的分类并不核验事实，搜索摘要仍需通过原文核验。

来源目录以中国为主，兼顾海外官方资料。`web_fetch` 按最终页面域名使用同一目录分类，
允许读取目录外网页及用户提供的链接。现有搜索后端参数 `search_url`、
`OPENHARNESS_WEB_SEARCH_URL` 和代理配置继续生效。

## 配置来源目录

在 `~/.openharness/settings.json` 的 `web.research_sites` 配置增补条目。
`domain` 是不带协议、路径、端口或通配符的域名，允许真实子域名。
同域名条目覆盖内置值，未指定的字段保留内置值；`enabled=false` 禁用该条目。
新域名默认归类为行业专业来源、地区 CN；公司官网需明确添加。

```json
{
  "web": {
    "research_sites": [
      {"domain": "stats.gov.cn", "name": "官方统计"},
      {"domain": "caixin.com", "enabled": false},
      {
        "domain": "ir.company.example",
        "name": "公司投资者关系",
        "categories": ["disclosure"],
        "region": "CN",
        "tier": "official"
      }
    ]
  }
}
```

`categories` 使用上述类别值（不含 `all`）；`tier` 可选 `official`、`professional`、`media`。
内置目录和共用分类逻辑位于 `src/openharness/utils/research_sites.py`。

## 技能插件与分层资源

`src/openharness/skills` 保存技能解析、发现与注册框架。
原内置 `skill-creator` 由包内 `skill-authoring` 插件提供，使用普通插件启停机制，
可通过 SkillHub 或 `enabled_plugins.skill-authoring` 控制。插件资源随安装包分发。
项目中的 `.claude/skills` 开发辅助技能已移除；用户安装的兼容技能目录仍可发现。

```text
<plugin>/
├── plugin.json
└── skills/
    └── <skill-name>/
        ├── SKILL.md
        ├── references/
        ├── scripts/
        └── templates/
```

技能入口包含名称、Description、适用与禁用场景、输入要求、主流程、关键决策、完成标准、失败处理和直接资源导航。
参考资料保存大型规范、背景知识和典型案例；脚本承担可重复的确定性校验与转换；
模板定义报告、代码或表格输出结构。正文按步骤直接引用资源，避免复制与多层跳转。
`skill` 工具返回入口和资源绝对路径，不自动展开资源正文或执行脚本。

`skill-authoring/skills/skill-creator/templates/skill/` 是可复制的四层占位骨架，
仅作为新技能占位骨架，不注册为可调用技能。兼容原单文件技能格式；同名目录入口优先。
用户插件放在 `~/.openharness/plugins`，项目插件放在 `.openharness/plugins`，
项目插件仅在 `allow_project_plugins=true` 时加载。


## 四项正式投研插件

| 插件ID | 名称 | 输出 |
|---|---|---|
| financial-statement-analysis | 财报穿透解析 | 三大表、六项比率、勾稽、同比与附注缺口 |
| company-event-monitor | 舆情与公告监控 | 7天内事件、日期未知/窗口外资料、两种评分与渠道状态 |
| research-report-digest | 研报精读与摘要 | 逐篇简报、同年度/币种/指标/口径预测对照 |
| deep-investment-report | 深度投研报告撰写 | 八个章节、三年三情景简化利润预测、敏感性 |

四项默认启用，分别从下一轮生效，支持A股非金融企业。深度报告只调用已启用的前置技能；禁用插件不会被自动重新启用。
模型负责语义提取，公共 `utils/research_workflows` 校验与计算，插件 `scripts/run.py` 通过已有bash执行，不注册新的业务工具。
`templates/input.schema.json` 是输入契约，`input.json` 为待填示例；未知值为null并说明原因。
币种和金额单位不能隐式假定。数值、观点与结论使用原文URL/路径、页码或文本行位置；来源目录收录和脚本成功都不是事实已核验。

财报默认合并口径，母公司和重述分别标记；简化ROE使用归母净利润/平均期初期末归母权益，区别于披露的加权平均ROE。
季报、半年报不年化；分母为零、缺权益和负权益保留不可计算原因。校验不通过保留原值与容差，缺附注明细不推断事实。
事件监控是每次调用时执行，以上海时区标记截止时间，不启用后台定时任务。
研报预测与目标价保留发布时点及原机构口径，不能视为实际业绩或当前结论。
深度预测采用最新已结束完整财年的收入基期，所有假设明确历史/外部/分析者设定；不生成自主评级、目标价或完整预测三大表。

元数据包含skill_id、version、owner、permissions、required_tools、optional_tools、compatible_models、scope、status、published_at、deprecation。
加载器按入口与references/scripts/templates/assets的相对路径和内容计算SHA-256 content_hash，排除缓存、测试与哈希字段自身。
旧技能使用兼容默认值；draft/retired不会参与模型调用，deprecated显示替代说明。
模型兼容性按文本理解、工具调用能力描述；权限字段只声明需要的操作，实际执行仍走权限机制。

## 附件与导出

网页支持PDF/TXT/MD上传、选择本轮附件、展示解析状态和删除附件，每文件30 MB，每次最多10个、PDF最多1000页。
使用pypdf逐页提取布局文字和文档哈希；不做OCR，扫描件/损坏/加密文件显示缺口。布局文字不保证表格语义对应可靠，仍须模型对照原文。
公开PDF通过已有逐跳网络防护下载并限制解压后的响应大小；HTML入口仍用web_fetch导航。
本地文件与粘贴文本仍支持。资源按需分段读取，不自动将完整长文装进上下文。

每项保存Markdown、JSON、DOCX、XLSX，Excel包含已计算数值及公式说明，不依赖首次打开重算。
深度报告Excel包含基期、假设、预测及敏感性；各格式含能脱离会话识别的原始资料说明。
导入跨会话结果须重新登记来源，旧source_id/evidence_id不会直接通过导出校验。

会话附件：`POST/GET /api/sessions/{id}/attachments`、`DELETE .../attachments/{file_id}`；WebSocket提交使用attachment_ids。
产物：`GET /api/sessions/{id}/artifacts`、`GET .../artifacts/{file_id}/download`。
下载只接受服务器分配的文件ID，不能指定任意文件路径。附件、解析缓存和产物保存在会话研究目录，删除会话一并清理；服务重启可恢复列表。
SkillHub卡片名称、分类和调用示例来自plugin.json，详情展示入口、依赖、状态和内容指纹。缺少可选金融MCP可公开网页降级。

## 验证入口

固定合成材料在 `tests/fixtures/research_skills`，人工核对的勾稽与预期值写在同目录README。
后端专项覆盖未知值、单位、母公司/合并、零分母、负权益、跨页定位、事件窗口/冲突、研报可比性、预测和导出：

```bash
uv run pytest tests/test_research/test_skill_workflows.py tests/test_skills/test_plugin_resources.py tests/test_web/test_app.py
cd frontend/web
npm run build
npm run test:e2e
```

真实模型验收通过HTTP/WebSocket提交上传PDF及固定公告/研报材料，最后整合同会话结果；会调用真实服务并可能产生计费。
凭据保存在临时配置，结果写入指定目录，不修改个人配置：

```bash
uv run python tests/test_web/real_skill_eval.py --profile YOUR_PROFILE_ID --output .openharness/verification/skills/real
```

真实渠道不足须单独登记，成功抓取页面不代表投研质量验收通过。
实现遵循 [pypdf布局提取](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[python-docx导出](https://python-docx.readthedocs.io/en/latest/user/quickstart.html)和 [openpyxl公式说明](https://openpyxl.readthedocs.io/en/stable/simple_formulae.html)。
