"""Explain schedule edits from evidence; never infer an editor's intent.

Input history accepts Store.history() events or normalized
{table, record_id, before, after, actor, recorded_at, approval} dictionaries.
A recorded approval matches only an exact table/record/field/before/after change,
with approved_by, approved_at and evidence. A reused baseline_change_id alone
cannot authorize every subsequent edit. Actor labels are not authenticated IDs.
"""
from datetime import date, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo
import json

TABLE_FIELDS = {
    'projects': ('baseline_start','baseline_finish','forecast_finish'),
    'wbs': ('baseline_start','baseline_finish','forecast_start','forecast_finish','actual_start','actual_finish'),
    'operations': ('baseline_start','baseline_finish','forecast_start','forecast_finish','actual_start','actual_finish'),
    'weekly_commitments': ('due_date','completed_at'),
}
ACTUAL_FIELDS = {'actual_start','actual_finish','completed_at'}
BASELINE_FIELDS = {'baseline_start','baseline_finish','due_date'}


def _instant(value):
    if isinstance(value,datetime): result=value
    elif isinstance(value,str): result=datetime.fromisoformat(value.replace('Z','+00:00'))
    else: raise ValueError('审计时点必须是带时区日期时间')
    if result.tzinfo is None: raise ValueError('审计时点必须带时区')
    return result


def _day(value, zone):
    if value in (None,''): return None
    try:
        if isinstance(value,date) and not isinstance(value,datetime): return value
        if isinstance(value,str) and len(value)==10: return date.fromisoformat(value)
        return _instant(value).astimezone(zone).date()
    except (ValueError,TypeError): return None


def _row(value):
    if isinstance(value,str): value=json.loads(value)
    if value is not None and not isinstance(value,dict): raise ValueError('前后记录须为对象')
    return value


def _timezone(data, table, record):
    project_id=record.get('id') if table=='projects' else record.get('project_id')
    project=next((p for p in data.get('projects',[]) if p['id']==project_id),{})
    calendar=next((c for c in data.get('calendars',[]) if c['id']==project.get('calendar_id')), {})
    name=calendar.get('timezone')
    if not name: return None
    try: return ZoneInfo(name)
    except (ValueError,KeyError): return None


def _comparison(record, table, cutoff, zone):
    end_field='due_date' if table=='weekly_commitments' else 'baseline_finish' if record.get('baseline_finish') else 'forecast_finish'
    end=_day(record.get(end_field),zone)
    actual=_day(record.get('completed_at' if table=='weekly_commitments' else 'actual_finish'),zone)
    start_field='baseline_start' if record.get('baseline_start') else 'forecast_start'
    start=_day(record.get(start_field),zone)
    actual_start=_day(record.get('actual_start'),zone)
    finish_days=max(0,((actual or cutoff)-end).days) if end else None
    # No actual start: late-start only when the item has not been cancelled.
    start_days=max(0,((actual_start or cutoff)-start).days) if start and record.get('status')!='cancelled' else None
    return {'status':record.get('status'),'active':record.get('status')!='cancelled','late_days':finish_days,'late_start_days':start_days,'finish_basis':end_field if end else None,
            'start_basis':start_field if start else None,'planned_finish':end.isoformat() if end else None,
            'planned_start':start.isoformat() if start else None,'actual_finish':actual.isoformat() if actual else None,
            'actual_start':actual_start.isoformat() if actual_start else None}


