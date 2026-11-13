# 飞书内部智能体接入与可选 HTTP 桥接

本版首选飞书内智能体。宿主需要配置知识/指令、实际工具和持久触发；标准 SKILL.md 并不天然等于 Aily 的导入格式，本包不伪造导入清单，也不依赖旧母版固定 token 或未随包交付的补号自动化。

新版母版的字段终态与旧导出包不同。接入前核对 [母版变更同步记录](../docs/飞书母版变更同步记录.md)：通讯录首列姓名且无旧编号/飞书通讯地址，WBS 编号和项目编号为公式展示值。不要为了套用旧配置恢复已删字段，也不要把展示值当成稳定身份。

## 原生工具接入

将 SKILL.md 作为工作指令，将相关 references 和字段字典加入知识材料。为一个项目副本配置以下能力，并在真实租户逐项验证：

| 能力 | 输入 | 必须返回/保留 |
|---|---|---|
| 多维表格读写 | 配置的 Base/表/字段、稳定记录身份与当前展示值 | 完整分页、原值、远端 record_id、修改字段、写后值、错误；禁止拿未读表当空表 |
| 群消息 | 已配置 chat_id 和已核实用户 ID、提醒事项 | 真正的 mention、消息 ID、每名成员发送记录；纯文本 @姓名不算通知 |
| 群回复与读取覆盖 | 成功提问消息/行动、实际回复及群消息读取窗口 | 原始发送人及映射身份、消息/问题关联、真实时间、完整分页/事件覆盖依据；能发消息不代表能核验未回复 |
| 日历 | 日历/事件 ID、时区、起止、组织者、参与者 | 日程与邀请分开结果；参与人是否接受须另查，不推断 |
| 妙记 | 登记的会议对应 minute_token | 元信息、摘要/待办、必要逐字稿、读取时点和来源定位 |
| 文档/附件 | 与 HTML 相同快照的报告、截止时间 | 文档 ID/链接、HTML 文件交付结果；保存前后版本 |
| 持久执行记录 | 项目、动作/提醒身份和处理状态 | 重启/事件重送后可查询；不能只存模型上下文 |

宿主触发入口：周一计划收集、项目工作日当天安排确认、10点情景巡检、17点问题/风险汇总、周五结果收集、例会会前/会后、月度复核与留档。周会遇假日按项目选定规则生成真实实例，不能把“每周一”自动当有效工作日。妙记生成事件只触发读取，不表示内容已获批准。宿主不提供事件工具时巡检已登记会议/链接。

原生工具执行也必须遵守 meeting-actions.md 的来源、精确匹配、权限、预读/回读与防重复契约；工具成功不等于完整业务成功。安装时用虚构副本验证：重复提醒、邀请部分失败、旧纪要与新人工值冲突、调休周末、未知年度、只读字段和删除引用。

通讯录先按邮箱或姓名+部门查找候选，唯一匹配后记录稳定身份；同名、同邮箱或信息不足时列待确认。`record_id` 用于记录关联，真实飞书用户 ID 用于 @ 和邀请，两者不能混用。新 WBS 由平台公式出号，父子关系使用实际父级关联；不手写公式、不补跳号、不启用第二套补号。公式依赖字段和状态类下拉保持原约定，其他下拉先登记修改日志。

## 可选 Python 桥接

当宿主能够执行 Python 或接入受控服务时，使用本包实际 HTTP 连接器。令牌由宿主提供 `FEISHU_ACCESS_TOKEN`（可以是获准范围的应用或用户令牌），令牌续期由宿主负责；不把令牌写入配置、对话、日志或样例。

```text
python -m pm_core.cli feishu-config-template --out workspace/feishu.json
python -m pm_core.cli feishu-pull --config workspace/feishu.json --out workspace/source.json
python -m pm_core.cli minutes-read --config workspace/feishu.json --token <已登记妙记令牌> --out workspace/minutes.json
python -m pm_core.cli agent-tick --config workspace/feishu.json --ledger workspace/runtime.sqlite3
python -m pm_core.cli actions-run --config workspace/feishu.json --ledger workspace/runtime.sqlite3 --file workspace/actions.json
```

接入 2026-11-11 母版时，先生成对应脚手架：

