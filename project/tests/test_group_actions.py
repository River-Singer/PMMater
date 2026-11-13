"""Actual recipient, obligation, deadline and partial-send regressions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pm_core.actions import Ledger
from pm_core.feishu import FeishuClient, FeishuError
from pm_core.group_actions import project_group_actions, action_batches, message_limits
from pm_core.runtime import dispatch_tick, plan_tick
from test_workflow import fixture, NOW


class GroupActionTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.config = {'project_id': 'P', 'timezone': 'Asia/Shanghai', 'mode': 'feishu',
                       'chat_id': 'chat', 'enabled': True, 'allowed_recipients': ['OWNER', 'QA', 'PM']}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(str(Path(self.tmp.name) / 'ledger.sqlite3'))
        self.addCleanup(self.ledger.close)
        self.plan = {'date': '2026-11-11', 'recipients': {}, 'workflow': None}

    def item(self, kind, table='registers', rid='R', recipient='OWNER', **extra):
        return dict(kind=kind, table=table, record_id=rid, recipient_id=recipient,
                    title='待办' + rid, reason='需要处理', required_fields=[], **extra)

    def project(self, items):
        self.plan['recipients'] = {'OWNER': items}
        return project_group_actions(self.data, self.plan, self.config, now=NOW)

    def send(self, client=None, dry_run=False):
        with patch('pm_core.runtime.plan_tick', return_value=copy.deepcopy(self.plan)):
            return dispatch_tick(lambda: self.data, self.config, self.ledger, client, now=NOW, dry_run=dry_run)

    def test_explicit_decision_maker_receives_request_not_register_owner(self):
        self.data['registers'] = [{'id': 'R', 'kind': 'decision', 'owner_id': 'OWNER', 'decision_by_id': 'QA',
                                   'deadline_at': '2026-11-11T12:00:00+08:00', 'next_action': '选择上线方案'}]
        raw = self.item('decision_due')
        actions = self.project([raw])['recipients']['QA']
        self.assertEqual('decision', actions[0]['type'])
        self.assertEqual('2026-11-11T12:00:00+08:00', actions[0]['deadline'])
        calls = []
        class Client:
            def mention(self, *args, **kwargs):
                calls.append(args); return {'message_id': 'M'}
        result = self.send(Client())
        self.assertEqual('succeeded', result['deliveries'][0]['status'])
        self.assertEqual('ou_QA', calls[0][1])
        self.assertIn('【决策】', calls[0][2])

    def test_missing_or_inactive_decision_maker_goes_to_pm_to_assign_not_decide(self):
        for decision_by in (None, 'MISSING'):
            self.data['registers'] = [{'id': 'R', 'kind': 'decision', 'owner_id': 'OWNER', 'decision_by_id': decision_by,
                                       'deadline_at': '2026-11-11T12:00:00+08:00'}]
            a = self.project([self.item('decision_due')])['recipients']['PM'][0]
            self.assertEqual('coordination', a['type'])
            self.assertIsNone(a['deadline'])
            self.assertIn('不授予决策权', a['action'])
            self.assertEqual('原事项处理期限', a['date_reference'][0])
        self.data['people'][1]['active'] = False
        self.assertTrue(self.project([self.item('decision_due')])['pending'])

    def test_resource_execution_is_employee_owner_human_facts_stay_pm(self):
        self.data['interfaces'] = [{'id': 'I', 'owner_id': 'QA', 'expected_ready': '2026-11-13'}]
        self.data['registers'] = [{'id': 'R', 'kind': 'issue', 'owner_id': 'OWNER', 'deadline_at': '2026-11-12T12:00:00+08:00'}]
        constraints = []
        for table, rid, kind, owner in [('interfaces', 'I', 'interface', 'QA'), ('registers', 'R', 'register', 'OWNER'), ('resource_weeks', 'RW', 'capacity', 'PM')]:
            constraints.append({'id': table + ':' + rid + ':' + kind, 'kind': kind, 'source_table': table,
                'source_id': rid, 'title': '资源', 'owner_id': owner, 'work_item_ids': ['LEAF'],
                'reason': '未落实', 'next_action': '确认并落实资源', 'required_fields': [],
                'deadline': '2026-11-12T12:00:00+08:00' if kind == 'register' else None})
        with patch('pm_core.constraints.resource_constraints', return_value=constraints):
            plan = plan_tick(self.data, self.config, now=NOW)
        actions = project_group_actions(self.data, plan, self.config, now=NOW)['recipients']
        self.assertEqual('execution', actions['QA'][0]['type'])
        self.assertIsNone(actions['QA'][0]['deadline'])
        self.assertEqual(('接口预计就绪日', '2026-11-13'), actions['QA'][0]['date_reference'])
        self.assertEqual('execution', next(a for a in actions['OWNER'] if a['source']['id'] == 'R')['type'])
        self.assertEqual('coordination', actions['PM'][0]['type'])
        self.assertFalse(plan['attendance']['eligible'])  # All three are employees.

    def test_old_and_routine_same_decision_obligation_merge(self):
        self.data['registers'] = [{'id': 'R', 'kind': 'decision', 'decision_by_id': 'OWNER', 'deadline_at': '2026-11-12T12:00:00+08:00'}]
        c = {'id': 'registers:R:decision', 'kind': 'decision', 'next_action': '请给出方案结论',
             'deadline': '2026-11-12T12:00:00+08:00'}
        a = self.project([self.item('decision_due'), self.item('workflow_resource', constraint=c)])['recipients']['OWNER']
        self.assertEqual(1, len(a))
        self.assertEqual(['decision_due', 'workflow_resource'], a[0]['source_kinds'])

    def test_required_field_labels_follow_source_table_with_composite_fallback(self):
        raw = self.item('other', table='interfaces', rid='I')
        raw['required_fields'] = ['notes', 'weekly_commitments.outcome']
        actions = self.project([raw])['recipients']['OWNER']
        self.assertIn('提醒/限制', actions[0]['evidence_required'])
        self.assertIn('本周可验证成果', actions[0]['evidence_required'])
        self.assertNotIn('容量依据/变化', actions[0]['evidence_required'])
        self.assertNotIn('容量依据/变化', action_batches(actions, self.config)[0]['text'])

    def test_task_forecast_and_baseline_never_become_response_ddl(self):
        self.data['wbs'][-1].update(description=None, baseline_finish='2026-11-10', forecast_finish='2026-11-20')
        # Use an explicit missing field, then verify both independently normalized obligations.
        raw = self.item('workflow_task', 'wbs', 'LEAF'); raw['required_fields'] = ['description']
        actions = self.project([raw, self.item('overdue', 'wbs', 'LEAF', deadline='2026-11-10')])['recipients']['OWNER']
        self.assertEqual(2, len(actions))
        self.assertTrue(all(a['deadline'] is None for a in actions))
        text = action_batches(actions, self.config)[0]['text']
        self.assertIn('任务当前预计完成日：2026-11-20', text)
        self.assertNotIn('DDL：2026-11-10', text)

    def test_next_action_dedupes_but_remediation_retains_own_deadline(self):
        self.data['wbs'][-1].update(status='blocked', coordinator_id='QA', next_action='完成接口联调',
            next_due_at='2026-11-11T15:00:00+08:00', remediation_owner_id='OWNER',
            remediation_expected='2026-11-12', remediation_action='提交补救验证')
        raw = self.item('workflow_task', 'wbs', 'LEAF'); raw['required_fields'] = ['blocker_reason', 'next_action']
        actions = self.project([raw, self.item('blocked', 'wbs', 'LEAF', 'QA'), self.item('next_action_due', 'wbs', 'LEAF', 'QA'),
                                self.item('remediation_due', 'wbs', 'LEAF')])['recipients']
        self.assertEqual(1, len(actions['QA']))
        self.assertEqual('2026-11-11T15:00:00+08:00', actions['QA'][0]['deadline'])
        self.assertEqual('2026-11-12', actions['OWNER'][0]['deadline'])

    def test_future_remediation_is_assigned_now_and_stops_with_evidence(self):
        self.data['wbs'][-1].update(delay_risk='at_risk', remediation_owner_id='QA',
            remediation_action='补充兼容验证', remediation_expected='2026-11-20')
        with patch('pm_core.constraints.resource_constraints', return_value=[]):
            plan = plan_tick(self.data, self.config, now=NOW)
        actions = project_group_actions(self.data, plan, self.config, now=NOW)['recipients']
        remedy = [a for a in actions['QA'] if a['obligation'] == 'remediation'][0]
        self.assertEqual('execution', remedy['type'])
        self.assertEqual('2026-11-20', remedy['deadline'])
        self.assertIn('未批准', remedy['action'])
        self.data['wbs'][-1].update(remediation_actual='2026-11-11', remediation_result='验证记录已存档')
        with patch('pm_core.constraints.resource_constraints', return_value=[]):
            plan = plan_tick(self.data, self.config, now=NOW)
        actions = project_group_actions(self.data, plan, self.config, now=NOW)['recipients']
        self.assertFalse(any(a['obligation'] == 'remediation' for rows in actions.values() for a in rows))

    def test_approved_named_decision_and_closed_sources_stop(self):
        self.data['registers'] = [{'id': 'R', 'kind': 'decision', 'decision_by_id': 'OWNER', 'status': 'approved',
                                  'decision': '按方案A执行', 'decided_at': '2026-11-11T09:00:00+08:00'}]
        self.assertFalse(self.project([self.item('decision_due')])['recipients'])
        self.data['registers'][0]['decided_at'] = '2026-11-11T18:00:00+08:00'
        self.assertTrue(self.project([self.item('decision_due')])['recipients'])
        self.data['registers'][0]['status'] = 'closed'
        self.assertFalse(self.project([self.item('decision_due')])['recipients'])

    def test_approved_decision_without_named_decider_remains_pm_coordination(self):
        self.data['registers'] = [{'id': 'R', 'project_id': 'P', 'kind': 'decision',
            'owner_id': 'OWNER', 'status': 'approved', 'decision': '按方案A执行',
            'decided_at': '2026-11-11T09:00:00+08:00', 'work_item_id': 'LEAF'}]
        plan = plan_tick(self.data, self.config, now=NOW)
        actions = project_group_actions(self.data, plan, self.config, now=NOW)['recipients']
        decision = next(a for a in actions['PM'] if a['obligation'] == 'decision')
        self.assertEqual('coordination', decision['type'])
        self.assertIn('不授予决策权', decision['action'])

    def test_completed_parent_conflict_survives_closed_item_filter(self):
        self.data['wbs'][1].update(status='done', owner_id='OWNER')
        with patch('pm_core.constraints.resource_constraints', return_value=[]):
            plan = plan_tick(self.data, self.config, now=NOW)
        actions = project_group_actions(self.data, plan, self.config, now=NOW)['recipients']
        conflict = next(a for a in actions['OWNER'] if a['obligation'] == 'workflow_parent_conflict')
        self.assertIn('有效子任务未完成', conflict['action'])

    def test_excel_can_preview_without_feishu_but_cannot_send(self):
        self.config['mode'] = 'excel'
        for person in self.data['people']: person.pop('feishu_user_id')
        self.plan['recipients'] = {'OWNER': [self.item('stale_progress', 'wbs', 'LEAF')]}
        preview = self.send(dry_run=True)
        self.assertEqual('preview', preview['deliveries'][0]['status'])
        self.assertIn('【补填】', preview['deliveries'][0]['text'])
        self.assertEqual('pending_identity', self.send()['deliveries'][0]['status'])

    def test_excel_real_tick_keeps_missing_contact_in_offline_action_list(self):
        self.config['mode'] = 'excel'
        for person in self.data['people']: person.pop('feishu_user_id')
        self.data['wbs'][-1]['description'] = None
        with patch('pm_core.constraints.resource_constraints', return_value=[]):
            result = dispatch_tick(lambda: self.data, self.config, self.ledger, None, now=NOW)
        actions = result['plan']['group_actions']['recipients']['OWNER']
        self.assertIn('owner_contact', actions[0]['required_fields'])
        self.assertEqual('preview', result['deliveries'][0]['status'])

    def test_item_and_character_bounds_keep_full_source_in_projection(self):
        rows = [self.item('other', rid=str(i)) for i in range(8)]
        for r in rows:
            r.update(title='长标题' * 300, reason='长内容' * 1000)
        self.data['registers'] = [{'id': str(i), 'source_url': 'https://example.invalid/' + 'x' * 2000} for i in range(8)]
        actions = self.project(rows)['recipients']['OWNER']
        batches = action_batches(actions, dict(self.config, group_max_items=2, group_max_chars=1200))
        self.assertTrue(all(len(b['actions']) <= 2 and len(b['text']) <= 1200 for b in batches))
        self.assertEqual(8, sum(len(b['actions']) for b in batches))
        self.assertIn('x' * 2000, actions[0]['source']['url'])
        self.assertNotIn('https://example.invalid/', batches[0]['text'])
        for invalid in ({'group_max_items': 0}, {'group_max_chars': 100000}, {'group_max_chars': True}):
            with self.assertRaises(ValueError): message_limits(invalid)

    def test_batches_keep_success_unknown_failed_independent_and_preview_matches(self):
        self.config['group_max_items'] = 1
        self.plan['recipients'] = {'OWNER': [self.item('other', rid=str(i)) for i in range(3)]}
        preview = self.send(dry_run=True)
        calls = []
        class Client:
            def mention(self, *args, **kwargs):
                calls.append((args, kwargs))
                if len(calls) == 2: raise FeishuError('timeout', uncertain=True)
                if len(calls) == 3: raise FeishuError('rejected', uncertain=False)
                return {'message_id': 'M'}
        result = self.send(Client())
        self.assertEqual(['succeeded', 'unknown', 'failed'], [x['status'] for x in result['deliveries']])
        self.assertEqual([x['text'] for x in preview['deliveries']], [x[0][2] for x in calls])
        first_ids = [x[1]['request_id'] for x in calls]
        self.assertEqual(3, len(set(first_ids)))
        result = self.send(Client())
        self.assertEqual(4, len(calls))
        self.assertEqual(first_ids[2], calls[3][1]['request_id'])
        self.send(Client()); self.assertEqual(4, len(calls))
        states = self.ledger.conn.execute("SELECT status FROM executions WHERE key LIKE 'reminder-batch:%'").fetchall()
        self.assertEqual(['succeeded', 'succeeded', 'unknown'], sorted(r[0] for r in states))

    def test_recipient_scope_and_disabled_never_claim_batches(self):
        self.plan['recipients'] = {'OWNER': [self.item('other')]}
        self.config['allowed_recipients'] = []
        self.assertEqual('outside_recipient_scope', self.send()['deliveries'][0]['status'])
        self.config['enabled'] = False
        self.assertEqual('disabled', self.send()['deliveries'][0]['status'])
        self.assertEqual(0, self.ledger.conn.execute('SELECT count(*) FROM executions').fetchone()[0])

    def test_changed_batch_bound_cannot_bypass_occurrence_retry_limit(self):
        self.config.update(group_max_items=2, max_attempts=2)
        self.plan['recipients'] = {'OWNER': [self.item('other', rid=str(i)) for i in range(2)]}
        calls = []
        class Client:
            def mention(self, *args, **kwargs):
                calls.append(args)
                raise FeishuError('rejected', uncertain=False)
        self.send(Client())
        self.config['group_max_items'] = 1
        self.send(Client())
        self.config['group_max_items'] = 2
        self.send(Client())
        self.assertEqual(3, len(calls))  # One first batch, then two retries; no third attempts.
        occurrences = self.ledger.conn.execute("SELECT key FROM executions WHERE key LIKE 'reminder:%'").fetchall()
        self.assertTrue(all(self.ledger.get(r[0])['detail']['attempts'] == 2 for r in occurrences))

    def test_explicit_two_layer_reconciliation_recovers_only_verified_chunk(self):
        self.config['group_max_items'] = 1
        self.plan['recipients'] = {'OWNER': [self.item('other', rid=str(i)) for i in range(2)]}
        calls = []
        class Client:
            def mention(self, *args, **kwargs):
                calls.append((args, kwargs))
                if len(calls) <= 2: raise FeishuError('timeout', uncertain=True)
                return {'message_id': 'M'}
        first = self.send(Client())['deliveries']
        batch = first[0]['detail']
        self.ledger.reconcile(batch['batch_key'], outcome='not_applied', evidence='Verified this chunk absent in group history')
        self.send(Client())
        self.assertEqual(2, len(calls))  # Batch reconciliation alone cannot bypass unknown occurrences.
        for action in batch['items']:
            self.ledger.reconcile(action['occurrence_key'], outcome='not_applied', evidence='Same verified absent chunk')
        self.send(Client())
        self.assertEqual(3, len(calls))
        self.assertEqual(calls[0][1]['request_id'], calls[-1][1]['request_id'])
        untouched = first[1]['detail']
        self.assertEqual('unknown', self.ledger.get(untouched['batch_key'])['status'])
        self.assertTrue(all(self.ledger.get(a['occurrence_key'])['status'] == 'unknown' for a in untouched['items']))

    def test_raw_business_markup_stays_plain_text_and_only_one_real_at(self):
        self.plan['recipients'] = {'OWNER': [self.item('other')]}
        self.plan['recipients']['OWNER'][0]['reason'] = '<at user_id="all">所有人</at> @all'
        requests = []
        def transport(method, path, body, query, raw):
            requests.append(body)
            return {'code': 0, 'data': {'message_id': 'M'}}
        self.send(FeishuClient(transport=transport))
        payload = json.loads(requests[0]['content'])['zh_cn']['content'][0]
        self.assertEqual(['at', 'text'], [element['tag'] for element in payload])
        self.assertEqual('ou_OWNER', payload[0]['user_id'])
        self.assertIn('<at user_id="all">', payload[1]['text'])


if __name__ == '__main__': unittest.main()
