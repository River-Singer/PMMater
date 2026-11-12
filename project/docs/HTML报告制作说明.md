# HTML 报告制作说明

写给维护报告的人和参与设计的 AI。目标是：任何人改版式或新增页面后，生成的 HTML 与 `demo/` 里的示例仍然是同一套样子。使用者直接打开报告文件，无需启动本地网页服务。

## 先记住五条

1. **HTML 由代码生成，不手改。** 包括 `demo/` 里的示例。要改样子就改渲染函数，再重新生成。
2. **颜色和字号只用设计 token。** CSS 里写 `var(--名称)`，不写色值。token 的唯一来源是 `pm_core/design_tokens.py`，导出文件是 [`references/report-design-tokens.json`](../references/report-design-tokens.json)。
3. **状态不能只靠颜色。** 每个状态同时要有文字标签、位置或纹理。
4. **核心信息在首屏。** 先给结论、日期和数字，再给明细；口径说明收进折叠块。
5. **改完要重建并核对。** 运行文末的三条命令，测试会拦住“改了代码没更新 demo”和“写了 token 之外的颜色”。

## 各 HTML 从哪里来

| 产物 | 命令 | 渲染函数 |
|---|---|---|
| 六页项目汇报 | `python -m pm_core.cli project-report --db … --out … [--kind summary\|plan\|review]`；`report --db … --out … [--snapshot …]` | `project_report.export_project_report` |
| 周一计划、周五验收 | `python -m pm_core.cli weekly-report --kind monday\|friday …`；`weekly-tick` | `weekly.generate_weekly_report`：在六页汇报上替换第 03 页并加抬头条 |
| 月度或周管理复核 | `python -m pm_core.cli management-review --period month --html …` | `operating.export_management_report` |
| 日期变更审议 | `python -m pm_core.cli schedule-audit … --html …` | `management_text.export_schedule_audit_report` |
| 飞书快照审议材料 | `python -m pm_core.cli feishu-report --config … --out …`（`--period month` 出月度复核） | 同上两种，另存同名 JSON |
| 虚构示例 `demo/*.html` | `python -m pm_core.demo_reports` | 以上全部，固定截止 2026-11-11 17:00 Asia/Shanghai |

所有报告共用 `project_report.REPORT_CSS`，输出是单个 HTML 文件，样式和脚本内联，不依赖外部资源。

## 制作流程

1. **读取事实**：本地库 `Store.data()` 或飞书读取快照；记录截止时间和时区。
2. **判定延期与范围**：`tracking.tracking_data(data, as_of)`。延期、风险、延期交付只在这里判定，渲染层不自行推断。
3. **管理读数**：`operating.management_review(...)`；日期变更用 `schedule_audit.audit_schedule_changes(...)`，没有历史时传 `None`，页面会写明“历史覆盖未知”。
4. **渲染**：上表中的渲染函数。文字一律经 `h()` 转义，链接经 `_link()`（只放行 http/https）。
5. **留档**：报告是生成时点的快照。`demo/weekly_reports/` 里的历史周报不重新生成。

飞书文档与 HTML 来自同一份事实：`management_text.schedule_paragraphs`（文档段落）和 `render_schedule_audit`（HTML）共用 `_audit_parts`，改措辞时两边一起变。

## 页面结构

第05页及月度复核在实际延期之后展示“每日巡检与逐级影响”和“延期风险与资源落实”：全项目读数、父级去重汇总、最小子任务及未落实资源卡片共用 `management_review.workflow`。HTML 与飞书段落通过 `management_text._workflow_parts` 共享事实和措辞。资源记录数与关联未完成叶子数分开；没有任务关联的资源列待定位，不能因缺关联而隐藏。来源内容须转义，链接继续使用安全链接渲染。

资源卡默认只展示缺口、责任人、DDL（未约定则“待确认”）、短影响和下一步。长证据、完整逐级路径、来源元信息与局限放进默认关闭的“证据与完整影响链”；摘要省略的原文必须在详情和飞书文档中完整保留。父级及逐叶补填明细继续折叠，不把整份每日补填清单铺在首屏。任务预计完成日、接口预计就绪日不能冒充补填或行动 DDL。

日常催办由智能体在项目群按责任人合并、逐项列动作和 DDL 并真实 @；HTML 用于审议和按需追溯，不显示虚构的“已发送”状态或无实际工具支持的发送按钮。

