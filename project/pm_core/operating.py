"""Evidence-backed management projections. No network or implicit business writes."""
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo
from .store import parse_datetime
from .tracking import tracking_data, DELAY_LABELS


def context(data, now):
    instant = parse_datetime(now) if isinstance(now, str) else now
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ValueError("管理时点必须带时区")
    projects = data.get("projects", [])
    if len(projects) != 1:
        raise ValueError("需要且仅能有一个项目")
    project = projects[0]
    calendar = next((c for c in data.get("calendars", []) if c["id"] == project.get("calendar_id")), {})
    zone = calendar.get("timezone")
    if not zone:
        raise ValueError("项目日历时区待配置")
    return project, instant, instant.astimezone(ZoneInfo(zone)), zone


def execution_rows(data):
    parents = {w.get("parent_id") for w in data.get("wbs", []) if w.get("scope") == "project"}
    return [("wbs", w) for w in data.get("wbs", []) if w.get("scope") == "project" and w.get("node_kind") == "task" and w["id"] not in parents] + [("operations", w) for w in data.get("operations", []) if not w.get("promoted_wbs_id")]


def operation_delay(w, today):
    due = w.get("baseline_finish") or w.get("forecast_finish")
    actual = w.get("actual_finish")
    status = w.get("status")
    state = "unknown"
    if status == "cancelled": state = "cancelled"
    elif due:
        if status == "done": state = "unknown" if not actual else "late_done" if actual > due else "on_track"
        elif due < today: state = "overdue"
        elif w.get("delay_risk") == "at_risk" or (w.get("forecast_finish") or due) > due or status == "blocked": state = "risk"
        else: state = "on_track"
    return {"delay_state": state, "delay_state_label": DELAY_LABELS[state], "commitment_finish": due,
            "commitment_source": "原计划完成日" if w.get("baseline_finish") else "当前预计日（尚未设原计划）"}


def delay_review(data, *, now):
    project, _, local, _ = context(data, now)
    tracking = tracking_data(data, now)
    registers = {r["id"]: r for r in data.get("registers", [])}
    rows = []
    for table, item in execution_rows(data):
        if item.get("project_id") != project["id"]: continue
        facts = tracking["tasks"][item["id"]] if table == "wbs" else operation_delay(item, local.date().isoformat())
        if facts["delay_state"] not in ("overdue", "late_done", "risk"): continue
        decision = registers.get(item.get("decision_id"), {})
        rows.append({"table": table, "id": item["id"], "title": item["title"], "owner_id": item.get("owner_id"),
            **{k: facts.get(k) for k in ("delay_state", "delay_state_label", "commitment_finish", "commitment_source")},
            **{k: item.get(k) for k in ("forecast_finish", "actual_finish", "delay_reason", "delay_impact", "delay_risk_reason", "remediation_action", "remediation_owner_id", "remediation_expected", "remediation_actual", "remediation_result", "decision_id", "source_url", "meeting_id")},
            "decision": decision})
    return rows


def commitment_review(data, *, now, baseline_data=None):
    project, _, local, _ = context(data, now)
    start = (local.date()-timedelta(days=local.weekday())).isoformat()
    source = baseline_data if baseline_data is not None else data
    rows = [r for r in source.get("weekly_commitments", []) if r.get("project_id") == project["id"] and r.get("week_start") == start and ((baseline_data is not None and r.get("status") != "cancelled") or (baseline_data is None and r.get("confirmed_at")))]
    current = {r["id"]: r for r in data.get("weekly_commitments", [])}
    result = []
    for original in rows:
        r = current.get(original["id"])
        completed = r.get("completed_at") if r else None
        completed_day = parse_datetime(completed).astimezone(local.tzinfo).date().isoformat() if completed else None
        due = original.get("due_date")
        state = "missing" if not r else "cancelled" if r.get("status") == "cancelled" else "pending"
        if r and r.get("status") == "done" and completed_day and r.get("result_evidence"):
            state = "accepted_on_time" if due and completed_day <= due else "accepted_late"
        elif r and state != "cancelled" and due and due < local.date().isoformat(): state = "overdue"
        result.append(dict(original, outcome_state=state, current_result=r, comparison_due=due))
    return result


