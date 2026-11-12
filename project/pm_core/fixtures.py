"""Explicit fictional examples; never mixed into a blank operational workspace."""
from datetime import date, timedelta
from pathlib import Path
from .calendar import build_calendar, load_calendar_overrides

DEFAULT_OVERRIDES=Path(__file__).resolve().parent.parent/"data"/"cn_2026_holidays.csv"
CALENDAR_ID="CAL-CN-2026"

def base_data(*,start="2026-01-01",end="2026-12-31",timezone="Asia/Shanghai",calendar_id=CALENDAR_ID,overrides=None):
    rows=load_calendar_overrides(overrides) if overrides else []
    rows=[r for r in rows if start<=r["date"]<=end]
    dates=build_calendar(start,end,timezone,rows)
    return {"calendars":[{"id":calendar_id,"name":"项目工作日历", "timezone":timezone,"region":"按项目配置", "coverage_start":start,"coverage_end":end,
             "notes":"默认周一至周五叠加导入的节假日、调休或公司例外；默认日期未自动宣称完成核验。"}],
            "calendar_days":[{"id":f"{calendar_id}-{r['date']}","calendar_id":calendar_id,**r} for r in dates]}

def demo_data():
    data=base_data(overrides=DEFAULT_OVERRIDES)
    names=[("PER-PO","林岚","PMO","资源与跨部门协调"),("PER-PM","陈越","项目管理","WBS、进度、会议与行动"),("PER-BA","周宁","采购业务","业务规则与验收"),("PER-DEV","吴桐","IT实施","配置、接口、生产缺陷"),("PER-QA","杨琪","测试","验证与缺陷复测"),("PER-SP","郑衡","业务管理","业务取舍与上线确认"),("PER-VENDOR","周敏","供应商","接口测试数据")]
    data["people"]=[{"id":i,"name":n,"department":d,"expertise":x,"active":True} for i,n,d,x in names]
    data["services"]=[{"id":"SVC-PROC","name":"采购协同服务（虚构）","business_owner_id":"PER-BA","technical_owner_id":"PER-DEV","calendar_id":CALENDAR_ID,"active":True}]
    data["projects"]=[{"id":"PRJ-DEMO","name":"采购协同升级（虚构示例）","project_type":"development","stage":"executing","po_id":"PER-PO","pm_id":"PER-PM","sponsor_id":"PER-SP","service_id":"SVC-PROC","calendar_id":CALENDAR_ID,"baseline_start":"2026-11-02","baseline_finish":"2026-11-20","forecast_finish":"2026-11-23","goal":"验证通用项目与运维共用工作项的数据模型。","summary":"接口数据待补齐，采购验收窗口需协调。","business_impact":"当前预测比基线晚1工作日；生产故障同时占用实施人员，需PO确认优先安排。"}]
    common={"scope":"project","work_type":"feature","node_kind":"task","project_id":"PRJ-DEMO","service_id":"SVC-PROC","priority":"P2","owner_id":"PER-DEV","acceptor_id":"PER-PM","acceptance_criteria":"结果符合约定标准，并保留检查记录"}
    # WBS 单表树：项目 -> 交付物 -> 任务（含嵌套任务），层级节点与任务同表同列。
    data["wbs"]=[
      {"id":"WBS-ROOT","node_kind":"project","project_id":"PRJ-DEMO","title":"采购协同能力","owner_id":"PER-PM","status":"in_progress"},
      {"id":"WBS-REQ","node_kind":"deliverable","project_id":"PRJ-DEMO","title":"确认的业务范围","parent_id":"WBS-ROOT","owner_id":"PER-BA","status":"done","acceptance_standard":"业务负责人确认范围"},
      {"id":"WBS-BUILD","node_kind":"deliverable","project_id":"PRJ-DEMO","title":"可验证的配置与接口","parent_id":"WBS-ROOT","owner_id":"PER-DEV","status":"in_progress","acceptance_standard":"约定接口场景通过"},
      {"id":"WBS-UAT","node_kind":"deliverable","project_id":"PRJ-DEMO","title":"业务验收与移交","parent_id":"WBS-ROOT","owner_id":"PER-BA","status":"ready","acceptance_standard":"验收人确认并归档记录"},
      {**common,"id":"WI-001","title":"需求范围确认","parent_id":"WBS-REQ","owner_id":"PER-BA","acceptor_id":"PER-SP","status":"done","baseline_start":"2026-11-02","baseline_finish":"2026-11-03","forecast_finish":"2026-11-03","actual_start":"2026-11-02","actual_finish":"2026-11-03","completion_evidence":"DEMO-E01：虚构确认记录"},
      {**common,"id":"WI-002","title":"配置与接口验证","parent_id":"WBS-BUILD","status":"in_progress","baseline_start":"2026-11-04","baseline_finish":"2026-11-11","forecast_finish":"2026-11-12","actual_start":"2026-11-04","next_action":"完成剩余接口场景"},
      {**common,"id":"WI-003","title":"验收场景缺陷修复","work_type":"bug","severity":"S2","parent_id":"WBS-BUILD","status":"blocked","baseline_start":"2026-11-04","baseline_finish":"2026-11-06","forecast_finish":"2026-11-09","actual_start":"2026-11-04","priority":"P1","urgency":"soon","impact":"high","blocker_reason":"供应商测试数据不完整","coordinator_id":"PER-PO","next_action":"林岚联系周敏确认数据补齐时间","next_due_at":"2026-11-06T16:00:00+08:00","register_id":"REG-001"},
      {**common,"id":"WI-004","title":"业务验收确认","parent_id":"WBS-UAT","owner_id":"PER-BA","acceptor_id":"PER-SP","status":"ready","baseline_start":"2026-11-16","baseline_finish":"2026-11-20","forecast_finish":"2026-11-23"},
      {"id":"WI-005","node_kind":"task","title":"生产报表导出乱码修复","scope":"operations","work_type":"bug","service_id":"SVC-PROC","status":"in_progress","owner_id":"PER-DEV","acceptor_id":"PER-QA","priority":"P2","severity":"S3","urgency":"normal","impact":"medium","forecast_finish":"2026-11-06","actual_start":"2026-11-05","acceptance_criteria":"目标客户端导出中文不乱码，测试通过"},
      {"id":"WI-006","node_kind":"task","title":"筛选条件体验优化","scope":"operations","work_type":"improvement","service_id":"SVC-PROC","status":"intake","description":"尚未承诺排期；先判断价值、工作量及是否需要立项。"},
      {"id":"WI-007","node_kind":"task","title":"采购对账接口中断恢复","scope":"operations","work_type":"incident","service_id":"SVC-PROC","status":"in_review","owner_id":"PER-DEV","acceptor_id":"PER-BA","priority":"P0","severity":"S1","urgency":"immediate","impact":"critical","priority_reason":"核心对账中断，当前优先恢复，PO协调原项目安排。","forecast_finish":"2026-11-05","actual_start":"2026-11-05","acceptance_criteria":"业务确认恢复且观察期检查通过","register_id":"REG-002"},
      {"id":"WI-008","node_kind":"task","title":"整理下周业务接口参与安排","scope":"adhoc","work_type":"action","service_id":"SVC-PROC","status":"ready","owner_id":"PER-PM","acceptor_id":"PER-PO","priority":"P2","forecast_finish":"2026-11-06","acceptance_criteria":"形成具名参与人、日期和待确认窗口清单","meeting_id":"MTG-OPS"}]
    data["participants"]=[
      {"id":"REL-001","project_id":"PRJ-DEMO","person_id":"PER-SP","role":"stakeholder","starts_on":"2026-11-02","ends_on":"2026-11-23","responsibility":"关键业务取舍与上线确认"},
      {"id":"REL-002","work_item_id":"WI-003","person_id":"PER-VENDOR","role":"interface","starts_on":"2026-11-04","ends_on":"2026-11-09","responsibility":"补齐测试数据"},
      {"id":"REL-003","work_item_id":"WI-003","person_id":"PER-QA","role":"collaborator","responsibility":"缺陷复测"}]
    data["registers"]=[
      {"id":"REG-001","kind":"issue","title":"供应商数据缺失","project_id":"PRJ-DEMO","work_item_id":"WI-003","owner_id":"PER-DEV","coordinator_id":"PER-PO","impact":"推迟缺陷确认及验收准备","deadline_at":"2026-11-06T16:00:00+08:00","status":"open","proposal":"供应商确认补齐安排后重估预测"},
      {"id":"REG-002","kind":"decision","title":"故障恢复期间的项目人员安排","work_item_id":"WI-007","owner_id":"PER-PM","coordinator_id":"PER-PO","decision_by_id":"PER-SP","impact":"同一实施人员处理生产问题和项目接口，需要确认取舍","deadline_at":"2026-11-05T16:00:00+08:00","status":"open"}]
    data["dependencies"]=[{"id":"DEP-001","predecessor_id":"WI-001","successor_id":"WI-002","dependency_type":"FS","lag_workdays":0},
       {"id":"DEP-002","predecessor_id":"WI-002","successor_id":"WI-004","dependency_type":"FS","lag_workdays":0},
       {"id":"DEP-003","predecessor_id":"WI-003","successor_id":"WI-004","dependency_type":"FS","lag_workdays":0}]
    records=[]
    for person in ("PER-PM","PER-DEV","PER-QA"):
      for d in ("2026-10-30","2026-11-02","2026-11-03","2026-11-04"):
        for period in ("AM","PM"):
          for kind in ("planned","actual"):
            records.append({"id":f"DAY-{person}-{d}-{period}-{kind}","person_id":person,"calendar_id":CALENDAR_ID,"date":d,"period":period,"record_kind":kind,"status":"available" if kind=="planned" else "onsite","notes":"虚构出勤示例"})
    for period in ("AM","PM"):
      records.append({"id":f"DAY-PER-DEV-2026-11-06-{period}-planned","person_id":"PER-DEV","calendar_id":CALENDAR_ID,"date":"2026-11-06","period":period,"record_kind":"planned","status":"other_work","notes":"生产支持，需协调项目接口安排"})
    data["day_records"]=records
    # The delivered workspace contains ONE complete project. Bug fixes,
    # improvements and meeting actions belong to that same project.
    data["services"]=[]
    project=data["projects"][0]
    project.update(service_id=None,goal="交付采购协同系统升级，从需求确认、开发联调到业务验收与移交。",
                   summary="核心配置按计划推进；联调数据与业务验收窗口需要本周确认。",
                   business_impact="当前预测比原计划晚1工作日。P0对账异常待业务复核，接口数据缺口可能继续影响验收准备。")
    for person in data["people"]:
        person.update(person_type="vendor" if person["id"]=="PER-VENDOR" else "employee",
                      company="示例实施供应商" if person["id"]=="PER-VENDOR" else "项目所属企业（示例）",
                      project_role=person["expertise"].split("、")[0], starts_on="2026-11-02",ends_on="2026-11-23",
                      email=person["id"].lower()+"@example.invalid")
    for work in data["wbs"]:
        work.update(scope="project",project_id="PRJ-DEMO",service_id=None)
    next(w for w in data["wbs"] if w["id"]=="WI-005").update(title="导出中文编码缺陷修复",parent_id="WBS-BUILD")
    next(w for w in data["wbs"] if w["id"]=="WI-006").update(description="项目中的小优化请求，先确认影响和处理顺序。",parent_id="WBS-BUILD")
    next(w for w in data["wbs"] if w["id"]=="WI-007").update(title="采购对账接口异常复核",priority_reason="对账结果异常影响关键验收场景，本周优先处理。",parent_id="WBS-BUILD",meeting_id="MTG-OPS")
    next(w for w in data["wbs"] if w["id"]=="WI-003").update(waiting_on="等待供应商周敏提供完整联调数据。",meeting_id="MTG-OPS")
    next(w for w in data["wbs"] if w["id"]=="WI-008").update(title="确认下周业务验收参与时间",parent_id="WBS-UAT")
    data["meetings"]=[
      {"id":"MTG-OPS","title":"本周项目进度会","project_id":"PRJ-DEMO","organizer_id":"PER-PO","scheduled_at":"2026-11-05T10:00:00+08:00","status":"held","agenda":"联调数据、P0对账异常复核、业务验收窗口","minutes":"（纪要以下方更新为准）"}]
    data["meetings"][0].update(title="本周项目进度会（虚构）",project_id="PRJ-DEMO",service_id=None,
        agenda="联调数据、P0对账异常复核、业务验收时间",
        minutes="1. 林岚联系供应商周敏，11月6日16:00前确认联调数据补齐。2. 周宁复核P0对账异常处理结果。3. 陈越整理下周业务验收参与时间，林岚协调窗口。")
    for reminder in data["registers"]:
        reminder.update(project_id="PRJ-DEMO",priority="P1")
    data["registers"][1].update(title="P0异常复核与接口开发顺序确认",impact="同一项目的P0复核与接口开发占用同一负责人，需要明确本周顺序。",next_action="业务确认P0结果，PO确认剩余开发安排。")
    data["registers"].extend([
        {"id":"REG-003","kind":"risk","title":"业务验收窗口尚未锁定","project_id":"PRJ-DEMO","work_item_id":"WI-004","priority":"P1","owner_id":"PER-PM","coordinator_id":"PER-PO","impact":"可能影响11月23日的验收安排。","deadline_at":"2026-11-06T14:00:00+08:00","status":"open","next_action":"与业务确认验收参与人及可用时间。"},
        {"id":"REG-004","kind":"reminder","title":"归档本周接口说明与会议纪要","project_id":"PRJ-DEMO","priority":"P2","owner_id":"PER-PM","deadline_at":"2026-11-06T17:00:00+08:00","status":"open","next_action":"补齐项目文档链接与文档维护人。"}
    ])
    data["interfaces"]=[
        {"id":"API-001","name":"采购订单同步","project_id":"PRJ-DEMO","source_system":"采购协同系统","target_system":"ERP","protocol":"http","method":"POST","direction":"outbound","address":"https://api.example.invalid/purchase/orders","description":"提交确认后的采购订单；地址为虚构占位。","owner_id":"PER-DEV","counterparty_id":"PER-VENDOR","status":"developing","expected_ready":"2026-11-12","work_item_id":"WI-002","spec_url":"https://example.invalid/project/interface-spec","auth_note":"企业接口令牌，按接口平台流程申请；不在台账保存密钥。"},
        {"id":"API-002","name":"对账结果回传","project_id":"PRJ-DEMO","source_system":"ERP","target_system":"采购协同系统","protocol":"http","method":"POST","direction":"inbound","address":"/api/reconciliation/callback","description":"回传业务对账结果，当前复核异常场景。","owner_id":"PER-DEV","counterparty_id":"PER-VENDOR","status":"testing","expected_ready":"2026-11-06","work_item_id":"WI-007","notes":"P0复核完成后由业务确认。"},
        {"id":"API-003","name":"历史采购数据交换","project_id":"PRJ-DEMO","source_system":"供应商测试环境","target_system":"采购协同系统","protocol":"file","direction":"inbound","address":"约定的测试文件交接目录（示例）","description":"提供联调用历史样本数据，尚缺部分场景。","owner_id":"PER-DEV","counterparty_id":"PER-VENDOR","status":"blocked","expected_ready":"2026-11-09","work_item_id":"WI-003","notes":"关联供应商数据缺失提醒。"}
    ]
    data["project_documents"]=[
        {"id":"DOC-001","title":"需求与业务规则","project_id":"PRJ-DEMO","document_type":"requirements","url":"https://example.invalid/project/requirements","owner_id":"PER-BA","status":"approved","version_label":"v1.0","updated_on":"2026-11-03","work_item_id":"WI-001","description":"虚构占位链接；正式使用时替换成飞书文档链接。"},
        {"id":"DOC-002","title":"接口设计说明","project_id":"PRJ-DEMO","document_type":"design","url":"https://example.invalid/project/interface-spec","owner_id":"PER-DEV","status":"reviewing","version_label":"v0.8","updated_on":"2026-11-04","work_item_id":"WI-002","description":"虚构占位链接；包含接口地址、字段及异常处理约定。"},
        {"id":"DOC-003","title":"本周项目进度会纪要","project_id":"PRJ-DEMO","document_type":"minutes","url":"https://example.invalid/project/weekly-minutes","owner_id":"PER-PM","status":"approved","version_label":"2026-W45","updated_on":"2026-11-05","meeting_id":"MTG-OPS","description":"虚构占位链接；待办已经关联同一会议，不另建重复任务。"}
    ]
    for row in data["day_records"]:
        if row["status"]=="other_work": row["notes"]="本项目P0复核，需协调接口开发时间。"
    # Examples demonstrate nested tasks and a separate acceptance
    # result. All dates, people and links remain explicitly fictional.
    for person in data["people"]:
        person["feishu_url"]="https://example.invalid/feishu/people/"+person["id"].lower()
    for work in data["wbs"]:
        if work.get("node_kind")!="task": continue
        work["stage"]="acceptance" if work["id"] in ("WI-004","WI-008") else "requirements" if work["id"]=="WI-001" else "integration"
        if work.get("owner_id"): work["member_ids"]=[work["owner_id"]]
        if work.get("baseline_start"): work["forecast_start"]=work["baseline_start"]
    by_id={w["id"]:w for w in data["wbs"]}
    by_id["WI-001"].update(is_acceptance_node=True,actual_person_days=2.5)
    by_id["WI-002"].update(member_ids=["PER-DEV","PER-QA","PER-VENDOR"],backup_member_ids=["PER-PM"])
    by_id["WI-003"].update(member_ids=["PER-DEV","PER-QA","PER-VENDOR"],delay_risk="at_risk",delay_risk_reason="供应商联调数据缺口可能导致原计划无法兑现。",remediation_action="PO确认补齐数据时间，QA并行准备可用场景。",remediation_expected="2026-11-06")
    by_id["WI-004"].update(is_acceptance_node=True)
    by_id["WI-005"].update(baseline_start="2026-11-04",baseline_finish="2026-11-04",forecast_start="2026-11-05",remediation_action="补做客户端编码兼容复测并由QA确认。",remediation_expected="2026-11-06",remediation_actual="2026-11-05",actual_person_days=1.5)
    child={**common,"scope":"project","project_id":"PRJ-DEMO","service_id":None,"parent_id":"WI-002","work_type":"feature","priority":"P2","member_ids":["PER-DEV","PER-QA"]}
    data["wbs"].extend([
        {**child,"id":"WI-002-1","title":"订单接口字段映射确认","stage":"design","status":"done","baseline_start":"2026-11-04","baseline_finish":"2026-11-05","forecast_start":"2026-11-04","forecast_finish":"2026-11-05","actual_start":"2026-11-04","actual_finish":"2026-11-05","completion_evidence":"DEMO-FIELD-MAP：虚构确认记录","actual_person_days":1.0},
        {**child,"id":"WI-002-2","title":"对账回调异常重试实现","stage":"development","status":"in_progress","baseline_start":"2026-11-05","baseline_finish":"2026-11-08","forecast_start":"2026-11-05","forecast_finish":"2026-11-09","actual_start":"2026-11-05","delay_risk":"at_risk","delay_risk_reason":"测试环境白名单待确认，预计比原承诺晚1天。","remediation_action":"请供应商接口人确认白名单，并先完成本地场景测试。","remediation_expected":"2026-11-06","actual_person_days":0.5},
        {**child,"id":"WI-002-3","title":"采购接口验收用例复核","stage":"testing","status":"ready","owner_id":"PER-QA","baseline_start":"2026-11-06","baseline_finish":"2026-11-08","forecast_start":"2026-11-06","forecast_finish":"2026-11-08"}
    ])
    data["deliverables"]=[
        {"id":"DEL-001","project_id":"PRJ-DEMO","work_item_id":"WI-001","title":"需求与业务规则确认版","document_url":"https://example.invalid/project/requirements","document_id":"DOC-001","version_label":"v1.0","submitted_by_id":"PER-BA","submitted_at":"2026-11-03T09:00:00+08:00","acceptor_id":"PER-SP","acceptance_status":"accepted","reviewed_at":"2026-11-03T15:00:00+08:00","acceptance_comments":"虚构验收示例：范围与业务规则符合约定。"},
        {"id":"DEL-002","project_id":"PRJ-DEMO","work_item_id":"WI-002","title":"接口设计说明待验收版","document_url":"https://example.invalid/project/interface-spec","document_id":"DOC-002","version_label":"v0.8","submitted_by_id":"PER-DEV","submitted_at":"2026-11-04T16:00:00+08:00","acceptor_id":"PER-BA","acceptance_status":"pending"},
        {"id":"DEL-003","project_id":"PRJ-DEMO","work_item_id":"WI-003","title":"异常场景复测记录","document_url":"https://example.invalid/project/retest","version_label":"v0.1","submitted_by_id":"PER-QA","submitted_at":"2026-11-05T09:00:00+08:00","acceptor_id":"PER-BA","acceptance_status":"rejected","reviewed_at":"2026-11-05T10:00:00+08:00","acceptance_comments":"虚构示例：缺少供应商样本，尚不能通过验收。"}
    ]
    project["summary"]="接口工作已细分为可追踪todo；编码修复需要补救，联调数据存在延期风险。"
    return data


