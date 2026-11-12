"""Business and failure-path checks for weekly commitments and scheduling."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from pm_core.fixtures import base_data, CALENDAR_ID
from pm_core.store import Store
from pm_core.weekly import (
    get_config, save_config, preview_week, generate_weekly_report,
    list_reports, run_due, retry_report,
)

MON = datetime(2026, 11, 2, 10, tzinfo=ZoneInfo("America/Vancouver")).astimezone(timezone.utc)  # Monday 10:00 Vancouver.
FRI = datetime(2026, 11, 6, 18, tzinfo=timezone.utc)  # Friday 10:00 / Shanghai Saturday.
FRI_TICK = datetime(2026, 11, 6, 17, 10, tzinfo=ZoneInfo("America/Vancouver")).astimezone(timezone.utc)  # Friday 17:10 Vancouver.


def facts():
    data = base_data()
    data["people"] = [
        {"id": "PER-PM", "name": "测试PM", "active": True},
        {"id": "PER-PO", "name": "测试PO", "active": True},
        {"id": "PER-DEV", "name": "测试开发", "active": True},
    ]
    data["projects"] = [{
        "id": "PRJ-1", "name": "单项目周报测试", "po_id": "PER-PO", "pm_id": "PER-PM",
        "calendar_id": CALENDAR_ID, "baseline_start": "2026-10-26",
        "baseline_finish": "2026-11-20", "forecast_finish": "2026-11-20",
        "summary": "项目测试结论", "business_impact": "本周验收与接口窗口需要确认",
    }]
    common = {
        "scope": "project", "project_id": "PRJ-1", "parent_id": "WBS-1", "work_type": "feature",
        "owner_id": "PER-DEV", "acceptor_id": "PER-PM", "priority": "P2",
        "acceptance_criteria": "确认结果并保存依据", "status": "ready",
    }
    data["wbs"] = [
        {"id": "WBS-1", "node_kind": "project", "project_id": "PRJ-1",
         "title": "测试交付", "owner_id": "PER-DEV"},
        {**common, "id": "WI-1", "title": "按期确认任务", "status": "in_review",
         "forecast_finish": "2026-11-04", "actual_start": "2026-11-02"},
        {**common, "id": "WI-2", "title": "后续移出本周", "status": "in_progress",
         "forecast_finish": "2026-11-05", "actual_start": "2026-11-02", "meeting_id": "MTG-1"},
        {**common, "id": "WI-3", "title": "周日才到期", "forecast_finish": "2026-11-08"},
        {**common, "id": "WI-4", "title": "原承诺逾期", "forecast_finish": "2026-11-04"},
        {**common, "id": "WI-5", "title": "随后取消", "forecast_finish": "2026-11-06"},
        {**common, "id": "WI-6", "title": "已逾期结转", "forecast_finish": "2026-10-30",
         "priority": "P0", "priority_reason": "关键验收阻塞需要优先处理"},
        {**common, "id": "WI-LATER", "title": "本周开始下周交付", "status": "in_progress",
         "forecast_finish": "2026-11-12", "actual_start": "2026-11-02"},
        {"id": "WI-INBOX", "title": "待分流优化", "scope": "project", "project_id": "PRJ-1",
         "work_type": "improvement", "status": "intake"},
    ]
    data["meetings"] = [{
        "id": "MTG-1", "title": "本周进度会", "project_id": "PRJ-1",
        "organizer_id": "PER-PM", "scheduled_at": "2026-11-02T18:00:00+08:00",
        "status": "held", "minutes": "测试开发处理接口任务，PM核对结果。",
    }]
    data["registers"] = [{
        "id": "REG-1", "kind": "reminder", "title": "确认验收窗口",
        "project_id": "PRJ-1", "owner_id": "PER-PM", "priority": "P1",
        "status": "open", "next_action": "与业务接口人确认",
    }]
    return data


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "pm.sqlite3"
        self.reports = self.root / "reports"
        with Store(self.db) as store:
            store.apply_batch(facts(), actor="fixture", now=MON - timedelta(minutes=1))

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, at=MON, **kwargs):
        with patch("pm_core.weekly.utc_now", return_value=at):
            return generate_weekly_report(self.db, self.reports, **kwargs)

    def tick(self, at=MON, **kwargs):
        with patch("pm_core.weekly.utc_now", return_value=at):
            return run_due(self.db, self.reports, **kwargs)

    def update(self, record, at=FRI):
        with Store(self.db) as store:
            old = next(item for item in store.data()["wbs"] if item["id"] == record["id"])
            store.apply_batch({"wbs": [dict(record, version=old["version"])]}, actor="PM", now=at)

    def row_count(self, table):
        with Store(self.db) as store:
            return store.conn.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]

    def test_config_defaults_and_strict_values(self):
        config = get_config(self.db)
        self.assertFalse(config["enabled"])
        self.assertEqual(config["timezone"], "Asia/Shanghai")
        with self.assertRaises(ValueError):
            save_config(self.db, {"enabled": "yes"})
        with self.assertRaises(ValueError):
            save_config(self.db, {"monday_time": "9:0"})
        with self.assertRaises(ValueError):
            save_config(self.db, {"timezone": "not/a_zone"})
        changed = save_config(self.db, {"enabled": True, "version": config["version"]})
        self.assertTrue(changed["enabled"])
        with self.assertRaises(ValueError):
            save_config(self.db, {"enabled": False, "version": config["version"]})

    def test_preview_candidates_include_carryover_exclude_next_week_and_inbox(self):
        with patch("pm_core.weekly.utc_now", return_value=MON):
            preview = preview_week(self.db)
        self.assertEqual(preview["week_start"], "2026-11-02")
        self.assertEqual(preview["week_end"], "2026-11-08")
        self.assertTrue(preview["can_confirm"])
        self.assertEqual({row["id"] for row in preview["candidates"]}, {f"WI-{n}" for n in range(1, 7)})
        self.assertTrue(next(row for row in preview["candidates"] if row["id"] == "WI-6")["carryover"])

    def test_delayed_plan_confirmation_keeps_actual_time_and_only_plan(self):
        with self.assertRaisesRegex(ValueError, "只有周一计划"):
            self.capture(MON, kind="friday", confirm=True)
        confirmed = self.capture(FRI, kind="monday", confirm=True)
        self.assertEqual(confirmed["cutoff"], FRI.isoformat())
        self.assertEqual(self.row_count("snapshots"), 1)

    def test_confirmed_plan_is_immutable_and_repeat_reuses_snapshot(self):
        first = self.capture(kind="monday", confirm=True)
        self.update({"id": "WI-3", "forecast_finish": "2026-11-12"}, at=MON)
        repeated = self.capture(kind="monday", confirm=True)
        self.assertEqual(first["snapshot_id"], repeated["snapshot_id"])
        self.assertEqual(first["id"], repeated["id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(self.row_count("snapshots"), 1)
        self.assertEqual(first["metrics"]["committed_count"], 6)
        with Store(self.db) as store:
            data = store.read_snapshot(first["snapshot_id"])["data"]
        self.assertEqual(next(row for row in data["wbs"] if row["id"] == "WI-3")["forecast_finish"], "2026-11-08")

    def test_stale_confirmation_preview_rejected_atomically(self):
        with patch("pm_core.weekly.utc_now", return_value=MON):
            preview = preview_week(self.db)
        self.update({"id": "WI-3", "forecast_finish": "2026-11-09"}, at=MON)
        with self.assertRaisesRegex(ValueError, "清单已变化"):
            self.capture(kind="monday", confirm=True,
                         expected_candidate_versions=preview["expected_candidate_versions"])
        self.assertEqual(self.row_count("snapshots"), 0)

    def test_friday_without_confirmed_monday_leaves_ratio_unknown(self):
        self.capture(kind="monday", confirm=False)
        review = self.capture(FRI, kind="friday")
        self.assertTrue(review["metrics"]["baseline_missing"])
        self.assertIsNone(review["metrics"]["committed_count"])
        self.assertIsNone(review["metrics"]["acceptance_rate"])
        content = Path(review["path"]).read_text(encoding="utf-8")
        self.assertIn("没有同一周", content)
        self.assertIn("正在推进与本周待交付", content)
        self.assertIn("本周进度会", content)
        self.assertIn("测试开发处理接口任务，PM核对结果。", content)
        self.assertIn("全项目未完成 P0", content)

    def test_friday_comparison_uses_original_commitments_and_explicit_acceptance(self):
        plan = self.capture(kind="monday", confirm=True)
        self.update({"id": "WI-1", "status": "done", "actual_finish": "2026-11-04",
                     "completion_evidence": "验收人确认记录"})
        self.update({"id": "WI-2", "forecast_finish": "2026-11-10"})
        self.update({"id": "WI-5", "status": "cancelled", "cancel_reason": "确认不再需要"})
        with Store(self.db) as store:
            store.apply_batch({"wbs": [{
                "id": "WI-NEW", "title": "周内新增动作", "scope": "project", "project_id": "PRJ-1",
                "work_type": "action", "status": "ready", "owner_id": "PER-PM", "acceptor_id": "PER-PO",
                "priority": "P1", "forecast_finish": "2026-11-06", "acceptance_criteria": "确认安排",
            }]}, actor="PM", now=FRI)
        review = self.capture(FRI, kind="friday")
        metrics = review["metrics"]
        self.assertEqual(review["baseline_report_id"], plan["id"])
        self.assertEqual(metrics["committed_count"], 6)
        self.assertEqual(metrics["accepted_count"], 1)
        self.assertEqual(metrics["accepted_on_time"], 1)
        self.assertEqual(metrics["pending_not_due"], 1)  # Sunday is still in the future.
        self.assertEqual(metrics["overdue_unaccepted"], 3)  # Reforecasting does not hide an overdue promise.
        self.assertEqual(metrics["deferred"], 0)
        self.assertEqual(metrics["cancelled"], 1)
        self.assertEqual(metrics["new_or_added_count"], 1)
        self.assertEqual(metrics["acceptance_rate"], 16.7)
        content = Path(review["path"]).read_text(encoding="utf-8")
        self.assertIn("2026-11-05", content)  # Original promise remains visible.
        self.assertIn("2026-11-10", content)
        self.assertIn("验收人确认记录", content)

    def test_current_in_review_is_not_accepted(self):
        self.capture(kind="monday", confirm=True)
        report = self.capture(FRI, kind="friday")
        self.assertEqual(report["metrics"]["accepted_count"], 0)
        self.assertEqual(report["metrics"]["data_quality"]["awaiting_acceptance"], 1)
        self.assertIn("待确认不计完成", Path(report["path"]).read_text(encoding="utf-8"))

    def test_decomposing_a_committed_task_keeps_original_friday_comparison(self):
        plan = self.capture(kind="monday", confirm=True)
        self.assertEqual(plan["metrics"]["committed_count"], 6)
        # WI-4 was a leaf when promised on Monday. Wednesday's decomposition
        # changes candidate counting, but must not remove that frozen promise.
        with Store(self.db) as store:
            parent = next(row for row in store.data()["wbs"] if row["id"] == "WI-4")
            store.apply_batch({"wbs": [
                {"id": "WI-4", "version": parent["version"], "forecast_finish": "2026-11-06"},
                {"id": "WI-4-TODO", "parent_id": "WI-4", "title": "周中拆分的验证待办",
                 "scope": "project", "project_id": "PRJ-1",
                 "work_type": "action", "status": "ready", "priority": "P2",
                 "owner_id": "PER-DEV", "acceptor_id": "PER-PM",
                 "forecast_finish": "2026-11-08", "acceptance_criteria": "保留验证结果"},
            ]}, actor="PM", now=MON + timedelta(days=2))
        with patch("pm_core.weekly.utc_now", return_value=FRI):
            preview = preview_week(self.db)
        candidate_ids = {row["id"] for row in preview["candidates"]}
        self.assertNotIn("WI-4", candidate_ids)
        self.assertIn("WI-4-TODO", candidate_ids)
        self.assertEqual(len(candidate_ids), 6)  # Parent and child are not counted twice.

        review = self.capture(FRI, kind="friday")
        self.assertEqual(review["baseline_report_id"], plan["id"])
        self.assertEqual(review["metrics"]["committed_count"], 6)
        self.assertEqual(review["metrics"]["missing"], 0)
        self.assertEqual(review["metrics"]["overdue_unaccepted"], 5)
        self.assertEqual(review["metrics"]["forecast_changes"], 1)
        self.assertEqual(review["metrics"]["new_or_added_count"], 1)
        content = Path(review["path"]).read_text(encoding="utf-8")
        self.assertRegex(content, r"<tr><td><b>原承诺逾期</b>.*?2026-11-04.*?2026-11-06.*?原承诺已过期.*?</tr>")
        with Store(self.db) as store:
            frozen = store.read_snapshot(plan["snapshot_id"])["data"]["wbs"]
        self.assertNotIn("WI-4-TODO", {row["id"] for row in frozen})
        self.assertEqual(next(row for row in frozen if row["id"] == "WI-4")["forecast_finish"], "2026-11-04")

    def test_business_timezone_is_used_for_due_date_not_machine_day(self):
        self.capture(kind="monday", confirm=True)
        # Vancouver is still Friday, but the project's Shanghai calendar is Saturday.
        report = self.capture(FRI, kind="friday")
        self.assertEqual(report["metrics"]["overdue_unaccepted"], 5)
        self.assertEqual(report["metrics"]["pending_not_due"], 1)

    def test_previous_week_plan_is_not_reused(self):
        self.capture(kind="monday", confirm=True)
        report = self.capture(FRI + timedelta(days=7), kind="friday")
        self.assertTrue(report["metrics"]["baseline_missing"])
        self.assertIsNone(report["baseline_report_id"])

    def test_report_does_not_mutate_business_facts(self):
        with Store(self.db) as store:
            before = store.data()
            events = len(store.history())
        report = self.capture(kind="monday", confirm=True, demo=True)
        self.assertTrue(report["is_demo"])
        self.assertIn("虚构演示项目 · 不代表真实业务事实", Path(report["path"]).read_text(encoding="utf-8"))
        with Store(self.db) as store:
            self.assertEqual(store.data(), before)
            self.assertEqual(len(store.history()), events)

    def test_future_and_backdated_capture_are_rejected(self):
        with patch("pm_core.weekly.utc_now", return_value=MON):
            with self.assertRaisesRegex(ValueError, "未来"):
                generate_weekly_report(self.db, self.reports, now=MON + timedelta(minutes=1))
            with self.assertRaisesRegex(ValueError, "当前日期"):
                generate_weekly_report(self.db, self.reports, now=MON - timedelta(days=1))
            with self.assertRaisesRegex(ValueError, "截止时间之后"):
                generate_weekly_report(self.db, self.reports, now=MON - timedelta(minutes=2))
        self.assertEqual(self.row_count("snapshots"), 0)

    def test_disabled_scheduler_has_no_side_effect_reports(self):
        result = self.tick()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(list_reports(self.db), [])
        self.assertEqual(self.row_count("snapshots"), 0)

    def test_tick_is_idempotent_and_late_run_keeps_actual_capture(self):
        config = save_config(self.db, {"enabled": True, "timezone": "America/Vancouver"})
        first = self.tick(config=config)
        second = self.tick(config=config)
        self.assertEqual(first["status"], "generated")
        self.assertTrue(first["late_run"])
        self.assertEqual(second["status"], "already_generated")
        self.assertEqual(first["reports"][0]["id"], second["reports"][0]["id"])
        self.assertEqual(first["reports"][0]["status"], "draft")
        self.assertEqual(first["reports"][0]["cutoff"], MON.isoformat())
        self.assertNotEqual(first["reports"][0]["cutoff"], first["scheduled_for"])
        self.assertEqual(self.row_count("snapshots"), 1)

    def test_vancouver_friday_tick_can_be_saturday_in_utc(self):
        save_config(self.db, {"enabled": True, "timezone": "America/Vancouver"})
        result = self.tick(FRI_TICK)
        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["reports"][0]["kind"], "friday")
        self.assertEqual(result["reports"][0]["week_start"], "2026-11-02")
        self.assertTrue(result["scheduled_for"].startswith("2026-11-06T17:00"))

    def test_no_previous_day_backfill(self):
        save_config(self.db, {"enabled": True, "timezone": "America/Vancouver"})
        result = self.tick(MON + timedelta(days=1))
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(self.row_count("snapshots"), 0)

    def test_before_schedule_is_not_due(self):
        save_config(self.db, {"enabled": True, "timezone": "America/Vancouver"})
        result = self.tick(MON - timedelta(hours=2))
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(self.row_count("snapshots"), 0)

    def test_render_failure_retry_reuses_capture(self):
        save_config(self.db, {"enabled": True, "timezone": "America/Vancouver"})
        with patch("pm_core.weekly.export_project_report", side_effect=OSError("模拟输出故障")):
            failed = self.tick()
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], "failed")
        failed_report = list_reports(self.db)[0]
        self.assertEqual(failed_report["render_state"], "failed")
        self.assertEqual(self.row_count("snapshots"), 1)
        recovered = self.tick(MON + timedelta(minutes=1))
        self.assertEqual(recovered["status"], "generated")
        self.assertEqual(recovered["reports"][0]["id"], failed_report["id"])
        self.assertEqual(recovered["reports"][0]["snapshot_id"], failed_report["snapshot_id"])
        self.assertEqual(recovered["reports"][0]["cutoff"], MON.isoformat())
        self.assertEqual(self.row_count("snapshots"), 1)

    def test_concurrent_ticks_produce_one_report(self):
        save_config(self.db, {"enabled": True, "timezone": "America/Vancouver"})
        with patch("pm_core.weekly.utc_now", return_value=MON):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: run_due(self.db, self.reports), range(2)))
        statuses = {row["status"] for row in results}
        self.assertIn("generated", statuses)
        self.assertLessEqual(statuses, {"generated", "already_generated", "running"})
        self.assertEqual(self.row_count("snapshots"), 1)
        self.assertEqual(len(list_reports(self.db)), 1)

    def test_retry_uses_saved_snapshot_after_current_facts_change(self):
        report = self.capture(kind="monday", confirm=True)
        self.update({"id": "WI-3", "title": "事后修改标题"}, at=FRI)
        retried = retry_report(self.db, self.reports, report["id"])
        content = Path(retried["path"]).read_text(encoding="utf-8")
        self.assertIn("周日才到期", content)
        self.assertNotIn("事后修改标题", content)
        self.assertEqual(self.row_count("snapshots"), 1)

    def test_friday_after_meeting_keeps_pre_meeting_capture(self):
        pre = self.capture(FRI, kind="friday")
        original = Path(pre["path"]).read_text(encoding="utf-8")
        self.update({"id":"WI-2", "delay_reason":"已确认环境阻碍", "remediation_action":"协调环境恢复"}, at=FRI)
        after = self.capture(FRI, kind="friday", review_of=pre["id"])
        self.assertEqual(after["review_of"], pre["id"])
        self.assertNotEqual(after["snapshot_id"], pre["snapshot_id"])
        self.assertEqual(Path(pre["path"]).read_text(encoding="utf-8"), original)
        self.assertIn("会后记录版", Path(after["path"]).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "同周期"):
            self.capture(FRI, kind="friday", review_of="missing")

    def test_weekly_intermediate_outcome_uses_frozen_due_date(self):
        with Store(self.db) as store:
            store.apply_batch({"weekly_commitments":[{"id":"WC-1", "project_id":"PRJ-1", "work_item_id":"WI-LATER", "week_start":"2026-11-02", "outcome":"提交方案评审", "due_date":"2026-11-06", "owner_id":"PER-DEV", "verification":"评审记录", "status":"candidate"}]}, actor="PM", now=MON)
        plan = self.capture(MON, kind="monday", confirm=True)
        with Store(self.db) as store:
            row = store.data()["weekly_commitments"][0]
            store.apply_batch({"weekly_commitments":[dict(row, status="done", confirmed_at=MON.isoformat(), confirmed_by_id="PER-PM", completed_at=FRI.isoformat(), result_evidence="评审已通过", due_date="2026-11-08")]}, actor="PM", now=FRI)
        review = self.capture(FRI, kind="friday")
        self.assertEqual(plan["metrics"]["committed_count"], 1)
        self.assertEqual(review["metrics"]["committed_count"], 1)
        self.assertEqual(review["metrics"]["accepted_late"], 1)  # Shanghai Saturday, original Friday promise.
        with Store(self.db) as store:
            work = next(r for r in store.data()["wbs"] if r["id"]=="WI-LATER")
            self.assertEqual(work["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
