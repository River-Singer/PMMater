import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pm_core.actions import Ledger, execute_action
from pm_core.feishu import BitableAdapter, FeishuClient, FeishuError
from pm_core.runtime import dispatch_tick
from pm_core.schema import TABLES


class MemoryBackend:
    def __init__(self):
        self.rows = {'A': {'id': 'A', 'title': 'old', 'status': 'intake'}}
        self.writes = []
        self.refs = []

    def read(self, table, rid):
        return copy.deepcopy(self.rows.get(rid))

    def references(self, table, rid):
        return self.refs

    def write(self, op, table, rid, values, request_id, *, expected=None):
        self.writes.append((op, rid, request_id))
        if op == 'delete':
            self.rows.pop(rid)
        else:
            self.rows[rid] = dict(self.rows.get(rid, {}), **values, id=rid)
        return {'ok': True}


def action(op='update'):
    return {'id': 'act-1', 'operation': op, 'table': 'wbs', 'record_id': 'A',
            'values': {'title': 'new'}, 'expected': {'title': 'old'},
            'source': {'id': 'minute-1', 'url': 'https://example.invalid/minute',
                       'quote': 'rename task to new', 'kind': 'confirmed_fact'}}


POLICY = {'enabled': True, 'allowed_tables': ['wbs'],
          'allowed_operations': ['query', 'create', 'update', 'delete', 'book', 'reschedule'],
          'decision_makers': ['PM'], 'calendar_id': 'cal', 'organizer_id': 'owner',
          'allowed_attendees': ['ou_person']}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(str(Path(self.temp.name) / 'runtime.sqlite'))
        self.addCleanup(self.ledger.close)
        self.backend = MemoryBackend()

    def test_action_preview_and_idempotency(self):
        a = action()
        self.assertEqual('preview', execute_action(a, self.backend, self.ledger, POLICY)['status'])
        self.assertFalse(self.backend.writes)
        self.assertEqual('succeeded', execute_action(a, self.backend, self.ledger, POLICY, dry_run=False)['status'])
        self.assertTrue(execute_action(a, self.backend, self.ledger, POLICY, dry_run=False)['duplicate'])
        self.assertEqual(1, len(self.backend.writes))
        a['values']['title'] = 'another'
        self.assertEqual('revision_pending', execute_action(a, self.backend, self.ledger, POLICY, dry_run=False)['status'])

    def test_conflict_and_suggestion_never_write(self):
        self.backend.rows['A']['title'] = 'human edit'
        self.assertEqual('conflict', execute_action(action(), self.backend, self.ledger, POLICY, dry_run=False)['status'])
        a = action(); a['source']['kind'] = 'suggestion'
        self.assertEqual('pending', execute_action(a, self.backend, self.ledger, POLICY, dry_run=False)['status'])
        self.assertFalse(self.backend.writes)

    def test_delete_authority_and_references(self):
        a = action('delete'); a['values'] = {}; a['expected'] = self.backend.rows['A'].copy()
        a['delete_intent'] = True
        self.assertEqual('pending', execute_action(a, self.backend, self.ledger, POLICY, dry_run=False)['status'])
        a['approval'] = {'by': 'PM', 'evidence': 'confirmed duplicate'}
        self.backend.refs = [('registers', 'R', 'work_item_id')]
        self.assertEqual('pending', execute_action(a, self.backend, self.ledger, POLICY, dry_run=False)['status'])
        self.backend.refs = []
        self.assertEqual('succeeded', execute_action(a, self.backend, self.ledger, POLICY, dry_run=False)['status'])
        self.assertIsNone(self.backend.read('wbs', 'A'))

    def test_unknown_write_not_retried(self):
        def unavailable(*args, **kwargs):
            raise FeishuError('timeout', uncertain=True)
        self.backend.write = unavailable
        first = execute_action(action(), self.backend, self.ledger, POLICY, dry_run=False)
        self.assertEqual('unknown', first['status'])
        self.assertTrue(execute_action(action(), self.backend, self.ledger, POLICY, dry_run=False)['duplicate'])

    def test_failed_invite_resumes_event(self):
        class Calendar:
            creates = 0
            invites = 0
            def event(self, cal, values, event_id=None):
                self.creates += 1; self.values = values
                return {'event': dict(values, event_id='event1')}
            def invite(self, *args):
                self.invites += 1
                if self.invites == 1:
                    raise FeishuError('missing temporary permission', code=403)
                return {}
            def read_event(self, *args):
                return {'event': dict(self.values, event_id='event1')}
        client = Calendar()
        a = action('book'); a['calendar_id'] = 'cal'
        a['values'] = {'title': 'Review', 'start': '2026-11-12T14:00:00+08:00',
                       'end': '2026-11-12T15:00:00+08:00', 'organizer_id': 'owner', 'attendees': ['ou_person']}
        self.assertEqual('failed', execute_action(a, self.backend, self.ledger, POLICY, client=client, dry_run=False)['status'])
        self.assertEqual('succeeded', execute_action(a, self.backend, self.ledger, POLICY, client=client, dry_run=False)['status'])
        self.assertEqual(1, client.creates)
        self.assertEqual(2, client.invites)

    def test_retry_is_bounded_and_reconciliation_is_recorded(self):
        def denied(*args, **kwargs):
            raise FeishuError('permission denied', code=403)
        self.backend.write = denied
        for _ in range(8):
            execute_action(action(), self.backend, self.ledger, POLICY, dry_run=False)
        state = self.ledger.get('action:minute-1:act-1')
        self.assertEqual(3, state['detail']['attempts'])
        resumed = self.ledger.reconcile(state['key'], outcome='not_applied', evidence='Remote record unchanged; permission repaired')
        self.assertEqual(0, resumed['detail']['attempts'])
        self.assertEqual('not_applied', resumed['detail']['reconciliations'][-1]['outcome'])

    def test_claim_has_single_owner(self):
        first = self.ledger.claim('x', 'hash')
        second = self.ledger.claim('x', 'hash')
        self.assertTrue(first['claimed_now'])
        self.assertFalse(second['claimed_now'])

    def test_feishu_pagination_and_real_mention(self):
        calls = []
        def transport(method, path, body, query, raw):
            calls.append((method, path, body, query))
            if path == '/im/v1/messages':
                content = json.loads(body['content'])
                self.assertEqual('at', content['zh_cn']['content'][0][0]['tag'])
                self.assertEqual('ou_person', content['zh_cn']['content'][0][0]['user_id'])
                return {'code': 0, 'data': {'message_id': 'msg1'}}
            token = query.get('page_token')
            return {'code': 0, 'data': {'items': [2 if token else 1], 'has_more': not token, 'page_token': 'next'}}
        client = FeishuClient(transport=transport)
        self.assertEqual([1, 2], client.pages('POST', '/test', body={}))
        client.mention('chat', 'ou_person', 'update', request_id='id')
        self.assertEqual('next', calls[1][3]['page_token'])

    def test_pagination_loop_rejected(self):
        client = FeishuClient(transport=lambda *args: {'code': 0, 'data': {'items': [], 'has_more': True, 'page_token': 'same'}})
        with self.assertRaises(FeishuError):
            client.pages('POST', '/test')

    def test_person_field_roundtrip_uses_stable_identity(self):
        adapter = BitableAdapter(FeishuClient(transport=lambda *args: {}), {'base_token': 'base', 'timezone': 'Asia/Shanghai', 'tables': {'wbs': {'table_id': 'tbl', 'fields': {'id': '编号', 'owner_id': {'name': '负责人', 'type': 'person'}}}}})
        adapter._user_maps = {'PER-1': 'ou_person'}
        self.assertEqual([{'id': 'ou_person'}], adapter._value('wbs', 'owner_id', 'PER-1', encode=True))
        self.assertEqual('PER-1', adapter._value('wbs', 'owner_id', [{'id': 'ou_person', 'name': 'Same Name'}]))

    def test_bitable_unread_tables_not_zero(self):
        config = {'base_token': 'base', 'timezone': 'Asia/Shanghai', 'tables': {}}
        with self.assertRaisesRegex(ValueError, 'Unread table coverage'):
            BitableAdapter(FeishuClient(transport=lambda *args: {}), config).read_all()

    def test_bitable_maps_select_and_rich_text(self):
        def transport(*args):
            if args[1].endswith('/fields'):
                codes = {'text': 1, 'select': 3, 'checkbox': 7}
                return {'code': 0, 'data': {'items': [{'field_name': v if isinstance(v,str) else v['name'], 'type': 1 if isinstance(v,str) else codes[v['type']]} for v in config['tables']['people']['fields'].values()]}}
            return {'code': 0, 'data': {'items': [{'record_id': 'rec1', 'fields': {
                '编号': [{'text': 'P1'}], '姓名': [{'text': '张工'}], '类型': '外部', '有效': True}}]}}
        config = {'base_token': 'base', 'timezone': 'Asia/Shanghai',
                  'confirmed_empty_tables': [t for t in TABLES if t != 'people'],
                  'tables': {'people': {'table_id': 'tbl1', 'fields': {
                    'id': '编号', 'name': '姓名', 'person_type': {'name': '类型', 'type': 'select', 'options': {'vendor': '外部'}},
                    'active': {'name': '有效', 'type': 'checkbox'}}}}}
        for f in TABLES['people']['fields']:
            if f.name != 'version':
                config['tables']['people']['fields'].setdefault(f.name, f.label)
        data = BitableAdapter(FeishuClient(transport=transport), config).read_all()
        self.assertEqual('vendor', data['people'][0]['person_type'])
        self.assertEqual('张工', data['people'][0]['name'])

    def test_tick_times_and_unknown_calendar(self):
        from pm_core.runtime import plan_tick
        data = {'projects': [{'id': 'PRJ', 'calendar_id': 'CAL', 'stage': 'executing'}],
                'calendars': [{'id': 'CAL', 'timezone': 'Asia/Shanghai'}],
                'people': [{'id': 'P1', 'active': True}], 'calendar_days': []}
        config = {'project_id': 'PRJ', 'timezone': 'Asia/Shanghai'}
        attendance = {'eligible': [{'person_id': 'P1', 'periods': ['AM', 'PM']}], 'pending': [], 'skipped': []}
        task = {'kind': 'blocked', 'recipient_id': 'P1', 'record_id': 'A', 'reason': 'blocked', 'required_fields': []}
        with patch('pm_core.attendance.plan_daily_confirmations', return_value=attendance), patch('pm_core.operating.plan_followups', return_value=[task]), patch('pm_core.project_calendar.workday_status', return_value={'verified': True, 'is_workday': True}):
            self.assertFalse(plan_tick(data, config, now='2026-11-11T00:00:00+08:00')['recipients'])
            nine = plan_tick(data, config, now='2026-11-11T09:00:00+08:00')['recipients']['P1']
            self.assertEqual(['daily_confirmation'], [i['kind'] for i in nine])
            ten = plan_tick(data, config, now='2026-11-11T10:30:00+08:00')['recipients']['P1']
            self.assertEqual(['daily_confirmation_followup', 'blocked'], [i['kind'] for i in ten])
            self.assertFalse(plan_tick(data, config, now='2026-11-11T18:00:00+08:00')['recipients'])
        with patch('pm_core.attendance.plan_daily_confirmations', return_value={'eligible': []}), patch('pm_core.project_calendar.workday_status', return_value={'verified': False, 'is_workday': None}):
            self.assertFalse(plan_tick(data, config, now='2026-11-11T11:00:00+08:00')['recipients'])

    def test_daily_send_does_not_suppress_new_issue_or_cutoff(self):
        data = {'people': [{'id': 'P1', 'active': True, 'feishu_user_id': 'ou_person', 'feishu_id_type': 'open_id'}]}
        config = {'project_id': 'PRJ', 'timezone': 'Asia/Shanghai', 'enabled': True,
                  'chat_id': 'chat', 'allowed_recipients': ['P1']}
        deliveries = []
        class Client:
            def mention(self, *args, **kwargs):
                deliveries.append(args)
                return {'message_id': 'msg' + str(len(deliveries))}
        def tick(items):
            plan = {'date': '2026-11-11', 'recipients': {'P1': items}}
            with patch('pm_core.runtime.plan_tick', return_value=plan):
                return dispatch_tick(lambda: data, config, self.ledger, Client(), now='2026-11-11T09:00:00+08:00', dry_run=False)
        daily = {'kind': 'daily_confirmation', 'reason': '请确认', 'required_fields': ['work_location']}
        issue = {'kind': 'blocked', 'table': 'wbs', 'record_id': 'A', 'reason': '新阻塞', 'required_fields': []}
        tick([daily]); tick([daily]); tick([daily, issue])
        tick([dict(daily, kind='daily_confirmation_followup')])
        self.assertEqual(3, len(deliveries))
        self.assertNotIn('work_location', deliveries[0][2])
        # A reply/closure removes the items in the freshly recomputed plan.
        tick([])
        self.assertEqual(3, len(deliveries))


if __name__ == '__main__':
    unittest.main()