def operating_demo_data():
    """Fictional v7 workflow examples, as of 2026-11-11; no real destinations."""
    data=demo_data()
    next(p for p in data['people'] if p['id']=='PER-VENDOR').update(feishu_user_id='ou_fictional_vendor',feishu_id_type='open_id',starts_on='2026-11-02',ends_on='2026-11-30')
    data['operations']=[{'id':'OPS-DEMO-1','project_id':'PRJ-DEMO','title':'测试环境日志级别配置调整（虚构）','work_type':'configuration','scope':'operations','status':'blocked','owner_id':'PER-DEV','acceptor_id':'PER-QA','coordinator_id':'PER-PM','priority':'P2','baseline_finish':'2026-11-10','forecast_finish':'2026-11-12','actual_start':'2026-11-09','acceptance_criteria':'日志级别确认并记录回退方案','blocker_reason':'等待环境维护窗口','next_action':'确认维护窗口','next_due_at':'2026-11-11T16:00:00+08:00','delay_reason':'维护窗口冲突','delay_impact':'仅影响测试排查效率；交付影响待确认','remediation_action':'协调次日窗口并准备回退配置','remediation_expected':'2026-11-12','remediation_owner_id':'PER-DEV','related_wbs_id':'WI-002'}]
    data['registers'].append({'id':'REG-DEMO-DEC','kind':'decision','title':'选择测试维护窗口（虚构待审议）','project_id':'PRJ-DEMO','operation_id':'OPS-DEMO-1','owner_id':'PER-PM','decision_by_id':'PER-SP','deadline_at':'2026-11-13T15:00:00+08:00','status':'open','proposal':'方案A：周五晚安排2小时窗口，增加值守；方案B：下周一午间处理，验收材料延后提交。','impact':'请评估维护配合与验收窗口冲突','next_action':'周五会审议窗口及值守安排'})
    data['operations'][0]['decision_id']='REG-DEMO-DEC'
    data['resource_weeks']=[{'id':'RES-DEMO-1','project_id':'PRJ-DEMO','person_id':'PER-VENDOR','week_start':'2026-11-09','net_available_hours':24,'project_hours':18,'operations_hours':4,'buffer_hours':2,'confirmed_at':'2026-11-09T10:00:00+08:00','confirmed_by_id':'PER-VENDOR','notes':'净容量已扣除其他项目；不再扣第二次'}]
    data['daily_confirmations']=[{'id':'DC-DEMO-AM','project_id':'PRJ-DEMO','person_id':'PER-VENDOR','date':'2026-11-11','period':'AM','work_location':'remote','allocation_mode':'shared','available_hours':3,'project_hours':2,'operations_hours':1,'response_status':'confirmed','confirmed_at':'2026-11-11T09:00:00+08:00','confirmed_by_id':'PER-VENDOR','source':'fictional:daily-reply','future_leave_change':'changed','future_leave_note':'已登记下周三半日休假计划，尚待确认','leave_plan_id':'LEAVE-DEMO-1'}]
    data['leave_plans']=[{'id':'LEAVE-DEMO-1','project_id':'PRJ-DEMO','person_id':'PER-VENDOR','start_date':'2026-11-18','end_date':'2026-11-18','start_period':'PM','end_period':'PM','status':'proposed','backup_id':'PER-DEV','source':'fictional:leave-intent'}]
    data['weekly_commitments']=[{'id':'WC-DEMO-1','project_id':'PRJ-DEMO','work_item_id':'WI-002','week_start':'2026-11-09','outcome':'完成两项异常重试场景验证','due_date':'2026-11-13','owner_id':'PER-DEV','verification':'保存测试记录并由QA确认','status':'confirmed','confirmed_at':'2026-11-09T10:00:00+08:00','confirmed_by_id':'PER-PM'}]
    next(w for w in data['wbs'] if w['id']=='WI-003').update(delay_reason='联调数据仍未补齐',delay_impact='异常场景验收等待',remediation_owner_id='PER-PO',progress_note='已完成现有样本验证，等待增补样本',progress_confirmed_at='2026-11-11T09:00:00+08:00',progress_confirmed_by_id='PER-DEV')
    return data