def _approval(event, changes, table, rid, before, after, data, policy, instant):
    candidates=[]
    trusted_sources=set(policy.get('trusted_approval_sources',[]))
    if isinstance(event.get('approval'),dict) and event.get('source') in trusted_sources:
        candidates.append(event['approval'])
    candidates.extend(a for a in policy.get('approved_changes',[]) if isinstance(a,dict))
    event_time=event.get('recorded_at')
    timing_unverified=None
    decision_makers=set(policy.get('decision_makers',[]))
    for candidate in candidates:
        if candidate.get('approved_by') not in decision_makers: continue
        if candidate.get('table')!=table or candidate.get('record_id')!=rid: continue
        supplied=candidate.get('changes',{})
        if not isinstance(supplied,dict): continue
        if not all(isinstance(supplied.get(c['field']),dict) and supplied[c['field']].get('before')==c['before'] and supplied[c['field']].get('after')==c['after'] for c in changes): continue
        if not all(candidate.get(k) for k in ('approved_by','approved_at','evidence')): continue
        try:
            approved=_instant(candidate['approved_at'])
            if approved>instant: continue
            if event_time:
                if approved>_instant(event_time): continue
            else:
                # Polling sees an interval, not an edit timestamp. Approval
                # before the previous capture is provably prior authorization.
                prior=event.get('previous_observed_at')
                if not prior or approved>_instant(prior):
                    timing_unverified={'status':'approval_timing_unverified','approved_by':candidate['approved_by'],
                        'approved_at':candidate['approved_at'],'evidence':candidate['evidence'],
                        'note':'批准与实际修改的先后未知；不能把发现时间当修改时间，可由有权人员复核处理。'}
                    continue
        except ValueError: continue
        return {'status':'matched_recorded_approval','kind':candidate.get('kind','replan'),
                'approved_by':candidate['approved_by'],'approved_at':candidate['approved_at'],
                'evidence':candidate['evidence'],'decision_id':candidate.get('decision_id'),
                'note':'精确对应本次字段前后值的已记录批准；身份仍以来源系统为准'}
    reviews=[]
    if isinstance(event.get('review'),dict) and event.get('source') in trusted_sources:
        reviews.append(event['review'])
    reviews.extend(r for r in policy.get('reviewed_changes',[]) if isinstance(r,dict))
    for review in reviews:
        if review.get('reviewed_by') not in decision_makers or review.get('kind') not in ('correction','replan'): continue
        if review.get('table')!=table or review.get('record_id')!=rid: continue
        supplied=review.get('changes',{})
        if not isinstance(supplied,dict) or not all(isinstance(supplied.get(c['field']),dict) and supplied[c['field']].get('before')==c['before'] and supplied[c['field']].get('after')==c['after'] for c in changes): continue
        if not review.get('evidence') or not review.get('reviewed_at'): continue
        try:
            reviewed=_instant(review['reviewed_at'])
            known_at=event_time or event.get('observed_at')
            if not known_at or reviewed<_instant(known_at) or reviewed>instant: continue
        except ValueError: continue
        return {'status':'matched_recorded_review','kind':review['kind'],'reviewed_by':review['reviewed_by'],
            'reviewed_at':review['reviewed_at'],'evidence':review['evidence'],
            'decision_id':review.get('decision_id'),'prior_authorization_proven':False,
            'note':'本次变化已按精确前后值事后复核；不追认为修改前已获授权，原始偏差与审计记录继续保留。'}
    if timing_unverified: return timing_unverified
    ref=after.get('baseline_change_id') or after.get('decision_id')
    register=next((r for r in data.get('registers',[]) if r['id']==ref),None)
    if register:
        return {'status':'reference_only','decision_id':ref,'recorded_status':register.get('status'),
                'evidence':register.get('decision') or register.get('proposal'),
                'note':'关联决定未精确证明本次字段前后值及生效顺序，需核对，不能用旧批准覆盖新修改'}
    return {'status':'not_found','decision_id':ref,'note':'尚无匹配本次修改的批准或纠错依据'}