def collection_schedule(data, *, now, config=None):
    """Resolve actual meeting dates; never silently move a holiday meeting."""
    config = config or {}
    project, _, local, _ = context(data, now)
    start = local.date()-timedelta(days=local.weekday())
    from .project_calendar import workday_status
    schedule = {}
    for kind, offset in (("plan", 0), ("review", 4)):
        day = config.get(kind+"_date")
        meeting_id = config.get(kind+"_meeting_id")
        reason = None
        if meeting_id:
            meeting = next((r for r in data.get("meetings", []) if r["id"] == meeting_id and r.get("project_id") == project["id"] and r.get("status") != "cancelled"), None)
            if not meeting: reason = "指定会议不存在、已取消或不属于本项目"
            else: day = parse_datetime(meeting["scheduled_at"]).astimezone(local.tzinfo).date().isoformat()
        day = day or (start+timedelta(days=offset)).isoformat()
        selected = date.fromisoformat(day)
        if not start <= selected <= start+timedelta(days=6): reason = "配置的会议日期不在本周，需更新实际会议实例"
        effective = workday_status(data, project["id"], day)
        if not effective.get("verified") or not effective.get("is_workday"):
            reason = reason or "例会日期为非工作日或尚未核验，需明确改期及填写日期"
        schedule[kind] = {"date": day, "ready": reason is None, "reason": reason, "meeting_id": meeting_id}
    return schedule


