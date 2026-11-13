"""One project tick: fresh planning plus scoped, deduplicated reminder dispatch."""
from datetime import datetime, time
from zoneinfo import ZoneInfo
import json
from pathlib import Path

from .actions import Ledger, digest
from .feishu import FeishuError
from .store import parse_datetime


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not config.get('project_id') or not config.get('timezone'):
        raise ValueError('Project and project timezone are required')
    ZoneInfo(config['timezone'])
    if not isinstance(config.get('enabled', False), bool):
        raise ValueError('enabled must be a JSON boolean')
    attempts = config.get('max_attempts', 3)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise ValueError('max_attempts must be an integer between 1 and 10')
    for key in ('daily_time', 'daily_deadline', 'collection_time', 'plan_cutoff', 'result_cutoff', 'quiet_after', 'workflow_time', 'response_cutoff', 'escalation_time'):
        if config.get(key):
            time.fromisoformat(config[key])
    for key in ('response_cutoff', 'escalation_time'):
        if key in config and (not isinstance(config[key], str) or len(config[key]) != 5
                              or time.fromisoformat(config[key]).strftime('%H:%M') != config[key]):
            raise ValueError(key + ' must be a project-local HH:MM time')
    for value in config.get('inspection_times', ['10:30', '16:00']):
        time.fromisoformat(value)
    from .group_actions import message_limits
    message_limits(config)
    grace = config.get('min_response_minutes', 30)
    if isinstance(grace, bool) or not isinstance(grace, int) or not 0 <= grace <= 1440:
        raise ValueError('min_response_minutes must be an integer between 0 and 1440')
    sources = config.get('reply_evidence_sources', [])
    if not isinstance(sources, list) or any(not isinstance(s, str) or not s.strip() for s in sources):
        raise ValueError('reply_evidence_sources must be a list of trusted host identifiers')
    if any(k in config for k in ('token', 'access_token', 'app_secret')):
        raise ValueError('Store secrets in the host credential store/environment, not project JSON')
    return config


def plan_tick(data, config, *, now):
    from .attendance import plan_daily_confirmations
    from .operating import plan_followups
    from .workflow import plan_workflow_followups
    instant = parse_datetime(now) if isinstance(now, str) else now
    if instant.tzinfo is None:
        raise ValueError('Tick time requires timezone')
    local = instant.astimezone(ZoneInfo(config['timezone']))
    project = next((p for p in data.get('projects', []) if p['id'] == config['project_id']), None)
    if not project:
        raise ValueError('Configured project not found')
    calendar = next((c for c in data.get('calendars', []) if c['id'] == project['calendar_id']), None)
    if not calendar or calendar['timezone'] != config['timezone']:
        raise ValueError('Runtime timezone must match project calendar')
    attendance = plan_daily_confirmations(data, config['project_id'], instant)
    people = {r['id']: r for r in data.get('people', [])}
    items = []
    if local.strftime('%H:%M') >= config.get('quiet_after', '18:00'):
        return {'project_id': config['project_id'], 'date': local.date().isoformat(), 'attendance': attendance,
                'recipients': {}, 'workflow': None, 'pending': []}
    daily_at = config.get('daily_time', '09:00')
    if local.strftime('%H:%M') >= daily_at:
        for r in attendance['eligible']:
            items.append({'key': 'daily:' + local.date().isoformat() + ':' + r['person_id'],
                          'kind': 'daily_confirmation_followup' if local.strftime('%H:%M') >= config.get('daily_deadline', '10:30') else 'daily_confirmation', 'recipient_id': r['person_id'],
                          'reason': '请补齐今日' + '、'.join({'AM': '上午', 'PM': '下午'}[p] for p in r['periods']) + '尚未确认的安排信息（见待维护字段）。',
                          'required_fields': r.get('required_fields', []),
                          'deadline': config.get('daily_deadline', '10:30'), 'periods': r['periods']})
    # Ordinary task reminders follow project calendar, not literal Monday-Friday.
    day = next((d for d in data.get('calendar_days', []) if d['calendar_id'] == project['calendar_id'] and d['date'] == local.date().isoformat()), None)
    from .project_calendar import workday_status
    working = workday_status(data, config['project_id'], local.date().isoformat())
    if working['verified'] and working['is_workday'] and project.get('stage') not in ('closed', 'paused'):
        for item in plan_followups(data, now=instant, config=config):
            collection = item['kind'] in ('monday_plan', 'friday_result')
            earliest = config.get('collection_time', '09:00') if collection else min(config.get('inspection_times') or ['10:30', '16:00'])
            if local.strftime('%H:%M') < earliest:
                continue
            if collection:
                cutoff = config.get('plan_cutoff' if item['kind'] == 'monday_plan' else 'result_cutoff', '10:30')
                if cutoff and local.strftime('%H:%M') >= cutoff:
                    item = dict(item, kind=item['kind'] + '_followup')
            items.append(item)
    workflow = plan_workflow_followups(data, now=instant, config=config)
    routine_tasks = {(i['table'], i['record_id']): i for i in workflow['items']
                     if i['kind'] == 'workflow_task'}
    merged = []
    for item in items:
        routine = routine_tasks.get((item.get('table'), item.get('record_id')))
        if routine and item['kind'] == 'stale_progress' and 'progress_confirmed_at' in routine['required_fields']:
            continue
        if routine and item['kind'] == 'monday_plan':
            fields = [f for f in item['required_fields'] if f not in routine['required_fields']]
            if not fields: continue
            item = dict(item, required_fields=fields)
        merged.append(item)
    items = merged
    items.extend(workflow['items'])
    from .group_actions import route_item
    grouped = {}; pending = list(workflow['pending'])
    for item in items:
        item = route_item(data, item, day=local.date().isoformat(), require_identity=config.get('mode') == 'feishu')
        if item.get('routing_status'):
            pending.append(item)
            continue
        grouped.setdefault(item['recipient_id'], []).append(item)
    return {'project_id': config['project_id'], 'date': local.date().isoformat(), 'as_of': instant.isoformat(),
            'attendance': attendance, 'recipients': grouped, 'workflow': workflow['review'],
            'pending': pending}


