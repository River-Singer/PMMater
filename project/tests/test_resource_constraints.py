"""Resource evidence may expose delivery risk without inventing a schedule."""
import copy
import unittest

from pm_core.constraints import resource_constraints


NOW = '2026-11-11T10:00:00+08:00'


class ResourceConstraintTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            'projects': [{'id': 'P', 'calendar_id': 'C', 'pm_id': 'PM'}],
            'calendars': [{'id': 'C', 'timezone': 'Asia/Shanghai'}],
            'people': [{'id': 'EMP', 'name': '员工', 'person_type': 'employee'},
                       {'id': 'PM', 'name': '经理', 'person_type': 'employee'},
                       {'id': 'EXT', 'name': '供应商', 'person_type': 'vendor'}],
            'wbs': [{'id': 'T', 'project_id': 'P', 'scope': 'project', 'node_kind': 'task',
                     'title': '任务', 'status': 'in_progress', 'owner_id': 'EMP',
                     'forecast_start': '2026-11-09', 'forecast_finish': '2026-11-13'}],
        }

    def rows(self, kind):
        return [r for r in resource_constraints(self.data, now=NOW) if r['kind'] == kind]

    def leave(self, **changes):
        row = dict(id='L', project_id='P', person_id='EMP', start_date='2026-11-12',
                   end_date='2026-11-12', start_period='PM', end_period='PM',
                   status='confirmed', confirmed_at='2026-11-10T10:00:00+08:00', backup_id='EXT')
        row.update(changes)
        self.data['leave_plans'] = [row]
        return row

    def capacity(self, **changes):
        row = dict(id='R', project_id='P', person_id='EMP', week_start='2026-11-09',
                   net_available_hours=20, project_hours=24, operations_hours=2, buffer_hours=0,
                   confirmed_at='2026-11-10T10:00:00+08:00')
        row.update(changes)
        self.data['resource_weeks'] = [row]
        return row

    def test_employee_leave_overlaps_and_backup_is_not_proven_handover(self):
        self.leave()
        row = self.rows('leave')[0]
        self.assertEqual(row['work_item_ids'], ['T'])
        self.assertEqual(row['assessment'], 'risk')
        self.assertEqual(row['evidence']['start_period'], 'PM')
        self.assertEqual(row['owner_id'], 'PM')
        self.assertIn('不证明已承接', ''.join(row['limitations']))
        self.assertNotIn('delay_days', row)

    def test_proposed_future_confirmation_and_cancelled_leave(self):
        leave = self.leave(status='proposed')
        self.assertEqual(self.rows('leave')[0]['assessment'], 'pending_confirmation')
        leave.update(status='confirmed', confirmed_at='2026-11-12T10:00:00+08:00')
        self.assertEqual(self.rows('leave')[0]['assessment'], 'pending_confirmation')
        leave['status'] = 'cancelled'
        self.assertEqual(self.rows('leave'), [])

    def test_outside_window_is_unmapped_and_stale_schedule_is_unknown(self):
        self.leave(start_date='2026-11-20', end_date='2026-11-20')
        row = self.rows('leave')[0]
        self.assertEqual(row['work_item_ids'], [])
        self.assertEqual(row['assessment'], 'needs_mapping')
        self.data['wbs'][0]['forecast_finish'] = '2026-11-10'
        row = self.rows('leave')[0]
        self.assertEqual(row['evidence']['task_timing'], {'T': 'timing_unknown'})
        self.assertNotIn('计划日期重叠', row['reason'])

    def test_only_explicit_execution_participants_with_overlapping_windows(self):
        self.leave(person_id='EXT')
        self.data['participants'] = [dict(id='REL', work_item_id='T', person_id='EXT',
            role='stakeholder', starts_on='2026-11-01', ends_on='2026-11-30')]
        self.assertEqual(self.rows('leave')[0]['work_item_ids'], [])
        rel = self.data['participants'][0]
        rel['role'] = 'collaborator'
        self.assertEqual(self.rows('leave')[0]['work_item_ids'], ['T'])
        rel['starts_on'] = '2026-11-14'
        self.assertEqual(self.rows('leave')[0]['work_item_ids'], [])

    def test_capacity_overallocation_keeps_employee_facts_without_double_leave_deduction(self):
        self.capacity()
        self.leave()
        row = self.rows('capacity')[0]
        self.assertEqual(row['evidence']['net_available_hours'], 20)
        self.assertEqual(row['evidence']['capacity_status'], 'overallocated')
        self.assertEqual(row['evidence']['person_type'], 'employee')
        self.assertIn('不重复扣减', ''.join(row['limitations']))
        self.assertEqual(self.rows('capacity_unknown'), [])

    def test_missing_and_unconfirmed_capacity_is_not_full_availability(self):
        row = self.rows('capacity_unknown')[0]
        self.assertEqual(row['owner_id'], 'PM')
        self.assertFalse(row['evidence']['record_exists'])
        self.assertIsNone(row['deadline'])
        self.capacity(confirmed_at=None)
        self.assertEqual(self.rows('capacity')[0]['assessment'], 'pending_confirmation')
        self.capacity(confirmed_at='2026-11-12T10:00:00+08:00')
        self.assertEqual(self.rows('capacity')[0]['evidence']['capacity_status'], 'pending')

    def test_shared_remote_work_is_not_inferred_to_be_shortage(self):
        row = dict(id='D', project_id='P', person_id='EMP', date='2026-11-11', period='AM',
            work_location='remote', allocation_mode='shared', available_hours=3,
            project_hours=2, operations_hours=1, response_status='confirmed', confirmed_at=NOW)
        self.data['daily_confirmations'] = [row]
        self.assertEqual(self.rows('daily_availability'), [])
        row['available_hours'] = 0
        issue = self.rows('daily_availability')[0]
        self.assertEqual(issue['assessment'], 'risk')
        self.assertEqual(issue['evidence']['period'], 'AM')
        row['date'] = '2026-11-10'
        self.assertEqual(self.rows('daily_availability'), [])

    def test_interface_expected_ready_is_not_assumed_consumption_deadline(self):
        row = dict(id='I', project_id='P', name='接口', work_item_id='T', status='developing',
                   expected_ready='2026-11-12', owner_id='EXT')
        self.data['interfaces'] = [row]
        issue = self.rows('interface')[0]
        self.assertEqual(issue['work_item_ids'], ['T'])
        self.assertIsNone(issue['deadline'])
        self.assertEqual(issue['evidence']['expected_ready'], '2026-11-12')
        self.assertIn('开发任务本身', ''.join(issue['limitations']))
        row['status'] = 'ready'
        self.assertEqual(self.rows('interface'), [])

    def test_open_material_register_is_visible_without_keyword_blocking(self):
        row = dict(id='R', project_id='P', kind='issue', title='物资到货待确认', status='open',
                   owner_id='EXT', deadline_at='2026-11-13T12:00:00+08:00', impact='测试条件待落实')
        self.data['registers'] = [row]
        issue = self.rows('register')[0]
        self.assertEqual(issue['assessment'], 'needs_mapping')
        self.assertEqual(issue['work_item_ids'], [])
        self.assertNotIn('blocked', issue)
        row['work_item_id'] = 'T'
        self.assertEqual(self.rows('register')[0]['work_item_ids'], ['T'])
        row['status'] = 'closed'
        self.assertEqual(self.rows('register'), [])

    def test_decision_reverse_reference_and_complete_conclusion_stop_risk(self):
        self.data['wbs'][0]['decision_id'] = 'R'
        row = dict(id='R', project_id='P', kind='decision', title='决定', status='approved',
                   decision_by_id='PM')
        self.data['registers'] = [row]
        self.assertEqual(self.rows('decision')[0]['work_item_ids'], ['T'])
        row.update(decision='采用方案A', decided_at=NOW)
        self.assertEqual(self.rows('decision'), [])
        self.data['interfaces'] = [dict(id='R', project_id='P', status='developing', name='接口')]
        self.assertEqual(self.rows('interface')[0]['work_item_ids'], [])

    def test_closed_tasks_and_foreign_projects_do_not_receive_resource_paths(self):
        self.leave()
        self.data['wbs'][0]['status'] = 'done'
        self.assertEqual(self.rows('leave')[0]['work_item_ids'], [])
        self.assertEqual(self.rows('capacity_unknown'), [])
        self.data['leave_plans'][0]['project_id'] = 'OTHER'
        self.assertEqual(self.rows('leave'), [])

    def test_operations_only_constraints_do_not_become_project_mapping_risks(self):
        self.data['operations'] = [dict(id='OP', project_id='P', status='ready')]
        self.data['interfaces'] = [dict(id='I', project_id='P', status='developing',
                                       name='运维接口', operation_id='OP')]
        self.data['registers'] = [dict(id='R', project_id='P', status='open', kind='issue',
                                      title='运维条件', operation_id='OP')]
        for kind in ('interface', 'register'):
            row = self.rows(kind)[0]
            self.assertEqual(row['scope'], 'operations')
            self.assertEqual(row['work_item_ids'], [])
            self.assertEqual(row['operation_ids'], ['OP'])
            self.assertNotEqual(row['assessment'], 'needs_mapping')
        self.data['operations'][0]['related_wbs_id'] = 'T'
        self.assertEqual(self.rows('interface')[0]['scope'], 'project')
        self.assertEqual(self.rows('interface')[0]['work_item_ids'], ['T'])

    def test_person_windows_are_not_fabricated_promised_deadlines(self):
        self.leave()
        self.capacity()
        self.data['daily_confirmations'] = [dict(id='D', project_id='P', person_id='EMP',
                                                 date='2026-11-11', response_status='pending')]
        for kind in ('leave', 'capacity', 'daily_availability'):
            row = self.rows(kind)[0]
            self.assertIsNone(row['deadline'])
            self.assertTrue(row['deadline_basis'])

    def test_pure_projection_has_stable_source_identity(self):
        self.leave()
        self.capacity()
        before = copy.deepcopy(self.data)
        first = resource_constraints(self.data, now=NOW)
        self.assertEqual(first, resource_constraints(self.data, now=NOW))
        self.assertEqual(before, self.data)
        self.assertEqual(len({r['id'] for r in first}), len(first))


if __name__ == '__main__':
    unittest.main()
