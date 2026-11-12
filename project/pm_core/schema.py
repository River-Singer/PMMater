"""One field catalogue drives SQL, validation, Excel controls and documentation."""
from dataclasses import dataclass, asdict, replace
import json

ENUMS = {
    "work_stage": {"initiation":"启动", "requirements":"需求", "design":"设计", "development":"开发", "integration":"联调", "testing":"测试", "acceptance":"验收", "launch":"上线", "handover":"移交", "other":"其他"},
    "delay_risk": {"none":"未主动标记", "at_risk":"有延期风险"},
    "deliverable_acceptance": {"draft":"未提交", "pending":"待验收", "accepted":"合格", "rejected":"不合格"},
    "person_type": {"employee":"员工", "vendor":"供应商"},
    "interface_protocol": {"http":"HTTP / HTTPS", "sftp":"SFTP", "file":"文件交换", "database":"数据库", "message":"消息队列", "other":"其他"},
    "http_method": {"GET":"GET", "POST":"POST", "PUT":"PUT", "PATCH":"PATCH", "DELETE":"DELETE", "SOAP":"SOAP", "OTHER":"其他"},
    "interface_direction": {"outbound":"本项目调用对方", "inbound":"对方调用本项目", "bidirectional":"双向"},
    "interface_status": {"designing":"设计中", "developing":"开发中", "testing":"联调中", "ready":"已就绪", "blocked":"受阻", "retired":"已停用"},
    "document_type": {"charter":"项目章程/立项", "requirements":"需求/业务规则", "design":"设计/接口说明", "test":"测试/验收", "minutes":"会议纪要", "runbook":"上线/运维移交", "other":"其他"},
    "document_status": {"draft":"草稿", "reviewing":"待评审", "approved":"已确认", "archived":"已归档"},
    "node_kind": {"project":"项目", "deliverable":"交付物", "module":"功能模块", "task":"任务"},
    "scope": {"project":"项目内", "operations":"日常运维", "adhoc":"临时事项"},
    "work_type": {"feature":"开发功能", "improvement":"优化", "bug":"缺陷修复", "incident":"故障恢复", "request":"服务请求", "action":"会议/协调行动"},
    "work_status": {"intake":"待分流", "ready":"待开始", "in_progress":"进行中", "blocked":"受阻", "in_review":"待确认", "done":"已确认完成", "cancelled":"取消"},
    "priority": {"P0":"P0 紧急", "P1":"P1 高", "P2":"P2 一般", "P3":"P3 低"},
    "urgency": {"immediate":"立即处理", "soon":"近期处理", "normal":"可正常排期"},
    "impact": {"critical":"核心业务中断/重大影响", "high":"多团队或关键节点", "medium":"单团队/部分用户", "low":"局部轻微影响"},
    "severity": {"S1":"S1 致命", "S2":"S2 严重", "S3":"S3 一般", "S4":"S4 轻微"},
    "project_type": {"development":"新建开发", "enhancement":"系统优化项目", "migration":"迁移/改造", "research":"研究/验证", "other":"其他"},
    "lifecycle": {"initiating":"启动", "planning":"计划", "executing":"执行", "accepting":"验收移交", "closed":"已结项", "paused":"暂停"},
    "person_role": {"collaborator":"协作人", "interface":"接口人", "stakeholder":"关注/知会人", "coordinator":"协调人", "backup":"替补"},
    "record_kind": {"planned":"计划安排", "actual":"实际出勤"},
    "period": {"AM":"上午", "PM":"下午"},
    "attendance_status": {"available":"可安排", "other_work":"其他工作安排", "onsite":"现场在岗", "remote":"远程在岗", "travel":"出差", "leave":"请假", "off":"休息", "unavailable":"不可参加", "unknown":"待确认"},
    "day_type": {"default_workday":"默认工作日", "default_weekend":"默认周末", "holiday":"法定假日/调休休息", "makeup_workday":"调休工作日", "company_override":"公司例外"},
    "verification_status": {"default_unverified":"仅默认周历", "draft":"待核验", "verified":"已记录核验来源"},
    "register_type": {"reminder":"提醒事项", "issue":"已发生问题", "risk":"风险提醒", "change":"范围/基线变更", "decision":"待决定事项"},
    "register_status": {"open":"开放", "in_review":"处理中/评审中", "approved":"已批准", "rejected":"已拒绝", "closed":"已关闭"},
    "meeting_status": {"planned":"会前计划", "held":"已召开", "cancelled":"取消"},
    "dependency_type": {"FS":"完成后开始"},
}