def plan_followups(data, *, now, config=None):
    """Plan task reminders only; caller combines attendance and enforces delivery policy."""
    config = config or {}
    project, instant, local, zone = context(data, now)
    today = local.date().isoformat()
    if project.get("stage") in ("closed", "paused"): return []
    from .project_calendar import workday_status
    day = workday_status(data, project["id"], today)
    if not day.get("is_workday") or not day.get("verified"): return []
    schedule = collection_schedule(data, now=now, config=config)
    is_plan = schedule["plan"]["ready"] and schedule["plan"]["date"] == today
    is_review = schedule["review"]["ready"] and schedule["review"]["date"] == today
    lead = config.get("due_soon_days", 2)
    stale = config.get("stale_days", 3)
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (lead, stale)):
        raise ValueError("提醒阈值必须是非负整数")
    output = []
    def add(kind, table, row, fields, reason, deadline=None, recipient=None):
        pid = recipient or row.get("owner_id") or project.get("pm_id")
        output.append({"key": f"{project['id']}:{today}:{table}:{row['id']}:{kind}:{pid}",
            "project_id": project["id"], "local_date": today, "timezone": zone, "kind": kind,
            "table": table, "record_id": row["id"], "title": row.get("title", row["id"]),
            "recipient_id": pid, "required_fields": fields, "reason": reason, "deadline": deadline,
            "stop_condition": "发送前重新检查；记录关闭或指定资料已补齐时停止本项催办"})
    for kind, slot in schedule.items():
        if not slot["ready"]:
            add("schedule_pending_"+kind, "projects", project, [kind+"_date", kind+"_meeting_id"], slot["reason"], recipient=project.get("pm_id"))
    for table, w in execution_rows(data):
        if w.get("project_id") != project["id"] or w.get("status") in ("done", "cancelled"): continue
        due = w.get("baseline_finish") or w.get("forecast_finish")
        required = [f for f in ("owner_id", "forecast_start", "forecast_finish", "acceptance_criteria") if not w.get(f)]
        relevant = w.get("status") in ("in_progress", "blocked", "in_review") or (w.get("forecast_start") or w.get("forecast_finish") or "9999") <= (local.date()+timedelta(days=6-local.weekday())).isoformat()
        if is_plan and relevant:
            link_field = "work_item_id" if table == "wbs" else "operation_id"
            week_start = (local.date()-timedelta(days=local.weekday())).isoformat()
            planned = any(r.get(link_field) == w["id"] and r.get("week_start") == week_start and r.get("outcome") and r.get("due_date") and r.get("verification") for r in data.get("weekly_commitments", []))
            if not planned: required += ["weekly_commitments.outcome", "weekly_commitments.due_date", "weekly_commitments.verification"]
            if required: add("monday_plan", table, w, required, "本周拟推进工作需补计划/可验证成果", config.get("plan_cutoff"))
        facts = operation_delay(w, today)
        if not relevant and facts["delay_state"] not in ("overdue", "risk"):
            continue  # Future work is not an everyday progress-report obligation.
        if facts["delay_state"] == "overdue":
            fields = [f for f in ("delay_reason", "delay_impact", "remediation_action", "remediation_owner_id", "remediation_expected") if not w.get(f)]
            add("overdue", table, w, fields or ["progress_note", "forecast_finish"], "已超过对照日期，需确认结果及补救进展", due, w.get("acceptor_id") if w.get("status") == "in_review" else None)
        elif facts["delay_state"] == "risk":
            add("forecast_risk", table, w, ["delay_risk_reason", "delay_impact", "remediation_action", "remediation_expected"], "尚未逾期，但存在预测推迟或阻塞信号", due)
        elif due and 0 <= (date.fromisoformat(due)-local.date()).days <= lead:
            add("due_soon", table, w, ["progress_note", "forecast_finish"], "进入到期提醒窗口，尚未判定延期", due, w.get("acceptor_id") if w.get("status") == "in_review" else None)
        if w.get("status") == "blocked": add("blocked", table, w, ["blocker_reason", "next_action", "next_due_at"], "已登记阻塞，需落实解除行动", w.get("next_due_at"), w.get("coordinator_id"))
        if w.get("remediation_expected") and w["remediation_expected"] < today and not (w.get("remediation_actual") and w.get("remediation_result")):
            add("remediation_due", table, w, ["remediation_actual", "remediation_result"], "补救措施到期且尚无完整落实依据", w["remediation_expected"], w.get("remediation_owner_id"))
        if w.get("next_due_at") and parse_datetime(w["next_due_at"]) < instant:
            add("next_action_due", table, w, ["next_action", "next_due_at", "progress_note"], "下一步跟进期限已过，需确认结果或新安排", w["next_due_at"], w.get("coordinator_id"))
        threshold = (config.get("age_threshold_by_type") or {}).get(w.get("work_type"))
        if threshold is not None:
            if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0: raise ValueError("在制年龄阈值必须为非负整数自然日")
            if w.get("actual_start") and (local.date()-date.fromisoformat(w["actual_start"])).days > threshold:
                add("work_age", table, w, ["progress_note", "forecast_finish", "next_action"], "超过该类型配置的在制年龄，需核实；不推断停工")
        confirmed = w.get("progress_confirmed_at")
        aged = not confirmed or (instant-parse_datetime(confirmed)).total_seconds() >= stale*86400
        if aged or (is_review and (not confirmed or parse_datetime(confirmed).astimezone(local.tzinfo).date() < local.date())):
            add("friday_result" if is_review else "stale_progress", table, w, ["progress_note", "progress_confirmed_at", "forecast_finish"], "进展尚无近期业务确认；不推断未开展工作", config.get("result_cutoff") if is_review else None)
    for r in data.get("registers", []):
        if r.get("project_id") != project["id"] or r.get("status") in ("closed", "rejected"): continue
        deadline = r.get("deadline_at")
        if deadline and parse_datetime(deadline) <= instant:
            add("decision_due" if r.get("kind") == "decision" else "risk_review" if r.get("kind") == "risk" else "register_due", "registers", r, ["next_action", "deadline_at", "status"], "登记事项已到跟进期限", deadline, r.get("decision_by_id") or r.get("owner_id"))
    for d in data.get("deliverables", []):
        if d.get("project_id") != project["id"]: continue
        if d.get("acceptance_status") == "pending":
            add("awaiting_acceptance", "deliverables", d, ["acceptance_status", "reviewed_at", "acceptance_comments"], "已提交待验收，需验收人确认结果或约定期限", recipient=d.get("acceptor_id"))
        elif d.get("acceptance_status") == "rejected":
            task = next((w for w in data.get("wbs", []) if w["id"] == d.get("work_item_id")), {})
            add("rework", "deliverables", d, ["version_label", "submitted_at"], "验收退回，需要明确返工与重新提交安排", recipient=task.get("owner_id"))
    index = {w["id"]: w for w in data.get("wbs", []) if w.get("scope") == "project"}
    from .calendar import workday_distance
    days = [d for d in data.get("calendar_days", []) if d["calendar_id"] == project["calendar_id"]]
    for dep in data.get("dependencies", []):
        pre, suc = index.get(dep.get("predecessor_id")), index.get(dep.get("successor_id"))
        if not pre or not suc or suc.get("status") in ("done", "cancelled") or pre.get("status") == "done": continue
        a, b = pre.get("forecast_finish"), suc.get("forecast_start")
        if a and b:
            try: gap = workday_distance(a, b, days)
            except ValueError: continue
            if gap < (dep.get("lag_workdays") or 0):
                add("dependency_risk", "wbs", suc, ["forecast_start", "waiting_on", "next_action"], f"显式前置 {pre['id']} 的预测与后续开始约束冲突；请两端核实", b, suc.get("coordinator_id"))
    escalation = config.get("escalation_after_days")
    if escalation is not None:
        if isinstance(escalation, bool) or not isinstance(escalation, int) or escalation < 0: raise ValueError("升级阈值必须为非负整数自然日")
        for item in list(output):
            deadline = item.get("deadline")
            if item["kind"] not in ("overdue", "remediation_due", "decision_due", "next_action_due") or not deadline: continue
            if (local.date()-date.fromisoformat(deadline[:10])).days >= escalation and item["recipient_id"] != project.get("pm_id"):
                output.append(dict(item, key=item["key"]+":escalation", kind="escalation", recipient_id=project.get("pm_id"), reason="已超过配置升级期限；"+item["reason"]))
    return output


