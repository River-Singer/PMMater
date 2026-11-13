"""Conditional task questions, observable response contracts and closed-data audits."""
import copy
import unittest
from unittest.mock import patch

from pm_core.group_actions import action_batches, project_group_actions
from pm_core.runtime import plan_tick
from pm_core.workflow import workflow_review
from test_workflow import fixture, NOW


class TaskScenarioTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.row = self.data['wbs'][-1]
        self.row.update(status='in_progress', actual_start='2026-11-10')
        self.config = {'project_id': 'P', 'timezone': 'Asia/Shanghai', 'mode': 'feishu',
                       'enabled': True, 'chat_id': 'chat', 'allowed_recipients': ['OWNER', 'PM', 'QA']}
        self.resources = patch('pm_core.constraints.resource_constraints', return_value=[]).start()
        self.addCleanup(patch.stopall)

    def review(self, now=NOW):
        return workflow_review(self.data, now=now, config=self.config)

    def scenarios(self, now=NOW):
        return {s['code']: s for s in self.review(now)['tasks'][0]['scenarios']}

    def projected(self, now=NOW):
        return project_group_actions(self.data, plan_tick(self.data, self.config, now=now), self.config, now=now)

    def actions(self, now=NOW):
        return [a for rows in self.projected(now)['recipients'].values() for a in rows]

    def test_future_actual_dates_are_legitimately_blank_and_inspection_is_read_only(self):
        self.row.update(status='ready', forecast_start='2026-11-18', actual_start=None,
                        progress_confirmed_at='2026-11-01T09:00:00+08:00')
        original = copy.deepcopy(self.data)
        self.assertFalse(self.scenarios())
        self.assertFalse(self.actions())
        self.assertEqual(original, self.data)

    def test_start_ambiguity_uses_current_plan_and_stops_when_facts_match(self):
        self.row.update(status='ready', actual_start=None, baseline_start='2026-11-01', forecast_start='2026-11-12')
        self.assertNotIn('start_unconfirmed', self.scenarios())
        self.row['forecast_start'] = '2026-11-11'
        scenario = self.scenarios()['start_unconfirmed']
        self.assertEqual('needs_confirmation', scenario['assessment'])
        self.assertIn('已开工但漏填，还是尚未开始', scenario['questions'][0]['prompt'])
        self.assertIn('是否影响交付', scenario['questions'][1]['prompt'])
        self.row['actual_start'] = '2026-11-11'
        self.assertIn('started_state_conflict', self.scenarios())
        self.row['status'] = 'in_progress'
        self.assertNotIn('started_state_conflict', self.scenarios())
        self.assertNotIn('start_unconfirmed', self.scenarios())

    def test_missing_finish_asks_estimate_split_resources_and_proposed_date(self):
        self.row['forecast_finish'] = None
        scenario = self.scenarios()['plan_finish_missing']
        questions = ' '.join(q['prompt'] for q in scenario['questions'])
        for phrase in ('难点', '剩余工作量', '不确定性', '拆分可验收子任务', '增加资源', '更多时间', '预计完成日', '依赖'):
            self.assertIn(phrase, questions)
        self.row['forecast_finish'] = '2026-11-20'
        self.assertNotIn('plan_finish_missing', self.scenarios())

    def test_finish_today_is_not_overdue_and_new_forecast_preserves_baseline_check(self):
        self.row.update(baseline_finish='2026-11-11', forecast_finish='2026-11-11')
        today = self.scenarios()
        self.assertIn('finish_today', today)
        self.assertEqual('due_today', today['finish_today']['assessment'])
        self.assertNotIn('finish_overdue', today)
        self.assertNotEqual('overdue', self.review()['tasks'][0]['delay_state'])
        self.row.update(baseline_finish='2026-11-10', forecast_finish='2026-11-20')
        overdue = self.scenarios()['finish_overdue']
        self.assertEqual('2026-11-10', overdue['facts']['baseline_finish'])
        self.assertEqual('overdue', self.review()['tasks'][0]['delay_state'])
        self.row['baseline_finish'] = '2026-11-20'
        self.assertNotIn('finish_overdue', self.scenarios())
        self.assertNotIn('finish_today', self.scenarios())

    def test_completed_missing_facts_remain_a_data_audit_not_an_overdue_task(self):
        self.row.update(status='done', baseline_finish='2026-11-01', actual_start=None, actual_finish=None)
        report = self.review()
        task = report['tasks'][0]
        self.assertEqual(['completion_record_gap'], list(self.scenarios()))
        self.assertEqual('information_gap', task['scenarios'][0]['assessment'])
        self.assertEqual([], task['signals'])
        self.assertEqual(0, report['summary']['overdue'])
        actions = self.actions()
        self.assertEqual(1, len(actions))
        self.assertEqual('completion_audit', actions[0]['obligation'])
        self.assertIn('workflow_task_audit', actions[0]['source_kinds'])
        self.assertIn('actual_finish', actions[0]['required_fields'])
        self.row.update(actual_start='2026-10-29', actual_finish='2026-11-01', completion_evidence='已核验成果')
        self.assertFalse(self.actions())

    def test_finished_state_and_acceptance_questions_use_acceptor(self):
        self.row.update(status='in_review', actual_finish='2026-11-11', completion_evidence='测试报告')
        self.assertIn('finished_state_conflict', self.scenarios())
        actions = self.actions()
        acceptance = [a for a in actions if a['obligation'] == 'acceptance']
        self.assertEqual(1, len(acceptance))
        self.assertEqual('QA', acceptance[0]['recipient_id'])
        self.row['actual_finish'] = None
        self.assertIn('acceptance_pending', self.scenarios())

    def test_priority_and_owner_feedback_are_distinct_and_current_owner_matters(self):
        self.row.update(priority=None, progress_confirmed_by_id='PM')
        scenarios = self.scenarios()
        self.assertIn('priority_missing', scenarios)
        self.assertIn('owner_response_missing', scenarios)
        self.assertEqual(['priority'], scenarios['priority_missing']['required_fields'])
        self.row.update(priority='P0', progress_confirmed_by_id='OWNER')
        self.assertIn('priority_reason_missing', self.scenarios())
        self.assertNotIn('owner_response_missing', self.scenarios())
        self.row.update(priority_reason='上线窗口受阻', progress_confirmed_at='2026-11-07T09:00:00+08:00')
        self.assertIn('progress_stale', self.scenarios())

    def test_inactive_acceptor_is_not_lost_when_other_scenarios_exist(self):
        self.data['people'][-1]['active'] = False
        self.row['priority'] = None
        scenario = self.scenarios()['definition_missing']
        self.assertIn('acceptor_active', scenario['required_fields'])
        self.assertTrue(any('acceptor_active' in a['required_fields'] for a in self.actions()))

    def test_missing_owner_routes_coordination_question_to_pm(self):
        self.row['owner_id'] = None
        self.assertIn('owner_unavailable', self.scenarios())
        action = self.actions()[0]
        self.assertEqual('PM', action['recipient_id'])
        self.assertEqual('coordination', action['type'])
        self.assertEqual(['routing:assignment'], action['response_fields'])
        self.assertIn('责任人', action['questions'][0]['prompt'])

    def test_blocked_and_delay_questions_have_recovery_paths_and_distinct_execution_deadline(self):
        self.row.update(status='blocked', blocker_reason='接口未交付', coordinator_id='QA',
                        next_action='确认接口就绪', next_due_at='2026-11-11T15:00:00+08:00',
                        delay_risk='at_risk', remediation_owner_id='OWNER', remediation_action='并行准备测试',
                        remediation_expected='2026-11-12')
        scenarios = self.scenarios()
        for code in ('blocked_recovery', 'delay_recovery_gap'):
            questions = ' '.join(q['prompt'] for q in scenarios[code]['questions'])
            self.assertIn('继续', questions)
            self.assertIn('等待', questions)
            self.assertIn('恢复条件', questions)
        self.assertIn('上级任务', scenarios['delay_recovery_gap']['questions'][0]['prompt'])
        actions = self.actions()
        next_action = [a for a in actions if a['obligation'] == 'next_action']
        self.assertEqual(1, len(next_action))  # Legacy blocked/next-action and routine merge.
        self.assertEqual('QA', next_action[0]['recipient_id'])
        self.assertEqual('2026-11-11T15:00:00+08:00', next_action[0]['deadline'])
        self.assertEqual('2026-11-11T17:00:00+08:00', next_action[0]['response_deadline'])
        self.assertIn('涉及 2 级上级', next_action[0]['impact'])
        remedy = next(a for a in actions if a['obligation'] == 'remediation')
        self.assertEqual('2026-11-12', remedy['deadline'])

    def test_waiting_requires_current_checkpoint_and_remedy_needs_evidence(self):
        self.row.update(waiting_on='外部确认', next_action='核对反馈', next_due_at='2026-11-11T12:00:00+08:00')
        self.assertNotIn('waiting_checkpoint', self.scenarios())
        self.row['next_due_at'] = '2026-11-11T09:00:00+08:00'
        self.assertIn('waiting_checkpoint', self.scenarios())
        self.row.update(remediation_expected='2026-11-11', remediation_actual='2026-11-11', remediation_result=None)
        self.assertEqual('due_today', self.scenarios()['remediation_result_pending']['assessment'])
        self.row['remediation_result'] = '结果已核验'
        self.assertNotIn('remediation_result_pending', self.scenarios())

    def test_reply_cutoff_is_project_local_stable_and_not_an_execution_ddl(self):
        self.row['description'] = None
        early = self.actions('2026-11-11T08:59:00+00:00')[0]
        self.assertEqual('2026-11-11T17:00:00+08:00', early['response_deadline'])
        self.assertIsNone(early['deadline'])
        self.assertEqual([q['id'] for q in early['questions']], early['response_fields'])
        for instant in ('2026-11-11T17:00:00+08:00', '2026-11-11T17:59:00+08:00'):
            later = self.actions(instant)[0]
            self.assertIsNone(later['response_deadline'])
            self.assertEqual(early['id'], later['id'])
            self.assertEqual(early['scenario_ids'], later['scenario_ids'])
        self.config['response_cutoff'] = '16:30'
        self.assertEqual('2026-11-11T16:30:00+08:00', self.actions()[0]['response_deadline'])

    def test_all_questions_visible_with_minimum_message_bound_and_long_source(self):
        self.row.update(status='blocked', title='标题' * 1000, description=None, acceptance_criteria=None,
                        actual_start=None, forecast_start=None, forecast_finish=None, priority=None,
                        progress_confirmed_at=None, remediation_action='措施' * 1000,
                        next_action='行动' * 1000, next_due_at='2026-11-11T09:00:00+08:00',
                        remediation_expected='2026-11-10', remediation_owner_id='QA')
        actions = self.actions()
        batches = action_batches(actions, dict(self.config, group_max_chars=1200))
        self.assertTrue(all(len(b['text']) <= 1200 for b in batches))
        for batch in batches:
            for action in batch['actions']:
                for question in action['questions']:
                    self.assertIn(question['prompt'], batch['text'])
        self.assertEqual(len(actions), len({a['id'] for a in actions}))

    def test_scenarios_apply_only_to_project_leaves(self):
        self.row.update(scope='operations')
        self.assertFalse(self.review()['tasks'])
        self.assertFalse(self.actions())


if __name__ == '__main__':
    unittest.main()
