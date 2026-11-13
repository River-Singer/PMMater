"""Review regressions for conditional wording and actual delivery timestamps."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pm_core.actions import Ledger
from pm_core.followup_evidence import sent_questions, response_state
from pm_core.group_actions import action_batches, project_group_actions
from pm_core.integration_cli import run
from pm_core.runtime import dispatch_tick, load_config, plan_tick
from test_workflow import fixture, NOW


class DailyReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(str(Path(self.tmp.name) / 'runtime.sqlite3'))
        self.addCleanup(self.ledger.close)
        self.data = fixture()
        self.config = {'project_id': 'P', 'timezone': 'Asia/Shanghai', 'mode': 'feishu',
                       'chat_id': 'chat', 'enabled': True, 'allowed_recipients': ['OWNER', 'QA', 'PM']}

    def test_past_due_questions_ask_new_forecast_and_allow_true_blank_actuals(self):
        self.data['wbs'][-1].update(baseline_finish='2026-11-10', actual_start=None, actual_finish=None)
        plan = plan_tick(self.data, self.config, now=NOW)
        actions = project_group_actions(self.data, plan, self.config, now=NOW)
        actions = [a for rows in actions['recipients'].values() for a in rows]
        overdue = next(a for a in actions if a['obligation'] == 'delay_review')
        text = '\n'.join(b['text'] for b in action_batches([overdue], self.config))
        self.assertIn('新的预计完成日', text)
        self.assertNotIn('能否按期交付', text)
        self.assertIn('未开始/未完成请如实说明', text)

    def test_slow_send_uses_completion_clock_for_response_opportunity(self):
        item = {'kind': 'workflow_task', 'table': 'wbs', 'record_id': 'LEAF', 'recipient_id': 'OWNER',
                'title': '任务', 'reason': '请维护', 'required_fields': ['actual_start']}
        plan = {'date': '2026-11-11', 'recipients': {'OWNER': [item]}, 'workflow': None, 'pending': []}
        class Client:
            def mention(self, *args, **kwargs):
                return {'message_id': 'M'}
        with patch('pm_core.runtime.plan_tick', return_value=copy.deepcopy(plan)):
            dispatch_tick(lambda: self.data, self.config, self.ledger, Client(),
                          now='2026-11-11T16:00:00+08:00', dry_run=False,
                          delivery_clock=lambda: '2026-11-11T16:59:00+08:00')
        receipt = sent_questions(self.ledger, 'P')[0]
        self.assertEqual('2026-11-11T16:59:00+08:00', receipt['sent_at'])
        self.assertEqual('too_recent', response_state(self.ledger, self.config, receipt,
                         now='2026-11-11T17:00:00+08:00')['state'])

    def test_live_cli_supplies_real_delivery_clock(self):
        config_path = Path(self.tmp.name) / 'config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        args = SimpleNamespace(command='agent-tick', config=str(config_path), now=None, db=None,
                               ledger=str(Path(self.tmp.name) / 'live.sqlite3'), apply=True,
                               project_id=None, actor='test', out=None)
        with patch('pm_core.integration_cli.FeishuClient'), patch('pm_core.integration_cli.BitableAdapter'), \
                patch('pm_core.integration_cli.utc_now', return_value=NOW) as clock, \
                patch('pm_core.runtime.dispatch_tick', return_value={}) as dispatch:
            run(args)
        self.assertIs(clock, dispatch.call_args.kwargs['delivery_clock'])

    def test_cutoff_config_must_be_local_minute_precision(self):
        path = Path(self.tmp.name) / 'config.json'
        for key in ('response_cutoff', 'escalation_time'):
            for value in ('17:00+08:00', '17:00:30', ''):
                with self.subTest(key=key, value=value):
                    path.write_text(json.dumps(dict(self.config, **{key: value})), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        load_config(path)


if __name__ == '__main__':
    unittest.main()
