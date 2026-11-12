"""Calendar business-boundary tests; no network or installed test framework needed."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pm_core.calendar import (
    CalendarValidationError,
    build_calendar,
    current_local_date,
    load_calendar_overrides,
    workday_distance,
)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.verified_holiday = {
            "date": "2026-01-01", "is_workday": False, "day_type": "holiday",
            "label": "元旦", "source_url": "https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm",
            "verified_at": "2026-11-05T09:00:00+08:00",
        }

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(CalendarValidationError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        json.dumps(caught.exception.to_dict(), ensure_ascii=False)
        return caught.exception

    def test_calendar_covers_full_month_and_next_month_including_weekends(self):
        rows = build_calendar("2026-11-01", "2026-12-01", "Asia/Shanghai")
        self.assertEqual(len(rows), 31)
        self.assertEqual(rows[0]["weekday"], 7)
        self.assertFalse(rows[0]["is_workday"])
        self.assertEqual(rows[-1]["date"], "2026-12-01")

    def test_leap_day_and_cross_year_are_real_dates(self):
        leap = build_calendar("2028-02-28", "2028-03-01", "UTC")
        self.assertEqual([r["date"] for r in leap], ["2028-02-28", "2028-02-29", "2028-03-01"])
        rows = build_calendar("2026-12-31", "2027-01-02", "UTC")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["verification_status"], "default_unverified")
        self.assertEqual(rows[1]["day_type"], "default_workday")
        self.assertEqual(rows[1]["source_url"], "")

    def test_today_uses_project_timezone_at_midnight_boundary(self):
        instant = datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(current_local_date("Asia/Shanghai", instant), "2026-11-05")
        self.assertEqual(current_local_date("America/Vancouver", instant), "2026-11-04")
        later = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(current_local_date("Asia/Shanghai", later), "2026-11-06")

    def test_clock_rejects_naive_or_non_datetime_values(self):
        for value in [datetime(2026, 11, 5), "2026-11-05", 0]:
            with self.subTest(value=value):
                self.assertCode("invalid_clock", current_local_date, "UTC", value)

    def test_unknown_timezone_is_actionable_error(self):
        error = self.assertCode("invalid_timezone", build_calendar, "2026-01-01", "2026-01-03", "Mars/Olympus")
        self.assertIn("tzdata", str(error))

    def test_official_holiday_and_sunday_makeup_override_defaults(self):
        overrides = [dict(self.verified_holiday, date=f"2026-01-0{day}") for day in [1, 2, 3]]
        overrides.append(dict(self.verified_holiday, date="2026-01-04", is_workday=True, day_type="makeup_workday", label="元旦调休上班"))
        rows = build_calendar("2025-12-31", "2026-01-04", "Asia/Shanghai", overrides)
        self.assertTrue(rows[-1]["is_workday"])
        self.assertEqual(rows[-1]["weekday"], 7)
        self.assertEqual(rows[-1]["verification_status"], "verified")
        self.assertEqual(workday_distance("2025-12-31", "2026-01-04", rows), 1)
        # An imported exception does not claim all other dates were verified.
        self.assertEqual(rows[0]["verification_status"], "default_unverified")

    def test_company_override_can_make_weekend_work_or_weekday_rest(self):
        overrides = [
            {"date": "2026-11-07", "is_workday": True, "day_type": "company_override", "label": "项目演练拟安排"},
            {"date": "2026-11-09", "is_workday": False, "day_type": "company_override", "label": "公司休息安排待确认"},
        ]
        rows = build_calendar("2026-11-07", "2026-11-09", "Asia/Shanghai", overrides)
        self.assertTrue(rows[0]["is_workday"])
        self.assertFalse(rows[-1]["is_workday"])
        self.assertEqual(rows[0]["verification_status"], "draft")

    def test_explicit_draft_is_not_silently_promoted_by_evidence(self):
        rows = build_calendar("2026-01-01", "2026-01-01", "UTC", [dict(self.verified_holiday, verification_status="draft")])
        self.assertEqual(rows[0]["verification_status"], "draft")

    def test_missing_provenance_never_marks_verified(self):
        self.assertCode(
            "missing_verification_evidence", build_calendar, "2026-01-01", "2026-01-01", "UTC",
            [{"date": "2026-01-01", "is_workday": False, "day_type": "holiday", "verification_status": "verified"}],
        )

    def test_partial_provenance_is_rejected(self):
        for field in ["source_url", "verified_at"]:
            override = dict(self.verified_holiday, **{field: ""})
            with self.subTest(field=field):
                self.assertCode("incomplete_provenance", build_calendar, "2026-01-01", "2026-01-01", "UTC", [override])

    def test_invalid_record_fields_have_structured_location(self):
        cases = [
            ({"date": "2026-02-30"}, "invalid_date"),
            ({"date": "20260101"}, "invalid_date"),
            ({"is_workday": "false"}, "invalid_boolean"),
            ({"is_workday": 0}, "invalid_boolean"),
            ({"is_workday": True}, "day_type_conflict"),
            ({"day_type": "bank_holiday"}, "invalid_day_type"),
            ({"source_url": "javascript:alert(1)"}, "invalid_source_url"),
            ({"source_url": "https://name:password@example.test/calendar"}, "invalid_source_url"),
            ({"verified_at": "2026-11-05T09:00:00"}, "invalid_verified_at"),
            ({"verification_status": "approved"}, "invalid_verification_status"),
            ({"label": 12}, "invalid_text"),
        ]
        for patch, code in cases:
            with self.subTest(patch=patch):
                error = self.assertCode(code, build_calendar, "2026-01-01", "2026-12-31", "UTC", [dict(self.verified_holiday, **patch)])
                self.assertEqual(error.to_dict()["row"], 1)

    def test_verified_timestamp_accepts_explicit_timezone(self):
        rows = build_calendar("2026-01-01", "2026-01-01", "UTC", [dict(self.verified_holiday, verified_at="2026-11-05T01:00:00Z")])
        self.assertEqual(rows[0]["verified_at"], "2026-11-05T01:00:00+00:00")

    def test_verification_date_only_is_rejected_by_build_and_csv(self):
        error = self.assertCode(
            "invalid_verified_at", build_calendar, "2026-01-01", "2026-01-01", "UTC",
            [dict(self.verified_holiday, verified_at="2026-11-05")],
        )
        self.assertEqual(error.field, "verified_at")
        self.assertIn("不接受仅日期", error.message)
        path = self.write_csv(
            "date,is_workday,day_type,source_url,verified_at\n"
            "2026-01-01,false,holiday,https://example.test/calendar,2026-11-05\n"
        )
        error = self.assertCode("invalid_verified_at", load_calendar_overrides, path)
        self.assertEqual(error.to_dict()["row"], 2)
        self.assertEqual(error.field, "verified_at")

    def test_unknown_fields_and_missing_required_fields_are_not_ignored(self):
        self.assertCode("unknown_field", build_calendar, "2026-01-01", "2026-01-01", "UTC", [dict(self.verified_holiday, weekdays=True)])
        self.assertCode("missing_field", build_calendar, "2026-01-01", "2026-01-01", "UTC", [{"date": "2026-01-01"}])

    def test_duplicate_and_outside_range_override_are_rejected(self):
        error = self.assertCode("duplicate_date", build_calendar, "2026-01-01", "2026-01-03", "UTC", [self.verified_holiday, self.verified_holiday])
        self.assertEqual(error.to_dict()["details"]["first_row"], 1)
        self.assertCode("override_out_of_range", build_calendar, "2026-02-01", "2026-02-03", "UTC", [self.verified_holiday])

    def test_reversed_range_and_wrong_date_type_fail(self):
        self.assertCode("reversed_range", build_calendar, "2026-01-04", "2026-01-01", "UTC")
        self.assertCode("invalid_date", build_calendar, None, "2026-01-01", "UTC")
        self.assertCode("invalid_overrides", build_calendar, "2026-01-01", "2026-01-01", "UTC", {"date": "2026-01-01"})

    def test_end_of_date_domain_has_no_overflow(self):
        rows = build_calendar("9999-12-31", "9999-12-31", "UTC")
        self.assertEqual(rows[0]["date"], "9999-12-31")

    def test_inputs_are_not_mutated(self):
        overrides = [copy.deepcopy(self.verified_holiday)]
        original = copy.deepcopy(overrides)
        rows = build_calendar("2026-01-01", "2026-01-03", "UTC", overrides)
        rows[0]["label"] = "changed result"
        self.assertEqual(overrides, original)

    def test_workday_variance_is_signed_and_not_natural_day_variance(self):
        rows = build_calendar("2026-11-27", "2026-12-01", "Asia/Shanghai")
        self.assertEqual(workday_distance("2026-11-27", "2026-12-01", rows), 2)
        self.assertEqual(workday_distance("2026-12-01", "2026-11-27", rows), -2)
        self.assertEqual(workday_distance("2026-11-27", "2026-11-27", rows), 0)

    def test_workday_calculation_rejects_missing_dates_instead_of_guessing(self):
        rows = build_calendar("2026-11-27", "2026-12-01", "UTC")
        del rows[2]
        error = self.assertCode("missing_calendar_dates", workday_distance, "2026-11-27", "2026-12-01", rows)
        self.assertEqual(error.to_dict()["details"]["missing_dates"], ["2026-11-29"])
        self.assertCode("missing_calendar_dates", workday_distance, "2026-11-27", "2026-11-27", [])

    def test_workday_calculation_rejects_duplicate_and_text_boolean(self):
        rows = build_calendar("2026-11-27", "2026-12-01", "UTC")
        self.assertCode("duplicate_date", workday_distance, "2026-11-27", "2026-12-01", rows + rows[:1])
        rows[0]["is_workday"] = "false"
        self.assertCode("invalid_boolean", workday_distance, "2026-11-27", "2026-12-01", rows)

    def write_csv(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "calendar.csv"
        path.write_text(text, encoding="utf-8-sig")
        return path

    def test_csv_import_bom_boolean_and_provenance(self):
        path = self.write_csv(
            "date,is_workday,day_type,label,source_url,verified_at\n"
            "2026-01-01,0,holiday,元旦,https://example.test/calendar,2026-11-05T09:00:00+08:00\n"
            "2026-01-04,TRUE,makeup_workday,拟调休上班,,\n"
        )
        rows = load_calendar_overrides(path)
        self.assertFalse(rows[0]["is_workday"])
        self.assertEqual(rows[0]["verification_status"], "verified")
        self.assertTrue(rows[1]["is_workday"])
        self.assertEqual(rows[1]["verification_status"], "draft")
        self.assertEqual(len(build_calendar("2026-01-01", "2026-01-04", "UTC", rows)), 4)

    def test_csv_preserves_draft_with_source_and_ignores_blank_line(self):
        path = self.write_csv(
            "date,is_workday,day_type,source_url,verified_at,verification_status\n\n"
            "2026-01-01,false,holiday,https://example.test/calendar,2026-11-05T09:00:00+08:00,draft\n"
        )
        self.assertEqual(load_calendar_overrides(path)[0]["verification_status"], "draft")

    def test_csv_typo_duplicate_bad_boolean_and_missing_cell_fail(self):
        cases = [
            ("date,is_workday,is_workday\n", "duplicate_column"),
            ("date,is_workday,day_typo\n", "invalid_header"),
            ("date,is_workday,day_type\n2026-01-01,no,holiday\n", "invalid_boolean"),
            ("date,is_workday,day_type\n2026-01-01,false\n", "csv_row_width"),
            ("date,is_workday,day_type\n2026-01-01,false,holiday\n2026-01-01,true,company_override\n", "duplicate_date"),
        ]
        for contents, code in cases:
            with self.subTest(code=code):
                error = self.assertCode(code, load_calendar_overrides, self.write_csv(contents))
                self.assertIn("row", error.to_dict())


if __name__ == "__main__":
    unittest.main()