@dataclass(frozen=True)
class Field:
    name: str
    label: str
    dtype: str = "text"
    required: bool = False
    enum: str | None = None
    ref: str | None = None
    default: object = None
    rule: str = ""
    group: str = "基本"
    editable: bool = True
    max_length: int = 2000

    @property
    def widget(self):
        if not self.editable: return "只读"
        if self.dtype=="refs": return "多选关联；Excel用分号分隔"
        if self.enum or self.ref: return "单选下拉"
        return {"date":"日期输入", "datetime":"带时区日期时间", "integer":"整数输入", "decimal":"非负小数输入", "boolean":"是/否下拉", "multiline":"多行文本"}.get(self.dtype, "文本编辑")

def F(name,label,dtype="text",**kw): return Field(name,label,dtype,**kw)
ID = F("id","编号",rule="新增可留空由Python生成；已有编号不得修改",max_length=80)
VERSION = F("version","版本","integer",editable=False,rule="系统生成；更新必须携带导出时版本，防止旧Excel覆盖新记录",group="系统")

TABLES = {
"people": {"sheet":"项目通讯录", "prefix":"PER", "fields":[ID,
    F("name","姓名",required=True,max_length=100), F("person_type","类型",enum="person_type",required=True,default="employee"),
    F("company","所属公司",max_length=150), F("department","职能/部门",max_length=150),
    F("project_role","项目角色",max_length=150), F("expertise","职责/负责范围","multiline"),
    F("phone","手机号/电话",max_length=80,rule="按文本保存，保留+、区号和前导0"),
    F("feishu_url","飞书通讯地址",max_length=1000,rule="飞书个人资料链接；不自动发消息"),
    F("email","邮箱",max_length=254),
    F("starts_on","参与开始","date",group="参与窗口"), F("ends_on","参与结束","date",group="参与窗口"),
    F("contact","其他联系方式",max_length=300,group="补充"),
    F("backup_id","替补联系人",ref="people",group="补充"), F("active","在项目中","boolean",required=True,default=True), VERSION]},
"calendars": {"sheet":"日历配置", "prefix":"CAL", "fields":[ID,
    F("name","日历名称",required=True), F("timezone","时区",required=True,default="Asia/Shanghai",rule="IANA时区；项目所在地，不自动沿用聊天/电脑所在地"),
    F("region","地区/适用组织"), F("coverage_start","覆盖开始","date",required=True), F("coverage_end","覆盖结束","date",required=True),
    F("source_url","核验资料网址",group="来源"), F("verified_at","核验时间","datetime",group="来源"),
    F("notes","使用说明","multiline",rule="工作日=默认周历叠加已导入例外；公司另有安排需覆盖"), VERSION]},
"calendar_days": {"sheet":"日期表", "prefix":"CD", "fields":[ID,
    F("calendar_id","所属日历",ref="calendars",required=True),F("date","日期","date",required=True),
    F("weekday","星期序号","integer",required=True,rule="1=周一…7=周日，必须与日期一致"),
    F("is_workday","工作日","boolean",required=True), F("day_type","日期类型",enum="day_type",required=True),
    F("label","节日/例外说明"), F("source_url","来源网址",group="来源"),F("verified_at","核验时间","datetime",group="来源"),
    F("verification_status","核验状态",enum="verification_status",required=True,default="default_unverified",group="来源"),VERSION]},
"services": {"sheet":"产品与服务", "prefix":"SVC", "fields":[ID,
    F("name","产品/服务名称",required=True),F("business_owner_id","业务接口",ref="people"),F("technical_owner_id","技术接口",ref="people"),
    F("calendar_id","使用日历",ref="calendars",required=True), F("description","服务范围","multiline"),F("active","在用","boolean",required=True,default=True),VERSION]},
"projects": {"sheet":"项目概况", "prefix":"PRJ", "fields":[ID,
    F("name","项目名称",required=True),F("project_type","项目类别",enum="project_type",required=True,default="development"),
    F("stage","生命周期阶段",enum="lifecycle",required=True,default="initiating"),F("po_id","PO/资源协调人",ref="people",required=True),
    F("pm_id","PM",ref="people",required=True),F("sponsor_id","业务决定人",ref="people"),F("service_id","涉及产品/服务",ref="services"),
    F("calendar_id","项目日历",ref="calendars",required=True),F("goal","目标和范围","multiline"),
    F("baseline_start","基线开始","date"),F("baseline_finish","基线完成","date"),F("forecast_finish","当前预计完成","date"),
    F("summary","整体结论","multiline"),F("business_impact","业务影响","multiline"),F("baseline_change_id","基线变更依据",ref="registers",group="变更"),VERSION]},
"meetings": {"sheet":"会议", "prefix":"MTG", "fields":[ID,
    F("title","会议名称",required=True),F("project_id","项目",ref="projects"),F("service_id","产品/服务",ref="services"),
    F("organizer_id","组织人",ref="people",required=True),F("scheduled_at","会议时间","datetime",required=True),
    F("status","会议状态",enum="meeting_status",required=True,default="planned"),F("agenda","议题","multiline"),
    F("minutes","确认纪要","multiline",rule="会前不得当成已确认纪要"),F("evidence","记录依据"),VERSION]},
"wbs": {"sheet":"WBS总表", "prefix":"WBS", "fields":[
    F("id","WBS编号",group="基本情况",rule="新增可留空由Python生成（WBS-兜底前缀）；业务编号建议WBS-<项目缩写>-<流水号>，已有编号不得修改",max_length=80),
    F("node_kind","层级类型",enum="node_kind",required=True,default="task",group="基本情况",rule="项目=根分组；交付物/功能模块=中间分组；任务=执行叶子；仅任务行参与执行统计"),
    F("parent_id","父级WBS",ref="wbs",group="基本情况",rule="同项目；禁止循环；层级类型=项目必须留空；任务可无父级；独立运维使用运维与优化事项表"),
    F("title","工作标题",required=True,group="基本情况"),
    F("project_id","所属项目",ref="projects",group="基本情况",rule="当前项目；独立运维通过运维表维护"),
    F("service_id","所属产品/服务",ref="services",group="基本情况",rule="兼容旧库字段；单项目前端不需要维护"),
    F("work_type","工作类型",enum="work_type",required=True,default="request",group="基本情况",rule="仅任务行有执行意义；分组行保持默认"),
    F("stage","任务阶段",enum="work_stage",group="基本情况"),F("is_acceptance_node","验收节点？","boolean",default=False,required=True,group="基本情况"),
    F("forecast_start","当前预计开始","date",group="排期"),F("forecast_finish","当前预计完成","date",group="排期",rule="任务待开始及以后必填，不能覆盖原计划"),
    F("member_ids","任务成员","refs",ref="people",group="基本情况",rule="从通讯录多选；Excel按分号分隔稳定编号，不另填人员关系表"),
    F("backup_member_ids","任务成员（备份）","refs",ref="people",group="人员",rule="作为备用成员维护，不是另一份重复名册"),
    F("owner_id","负责人",ref="people",group="基本情况",rule="交付物/功能模块与任务待开始及以后必填"),
    F("acceptor_id","确认/验收人",ref="people",group="基本情况",rule="任务待开始及以后必填"),
    F("coordinator_id","协调人",ref="people",group="协调"),
    F("actual_start","实际开始","date",group="排期"),F("actual_finish","实际完成","date",group="完成"),
    F("actual_person_days","实际人天总和","decimal",group="完成",rule="任务实际投入的人天，可填0.5；未知留空，不用项目考勤推算任务工时"),
    F("status","状态",enum="work_status",required=True,default="intake",group="任务跟踪",rule="分组行保持待分流；执行状态校验仅针对任务行"),
    F("priority","优先级",enum="priority",group="任务跟踪",rule="任务待开始及以后必填；P0需要说明紧急依据"),
    F("urgency","紧迫性",enum="urgency",group="任务跟踪"),F("scope","工作归属",enum="scope",required=True,default="project",group="任务跟踪",rule="项目内任务挂所属项目；运维挂服务；临时事项可留空"),
    F("impact","影响范围",enum="impact",group="任务跟踪"),F("severity","缺陷严重度",enum="severity",group="任务跟踪",rule="仅Bug/故障使用；不等于优先级"),
    F("delay_risk","延期风险标记",enum="delay_risk",default="none",required=True,group="任务跟踪"),
    F("baseline_start","原计划开始","date",group="任务跟踪"),F("baseline_finish","原计划完成","date",group="任务跟踪"),
    F("next_action","下一步动作","multiline",group="任务跟踪"),F("next_due_at","下一步最迟时间","datetime",group="任务跟踪"),
    F("waiting_on","等待事项/前置输入","multiline",group="任务跟踪",rule="用业务语言说明需要谁提供什么；需持续推进时关联提醒或风险"),
    F("blocker_reason","阻塞原因","multiline",group="任务跟踪",rule="受阻时必填"),
    F("description","工作说明","multiline",group="任务相关情况"),
    F("acceptance_criteria","完成标准","multiline",group="任务相关情况",rule="任务待开始及以后必填"),
    F("completion_evidence","完成依据","multiline",group="任务相关情况",rule="已确认完成必填"),
    F("acceptance_standard","完成/验收标准","multiline",group="任务相关情况",rule="交付物/功能模块行填写验收口径；任务行用完成标准"),
    F("deliverable_spec","交付成果","multiline",group="任务相关情况",rule="交付物/功能模块行说明应产出的成果物"),
    F("scope_note","范围说明","multiline",group="任务相关情况",rule="交付物/功能模块行写范围边界；明确不含的内容"),
    F("priority_reason","优先级理由","multiline",group="任务相关情况",rule="P0或与建议优先级不同需要填写"),
    F("delay_risk_reason","延期风险说明","multiline",group="任务相关情况",rule="主动标记有延期风险时必填"),
    F("remediation_action","延期补救措施","multiline",group="任务相关情况"),
    F("remediation_expected","预期补救时间","date",group="任务相关情况"),F("remediation_actual","实际补救时间","date",group="任务相关情况",rule="不得在未来；填写后不自动抹掉延期或把任务标为完成"),
    F("cancel_reason","取消原因","multiline",group="任务相关情况"),F("reopen_reason","重新打开原因","multiline",group="任务相关情况"),
    F("meeting_id","来源会议",ref="meetings",group="任务相关情况"),F("register_id","来源问题/决定",ref="registers",group="任务相关情况"),
    F("baseline_change_id","基线变更依据",ref="registers",group="任务相关情况"),VERSION]},
"participants": {"sheet":"参与关系", "prefix":"REL", "fields":[ID,
    F("project_id","项目",ref="projects",rule="与工作项二选一"),F("work_item_id","工作项",ref="wbs",rule="与项目二选一；建议关联任务行"),
    F("person_id","参与人",ref="people",required=True),F("role","关系角色",enum="person_role",required=True),
    F("starts_on","参与开始","date"),F("ends_on","参与结束","date"),F("responsibility","接口/职责说明","multiline"),VERSION]},
"dependencies": {"sheet":"前置依赖", "prefix":"DEP", "fields":[ID,
    F("predecessor_id","前置工作项",ref="wbs",required=True),F("successor_id","后续工作项",ref="wbs",required=True),
    F("dependency_type","依赖类型",enum="dependency_type",required=True,default="FS"),F("lag_workdays","间隔工作日","integer",required=True,default=0,rule="非负；用于约束检查，当前版本不自动重排工期"),F("notes","依赖说明","multiline"),VERSION]},
"registers": {"sheet":"提醒与风险", "prefix":"REG", "fields":[ID,
    F("kind","记录类型",enum="register_type",required=True),F("title","事项",required=True),F("project_id","项目",ref="projects"),F("work_item_id","相关工作项",ref="wbs"),
    F("priority","优先级",enum="priority",default="P2"),
    F("owner_id","处理负责人",ref="people",required=True),F("coordinator_id","协调人",ref="people"),F("decision_by_id","决定人",ref="people"),
    F("impact","项目/业务影响","multiline"),F("deadline_at","最迟时间","datetime"),F("status","状态",enum="register_status",required=True,default="open"),
    F("proposal","建议/应对方案","multiline"),F("next_action","下一步/提醒内容","multiline"),F("decision","决定/结论","multiline"),F("decided_at","决定时间","datetime"),
    F("closure_evidence","落实/关闭依据","multiline"),F("closed_at","关闭时间","datetime"),VERSION]},
"day_records": {"sheet":"每日记录", "prefix":"DAY", "fields":[ID,
    F("person_id","人员",ref="people",required=True),F("calendar_id","使用日历",ref="calendars",required=True),F("date","实际日期","date",required=True),
    F("period","时段",enum="period",required=True),F("record_kind","记录类别",enum="record_kind",required=True),
    F("status","安排/出勤状态",enum="attendance_status",required=True),F("notes","说明","multiline"),VERSION]},
"interfaces": {"sheet":"接口清单", "prefix":"API", "fields":[ID,
    F("name","接口名称",required=True),F("project_id","所属项目",ref="projects",required=True,group="系统"),
    F("source_system","来源系统",required=True),F("target_system","目标系统",required=True),
    F("direction","调用方向",enum="interface_direction",default="outbound",required=True),
    F("protocol","接入方式",enum="interface_protocol",default="http",required=True),F("address","接口地址/路径",required=True,max_length=1500),
    F("method","请求方式",enum="http_method",rule="HTTP/HTTPS接口必填"),F("description","用途/业务描述","multiline",required=True),
    F("owner_id","项目接口负责人",ref="people",required=True),F("counterparty_id","对方接口人",ref="people"),
    F("status","当前状态",enum="interface_status",default="designing",required=True),F("expected_ready","预计就绪日","date"),
    F("work_item_id","关联任务",ref="wbs"),F("spec_url","接口说明链接",max_length=1500),
    F("auth_note","鉴权方式说明","multiline",group="补充",rule="只写鉴权方式/申请入口，不存密码、密钥或令牌"),
    F("notes","提醒/限制","multiline",group="补充"),VERSION]},
"project_documents": {"sheet":"项目文档", "prefix":"DOC", "fields":[ID,
    F("title","文档名称",required=True),F("project_id","所属项目",ref="projects",required=True,group="系统"),
    F("document_type","文档类型",enum="document_type",required=True,default="other"),
    F("url","飞书/文档链接",required=True,max_length=1500),F("owner_id","维护负责人",ref="people",required=True),
    F("status","文档状态",enum="document_status",required=True,default="draft"),F("version_label","版本/修订说明",max_length=200),
    F("updated_on","最后维护日期","date"),F("work_item_id","关联任务",ref="wbs"),
    F("meeting_id","关联会议",ref="meetings"),F("description","内容/使用说明","multiline"),VERSION]},
"deliverables": {"sheet":"交付物验收", "prefix":"DEL", "fields":[ID,
    F("project_id","所属项目",ref="projects",required=True,group="系统"),
    F("work_item_id","关联WBS任务",ref="wbs",required=True),F("title","交付物名称",required=True),
    F("document_url","交付物 / 飞书文档链接",rule="保存可访问的HTTP/HTTPS链接；提交后必填"),
    F("document_id","关联项目文档",ref="project_documents",group="补充"),F("version_label","提交版本",group="提交"),
    F("submitted_by_id","提交人",ref="people",group="提交"),F("submitted_at","提交时间","datetime",group="提交"),
    F("acceptor_id","验收人",ref="people",group="验收"),
    F("acceptance_status","验收结果",enum="deliverable_acceptance",default="draft",required=True,group="验收"),
    F("reviewed_at","验收时间","datetime",group="验收"),F("acceptance_comments","验收意见","multiline",group="验收"),VERSION]},
}

