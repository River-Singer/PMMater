"""Scoped host observations and fair, bounded end-of-day escalation."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pm_core.actions import Ledger
from pm_core.feishu import FeishuError
from pm_core.followup_evidence import (record_sent, sent_questions, import_evidence,
    response_state, escalation_review, dispatch_escalation)
from pm_core.integration_cli import run
from pm_core.runtime import dispatch_tick, load_config
from test_workflow import fixture, NOW


END = '2026-11-11T17:00:00+08:00'


class FollowupEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.sqlite3'
        self.ledger = Ledger(str(self.path))
        self.addCleanup(lambda: self.ledger.close())
        self.config = {'project_id': 'P', 'timezone': 'Asia/Shanghai', 'mode': 'feishu',
                       'chat_id': 'chat', 'enabled': True, 'allowed_recipients': ['OWNER', 'QA', 'PM'],
                       'reply_evidence_sources': ['verified-host']}
        self.data = fixture()
        self.plan = {'date': '2026-11-11', 'recipients': {}, 'workflow': None, 'pending': []}
        self.action = {'id': 'wbs:LEAF:followup:OWNER', 'occurrence_key': 'reminder:P:2026-11-11:OWNER:wbs:LEAF:followup:OWNER',
                       'recipient_id': 'OWNER', 'source': {'id': 'LEAF', 'table': 'wbs', 'title': '任务A'},
                       'action': '确认开始及补救安排', 'impact': '任务A → 一级交付 → 项目，日期影响待确认',
                       'questions': [{'id': 'start:actual', 'prompt': '是否实际开始？'}, {'id': 'start:remedy', 'prompt': '补救安排是什么？'}],
                       'response_fields': ['start:actual', 'start:remedy'], 'response_deadline': END}
        self.actions = {'recipients': {'OWNER': [self.action]}, 'pending': []}

    def sent(self, action=None, at=NOW, message='M'):
        record_sent(self.ledger, self.config, 'OWNER', [action or self.action], message, now=at)
        return sent_questions(self.ledger, 'P')[-1]

    def envelope(self, replies=None, coverage=None):
        return {'source_id': 'verified-host', 'project_id': 'P', 'chat_id': 'chat',
                'replies': replies or [], 'coverage': coverage or []}

    def reply(self, **values):
        return dict({'occurrence_key': self.action['occurrence_key'], 'prompt_message_id': 'M',
                     'message_id': 'R', 'sender_id': 'OWNER', 'at': '2026-11-11T11:00:00+08:00',
                     'quote': '已经开始，下午提交验证', 'kind': 'answer', 'answers': {'start:actual': '已开始'}}, **values)

    def coverage(self, **values):
        return dict({'id': 'C', 'from': NOW, 'through': END, 'captured_at': END, 'complete': True,
                     'evidence': 'host: paginated chat capture C'}, **values)

    def ingest(self, payload, **values):
        return import_evidence(self.ledger, self.config, payload, now=END, apply=True, **values)

    def state(self, **values):
        return response_state(self.ledger, self.config, sent_questions(self.ledger, 'P')[0], now=END, **values)

    def test_no_coverage_is_unverified_not_silence_and_table_edits_are_not_replies(self):
        self.sent()
        self.data['wbs'][-1].update(progress_confirmed_at=END, updated_at=END)
        self.assertEqual('reply_unverified', self.state()['state'])
        self.ingest(self.envelope(coverage=[self.coverage()]))
        self.assertEqual('unanswered', self.state()['state'])

    def test_partial_coverage_and_incomplete_pagination_cannot_prove_silence(self):
        self.sent()
        self.ingest(self.envelope(coverage=[self.coverage(through='2026-11-11T16:00:00+08:00'),
                     self.coverage(id='C2', **{'from': '2026-11-11T16:01:00+08:00'}),
                     self.coverage(id='C3', complete=False)]))
        self.assertEqual('reply_unverified', self.state()['state'])
        self.ingest(self.envelope(coverage=[self.coverage(id='C4', **{'from': '2026-11-11T16:00:00+08:00', 'through': '2026-11-11T16:01:00+08:00'})]))
        self.assertEqual('unanswered', self.state()['state'])

    def test_answers_are_per_question_and_ack_is_not_an_answer(self):
        self.sent()
        self.ingest(self.envelope([self.reply(kind='ack', quote='收到 👍', answers={})]))
        self.assertEqual('acknowledged', self.state()['state'])
        self.ingest(self.envelope([self.reply(message_id='R2')]))
        self.assertEqual('partial', self.state()['state'])
        self.assertEqual(['start:remedy'], self.state()['missing'])
        self.ingest(self.envelope([self.reply(message_id='R3', answers={'start:remedy': '下午提交验证'})]))
        self.assertEqual('answered', self.state()['state'])
        # Answering never updates business dates or proves actual task closure.
        review = escalation_review(self.data, self.plan, self.actions, self.config, self.ledger, now=END)
        self.assertEqual('answered', review['items'][0]['response']['state'])
        self.assertNotIn('actual_start', self.data['wbs'][-1])

    def test_ack_mislabeled_as_answer_is_rejected(self):
        self.sent()
        for text in ('收到', 'OK!', '👍👍', '收到，谢谢。'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.ingest(self.envelope([self.reply(answers={'start:actual': text})]))
        with self.assertRaises(ValueError):
            self.ingest(self.envelope([self.reply(quote='收到', answers={'start:actual': '已开始'})]))

    def test_source_scope_sender_question_and_times_are_validated_atomically(self):
        self.sent()
        bad = [self.reply(sender_id='QA'), self.reply(prompt_message_id='other'),
               self.reply(occurrence_key='missing'), self.reply(at='2026-11-11T09:00:00+08:00'),
               self.reply(at='2026-11-11T17:01:00+08:00'), self.reply(at='2026-11-11T11:00:00'),
               self.reply(answers={'unasked': '已确认'})]
        for bad_reply in bad:
            with self.subTest(bad=bad_reply), self.assertRaises(ValueError):
                self.ingest(self.envelope([self.reply(message_id='valid'), bad_reply]))
        for field, value in [('source_id', 'untrusted'), ('chat_id', 'elsewhere'), ('project_id', 'OTHER')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.ingest(dict(self.envelope([self.reply()]), **{field: value}))
        self.assertEqual('reply_unverified', self.state()['state'])

    def test_preview_idempotency_conflict_and_restart(self):
        self.sent()
        payload = self.envelope([self.reply()], [self.coverage()])
        result = import_evidence(self.ledger, self.config, payload, now=END)
        self.assertEqual('preview', result['status'])
        self.assertEqual(0, self.ledger.conn.execute('SELECT COUNT(*) FROM followup_evidence').fetchone()[0])
        self.ingest(payload); self.ingest(payload)
        self.assertEqual(2, self.ledger.conn.execute('SELECT COUNT(*) FROM followup_evidence').fetchone()[0])
        with self.assertRaises(ValueError): self.ingest(self.envelope([self.reply(quote='改写历史')]))
        self.ledger.close(); self.ledger = Ledger(str(self.path))
        self.assertEqual('partial', self.state()['state'])
        self.config['reply_evidence_sources'] = []
        self.assertEqual('reply_unverified', self.state()['state'])

    def test_multi_action_message_only_covers_explicit_occurrences(self):
        self.sent()
        other = dict(self.action, id='OTHER', occurrence_key='OTHER')
        self.sent(other)
        self.ingest(self.envelope([self.reply(answers={q: '已落实并有验证记录' for q in self.action['response_fields']})]))
        receipts = {r['action']['id']: r for r in sent_questions(self.ledger, 'P')}
        self.assertEqual('answered', response_state(self.ledger, self.config, receipts[self.action['id']], now=END)['state'])
        self.assertEqual('reply_unverified', response_state(self.ledger, self.config, receipts['OTHER'], now=END)['state'])

    def test_grace_period_no_deadline_and_after_deadline_never_immediate_nonresponse(self):
        for due, at, expected in [(END, '2026-11-11T16:59:00+08:00', 'too_recent'),
                                  (None, END, 'not_due'), (END, END, 'not_due')]:
            with self.subTest(due=due, at=at):
                action = dict(self.action, occurrence_key=str((due, at)), response_deadline=due)
                self.sent(action, at=at)
                receipt = next(r for r in sent_questions(self.ledger, 'P') if r['occurrence_key'] == action['occurrence_key'])
                self.assertEqual(expected, response_state(self.ledger, self.config, receipt, now=END)['state'])

    def test_removed_questions_stop_followup_and_late_reply_is_not_silence(self):
        self.sent()
        self.assertEqual('resolved', self.state(active_fields=[])['state'])
        self.actions['recipients'] = {}
        result = escalation_review(self.data, self.plan, self.actions, self.config, self.ledger, now=END)
        self.assertFalse(result['items']); self.assertTrue(result['resolved'])
        later = '2026-11-11T17:05:00+08:00'
        import_evidence(self.ledger, self.config, self.envelope([self.reply(at=later)]), now=later, apply=True)
        result = response_state(self.ledger, self.config, sent_questions(self.ledger, 'P')[0], now=later)
        self.assertEqual('partial', result['state']); self.assertTrue(result['late_reply'])

    def test_send_time_is_immutable_and_not_ledger_updated_time(self):
        receipt = self.sent()
        self.ledger.save(self.action['occurrence_key'], 'fp', 'unknown', {})
        self.ledger.reconcile(self.action['occurrence_key'], outcome='applied', evidence='host proof')
        self.assertEqual(NOW, sent_questions(self.ledger, 'P')[0]['sent_at'])
        with self.assertRaises(ValueError): self.sent(at=END)
        self.assertEqual(receipt, sent_questions(self.ledger, 'P')[0])

    def test_changed_questions_are_new_discovery_not_morning_nonresponse(self):
        self.sent()
        self.action['response_fields'] = ['finish:forecast']
        self.action['questions'] = [{'id': 'finish:forecast', 'prompt': '新的完成时间安排？'}]
        review = escalation_review(self.data, self.plan, self.actions, self.config, self.ledger, now=END)
        self.assertEqual('resolved', review['resolved'][0]['state'])
        self.assertEqual('notification', review['items'][0]['kind'])
        self.assertIn('新发现问题', review['items'][0]['label'])
        self.assertEqual(0, review['counts']['unanswered'])

    def test_failed_unknown_and_unsent_separate_from_nonresponse(self):
        actions = []
        for i, status in enumerate(('failed', 'unknown', 'running', 'succeeded', 'not_sent')):
            action = dict(self.action, id=str(i)); actions.append(action)
            if status != 'not_sent': self.ledger.save('reminder:P:2026-11-11:OWNER:' + str(i), 'fp', status, {})
        result = escalation_review(self.data, self.plan, {'recipients': {'OWNER': actions}}, self.config, self.ledger, now=END)
        self.assertEqual(5, len(result['items']))
        self.assertTrue(all(r['kind'] == 'notification' for r in result['items']))
        self.assertEqual(0, result['counts']['unanswered'])

    def test_current_task_and_operations_resource_risks_survive_without_replies(self):
        self.actions['recipients'] = {}
        self.plan['workflow'] = {'tasks': [{'id': 'LEAF', 'title': '任务A', 'owner_id': 'OWNER', 'delay_state': 'risk',
            'impact': {'project_assessment': '影响总计划待复核'}}], 'operations_constraints': [
            {'id': 'ops:C', 'source_table': 'operations', 'source_id': 'O', 'title': '配置变更资源',
             'scope': 'operations', 'owner_id': 'OWNER', 'reason': '窗口未落实', 'impact': {}}]}
        review = escalation_review(self.data, self.plan, self.actions, self.config, self.ledger, now=END)
        self.assertEqual({'当前延期/阻碍风险', '运维资源风险'}, {r['label'] for r in review['items']})

    def test_answered_and_resolved_maintenance_cannot_hide_current_risk_facts(self):
        from pm_core.workflow import workflow_review
        self.data['wbs'][-1].update(baseline_finish='2026-11-10', forecast_finish='2026-12-01',
            delay_reason='接口交付推迟', remediation_action='补充模拟接口', remediation_owner_id='QA',
            remediation_expected='2026-11-15', waiting_on='等待供应方确认', next_action='先推进本地验证',
            next_due_at='2026-11-12T12:00:00+08:00', schedule_audit='PRIVATE_AUDIT_MARKER')
        task = workflow_review(self.data, now=END, config=self.config)['tasks'][0]
        task['schedule_audit'] = 'PRIVATE_AUDIT_MARKER'
        self.plan['workflow'] = {'tasks': [task], 'schedule_audit': 'PRIVATE_AUDIT_MARKER'}
        self.sent()
        self.ingest(self.envelope([self.reply(answers={f: '已核实并提供记录' for f in self.action['response_fields']})]))
        for active in (True, False):
            with self.subTest(active=active):
                actions = self.actions if active else {'recipients': {}, 'pending': []}
                review = escalation_review(self.data, self.plan, actions, self.config, self.ledger, now=END)
                risk = next(r for r in review['items'] if r['id'] == 'risk:LEAF')
                self.assertEqual('接口交付推迟', risk['risk_facts']['delay_reason'])
                self.assertEqual('2026-12-01', risk['risk_facts']['forecast_finish'])
                self.assertEqual('2026-11-15', risk['risk_facts']['remediation_expected'])
                self.assertEqual(['GROUP', 'ROOT'], [p['id'] for p in risk['risk_impact']['ancestors']])
                self.assertTrue(any(e['target_id'] == 'P' for e in risk['risk_impact']['date_evidence']))
                self.assertNotIn('PRIVATE_AUDIT_MARKER', json.dumps(review))
                result = dispatch_escalation(self.data, self.plan, actions, self.config, self.ledger, None, now=END)
                text = '\n'.join(d['text'] for d in result['deliveries'])
                for phrase in ('接口交付推迟', '2026-11-10', '2026-12-01', '2026-11-15', '一级交付', '测试项目', '先推进本地验证'):
                    self.assertIn(phrase, text)
                self.assertNotIn('PRIVATE_AUDIT_MARKER', text)

    def test_each_resource_constraint_survives_same_source_notification(self):
        from pm_core.workflow import workflow_review
        impact = workflow_review(self.data, now=END, config=self.config)['tasks'][0]['impact']
        constraints = [dict(id='same-source:' + kind, kind=kind, source_table='wbs', source_id='LEAF',
            title=kind, reason='各自尚未落实', owner_id='OWNER', work_item_ids=['LEAF'], impact=impact,
            evidence={'status': 'pending'}, next_action='核实条件', schedule_audit='PRIVATE_AUDIT_MARKER')
            for kind in ('interface', 'material')]
        self.plan['workflow'] = {'constraints': constraints}
        review = escalation_review(self.data, self.plan, self.actions, self.config, self.ledger, now=END)
        risks = [r for r in review['items'] if r['kind'] == 'risk']
        self.assertEqual({'same-source:interface', 'same-source:material'}, {r['constraint_id'] for r in risks})
        self.assertTrue(all('LEAF' in r['linked_task_facts'] for r in risks))
        self.assertNotIn('PRIVATE_AUDIT_MARKER', json.dumps(review))

    def test_recipient_reassignment_stops_old_followup_even_if_action_id_is_reused(self):
        self.sent()
        self.ingest(self.envelope(coverage=[self.coverage()]))
        reassigned = dict(self.action, recipient_id='QA')
        review = escalation_review(self.data, self.plan, {'recipients': {'QA': [reassigned]}}, self.config, self.ledger, now=END)
        self.assertEqual(0, review['counts']['unanswered'])
        self.assertEqual('no_longer_active', review['resolved'][0]['state'])
        self.assertEqual('QA', review['items'][0]['owner'])
        self.assertEqual('notification', review['items'][0]['kind'])

    def test_detailed_risk_digest_stays_inside_minimum_message_limit(self):
        from pm_core.workflow import workflow_review
        self.config['group_max_chars'] = 1200
        self.data['wbs'][-1].update(title='长任务名称' * 40, baseline_finish='2026-11-10', forecast_finish='2026-12-01',
            delay_reason='当前事实' * 100, remediation_action='补救措施' * 100, remediation_owner_id='QA',
            remediation_expected='2026-11-15', waiting_on='等待条件' * 100, next_action='下一步' * 100)
        for person in self.data['people']: person['name'] = '成员姓名' * 40
        self.plan['workflow'] = {'tasks': workflow_review(self.data, now=END, config=self.config)['tasks']}
        result = self.dispatch(None)
        self.assertTrue(all(len(d['text']) <= 1200 for d in result['deliveries']))

    def test_resource_branch_union_is_scope_not_an_invented_path(self):
        from pm_core.workflow import workflow_review
        self.data['wbs'][-1].update(title='分支A任务', forecast_finish='2026-12-01')
        self.data['wbs'][1]['title'] = '分支A交付'
        self.data['wbs'].append(dict(self.data['wbs'][1], id='GROUP2', title='分支B交付'))
        self.data['wbs'].append(dict(self.data['wbs'][2], id='LEAF2', title='分支B任务', parent_id='GROUP2'))
        constraint = {'id': 'interfaces:I:interface', 'source_table': 'interfaces', 'source_id': 'I',
                      'title': '共享接口', 'owner_id': 'OWNER', 'work_item_ids': ['LEAF', 'LEAF2'],
                      'scope': 'project', 'reason': '接口尚未就绪', 'next_action': '落实各消费节点'}
        with patch('pm_core.constraints.resource_constraints', return_value=[constraint]):
            projection = workflow_review(self.data, now=END, config=self.config)
        self.plan['workflow'] = {'constraints': projection['constraints']}
        self.actions['recipients'] = {}
        result = self.dispatch(None)
        text = '\n'.join(d['text'] for d in result['deliveries'])
        self.assertIn('关联任务：分支A任务、分支B任务', text)
        self.assertIn('涉及上级：分支A交付、项目根、分支B交付', text)
        self.assertIn('项目：测试项目', text)
        self.assertNotIn(' → ', text)
        self.assertIn('原计划 2026-11-20，子项预计 2026-12-01', text)
        self.assertNotIn('None', text)

    def test_project_workday_stage_quiet_and_window_boundaries(self):
        for change, now in [({}, NOW), ({}, '2026-11-11T18:00:00+08:00'),
                            ({'stage': 'paused'}, END), ({'stage': 'closed'}, END),
                            ({'reminder_end': '2026-11-10'}, END)]:
            data = copy.deepcopy(self.data); data['projects'][0].update(change)
            self.assertEqual('outside_review_window', escalation_review(data, self.plan, self.actions, self.config, self.ledger, now=now)['status'])
        self.data['calendar_years'][0]['status'] = 'pending'
        self.assertEqual('outside_review_window', escalation_review(self.data, self.plan, self.actions, self.config, self.ledger, now=END)['status'])

    def client(self, responses=None):
        calls = []; values = iter(responses or [{'message_id': 'sent'}] * 20)
        class Client:
            def mention(self, *args, **kwargs):
                calls.append((args, kwargs)); value = next(values)
                if isinstance(value, Exception): raise value
                return value
        return Client(), calls

    def dispatch(self, client, **values):
        return dispatch_escalation(self.data, self.plan, self.actions, self.config, self.ledger, client, now=END, **values)

    def test_digest_preview_does_not_freeze_and_target_defaults_pm(self):
        client, calls = self.client()
        result = self.dispatch(client)
        self.assertEqual('preview', result['deliveries'][0]['status'])
        self.assertEqual('PM', result['deliveries'][0]['recipient_id']); self.assertFalse(calls)
        self.assertFalse(self.ledger.conn.execute("SELECT 1 FROM sqlite_master WHERE name='followup_digests'").fetchone())
        self.config['escalation_recipient_id'] = 'QA'
        self.dispatch(client, dry_run=False)
        self.assertEqual('ou_QA', calls[0][0][1])

    def test_empty_first_review_does_not_freeze_future_meaningful_review(self):
        client, calls = self.client()
        self.actions['recipients'] = {}
        self.assertFalse(self.dispatch(client, dry_run=False)['deliveries'])
        self.actions['recipients'] = {'OWNER': [self.action]}
        self.assertEqual('succeeded', self.dispatch(client, dry_run=False)['deliveries'][0]['status'])
        self.assertEqual(1, len(calls))

    def test_bounded_chunks_restart_retry_and_unknown_protection(self):
        self.config['group_max_items'] = 1
        self.actions['recipients']['OWNER'].append(dict(self.action, id='B', source={'title': '任务B', 'table': 'wbs', 'id': 'B'}))
        client, calls = self.client([{'message_id': 'A'}, FeishuError('failure'), {'message_id': 'B'}])
        result = self.dispatch(client, dry_run=False)
        self.assertEqual(['succeeded', 'failed'], [d['status'] for d in result['deliveries']])
        self.ledger.close(); self.ledger = Ledger(str(self.path))
        # Source reordering/content changes cannot resend successful daily segments.
        self.actions['recipients']['OWNER'].reverse()
        result = self.dispatch(client, dry_run=False)
        self.assertEqual(3, len(calls)); self.assertEqual('succeeded', result['deliveries'][1]['status'])
        self.assertTrue(result['deliveries'][0]['duplicate'])
        self.assertEqual(calls[1][1]['request_id'], calls[2][1]['request_id'])
        self.assertTrue(all(len(c[0][2]) <= 3500 for c in calls))

    def test_unknown_digest_cannot_retry_and_recipient_scope_change_stops(self):
        client, calls = self.client([{}])
        self.assertEqual('unknown', self.dispatch(client, dry_run=False)['deliveries'][0]['status'])
        self.dispatch(client, dry_run=False); self.assertEqual(1, len(calls))
        self.config['escalation_recipient_id'] = 'QA'
        self.assertEqual('scope_changed_pending', self.dispatch(client, dry_run=False)['deliveries'][0]['status'])

    def test_disabled_excel_unreachable_and_unscoped_do_not_send(self):
        client, calls = self.client()
        for config, status in [({'enabled': False}, 'disabled'), ({'mode': 'excel'}, 'pending_identity'),
                               ({'allowed_recipients': []}, 'outside_recipient_scope'),
                               ({'escalation_recipient_id': 'MISSING'}, 'pending_route')]:
            with self.subTest(config=config):
                result = dispatch_escalation(self.data, self.plan, self.actions, dict(self.config, **config), self.ledger, client, now=END, dry_run=False)
                self.assertEqual(status, result['deliveries'][0]['status'])
        self.assertFalse(calls)

    def test_real_tick_records_only_successful_sent_questions(self):
        item = {'kind': 'workflow_task', 'table': 'wbs', 'record_id': 'LEAF', 'recipient_id': 'OWNER',
                'title': '任务', 'reason': '请维护', 'required_fields': ['actual_start']}
        self.plan['recipients'] = {'OWNER': [item]}
        client, calls = self.client([{'message_id': 'M'}])
        with patch('pm_core.runtime.plan_tick', return_value=copy.deepcopy(self.plan)):
            result = dispatch_tick(lambda: self.data, self.config, self.ledger, client, now=NOW, dry_run=False)
        self.assertEqual('succeeded', result['deliveries'][0]['status'])
        receipts = sent_questions(self.ledger, 'P'); self.assertEqual(1, len(receipts))
        self.assertEqual(NOW, receipts[0]['sent_at']); self.assertEqual('M', receipts[0]['message_id'])
        self.assertTrue(receipts[0]['action']['response_fields'])
        self.assertEqual('outside_review_window', result['escalation']['review']['status'])

    def test_cli_import_preview_apply_and_live_clock_boundary(self):
        self.sent()
        config_path = Path(self.tmp.name) / 'config.json'; evidence_path = Path(self.tmp.name) / 'evidence.json'
        config_path.write_text(json.dumps(dict(self.config, mode='excel')), encoding='utf-8')
        evidence_path.write_text(json.dumps(self.envelope([self.reply()])), encoding='utf-8')
        args = SimpleNamespace(command='followup-evidence-import', config=str(config_path), file=str(evidence_path),
                               ledger=str(self.path), now=END, apply=False)
        with patch('pm_core.integration_cli.FeishuClient', side_effect=AssertionError('No chat client required')):
            self.assertEqual('preview', run(args)['status'])
            self.assertEqual('reply_unverified', self.state()['state'])
            args.apply = True; self.assertEqual('imported', run(args)['status'])
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        with self.assertRaises(ValueError): run(args)
        for invalid in ({'min_response_minutes': True}, {'min_response_minutes': -1}, {'reply_evidence_sources': 'host'}, {'response_cutoff': 'bad'}):
            config_path.write_text(json.dumps(dict(self.config, **invalid)), encoding='utf-8')
            with self.assertRaises(ValueError): load_config(config_path)


if __name__ == '__main__': unittest.main()