```text
python -m pm_core.cli feishu-config-template --profile mother-template-20261111 --out workspace/feishu.json
```

模板是**配置脚手架**，不是新母版完整映射，也不能生成后直接启用：填写项目、每张表的真实 table_id、字段名/类型/枚举/关联、群和成员白名单、时区、日历与决策身份。schema_version 的版本号是本地包约定；飞书业务表不必照搬 Excel version 字段。没有读取的数据表须映射，确实不用且确认没有业务数据的表才能列入 confirmed_empty_tables。不能为了通过检查把风险/运维等未读表标空。

- `tables.<table>.fields` 的键是 canonical 字段名，值为 `{name,type,writable}`；类型支持 text/number/checkbox/date/datetime/select/relation/person/link，以及只读文本结果的 formula。枚举用 `options` 对照，关联用 `table` 及 `many`；真正飞书人员字段用 person，并通过通讯录 feishu_user_id 映射为稳定人员 ID。
- 默认业务编号模式只用于真实存在稳定、唯一业务 ID 字段的表。新母版的姓名和公式展示编号不满足这个条件；采用稳定记录身份映射，并回读平台生成的显示值。不能再运行第二套补号。
- 配置 `enabled:false` 时实际动作不执行；省略 `--apply` 是预览。先核对预览，配置授权完成后启用并由宿主执行带 `--apply` 的命令，不要求每次催办另批准。
- `allowed_recipients` 是通讯录映射后的稳定 ID；people.feishu_user_id 是真实 open_id。项目负责人、决定人和允许参会人也须使用相同身份约定。当前桥接消息/邀请采用 open_id，其他身份类型须由宿主转换验证后再使用。
- `policy.organizer_id` 必须对应调用日历工具的实际授权组织身份。普通预约仅在 allowed_attendees 和 calendar_id 范围；删除、改期和重要字段修改需已登记决定人和依据。
- runtime.sqlite3 保存在持久存储，备份时与业务数据一起考虑。不要为一次失败换全新执行台账，否则会丢失防重证据。

HTTP 桥接已提供多维表格分页读取/新增/更新/删除，妙记摘要/文字读取，日历创建/修改与邀请，结构化群 mention，新版本飞书文档创建。真实租户权限、字段类型、应用可用范围、令牌续期、宿主触发和 HTML 附件交付仍需实际接入验证。已知失败默认总计最多 3 次尝试，超限/未知结果由 runtime-reconcile 记录真实查询依据后恢复；没有提供自动申请应用权限、自动安装 Aily 或后台常驻服务。

切换已有连接的身份映射前，先核对旧编号到远端记录的对应关系及历史台账。修改配置不会自动迁移旧行动、通知白名单或审计历史；未验证接续时保存旧档并明确迁移边界。离线 Excel 的稳定编号继续保留，不能将飞书公式编号规则直接套到离线关联键。

### 新母版的身份与字段配置

`mother-template-20261111` 为 `people`、`projects`、`wbs` 选择 `identity:"record_id"`。这三张表的本地 `id` 来自远端记录身份，不映射姓名、项目公式编号或 WBS 公式编号。其他表沿用默认 `identity:"field"`，仍需核实真实稳定编号字段。默认模式兼容旧配置，不会自动把已存编号切换为远端身份。

以下只是 WBS 表配置的一部分，不能作为完整可运行配置：

```json
{
  "identity": "record_id",
  "table_id": "填写该项目真实表ID",
  "display_field": {
    "name": "WBS编号",
    "type": "formula",
    "writable": false
  }
}
```

`display_field` 单独描述供人查看的字段；它不参与业务身份判定，也不会写回公式。`fields` 中的 `type:"formula"` 同样只读，不把它改成 text 来绕过写入限制。新增时不要提交 WBS 编号、项目公式编号或任何系统生成字段。

桥接写后将回读的显示值保存为 `detail.display_value`；显示值为空时结果为 `unknown`，保留已经创建的 `detail.record_id`，先检查源公式再按真实证据恢复，不能重建记录。`feishu-pull` 另返回 `display_values[表名][record_id]`，与统一快照 `data` 分开。快照及 HTML 目前仍以稳定 ID 作为记录编号，`display_field` 不会替换其关联键或报告编号；原生工具可以同时展示核实后的公式编号。