# Presentation-only fields are calculated from current or captured facts. They are
# deliberately absent from SQL tables: a report refresh cannot rewrite history.
DERIVED_FIELDS = {
 "wbs":[
    F("child_count","子节点数","integer",editable=False,group="WBS进度",rule="直接子节点个数，含交付物/功能模块分组节点"),
    F("child_done_count","已完成子级数","integer",editable=False,group="WBS进度",rule="状态为已确认完成或已取消的直接子节点数"),
    F("tree_progress_pct","完成率","decimal",editable=False,group="WBS进度",rule="叶子任务完成数/叶子任务总数；分组节点不参与统计，无叶子任务留空"),
    F("planned_duration_days","预计工期（自然日）","integer",editable=False,group="排期",rule="当前预计起止（缺失时采用原计划），含首尾；缺日期不猜测"),
    F("actual_duration_days","实际工期（自然日）","integer",editable=False,group="完成",rule="实际起止含首尾；尚未完成留空"),
    F("stage_acceptance_result","阶段验收结果",editable=False,group="验收",rule="由关联交付物的验收状态汇总；todo完成率不代表验收通过"),
    F("related_deliverables","相关交付物","multiline",editable=False),F("related_risks","相关风险/问题","multiline",editable=False),
    F("risk_count","风险及问题数","integer",editable=False,rule="关联且未关闭的风险/问题条数"),
    F("attendance_summary","人员考勤","multiline",editable=False,rule="成员在任务日期窗口的出勤记录摘要，不等于该任务实际人天"),
    F("todo_list","todo list","multiline",editable=False),F("todo_count","todo数量","integer",editable=False),
    F("todo_done_count","完成数量","integer",editable=False),F("todo_progress_pct","进度（todo完成占比）","decimal",editable=False,rule="有效叶子todo完成数/有效叶子todo数；无todo留空，不是整个项目加权完成率"),
    F("todo_completed_aux","辅助字段-todo已完成",editable=False,group="辅助"),F("task_count_aux","任务数量辅助字段","integer",editable=False,group="辅助",rule="执行叶子任务1、分组节点与父任务0，避免重复统计"),
    F("is_delayed","是否延期","boolean",editable=False,group="延期",rule="已逾承诺日未完成或实际完成晚于承诺日；未到期预测延期属于风险"),
    F("delay_state","延期状态",editable=False,group="延期"),F("delay_days","实际延期天数（自然日）","integer",editable=False,group="延期"),
    F("commitment_finish","延期判断基准日","date",editable=False,group="延期",rule="优先原计划完成日；未设基线时采用当前预计日并标明依据"),
    F("in_week_window","当前至周日关注","boolean",editable=False,group="跟踪窗口"),
    F("window_reason","本周关注原因","multiline",editable=False,group="跟踪窗口"),
 ],
 "deliverables":[F("pm_feishu_url","PM飞书通讯地址",editable=False,rule="项目PM关联到通讯录中的飞书地址，避免重复维护")],
}

