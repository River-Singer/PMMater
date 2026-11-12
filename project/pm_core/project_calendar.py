"""Verified annual calendars with project windows and approved company overlays."""
from calendar import monthrange
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo
import json
from .calendar import build_calendar, load_calendar_overrides


def project_window(project):
    return (project.get('reminder_start') or project.get('baseline_start'),
            project.get('reminder_end') or project.get('baseline_finish'))


def workday_status(data, project_id, day_iso, person_id=None):
    project=next((r for r in data.get('projects',[]) if r['id']==project_id),None)
    if not project: return {'is_workday':None,'verified':False,'reason':'missing_project'}
    start,end=project_window(project)
    if not start or not end: return {'is_workday':None,'verified':False,'reason':'project_window_missing'}
    if not start<=day_iso<=end: return {'is_workday':False,'verified':True,'reason':'outside_project_window'}
    calid=project['calendar_id']
    annual=next((r for r in data.get('calendar_years',[]) if r['calendar_id']==calid and r['year']==int(day_iso[:4])),{})
    row=next((r for r in data.get('calendar_days',[]) if r['calendar_id']==calid and r['date']==day_iso),None)
    if not row or annual.get('status')!='complete' or not all(annual.get(k) for k in ('source_url','verified_at','exception_hash')):
        return {'is_workday':None,'verified':False,'reason':'calendar_pending'}
    overrides=[r for r in data.get('calendar_overrides',[]) if r['calendar_id']==calid and r['date']==day_iso and r['status']=='confirmed' and r.get('person_id') in (None,person_id)]
    # Personal overrides are evaluated only for that person, never for the group.
    overrides=[r for r in overrides if not r.get('person_id') or person_id]
    overrides.sort(key=lambda r:bool(r.get('person_id')))
    effective=overrides[-1] if overrides else row
    return {'is_workday':bool(effective['is_workday']),'verified':True,'reason':'company_override' if overrides else row['day_type'],'calendar_id':calid}


def ensure_project_calendar(store, project_id, *, actor='calendar-maintainer', now=None):
    from .store import utc_now, parse_datetime
    instant=now or utc_now()
    if isinstance(instant,str): instant=parse_datetime(instant)
    data=store.data(); project=next((r for r in data['projects'] if r['id']==project_id),None)
    if not project: raise ValueError('项目不存在')
    start,end=project_window(project)
    if not start or not end: raise ValueError('先确认项目开始及提醒结束日期')
    cal=next(r for r in data['calendars'] if r['id']==project['calendar_id'])
    today=instant.astimezone(ZoneInfo(cal['timezone'])).date().isoformat()
    # Calendar display covers boundary months; reminders still obey project_window.
    start=min(cal['coverage_start'],start[:8]+'01')
    ed=date.fromisoformat(end); end=max(cal['coverage_end'],ed.replace(day=monthrange(ed.year,ed.month)[1]).isoformat())
    official={}; years=[]; bundled_years=set(); base=Path(__file__).resolve().parent.parent/'data'
    known={r['year']:r for r in data['calendar_years'] if r['calendar_id']==cal['id']}
    for year in range(int(start[:4]),int(end[:4])+1):
        path=base/f'cn_{year}_holidays.csv'
        source_path=base/('calendar_sources.json' if year==2026 else f'calendar_sources_{year}.json')
        prior=known.get(year)
        if path.exists() and source_path.exists():
            bundled_years.add(year)
            source=json.loads(source_path.read_text(encoding='utf-8')); rows=load_calendar_overrides(path)
            if len(rows)!=source['override_rows'] or any(int(r['date'][:4])!=year for r in rows): raise ValueError('年度例外来源与数据不一致')
            digest=sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            annual={'calendar_id':cal['id'],'year':year,'status':'complete','source_url':source['source_url'],'published_on':source['published_on'],'verified_at':source['verified_at'],'exception_hash':digest}
            official.update({r['date']:r for r in rows if start<=r['date']<=end})
        elif prior and prior.get('status')=='complete':
            # User-imported verified years are preserved, not replaced by defaults.
            continue
        else: annual={'calendar_id':cal['id'],'year':year,'status':'pending'}
        if prior: annual.update(id=prior['id'],version=prior['version'])
        else: annual['id']=f'CY-{sha256((cal["id"]+str(year)).encode()).hexdigest()[:16]}'
        years.append(annual)
    generated=build_calendar(start,end,cal['timezone'],list(official.values()))
    existing={r['date']:r for r in data['calendar_days'] if r['calendar_id']==cal['id']}; days=[]
    for row in generated:
        prior=existing.get(row['date'])
        year=int(row['date'][:4])
        custom_complete=year not in bundled_years and known.get(year,{}).get('status')=='complete'
        if custom_complete or (prior and prior['day_type']=='company_override'):
            # Custom complete manifests do not contain the exception set here;
            # leave uncovered dates missing instead of certifying default rows.
            continue
        record=dict(row,calendar_id=cal['id'])
        if prior: record.update(id=prior['id'],version=prior['version'])
        else: record['id']='CD-'+sha256((cal['id']+row['date']).encode()).hexdigest()[:20]
        days.append(record)
    batch={'calendars':[dict(cal,coverage_start=start,coverage_end=end)],'calendar_days':days,'calendar_years':years}
    result=store.apply_batch(batch,actor=actor,now=instant)
    result['pending_years']=[r['year'] for r in years if r['status']=='pending']
    result['project_window']={'start':project_window(project)[0],'end':project_window(project)[1]}
    return result