| 页 | 首屏先给什么 | 之后 |
|---|---|---|
| 01 项目概览 | 结论句、完成日期卡（原计划 → 当前预计、偏差）、6 个指标卡 | 当前最需关注（最多 5 条，P0 优先，再按逾期天数）、状态环形图、优先级分布 |
| 02 进度 Waterfall | 范围切换（本周按日 / 完整项目按 WK）、汇总行 | 展开到任务与 Todo，点击任务行看日期、验收与补救依据 |
| 03 开发与验收 | 4 个指标卡 | 待交付表、已确认表、交付物验收。周报把本页换成“承诺与验收结果” |
| 04 会议与 Todo | 会议结论 | 关联待办表 |
| 05 延期与决策 | 指标条：偏差数、已说明、已有措施、已落实、已登记审议 | 每个偏差一张卡，待决定事项与风险、P0、人员资源、阶段检查、日期变更审议 |
| 06 接口与文档 | 接口表 | 文档卡片、折叠的通讯录 |

月度复核和日期变更审议是单页，用同一套页头、指标卡和卡片。

## 设计 token

- **来源**：`pm_core/design_tokens.py`。改值后运行 `python -m pm_core.design_tokens`，它会重写导出的 JSON；重新生成报告后，所有 HTML 使用同一套变量。
- **导出文件**：`references/report-design-tokens.json`，每个 token 带 `$value`、`$type`、`$description` 和对应的 CSS 变量名，另有 `usage_rules`。让其他 AI 参与设计时，把这个文件和本说明一起交给它。
- **怎么用**：CSS 里写变量名；需要内联颜色的图表（环形图、优先级条、承诺条）在 Python 里用 `design_tokens.chart_colors(...)` 取值。

| 分组 | 变量 | 用途 |
|---|---|---|
| 底色 | `--page` `--card` `--soft` `--soft-2` `--hover` `--band` `--band-strong` | 页面、卡片、次级底、槽位、悬停、Waterfall 汇总行 |
| 描边 | `--line` `--line-2` `--line-strong` `--neutral` `--grid` | 外框、行分隔、中性强调、中性填充、网格线 |
| 文字 | `--ink` `--ink-2` `--muted` `--faint` `--link` | 数值与标题、正文、说明、缺失提示、链接 |
| 状态 | `--critical` `--serious` `--warning` `--good` `--info`，各带 `-ink`（文字）和 `-bg`（标签底） | 见下表 |
| Waterfall | `--wf-baseline` `--wf-acceptance` `--wf-remediation` `--wf-forecast-filter` `--wf-cutoff-opacity` | 原计划线、验收节点、补救节点、预测条加深、截止线透明度 |
| 其他 | `--cap-ops` `--type-feature` 等工作类型色、`--radius` `--font` | 容量条、Waterfall 预测条、圆角、字体 |

### 状态色是保留色

| token | 只表示 | 常见位置 |
|---|---|---|
| `--critical` | 已延期未完成、P0、需优先核对 | 指标卡色条、延期标签、Waterfall 事实延期条 |
| `--serious` | 延期交付（晚于对照日期才完成） | 指标卡色条、偏差卡侧边 |
| `--warning` | 延期风险、待确认、资料缺口 | 斜纹标签、风险卡顶边、Waterfall 斜纹条 |
| `--good` | 已确认完成、已验收 | 进度条、已验收标签、Waterfall 实际完成条 |
| `--info` | 进行中、待决定事项、项目投入 | 指标卡色条、容量条 |

不要把状态色当作普通配色或图表系列色。数值本身用 `--ink`，状态由旁边的色条、圆点或标签承载。

### Waterfall 标记

| 标记 | 画法 |
|---|---|
| 原计划 | 上方细线，`--wf-baseline` |
| 当前预测 | 中间彩条，工作类型色加 `--wf-forecast-filter` |
| 事实延期 | 中间实色条，`--critical` |
| 延期风险 | `--warning` 斜纹 |
| 实际完成 | 下方实色条，`--good` |
| 延期交付 | `--good-bg` 底加 `--critical` 双线框 |
| 验收节点、补救节点 | 菱形，`--wf-acceptance`、`--wf-remediation`（预期空心，实际实心） |
| 数据截止线 | `--ink` 竖线，不是状态色 |

画布是浅色中性底：任务行 `--card`，父任务行 `--soft`，分组行 `--band`，项目根行 `--band-strong`。不使用纯黑或带蓝色调的大面积背景。