# v7: separate execution streams and explicit availability/decision facts.
ENUMS.update({
    "location": {"onsite":"驻场", "remote":"远程", "travel":"出差", "mixed":"分时安排", "unknown":"待确认"},
    "allocation_mode": {"dedicated":"专职本项目及对应运维", "shared":"兼其他工作", "unknown":"待确认"},
    "response_status": {"pending":"待确认", "confirmed":"已确认", "corrected":"已修订"},
    "future_leave_change": {"unknown":"尚未回答", "no_change":"无新增或变化", "changed":"有新增或变化"},
    "plan_status": {"proposed":"待确认", "confirmed":"已确认", "cancelled":"已撤销"},
    "completeness": {"pending":"待核验", "complete":"年度例外已核齐"},
    "commitment_status": {"candidate":"候选", "confirmed":"已承诺", "done":"成果已确认", "cancelled":"取消"},
    "feishu_id_type": {"open_id":"open_id", "user_id":"user_id", "union_id":"union_id"},
})
ENUMS["person_type"].update({"external":"其他非员工", "unknown":"待确认"})
ENUMS["work_type"]["configuration"]="配置修改"

def extend(table, fields):
    TABLES[table]["fields"][-1:-1] = fields

extend("people", [F("feishu_user_id","飞书稳定人员ID",group="来源"),
    F("feishu_id_type","飞书ID类型",enum="feishu_id_type",default="open_id",group="来源"),
    F("identity_confirmed_at","身份核实时间","datetime",group="来源")])
