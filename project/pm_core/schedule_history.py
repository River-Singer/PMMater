"""Append-only remote observations. Observation times are not edit timestamps."""
import json
from pathlib import Path
import sqlite3
from .actions import digest
from .store import parse_datetime
from .schedule_audit import audit_schedule_changes, TABLE_FIELDS


class ScheduleObserver:
    def __init__(self, path_or_connection):
        self.owned = not isinstance(path_or_connection, sqlite3.Connection)
        if self.owned:
            Path(path_or_connection).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path_or_connection, timeout=30)
        else:
            self.conn = path_or_connection
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS schedule_observations(
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                project_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL, data_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_observed_changes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                project_id TEXT NOT NULL, observation_id INTEGER NOT NULL,
                event_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_observation_checks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                project_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                snapshot_id INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS schedule_observation_scope
                ON schedule_observations(source_id,project_id,id);
        ''')

    def close(self):
        if self.owned:
            self.conn.close()

    def observe(self, data, *, source_id, project_id, now, policy=None):
        instant = parse_datetime(now) if isinstance(now, str) else now
        if instant.tzinfo is None:
            raise ValueError('Observation time must include timezone')
        projects = data.get('projects', [])
        if len(projects) != 1 or projects[0]['id'] != project_id:
            raise ValueError('Observer requires the configured single project snapshot')
        fingerprint = digest(data)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            previous = self.conn.execute('SELECT * FROM schedule_observations WHERE source_id=? AND project_id=? ORDER BY id DESC LIMIT 1', (source_id, project_id)).fetchone()
            checkpoint = self.conn.execute('SELECT observed_at FROM schedule_observation_checks WHERE source_id=? AND project_id=? ORDER BY id DESC LIMIT 1', (source_id, project_id)).fetchone()
            previous_at = checkpoint['observed_at'] if checkpoint else previous['observed_at'] if previous else None
            if previous_at and parse_datetime(previous_at) > instant:
                raise ValueError('Cannot backdate a remote observation')
            captured = previous is None or previous['fingerprint'] != fingerprint
            if captured:
                cur = self.conn.execute('INSERT INTO schedule_observations(source_id,project_id,observed_at,fingerprint,data_json) VALUES(?,?,?,?,?)',
                    (source_id, project_id, instant.isoformat(), fingerprint, json.dumps(data, ensure_ascii=False)))
                capture_id = cur.lastrowid
                before = json.loads(previous['data_json']) if previous else {}
                for table, fields in TABLE_FIELDS.items():
                    old = {r['id']: r for r in before.get(table, [])}
                    current = {r['id']: r for r in data.get(table, [])}
                    for rid in sorted(set(old) | set(current)):
                        a, b = old.get(rid), current.get(rid)
                        if a and b and all(a.get(f) == b.get(f) for f in fields):
                            continue
                        event = {'table': table, 'record_id': rid, 'before': a, 'after': b,
                                 'actor': None, 'recorded_at': None, 'source_kind': 'observation',
                                 'observed_at': instant.isoformat(),
                                 'previous_observed_at': previous_at,
                                 'source': source_id, 'event_id': f'observation:{source_id}:{capture_id}:{table}:{rid}'}
                        self.conn.execute('INSERT INTO schedule_observed_changes(source_id,project_id,observation_id,event_json) VALUES(?,?,?,?)',
                                          (source_id, project_id, capture_id, json.dumps(event, ensure_ascii=False)))
            self.conn.execute('INSERT INTO schedule_observation_checks(source_id,project_id,observed_at,snapshot_id) VALUES(?,?,?,?)',
                              (source_id, project_id, instant.isoformat(), capture_id if captured else previous['id']))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        events = [json.loads(r['event_json']) for r in self.conn.execute(
            'SELECT event_json FROM schedule_observed_changes WHERE source_id=? AND project_id=? ORDER BY id', (source_id, project_id))]
        result = audit_schedule_changes(data, history=events, now=instant, policy=policy)
        count = self.conn.execute('SELECT COUNT(*) FROM schedule_observations WHERE source_id=? AND project_id=?', (source_id, project_id)).fetchone()[0]
        result['coverage'].update(mode='observations' if count > 1 else 'unknown', complete=False,
                                  observations=count, source_id=source_id, captured_new_snapshot=captured)
        result['coverage']['limitations'].extend(['观察时点不是实际修改时点；操作者未知，不能从负责人推断。',
                                                 '两次观察之间修改后又改回原值的过程无法检出。'])
        if count == 1:
            result['coverage']['limitations'].append('首次记录只建立后续比较起点；之前有无改期未知，首次值不是违规。')
        return result


def source_identity(config):
    mapping = {k: v.get('table_id') for k, v in config.get('tables', {}).items()}
    # Keep legacy fingerprints unchanged; a new identity mode starts explicit
    # new coverage rather than joining different IDs to old audit observations.
    identities = {k: v['identity'] for k, v in config.get('tables', {}).items()
                  if v.get('identity', 'field') != 'field'}
    if identities:
        mapping = {'tables': mapping, 'identities': identities}
    return 'feishu:' + config['base_token'] + ':' + digest(mapping)[:16]