`confirmed_absent_fields` 用于**已确认真实表没有、且本地不是必填**的字段。列表里的字段必须不再出现在 `fields` 映射中，读取结果按未提供处理，不能捏造替代值。母版脚手架已将通讯录 `feishu_url` 列入该列表；不要重新创建「飞书通讯地址」。其他未映射字段不能一概视为空，仍须逐一核实；整张未读表不能靠此列表代替读取。

在 `identity:"record_id"` 模式下，新建行动可省略 `record_id`；保留稳定的行动 `id` 和来源 `source.id` 供执行台账防重，`values` 内不能自行提供 `id`。成功后使用执行结果的 `detail.record_id` 作为后续查询、更新和关联的真实身份。默认 `field` 模式新增仍要求明确稳定业务编号。旧纪要若只含公式展示编号，应先查询解析为稳定身份，不能直接拿展示编号执行更新。

## 工作日10点任务与资源巡检

将宿主唤醒配置为项目时区每天10:00，并由项目日历过滤法定假日、调休与公司例外，或使用等价的已核验工作日触发。桥接参数 `workflow_time:"10:00"` 只规定本轮最早处理时点，不会自行安装定时任务；原09:00人力/周会收集与其他异常巡检保留。宿主晚到可以当天补执行，沿用持久执行台账防重和发送前重读。

本轮核对所有未关闭叶子任务（含无计划的待分流），并将人力/休假、接口、决策及物资等未落实条件送入延期风险材料。负责人不可定位交PM，PM也不可达保留待配置；员工任务及已有资源影响照常检查。具体字段、本人反馈局限和逐级影响口径见 [每日任务巡检与资源影响](../docs/每日任务巡检与资源影响.md)。这段规则不表示真实租户已完成触发绑定。

实际起止、缺估算、受阻/等待及补救问题按 [情景指南](../docs/每日字段情景与17点升级.md) 条件生成；已报完成但缺完成事实保留独立核查，不对普通已完成任务催日常进展。若希望减少手动选状态，可另建 [执行状态公式列](../docs/状态自动显示与维护.md)，保留原状态映射，不把公式输出当可写枚举字段。

### 项目群结构化跟进

`agent-tick` 的预览与发送共用同一行动投影：每项保留来源身份、决策/执行/补填/协调类型、动作、责任路由、DDL及依据、完成/回复要求、来源链接和短影响。按接收人合并，决定请求找明确决定人，执行找已登记负责人；缺责任或不可联系时交有效 PM 协调，不能替 PM 推断决定权。员工仅免例行人力确认，承担的任务、验收和决定请求仍可接收。

真实发送通过已配置项目群和核验后的 `open_id` 生成 mention，普通来源文本中的 `@` 或标签不会扩大提及对象。默认每段 5 项、3500 字符；可选 `group_max_items` 范围 1–10，`group_max_chars` 范围 1200–6000。长正文缩成摘要，完整行动和来源链接保留在预览；不生成截断后无法使用的假链接。

每段分别持久记录成功、失败或未知结果。发送前重读、已解决即停止，合并重复义务但不合并不同任务或不同期限。未知结果先查实际群消息再按既有核验流程处理，不能盲目重发。未约定行动 DDL 显示“待确认”；任务预测、接口预计就绪和原期限只作为对照信息。完整正文示例见 [每日巡检指南](../docs/每日任务巡检与资源影响.md)。这些是程序与原生工具应遵守的执行规则，不表示已绑定真实群或已发出消息。

宿主应保存每段返回的 `detail.batch_key` 与 `detail.items[].occurrence_key`。当前 `runtime-reconcile` 一次只处理一个键：经真实查询核验后，须分别核验该批次及其成员行动；仅核验批次不会自动解除成员的 unknown/running 保护。暂停该项目 tick 后按每日巡检指南的“双层恢复”步骤执行，不能重置其他未核验段落。

具名、含结论且决定时间有效的已批准记录停止原决策请求；后续执行需要关联已有任务或登记事项继续追踪，不从结论自动创建执行承诺。Excel 模式可在没有飞书 `open_id` 时预览结构化清单；实际发群仍必须通过飞书身份和通知范围校验。

### 17点汇总和真实回复证据