def management_review(data, *, now, period="week", prior_data=None, lookahead_weeks=6, schedule_audit=None):
    if period not in ("week", "month"): raise ValueError("期间只支持 week 或 month")
    if isinstance(lookahead_weeks, bool) or not isinstance(lookahead_weeks, int) or not 1 <= lookahead_weeks <= 12:
        raise ValueError("资源展望范围应为1至12周")
    project, instant, local, zone = context(data, now)
    start = local.date().replace(day=1) if period == "month" else local.date()-timedelta(days=local.weekday())
    today = local.date().isoformat()
    grouped = {}
    for table in ("wbs", "operations"):
        rows = [r for t, r in execution_rows(data) if t == table and r.get("project_id") == project["id"]]
        grouped[table] = {"total": sum(r.get("status") != "cancelled" for r in rows), "done": sum(r.get("status") == "done" for r in rows),
            "completed_in_period": [r["id"] for r in rows if r.get("status") == "done" and r.get("actual_finish") and start.isoformat() <= r["actual_finish"] <= today],
            "open": sum(r.get("status") not in ("done", "cancelled") for r in rows)}
    prior = next((p for p in (prior_data or {}).get("projects", []) if p["id"] == project["id"]), None)
    from .resources import capacity_summary, confirmation_scope
    week = (local.date()-timedelta(days=local.weekday())).isoformat()
    forecast = []
    for offset in range(lookahead_weeks):
        first = date.fromisoformat(week)+timedelta(days=7*offset)
        last = first+timedelta(days=6)
        known = {r['person_id']:r for r in capacity_summary(data,project['id'],week_start=first.isoformat())['people']}
        people = []
        for person in data.get('people', []):
            if not person.get('active'): continue
            if person.get('starts_on') and person['starts_on'] > last.isoformat(): continue
            if person.get('ends_on') and person['ends_on'] < first.isoformat(): continue
            row = known.get(person['id'])
            leaves = [r for r in data.get('leave_plans', []) if r.get('project_id')==project['id'] and r.get('person_id')==person['id'] and r.get('status')!='cancelled' and r['start_date']<=last.isoformat() and r['end_date']>=first.isoformat()]
            scope = confirmation_scope(person)
            if scope == 'employee_excluded' and not row and not leaves:
                continue
            note = ('员工不参加资源确认；保留已登记资源与休假事实' if scope == 'employee_excluded' else
                    '成员类型待核实，暂不发起人力资源确认' if scope == 'identity_unknown' else
                    '休假与净容量需核对；不重复扣减' if leaves else '未记录容量保持待确认' if not row else '已记录周安排')
            people.append(dict(row or {'person_id':person['id'],'week_start':first.isoformat(),'net_available_hours':None,'project_hours':None,'operations_hours':None,'buffer_hours':None,'remaining_hours':None,'status':'pending'},
                leave_plan_ids=[r['id'] for r in leaves], leave_plans=leaves,
                confirmation_scope=scope, confirmation_required=scope == 'non_employee', availability_note=note))
        forecast.append({'week_start':first.isoformat(),'week_end':last.isoformat(),'people':people})
    checks = [{"name": "目标和范围", "present": bool(project.get("goal")), "source": "projects:"+project["id"]},
              {"name": "基线日期", "present": bool(project.get("baseline_start") and project.get("baseline_finish")), "source": "projects:"+project["id"]},
              {"name": "责任和决定人", "present": bool(project.get("pm_id") and project.get("sponsor_id")), "source": "projects:"+project["id"]}]
    deliveries = [d for d in data.get("deliverables", []) if d.get("project_id") == project["id"]]
    if project.get("stage") in ("accepting", "closed"):
        checks.append({"name": "交付物验收证据", "present": bool(deliveries) and all(d.get("acceptance_status") == "accepted" and d.get("reviewed_at") for d in deliveries), "source": "deliverables"})
        checks.append({"name": "移交资料", "present": any(d.get("document_type") == "runbook" and d.get("status") == "approved" for d in data.get("project_documents", [])), "source": "project_documents"})
    from .attendance import plan_daily_confirmations
    daily_status = plan_daily_confirmations(data, project["id"], instant)
    return {"project_id": project["id"], "as_of": instant.isoformat(), "timezone": zone, "period": period, "period_start": start.isoformat(), "period_end": today,
        "schedule_audit": schedule_audit, "counts": grouped, "delays": delay_review(data, now=now), "commitments": commitment_review(data, now=now),
        "resources": capacity_summary(data, project["id"], week_start=week),
        "resource_forecast": forecast, "lookahead_weeks": lookahead_weeks,
        "daily_status": daily_status,
        "daily_confirmations": [r for r in data.get("daily_confirmations", []) if r.get("project_id") == project["id"] and r.get("date") == today],
        "leave_plans": [r for r in data.get("leave_plans", []) if r.get("project_id") == project["id"] and r.get("status") != "cancelled" and (r.get("end_date") or "") >= today],
        "forecast_comparison": {"previous": prior.get("forecast_finish"), "current": project.get("forecast_finish")} if prior else None,
        "stage": project.get("stage"), "stage_checks": checks, "stage_decision": "需有权人员依据证据确认；检查齐备不自动通过阶段",
        "limitations": ["完成条数不是工作量完成率；容量不从签到推算。", "月度按实际完成日去重，不累加重叠周报。", "无历史快照时不重构历史预测。"]}


