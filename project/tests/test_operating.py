"""Management semantics shared by reports and dispatch previews."""
import copy
import unittest
from pm_core.fixtures import demo_data
from pm_core.operating import management_review, delay_review, plan_followups, render_management_section, commitment_review

NOW = "2026-11-06T09:00:00+08:00"


class OperatingTests(unittest.TestCase):
    def setUp(self):
        self.data = demo_data()
        self.p = self.data["projects"][0]
        self.pid = self.p["id"]
        self.person = self.data["people"][0]["id"]
        self.work = next(w for w in self.data["wbs"] if w.get("node_kind") == "task")
        self.data["calendar_years"] = [{"id":"CY", "calendar_id":self.p["calendar_id"], "year":2026, "status":"complete", "source_url":"https://example.test/notice", "verified_at":"2026-01-01T00:00:00+08:00", "exception_hash":"test-evidence"}]

    def operation(self):
        r = dict(self.work, id="OPS-T", title="配置优化 <b>文本</b>", scope="operations", parent_id=None,
                 project_id=self.pid, status="in_progress", baseline_finish="2026-11-05", forecast_finish="2026-11-09", actual_finish=None,
                 delay_reason="环境准备延期", delay_impact="联调推迟", remediation_action="修复配置", remediation_owner_id=self.person,
                 remediation_expected="2026-11-07", remediation_result=None)
        self.data["operations"] = [r]
        return r

    def test_operations_do_not_change_project_denominator(self):
        before = management_review(self.data, now=NOW)["counts"]["wbs"]
        self.operation()
        after = management_review(self.data, now=NOW)
        self.assertEqual(before, after["counts"]["wbs"])
        self.assertEqual(after["counts"]["operations"]["open"], 1)

    def test_legacy_nonproject_stubs_excluded(self):
        self.operation()
        self.data["wbs"].append(dict(self.data["operations"][0], id="LEGACY", migrated_operation_id="OPS-T"))
        result = management_review(self.data, now=NOW)
        self.assertNotIn("LEGACY", [r["id"] for r in result["delays"]])

    def test_delay_reason_and_remedy_are_visible_and_escaped(self):
        self.operation()
        html = render_management_section(self.data, as_of=NOW)
        for value in ("环境准备延期", "修复配置", "2026-11-07", "运维与优化事项", "会前审议版"):
            self.assertIn(value, html)
        self.assertNotIn("<b>文本</b>", html)
        self.assertIn("&lt;b&gt;文本&lt;/b&gt;", html)

    def test_today_is_not_overdue(self):
        row = self.operation()
        row.update(baseline_finish="2026-11-06", forecast_finish="2026-11-06", delay_risk="none")
        self.assertNotIn("OPS-T", [r["id"] for r in delay_review(self.data, now=NOW)])
        reminders = plan_followups(self.data, now=NOW)
        self.assertFalse(any(r["record_id"] == "OPS-T" and r["kind"] == "overdue" for r in reminders))

    def test_closed_work_stops_followups(self):
        row = self.operation()
        row.update(status="done", actual_finish="2026-11-06")
        self.assertFalse(any(r["record_id"] == "OPS-T" for r in plan_followups(self.data, now=NOW)))

    def test_calendar_pending_does_not_send_assumed_workday(self):
        self.operation()
        self.data["calendar_years"] = []
        self.assertEqual(plan_followups(self.data, now=NOW), [])

    def test_unknown_resource_capacity_remains_unknown(self):
        self.data["resource_weeks"] = [{"id":"R", "project_id":self.pid, "person_id":self.person, "week_start":"2026-11-02", "net_available_hours":None, "project_hours":5, "operations_hours":2, "buffer_hours":1, "confirmed_at":NOW}]
        res = management_review(self.data, now=NOW)["resources"]["people"][0]
        self.assertIsNone(res["remaining_hours"])
        self.assertEqual(res["status"], "pending")

    def test_monthly_completed_items_not_sum_of_weekly_totals(self):
        row = self.operation()
        row.update(status="done", actual_finish="2026-11-03")
        result = management_review(self.data, now=NOW, period="month")
        self.assertEqual(result["counts"]["operations"]["completed_in_period"], ["OPS-T"])
        self.assertIsNone(result["forecast_comparison"])
        self.assertIn("不自动", result["stage_decision"])

    def test_checkpoint_completion_does_not_complete_parent_task(self):
        self.data["weekly_commitments"] = [{"id":"WC", "project_id":self.pid, "work_item_id":self.work["id"], "week_start":"2026-11-02", "outcome":"完成接口评审", "due_date":"2026-11-06", "status":"confirmed", "confirmed_at":"2026-11-02T11:00:00+08:00"}]
        baseline = copy.deepcopy(self.data)
        self.data["weekly_commitments"][0].update(status="done", completed_at=NOW, result_evidence="评审文档")
        before = self.work.get("status")
        rows = commitment_review(self.data, now=NOW, baseline_data=baseline)
        self.assertEqual(rows[0]["outcome_state"], "accepted_on_time")
        self.assertEqual(self.work.get("status"), before)

    def test_friday_freshness_is_separate_from_stale_threshold(self):
        row = self.operation()
        row.update(progress_confirmed_at="2026-11-05T16:00:00+08:00")
        items = plan_followups(self.data, now=NOW, config={"stale_days":30})
        target = [r for r in items if r["record_id"] == "OPS-T" and r["kind"] == "friday_result"]
        self.assertEqual(len(target), 1)
        self.assertIn("progress_note", target[0]["required_fields"])
        self.assertEqual(target[0]["local_date"], "2026-11-06")

    def test_moved_review_collects_on_actual_meeting_date(self):
        self.operation()
        items = plan_followups(self.data, now="2026-11-05T09:00:00+08:00", config={"review_date":"2026-11-05"})
        self.assertTrue(any(r["kind"]=="friday_result" and r["record_id"]=="OPS-T" for r in items))

    def test_holiday_plan_requires_explicit_replacement(self):
        self.operation()
        for row in self.data["calendar_days"]:
            if row["date"]=="2026-11-02": row["is_workday"]=False
        items = plan_followups(self.data, now="2026-11-03T09:00:00+08:00")
        self.assertTrue(any(r["kind"]=="schedule_pending_plan" for r in items))
        self.assertFalse(any(r["kind"]=="monday_plan" for r in items))
        items = plan_followups(self.data, now="2026-11-03T09:00:00+08:00", config={"plan_date":"2026-11-03"})
        self.assertTrue(any(r["kind"]=="monday_plan" for r in items))

    def test_future_unstarted_work_is_not_daily_progress_nag(self):
        row = self.operation()
        row.update(status="ready", baseline_finish="2026-12-20", forecast_finish="2026-12-20", forecast_start="2026-12-01", delay_risk="none")
        self.assertFalse(any(r["record_id"]=="OPS-T" for r in plan_followups(self.data, now=NOW)))

    def test_explicit_dependency_forecast_warns_without_marking_blocked(self):
        tasks = [w for w in self.data['wbs'] if w.get('node_kind') == 'task']
        pre, suc = tasks[0], tasks[1]
        pre.update(status='in_progress', forecast_finish='2026-11-09')
        suc.update(status='ready', forecast_start='2026-11-06')
        self.data['dependencies'] = [{'id':'DEP-T','predecessor_id':pre['id'],'successor_id':suc['id'],'lag_workdays':0}]
        items = plan_followups(self.data, now=NOW)
        self.assertTrue(any(r['kind']=='dependency_risk' and r['record_id']==suc['id'] for r in items))
        self.assertEqual(suc['status'], 'ready')

    def test_overdue_escalates_only_when_configured(self):
        self.operation()
        self.assertFalse(any(r['kind']=='escalation' and r['record_id']=='OPS-T' for r in plan_followups(self.data,now=NOW)))
        items = plan_followups(self.data, now=NOW, config={'escalation_after_days':1})
        self.assertTrue(any(r['kind']=='escalation' and r['record_id']=='OPS-T' and r['recipient_id']==self.p['pm_id'] for r in items))

    def test_staffing_and_leave_render_with_real_fields(self):
        from pm_core.fixtures import operating_demo_data
        html = render_management_section(operating_demo_data(), as_of='2026-11-11T12:00:00+00:00')
        self.assertIn('未来休假与交接', html)
        self.assertIn('2026-11-18', html)
        self.assertIn('远程', html)
        self.assertIn('24', html)

    def test_delay_sources_and_daily_unknown_are_safely_visible(self):
        from pm_core.fixtures import operating_demo_data
        data=operating_demo_data()
        for row in data['wbs']:
            row['source_url']='https://example.invalid/trial/friday?x=1&y=2'
        html=render_management_section(data,as_of='2026-11-11T12:00:00+00:00')
        self.assertIn('href="https://example.invalid/trial/friday?x=1&amp;y=2"',html)
        self.assertIn('未来休假:',html)
        self.assertNotIn('周敏 PM',html)
        for row in data['wbs']:
            row['source_url']='javascript:alert(1)'
        html=render_management_section(data,as_of='2026-11-11T12:00:00+00:00')
        self.assertNotIn('href="javascript:',html)

    def test_monthly_outlook_has_unknown_future_weeks_and_leave_evidence(self):
        from pm_core.fixtures import operating_demo_data
        review = management_review(operating_demo_data(), now='2026-11-11T12:00:00+00:00', period='month')
        self.assertEqual(len(review['resource_forecast']), 6)
        first = next(r for r in review['resource_forecast'][0]['people'] if r['person_id']=='PER-VENDOR')
        self.assertEqual(first['net_available_hours'], 24)
        second = next(r for r in review['resource_forecast'][1]['people'] if r['person_id']=='PER-VENDOR')
        self.assertIsNone(second['net_available_hours'])
        self.assertIsNone(second['remaining_hours'])
        self.assertEqual(second['leave_plan_ids'], ['LEAVE-DEMO-1'])
        self.assertEqual(second['leave_plans'][0]['status'], 'proposed')

    def test_monthly_standalone_report_uses_same_forecast(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from pm_core.fixtures import operating_demo_data
        from pm_core.operating import export_management_report
        with TemporaryDirectory() as directory:
            path=Path(directory)/'month.html'
            export_management_report(operating_demo_data(),path,as_of='2026-11-11T12:00:00+00:00',lookahead_weeks=4)
            html=path.read_text(encoding='utf-8')
            self.assertIn('未来 4 周资源展望',html)
            self.assertIn('2026-11-18',html)
            self.assertIn('待确认',html)

    def test_employee_missing_weeks_are_not_resource_confirmation_requests(self):
        from pm_core.fixtures import operating_demo_data
        data = operating_demo_data()
        review = management_review(data, now='2026-11-11T09:00:00+08:00', period='month')
        employees = {p['id'] for p in data['people'] if p['person_type'] == 'employee'}
        for week in review['resource_forecast']:
            requested = {r['person_id'] for r in week['people'] if r['confirmation_required']}
            self.assertFalse(requested & employees)
        vendor = next(r for r in review['resource_forecast'][1]['people'] if r['person_id'] == 'PER-VENDOR')
        self.assertTrue(vendor['confirmation_required'])
        self.assertIsNone(vendor['net_available_hours'])

    def test_employee_resource_and_leave_facts_survive_without_pending_confirmation(self):
        from pm_core.fixtures import operating_demo_data
        from pm_core.management_text import management_paragraphs
        data = operating_demo_data()
        employee = next(p for p in data['people'] if p['person_type'] == 'employee')
        row = dict(data['resource_weeks'][0], id='EMP-WEEK', person_id=employee['id'], confirmed_at=None)
        data['resource_weeks'].append(row)
        leave = dict(data['leave_plans'][0], id='EMP-LEAVE', person_id=employee['id'])
        data['leave_plans'].append(leave)
        before = copy.deepcopy(data)
        review = management_review(data, now='2026-11-11T09:00:00+08:00', period='month')
        existing = next(r for r in review['resources']['people'] if r['id'] == 'EMP-WEEK')
        self.assertEqual('pending', existing['status'])
        self.assertIsNone(existing['confirmed_at'])
        self.assertFalse(existing['confirmation_required'])
        self.assertEqual(row['net_available_hours'], existing['net_available_hours'])
        self.assertIn('EMP-LEAVE', [r['id'] for r in review['leave_plans']])
        self.assertEqual(before, data)
        html = render_management_section(data, as_of='2026-11-11T09:00:00+08:00', period='month')
        self.assertIn('资料未完整 · 员工免确认', html)
        paragraphs = management_paragraphs(data, review)
        self.assertTrue(any('员工免确认' in p and employee['name'] in p for p in paragraphs))

    def test_unknown_person_type_is_pending_identity_not_resource_request(self):
        from pm_core.fixtures import operating_demo_data
        data = operating_demo_data()
        person = next(p for p in data['people'] if p['id'] == 'PER-VENDOR')
        person.pop('person_type')
        review = management_review(data, now='2026-11-11T09:00:00+08:00', period='month')
        for week in review['resource_forecast'][:2]:
            row = next(r for r in week['people'] if r['person_id'] == person['id'])
            self.assertFalse(row['confirmation_required'])
            self.assertEqual('identity_unknown', row['confirmation_scope'])
            self.assertIn('类型待核实', row['availability_note'])
        self.assertFalse(any(r['person_id'] == person['id'] for r in review['daily_status']['eligible']))

    def test_employee_task_followup_is_not_suppressed(self):
        row = self.operation()
        row['owner_id'] = self.person
        self.data['people'][0]['person_type'] = 'employee'
        reminders = plan_followups(self.data, now=NOW)
        self.assertTrue(any(r['record_id'] == row['id'] and r['recipient_id'] == self.person for r in reminders))


if __name__ == "__main__":
    unittest.main()
