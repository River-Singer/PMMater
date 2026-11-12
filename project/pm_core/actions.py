"""Durable sourced actions. Model extraction is separate from deterministic execution."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import uuid

from .feishu import FeishuError, WriteConflict
from .schema import TABLES
from .store import Store, ValidationError, parse_datetime


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Ledger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE IF NOT EXISTS executions (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, request_id TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL)')
        self.conn.commit()

    def close(self):
        self.conn.close()

    def get(self, key):
        row = self.conn.execute('SELECT * FROM executions WHERE key=?', (key,)).fetchone()
        return dict(row, detail=json.loads(row['detail'])) if row else None

    def claim(self, key, fingerprint, *, max_attempts=3):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            old = self.get(key)
            if old and (old['status'] != 'failed' or old['fingerprint'] != fingerprint or old['detail'].get('attempts', 1) >= max_attempts):
                return dict(old, claimed_now=False)
            if old:
                retry_detail = dict(old['detail'], attempts=old['detail'].get('attempts', 1) + 1)
                self.save(key, fingerprint, 'running', retry_detail, commit=False)
                return dict(self.get(key), claimed_now=True)
            self.save(key, fingerprint, 'running', {'steps': {}, 'attempts': 1}, request_id=str(uuid.uuid4()), commit=False)
            return dict(self.get(key), claimed_now=True)
        finally:
            self.conn.commit()

    def save(self, key, fingerprint, status, detail, request_id=None, commit=True):
        old = self.get(key)
        detail = dict(detail)
        if old:
            detail.setdefault('attempts', old['detail'].get('attempts', 1))
        request_id = request_id or (old['request_id'] if old else str(uuid.uuid4()))
        self.conn.execute('INSERT INTO executions VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint,status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at',
                          (key, fingerprint, status, request_id, json.dumps(detail, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        if commit:
            self.conn.commit()


    def reconcile(self, key, *, outcome, evidence):
        if not evidence or not str(evidence).strip():
            raise ValueError('Reconciliation requires remote verification evidence')
        old = self.get(key)
        if not old or old['status'] not in ('unknown', 'running', 'failed'):
            raise ValueError('Only failed or uncertain executions can be reconciled')
        if outcome not in ('applied', 'not_applied'):
            raise ValueError('Outcome must be applied or not_applied')
        detail = dict(old['detail'])
        detail.setdefault('reconciliations', []).append({'outcome': outcome, 'evidence': evidence,
                                                       'at': datetime.now(timezone.utc).isoformat()})
        detail['attempts'] = 0 if outcome == 'not_applied' else detail.get('attempts', 1)
        self.save(key, old['fingerprint'], 'succeeded' if outcome == 'applied' else 'failed', detail)
        return self.get(key)


class LocalBackend:
    def __init__(self, store, actor='meeting-agent'):
        self.store, self.actor = store, actor

    def read_all(self):
        return self.store.data()

    def read(self, table, record_id):
        return next((r for r in self.store.data()[table] if r['id'] == record_id), None)

    def references(self, table, record_id):
        refs = []
        for t, rows in self.store.data().items():
            for f in TABLES[t]['fields']:
                if f.ref == table:
                    refs.extend((t, r['id'], f.name) for r in rows if r.get(f.name) == record_id or
                                isinstance(r.get(f.name), list) and record_id in r[f.name])
        return refs

    def validate_write(self, operation, table, record_id, values):
        old = self.read(table, record_id)
        if operation == 'delete':
            self.store.delete_record(table, record_id, version=old['version'], actor=self.actor, dry_run=True)
        else:
            row = dict(values, id=record_id)
            if old:
                row['version'] = old['version']
            self.store.apply_batch({table: [row]}, actor=self.actor, dry_run=True)

    def write(self, operation, table, record_id, values, request_id, *, expected=None):
        old = self.read(table, record_id)
        if old != expected:
            raise WriteConflict('Record changed before local write')
        if operation == 'create' and old:
            raise ValueError('Record already exists')
        row = dict(values, id=record_id)
        if expected:
            row['version'] = expected['version']
        try:
            if operation == 'delete':
                return self.store.delete_record(table, record_id, version=expected['version'], actor=self.actor)
            # apply_batch checks the reviewed version under BEGIN IMMEDIATE;
            # never replace it with a newly fetched version after validation.
            return self.store.apply_batch({table: [row]}, actor=self.actor)
        except ValidationError as exc:
            if any(e.get('code') == 'version_conflict' for e in exc.errors):
                raise WriteConflict('Record changed during local write') from exc
            raise


def validate_action(action, policy, *, assigned_create=False):
    for field in ('id', 'operation', 'source'):
        if not action.get(field):
            raise ValueError('Action missing ' + field)
    source = action['source']
    if not all(source.get(k) for k in ('id', 'url', 'quote', 'kind')):
        raise ValueError('Source ID, URL, quote and kind are required')
    if source['kind'] not in ('confirmed_fact', 'confirmed_decision'):
        raise ValueError('Suggestion or unconfirmed AI summary: pending decision')
    op = action['operation']
    if op not in ('query', 'create', 'update', 'delete', 'book', 'reschedule'):
        raise ValueError('Unsupported action operation')
    if op not in policy.get('allowed_operations', []):
        raise ValueError('Operation outside configured authority')
    values = action.get('values', {})
    important = op in ('delete', 'reschedule') or any(k in values for k in policy.get(
        'important_fields', ['baseline_start', 'baseline_finish', 'acceptance', 'stage', 'goal', 'net_available_hours', 'project_hours', 'operations_hours', 'buffer_hours', 'acceptance_result']))
    if action.get('table') == 'deliverables' and (values.get('acceptance_status') in ('accepted', 'rejected') or 'reviewed_at' in values):
        important = True
    if action.get('table') == 'registers' and (values.get('status') in ('approved', 'rejected') or any(k in values for k in ('decision', 'decided_at'))):
        important = True
    if important:
        approval = action.get('approval', {})
        if approval.get('by') not in policy.get('decision_makers', []) or not approval.get('evidence'):
            raise ValueError('Important change needs recorded decision authority')
    if op in ('book', 'reschedule'):
        if action.get('calendar_id') != policy.get('calendar_id'):
            raise ValueError('Calendar outside configured scope')
        start, end = parse_datetime(values['start']), parse_datetime(values['end'])
        if end <= start or not values.get('title') or not values.get('organizer_id'):
            raise ValueError('Complete title, organizer and valid meeting interval required')
        if values['organizer_id'] != policy.get('organizer_id'):
            raise ValueError('Organizer must match configured calling calendar identity')
        attendees = values.get('attendees', [])
        if not attendees or set(attendees) - set(policy.get('allowed_attendees', [])):
            raise ValueError('Meeting attendees missing/outside configured scope')
        if op == 'reschedule' and (not action.get('event_id') or not action.get('expected_event')):
            raise ValueError('Reschedule requires an exact event ID and previous event fields')
        return
    table = action.get('table')
    if table not in TABLES or table not in policy.get('allowed_tables', []):
        raise ValueError('Table outside configured scope')
    if assigned_create and (action.get('record_id') is not None or 'id' in values):
        raise ValueError('Remote-assigned create must omit record_id and values.id; use the action id for deduplication')
    if not assigned_create and not action.get('record_id'):
        raise ValueError('An exact stable business ID is required')
    fields = {f.name: f for f in TABLES[table]['fields']}
    if set(values) - set(fields) or any(not fields[k].editable for k in values):
        raise ValueError('Unknown or system-only fields')
    if 'id' in values and values['id'] != action['record_id']:
        raise ValueError('Business ID cannot change')
    if op == 'update' and not set(values).issubset(action.get('expected', {})):
        raise ValueError('Every changed field needs its previous value')
    if op == 'delete' and (not action.get('delete_intent') or not action.get('expected')):
        raise ValueError('Explicit deletion intent and full prior record required')


def execute_action(action, backend, ledger, policy, *, client=None, dry_run=True):
    key = 'action:' + str(action.get('source', {}).get('id')) + ':' + str(action.get('id'))
    fingerprint = digest(action)
    try:
        assigned_create = (action.get('operation') == 'create' and
                           hasattr(backend, 'assigns_record_id') and
                           backend.assigns_record_id(action.get('table')))
        validate_action(action, policy, assigned_create=assigned_create)
    except (ValueError, KeyError, TypeError) as exc:
        return {'key': key, 'status': 'pending', 'reason': str(exc)}
    old = ledger.get(key)
    if old:
        if old['fingerprint'] != fingerprint:
            return {'key': key, 'status': 'revision_pending', 'reason': 'Existing action changed; review a corrective action against current values'}
        if old['status'] in ('succeeded', 'unknown', 'running'):
            return {'key': key, 'status': old['status'], 'detail': old['detail'], 'duplicate': True}
    op = action['operation']
    # A temporary validation identity is never sent to the server or stored as
    # a real entity. Its only purpose is complete-state create validation.
    record_id = 'pending-' + digest(key)[:32] if assigned_create else action.get('record_id')
    before = None
    if op not in ('book', 'reschedule'):
        before = backend.read(action['table'], record_id)
        if op == 'query':
            return {'key': key, 'status': 'read', 'record': before}
        if op == 'create' and before:
            return {'key': key, 'status': 'conflict', 'reason': 'Record already exists'}
        if op != 'create':
            if before is None or any(before.get(k) != v for k, v in action.get('expected', {}).items()):
                return {'key': key, 'status': 'conflict', 'reason': 'Current record differs from extracted source snapshot'}
        if op == 'delete':
            if set(before) - set(action['expected']):
                return {'key': key, 'status': 'pending', 'reason': 'Deletion requires complete current snapshot'}
            refs = backend.references(action['table'], action['record_id'])
            if refs:
                return {'key': key, 'status': 'pending', 'reason': 'Record is referenced', 'references': refs}
    if op not in ('book', 'reschedule') and hasattr(backend, 'validate_write'):
        try:
            backend.validate_write(op, action['table'], record_id, action.get('values', {}))
        except (ValueError, KeyError, TypeError) as exc:
            return {'key': key, 'status': 'pending', 'reason': str(exc)}
    if op == 'reschedule' and client and not (old and old['detail'].get('event_written')):
        current_event = client.read_event(action['calendar_id'], action['event_id'])
        current_event = current_event.get('event', current_event)
        if any(current_event.get(k) != v for k, v in action['expected_event'].items()):
            return {'key': key, 'status': 'conflict', 'reason': 'Calendar event changed since extraction'}
    if dry_run:
        return {'key': key, 'status': 'preview', 'before': before, 'action': action}
    if not policy.get('enabled'):
        return {'key': key, 'status': 'disabled'}
    state = ledger.claim(key, fingerprint, max_attempts=policy.get('max_attempts', 3))
    detail = state['detail']
    # A concurrent caller which observed no old row must not share the claimed occurrence.
    if not state.get('claimed_now'):
        return {'key': key, 'status': state['status'], 'duplicate': True}
    detail.update(claimed=True, source=action['source'], before=before, action=action)
    ledger.save(key, fingerprint, 'running', detail)
    try:
        if op in ('book', 'reschedule'):
            if client is None:
                raise ValueError('Calendar connector unavailable')
            values = action['values']
            event_id = detail.get('event_id') or action.get('event_id')
            if not detail.get('event_written'):
                event = client.event(action['calendar_id'], {
                    'summary': values['title'], 'description': values.get('description', ''),
                    'start_time': {'timestamp': str(int(parse_datetime(values['start']).timestamp()))},
                    'end_time': {'timestamp': str(int(parse_datetime(values['end']).timestamp()))},
                    'need_notification': True}, event_id=event_id)
                event_id = event.get('event', event)['event_id']
                detail.update(event_id=event_id, event_written=True)
                ledger.save(key, fingerprint, 'running', detail)
            if not detail.get('invited'):
                client.invite(action['calendar_id'], event_id, values['attendees'])
                detail['invited'] = True
            checked = client.read_event(action['calendar_id'], event_id)
            checked = checked.get('event', checked)
            if checked.get('event_id') != event_id or checked.get('summary') != values['title'] or any(
                str(checked.get(k, {}).get('timestamp')) != str(int(parse_datetime(values[v]).timestamp()))
                for k, v in (('start_time', 'start'), ('end_time', 'end'))):
                raise FeishuError('Calendar content readback mismatch', uncertain=True)
            detail['result'] = {'event_id': event_id, 'invitation_submitted': True}
        else:
            values = dict(action.get('values', {}))
            if op == 'create' and not assigned_create:
                values['id'] = record_id
            detail['write_result'] = backend.write(op, action['table'], record_id, values, state['request_id'], expected=before)
            ledger.save(key, fingerprint, 'running', detail)
            if assigned_create:
                response = detail['write_result']
                record = response.get('record') if isinstance(response, dict) else None
                record_id = record.get('record_id') if isinstance(record, dict) else None
                if not isinstance(record_id, str) or not record_id:
                    raise FeishuError('Created record identity missing; inspect remote before retry', uncertain=True)
            detail['record_id'] = record_id
            ledger.save(key, fingerprint, 'running', detail)
            after = backend.read(action['table'], record_id)
            detail['after'] = after
            displayed = getattr(backend, 'display_values', {}).get(action['table'], {})
            if op != 'delete' and record_id in displayed:
                detail['display_value'] = displayed[record_id]
                if not str(displayed[record_id] or '').strip():
                    raise FeishuError('Display value is empty after write; verify the source formula before recovery', uncertain=True)
            if op == 'delete' and after is not None or op != 'delete' and (
                after is None or any(after.get(k) != v for k, v in values.items())):
                raise FeishuError('Write readback mismatch; reconcile before retry', uncertain=True)
        status = 'succeeded'
    except WriteConflict as exc:
        status = 'conflict'
        detail['error'] = str(exc)
    except FeishuError as exc:
        status = 'unknown' if exc.uncertain or 'write_result' in detail else 'failed'
        detail['error'] = str(exc)
    except (ValueError, KeyError, TypeError) as exc:
        status = 'unknown' if 'write_result' in detail else 'failed'
        detail['error'] = str(exc)
    ledger.save(key, fingerprint, status, detail)
    return {'key': key, 'status': status, 'detail': detail}