def render_management_section(data, *, as_of, phase="pre_meeting", period="week", lookahead_weeks=6, schedule_audit=None, decisions_html=""):
    from .resources import confirmation_scope
    if phase not in ("pre_meeting", "post_meeting"): raise ValueError("未知会议材料阶段")
    review = management_review(data, now=as_of, period=period, lookahead_weeks=lookahead_weeks, schedule_audit=schedule_audit)
    people = {p["id"]: p.get("name", p["id"]) for p in data.get("people", [])}
    h = lambda x: escape(str("待确认" if x is None or x == "" else x), quote=True)
    gap = lambda text="待确认": '<span class="missing">'+escape(text)+'</span>'
    val = lambda x, missing="待确认": gap(missing) if x is None or x == "" else escape(str(x), quote=True)
    person = lambda x: h(people.get(x, "待指定"))
    pval = lambda x: escape(people[x], quote=True) if x in people else gap("待指定")
    from .schema import ENUMS
    from .project_report import _link, _kpi
    enum_label = lambda enum, value: h(ENUMS.get(enum, {}).get(value, value))
    local_cutoff = parse_datetime(review["as_of"]).astimezone(ZoneInfo(review["timezone"])).strftime("%Y-%m-%d %H:%M")+" · "+review["timezone"]
    moment = lambda v: parse_datetime(v).astimezone(ZoneInfo(review["timezone"])).strftime("%m-%d %H:%M") if v else None
    pre = phase == "pre_meeting"
    out = ['<div class="section-head"><h2>延期与补救审议</h2><span class="phase-chip'+('' if pre else ' post')+'">'+("会前审议版" if pre else "会后记录版")+'</span></div>'
           '<p class="note mgmt-note">'+("会前审议版：措施和建议不代表已批准决定。" if pre else "会后记录版：仅展示已有决定与落实事实，未决项继续跟进。")+' 数据截至 '+h(local_cutoff)+'</p>']
    delays = review["delays"]
    reason_of = lambda r: r["delay_risk_reason"] if r["delay_state"] == "risk" else r["delay_reason"]
    total = len(delays)
    ratio = lambda n: f"{n}/{total}" if total else "0"
    explained = sum(bool(reason_of(r)) for r in delays)
    remedied = sum(bool(r.get("remediation_action")) for r in delays)
    landed = sum(bool(r.get("remediation_actual") and r.get("remediation_result")) for r in delays)
    decided = sum(bool((r.get("decision") or {}).get("title")) for r in delays)
    lacking = lambda n: "warning" if n < total else "good"
    out.append('<div class="kpi-row">'
               + _kpi("偏差事项", total, tone="critical" if total else "neutral", sub="项目 "+str(sum(r["table"] == "wbs" for r in delays))+" · 运维 "+str(sum(r["table"] == "operations" for r in delays)))
               + _kpi("已延期·未完成", sum(r["delay_state"] == "overdue" for r in delays), tone="critical" if any(r["delay_state"] == "overdue" for r in delays) else "neutral", sub="项目 "+str(sum(r["delay_state"] == "overdue" and r["table"] == "wbs" for r in delays))+" · 运维 "+str(sum(r["delay_state"] == "overdue" and r["table"] == "operations" for r in delays)))
               + _kpi("已说明原因", ratio(explained), tone=lacking(explained), meter=explained/total*100 if total else 0, sub="其余待补说明")
               + _kpi("已有补救措施", ratio(remedied), tone=lacking(remedied), meter=remedied/total*100 if total else 0, sub="措施、责任与期限")
               + _kpi("补救已落实", ratio(landed), tone=lacking(landed), meter=landed/total*100 if total else 0, sub="需落实时间与结果")
               + _kpi("已登记审议", ratio(decided), tone=lacking(decided), meter=decided/total*100 if total else 0, sub="关联待决定事项")
               + '</div>')
    for table, label in (("wbs", "项目交付"), ("operations", "运维与优化事项")):
        rows = [r for r in delays if r["table"] == table]
        out.append('<h3 class="block-title">'+label+' <span class="count">'+str(len(rows))+'</span></h3>')
        if not rows:
            out.append('<div class="empty">暂无已登记偏差；不等于无风险。</div>')
            continue
        out.append('<div class="delay-list">')
        for r in rows:
            d = r["decision"] or {}
            state = r["delay_state"]
            source = '<small>事项来源: '+(_link(r.get('source_url'), r.get('source_url')) if r.get('source_url') else gap('待补来源链接'))+'</small>'
            meeting = next((m for m in data.get('meetings', []) if m['id']==r.get('meeting_id')), {})
            source += '<small>来源会议: '+(_link(meeting.get('minutes_url'), meeting.get('title') or r.get('meeting_id')) if meeting or r.get('meeting_id') else gap('未关联会议'))+'</small>'
            main = ('<div class="dc-main"><b>'+h(r["title"])+'</b><small>'+h(label+' · '+r["id"])+' · '+person(r["owner_id"])+'</small>'
                    '<span class="delay-badge '+h(state)+'">'+h(r["delay_state_label"])+'</span>'
                    '<div class="date-shift"><span>'+h(r["commitment_source"])+'</span>'+h(r["commitment_finish"])+' → <span class="to">'+h(r["actual_finish"] or r["forecast_finish"])+'</span></div>'
                    '<div class="dc-source">'+source+'</div></div>')
            explain = ('<div class="dc-field"><label>'+("延期风险说明" if state == "risk" else "延期说明")+' / 影响</label><div class="val">'+val(reason_of(r))+'</div>'
                       '<small><span class="k">影响</span> '+val(r["delay_impact"])+'</small></div>')
            remedy = ('<div class="dc-field"><label>解决措施 / 责任与期限</label><div class="val">'+val(r["remediation_action"])+'</div>'
                      '<small><span class="k">责任</span> '+pval(r["remediation_owner_id"])+' · <span class="k">期限</span> '+val(r["remediation_expected"])+'</small>'
                      +('<small><span class="k">落实:</span> '+val(r["remediation_actual"])+' / '+val(r["remediation_result"])+'</small>' if r["remediation_action"] or r["remediation_actual"] or r["remediation_result"] else '')+'</div>')
            if d.get("title"):
                decision = ('<div class="dc-field"><label>审议与实际决定</label><div class="val">'+h(d.get("title"))+'</div>'
                            '<small><span class="k">方案</span> '+val(d.get("proposal"))+'</small>'
                            '<small><span class="k">决定人</span> '+pval(d.get("decision_by_id"))+' · '+val(moment(d.get("deadline_at")))+'</small>'
                            '<small><span class="k">状态</span> '+enum_label("register_status", d.get("status"))+' · <span class="k">决定</span> '+val(d.get("decision"))+' · <span class="k">依据</span> '+val(d.get("closure_evidence"))+'</small></div>')
            else:
                decision = '<div class="dc-field"><label>审议与实际决定</label><div class="val">'+gap("未登记审议事项")+'</div></div>'
            out.append('<article class="delay-card '+h(state)+'" data-delay-id="'+h(r["id"])+'">'+main+explain+remedy+decision+'</article>')
        out.append('</div>')
    out.append(decisions_html)
    out.append('<h3 class="block-title">人员资源与今日安排</h3><p class="note">净可用容量含本项目及对应运维；休假不重复扣减，未回复不等于缺勤。</p><div class="mgmt-grid"><section><h3>今日工作安排</h3>')
    confirmations = review["daily_confirmations"]
    if confirmations:
        out.append('<div class="confirm-list">')
        for r in confirmations:
            status = r.get("response_status")
            out.append('<div class="confirm-item"><div class="head"><b>'+person(r.get("person_id"))+' · '+enum_label('period', r.get("period"))+'</b><span class="chip '+('ok' if status in ('confirmed', 'corrected') else 'warn')+'">'+enum_label('response_status', status)+'</span></div>'
                       '<small>'+enum_label('location', r.get("work_location"))+' · '+enum_label('allocation_mode', r.get("allocation_mode"))+' · 净可用小时: '+h(r.get('available_hours'))+'</small>'
                       '<small>未来休假: '+enum_label('future_leave_change', r.get('future_leave_change') or 'unknown')+(' · '+h(r.get('future_leave_note')) if r.get('future_leave_note') else '')+'</small></div>')
        out.append('</div>')
    else:
        out.append('<div class="empty">今日尚无工作安排确认记录。</div>')
    missing = review["daily_status"].get("eligible", [])
    pending = review["daily_status"].get("pending", [])
    out.append('<div class="chip-row"><b>尚需确认</b>'+(''.join('<span class="chip warn">'+h(people.get(r['person_id'],r['person_id'])+' '+','.join(ENUMS['period'].get(p,p) for p in r['periods']))+'</span>' for r in missing) or '<span class="chip">无可确认的待回复名单</span>')+'</div>')
    reason_labels = {'calendar_pending':'日历待核验', 'project_window_missing':'项目起止日期待补', 'reminder_window_expired_or_not_started':'提醒有效期需确认', 'identity_unknown':'成员身份待确认', 'participation_window_missing':'参与窗口待补', 'mention_identity_missing':'飞书成员映射待补'}
    out.append('<div class="chip-row"><b>签到资料缺口</b>'+(''.join('<span class="chip warn">'+h(people.get(r.get('person_id'),'项目配置')+': '+reason_labels.get(r['reason'], '需核对成员与日历配置'))+'</span>' for r in pending) or '<span class="chip ok">无</span>')+'</div></section>')
    resources = review["resources"]
    rows = resources.get("people", resources.get("rows", [])) if isinstance(resources, dict) else resources
    states = {'pending':'待确认','available':'容量已确认','overallocated':'已分配超出容量','buffer_conflict':'预留缓冲不足'}
    scopes = {p['id']: confirmation_scope(p) for p in data.get('people', [])}
    number = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else None
    cards = []
    for r in rows:
        net = number(r.get("net_available_hours"))
        parts = [("cap-project", "项目", "var(--info)", r.get("project_hours")), ("cap-ops", "运维", "var(--cap-ops)", r.get("operations_hours")), ("cap-buffer", "缓冲", "var(--neutral)", r.get("buffer_hours"))]
        used = sum(number(v) or 0 for _, _, _, v in parts)
        scale = max(net or 0, used)
        if net is not None and scale > 0:
            bar = '<div class="cap-bar" role="img" aria-label="净可用 '+h(net)+' 小时，项目 '+h(r.get("project_hours"))+'，运维 '+h(r.get("operations_hours"))+'，缓冲 '+h(r.get("buffer_hours"))+'">'+''.join('<i class="'+cls+'" style="width:'+f'{(number(v) or 0)/scale*100:.2f}'+'%" title="'+name+' '+h(v)+' 小时"></i>' for cls, name, _, v in parts if number(v))+'</div>'
        else:
            bar = '<div class="cap-bar unknown" title="容量未记录"></div>'
        state = states.get(r.get('status'), '待核对')
        if r.get('status') == 'pending' and r.get('confirmation_scope') != 'non_employee':
            state = '资料未完整 · 员工免确认' if r.get('confirmation_scope') == 'employee_excluded' else '成员类型待核实'
        number_text = lambda x: h(x) if x is not None else ('未记录' if not r.get('confirmation_required') else '待确认')
        cards.append('<div class="person-card"><div class="who"><b>'+person(r.get("person_id"))+'</b><span class="chip '+('ok' if r.get('status') == 'available' else 'warn')+'">'+h(state)+'</span></div>'+bar
                     +'<div class="cap-legend cap-nums">'+''.join('<span style="--k:'+color+'">'+name+' '+number_text(v)+'</span>' for _, name, color, v in parts)+'</div>'
                     +'<small class="cap-nums">净可用 '+number_text(r.get("net_available_hours"))+' 小时 · 可再安排 '+number_text(r.get("remaining_hours"))+' 小时</small></div>')
    out.append('<section><h3>本周容量（小时）</h3><div class="people-grid">'+(''.join(cards) or '<div class="empty">本周尚无容量记录；未记录保持待确认。</div>')+'</div></section>')
    leaves = ''.join('<li><b>'+person(r.get('person_id'))+'</b><span class="cap-nums">'+h(r.get('start_date'))+' '+enum_label('period', r.get('start_period'))+' — '+h(r.get('end_date'))+' '+enum_label('period', r.get('end_period'))+'<small>影响 '+val(r.get('impact'))+'</small></span><span><span class="chip '+('ok' if r.get('status') == 'confirmed' else 'warn')+'">'+enum_label('plan_status', r.get('status'))+'</span><small>交接 '+pval(r.get('backup_id'))+'</small></span></li>' for r in review['leave_plans'])
    checks = ''.join('<li class="'+('ok' if r["present"] else 'gap')+'"><span class="mark">'+('✓' if r["present"] else '!')+'</span>'+h(r["name"])+'<span class="state">'+('证据已登记' if r["present"] else '待补证据')+'</span></li>' for r in review["stage_checks"])
    out.append('<section><h3>未来休假与交接</h3>'+('<ul class="leave-list">'+leaves+'</ul>' if leaves else '<div class="empty">尚无未来休假记录。</div>')+'</section>'
               '<section><h3>阶段检查</h3><ul class="check-list">'+checks+'</ul><p class="note">'+h(review["stage_decision"])+'</p></section></div>')
    if period == 'month':
        weeks = review['resource_forecast']
        order = list(dict.fromkeys(r['person_id'] for week in weeks for r in week['people']))
        head = ''.join('<th title="'+h(w['week_start'])+' — '+h(w['week_end'])+'">'+h(w['week_start'][5:])+' — '+h(w['week_end'][5:])+'</th>' for w in weeks)
        body = []
        for pid in order:
            cells = []
            for week in weeks:
                r = next((x for x in week['people'] if x['person_id'] == pid), None)
                if r is None:
                    cells.append('<td><span class="fc-cell outside">'+('员工免确认' if scopes.get(pid) == 'employee_excluded' else '不在参与期')+'</span></td>'); continue
                leave = '；'.join(str(x['start_date'])+'—'+str(x['end_date'])+' '+ENUMS['plan_status'].get(x['status'],x['status']) for x in r['leave_plans'])
                short = '；'.join((str(x['start_date'])[5:] if x['start_date'] == x['end_date'] else str(x['start_date'])[5:]+'—'+str(x['end_date'])[5:])+' '+ENUMS['plan_status'].get(x['status'],x['status']) for x in r['leave_plans'])
                leave_html = '<span class="fc-leave" title="休假 '+h(leave)+'">休假 '+h(short)+'</span>' if leave else ''
                net = number(r.get('net_available_hours'))
                if net is None:
                    missing_label = '待确认' if r['confirmation_required'] else '员工免确认' if r['confirmation_scope'] == 'employee_excluded' else '成员类型待核实'
                    cells.append('<td><span class="fc-cell unknown" title="'+h(r['availability_note'])+'"><b>'+missing_label+'</b><small>未记录容量</small>'+leave_html+'</span></td>')
                else:
                    remaining = number(r.get('remaining_hours'))
                    cells.append('<td><span class="fc-cell known'+(' full' if remaining is not None and remaining <= 0 else '')+'" title="'+h(r['availability_note'])+'"><b>净 '+h(r.get('net_available_hours'))+' h</b><small>项目 '+h(r.get('project_hours'))+' · 运维 '+h(r.get('operations_hours'))+' · 余 '+h(r.get('remaining_hours'))+'</small>'+leave_html+'</span></td>')
            body.append('<tr><td><b>'+person(pid)+'</b></td>'+''.join(cells)+'</tr>')
        out.append('<h3 class="block-title">未来 '+str(lookahead_weeks)+' 周资源展望</h3><p class="note">按周展示已有净容量及分配，不累加为月容量；未确认保持未知。休假需与已有净容量核对，不再扣减一次。</p>'
                   '<div class="table-scroll"><table class="forecast-grid"><thead><tr><th>成员</th>'+head+'</tr></thead><tbody>'+''.join(body)+'</tbody></table></div>')
    from .management_text import render_schedule_audit
    out.append(render_schedule_audit(schedule_audit))
    return ''.join(out)


