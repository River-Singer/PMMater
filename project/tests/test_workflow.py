"""Readiness and dependency evidence, independent of resource classification."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pm_core.actions import Ledger
from pm_core.feishu import FeishuError
from pm_core.runtime import plan_tick, dispatch_tick
from pm_core.schema import TABLES
from pm_core.workflow import workflow_review, plan_workflow_followups


NOW = '2026-11-11T10:00:00+08:00'


def fixture():
    data = {name: [] for name in TABLES}
    data['projects'] = [{'id': 'P', 'name': '测试项目', 'calendar_id': 'CAL', 'pm_id': 'PM',
                         'baseline_start': '2026-11-01', 'baseline_finish': '2026-11-30',
                         'stage': 'executing'}]
    data['calendars'] = [{'id': 'CAL', 'timezone': 'Asia/Shanghai'}]
    data['calendar_years'] = [{'calendar_id': 'CAL', 'year': 2026, 'status': 'complete',
                               'source_url': 'https://example.invalid/calendar',
                               'verified_at': '2026-01-01T00:00:00+08:00', 'exception_hash': 'test'}]
    data['calendar_days'] = [{'calendar_id': 'CAL', 'date': day, 'is_workday': working,
                              'day_type': 'makeup_workday' if day.endswith('14') else 'default_workday'}
                             for day, working in [('2026-11-09', True), ('2026-11-11', True),
                                                  ('2026-11-12', True), ('2026-11-14', True), ('2026-11-15', False)]]
    data['people'] = [{'id': p, 'name': p, 'active': True, 'person_type': 'employee',
                       'feishu_user_id': 'ou_' + p, 'starts_on': '2026-01-01', 'ends_on': '2026-12-31'}
                      for p in ('OWNER', 'PM', 'QA')]
    data['wbs'] = [{'id': 'ROOT', 'title': '项目根', 'project_id': 'P', 'scope': 'project', 'node_kind': 'project', 'status': 'ready'},
                   {'id': 'GROUP', 'title': '一级交付', 'project_id': 'P', 'scope': 'project', 'node_kind': 'deliverable',
                    'parent_id': 'ROOT', 'status': 'ready', 'baseline_finish': '2026-11-20'},
                   {'id': 'LEAF', 'title': '最小任务', 'project_id': 'P', 'scope': 'project', 'node_kind': 'task',
                    'parent_id': 'GROUP', 'status': 'ready', 'priority': 'P2', 'description': '明确工作范围', 'acceptance_criteria': '通过可复核场景',
                    'owner_id': 'OWNER', 'acceptor_id': 'QA', 'forecast_start': '2026-11-10', 'forecast_finish': '2026-11-20',
                    'baseline_finish': '2026-11-20', 'progress_note': '已核对计划，开始处理',
                    'progress_confirmed_by_id': 'OWNER', 'progress_confirmed_at': '2026-11-11T09:00:00+08:00'}]
    return data


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.row = self.data['wbs'][-1]
        self.resources = patch('pm_core.constraints.resource_constraints', return_value=[]).start()
        self.addCleanup(patch.stopall)
        self.config = {'project_id': 'P', 'timezone': 'Asia/Shanghai', 'mode': 'feishu', 'enabled': True,
                       'chat_id': 'chat', 'allowed_recipients': ['OWNER', 'PM', 'QA']}

    def review(self): return workflow_review(self.data, now=NOW)

    def routine(self, now=NOW): return plan_workflow_followups(self.data, now=now, config=self.config)

    def test_incomplete_intake_remains_visible_and_routes_to_pm(self):
        for field in ('owner_id', 'forecast_start', 'forecast_finish', 'description'):
            self.row.pop(field)
        self.row['status'] = 'intake'
        original = copy.deepcopy(self.data)
        result = self.review()
        self.assertEqual(1, result['summary']['total'])
        self.assertEqual('missing_data', result['tasks'][0]['readiness'])
        self.assertIn('forecast_start', result['tasks'][0]['missing_fields'])
        item = self.routine()['items'][0]
        self.assertEqual('PM', item['recipient_id'])
        self.assertIn('最小任务 → 一级交付 → 项目根 → 测试项目', item['reason'])
        self.assertEqual(original, self.data)

    def test_feedback_requires_current_owner_content_and_nonfuture_time(self):
        self.assertEqual('ready', self.review()['tasks'][0]['readiness'])
        for changes in ({'progress_confirmed_by_id': 'PM'}, {'progress_note': '  '},
                        {'progress_confirmed_at': '2026-11-11T10:01:00+08:00'},
                        {'progress_confirmed_at': '2026-11-11T09:00:00'}):
            original = copy.deepcopy(self.row); self.row.update(changes)
            self.assertEqual('awaiting_feedback', self.review()['tasks'][0]['readiness'])
            self.row.clear(); self.row.update(original)

    def test_future_work_does_not_need_daily_progress_and_closed_stops(self):
        self.row.update(forecast_start='2026-11-18', progress_confirmed_at='2026-11-01T09:00:00+08:00')
        self.assertFalse(self.review()['tasks'][0]['progress_stale'])
        self.assertEqual([], self.routine()['items'])
        self.row.update(status='done', description=None, progress_note=None, actual_start='2026-11-10',
                        actual_finish='2026-11-11', completion_evidence='已核验结果')
        self.assertEqual('closed', self.review()['tasks'][0]['readiness'])
        self.assertEqual([], self.routine()['items'])

    def test_stale_uses_progress_confirmation_not_arbitrary_edit(self):
        self.row.update(progress_confirmed_at='2026-11-07T09:00:00+08:00', updated_at=NOW)
        self.assertTrue(self.review()['tasks'][0]['progress_stale'])
        self.row['progress_confirmed_at'] = '2026-11-11T09:00:00+08:00'
        self.assertFalse(self.review()['tasks'][0]['progress_stale'])

    def test_bad_date_order_and_inactive_or_unreachable_owner(self):
        self.row['forecast_start'] = '2026-11-22'
        self.data['people'][0]['active'] = False
        row = self.review()['tasks'][0]
        self.assertIn('plan_date_order', row['missing_fields'])
        self.assertIn('owner_active', row['missing_fields'])
        self.assertEqual('PM', self.routine()['items'][0]['recipient_id'])
        self.data['people'][1]['feishu_user_id'] = None
        self.assertEqual([], self.routine()['items'])
        self.assertEqual('pending_route', self.routine()['pending'][0]['status'])

    def test_parent_counts_unique_leaves_and_detects_false_completion(self):
        self.data['wbs'][1]['status'] = 'done'
        self.data['wbs'].append(dict(self.row, id='CANCEL', status='cancelled'))
        report = self.review()
        self.assertEqual(1, report['summary']['total'])
        group = next(p for p in report['parents'] if p['id'] == 'GROUP')
        self.assertEqual(['LEAF'], group['leaf_ids'])
        self.assertTrue(group['inconsistent_completion'])
        self.assertEqual('done', self.data['wbs'][1]['status'])
        self.assertIn('workflow_parent_conflict', [r['kind'] for r in self.routine()['items']])

    def test_dependency_chain_and_downstream_parent_deliverable_are_retained(self):
        self.data['wbs'] += [dict(self.data['wbs'][1], id='NEXT-G', parent_id='ROOT'),
                             dict(self.row, id='NEXT', parent_id='NEXT-G'),
                             dict(self.row, id='FINAL', parent_id='ROOT')]
        self.data['dependencies'] = [{'id': 'D1', 'predecessor_id': 'GROUP', 'successor_id': 'NEXT'},
                                     {'id': 'D2', 'predecessor_id': 'LEAF', 'successor_id': 'NEXT'},
                                     {'id': 'D3', 'predecessor_id': 'NEXT', 'successor_id': 'FINAL'}]
        self.data['deliverables'] = [{'id': 'DEL', 'title': '交付验证', 'project_id': 'P', 'work_item_id': 'NEXT-G', 'acceptance_status': 'pending'}]
        impact = self.review()['tasks'][0]['impact']
        self.assertEqual(['GROUP', 'ROOT'], [n['id'] for n in impact['ancestors']])
        self.assertEqual(['NEXT', 'FINAL'], [n['id'] for n in impact['successors']])
        self.assertEqual(['DEL'], [d['id'] for d in impact['deliverables']])
        self.assertEqual(['NEXT', 'FINAL'], impact['dependency_leaf_ids'])
        self.assertEqual([], impact['date_evidence'])
        self.assertIn('影响评估', impact['project_assessment'])
        self.assertNotIn('存在逐级交付风险', impact['project_assessment'])

    def test_normal_and_completed_task_paths_are_not_active_risk_claims(self):
        for status in ('ready', 'done'):
            self.row['status'] = status
            task = self.review()['tasks'][0]
            self.assertEqual([], task['signals'])
            self.assertEqual(['GROUP', 'ROOT'], [r['id'] for r in task['impact']['ancestors']])
            self.assertNotIn('存在逐级交付风险', task['impact']['project_assessment'])
        self.row.update(status='blocked', blocker_reason='依赖待落实')
        self.assertIn('存在逐级交付风险', self.review()['tasks'][0]['impact']['project_assessment'])

    def test_forecast_finish_is_not_missing_info_submission_deadline(self):
        self.row['description'] = None
        item = self.routine()['items'][0]
        self.assertIsNone(item['deadline'])
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(str(Path(tmp) / 'ledger.sqlite3'))
            result = dispatch_tick(lambda: self.data, self.config, ledger, None, now=NOW)
            text = result['deliveries'][0]['text']
            self.assertIn('任务当前预计完成日：2026-11-20', text)
            self.assertIn('DDL：待确认', text)
            self.assertNotIn('截止：2026-11-20', text)
            ledger.close()

    def test_date_overrun_is_specific_evidence_not_critical_path(self):
        self.row['forecast_finish'] = '2026-12-02'
        impact = self.review()['tasks'][0]['impact']
        self.assertEqual({'GROUP', 'P'}, {e['target_id'] for e in impact['date_evidence']})
        self.assertIn('子项预测', impact['project_assessment'])
        self.assertFalse(any('days' in e for e in impact['date_evidence']))

    def test_cycles_fail_explicitly(self):
        self.data['wbs'][0]['parent_id'] = 'LEAF'
        with self.assertRaisesRegex(ValueError, 'parent cycle'): self.review()
        self.data['wbs'][0].pop('parent_id')
        self.data['dependencies'] = [{'predecessor_id': 'LEAF', 'successor_id': 'GROUP'},
                                     {'predecessor_id': 'GROUP', 'successor_id': 'LEAF'}]
        with self.assertRaisesRegex(ValueError, 'Dependency cycle'): self.review()

    def test_resource_parent_link_expands_leaves_unmapped_stays_unmapped(self):
        c = {'id': 'r1', 'kind': 'register', 'source_table': 'registers', 'source_id': 'R',
             'title': '物资待落实', 'work_item_ids': ['GROUP'], 'owner_id': 'PM', 'reason': '未到货', 'next_action': '确认到货时间'}
        self.resources.return_value = [c, dict(c, id='r2', work_item_ids=[])]
        report = self.review()
        self.assertEqual(['LEAF'], report['constraints'][0]['impact']['affected_leaf_ids'])
        self.assertEqual([], report['constraints'][1]['impact']['task_ids'])
        self.assertIn('待定位', report['constraints'][1]['impact']['project_assessment'])

    def test_operations_constraints_are_not_project_risks(self):
        c = {'id': 'r1', 'kind': 'register', 'source_table': 'registers', 'source_id': 'R',
             'scope': 'operations', 'operation_ids': ['OPS'], 'title': '运维物资待落实',
             'work_item_ids': [], 'owner_id': 'PM', 'reason': '未到货', 'next_action': '确认到货时间'}
        self.resources.return_value = [c]
        report = self.review()
        self.assertEqual([], report['constraints'])
        self.assertEqual(0, report['summary']['resource_constraints'])
        self.assertEqual(1, report['summary']['operations_resource_constraints'])
        self.assertEqual([], report['operations_constraints'][0]['impact']['affected_leaf_ids'])
        self.assertIn('仅关联运维', next(i for i in self.routine()['items'] if i['kind'] == 'workflow_resource')['reason'])

    def test_ten_am_calendar_timezone_and_catchup(self):
        self.row['description'] = None
        self.assertFalse(self.routine('2026-11-11T09:59:00+08:00')['items'])
        self.assertTrue(self.routine('2026-11-11T02:00:00+00:00')['items'])
        self.assertTrue(self.routine('2026-11-11T16:00:00+08:00')['items'])
        self.assertTrue(self.routine('2026-11-14T10:00:00+08:00')['items'])
        for now in ('2026-11-15T10:00:00+08:00', '2026-12-01T10:00:00+08:00', '2026-11-11T18:00:00+08:00'):
            self.assertFalse(self.routine(now)['items'])
        self.data['calendar_years'][0]['status'] = 'pending'
        self.assertFalse(self.routine()['items'])
        self.data['calendar_years'][0]['status'] = 'complete'
        for stage in ('paused', 'closed'):
            self.data['projects'][0]['stage'] = stage
            self.assertFalse(self.routine()['items'])

    def test_old_stale_reminder_is_not_repeated_after_routine(self):
        self.row['progress_confirmed_at'] = None
        result = plan_tick(self.data, self.config, now='2026-11-11T10:30:00+08:00')
        kinds = [x['kind'] for rows in result['recipients'].values() for x in rows if x.get('record_id') == 'LEAF']
        self.assertIn('workflow_task', kinds)
        self.assertNotIn('stale_progress', kinds)
        self.assertEqual([], result['attendance']['eligible'])  # employee task is still checked

    def test_dispatch_dedupes_unknown_results_and_stops_after_completion(self):
        self.row['description'] = None
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(str(Path(tmp) / 'ledger.sqlite3'))
            self.addCleanup(ledger.close)
            class Client:
                count = 0
                def mention(self, *args, **kwargs):
                    self.count += 1
                    raise FeishuError('unknown', uncertain=True)
            client = Client()
            def send(): return dispatch_tick(lambda: self.data, self.config, ledger, client, now=NOW, dry_run=False)
            self.assertEqual('unknown', send()['deliveries'][0]['status'])
            send(); self.assertEqual(1, client.count)
            self.row.update(description='已完善', actual_start='2026-11-10', status='in_progress')
            self.assertFalse(send()['plan']['recipients'])
            ledger.close()

    def test_distinct_resource_signals_share_source_without_dedupe_collision(self):
        self.row.update(actual_start='2026-11-10', status='in_progress')
        c = {'id': 'registers:R:gap', 'kind': 'register', 'source_table': 'registers', 'source_id': 'R',
             'title': '资源', 'work_item_ids': ['LEAF'], 'owner_id': 'OWNER', 'reason': '未到位', 'next_action': 'PM协调'}
        self.resources.return_value = [c, dict(c, id='registers:R:decision', kind='decision')]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(str(Path(tmp) / 'ledger.sqlite3'))
            class Client:
                def mention(self, *args, **kwargs): return {'message_id': 'M'}
            result = dispatch_tick(lambda: self.data, self.config, ledger, Client(), now=NOW, dry_run=False)
            self.assertEqual('PM', result['deliveries'][0]['person_id'])
            self.assertEqual(2, ledger.conn.execute("SELECT count(*) FROM executions WHERE key LIKE 'reminder:%'").fetchone()[0])
            ledger.close()


if __name__ == '__main__': unittest.main()