def dispatch_tick(data_provider, config, ledger, client, *, now, dry_run=True, history_provider=None, observer=None, delivery_clock=None):
    # Provider runs on every tick immediately before sending, so replies/closed tasks stop reminders.
    data = data_provider()
    from .schedule_audit import audit_schedule_changes
    from .schedule_history import source_identity
    audit_policy = dict(config.get('schedule_audit', {}))
    audit_policy.setdefault('decision_makers', config.get('policy', {}).get('decision_makers', []))
    if history_provider:
        audit = audit_schedule_changes(data, history=history_provider(), now=now, policy=audit_policy)
    elif observer:
        audit = observer.observe(data, source_id=source_identity(config), project_id=config['project_id'], now=now, policy=audit_policy)
    else:
        audit = audit_schedule_changes(data, now=now, policy=audit_policy)
    plan = plan_tick(data, config, now=now)
    people = {p['id']: p for p in data.get('people', [])}
    results = []
    from .group_actions import project_group_actions, action_batches
    actions = project_group_actions(data, plan, config, now=now)
    plan['group_actions'] = actions
    for item in actions['pending']:
        results.append({'person_id': item.get('responsible_id'), 'status': 'pending_route', 'item': item})
    for person_id, items in actions['recipients'].items():
        person = people[person_id]
        uid = person.get('feishu_user_id')
        if not dry_run and (not uid or person.get('feishu_id_type', 'open_id') != 'open_id'):
            results.append({'person_id': person_id, 'status': 'pending_identity'})
            continue
        pending = []
        for item in items:
            key = ':'.join(('reminder', config['project_id'], plan['date'], person_id, item['id']))
            row = next((r for r in data.get(item['source']['table'], []) if r['id'] == item['source']['id']), {})
            if row.get('priority') == 'P0' or row.get('impact') == 'critical':
                key += ':' + digest({k: row.get(k) for k in ('status', 'blocker_reason', 'forecast_finish', 'deadline_at', 'impact')})
            previous = ledger.get(key)
            if previous and previous['status'] in ('succeeded', 'running', 'unknown'):
                continue
            pending.append(dict(item, occurrence_key=key))
        if not pending:
            results.append({'person_id': person_id, 'status': 'already_processed'})
            continue
        for batch in action_batches(pending, config):
            if dry_run:
                results.append({'person_id': person_id, 'status': 'preview', 'text': batch['text'], 'items': batch['actions']})
                continue
            if not config.get('enabled') or not config.get('chat_id'):
                results.append({'person_id': person_id, 'status': 'disabled'})
                break
            if person_id not in config.get('allowed_recipients', []):
                results.append({'person_id': person_id, 'status': 'outside_recipient_scope'})
                break
            claimed = []
            for item in batch['actions']:
                key = item['occurrence_key']
                fingerprint = digest({'key': key, 'chat_id': config['chat_id']})
                state = ledger.claim(key, fingerprint, max_attempts=config.get('max_attempts', 3))
                if state.get('claimed_now'):
                    claimed.append((item, fingerprint))
            if not claimed:
                continue
            # Re-render the exact claimed subset with the same preview renderer.
            # Each chunk has its own UUID and durable outcome; never mark unsent
            # later chunks as successful after a partial delivery.
            actual = action_batches([i for i, _ in claimed], config)[0]
            batch_key = 'reminder-batch:' + digest({'keys': [i['occurrence_key'] for i, _ in claimed],
                                                   'text': actual['text'], 'chat_id': config['chat_id']})
            batch_fingerprint = digest({'key': batch_key, 'text': actual['text']})
            state = ledger.claim(batch_key, batch_fingerprint, max_attempts=config.get('max_attempts', 3))
            detail = {'items': actual['actions'], 'batch_key': batch_key, 'text': actual['text']}
            if state.get('claimed_now'):
                ledger.save(batch_key, batch_fingerprint, 'running', detail)
                for item, fingerprint in claimed:
                    ledger.save(item['occurrence_key'], fingerprint, 'running', {'batch_key': batch_key})
                try:
                    sent = client.mention(config['chat_id'], uid, actual['text'], request_id=state['request_id'])
                    detail['message_id'] = sent.get('message_id')
                    status = 'succeeded' if detail['message_id'] else 'unknown'
                    if status == 'succeeded':
                        from .followup_evidence import record_sent
                        # Live hosts sample after delivery, not before slow reads/batches.
                        # A fixed tick instant remains available for deterministic/offline callers.
                        record_sent(ledger, config, person_id, actual['actions'], detail['message_id'],
                                    now=delivery_clock() if delivery_clock else now)
                except FeishuError as exc:
                    status = 'unknown' if exc.uncertain else 'failed'
                    detail['error'] = str(exc)
                except Exception as exc:
                    # An unexpected transport failure cannot establish non-delivery.
                    status = 'unknown'
                    detail['error'] = str(exc)
                ledger.save(batch_key, batch_fingerprint, status, detail)
            else:
                status = state['status']
                detail = state['detail']
            for item, fingerprint in claimed:
                ledger.save(item['occurrence_key'], fingerprint, status, detail)
            results.append({'person_id': person_id, 'status': status, 'detail': detail})
    reviews = dispatch_schedule_reviews(data, audit, config, ledger, client, now=now, dry_run=dry_run)
    from .followup_evidence import dispatch_escalation
    escalation = dispatch_escalation(data, plan, actions, config, ledger, client, now=now, dry_run=dry_run)
    return {'plan': plan, 'deliveries': results, 'schedule_audit': audit, 'pm_reviews': reviews,
            'escalation': escalation}