### 字号与圆角

字号只用 token 文件 `typography.scale` 里的档位，正文不小于 12px，11px 只用于图内标注。圆角见 `radius`：主卡片 `--radius`，卡片和面板 12px，按钮和提示块 8px，标签用胶囊形。

## 组件与写法

| 需要 | 用什么 |
|---|---|
| 一个数字加一句说明 | `_kpi(label, value, tone=…, sub=…, meter=…, goto=…)`，`tone` 取 `neutral/critical/serious/warning/good/info` |
| 口径、定义、图例说明 | `_method_note(text, summary)`，默认折叠，打印时隐藏 |
| 延期状态标签 | `_delay_badge(facts)`，类名 `delay-badge` 加 `overdue/risk/late_done/on_track` |
| 优先级 | `_badge(work)`，类名 `priority` |
| 缺失的事实 | `<span class="missing">待确认</span>`，不留空白，不编造 |
| 一般状态标签 | `chip` 加 `warn/ok`，或 `acceptance`、`status-chip` |
| 任务表 | `work_table(items, empty, show_next)` |

新增区块时先看能否复用这些组件；确实需要新样式时，把规则加进 `REPORT_CSS`，颜色仍用 token。

## 文案与事实

- 页头写明数据截止时间和时区；演示数据标“虚构演示项目”。
- 条数不等于完成百分比，出勤不等于投入；这类口径放进折叠说明，不在每张图旁重复。
- 延期事实不会因为填了补救时间而消失；补救落实和任务完成分别确认。
- 日期变更只陈述前后值和来源，不推断修改意图；会前审议版和会后记录版分别保留。
- 没有登记的内容显示“待确认”“待指定”“未登记审议事项”。

## 不能改名的结构标记

测试、脚本和周报拼接依赖这些标记：`data-page`、`data-section`、`page-foot`、`<nav>`、`data-goto`、`data-trace-view`、`data-trace-scope`、`data-trace-expand`、`data-work-id`、`data-node-kind`、`data-group-id`、`data-delay-id`、`data-delay-state`、`data-week`。周报靠 `data-section="2"` 到 `page-foot` 之间的内容替换第 03 页，并在 `<nav>` 前插入抬头条。

## 文件内交互

页签导航、WBS 层级展开、任务详情和时间范围切换由 HTML 内嵌脚本完成。报告可通过浏览器直接打开本地文件，保持快照数据，不请求本地服务。数据维护由智能体命令或 Excel 导入完成，随后重新生成报告。

## 修改步骤与验收

```
python -m pm_core.design_tokens          # 改过 token 时：重写设计 token 导出文件
python -m pm_core.demo_reports           # 用当前代码重建 demo/
python -m unittest discover -s tests     # 全量测试
```

前两条命令加 `--check` 只核对不改写。然后直接打开生成的 HTML 逐项看：

- 1440、900、420 三种宽度下无横向溢出，首屏能看到结论和关键数字。
- Waterfall 切换两种范围、展开全部、收起、点开一条任务都正常。
- 浏览器控制台没有报错；打印预览为 A4 横向，每页一节。
- 新增或改动的状态同时有文字标签。
- 报告约定有变化时，同步更新 `.trellis/spec/frontend/project-report-contracts.md`。

## 常见岔子

| 做法 | 后果 | 应该 |
|---|---|---|
| 直接改 `demo/*.html` | 下次生成被覆盖，示例与真实输出不一致 | 改渲染函数后运行 `demo_reports` |
| 在 CSS 里写新色值或新字号 | 测试失败，页面出现第二套配色 | 用现有 token；不够用先在 `design_tokens.py` 增加 |
| 用红、黄、绿做普通装饰或系列色 | 读者误以为是状态 | 装饰用中性色，系列用 `chart` 里的颜色 |
| 只用颜色区分状态 | 色弱读者和黑白打印无法分辨 | 加文字标签、位置或纹理 |
| 给 Waterfall 换深色或蓝色底 | 与全页不协调，不符合现行约定 | 保持浅色中性画布 |
| 在渲染层重新判断是否延期 | HTML 与文档、命令读数不一致 | 只读 `tracking_data` 的结果 |
| 把口径说明铺在每张图旁 | 文字淹没核心数字 | 用 `_method_note` 折叠 |
| 重新生成历史周报快照 | 历史留档被改写 | `demo/weekly_reports/` 保持原样 |
