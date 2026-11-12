"""Validation for v7 business tables; called inside the Store transaction."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from .schema import STATE_TRANSITIONS


def validate_foundation(data, idx, previous, now, add, req, ordered, day_index):
    from .store import parse_datetime
    for table in ('projects','wbs','operations','daily_confirmations','leave_plans','resource_weeks','weekly_commitments'):
        for row in data.get(table, []):
            project=idx['projects'].get(row.get('project_id'), row if table=='projects' else {})
            calendar=idx['calendars'].get(project.get('calendar_id'), {})
            today=now.astimezone(ZoneInfo(calendar.get('timezone','UTC'))).date().isoformat()
            for field in ('confirmed_at','completed_at','progress_confirmed_at','promoted_at'):
                if row.get(field) and parse_datetime(row[field])>now:
                    add(table,row,field,'future_actual','确认/完成时间不能在未来')
            if table in ('wbs','operations') and row.get('progress_confirmed_at'):
                req(table,row,'progress_confirmed_by_id','progress_note')
            if table=='projects':
                ordered(table,row,'reminder_start','reminder_end')
            elif table=='operations':
                req(table,row,'project_id')
                state=row['status']
                if row.get('priority')=='P0': req(table,row,'priority_reason')
                if row.get('severity') and row.get('work_type') not in ('bug','incident'): add(table,row,'severity','severity_type','严重度仅用于缺陷/故障')
                if row.get('delay_risk')=='at_risk': req(table,row,'delay_risk_reason')
                if row.get('remediation_expected'): req(table,row,'remediation_action')
                if row.get('scope')=='project': add(table,row,'scope','scope_mismatch','正式交付请转入项目WBS')
                if state not in ('intake','cancelled'): req(table,row,'owner_id','forecast_finish','acceptance_criteria')
                if state in ('in_progress','in_review','done'): req(table,row,'actual_start')
                if state=='blocked': req(table,row,'blocker_reason','next_action','next_due_at','coordinator_id')
                if state=='done': req(table,row,'actual_finish','completion_evidence')
                elif row.get('actual_finish'): add(table,row,'actual_finish','incomplete_actual_finish','未确认完成不能填实际完成')
                if state=='cancelled': req(table,row,'cancel_reason')
                for a,b in (('baseline_start','baseline_finish'),('forecast_start','forecast_finish'),('actual_start','actual_finish')): ordered(table,row,a,b)
                for field in ('actual_start','actual_finish','remediation_actual'):
                    if row.get(field) and row[field]>today: add(table,row,field,'future_actual','实际日期不能在未来')
                if row.get('remediation_actual'): req(table,row,'remediation_action','remediation_expected')
                old=previous.get(table,{}).get(row['id'])
                if old and old['status']!=state:
                    if state not in STATE_TRANSITIONS[old['status']]: add(table,row,'status','state_transition','不允许跳过执行确认状态')
                    if old['status'] in ('done','cancelled'): req(table,row,'reopen_reason')
                if old and old.get('promoted_wbs_id') and any(row.get(k)!=v for k,v in old.items() if k!='version'):
                    add(table,row,'promoted_wbs_id','promoted_readonly','已转入项目，维护目标任务；原事项只保留来源')
                if row.get('promoted_wbs_id'):
                    req(table,row,'promoted_at','promotion_decision_id')
                    if state!='cancelled': add(table,row,'status','promotion','已转入项目的旧事项须停止独立执行')
                for target_field,target_table in (('related_wbs_id','wbs'),('promoted_wbs_id','wbs'),('meeting_id','meetings'),('decision_id','registers')):
                    target=idx[target_table].get(row.get(target_field))
                    if target and target.get('project_id')!=row.get('project_id'): add(table,row,target_field,'project_mismatch','关联对象须属于同一项目')
                if old and any(old.get(f) is not None and old.get(f)!=row.get(f) for f in ('baseline_start','baseline_finish')):
                    decision=idx['registers'].get(row.get('baseline_change_id'),{})
                    if decision.get('kind')!='change' or decision.get('status')!='approved': add(table,row,'baseline_change_id','baseline_change','基线修改需要已批准变更')
            elif table=='daily_confirmations':
                if (project.get('calendar_id'),row['date']) not in day_index: add(table,row,'date','calendar_missing','项目日期未生成')
                if row['response_status']!='pending': req(table,row,'confirmed_at','confirmed_by_id','source')
                if row.get('confirmed_at') and row['date']>today: add(table,row,'date','future_confirmation','未来日期只能作为安排计划，不能提前签到确认')
                if row.get('future_leave_change')=='changed' and not (row.get('future_leave_note') or row.get('leave_plan_id')):
                    req(table,row,'future_leave_note')
                leave=idx['leave_plans'].get(row.get('leave_plan_id'))
                if leave and (leave.get('project_id')!=row['project_id'] or leave.get('person_id')!=row['person_id']):
                    add(table,row,'leave_plan_id','leave_mismatch','关联休假必须属于本项目同一成员')
                total=row.get('available_hours'); parts=[row.get('project_hours'),row.get('operations_hours')]
                if total is not None and sum(x for x in parts if x is not None)>total:
                    add(table,row,'available_hours','overallocated','项目和运维安排超过净可用时间')
                if total is not None and total>12: add(table,row,'available_hours','slot_capacity','半日可用小时不能超过12')
            elif table=='leave_plans':
                ordered(table,row,'start_date','end_date')
                if (row['start_date'],row['start_period'])>(row['end_date'],row['end_period']): add(table,row,'end_period','date_order','休假时段顺序错误')
                if row['status']=='confirmed': req(table,row,'confirmed_at','confirmed_by_id','source')
            elif table=='resource_weeks':
                if date.fromisoformat(row['week_start']).weekday()!=0: add(table,row,'week_start','week_start','周起始须为周一')
                if row.get('confirmed_at'): req(table,row,'confirmed_by_id','net_available_hours')
            elif table=='weekly_commitments':
                if bool(row.get('work_item_id'))==bool(row.get('operation_id')): add(table,row,'work_item_id','exclusive_reference','项目任务或运维事项必须且只能选一项')
                start=date.fromisoformat(row['week_start'])
                if start.weekday()!=0: add(table,row,'week_start','week_start','周起始须为周一')
                if not start<=date.fromisoformat(row['due_date'])<=start+timedelta(days=6): add(table,row,'due_date','week_range','本周成果期限须在本周内')
                if row['status'] in ('confirmed','done'): req(table,row,'confirmed_at','confirmed_by_id')
                if row['status']=='done': req(table,row,'completed_at','result_evidence')
                for f,t in (('work_item_id','wbs'),('operation_id','operations')):
                    item=idx[t].get(row.get(f))
                    if item and item.get('project_id')!=row['project_id']: add(table,row,f,'project_mismatch','承诺与工作所属项目不一致')
    for row in data.get('meetings',[]):
        if row.get('ends_at') and parse_datetime(row['ends_at'])<=parse_datetime(row['scheduled_at']): add('meetings',row,'ends_at','date_order','会议结束须晚于开始')
    for row in data.get('calendar_years',[]):
        if row['status']=='complete': req('calendar_years',row,'source_url','verified_at','exception_hash')
        if row.get('verified_at') and parse_datetime(row['verified_at'])>now: add('calendar_years',row,'verified_at','future_actual','核验时间不能在未来')
    seen=set()
    for row in data.get('calendar_overrides',[]):
        if row.get('confirmed_at') and parse_datetime(row['confirmed_at'])>now: add('calendar_overrides',row,'confirmed_at','future_actual','确认时间不能在未来')
        if row['status']=='confirmed':
            req('calendar_overrides',row,'source_url','confirmed_at','confirmed_by_id')
            key=(row['calendar_id'],row['date'],row.get('person_id'))
            if key in seen: add('calendar_overrides',row,'date','duplicate_override','同一日同一适用对象存在多个已确认例外')
            seen.add(key)