extend("projects", [F("reminder_start","提醒有效开始","date"), F("reminder_end","提醒有效结束","date")])
extend("meetings", [F("ends_at","会议结束","datetime"), F("attendee_ids","参会人员","refs",ref="people"),
    F("minutes_url","妙记/纪要来源",max_length=1500), F("calendar_id","外部日历ID"), F("calendar_event_id","外部日程ID")])
extend("wbs", [F("delay_reason","实际延期说明","multiline"), F("delay_impact","延期影响","multiline"),
    F("remediation_owner_id","补救责任人",ref="people"), F("remediation_result","补救验证结果","multiline"),
    F("decision_id","审议决定",ref="registers"), F("source_url","来源链接",max_length=1500),
    F("progress_note","近期进展","multiline"), F("progress_confirmed_at","进展确认时间","datetime"),
    F("progress_confirmed_by_id","进展确认人",ref="people"),
    F("migrated_operation_id","历史运维迁移记录",ref="operations",editable=False,group="系统")])
ops_fields=[]
for field in TABLES["wbs"]["fields"]:
    if field.name in {"parent_id","node_kind","is_acceptance_node","migrated_operation_id"}: continue
    if field.name=="id": field=replace(ID, label="事项编号")
    if field.name=="scope": field=replace(field, default="operations")
    ops_fields.append(field)
