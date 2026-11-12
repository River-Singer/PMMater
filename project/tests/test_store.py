"""Business invariants for the Excel-backed project repository.

These tests exercise complete transactions rather than duplicating SQL/schema
definitions.  Synthetic people/projects are used throughout; nothing is live.
"""

from __future__ import annotations

import copy
import json
import unittest

from pm_core.store import Store, ValidationError


NOW = "2026-11-05T10:00:00+08:00"


def project(record_id="PRJ-A", **patch):
    result = {
        "id": record_id, "name": "虚构项目 " + record_id,
        "po_id": "PER-PO", "pm_id": "PER-PM", "sponsor_id": "PER-ACC",
        "calendar_id": "CAL-A", "service_id": "SVC-A",
        "baseline_start": "2026-11-01", "baseline_finish": "2026-11-30",
        "forecast_finish": "2026-11-30",
    }
    return result | patch


def work(record_id="WI-A", **patch):
    result = {
        "id": record_id, "title": "虚构接口工作 " + record_id,
        "scope": "project", "work_type": "feature", "project_id": "PRJ-A", "parent_id": "WBS-A", "node_kind": "task",
        "status": "ready", "owner_id": "PER-OWN", "acceptor_id": "PER-ACC", "priority": "P2",
        "baseline_start": "2026-11-04", "baseline_finish": "2026-11-09", "forecast_finish": "2026-11-09",
        "acceptance_criteria": "约定场景验证通过，由确认人检查测试记录。",
    }
    return result | patch


def day_record(record_id="DAY-A", **patch):
    return {
        "id": record_id, "person_id": "PER-OWN", "calendar_id": "CAL-A", "date": "2026-11-05",
        "period": "AM", "record_kind": "actual", "status": "onsite",
    } | patch


def calendar_day(day, weekday, *, calendar="CAL-A", workday=True, **patch):
    return {
        "id": calendar + ":" + day, "calendar_id": calendar, "date": day, "weekday": weekday,
        "is_workday": workday, "day_type": "default_workday" if workday else "default_weekend",
        "verification_status": "default_unverified",
    } | patch


class StoreBusinessTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.apply({
            "people": [
                {"id": "PER-PO", "name": "资源协调人"}, {"id": "PER-PM", "name": "项目经理"},
                {"id": "PER-OWN", "name": "执行负责人"}, {"id": "PER-ACC", "name": "业务确认人"},
            ],
            "calendars": [{"id": "CAL-A", "name": "项目测试日历", "timezone": "Asia/Shanghai", "coverage_start": "2026-11-01", "coverage_end": "2026-11-30"}],
            "calendar_days": [calendar_day("2026-11-05", 4)],
            "services": [{"id": "SVC-A", "name": "虚构业务服务", "calendar_id": "CAL-A"}],
            "projects": [project()],
            "wbs": [{"id": "WBS-A", "node_kind": "project", "project_id": "PRJ-A", "title": "可验收交付", "owner_id": "PER-OWN"}],
        })

    def apply(self, batch, *, now=NOW, dry_run=False):
        return self.store.apply_batch(batch, actor="test", now=now, dry_run=dry_run)

    def get(self, table, record_id):
        return next(row for row in self.store.data()[table] if row["id"] == record_id)

    def invalid(self, batch, code=None, *, now=NOW):
        before, history = self.store.data(), self.store.history()
        with self.assertRaises(ValidationError) as caught:
            self.apply(batch, now=now)
        if code:
            self.assertIn(code, {error["code"] for error in caught.exception.errors}, str(caught.exception))
        # Rejected changes must leave neither facts nor audit events behind.
        self.assertEqual(self.store.data(), before)
        self.assertEqual(self.store.history(), history)
        json.dumps(caught.exception.to_dict(), ensure_ascii=False)
        return caught.exception

    def test_initial_import_can_accept_ready_or_evidenced_done_records(self):
        self.apply({"wbs": [work(), work("WI-B", status="done", actual_start="2026-11-04", actual_finish="2026-11-05", completion_evidence="TEST-REPORT-1") ]})
        self.assertEqual(self.get("wbs", "WI-A")["status"], "ready")
        self.assertEqual(self.get("wbs", "WI-B")["actual_finish"], "2026-11-05")
        self.assertIs(self.get("people", "PER-OWN")["active"], True)

    def test_type_enum_and_foreign_key_failures_are_transactional(self):
        cases = [
            ({"people": [{"id": "PER-X", "name": "错误布尔", "active": "false"}]}, "invalid_type"),
            ({"wbs": [work(priority="urgent")]}, "invalid_type"),
            ({"wbs": [work(owner_id="PER-MISSING")]}, "foreign_key"),
            ({"wbs": [work(forecast_finish="2026-02-30")]}, "invalid_type"),
        ]
        for batch, code in cases:
            with self.subTest(code=code, batch=batch):
                self.invalid(batch, code)

    def test_duplicate_participation_with_blank_dates_is_rejected(self):
        base = {"project_id": "PRJ-A", "person_id": "PER-OWN", "role": "collaborator"}
        self.invalid({"participants": [base | {"id": "REL-A"}, base | {"id": "REL-B"}]}, "duplicate")

    def test_participation_targets_exactly_project_or_work_item(self):
        self.apply({"wbs": [work()]})
        self.invalid({"participants": [{"id": "REL-A", "project_id": "PRJ-A", "work_item_id": "WI-A", "person_id": "PER-OWN", "role": "interface"}]}, "target_count")
        self.invalid({"participants": [{"id": "REL-A", "person_id": "PER-OWN", "role": "interface"}]}, "target_count")

    def test_project_work_needs_wbs_after_intake(self):
        self.apply({"wbs": [work("WI-INTAKE", status="intake", parent_id=None)]})
        self.invalid({"wbs": [work(parent_id=None)]}, "conditional_required")

    def test_operations_and_adhoc_use_independent_table(self):
        rows=[work("WI-BUG", scope="operations", work_type="bug", severity="S1"),
              work("WI-ADHOC", scope="adhoc", work_type="action")]
        for row in rows:
            row.pop("parent_id");row.pop("node_kind")
        self.apply({"operations":rows})
        self.assertEqual(self.get("operations","WI-BUG")["severity"],"S1")
        self.assertFalse(any(r["id"]=="WI-BUG" for r in self.store.data()["wbs"]))

    def test_new_nonproject_wbs_is_rejected_with_conversion_hint(self):
        self.invalid({"wbs":[work(scope="operations",project_id=None,parent_id=None)]},"operations_separate")
        self.invalid({"wbs":[work(scope="adhoc")]},"operations_separate")

    def test_severity_is_not_a_general_task_priority(self):
        self.invalid({"wbs": [work(work_type="feature", severity="S1")]}, "severity_type")
        self.invalid({"wbs": [work(work_type="bug", severity="critical")]}, "invalid_type")

    def test_p0_and_manual_priority_override_require_explanation(self):
        self.invalid({"wbs": [work(priority="P0")]}, "conditional_required")
        self.invalid({"wbs": [work(priority="P3", impact="critical", urgency="immediate")]}, "conditional_required")
        self.apply({"wbs": [work(priority="P3", impact="critical", urgency="immediate", priority_reason="隔离测试环境，正式恢复窗口经业务确认在下周。") ]})
        self.assertEqual(self.get("wbs", "WI-A")["priority"], "P3")

    def test_wbs_parent_and_work_item_cannot_cross_projects(self):
        self.apply({"projects": [project("PRJ-B")], "wbs": [{"id": "WBS-B", "node_kind": "project", "project_id": "PRJ-B", "title": "另一个项目交付", "owner_id": "PER-OWN"}]})
        self.invalid({"wbs": [{"id": "WBS-A", "version": 1, "parent_id": "WBS-B"}]}, "project_mismatch")
        self.invalid({"wbs": [work(parent_id="WBS-B")]}, "project_mismatch")

    def test_wbs_cycles_are_rejected_even_with_all_parents_in_one_batch(self):
        self.invalid({"wbs": [
            {"id": "WBS-A", "version": 1, "parent_id": "WBS-B"},
            {"id": "WBS-B", "project_id": "PRJ-A", "title": "循环节点", "owner_id": "PER-OWN", "parent_id": "WBS-A"},
        ]}, "cycle")

    def test_work_items_attach_to_any_wbs_level_drawer_semantics(self):
        self.apply({"wbs": [{"id": "WBS-B", "node_kind": "deliverable", "project_id": "PRJ-A", "title": "叶子交付", "owner_id": "PER-OWN", "parent_id": "WBS-A"}]})
        # 抽屉语义：任务可挂项目根、交付物或任务任意层级
        self.apply({"wbs": [work()]})
        self.apply({"wbs": [work("WI-B2", parent_id="WBS-B")]})
        self.apply({"wbs": [work("WI-B3", parent_id="WI-B2")]})
        self.assertEqual(self.get("wbs", "WI-B3")["parent_id"], "WI-B2")

    def test_progress_to_review_to_done_requires_real_completion_evidence(self):
        self.apply({"wbs": [work()]})
        self.invalid({"wbs": [{"id": "WI-A", "version": 1, "status": "done", "actual_start": "2026-11-05", "actual_finish": "2026-11-05", "completion_evidence": "TEST-1"}]}, "state_transition")
        self.apply({"wbs": [{"id": "WI-A", "version": 1, "status": "in_progress", "actual_start": "2026-11-05"}]})
        self.apply({"wbs": [{"id": "WI-A", "version": 2, "status": "in_review"}]})
        self.invalid({"wbs": [{"id": "WI-A", "version": 3, "status": "done", "actual_finish": "2026-11-05"}]}, "conditional_required")
        self.apply({"wbs": [{"id": "WI-A", "version": 3, "status": "done", "actual_finish": "2026-11-05", "completion_evidence": "TEST-1 accepted by business"}]})
        self.assertEqual(self.get("wbs", "WI-A")["version"], 4)

    def test_blocked_item_requires_actionable_coordination(self):
        self.invalid({"wbs": [work(status="blocked", actual_start="2026-11-05", blocker_reason="输入未到")]}, "conditional_required")
        self.apply({"wbs": [work(status="blocked", actual_start="2026-11-05", blocker_reason="供应商输入未到", coordinator_id="PER-PO", next_action="联系供应商接口确认补齐日期", next_due_at="2026-11-05T16:00:00+08:00")]})

    def test_reopen_requires_reason_and_preserves_completed_history(self):
        self.apply({"wbs": [work(status="done", actual_start="2026-11-04", actual_finish="2026-11-05", completion_evidence="TEST-1")]})
        self.invalid({"wbs": [{"id": "WI-A", "version": 1, "status": "in_progress", "actual_finish": None}]}, "conditional_required")
        self.apply({"wbs": [{"id": "WI-A", "version": 1, "status": "in_progress", "actual_finish": None, "reopen_reason": "验收后发现未覆盖场景，需要补测。"}]})
        events = self.store.history("wbs", "WI-A")
        self.assertEqual(json.loads(events[-1]["before_json"])["actual_finish"], "2026-11-05")
        self.assertIsNone(self.get("wbs", "WI-A")["actual_finish"])

    def test_optimistic_lock_blocks_stale_or_missing_versions(self):
        self.apply({"wbs": [work()]})
        self.invalid({"wbs": [{"id": "WI-A", "description": "没有版本"}]}, "version_conflict")
        self.apply({"wbs": [{"id": "WI-A", "version": 1, "description": "第一次更新"}]})
        self.invalid({"wbs": [{"id": "WI-A", "version": 1, "description": "旧Excel覆盖"}]}, "version_conflict")
        self.assertEqual(self.get("wbs", "WI-A")["description"], "第一次更新")

    def test_versions_are_integers_not_bool_or_float(self):
        self.apply({"wbs": [work()]})
        for version in [True, 1.0]:
            with self.subTest(version=version, type=type(version).__name__):
                self.invalid({"wbs": [{"id": "WI-A", "version": version, "description": "必须拒绝非整数版本"}]})

    def test_non_text_id_is_rejected_instead_of_creating_unintended_person(self):
        self.invalid({"people": [{"id": 0, "name": "不能悄悄新建"}]})

    def test_whole_batch_rolls_back_valid_rows_when_any_relation_is_invalid(self):
        self.invalid({"people": [{"id": "PER-NEW", "name": "本不该被提交"}], "wbs": [work(owner_id="PER-MISSING")]}, "foreign_key")

    def test_dry_run_reports_changes_without_facts_or_history_writes(self):
        before, history = self.store.data(), self.store.history()
        batch = {"wbs": [work()]}
        original = copy.deepcopy(batch)
        result = self.apply(batch, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.store.data(), before)
        self.assertEqual(self.store.history(), history)
        self.assertEqual(batch, original)

    def test_future_actual_dates_cannot_be_recorded_as_progress(self):
        self.invalid({"wbs": [work(status="in_progress", actual_start="2026-11-06")]}, "future_actual")
        self.invalid({"wbs": [work(status="done", actual_start="2026-11-04", actual_finish="2026-11-06", completion_evidence="future record")]}, "future_actual")

    def test_planned_and_actual_same_slot_coexist_but_morning_cannot_sign_afternoon(self):
        self.apply({"day_records": [day_record(), day_record("DAY-PLAN", record_kind="planned", status="available")]})
        self.assertEqual(len(self.store.data()["day_records"]), 2)
        self.invalid({"day_records": [day_record("DAY-FUTURE", period="PM")]}, "future_attendance")
        self.apply({"day_records": [day_record("DAY-PM-PLAN", record_kind="planned", status="available", period="PM")]})

    def test_attendance_status_must_match_plan_or_actual_kind(self):
        self.invalid({"day_records": [day_record(status="available")]}, "attendance_kind")
        self.invalid({"day_records": [day_record(record_kind="planned", status="onsite")]}, "attendance_kind")

    def test_person_attendance_is_not_duplicated_for_different_project_calendars(self):
        self.apply({
            "calendars": [{"id": "CAL-B", "name": "另一项目日历", "timezone": "Asia/Shanghai", "coverage_start": "2026-11-01", "coverage_end": "2026-11-30"}],
            "calendar_days": [calendar_day("2026-11-05", 4, calendar="CAL-B")],
        })
        self.invalid({"day_records": [day_record(), day_record("DAY-B", calendar_id="CAL-B")]}, "duplicate")

    def test_attendance_requires_date_row_and_rejects_future_actual(self):
        self.invalid({"day_records": [day_record(date="2026-11-06", record_kind="planned", status="available")]}, "calendar_missing")
        self.apply({"calendar_days": [calendar_day("2026-11-06", 5)]})
        self.invalid({"day_records": [day_record(date="2026-11-06")]}, "future_attendance")

    def test_calendar_day_needs_real_weekday_and_complete_source_record(self):
        self.invalid({"calendar_days": [calendar_day("2026-11-06", 3)]}, "weekday")
        self.invalid({"calendar_days": [calendar_day("2026-11-06", 5, source_url="https://example.test/company-calendar")]}, "provenance")
        self.invalid({"calendar_days": [calendar_day("2026-11-06", 5, verification_status="verified")]}, "conditional_required")

    def test_dependencies_reject_cycles_and_unfinished_fs_predecessor(self):
        self.invalid({
            "wbs": [work(), work("WI-B")],
            "dependencies": [
                {"id": "DEP-A", "predecessor_id": "WI-A", "successor_id": "WI-B"},
                {"id": "DEP-B", "predecessor_id": "WI-B", "successor_id": "WI-A"},
            ],
        }, "cycle")
        self.invalid({
            "wbs": [work(), work("WI-B", status="in_progress", actual_start="2026-11-05")],
            "dependencies": [{"id": "DEP-A", "predecessor_id": "WI-A", "successor_id": "WI-B"}],
        }, "unfinished_predecessor")

    def test_fs_successor_cannot_start_before_predecessor_finished(self):
        self.invalid({
            "wbs": [work(status="done", actual_start="2026-11-04", actual_finish="2026-11-05", completion_evidence="TEST-1"), work("WI-B", status="in_progress", actual_start="2026-11-04")],
            "dependencies": [{"id": "DEP-A", "predecessor_id": "WI-A", "successor_id": "WI-B"}],
        }, "dependency_date")

    def test_reopening_predecessor_keeps_historical_start_but_blocks_new_starts(self):
        self.apply({
            "wbs": [work(status="done", actual_start="2026-11-04", actual_finish="2026-11-04", completion_evidence="TEST-1"), work("WI-B", status="in_progress", actual_start="2026-11-05")],
            "dependencies": [{"id": "DEP-A", "predecessor_id": "WI-A", "successor_id": "WI-B"}],
        })
        self.apply({"wbs": [{"id": "WI-A", "version": 1, "status": "in_progress", "actual_finish": None, "reopen_reason": "后续联调发现缺陷，前置交付重开修复。"}]})
        self.assertEqual(self.get("wbs", "WI-B")["actual_start"], "2026-11-05")
        self.assertEqual(self.get("wbs", "WI-B")["version"], 1)
        self.assertEqual(json.loads(self.store.history("wbs", "WI-A")[-1]["before_json"])["status"], "done")
        # An existing fact is retained, but changing it or creating a new start is checked.
        self.invalid({"wbs": [{"id": "WI-B", "version": 1, "actual_start": "2026-11-04"}]}, "unfinished_predecessor")
        self.invalid({
            "wbs": [work("WI-C", status="in_progress", actual_start="2026-11-05")],
            "dependencies": [{"id": "DEP-C", "predecessor_id": "WI-A", "successor_id": "WI-C"}],
        }, "unfinished_predecessor")

    def test_fs_workday_lag_uses_makeup_workday_and_does_not_guess_missing_days(self):
        dependency = {"id": "DEP-A", "predecessor_id": "WI-A", "successor_id": "WI-B", "lag_workdays": 1}
        self.invalid({
            "wbs": [work(status="done", actual_start="2026-11-04", actual_finish="2026-11-04", completion_evidence="TEST-1"), work("WI-B", status="in_progress", actual_start="2026-11-05")],
            "dependencies": [dependency],
        }, "calendar_missing")
        self.apply({"calendar_days": [calendar_day("2026-11-06", 5), calendar_day("2026-11-07", 6, workday=False), calendar_day("2026-11-08", 7, workday=False), calendar_day("2026-11-09", 1)]})
        batch = {
            "wbs": [work(status="done", actual_start="2026-11-05", actual_finish="2026-11-06", completion_evidence="TEST-1"), work("WI-B", status="in_progress", actual_start="2026-11-09")],
            "dependencies": [dependency | {"lag_workdays": 2}],
        }
        self.invalid(batch, "dependency_lag", now="2026-11-09T10:00:00+08:00")
        batch["calendar_days"] = [{"id": "CAL-A:2026-11-07", "version": 1, "is_workday": True, "day_type": "makeup_workday", "verification_status": "verified", "source_url": "https://example.test/company-calendar", "verified_at": "2026-11-05T09:00:00+08:00"}]
        self.apply(batch, now="2026-11-09T10:00:00+08:00")
        self.assertEqual(self.get("wbs", "WI-B")["actual_start"], "2026-11-09")

    def test_existing_baseline_cannot_be_overwritten_without_approved_change(self):
        self.apply({"wbs": [work()]})
        update = {"id": "WI-A", "version": 1, "baseline_finish": "2026-11-10"}
        self.invalid({"wbs": [update]}, "baseline_control")
        self.apply({"registers": [{"id": "REG-A", "kind": "change", "title": "变更尚在评审", "project_id": "PRJ-A", "owner_id": "PER-PM"}]})
        self.invalid({"wbs": [update | {"baseline_change_id": "REG-A"}]}, "baseline_control")

    def test_approved_change_allows_baseline_update_and_keeps_before_image(self):
        self.apply({"wbs": [work()]})
        self.apply({
            "registers": [{"id": "REG-A", "kind": "change", "title": "批准调整日期", "project_id": "PRJ-A", "owner_id": "PER-PM", "status": "approved", "decision_by_id": "PER-PO", "decision": "同意调整至11月10日", "decided_at": "2026-11-05T09:00:00+08:00"}],
            "wbs": [{"id": "WI-A", "version": 1, "baseline_finish": "2026-11-10", "forecast_finish": "2026-11-10", "baseline_change_id": "REG-A"}],
        })
        event = self.store.history("wbs", "WI-A")[-1]
        self.assertEqual(json.loads(event["before_json"])["baseline_finish"], "2026-11-09")
        self.assertEqual(json.loads(event["after_json"])["baseline_finish"], "2026-11-10")

    def test_source_register_cannot_reference_another_project(self):
        self.apply({"projects": [project("PRJ-B")], "registers": [{"id": "REG-B", "kind": "issue", "title": "另一项目的问题", "project_id": "PRJ-B", "owner_id": "PER-PM"}]})
        self.invalid({"wbs": [work(register_id="REG-B")]}, "project_mismatch")

    def test_snapshot_is_fixed_after_later_updates_and_reader_mutations(self):
        self.apply({"wbs": [work()]})
        snapshot = self.store.snapshot("周一确认版本", cutoff=NOW)
        stored = self.store.read_snapshot(snapshot["id"])
        old_title = stored["data"]["wbs"][0]["title"]
        self.apply({"wbs": [{"id": "WI-A", "version": 1, "title": "周中更新标题"}]})
        stored["data"]["wbs"][0]["title"] = "试图修改返回对象"
        again = self.store.read_snapshot(snapshot["id"])
        self.assertEqual(again["data"]["wbs"][0]["title"], old_title)
        self.assertEqual(self.get("wbs", "WI-A")["title"], "周中更新标题")
        self.assertLess(again["event_watermark"], self.store.history()[-1]["event_id"])


if __name__ == "__main__":
    unittest.main()
