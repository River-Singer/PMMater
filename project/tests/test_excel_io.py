"""Behavior-focused tests of the Excel input boundary and monthly attendance."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from pm_core.excel_io import export_workbook, read_workbook, ExcelFormatError, FUTURE, excel_fields, DERIVED_FIELDS
from pm_core.schema import TABLES
from pm_core.store import Store


def sample_data():
    data = {name: [] for name in TABLES}
    data["people"] = [
        {"id": "PER-1", "name": "林岚", "department": "协调组", "active": True, "version": 1},
        {"id": "PER-2", "name": "陈越", "department": "项目组", "active": True, "version": 1},
    ]
    data["calendars"] = [{"id": "CAL-1", "name": "测试周历", "timezone": "Asia/Shanghai",
                          "coverage_start": "2026-10-01", "coverage_end": "2026-12-31", "version": 1}]
    d = date(2026, 10, 1)
    while d <= date(2026, 12, 31):
        data["calendar_days"].append({"id": "CD-" + d.isoformat(), "calendar_id": "CAL-1", "date": d.isoformat(),
            "weekday": d.isoweekday(), "is_workday": d.weekday() < 5,
            "day_type": "default_workday" if d.weekday() < 5 else "default_weekend",
            "verification_status": "default_unverified", "version": 1})
        d += timedelta(days=1)
    data["projects"] = [{"id": "PRJ-1", "name": "单项目测试", "project_type": "development", "stage": "executing",
                         "po_id": "PER-1", "pm_id": "PER-2", "calendar_id": "CAL-1", "version": 1}]
    data["wbs"] = [{"id": "WI-1", "node_kind": "task", "title": "检查接口异常", "scope": "project", "project_id": "PRJ-1", "work_type": "bug",
                           "status": "intake", "owner_id": "PER-1", "is_acceptance_node": False, "delay_risk": "none", "version": 1}]
    data["day_records"] = [
        {"id": "DAY-OLD", "person_id": "PER-1", "calendar_id": "CAL-1", "date": "2026-10-31", "period": "AM", "record_kind": "actual", "status": "onsite", "notes": "历史说明", "version": 3},
        {"id": "DAY-1", "person_id": "PER-1", "calendar_id": "CAL-1", "date": "2026-11-01", "period": "AM", "record_kind": "actual", "status": "onsite", "notes": "保留说明", "version": 2},
        {"id": "DAY-2", "person_id": "PER-2", "calendar_id": "CAL-1", "date": "2026-11-03", "period": "PM", "record_kind": "planned", "status": "available", "version": 1},
    ]
    return data


class ExcelIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "workbench.xlsx"
        self.data = sample_data()
        self.export()

    def tearDown(self):
        self.temp.cleanup()

    def export(self, data=None, month="2026-11"):
        export_workbook(self.data if data is None else data, self.path,
                        month=month, calendar_id="CAL-1", as_of="2026-11-05T10:00:00+08:00")

    def edit(self, action):
        wb = load_workbook(self.path)
        try:
            action(wb)
            wb.save(self.path)
        finally:
            wb.close()

    def col(self, table, name):
        return [f.name for f in excel_fields(table)].index(name) + 1

    def test_unchanged_roundtrip_is_empty_batch(self):
        self.assertEqual(read_workbook(self.path), {})

    def test_edit_work_item_keeps_original_version(self):
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "title")), "value", "检查供应商接口异常"))
        result = read_workbook(self.path)
        self.assertEqual(list(result), ["wbs"])
        self.assertEqual(result["wbs"][0]["id"], "WI-1")
        self.assertEqual(result["wbs"][0]["version"], 1)
        self.assertEqual(result["wbs"][0]["title"], "检查供应商接口异常")

    def test_new_row_gets_schema_defaults_and_auto_id(self):
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(8, self.col("wbs", "title")), "value", "新增本项目待办"))
        row = read_workbook(self.path)["wbs"][0]
        self.assertNotIn("id", row)
        self.assertNotIn("version", row)
        self.assertEqual((row["scope"], row["work_type"], row["status"]), ("project", "request", "intake"))
        self.assertEqual(row["project_id"], "PRJ-1")

    def test_single_project_front_and_hidden_context(self):
        wb = load_workbook(self.path)
        try:
            self.assertEqual([ws.title for ws in wb if ws.sheet_state == "visible"], [
                "首页", "项目概况", "WBS总表", "运维与优化事项", "每日工作安排", "未来休假计划", "人员周资源安排", "本周成果承诺", "项目通讯录", "计划月历", "实际月历",
                "交付物验收", "接口清单", "项目文档", "会议", "提醒与风险", "字段字典"])
            for sheet in ("产品与服务", "参与关系", "前置依赖", "每日记录", "日期表", "日历配置"):
                self.assertEqual(wb[sheet].sheet_state, "hidden")
            for field in ("scope", "project_id", "service_id", "version"):
                self.assertTrue(wb["WBS总表"].column_dimensions[get_column_letter(self.col("wbs", field))].hidden)
            for field in ("person_type", "company", "department", "project_role", "phone", "feishu_url", "email"):
                self.assertFalse(wb["项目通讯录"].column_dimensions[get_column_letter(self.col("people", field))].hidden)
        finally:
            wb.close()

    def test_new_interfaces_documents_and_actions_bind_current_project(self):
        updates = {
            "interfaces": {"name": "查询审批状态", "source_system": "采购系统", "target_system": "审批系统",
                           "address": "https://example.invalid/api/status", "method": "GET",
                           "description": "读取当前审批结论", "owner_id": "PER-1"},
            "project_documents": {"title": "需求说明", "url": "https://example.invalid/docs/requirements", "owner_id": "PER-2"},
            "wbs": {"title": "已确认的业务范围", "owner_id": "PER-1"},
            "meetings": {"title": "项目接口协调会", "organizer_id": "PER-2", "scheduled_at": "2026-11-05T09:00:00+08:00"},
            "registers": {"kind": "reminder", "title": "提醒业务确认接口口径", "owner_id": "PER-2"},
            "deliverables": {"work_item_id": "WI-1", "title": "接口测试材料", "document_url": "https://example.invalid/docs/test"},
        }
        def mutate(wb):
            for table, values in updates.items():
                for field, value in values.items():
                    wb[TABLES[table]["sheet"]].cell(7, self.col(table, field)).value = value
        self.edit(mutate)
        batch = read_workbook(self.path)
        self.assertEqual(set(batch), set(updates))
        for table in updates:
            self.assertEqual(batch[table][0]["project_id"], "PRJ-1")
        with Store(Path(self.temp.name) / "project.sqlite3") as store:
            seed = {t: [{k: v for k, v in r.items() if k != "version"} for r in rows] for t, rows in self.data.items()}
            store.apply_batch(seed, actor="fixture")
            result = store.apply_batch(batch, actor="PM")
            self.assertEqual(result["changed"], len(updates))
            self.assertEqual(store.data()["interfaces"][0]["method"], "GET")

    def test_blank_project_and_child_rows_can_be_created_in_one_import(self):
        blank = deepcopy(self.data)
        blank["projects"], blank["wbs"] = [], []
        self.export(blank)
        def mutate(wb):
            for field, value in {"name": "新建项目", "po_id": "PER-1", "pm_id": "PER-2"}.items():
                wb["项目概况"].cell(7, self.col("projects", field)).value = value
            wb["WBS总表"].cell(7, self.col("wbs", "title")).value = "确认输入范围"
        self.edit(mutate)
        batch = read_workbook(self.path)
        project = batch["projects"][0]
        self.assertTrue(project["id"].startswith("PRJ-"))
        self.assertEqual(project["calendar_id"], "CAL-1")
        self.assertEqual(batch["wbs"][0]["project_id"], project["id"])
        with Store(Path(self.temp.name) / "new-project.sqlite3") as store:
            seed = {t: [{k: v for k, v in r.items() if k != "version"} for r in rows] for t, rows in blank.items()}
            store.apply_batch(seed, actor="fixture")
            store.apply_batch(batch, actor="PM")
            self.assertEqual(len(store.data()["projects"]), 1)

    def test_new_project_task_requires_project_overview(self):
        blank = deepcopy(self.data)
        blank["projects"], blank["wbs"] = [], []
        self.export(blank)
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "title")), "value", "新增任务"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "missing_project")

    def test_multiple_projects_are_not_silently_combined(self):
        data = deepcopy(self.data)
        data["projects"].append({**data["projects"][0], "id": "PRJ-2", "name": "另一个项目"})
        with self.assertRaises(ExcelFormatError) as cm:
            self.export(data)
        self.assertEqual(cm.exception.code, "single_project_required")

    def test_legacy_dependency_sheet_is_readonly(self):
        self.edit(lambda wb: setattr(wb["前置依赖"]["B7"], "value", "WI-1"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "readonly_changed")

    def test_project_roster_contacts_preserve_text_and_links(self):
        values = {"name": "供应商联系人", "person_type": "vendor", "company": "示例供应商",
                  "department": "接口开发", "project_role": "外部接口负责人", "phone": "00123456789",
                  "feishu_url": "https://example.invalid/feishu/person", "email": "contact@example.invalid"}
        def mutate(wb):
            for field, value in values.items():
                wb["项目通讯录"].cell(9, self.col("people", field)).value = value
        self.edit(mutate)
        row = read_workbook(self.path)["people"][0]
        for field, value in values.items():
            self.assertEqual(row[field], value)
        wb = load_workbook(self.path)
        try:
            self.assertEqual(wb["项目通讯录"].cell(9, self.col("people", "phone")).number_format, "@")
        finally:
            wb.close()

    def test_old_excel_contract_requires_reexport(self):
        self.edit(lambda wb: setattr(wb["_meta"]["A1"], "value", "PM_WORKBENCH_EXCEL_V4"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "schema_version")

    def test_new_calendar_cell_returns_date_and_halfday(self):
        # Person 1, Nov 4, AM: column G (D is Nov 1).
        self.edit(lambda wb: setattr(wb["实际月历"]["G7"], "value", "远程在岗"))
        rows = read_workbook(self.path)["day_records"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {"person_id": "PER-1", "calendar_id": "CAL-1", "date": "2026-11-04", "period": "AM", "record_kind": "actual", "status": "remote", "notes": None})

    def test_existing_calendar_change_keeps_id_version_notes(self):
        self.edit(lambda wb: setattr(wb["实际月历"]["D7"], "value", "远程在岗"))
        row = read_workbook(self.path)["day_records"][0]
        self.assertEqual((row["id"], row["version"], row["notes"]), ("DAY-1", 2, "保留说明"))
        self.assertEqual(row["status"], "remote")

    def test_clear_existing_attendance_marks_unknown(self):
        self.edit(lambda wb: setattr(wb["实际月历"]["D7"], "value", None))
        row = read_workbook(self.path)["day_records"][0]
        self.assertEqual(row["id"], "DAY-1")
        self.assertEqual(row["status"], "unknown")

    def test_plan_and_actual_are_separate_records(self):
        def mutate(wb):
            wb["计划月历"]["G7"] = "可安排"
            wb["实际月历"]["G7"] = "现场在岗"
        self.edit(mutate)
        rows = read_workbook(self.path)["day_records"]
        self.assertEqual({r["record_kind"] for r in rows}, {"planned", "actual"})
        self.assertEqual(len(rows), 2)

    def test_cross_month_has_all_days_and_preserves_history(self):
        self.export(month="2026-12")
        wb = load_workbook(self.path)
        self.assertEqual(wb["实际月历"].cell(5, 34).value.date(), date(2026, 12, 31))
        wb.close()
        self.assertEqual(read_workbook(self.path), {})
        self.edit(lambda wb: setattr(wb["计划月历"]["AH7"], "value", "请假"))
        row = read_workbook(self.path)["day_records"][0]
        self.assertEqual(row["date"], "2026-12-31")
        self.assertNotIn("DAY-OLD", {r.get("id") for r in read_workbook(self.path)["day_records"]})

    def test_missing_existing_row_is_not_delete(self):
        self.edit(lambda wb: [setattr(wb["WBS总表"].cell(7, c), "value", None) for c in range(1, len(excel_fields("wbs")) + 1)])
        self.assertEqual(read_workbook(self.path), {})

    def test_unknown_enum_is_not_silently_dropped(self):
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "status")), "value", "差不多好了"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "invalid_choice")

    def test_actual_cannot_take_planning_status(self):
        self.edit(lambda wb: setattr(wb["实际月历"]["G7"], "value", "可安排"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "invalid_attendance_status")

    def test_formula_input_rejected_and_literal_formula_text_safe(self):
        self.data["wbs"][0]["description"] = '=HYPERLINK("https://example.invalid","literal")'
        self.export()
        wb = load_workbook(self.path)
        cell = wb["WBS总表"].cell(7, self.col("wbs", "description"))
        self.assertEqual(cell.data_type, "s")
        wb.close()
        self.assertEqual(read_workbook(self.path), {})
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "title")), "value", "=1+1"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "formula_not_allowed")

    def test_changed_date_header_rejected(self):
        self.edit(lambda wb: setattr(wb["计划月历"]["D5"], "value", date(2026, 11, 2)))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "calendar_header_changed")

    def test_unknown_sheet_and_column_rejected(self):
        self.edit(lambda wb: wb.create_sheet("新表"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "sheet_structure_changed")
        self.export()
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(5, len(excel_fields("wbs")) + 1), "value", "新字段"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "unknown_column")

    def test_readonly_raw_attendance_is_protected_at_import(self):
        self.edit(lambda wb: setattr(wb["每日记录"]["H7"], "value", "篡改原始说明"))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "readonly_changed")

    def test_version_change_rejected(self):
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "version")), "value", 99))
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "readonly_changed")

    def test_foreign_keys_and_typed_dates_roundtrip(self):
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "owner_id")), "value", "PER-2 · 陈越"))
        row = read_workbook(self.path)["wbs"][0]
        self.assertEqual(row["owner_id"], "PER-2")
        wb = load_workbook(self.path)
        self.assertIsInstance(wb["日期表"]["C7"].value, datetime)
        self.assertEqual(wb["实际月历"]["H8"].fill.fgColor.rgb[-6:], FUTURE)
        self.assertTrue(any("2006" in str(d.sqref) for d in wb["WBS总表"].data_validations.dataValidation))
        wb.close()

    def test_reference_columns_are_complete_and_ordered(self):
        expected_work = [
            "id", "node_kind", "parent_id", "title", "is_acceptance_node", "stage", "forecast_start",
            "forecast_finish", "planned_duration_days", "member_ids", "actual_start", "actual_finish",
            "actual_duration_days", "actual_person_days", "status", "backup_member_ids",
            "child_count", "child_done_count", "tree_progress_pct",
            "stage_acceptance_result", "related_deliverables", "related_risks", "risk_count",
            "attendance_summary", "todo_list", "todo_count", "todo_done_count", "todo_progress_pct",
            "project_id", "todo_completed_aux", "task_count_aux"]
        expected_delivery = ["work_item_id", "project_id", "title", "document_url", "submitted_by_id",
                             "submitted_at", "acceptor_id", "acceptance_status", "acceptance_comments", "pm_feishu_url"]
        wb = load_workbook(self.path)
        try:
            actual_work=[wb["WBS总表"].cell(6,c).value for c in range(1,wb["WBS总表"].max_column+1)]
            self.assertTrue(set(expected_work).issubset(actual_work))
            self.assertEqual(actual_work[:12],["id","title","owner_id","status","stage","forecast_start","forecast_finish","progress_note","next_action","next_due_at","delay_reason","remediation_action"])
            self.assertEqual([wb["交付物验收"].cell(6, c).value for c in range(1, 11)], expected_delivery)
            for name in ("todo_completed_aux", "task_count_aux"):
                self.assertTrue(wb["WBS总表"].column_dimensions[get_column_letter(self.col("wbs", name))].hidden)
            self.assertEqual(wb["WBS总表"].cell(7, self.col("wbs", "todo_progress_pct")).number_format, '0.00"%"')
        finally:
            wb.close()

    def test_multiple_members_and_manual_decimal_effort_roundtrip(self):
        def mutate(wb):
            ws = wb["WBS总表"]
            ws.cell(7, self.col("wbs", "member_ids")).value = "PER-1 · 林岚; PER-2 · 陈越"
            ws.cell(7, self.col("wbs", "backup_member_ids")).value = "PER-2"
            ws.cell(7, self.col("wbs", "actual_person_days")).value = 1.5
        self.edit(mutate)
        record = read_workbook(self.path)["wbs"][0]
        self.assertEqual(record["member_ids"], ["PER-1", "PER-2"])
        self.assertEqual(record["backup_member_ids"], ["PER-2"])
        self.assertEqual(record["actual_person_days"], 1.5)
        self.data["wbs"][0].update(member_ids=["PER-1", "PER-2"], backup_member_ids=[], actual_person_days=1.5)
        self.export()
        self.assertEqual(read_workbook(self.path), {})
        wb = load_workbook(self.path)
        try:
            ws = wb["WBS总表"]
            member_cell = ws.cell(7, self.col("wbs", "member_ids"))
            self.assertEqual(member_cell.value, "PER-1 · 林岚; PER-2 · 陈越")
            self.assertTrue(any(member_cell.coordinate in dv and not dv.showErrorMessage
                                for dv in ws.data_validations.dataValidation if dv.type == "list"))
            effort = ws.cell(7, self.col("wbs", "actual_person_days"))
            self.assertTrue(any(effort.coordinate in dv and dv.type == "decimal"
                                for dv in ws.data_validations.dataValidation))
        finally:
            wb.close()

    def test_bad_multi_reference_and_nonfinite_decimal_rejected(self):
        for field, value, code in [
            ("member_ids", "林岚,陈越", "invalid_reference"),
            ("member_ids", "PER-1; PER-1", "duplicate_reference"),
            ("actual_person_days", "NaN", "invalid_decimal"),
            ("actual_person_days", -0.5, "invalid_decimal"),
            ("actual_person_days", True, "invalid_decimal"),
        ]:
            with self.subTest(field=field, value=value):
                self.export()
                self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", field)), "value", value))
                with self.assertRaises(ExcelFormatError) as cm:
                    read_workbook(self.path)
                self.assertEqual(cm.exception.code, code)

    def test_all_derived_fields_are_readonly_and_never_imported(self):
        self.data["people"][1]["feishu_url"] = "https://example.invalid/people/pm"
        self.data["deliverables"] = [{"id": "DEL-1", "project_id": "PRJ-1", "work_item_id": "WI-1",
                                       "title": "接口测试材料", "acceptance_status": "draft", "version": 1}]
        self.export()
        wb = load_workbook(self.path)
        try:
            for table, fields in DERIVED_FIELDS.items():
                ws = wb[TABLES[table]["sheet"]]
                for field in fields:
                    with self.subTest(table=table, field=field.name):
                        cell = ws.cell(7, self.col(table, field.name))
                        self.assertTrue(cell.protection.locked)
                        previous = cell.value
                        cell.value = "修改自动字段"
                        wb.save(self.path)
                        with self.assertRaises(ExcelFormatError) as cm:
                            read_workbook(self.path)
                        self.assertEqual(cm.exception.code, "readonly_changed")
                        self.assertEqual(cm.exception.cell, cell.coordinate)
                        cell.value = previous
            wb.save(self.path)
        finally:
            wb.close()
        self.assertEqual(read_workbook(self.path), {})
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "title")), "value", "仅修改执行事实"))
        record = read_workbook(self.path)["wbs"][0]
        self.assertFalse(set(record).intersection(field.name for field in DERIVED_FIELDS["wbs"]))

    def test_new_row_cannot_enter_calculated_value(self):
        def mutate(wb):
            ws = wb["WBS总表"]
            ws.cell(8, self.col("wbs", "title")).value = "新的子任务"
            ws.cell(8, self.col("wbs", "planned_duration_days")).value = 3
        self.edit(mutate)
        with self.assertRaises(ExcelFormatError) as cm:
            read_workbook(self.path)
        self.assertEqual(cm.exception.code, "readonly_changed")

    def test_duration_leaf_counts_and_snapshot_derived_values(self):
        parent = self.data["wbs"][0]
        parent.update(forecast_start="2026-11-01", forecast_finish="2026-11-08",
                      member_ids=["PER-1", "PER-2"], actual_start="2026-11-01")
        self.data["wbs"].extend([
            {"id": "WI-2", "node_kind": "task", "title": "已完成子待办", "scope": "project", "project_id": "PRJ-1",
             "work_type": "bug", "parent_id": "WI-1", "status": "done", "actual_start": "2026-11-02",
             "actual_finish": "2026-11-03", "version": 1},
            {"id": "WI-3", "node_kind": "task", "title": "未开始子待办", "scope": "project", "project_id": "PRJ-1",
             "work_type": "bug", "parent_id": "WI-1", "status": "intake", "version": 1}])
        self.export()
        wb = load_workbook(self.path)
        try:
            ws = wb["WBS总表"]
            expected = {"planned_duration_days": 8, "actual_duration_days": None,
                        "todo_count": 2, "todo_done_count": 1, "todo_progress_pct": 50,
                        "task_count_aux": 0, "actual_person_days": None}
            for field, value in expected.items():
                self.assertEqual(ws.cell(7, self.col("wbs", field)).value, value, field)
            self.assertEqual(ws.cell(8, self.col("wbs", "actual_duration_days")).value, 2)
            self.assertEqual(ws.cell(8, self.col("wbs", "task_count_aux")).value, 1)
            self.assertEqual(wb["首页"]["B9"].value, 2)
        finally:
            wb.close()
        self.assertEqual(read_workbook(self.path), {})
        # Editable date changes do not require inventing a replacement read-only value.
        self.edit(lambda wb: setattr(wb["WBS总表"].cell(7, self.col("wbs", "forecast_finish")), "value", date(2026, 11, 9)))
        changed = read_workbook(self.path)["wbs"][0]
        self.assertEqual(changed["forecast_finish"], "2026-11-09")
        self.assertNotIn("planned_duration_days", changed)

    def test_pm_feishu_is_derived_from_project_roster(self):
        self.data["people"][1]["feishu_url"] = "https://example.invalid/people/pm"
        self.data["deliverables"] = [{"id": "DEL-1", "project_id": "PRJ-1", "work_item_id": "WI-1",
                                       "title": "接口测试材料", "acceptance_status": "draft", "version": 1}]
        self.export()
        wb = load_workbook(self.path)
        try:
            cell = wb["交付物验收"].cell(7, self.col("deliverables", "pm_feishu_url"))
            self.assertEqual(cell.value, "https://example.invalid/people/pm")
            self.assertEqual(cell.hyperlink.target, cell.value)
            self.assertTrue(cell.protection.locked)
        finally:
            wb.close()
        self.assertEqual(read_workbook(self.path), {})

    def test_calendar_gap_fails_without_inventing_day(self):
        data = deepcopy(self.data)
        data["calendar_days"] = [r for r in data["calendar_days"] if r["date"] != "2026-11-10"]
        with self.assertRaises(ExcelFormatError) as cm:
            self.export(data)
        self.assertEqual(cm.exception.code, "calendar_gap")

    def test_empty_people_calendar_roundtrip(self):
        data = deepcopy(self.data)
        data["people"], data["day_records"], data["wbs"] = [], [], []
        self.export(data)
        self.assertEqual(read_workbook(self.path), {})


if __name__ == "__main__":
    unittest.main()
