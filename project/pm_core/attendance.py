"""Daily external-member arrangement confirmation, separate from actual attendance."""
from hashlib import sha256
from zoneinfo import ZoneInfo
from .project_calendar import workday_status, project_window
from .resources import confirmation_scope


def leave_periods(data, project_id, person_id, day):
    periods=set()
    for r in data.get('leave_plans',[]):
        if r['project_id']!=project_id or r['person_id']!=person_id or r['status']!='confirmed': continue
        for p in ('AM','PM'):
            if (r['start_date'],r['start_period']) <= (day,p) <= (r['end_date'],r['end_period']): periods.add(p)
    return periods


def plan_daily_confirmations(data, project_id, now, *, skip_confirmed_leave=True):
    from .store import parse_datetime
    if isinstance(now,str): now=parse_datetime(now)
    if now.tzinfo is None: raise ValueError('now必须带时区')
    project=next((r for r in data.get('projects',[]) if r['id']==project_id),None)
    if not project: raise ValueError('项目不存在')
    cal=next(r for r in data['calendars'] if r['id']==project['calendar_id'])
    day=now.astimezone(ZoneInfo(cal['timezone'])).date().isoformat()
    state=workday_status(data,project_id,day)
    result={'date':day,'project_id':project_id,'is_workday':state['is_workday'],'calendar_ready':state['verified'],
            'project_active':project['stage'] not in ('closed','paused'),'eligible':[],'pending':[],'skipped':[]}
    if not result['project_active']: result['skipped'].append({'reason':'project_inactive'}); return result
    if state['reason']=='outside_project_window':
        result['pending'].append({'person_id':None,'reason':'reminder_window_expired_or_not_started'});return result
    if not state['verified']: result['pending'].append({'person_id':None,'reason':state['reason']});return result
    confirmations={(r['person_id'],r['period']):r for r in data.get('daily_confirmations',[]) if r['project_id']==project_id and r['date']==day}
    for person in data.get('people',[]):
        pid=person['id']; reason=None
        scope=confirmation_scope(person)
        if scope=='employee_excluded': reason='employee'
        elif scope=='identity_unknown': result['pending'].append({'person_id':pid,'reason':'identity_unknown'});continue
        elif not person['active']: reason='inactive'
        elif not person.get('starts_on') or not person.get('ends_on'): result['pending'].append({'person_id':pid,'reason':'participation_window_missing'});continue
        elif not person['starts_on']<=day<=person['ends_on']: reason='outside_participation_window'
        personal=workday_status(data,project_id,day,pid)
        if not personal['is_workday']: reason=reason or 'nonworkday'
        if reason: result['skipped'].append({'person_id':pid,'reason':reason});continue
        excluded=leave_periods(data,project_id,pid,day) if skip_confirmed_leave else set()
        def missing_fields(period):
            row=confirmations.get((pid,period),{})
            missing=[field for field in ('work_location','allocation_mode','future_leave_change') if row.get(field) in (None,'unknown')]
            if row.get('available_hours') is None: missing.append('available_hours')
            if row.get('response_status') not in ('confirmed','corrected'): missing.append('response_status')
            return missing
        missing={p:missing_fields(p) for p in ('AM','PM') if p not in excluded}
        needed=[p for p,fields in missing.items() if fields]
        if not needed: result['skipped'].append({'person_id':pid,'reason':'confirmed_or_planned_leave'});continue
        if not person.get('feishu_user_id'):
            result['pending'].append({'person_id':pid,'reason':'mention_identity_missing'});continue
        result['eligible'].append({'person_id':pid,'name':person['name'],'feishu_user_id':person['feishu_user_id'],
            'feishu_id_type':person.get('feishu_id_type') or 'open_id','periods':needed,
            'missing_fields':{p:missing[p] for p in needed},
            'required_fields':list(dict.fromkeys(f for p in needed for f in missing[p]))})
    return result


def confirm_day(store, project_id, person_id, day, *, periods=('AM','PM'), values, actor, now=None):
    from .store import utc_now, parse_datetime
    instant=now or utc_now()
    if isinstance(instant,str): instant=parse_datetime(instant)
    if not periods or len(set(periods))!=len(periods) or set(periods)-{'AM','PM'}: raise ValueError('时段只能为AM/PM且不能重复')
    if len(periods)>1 and any(values.get(k) is not None for k in ('available_hours','project_hours','operations_hours')):
        raise ValueError('数值按半日填写，全天总量不能重复写入两个时段')
    forbidden={'id','project_id','person_id','date','period','version'} & values.keys()
    if forbidden: raise ValueError('values不能覆盖定位字段')
    data=store.data()
    person=next((p for p in data.get('people', []) if p['id']==person_id), None)
    current={(r['date'],r['period']):r for r in data['daily_confirmations'] if r['project_id']==project_id and r['person_id']==person_id}
    if confirmation_scope(person) != 'non_employee' and any((day, period) not in current for period in periods):
        raise ValueError('新增每日人力确认仅面向已知非员工；员工免确认，未知类型先核实。历史记录可单独更正。')
    rows=[]
    for period in periods:
        row=dict(values,project_id=project_id,person_id=person_id,date=day,period=period)
        row.setdefault('response_status','confirmed');row.setdefault('confirmed_at',instant.isoformat())
        row.setdefault('confirmed_by_id',person_id)
        old=current.get((day,period))
        if old: row.update(id=old['id'],version=old['version'])
        else: row['id']='DC-'+sha256(f'{project_id}|{person_id}|{day}|{period}'.encode()).hexdigest()[:20]
        rows.append(row)
    return store.apply_batch({'daily_confirmations':rows},actor=actor,now=instant)
