"""Durable sent-question facts and controlled-host reply observations.

Import validates evidence structure and scope, not the authenticity of arbitrary
JSON. The configured host must verify Feishu sender identities and pagination.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .actions import digest
from .store import parse_datetime


def instant(value):
    parsed = parse_datetime(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError('Evidence time requires an offset')
    return parsed


def _exists(ledger, table):
    return ledger.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _ensure(ledger):
    ledger.conn.execute('CREATE TABLE IF NOT EXISTS followup_sent (occurrence_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL)')
    ledger.conn.execute('CREATE TABLE IF NOT EXISTS followup_evidence (event_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, chat_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)')
    ledger.conn.execute('CREATE TABLE IF NOT EXISTS followup_digests (project_id TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(project_id,day))')


def _ack_only(value):
    compact = ''.join(c for c in value.casefold() if c.isalnum())
    return not compact or compact in ('收到', '好的', '好', 'ok', 'okay', '收到谢谢', '明白', '了解')


def sent_questions(ledger, project_id, day=None):
    if not _exists(ledger, 'followup_sent'): return []
    sql = 'SELECT payload FROM followup_sent WHERE project_id=?'
    args = [project_id]
    if day is not None: sql += ' AND day=?'; args.append(day)
    return [json.loads(r[0]) for r in ledger.conn.execute(sql + ' ORDER BY occurrence_key', args)]


def record_sent(ledger, config, person_id, actions, message_id, *, now):
    """Only call after successful delivery; immutable facts survive reconcile."""
    if not message_id: raise ValueError('A successful message ID is required')
    sent_at = instant(now)
    day = sent_at.astimezone(ZoneInfo(config['timezone'])).date().isoformat()
    _ensure(ledger)
    with ledger.conn:
        for action in actions:
            row = {'occurrence_key': action['occurrence_key'], 'project_id': config['project_id'],
                   'chat_id': config['chat_id'], 'message_id': message_id, 'recipient_id': person_id,
                   'sent_at': sent_at.isoformat(), 'day': day, 'action': action}
            old = ledger.conn.execute('SELECT payload FROM followup_sent WHERE occurrence_key=?', (row['occurrence_key'],)).fetchone()
            if old and json.loads(old[0]) != row:
                raise ValueError('Sent-question evidence cannot be overwritten')
            ledger.conn.execute('INSERT OR IGNORE INTO followup_sent VALUES(?,?,?,?)',
                                (row['occurrence_key'], config['project_id'], day, json.dumps(row, ensure_ascii=False)))


def import_evidence(ledger, config, payload, *, now, apply=False):
    """Validate the whole envelope before any insertion; repeated events are inert."""
    current = instant(now)
    if not isinstance(payload, dict): raise ValueError('Evidence envelope must be an object')
    source = payload.get('source_id')
    if not isinstance(source, str) or source not in config.get('reply_evidence_sources', []):
        raise ValueError('Unconfigured reply evidence source')
    if payload.get('project_id') != config['project_id'] or not config.get('chat_id') or payload.get('chat_id') != config['chat_id']:
        raise ValueError('Evidence project/chat does not match configured scope')
    receipts = {r['occurrence_key']: r for r in sent_questions(ledger, config['project_id'])}
    entries = []
    for kind, plural in (('reply', 'replies'), ('coverage', 'coverage')):
        values = payload.get(plural, [])
        if not isinstance(values, list): raise ValueError(plural + ' must be an array')
        for value in values:
            if not isinstance(value, dict): raise ValueError('Evidence entries must be objects')
            row = dict(value, source_id=source, project_id=config['project_id'], chat_id=config['chat_id'])
            if kind == 'reply':
                receipt = receipts.get(row.get('occurrence_key'))
                if not receipt or receipt['chat_id'] != config['chat_id']:
                    raise ValueError('Reply must identify a successful scoped question')
                if row.get('prompt_message_id') != receipt['message_id'] or row.get('sender_id') != receipt['recipient_id']:
                    raise ValueError('Reply prompt/sender does not match the addressed question')
                if not isinstance(row.get('message_id'), str) or not row['message_id'].strip() or row['message_id'] == receipt['message_id']:
                    raise ValueError('Reply needs its own message ID')
                at = instant(row.get('at'))
                if not instant(receipt['sent_at']) <= at <= current: raise ValueError('Reply time is before the question or in the future')
                if not isinstance(row.get('quote'), str) or not row['quote'].strip(): raise ValueError('Reply quote required')
                if row.get('kind') not in ('ack', 'answer'): raise ValueError('Reply kind must be ack or answer')
                answers = row.get('answers', {})
                fields = receipt['action'].get('response_fields', ['response'])
                if not isinstance(answers, dict) or set(answers) - set(fields): raise ValueError('Answers must reference sent question IDs')
                if any(not isinstance(v, str) or not v.strip() for v in answers.values()): raise ValueError('Answers require substantive text')
                if row['kind'] == 'ack' and answers: raise ValueError('Acknowledgement cannot claim answers')
                if row['kind'] == 'answer' and (not answers or _ack_only(row['quote']) or any(_ack_only(v) for v in answers.values())):
                    raise ValueError('Acknowledgements must not be classified as substantive answers')
                row['answers'] = answers
                key = digest([source, 'reply', row['message_id'], row['occurrence_key']])
            else:
                if not isinstance(row.get('id'), str) or not row['id'].strip() or not isinstance(row.get('complete'), bool):
                    raise ValueError('Coverage needs an ID and boolean complete flag')
                start, end, captured = (instant(row.get(f)) for f in ('from', 'through', 'captured_at'))
                if not start <= end <= captured <= current: raise ValueError('Coverage times must be ordered and nonfuture')
                if not isinstance(row.get('evidence'), str) or not row['evidence'].strip(): raise ValueError('Coverage source evidence required')
                key = digest([source, 'coverage', row['id']])
            prior = None
            if _exists(ledger, 'followup_evidence'):
                prior = ledger.conn.execute('SELECT payload FROM followup_evidence WHERE event_key=?', (key,)).fetchone()
            if prior and json.loads(prior[0]) != row: raise ValueError('Evidence event conflicts with previously imported facts')
            for seen_key, _, seen in entries:
                if seen_key == key and seen != row: raise ValueError('Conflicting evidence in envelope')
            entries.append((key, kind, row))
    if apply:
        _ensure(ledger)
        with ledger.conn:
            for key, kind, row in entries:
                ledger.conn.execute('INSERT OR IGNORE INTO followup_evidence VALUES(?,?,?,?,?)',
                                    (key, config['project_id'], config['chat_id'], kind, json.dumps(row, ensure_ascii=False)))
    return {'status': 'imported' if apply else 'preview', 'events': len({e[0] for e in entries}),
            'source_id': source, 'boundary': 'Source authentication and complete chat reading are the configured host responsibility; no business rows were changed.'}


def response_state(ledger, config, receipt, *, now, active_fields=None):
    current = instant(now)
    due = receipt['action'].get('response_deadline')
    due = instant(due) if due else None
    fields = set(receipt['action'].get('response_fields') or ['response'])
    if active_fields is not None: fields &= set(active_fields)
    if not fields: return {'state': 'resolved', 'missing': [], 'replies': []}
    records = []
    if _exists(ledger, 'followup_evidence'):
        records = [(r[0], json.loads(r[1])) for r in ledger.conn.execute(
            'SELECT kind,payload FROM followup_evidence WHERE project_id=? AND chat_id=?', (config['project_id'], receipt['chat_id']))]
    # Removing a source from the trusted configuration also removes its ability to suppress followup.
    records = [(k, r) for k, r in records if r['source_id'] in config.get('reply_evidence_sources', [])]
    replies = sorted([r for k, r in records if k == 'reply' and r['occurrence_key'] == receipt['occurrence_key'] and instant(r['at']) <= current], key=lambda r: instant(r['at']))
    answered = {f for r in replies for f in r.get('answers', {})}
    missing = sorted(fields - answered)
    if replies:
        state = 'answered' if not missing else 'partial' if answered & fields else 'acknowledged'
        return {'state': state, 'missing': missing, 'replies': replies,
                'last_reply_at': replies[-1]['at'], 'late_reply': bool(due and instant(replies[0]['at']) > due)}
    if not due or due > current or instant(receipt['sent_at']) >= due:
        return {'state': 'not_due', 'missing': sorted(fields), 'replies': []}
    if (due - instant(receipt['sent_at'])).total_seconds() < config.get('min_response_minutes', 30) * 60:
        return {'state': 'too_recent', 'missing': sorted(fields), 'replies': []}
    cursor = instant(receipt['sent_at'])
    intervals = sorted([(instant(r['from']), instant(r['through'])) for k, r in records
                        if k == 'coverage' and r['complete'] and instant(r['captured_at']) <= current])
    for start, end in intervals:
        if start <= cursor: cursor = max(cursor, end)
    return {'state': 'unanswered' if cursor >= due else 'reply_unverified', 'missing': sorted(fields), 'replies': []}


LABELS = {'unanswered': '截至回复期限未发现回复', 'reply_unverified': '回复情况待核验',
          'answered': '已回复，业务事项仍需落实', 'partial': '已回复，问题尚未答全',
          'acknowledged': '已回应，尚缺实质答案', 'not_due': '尚未到回复期限',
          'too_recent': '新发询问，回复时间不足', 'resolved': '对应问题已解除'}


def _risk_facts(row):
    """Only current business facts; never copy historical schedule-audit payloads."""
    return {k: row.get(k) for k in ('status', 'baseline_start', 'baseline_finish', 'forecast_start',
        'forecast_finish', 'delay_reason', 'delay_risk_reason', 'blocker_reason', 'delay_impact',
        'waiting_on', 'next_action', 'next_due_at', 'remediation_owner_id', 'remediation_action',
        'remediation_expected', 'remediation_actual', 'remediation_result')}


def _risk_impact(impact):
    return {k: impact[k] for k in ('task_ids', 'affected_leaf_ids', 'dependency_leaf_ids', 'ancestors',
        'successors', 'dependency_edges', 'downstream_ancestors', 'deliverables', 'date_evidence',
        'project_assessment', 'limitations') if k in impact}


def escalation_review(data, plan, actions, config, ledger, *, now):
    """Compare sent evidence to freshly projected business gaps, never row edit time."""
    from .project_calendar import workday_status
    current = instant(now); local = current.astimezone(ZoneInfo(config['timezone']))
    day = local.date().isoformat()
    project = next((r for r in data.get('projects', []) if r['id'] == config['project_id']), {})
    working = workday_status(data, config['project_id'], day)
    result = {'project_id': config['project_id'], 'date': day, 'as_of': current.isoformat(),
              'recipient_id': config.get('escalation_recipient_id') or project.get('pm_id'),
              'status': 'ready', 'items': [], 'resolved': []}
    if (project.get('stage') in ('paused', 'closed') or not working.get('verified') or not working.get('is_workday')
            or not config.get('escalation_time', '17:00') <= local.strftime('%H:%M') < config.get('quiet_after', '18:00')):
        return dict(result, status='outside_review_window')
    active = {a['id']: a for items in actions['recipients'].values() for a in items}
    covered = set()
    asked = {}
    people = {r['id']: r.get('name', r['id']) for r in data.get('people', [])}
    for receipt in sent_questions(ledger, config['project_id'], day):
        if receipt['chat_id'] != config.get('chat_id'): continue
        saved = receipt['action']; action = active.get(saved['id'])
        if action is None or action.get('recipient_id') != receipt['recipient_id']:
            result['resolved'].append({'id': saved['id'], 'state': 'no_longer_active', 'note': '当前跟进项已不再适用；可能已解除或已改派，不代表收到回复。'})
            continue
        asked.setdefault(action['id'], set()).update(saved.get('response_fields') or ['response'])
        state = response_state(ledger, config, receipt, now=current, active_fields=action.get('response_fields') or ['response'])
        covered.add(action['id'])
        if state['state'] == 'resolved':
            result['resolved'].append(dict(id=action['id'], **state)); continue
        result['items'].append({'id': 'sent:' + receipt['occurrence_key'], 'kind': 'response', 'action': action,
                               'owner': people.get(receipt['recipient_id'], receipt['recipient_id']),
                               'receipt': receipt, 'response': state, 'label': LABELS[state['state']]})
    for identifier, action in active.items():
        if identifier in covered:
            new_fields = set(action.get('response_fields') or ['response']) - asked.get(identifier, set())
            if new_fields:
                new_action = dict(action, response_fields=sorted(new_fields),
                                  questions=[q for q in action.get('questions', []) if q['id'] in new_fields])
                result['items'].append({'id': 'new:' + identifier, 'kind': 'notification', 'action': new_action,
                    'owner': people.get(action['recipient_id'], action['recipient_id']), 'label': '新发现问题，尚无对应询问记录'})
            continue
        key = ':'.join(('reminder', config['project_id'], day, action['recipient_id'], identifier))
        prior = ledger.get(key)
        # P0 occurrence identities may include a source-state suffix.
        if not prior:
            rows = ledger.conn.execute('SELECT key FROM executions WHERE substr(key,1,?)=?', (len(key) + 1, key + ':')).fetchall()
            if rows: prior = ledger.get(rows[-1][0])
        status = prior['status'] if prior else 'not_sent'
        label = {'failed': '通知发送失败', 'unknown': '通知结果待核验', 'running': '通知结果待核验',
                 'succeeded': '已记录发送结果，缺首次发送/回复期限证据'}.get(status, '尚无成功询问记录')
        result['items'].append({'id': 'pending:' + identifier, 'kind': 'notification', 'action': action,
                               'owner': people.get(action['recipient_id'], action['recipient_id']), 'label': label})
    for p in plan.get('pending', []) + actions.get('pending', []):
        source = {'title': p.get('title') or '待定位事项', 'table': p.get('table', ''), 'id': p.get('record_id', '')}
        result['items'].append({'id': 'route:' + digest(source), 'kind': 'routing', 'owner': '待指定', 'label': '通知责任或接收身份待配置',
                               'action': {'source': source, 'action': p.get('reason', ''), 'impact': ''}})
    # Reply/maintenance status cannot suppress current business risk. Each risk ID
    # gets its own facts even when several constraints share one source record.
    workflow = plan.get('workflow') or {}
    work_rows = {r['id']: r for r in data.get('wbs', []) if r.get('project_id') == config['project_id']}
    for task in workflow.get('tasks', []):
        if task.get('delay_state') not in ('risk', 'overdue') and task.get('status') != 'blocked': continue
        facts = _risk_facts(work_rows.get(task['id'], task))
        impact = _risk_impact(task.get('impact') or {})
        result['items'].append({'id': 'risk:' + task['id'], 'kind': 'risk', 'label': '当前延期/阻碍风险',
            'risk_facts': facts, 'risk_impact': impact, 'project_name': project.get('name', project['id']),
            'risk_reason': '；'.join(dict.fromkeys(v for v in [facts.get('delay_reason'), facts.get('delay_risk_reason'),
                            facts.get('blocker_reason'), *task.get('signals', [])] if v)),
            'remediation_owner': people.get(facts.get('remediation_owner_id'), facts.get('remediation_owner_id') or '待指定'),
            'owner': people.get(task.get('owner_id'), '待指定'), 'action': {'source': {'title': task['title'], 'id': task['id'], 'table': 'wbs'},
            'action': '核对原因、继续/等待安排、补救负责人及预计时间，协调所需决定或资源。',
            'impact': impact.get('project_assessment', '项目影响待评估')}})
    for resource in workflow.get('constraints', []) + workflow.get('operations_constraints', []):
        impact = _risk_impact(resource.get('impact') or {})
        linked = {wid: dict(_risk_facts(work_rows[wid]), title=work_rows[wid].get('title', wid))
                  for wid in impact.get('task_ids', resource.get('work_item_ids', [])) if wid in work_rows}
        result['items'].append({'id': 'resource:' + resource['id'], 'kind': 'risk',
            'constraint_id': resource['id'], 'risk_reason': resource.get('reason'),
            'risk_impact': impact, 'project_name': project.get('name', project['id']),
            'linked_task_facts': linked,
            'resource_facts': {k: resource[k] for k in ('kind', 'scope', 'assessment', 'work_item_ids',
                'operation_ids', 'evidence', 'deadline', 'deadline_basis', 'required_fields', 'limitations') if k in resource},
            'label': '运维资源风险' if resource.get('scope') == 'operations' else '资源落实风险',
            'owner': people.get(resource.get('owner_id'), '待指定'),
            'action': {'source': {'title': resource['title'], 'id': resource['source_id'], 'table': resource['source_table']},
                       'action': resource.get('next_action') or resource.get('reason'), 'deadline': resource.get('deadline'),
                       'impact': impact.get('project_assessment', '关联与日期影响待评估')}})
    result['items'] = list({r['id']: r for r in result['items']}.values())
    result['counts'] = {state: sum(x.get('response', {}).get('state') == state for x in result['items']) for state in LABELS}
    return result


def _digest_chunks(review, config):
    from .group_actions import message_limits, _short
    max_items, max_chars = message_limits(config)
    header = '17点项目跟进汇报｜截至 ' + review['as_of'] + '\n请协调未落实事项；回复状态与进度风险分别判断。\n'
    chunks = []; text = header; members = []
    for row in review['items']:
        action = row['action']; receipt = row.get('receipt'); response = row.get('response', {})
        lines = ['【' + row['label'] + '】' + _short(action.get('source', {}).get('title'), 80),
                 '责任人：' + _short(row['owner'], 70), '事项：' + _short(action.get('action'), 180)]
        if receipt:
            lines.append('询问：' + receipt['sent_at'] + '；消息 ' + _short(receipt['message_id'], 60))
            lines.append('回复DDL：' + str(receipt['action'].get('response_deadline') or '未约定'))
            if response.get('replies'):
                reply = response['replies'][-1]
                lines.append('最近回复：' + reply['at'] + ' ' + _short(reply['quote'], 140) + ('（截止后回复）' if response.get('late_reply') else ''))
            if response.get('missing'):
                prompts = {q['id']: q['prompt'] for q in receipt['action'].get('questions', [])}
                lines.append('待回应：' + _short('；'.join(prompts.get(f, f) for f in response['missing']), 180))
        elif action.get('questions'):
            lines.append('待确认：' + _short('；'.join(q['prompt'] for q in action['questions']), 180))
        if action.get('deadline'): lines.append('行动DDL：' + str(action['deadline']))
        if row.get('kind') == 'risk':
            facts = row.get('risk_facts', {}); impact = row.get('risk_impact', {})
            lines.append('现状：' + _short(row.get('risk_reason') or '原因待维护', 90))
            if facts:
                lines.append('完成日期：原计划 ' + str(facts.get('baseline_finish') or '待补') +
                    '；当前预计 ' + str(facts.get('forecast_finish') or '待补') + '；补救预计 ' + str(facts.get('remediation_expected') or '待补'))
                lines.append('补救：' + _short(row.get('remediation_owner'), 30) + '｜' + _short(facts.get('remediation_action') or '措施待补', 75))
                lines.append('继续/等待安排：' + _short('；'.join(v for v in [facts.get('waiting_on'), facts.get('next_action'), facts.get('next_due_at')] if v) or '待负责人确认', 75))
            elif row.get('linked_task_facts'):
                linked_dates = [v['title'] + '：原 ' + str(v.get('baseline_finish') or '待补') + '，当前 ' +
                    str(v.get('forecast_finish') or '待补') + '，补救 ' + str(v.get('remediation_expected') or '待补')
                    for v in row['linked_task_facts'].values()]
                lines.append('关联任务日期：' + _short('；'.join(linked_dates), 100))
            ancestor_names = [n.get('title') or n.get('id', '上级待定位') for n in impact.get('ancestors', [])]
            if row.get('constraint_id'):
                # The resource projection combines several branches. Their union
                # is a scope set, never an ordered dependency/ancestor chain.
                task_names = [v['title'] for v in row.get('linked_task_facts', {}).values()]
                lines.append('关联任务：' + _short('、'.join(task_names) or '待定位', 65) +
                    '；涉及上级：' + _short('、'.join(ancestor_names) or '待定位', 65))
                if impact.get('task_ids'): lines.append('项目：' + _short(row.get('project_name'), 40))
            else:
                names = [action.get('source', {}).get('title', ''), *ancestor_names]
                if impact.get('task_ids'): names.append(row.get('project_name', '项目'))
                lines.append('影响路径：' + _short(' → '.join(names), 100))
            if impact.get('date_evidence'):
                comparisons = [str(e.get('title') or e.get('target_id') or '对象待定位') + '原计划 ' + str(e.get('comparison_date') or '待补') +
                    '，子项预计 ' + str(e.get('forecast_finish') or '待补') for e in impact['date_evidence']]
                lines.append('日期对照：' + _short('；'.join(comparisons), 100))
        if action.get('impact'): lines.append('影响：' + _short(action['impact'], 170))
        lines.append('需协调：确认未答问题、责任/落实时间及所需决定或资源。')
        block = '\n'.join(lines) + '\n\n'
        if members and (len(members) >= max_items or len(text) + len(block) > max_chars):
            chunks.append({'text': text.rstrip(), 'item_ids': members}); text = header; members = []
        if len(header) + len(block) > max_chars: raise ValueError('Escalation item exceeds configured message bounds')
        members.append(row['id']); text += block
    if members: chunks.append({'text': text.rstrip(), 'item_ids': members})
    return chunks


def dispatch_escalation(data, plan, actions, config, ledger, client, *, now, dry_run=True):
    from .workflow import _available, _contactable
    from .feishu import FeishuError
    review = escalation_review(data, plan, actions, config, ledger, now=now)
    deliveries = []
    if review['status'] != 'ready': return {'review': review, 'deliveries': deliveries}
    person = next((r for r in data.get('people', []) if r['id'] == review['recipient_id']), {})
    if not _available(person, review['date']) or (config.get('mode') != 'excel' and not _contactable(person, 'feishu')):
        return {'review': review, 'deliveries': [{'status': 'pending_route', 'recipient_id': review['recipient_id']}]}
    chunks = _digest_chunks(review, config)
    if dry_run: return {'review': review, 'deliveries': [dict(c, status='preview', recipient_id=person['id']) for c in chunks]}
    if not chunks and not _exists(ledger, 'followup_digests'):
        return {'review': review, 'deliveries': deliveries}
    if not chunks and not ledger.conn.execute('SELECT 1 FROM followup_digests WHERE project_id=? AND day=?',
                                             (config['project_id'], review['date'])).fetchone():
        return {'review': review, 'deliveries': deliveries}
    if not config.get('enabled') or not config.get('chat_id'):
        return {'review': review, 'deliveries': [{'status': 'disabled'}]}
    if config.get('mode') == 'excel' or not _contactable(person, 'feishu'):
        return {'review': review, 'deliveries': [{'status': 'pending_identity'}]}
    if person['id'] not in config.get('allowed_recipients', []):
        return {'review': review, 'deliveries': [{'status': 'outside_recipient_scope'}]}
    _ensure(ledger)
    scope = {'chat_id': config['chat_id'], 'recipient_id': person['id'], 'open_id': person['feishu_user_id']}
    frozen = dict(scope, chunks=chunks, as_of=review['as_of'])
    with ledger.conn:
        ledger.conn.execute('INSERT OR IGNORE INTO followup_digests VALUES(?,?,?)',
                            (config['project_id'], review['date'], json.dumps(frozen, ensure_ascii=False)))
        frozen = json.loads(ledger.conn.execute('SELECT payload FROM followup_digests WHERE project_id=? AND day=?', (config['project_id'], review['date'])).fetchone()[0])
    if any(frozen[k] != v for k, v in scope.items()):
        return {'review': review, 'deliveries': [{'status': 'scope_changed_pending'}]}
    for index, chunk in enumerate(frozen['chunks']):
        key = ':'.join(('daily-escalation', config['project_id'], review['date'], str(index)))
        fingerprint = digest(dict(scope, **chunk))
        state = ledger.claim(key, fingerprint, max_attempts=config.get('max_attempts', 3))
        if not state.get('claimed_now'):
            deliveries.append({'key': key, 'status': state['status'], 'duplicate': True}); continue
        detail = dict(chunk, as_of=frozen['as_of'], recipient_id=person['id'])
        try:
            sent = client.mention(config['chat_id'], person['feishu_user_id'], chunk['text'], request_id=state['request_id'])
            detail['message_id'] = sent.get('message_id'); status = 'succeeded' if detail['message_id'] else 'unknown'
        except FeishuError as exc:
            status = 'unknown' if exc.uncertain else 'failed'; detail['error'] = str(exc)
        except Exception:
            status = 'unknown'; detail['error'] = 'Transport result could not be verified'
        ledger.save(key, fingerprint, status, detail)
        deliveries.append({'key': key, 'status': status, 'detail': detail})
    return {'review': review, 'deliveries': deliveries}