def audit_schedule_changes(data, *, history=None, prior_data=None, now, policy=None):
    """Return change evidence, comparable deviations and first-observed commitments.

    Recorded approvals require policy.decision_makers membership. Event approval
    objects additionally require an explicitly trusted source configured in
    policy.trusted_approval_sources; policy.approved_changes must come from trusted
    deployment/decision configuration, never task text. Absent configuration does
    not authorize a claimed approver.

    No writes. `history=None` differs from an explicitly supplied empty history.
    Snapshot diffs cannot determine who edited a field, or when/how many edits
    occurred between captures. Natural-day metrics use the project timezone.
    """
    instant=_instant(now); policy=policy or {}
    coverage={'mode':'history' if history is not None else 'snapshot' if prior_data is not None else 'unknown',
              'complete':False,'limitations':['仅覆盖所提供的事件或快照；不能据此判断修改人的主观意图。','操作者文本是来源记录标签，不等于经过认证的身份。'],
              'events_considered':0,'invalid_events':0,'future_events_ignored':0,'missing_timezones':0}
    events=[]
    if history is not None:
        for position,event in enumerate(history):
            try:
                if not isinstance(event,dict): raise ValueError('事件须为对象')
                table=event.get('table') or event.get('entity')
                if table not in TABLE_FIELDS: continue
                recorded=event.get('recorded_at') or event.get('observed_at')
                if recorded and _instant(recorded)>instant:
                    coverage['future_events_ignored']+=1;continue
                old=_row(event.get('before',event.get('before_json')))
                new=_row(event.get('after',event.get('after_json')))
                rid=event.get('record_id') or (new or old or {}).get('id')
                if not rid: raise ValueError('缺少目标记录')
                events.append(dict(event,table=table,record_id=rid,before=old,after=new,_position=position))
            except (ValueError,TypeError,json.JSONDecodeError): coverage['invalid_events']+=1
        events.sort(key=lambda e:(_instant(e.get('recorded_at') or e['observed_at']).timestamp() if e.get('recorded_at') or e.get('observed_at') else float('-inf'),e['_position']))
        observed=[e for e in events if e.get('source_kind')=='observation']
        if observed:
            coverage['mode']='observation' if len(observed)==len(events) else 'mixed_history'
            coverage['limitations'].append('观察差异仅确定两次读取之间发生变化；observed_at是发现时点，不是实际修改时点。')
            if len(observed)==len(events) and not any(e.get('before') for e in observed): coverage['mode']='unknown'
        if not events: coverage['limitations'].append('没有可比较的已提供日期事件；不代表外部平台从未改过日期。')
    elif prior_data is not None:
        previous=prior_data.get('data',prior_data)
        coverage['limitations'].append('快照对比不能归因操作者、具体修改时间或中间反复修改。')
        for table in TABLE_FIELDS:
            old_index={r['id']:r for r in previous.get(table,[])}
            for row in data.get(table,[]):
                old=old_index.get(row['id'])
                if old: events.append({'table':table,'record_id':row['id'],'before':old,'after':row,'recorded_at':None,'actor':None})
    else:
        coverage['limitations'].append('只有当前状态，无法识别日期是否被修改；先启用事件留痕或保存后续快照。')
    signals=[]; originals={}; seen=set()
    for event in events:
        table=event['table'];rid=event['record_id'];old=event['before'];new=event['after']
        zone=_timezone(data,table,new or old or {})
        if zone is None:
            coverage['missing_timezones']+=1;continue
        cutoff=instant.astimezone(zone).date();coverage['events_considered']+=1
        original=originals.setdefault((table,rid),{'table':table,'record_id':rid,'title':(new or old or {}).get('title') or (new or old or {}).get('name'), 'basis':'首次在提供的历史中观察到；不保证覆盖立项以来全部历史','first_observed_at':event.get('recorded_at') or event.get('observed_at')})
        for row in (old,new):
            if not row: continue
            fields=('due_date',) if table=='weekly_commitments' and row.get('confirmed_at') else () if table=='weekly_commitments' else ('baseline_start','baseline_finish')
            for field in fields:
                if row.get(field) is not None and 'original_'+field not in original: original['original_'+field]=row[field]
            for field in ACTUAL_FIELDS:
                if row.get(field) is not None and 'first_reported_'+field not in original: original['first_reported_'+field]=row[field]
        if not old or not new: continue  # Creation/first recorded actual is not an edit.
        changes=[{'field':f,'before':old.get(f),'after':new.get(f)} for f in TABLE_FIELDS[table] if old.get(f)!=new.get(f)]
        if not changes: continue
        before=_comparison(old,table,cutoff,zone);after=_comparison(new,table,cutoff,zone)
        effects=[]
        for change in changes:
            field=change['field'];a=_day(change['before'],zone);b=_day(change['after'],zone)
            if change['before'] is not None and change['after'] is None: effects.append('date_evidence_removed')
            elif change['after'] is not None and b is None: effects.append('invalid_date_value')
            elif a and b and b<a and field in ACTUAL_FIELDS: effects.append('actual_date_moved_earlier')
            elif a and b and b>a and field not in ACTUAL_FIELDS: effects.append('planned_date_postponed')
        observed_reduction=any(before[k] is not None and after[k] is not None and after[k]<before[k] for k in ('late_days','late_start_days'))
        first_actual_review=[]
        for change in changes:
            field=change['field']
            if field not in ACTUAL_FIELDS or change['before'] is not None or not change['after']: continue
            unfinished=old.get('status') not in (None,'done','cancelled')
            unstarted=old.get('status') in ('ready','intake')
            if not (unstarted if field=='actual_start' else unfinished): continue
            claimed=_day(change['after'],zone)
            if not claimed: continue
            for time_field,proof in (('progress_confirmed_at','confirmed_progress'),('previous_observed_at','observed_status')):
                timestamp=old.get(time_field) if time_field=='progress_confirmed_at' else event.get(time_field)
                if not timestamp: continue
                try:
                    known=_instant(timestamp)
                    contradiction=known>_instant(change['after']) if field=='completed_at' else known.astimezone(zone).date()>claimed
                    if not contradiction or known>instant: continue
                except ValueError: continue
                first_actual_review.append({'field':field,'claimed':change['after'],'prior_status':old.get('status'),
                    'evidence_kind':proof,'evidence_at':timestamp,
                    'note':'实际日期首次补录与较晚的进展确认需核对；可能是补录或纠错，不据此判断主观意图。' if proof=='confirmed_progress' else '较晚观察仍显示未完成/未开始，可能是状态未及时维护；这是较弱的观察证据。'})
                effects.append('first_actual_conflicts_'+proof)
                break
        edited_only=dict(old)
        for change in changes:
            if change['before'] is not None: edited_only[change['field']]=change['after']
        edited_comparison=_comparison(edited_only,table,cutoff,zone)
        reduced=any(before[k] is not None and edited_comparison[k] is not None and edited_comparison[k]<before[k] for k in ('late_days','late_start_days'))
        # First-entry actual values can legitimately report work completed earlier;
        # they are retained for review but do not by themselves imply concealment.
        edited_existing=any(c['before'] is not None for c in changes)
        governed=table!='weekly_commitments' or bool(old.get('confirmed_at'))
        reduced=reduced and edited_existing and governed
        if reduced: effects.append('reported_deviation_reduced')
        unexplained_actual=False
        for c in changes:
            a,b=_day(c['before'],zone),_day(c['after'],zone)
            if c['field'] not in ACTUAL_FIELDS or not a or not b or b>=a: continue
            metric='late_start_days' if c['field']=='actual_start' else 'late_days'
            if before[metric] is None or after[metric] is None or after[metric]<before[metric]: unexplained_actual=True
        confirmed_contradiction=any(r['evidence_kind']=='confirmed_progress' for r in first_actual_review)
        if governed and (any(e in effects for e in ('reported_deviation_reduced','date_evidence_removed')) or confirmed_contradiction and observed_reduction): kind='schedule_change_review';severity='high'
        elif first_actual_review: kind='schedule_change_review';severity='medium'
        elif 'invalid_date_value' in effects or (governed and (unexplained_actual or ('planned_date_postponed' in effects and any(c['field'] in BASELINE_FIELDS for c in changes)))):
            kind='schedule_change_review';severity='medium'
        else: kind='schedule_change_recorded';severity='info'
        original_classification={'kind':kind,'severity':severity}
        approval=_approval(event,changes,table,rid,old,new,data,policy,instant)
        if approval['status']=='matched_recorded_approval':
            kind='approved_correction' if approval['kind']=='correction' else 'approved_replan';severity='info'
        elif approval['status']=='matched_recorded_review':
            kind='reviewed_correction' if approval['kind']=='correction' else 'accepted_replan';severity='info'
        raw_key=[table,rid,event.get('event_id') or event.get('source_event_id'),event.get('recorded_at'),event.get('observed_at'),changes]
        key='schedule-'+sha256(json.dumps(raw_key,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:24]
        if key in seen: continue
        seen.add(key)
        comparison={'cutoff':cutoff.isoformat(),'timezone':str(zone),'unit':'calendar_days','before':before,'after':after,
                    'before_late_days':before['late_days'],'after_late_days':after['late_days'],
                    'before_late_start_days':before['late_start_days'],'after_late_start_days':after['late_start_days'],
                    'deviation_reduced':reduced,'observed_deviation_reduced':observed_reduction,'existing_date_edits_only':edited_comparison}
        signals.append({'key':key,'table':table,'record_id':rid,'title':new.get('title') or new.get('name') or rid,
                        'kind':kind,'severity':severity,'original_classification':original_classification,'changed_fields':changes,'effects':sorted(set(effects)),
                        'actor':event.get('actor') or None,'actor_verified':False,'recorded_at':event.get('recorded_at'),
                        'evidence':{'event_id':event.get('event_id'),'source':event.get('source') or event.get('source_url'),
                                    'mode':coverage['mode'],'source_kind':event.get('source_kind'),'observed_at':event.get('observed_at'),'previous_observed_at':event.get('previous_observed_at'),'before_version':old.get('version'),'after_version':new.get('version')},
                        'approval':approval,'comparison':comparison,'first_actual_review':first_actual_review,
                        'next_action':'核对修改原因、真实完成证据及本次批准范围；保留原日期和历史偏差。' if severity!='info' else '保留变更、批准或复核记录；原承诺及历史偏差继续对照。'})
    for (table,rid),original in originals.items():
        current=next((r for r in data.get(table,[]) if r['id']==rid),None)
        original['current_record_present']=current is not None
        if not current: continue
        zone=_timezone(data,table,current)
        if zone is None: continue
        baseline=dict(current)
        for field in BASELINE_FIELDS:
            if 'original_'+field in original:
                original['current_'+field]=current.get(field);baseline[field]=original['original_'+field]
        original['original_commitment_comparison']=_comparison(baseline,table,instant.astimezone(zone).date(),zone)
        original['current_comparison']=_comparison(current,table,instant.astimezone(zone).date(),zone)
        first_reported=dict(baseline)
        for field in ACTUAL_FIELDS:
            if 'first_reported_'+field in original: first_reported[field]=original['first_reported_'+field]
        original['first_reported_comparison']=_comparison(first_reported,table,instant.astimezone(zone).date(),zone)
    signals.sort(key=lambda s:({'high':0,'medium':1,'info':2}[s['severity']],s['key']))
    return {'coverage':coverage,'signals':signals,'original_commitments':[r for r in originals.values() if any(k.startswith('original_') and k!='original_commitment_comparison' for k in r)],
            'alert_count':sum(s['severity']!='info' for s in signals),'as_of':instant.isoformat()}
