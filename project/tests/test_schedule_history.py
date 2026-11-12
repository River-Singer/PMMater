import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pm_core.actions import Ledger, validate_action
from pm_core.management_text import management_paragraphs, schedule_paragraphs
from pm_core.runtime import dispatch_schedule_reviews
from pm_core.schedule_history import ScheduleObserver


class ScheduleHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'observer.sqlite'
        self.observer = ScheduleObserver(self.path)
        self.addCleanup(self.observer.close)
        self.data = {'projects': [{'id': 'P', 'calendar_id': 'C', 'pm_id': 'PM', 'stage': 'executing'}],
                     'calendars': [{'id': 'C', 'timezone': 'Asia/Shanghai'}],
                     'people': [{'id': 'PM', 'feishu_user_id': 'ou_pm', 'active': True}],
                     'wbs': [{'id': 'W', 'project_id': 'P', 'title': '接口开发', 'status': 'in_progress',
                              'baseline_finish': '2026-11-05', 'forecast_finish': '2026-11-12'}]}

    def observe(self, now='2026-11-11T09:00:00Z', **kwargs):
        return self.observer.observe(self.data, source_id='feishu:fictional', project_id='P', now=now, **kwargs)

    def changed(self):
        self.observe('2026-11-10T09:00:00Z')
        self.data['wbs'][0]['baseline_finish'] = '2026-11-13'
        return self.observe()

    def test_initial_capture_not_violation_and_changed_dates_have_unknown_editor(self):
        first = self.observe('2026-11-10T09:00:00Z')
        self.assertEqual('unknown', first['coverage']['mode'])
        self.assertFalse(first['signals'])
        self.data['wbs'][0]['baseline_finish'] = '2026-11-13'
        result = self.observe()
        signal = result['signals'][0]
        self.assertEqual('high', signal['severity'])
        self.assertIsNone(signal['actor'])
        self.assertIsNone(signal['recorded_at'])
        self.assertEqual('2026-11-10T09:00:00+00:00', signal['evidence']['previous_observed_at'])
        self.assertEqual('2026-11-05', result['original_commitments'][0]['original_baseline_finish'])
        text = '\n'.join(schedule_paragraphs(result))
        self.assertIn('2026-11-05 → 2026-11-13', text)
        self.assertIn('实际变更时间：待确认', text)

    def test_restart_dedup_scope_and_backdate(self):
        first = self.changed()
        other = ScheduleObserver(self.path)
        try:
            again = other.observe(self.data, source_id='feishu:fictional', project_id='P', now='2026-11-12T09:00:00Z')
            self.assertEqual(first['signals'][0]['key'], again['signals'][0]['key'])
            self.assertEqual(2, again['coverage']['observations'])
            self.assertFalse(again['coverage']['captured_new_snapshot'])
            separate = other.observe(self.data, source_id='feishu:other', project_id='P', now='2026-11-12T09:00:00Z')
            self.assertFalse(separate['signals'])
            with self.assertRaisesRegex(ValueError, 'backdate'):
                self.observe('2026-11-09T09:00:00Z')
        finally:
            other.close()

    def test_intermediate_change_back_is_explicitly_unobservable(self):
        self.observe('2026-11-10T09:00:00Z')
        self.data['wbs'][0]['baseline_finish'] = '2026-11-13'
        self.data['wbs'][0]['baseline_finish'] = '2026-11-05'
        result = self.observe()
        self.assertFalse(result['signals'])
        self.assertTrue(any('改回' in note for note in result['coverage']['limitations']))

    def test_unchanged_read_still_narrows_next_change_interval(self):
        self.observe('2026-11-09T09:00:00Z')
        self.observe('2026-11-10T09:00:00Z')
        self.data['wbs'][0]['baseline_finish'] = '2026-11-13'
        audit = self.observe()
        self.assertEqual('2026-11-10T09:00:00+00:00', audit['signals'][0]['evidence']['previous_observed_at'])

    def test_pm_private_only_and_durable_occurrence(self):
        audit = self.changed()
        ledger = Ledger(Path(self.temp.name) / 'runtime.sqlite')
        self.addCleanup(ledger.close)
        config = {'project_id': 'P', 'timezone': 'Asia/Shanghai', 'enabled': True,
                  'chat_id': 'public-group', 'allowed_recipients': ['PM']}
        calls = []
        class Client:
            def mention(self, *args, **kwargs):
                calls.append(args)
                return {'message_id': 'msg'}
        with patch('pm_core.project_calendar.workday_status', return_value={'verified': True, 'is_workday': True}):
            result = dispatch_schedule_reviews(self.data, audit, config, ledger, Client(), now='2026-11-11T09:00:00Z', dry_run=False)
            self.assertEqual('pending_private_channel', result[0]['status'])
            self.assertFalse(calls)
            config.update(pm_private_chat_id='private-pm', pm_private_recipient_id='PM')
            for day in ('11', '12'):
                dispatch_schedule_reviews(self.data, audit, config, ledger, Client(), now=f'2026-11-{day}T09:00:00Z', dry_run=False)
            self.assertEqual(1, len(calls))
            self.assertEqual('private-pm', calls[0][0])
            self.assertIn('不能据此推断', calls[0][2])

    def test_exact_post_review_resolves_observation_but_retains_original_warning(self):
        self.changed()
        policy = {'decision_makers': ['PM'], 'reviewed_changes': [{'table': 'wbs', 'record_id': 'W',
            'changes': {'baseline_finish': {'before': '2026-11-05', 'after': '2026-11-13'}},
            'reviewed_by': 'PM', 'reviewed_at': '2026-11-11T09:15:00Z', 'evidence': 'decision:fictional', 'kind': 'correction'}]}
        audit = self.observe('2026-11-11T09:30:00Z', policy=policy)
        self.assertEqual(0, audit['alert_count'])
        self.assertEqual('high', audit['signals'][0]['original_classification']['severity'])
        text = '\n'.join(schedule_paragraphs(audit))
        self.assertIn('不等于事前批准', text)
        self.assertIn('2026-11-05', text)

    def test_weekly_retry_uses_frozen_audit_not_current_history(self):
        from datetime import datetime, timezone
        from pm_core.fixtures import operating_demo_data
        from pm_core.store import Store
        from pm_core.weekly import generate_weekly_report, retry_report
        db = Path(self.temp.name) / 'project.sqlite'
        now = datetime(2026, 11, 13, 9, tzinfo=timezone.utc)
        with Store(db) as store:
            store.apply_batch(operating_demo_data(), actor='fictional', now=now)
        frozen = self.changed()
        with patch('pm_core.weekly.utc_now', return_value=now), patch('pm_core.schedule_audit.audit_schedule_changes', return_value=frozen):
            report = generate_weekly_report(db, Path(self.temp.name) / 'reports', kind='friday', timezone_name='Asia/Shanghai')
        with Store(db) as store:
            row = store.conn.execute('SELECT schedule_audit_json FROM weekly_reports WHERE id=?', (report['id'],)).fetchone()
            self.assertEqual(frozen, json.loads(row[0]))
        with patch('pm_core.schedule_audit.audit_schedule_changes', side_effect=AssertionError('must not recompute history')):
            retry_report(db, Path(self.temp.name) / 'retry', report['id'])

    def test_document_body_is_business_language(self):
        from pm_core.fixtures import operating_demo_data
        from pm_core.operating import management_review
        data = operating_demo_data()
        review = management_review(data, now='2026-11-11T09:00:00Z', period='month', schedule_audit=self.changed())
        text = '\n'.join(management_paragraphs(data, review))
        for label in ('延期说明与补救措施', '行动负责人', '日期变更审议', '人员资源', '待确认', '未来 6 周'):
            self.assertIn(label, text)
        for raw in ('baseline_finish', 'remediation_action', '"counts"', "{'", 'net_available_hours'):
            self.assertNotIn(raw, text)

    def test_live_feishu_capture_cannot_be_backdated(self):
        from argparse import Namespace
        from pm_core.integration_cli import run
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps({'mode': 'feishu', 'project_id': 'P', 'timezone': 'Asia/Shanghai'}), encoding='utf-8')
        args = Namespace(command='feishu-pull', config=str(config), now='2020-01-01T00:00:00Z', db=None)
        with patch('pm_core.integration_cli.FeishuClient', side_effect=AssertionError('no network before validation')):
            with self.assertRaisesRegex(ValueError, 'cannot backdate'):
                run(args)

    def test_fictional_action_examples_validate_against_current_schema(self):
        from datetime import datetime, timezone
        from pm_core.fixtures import operating_demo_data
        from pm_core.store import Store
        from pm_core.actions import LocalBackend, execute_action
        examples = Path(__file__).resolve().parents[1] / 'references' / 'examples'
        config = json.loads((examples / 'excel-config.json').read_text(encoding='utf-8'))
        ledger = Ledger(Path(self.temp.name) / 'examples.sqlite')
        self.addCleanup(ledger.close)
        with Store(Path(self.temp.name) / 'example-data.sqlite') as store:
            store.apply_batch(operating_demo_data(), actor='fictional')
            for name in ('weekly-commitment.json', 'friday-decision.json'):
                for a in json.loads((examples / name).read_text(encoding='utf-8'))['actions']:
                    with patch('pm_core.store.utc_now', return_value=datetime(2026, 11, 13, 9, tzinfo=timezone.utc)):
                        result = execute_action(a, LocalBackend(store, 'fictional'), ledger, config['policy'])
                    self.assertEqual('preview', result['status'], result)
        booking = json.loads((examples / 'booking.json').read_text(encoding='utf-8'))['actions'][0]
        validate_action(booking, {'allowed_operations': ['book'], 'calendar_id': 'fictional-calendar-id', 'organizer_id': 'ou_fictional_pm', 'allowed_attendees': ['ou_fictional_pm', 'ou_fictional_vendor']})

    def test_decisive_acceptance_and_decision_need_authority_but_facts_do_not(self):
        policy = {'allowed_tables': ['deliverables', 'registers', 'wbs'], 'allowed_operations': ['update'], 'decision_makers': ['PM']}
        source = {'id': 'minute', 'url': 'https://example.invalid/minute', 'quote': '虚构确认事实', 'kind': 'confirmed_fact'}
        for table, values in [('deliverables', {'acceptance_status': 'accepted'}), ('deliverables', {'acceptance_status': 'rejected'}), ('deliverables', {'reviewed_at': '2026-11-11T09:00:00Z'}), ('registers', {'status': 'approved'}), ('registers', {'decision': '选择方案A'}), ('registers', {'decided_at': '2026-11-11T09:00:00Z'})]:
            a = {'id': 'a', 'operation': 'update', 'table': table, 'record_id': 'A', 'values': values, 'expected': {k: None for k in values}, 'source': source}
            with self.assertRaisesRegex(ValueError, 'decision authority'):
                validate_action(a, policy)
            validate_action(dict(a, approval={'by': 'PM', 'evidence': '真实具名决定'}), policy)
        for table, values in [('deliverables', {'acceptance_status': 'pending'}), ('wbs', {'actual_finish': '2026-11-11'})]:
            validate_action({'id': 'a', 'operation': 'update', 'table': table, 'record_id': 'A', 'values': values, 'expected': {k: None for k in values}, 'source': source}, policy)


if __name__ == '__main__':
    unittest.main()