def export_management_report(data, path, *, as_of, period='month', lookahead_weeks=6, schedule_audit=None):
    """Standalone month/week review using the same canonical management projection."""
    from pathlib import Path
    from .project_report import REPORT_CSS, _kpi
    from .schema import ENUMS
    review = management_review(data, now=as_of, period=period, lookahead_weeks=lookahead_weeks, schedule_audit=schedule_audit)
    project = next(r for r in data['projects'] if r['id']==review['project_id'])
    title = ('月度管理复核' if period=='month' else '周管理复核')+' · '+project['name']
    h = lambda v: escape(str(v), quote=True)
    local_cutoff = parse_datetime(review["as_of"]).astimezone(ZoneInfo(review["timezone"])).strftime("%Y-%m-%d %H:%M")
    totals = ''.join(_kpi(label+' · 期间完成', len(review['counts'][table]['completed_in_period']), tone='good' if review['counts'][table]['completed_in_period'] else 'neutral', sub='当前未结 '+str(review['counts'][table]['open'])+' 项')
                     for table,label in (('wbs','项目交付'),('operations','运维与优化')))
    body = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+h(title)+'</title><style>'+REPORT_CSS+'</style></head><body class="standalone">'
            '<header><div class="top"><div><div class="brand-label">pmmaster / MANAGEMENT REVIEW</div><strong>'+h(title)+'</strong><small>统计期间 '+h(review['period_start'])+' — '+h(review['period_end'])+'</small></div>'
            '<div class="header-meta"><small>数据截至 <b>'+h(local_cutoff)+'</b> · '+h(review['timezone'])+'</small><small>项目阶段 '+h(ENUMS['lifecycle'].get(review['stage'], review['stage']) or '待确认')+' · 当前预计完成 '+h(project.get('forecast_finish') or '待确认')+'</small></div></div></header>'
            '<main><section class="page active"><p class="eyebrow">期间读数</p><div class="kpi-row">'+totals+'</div><p class="note">完成条数不代表工作量完成百分比。</p>'
            +render_management_section(data,as_of=as_of,period=period,lookahead_weeks=lookahead_weeks,schedule_audit=schedule_audit)
            +'</section></main></body></html>')
    out = Path(path)
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(body,encoding='utf-8')
    return {'ok':True,'path':str(out),'period':period,'as_of':as_of,'project_id':project['id']}