def dispatch_schedule_reviews(data, audit, config, ledger, client, *, now, dry_run=True):
    """Date edit concerns only reach an explicitly configured private PM channel."""
    from .management_text import schedule_paragraphs
    from .project_calendar import workday_status
    project = next((p for p in data.get('projects', []) if p['id'] == config['project_id']), None)
    if not project:
        return []
    instant = parse_datetime(now) if isinstance(now, str) else now
    local = instant.astimezone(ZoneInfo(config['timezone']))
    workday = workday_status(data, project['id'], local.date().isoformat())
    in_window = (min(config.get('inspection_times') or ['10:30', '16:00']) <= local.strftime('%H:%M') < config.get('quiet_after', '18:00'))
    pm = next((p for p in data.get('people', []) if p['id'] == project.get('pm_id')), {})
    results = []
    for signal in audit.get('signals', []):
        if signal['severity'] == 'info':
            continue
        key = 'schedule-review:' + project['id'] + ':' + signal['key']
        previous = ledger.get(key)
        if previous and previous['status'] in ('succeeded', 'running', 'unknown'):
            results.append({'key': key, 'status': previous['status'], 'duplicate': True})
            continue
        detail = {'signal': signal, 'recipient_id': project.get('pm_id'), 'audience': 'pm_private'}
        if not config.get('pm_private_chat_id') or config.get('pm_private_recipient_id') != project.get('pm_id'):
            results.append(dict(detail, key=key, status='pending_private_channel'))
            continue
        if project.get('pm_id') not in config.get('allowed_recipients', []):
            results.append(dict(detail, key=key, status='outside_recipient_scope'))
            continue
        if not pm.get('feishu_user_id') or pm.get('feishu_id_type', 'open_id') != 'open_id':
            results.append(dict(detail, key=key, status='pending_identity'))
            continue
        if not in_window or not workday.get('verified') or not workday.get('is_workday') or project.get('stage') in ('closed', 'paused'):
            results.append(dict(detail, key=key, status='waiting_review_window'))
            continue
        message = '\n'.join(schedule_paragraphs({'alert_count': 1, 'coverage': {'limitations': []}, 'signals': [signal], 'original_commitments': []}))
        if dry_run:
            results.append(dict(detail, key=key, status='preview', text=message))
            continue
        if not config.get('enabled'):
            results.append(dict(detail, key=key, status='disabled'))
            continue
        fingerprint = digest({'key': key, 'chat_id': config['pm_private_chat_id']})
        state = ledger.claim(key, fingerprint, max_attempts=config.get('max_attempts', 3))
        if not state.get('claimed_now'):
            results.append(dict(detail, key=key, status=state['status']))
            continue
        try:
            sent = client.mention(config['pm_private_chat_id'], pm['feishu_user_id'], message, request_id=state['request_id'])
            detail['message_id'] = sent.get('message_id')
            status = 'succeeded' if detail['message_id'] else 'unknown'
        except FeishuError as exc:
            status = 'unknown' if exc.uncertain else 'failed'
            detail['error'] = str(exc)
        ledger.save(key, fingerprint, status, detail)
        results.append(dict(detail, key=key, status=status))
    return results