TABLES["operations"]={"sheet":"运维与优化事项","prefix":"OPS","fields":ops_fields}
extend("operations", [F("related_wbs_id","影响项目任务",ref="wbs"), F("promoted_wbs_id","已转入项目任务",ref="wbs"),
    F("promoted_at","转入时间","datetime"), F("promotion_decision_id","转入决定",ref="registers"), F("legacy_wbs_id","迁移原编号",group="来源")])
for name in ("registers","project_documents","interfaces"):
    extend(name,[F("operation_id","关联运维事项",ref="operations")])
TABLES["daily_confirmations"]={"sheet":"每日工作安排","prefix":"DC","fields":[ID,
    F("project_id","项目",ref="projects",required=True), F("person_id","成员",ref="people",required=True),
    F("date","日期","date",required=True),F("period","时段",enum="period",required=True),
    F("work_location","地点",enum="location",required=True,default="unknown"),
    F("allocation_mode","投入方式",enum="allocation_mode",required=True,default="unknown"),
    F("available_hours","项目及运维净可用小时","decimal"), F("project_hours","已安排项目小时","decimal"),
    F("operations_hours","已安排运维小时","decimal"),F("response_status","确认状态",enum="response_status",default="pending",required=True),
    F("leave_plan_id","关联休假",ref="leave_plans"),F("notes","安排说明","multiline"),
    F("future_leave_change","未来休假有无变化",enum="future_leave_change",default="unknown",required=True),
    F("future_leave_note","休假变化说明","multiline"),
    F("confirmed_at","确认时间","datetime"),F("confirmed_by_id","确认人",ref="people"),F("source","回复来源"),VERSION]}
