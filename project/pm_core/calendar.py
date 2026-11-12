"""Date-based project calendars, with explicit provenance and no guessed holidays.

The host clock plus an IANA project timezone determines today's local date.
External sources are needed for holiday/makeup-day facts, not for deriving dates
or weekdays.  ``verified`` means the importer supplied a verification record;
this module does not fetch or authenticate the referenced source.
``verified_at`` must include an explicit time and timezone offset; a date alone
is rejected rather than being converted to an invented midnight timestamp.

JSON overrides use real booleans.  CSV files may use true/false or 1/0, and may
include the optional ``verification_status`` column to retain a draft even when
its evidence has already been collected.  A draft override is usable for planning
but remains visibly distinct from a verified record.  Default weekdays/weekends
are always ``default_unverified``; importing exceptions does not certify a year.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_CALENDAR_DAYS = 36600
OVERRIDE_DAY_TYPES = frozenset({"holiday", "makeup_workday", "company_override"})
OVERRIDE_FIELDS = frozenset(
    {"date", "is_workday", "day_type", "label", "source_url", "verified_at", "verification_status"}
)
REQUIRED_OVERRIDE_FIELDS = frozenset({"date", "is_workday", "day_type"})


class CalendarValidationError(ValueError):
    """A stable, JSON-serializable validation error for an API or Excel importer."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        row: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.row = row
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            result["field"] = self.field
        if self.row is not None:
            result["row"] = self.row
        if self.details is not None:
            result["details"] = self.details
        return result