同一配置可设置以下参数；宿主另外绑定项目当地17:00唤醒。`escalation_recipient_id` 为空时用项目 PM，非空须是已映射、有效且允许通知的通讯录内部 ID，不能填作者邮箱或仅填姓名。

```json
{
  "workflow_time": "10:00",
  "response_cutoff": "17:00",
  "escalation_time": "17:00",
  "min_response_minutes": 30,
  "escalation_recipient_id": "",
  "reply_evidence_sources": []
}
```

`response_cutoff` 是当天问题的回复截止，与执行/补救DDL分别输出。至少留出配置的回应时间；晚发/截止后新问题单列，不立即判未回复。17点重读业务事实，汇总给负责人；已回应但风险未解除仍列风险。失败、结果未知、未发送、负责人变更与真正未回复分开。

当前桥接提供回复证据的**受控导入接口**，没有自动安装群消息订阅或完整历史读取器。宿主先核验真实发送人并映射为内部 `people.id`，核对消息来源/时间、问题关联及分页完整性，再从白名单来源导入：

```text
python -m pm_core.cli followup-evidence-import --config workspace/feishu.json --ledger workspace/runtime.sqlite3 --file workspace/replies.json
python -m pm_core.cli followup-evidence-import --config workspace/feishu.json --ledger workspace/runtime.sqlite3 --file workspace/replies.json --apply
```

默认只预览，`--apply` 写入已有台账，不发消息。飞书模式拒绝 `--now`，不能用导入时间篡改真实回复时间。默认来源白名单为空，接入后仅添加实际受控来源。白名单只限制来源名称，不能认证任意 JSON；宿主仍承担身份和原始证据核验。完整数据结构见 [证据导入说明](../docs/每日字段情景与17点升级.md#宿主接入和证据导入)。

“收到”只证明回应；部分/完整回答分别保存，业务事实另行核实。没有完整覆盖成功发送到回复截止的读取窗口，只能写“回复情况待核验”。填写表格可解除资料问题，但不是群回复证据。一个回复涉及多个行动须分别关联各自成功发送的 `occurrence_key` 及问题ID。

管理汇总首次真正发送时冻结当时事实与分段，保留截至时点；成功/未知段不重复发，失败段沿用原请求身份恢复。后续风险变化不重发整份已发日报；可在下一次获授权跟进中处理。空汇总不占用当天发送记录。10点/17点、发送与读取权限、持久存储均须真实接入；本次交付未安装定时任务、未发送消息。

## 文档和 HTML

使用 `feishu-report --config workspace/feishu.json --out workspace/审议.html` 从一次完整读取生成 HTML 和相邻报告 JSON，两者共享快照及截止时点。`report-publish` 输入 `{title,paragraphs,snapshot_id,cutoff}`，每个 snapshot_id 只创建一次新文档：

```text
python -m pm_core.cli report-publish --config workspace/feishu.json --ledger workspace/runtime.sqlite3 --file workspace/report.json
```

确认配置后加 `--apply`。文档正文部分失败会保留创建结果/未知状态，检查现有文档后恢复，不能盲目重新建文档。HTML 用宿主附件/文件工具交付并记录实际链接；本地生成 HTML 不等于已有网络链接。

## 官方能力依据

[多维表格记录](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create)、[妙记摘要/待办](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/minutes-v1/minute/artifacts)、[逐字稿](https://open.feishu.cn/document/minutes-v1/minute-transcript/get)、[妙记生成订阅](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/minutes-v1/minute/subscription)、[创建日程](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event/create)、[添加参与人](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event-attendee/create)。能力核验不等于当前租户已启用。

## 日期审计与私下核对

为 agent-tick、schedule-audit、feishu-report 配置同一持久 --ledger；预览会保存读取事实和检查点，无外部变更。无变化时只追加读取时点引用，不重复存整份快照。实时读取不接受 --now 倒签；历史材料从真正保存的快照生成。

schedule_audit 配置 decision_makers、trusted_approval_sources、approved_changes、reviewed_changes，规则见 [日期变更审议](../docs/日期变更审议与留痕.md)。pm_private_chat_id 须验证为对应 PM 私聊，pm_private_recipient_id 等于项目 PM；没有私信渠道就 pending，不退回项目群。原生宿主也要实现同样的来源留痕与隐私路由。