TABLES["leave_plans"]={"sheet":"未来休假计划","prefix":"LEAVE","fields":[ID,
    F("project_id","项目",ref="projects",required=True),F("person_id","成员",ref="people",required=True),
    F("start_date","开始日期","date",required=True),F("start_period","开始时段",enum="period",default="AM",required=True),
    F("end_date","结束日期","date",required=True),F("end_period","结束时段",enum="period",default="PM",required=True),
    F("status","计划状态",enum="plan_status",default="proposed",required=True), F("backup_id","交接人",ref="people"),
    F("impact","受影响工作","multiline"),F("source","来源"),F("confirmed_at","确认时间","datetime"),
    F("confirmed_by_id","确认人",ref="people"),VERSION]}
TABLES["resource_weeks"]={"sheet":"人员周资源安排","prefix":"RES","fields":[ID,
    F("project_id","项目",ref="projects",required=True), F("person_id","成员",ref="people",required=True),
    F("week_start","周一日期","date",required=True),F("net_available_hours","净可用小时","decimal"),
    F("project_hours","项目分配小时","decimal"),F("operations_hours","运维分配小时","decimal"),
    F("buffer_hours","未分配缓冲小时","decimal"),F("confirmed_at","确认时间","datetime"),
    F("confirmed_by_id","确认人",ref="people"),F("notes","容量依据/变化","multiline"),VERSION]}
TABLES["calendar_years"]={"sheet":"年度日历来源","prefix":"CY","fields":[ID,
    F("calendar_id","日历",ref="calendars",required=True),F("year","年份","integer",required=True),
    F("status","完整性",enum="completeness",default="pending",required=True),F("source_url","官方公告",max_length=1500),
    F("published_on","发布日期","date"),F("verified_at","核验时间","datetime"), F("exception_hash","完整例外摘要"),VERSION]}
TABLES["calendar_overrides"]={"sheet":"公司日历例外","prefix":"CO","fields":[ID,
    F("calendar_id","日历",ref="calendars",required=True), F("date","日期","date",required=True),
    F("person_id","仅指定成员",ref="people"), F("is_workday","工作日","boolean",required=True),
    F("status","确认状态",enum="plan_status",default="proposed",required=True),F("reason","例外原因","multiline",required=True),
    F("source_url","依据网址"),F("confirmed_at","确认时间","datetime"),F("confirmed_by_id","确认人",ref="people"),VERSION]}
TABLES["weekly_commitments"]={"sheet":"本周成果承诺","prefix":"WC","fields":[ID,
    F("project_id","项目",ref="projects",required=True),F("work_item_id","项目任务",ref="wbs"),F("operation_id","运维事项",ref="operations"),
    F("week_start","周一日期","date",required=True),F("outcome","本周可验证成果","multiline",required=True),
    F("due_date","承诺日期","date",required=True),F("owner_id","负责人",ref="people",required=True),
    F("verification","验证方式","multiline",required=True),F("status","状态",enum="commitment_status",default="candidate",required=True),
    F("confirmed_at","承诺确认时间","datetime"),F("confirmed_by_id","承诺确认人",ref="people"),
    F("completed_at","成果完成时间","datetime"),F("result_evidence","结果依据","multiline"),F("meeting_id","来源会议",ref="meetings"),VERSION]}

ORDER = list(TABLES)
UNIQUE = {
    "daily_confirmations":[("project_id","person_id","date","period")],
    "resource_weeks":[("project_id","person_id","week_start")],
    "calendar_years":[("calendar_id","year")],
    "calendar_days":[("calendar_id","date")],
    "dependencies":[("predecessor_id","successor_id","dependency_type")],
    "day_records":[("person_id","date","period","record_kind")],
    "participants":[("project_id","work_item_id","person_id","role","starts_on","ends_on")],
}

STATE_TRANSITIONS = {
 "intake":{"ready","cancelled"}, "ready":{"intake","in_progress","blocked","cancelled"},
 "in_progress":{"blocked","in_review","cancelled"}, "blocked":{"ready","in_progress","cancelled"},
 "in_review":{"in_progress","done","cancelled"}, "done":{"in_progress"}, "cancelled":{"intake"},
}

MAINTAINERS={"people":"PO主导维护项目通讯录，PM更新参与情况", "calendars":"PM维护、PO确认组织日历", "calendar_days":"Python生成，PM核验并导入例外", "services":"兼容旧库；单项目不维护", "projects":"PM维护事实，PO协调，业务决定人确认取舍", "meetings":"PM/会议组织人", "wbs":"树结构与执行状态由负责人反馈，PM维护", "participants":"内部兼容关系，前端不单独填写", "dependencies":"可选技术依赖，前端不单独填写", "registers":"PM跟进提醒，负责人处理，PO协调", "day_records":"成员反馈，PM记录", "interfaces":"项目接口负责人维护，PM跟进", "project_documents":"各文档负责人维护链接，PM整理", "deliverables":"成员提交，指定验收人确认结果，PM跟进"}