def _parse_date(value: Any, field: str, row: int | None = None) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise CalendarValidationError(
            "invalid_date", "日期必须是有效的 YYYY-MM-DD 字符串。", field=field, row=row
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CalendarValidationError(
            "invalid_date", f"无效日期：{value}。", field=field, row=row
        ) from exc


def _project_zone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CalendarValidationError(
            "invalid_timezone", "时区必须是 IANA 名称，例如 Asia/Shanghai。", field="timezone"
        )
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CalendarValidationError(
            "invalid_timezone",
            f"无法识别时区 {value}；请核对 IANA 名称。系统缺少时区数据库时请安装 tzdata。",
            field="timezone",
        ) from exc


def _optional_text(record: dict[str, Any], field: str, row: int) -> str:
    value = record.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CalendarValidationError(
            "invalid_text", "该字段必须是文本或空值。", field=field, row=row
        )
    return value.strip()


def _validate_verification_timestamp(value: str, row: int) -> str:
    try:
        # Match the repository's datetime field without guessing time precision.
        if "T" not in value:
            raise ValueError("missing ISO time separator")
        timestamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timezone required")
        return timestamp.isoformat()
    except ValueError as exc:
        raise CalendarValidationError(
            "invalid_verified_at",
            "核验时间必须是带时区的 ISO 日期时间，例如 2026-11-05T09:00:00+08:00；不接受仅日期，也不推断午夜。",
            field="verified_at",
            row=row,
        ) from exc


def _normalize_override(record: Any, row: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise CalendarValidationError("invalid_record", "每条日历覆盖须是对象。", row=row)
    missing = sorted(REQUIRED_OVERRIDE_FIELDS - record.keys())
    if missing:
        raise CalendarValidationError(
            "missing_field", "日历覆盖缺少必填字段。", row=row, details={"fields": missing}
        )
    unknown = sorted(str(key) for key in record.keys() - OVERRIDE_FIELDS)
    if unknown:
        raise CalendarValidationError(
            "unknown_field", "日历覆盖包含未知字段。", row=row, details={"fields": unknown}
        )
    day = _parse_date(record["date"], "date", row)
    is_workday = record["is_workday"]
    if type(is_workday) is not bool:
        raise CalendarValidationError(
            "invalid_boolean", "JSON 中 is_workday 必须是 true 或 false，不能是数字或文本。",
            field="is_workday", row=row,
        )
    day_type = record["day_type"]
    if not isinstance(day_type, str) or day_type not in OVERRIDE_DAY_TYPES:
        raise CalendarValidationError(
            "invalid_day_type", "覆盖类型须为 holiday、makeup_workday 或 company_override。",
            field="day_type", row=row,
        )
    if (day_type == "holiday" and is_workday) or (day_type == "makeup_workday" and not is_workday):
        raise CalendarValidationError(
            "day_type_conflict", "holiday 必须为非工作日，makeup_workday 必须为工作日。",
            field="is_workday", row=row,
        )
    label = _optional_text(record, "label", row)
    source = _optional_text(record, "source_url", row)
    verified_at = _optional_text(record, "verified_at", row)
    status = _optional_text(record, "verification_status", row)
    if status and status not in {"draft", "verified"}:
        raise CalendarValidationError(
            "invalid_verification_status", "覆盖的核验状态只能是 draft 或 verified。",
            field="verification_status", row=row,
        )
    if bool(source) != bool(verified_at):
        raise CalendarValidationError(
            "incomplete_provenance", "来源地址与核验时间必须同时填写，或同时留空作为草稿。",
            field="source_url" if not source else "verified_at", row=row,
        )
    if source:
        try:
            parsed = urlsplit(source)
            valid_source = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not any(char.isspace() for char in source)
            )
        except ValueError:
            valid_source = False
        if not valid_source:
            raise CalendarValidationError(
                "invalid_source_url", "来源须为不含登录凭据的有效 HTTP(S) 地址。",
                field="source_url", row=row,
            )
        verified_at = _validate_verification_timestamp(verified_at, row)
    if status == "verified" and not source:
        raise CalendarValidationError(
            "missing_verification_evidence", "已核验记录必须具备来源地址与核验时间。",
            field="verification_status", row=row,
        )
    status = status or ("verified" if source else "draft")
    return {
        "date": day.isoformat(),
        "is_workday": is_workday,
        "day_type": day_type,
        "label": label,
        "source_url": source,
        "verified_at": verified_at,
        "verification_status": status,
    }


def _validate_range(start: date, end: date) -> None:
    if end < start:
        raise CalendarValidationError(
            "reversed_range", "结束日期不得早于开始日期。", field="end_date"
        )
    if (end - start).days + 1 > MAX_CALENDAR_DAYS:
        raise CalendarValidationError(
            "range_too_large", f"单次日历范围不能超过 {MAX_CALENDAR_DAYS} 天，请分段生成。",
            field="end_date",
        )


def current_local_date(timezone: str, now: datetime | None = None) -> str:
    """Return today's YYYY-MM-DD in the project timezone, from a real clock.

    ``now`` is an optional aware datetime for deterministic testing or replay.
    Naive datetime values are rejected instead of guessing the host timezone.
    """
    zone = _project_zone(timezone)
    if now is None:
        now = datetime.now(datetime_timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise CalendarValidationError(
            "invalid_clock", "now 必须是带时区的 datetime，或留空以使用系统时钟。", field="now"
        )
    return now.astimezone(zone).date().isoformat()


def load_calendar_overrides(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a UTF-8(/BOM) CSV without changing the file.

    Required columns: date,is_workday,day_type.
    Recommended additional columns: label,source_url,verified_at.
    verified_at must be an ISO datetime with an explicit timezone, never a date.
    Optional: verification_status (draft or verified).
    CSV booleans are true/false or 1/0, case-insensitive.  Rows are numbered by
    their physical CSV line; blank lines are ignored.  Duplicate dates fail.
    """
    try:
        csv_path = Path(path)
        handle = csv_path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, TypeError, ValueError) as exc:
        raise CalendarValidationError("calendar_file_error", f"无法读取日历 CSV：{exc}。", field="path") from exc
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    try:
        with handle:
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)
            if not header:
                raise CalendarValidationError("missing_header", "日历 CSV 必须包含表头。", row=1)
            header = [item.strip() for item in header]
            if len(set(header)) != len(header):
                raise CalendarValidationError("duplicate_column", "CSV 表头有重复字段。", row=1)
            missing = sorted(REQUIRED_OVERRIDE_FIELDS - set(header))
            unknown = sorted(set(header) - OVERRIDE_FIELDS)
            if missing or unknown:
                raise CalendarValidationError(
                    "invalid_header", "CSV 表头字段不符合日历导入格式。", row=1,
                    details={"missing": missing, "unknown": unknown},
                )
            for values in reader:
                line = reader.line_num
                if not values or all(not item.strip() for item in values):
                    continue
                if len(values) != len(header):
                    raise CalendarValidationError("csv_row_width", "CSV 行的字段数量与表头不一致。", row=line)
                record: dict[str, Any] = dict(zip(header, (item.strip() for item in values)))
                boolean = record["is_workday"].lower()
                if boolean not in {"true", "false", "1", "0"}:
                    raise CalendarValidationError(
                        "invalid_boolean", "CSV 中 is_workday 只接受 true、false、1、0。",
                        field="is_workday", row=line,
                    )
                record["is_workday"] = boolean in {"true", "1"}
                normalized = _normalize_override(record, line)
                if normalized["date"] in seen:
                    raise CalendarValidationError(
                        "duplicate_date", "同一日期只能有一条覆盖记录，请先解决冲突。",
                        field="date", row=line,
                        details={"date": normalized["date"], "first_row": seen[normalized["date"]]},
                    )
                seen[normalized["date"]] = line
                rows.append(normalized)
    except (csv.Error, UnicodeError, OSError) as exc:
        raise CalendarValidationError("calendar_csv_error", f"日历 CSV 格式或编码错误：{exc}。") from exc
    return rows


def build_calendar(
    start_date: str,
    end_date: str,
    timezone: str,
    overrides: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build one row per real date, including weekends, over an inclusive range.

    Defaults assume Monday-Friday work, explicitly unverified.  Holidays and
    company arrangements must come from dated overrides.  No year is extrapolated
    from another year.  The timezone is validated; dates themselves are project-
    local calendar dates, not UTC instants.  The input objects are never mutated.
    """
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    _validate_range(start, end)
    _project_zone(timezone)
    if overrides is None:
        overrides = []
    if not isinstance(overrides, list):
        raise CalendarValidationError("invalid_overrides", "overrides 必须是对象数组或空值。", field="overrides")
    by_date: dict[str, dict[str, Any]] = {}
    source_rows: dict[str, int] = {}
    for index, record in enumerate(overrides, start=1):
        normalized = _normalize_override(record, index)
        key = normalized["date"]
        if key in by_date:
            raise CalendarValidationError(
                "duplicate_date", "同一日期只能有一条覆盖记录，请先解决冲突。", field="date", row=index,
                details={"date": key, "first_row": source_rows[key]},
            )
        if not start_date <= key <= end_date:
            raise CalendarValidationError(
                "override_out_of_range", "覆盖日期不在本次生成范围内；请扩大范围或筛选导入记录。",
                field="date", row=index, details={"date": key, "start_date": start_date, "end_date": end_date},
            )
        by_date[key] = normalized
        source_rows[key] = index
    result: list[dict[str, Any]] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        key = day.isoformat()
        weekday = day.isoweekday()
        if key in by_date:
            result.append({**by_date[key], "weekday": weekday})
        else:
            is_workday = weekday <= 5
            result.append({
                "date": key,
                "weekday": weekday,
                "is_workday": is_workday,
                "day_type": "default_workday" if is_workday else "default_weekend",
                "label": "默认工作日（未核验）" if is_workday else "默认周末（未核验）",
                "source_url": "",
                "verified_at": "",
                "verification_status": "default_unverified",
            })
    return result


def workday_distance(start: str, end: str, calendar_rows: list[dict[str, Any]]) -> int:
    """Signed working-day difference, using supplied calendar facts only.

    For start < end, count workdays in (start, end].  For start > end, negate
    the same forward interval, so f(a,b) == -f(b,a).  The full closed interval
    [min(start,end), max(start,end)] must exist, including a same-day request.
    Verification status is not upgraded by this calculation: callers must retain
    any assumption/draft flags when displaying a forecast or date variance.
    """
    start_day = _parse_date(start, "start")
    end_day = _parse_date(end, "end")
    lower, upper = sorted((start_day, end_day))
    _validate_range(lower, upper)
    if not isinstance(calendar_rows, list):
        raise CalendarValidationError("invalid_calendar", "calendar_rows 必须是日期对象数组。", field="calendar_rows")
    workdays: dict[str, bool] = {}
    for index, record in enumerate(calendar_rows, start=1):
        if not isinstance(record, dict):
            raise CalendarValidationError("invalid_record", "日历行必须是对象。", row=index)
        key = _parse_date(record.get("date"), "date", index).isoformat()
        if type(record.get("is_workday")) is not bool:
            raise CalendarValidationError(
                "invalid_boolean", "日历行的 is_workday 必须是布尔值。", field="is_workday", row=index
            )
        if key in workdays:
            raise CalendarValidationError("duplicate_date", "日历含有重复日期。", field="date", row=index, details={"date": key})
        workdays[key] = record["is_workday"]
    interval = [(lower + timedelta(days=index)).isoformat() for index in range((upper - lower).days + 1)]
    missing = [day for day in interval if day not in workdays]
    if missing:
        raise CalendarValidationError(
            "missing_calendar_dates", "计算区间缺少日历日期，不能按普通工作周猜测。", field="calendar_rows",
            details={"missing_dates": missing[:20], "missing_count": len(missing)},
        )
    count = sum(workdays[day] for day in interval[1:])
    return -count if end_day < start_day else count