MAINTAINERS.update({name:"成员维护事实，PM核对范围及决定" for name in ("operations","daily_confirmations","leave_plans","resource_weeks","calendar_years","calendar_overrides","weekly_commitments")})

def field_contract(table,f):
    return asdict(f)|{"widget":f.widget,"python_type":{"text":"str","multiline":"str","date":"date → ISO str","datetime":"aware datetime → ISO str","integer":"int（不含bool）","decimal":"有限非负数（不含bool）","refs":"list[str]，逐个校验稳定编号","boolean":"bool"}[f.dtype],"sqlite_type":"INTEGER" if f.dtype in ("integer","boolean") else "REAL" if f.dtype=="decimal" else "JSON TEXT" if f.dtype=="refs" else "TEXT","null_meaning":"未提供/未确认；不等于0、缺勤或正常","maintainer":"系统" if not f.editable else MAINTAINERS[table]}

def suggested_priority(impact, urgency):
    if not impact or not urgency: return None
    matrix={"critical":{"immediate":"P0","soon":"P1","normal":"P1"},
            "high":{"immediate":"P1","soon":"P1","normal":"P2"},
            "medium":{"immediate":"P1","soon":"P2","normal":"P2"},
            "low":{"immediate":"P2","soon":"P3","normal":"P3"}}
    return matrix[impact][urgency]

def catalogue():
    return {"schema_version":7,"enums":ENUMS,"tables":{name:{"sheet":m["sheet"],"prefix":m["prefix"],"maintainer":MAINTAINERS[name],"unique":[list(k) for k in UNIQUE.get(name,[])],"fields":[field_contract(name,f) for f in m["fields"]]} for name,m in TABLES.items()},"derived_fields":{t:[field_contract(t,f) for f in fs] for t,fs in DERIVED_FIELDS.items()},"state_transitions":{k:sorted(v) for k,v in STATE_TRANSITIONS.items()}}

def dictionary_markdown():
    lines=["# pmmaster 字段字典", "", "本文件由 pm_core/schema.py 自动生成；下拉值、Python校验、SQLite字段与Excel列来自同一份定义。", "", "一个工作区管理一个完整项目。人员只维护项目通讯录；参与关系、结构化依赖和产品服务是内部兼容对象，不要求日常单独填写。ID新增可留空自动生成；关系使用稳定ID，Excel显示为‘ID · 名称’。所有日期为ISO日期，时间必须包含时区。版本号用于旧文件冲突检查。", ""]
    for name,m in TABLES.items():
        lines.extend([f"## {m['sheet']} `{name}`", "", f"维护角色：{MAINTAINERS[name]}。文本/日期在SQLite中保存为TEXT，布尔/整数保存为INTEGER；空值代表未提供，不推断缺勤或正常。", "", "|字段|中文名称|类型|控件|必填/默认|关联或选项|条件与规则|", "|---|---|---|---|---|---|---|"])
        for f in m["fields"]:
            rel=f"{f.ref}.id" if f.ref else ("；".join(f"{k}={v}" for k,v in ENUMS[f.enum].items()) if f.enum else "—")
            req=("是" if f.required else "条件/可选")+(f"；默认{f.default}" if f.default is not None else "")
            lines.append(f"|`{f.name}`|{f.label}|{f.dtype}|{f.widget}|{req}|{rel}|{f.rule or '—'}|")
        if name in UNIQUE: lines.extend(["", "联合唯一键："+"；".join(" + ".join(x) for x in UNIQUE[name])])
        lines.append("")
    for table,fields in DERIVED_FIELDS.items():
        lines.extend([f"## {TABLES[table]['sheet']}：系统计算字段", "", "这些字段只读，按当前或已保存快照的事实计算，不写回原始记录。", "", "|字段|名称|类型|口径|", "|---|---|---|---|"])
        lines.extend(f"|`{f.name}`|{f.label}|{f.dtype}|{f.rule or '参见tracking.py的统一计算规则'}|" for f in fields)
        lines.append("")
    lines.extend(["## 工作项状态迁移", "", *[f"- `{k}` → {', '.join(sorted(v))}" for k,v in STATE_TRANSITIONS.items()], "", "## 优先级口径", "", "建议优先级由影响×紧迫性计算，仅作为建议。人工优先级与建议不同或选择P0时需留理由；这里的P0–P3是本模板可调整的分级，不冒称PMI/四大的统一标准。严重度仅描述Bug/故障自身后果。没有自动承诺响应SLA。", "", "## 系统记录", "", "event_log保留每次事务的实体、编号、变更前后、操作者标签和记录时间。snapshots保留固定报告时点的完整事实快照；数据库文件可备份。操作者标签不是身份认证，Excel隐藏/锁定也不是权限安全边界。", ""])
    return "\n".join(lines)
