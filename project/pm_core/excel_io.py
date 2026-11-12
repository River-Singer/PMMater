"""Typed Excel input adapter and calendar views for single-project management.

Excel is an editable export of the database, not a second database.  Import
returns an optimistic-lock batch containing only changed and newly added rows.
Absence of a row never requests deletion.  All business and time validation is
performed again by Store.apply_batch; Excel validation is a usability feature.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
from math import ceil, isfinite
from pathlib import Path
import json
import os
import re
import tempfile
import uuid
from unicodedata import east_asian_width
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.defined_name import DefinedName

from .schema import TABLES, ORDER, ENUMS, Field, catalogue
from .tracking import tracking_data, DELAY_LABELS


class ExcelFormatError(ValueError):
    """A precise input error that is safe to display to a workbook editor."""
    def __init__(self, message, *, code="excel_format", sheet=None, cell=None):
        self.code, self.sheet, self.cell = code, sheet, cell
        self.message = str(message)
        where = " / ".join(str(x) for x in (sheet, cell) if x)
        super().__init__(f"{where}: {self.message}" if where else self.message)

    def to_dict(self):
        return {"ok": False, "errors": [{"code": self.code, "sheet": self.sheet,
                                          "cell": self.cell, "message": self.message}]}


CONTRACT = "PM_WORKBENCH_EXCEL_V7"
META_SHEET = "_meta"
LIST_SHEET = "_lists"
GRID_NAMES = {"planned": "计划月历", "actual": "实际月历"}
PLANNED_STATUSES = ("available", "other_work", "travel", "leave", "off", "unavailable", "unknown")
ACTUAL_STATUSES = ("onsite", "remote", "travel", "leave", "off", "unknown")
CATALOGUE = catalogue()
SCHEMA_HASH = sha256(json.dumps(CATALOGUE, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
DERIVED_FIELDS = {table: [Field(**{key: value for key, value in spec.items()
                                 if key in Field.__dataclass_fields__}) for spec in specs]
                  for table, specs in CATALOGUE.get("derived_fields", {}).items()}
REFERENCE_ORDER = {
    "wbs": ("id", "node_kind", "parent_id", "title", "is_acceptance_node", "stage", "forecast_start",
            "forecast_finish", "planned_duration_days", "member_ids", "actual_start", "actual_finish",
            "actual_duration_days", "actual_person_days", "status", "backup_member_ids",
            "child_count", "child_done_count", "tree_progress_pct",
            "stage_acceptance_result", "related_deliverables", "related_risks", "risk_count",
            "attendance_summary", "todo_list", "todo_count", "todo_done_count", "todo_progress_pct",
            "project_id", "todo_completed_aux", "task_count_aux"),
    "deliverables": ("work_item_id", "project_id", "title", "document_url", "submitted_by_id", "submitted_at",
                     "acceptor_id", "acceptance_status", "acceptance_comments", "pm_feishu_url"),
}
EVERYDAY_FIELDS = ("id","title","owner_id","status","stage","forecast_start","forecast_finish","progress_note","next_action","next_due_at","delay_reason","remediation_action")
REFERENCE_ORDER.update({"wbs":EVERYDAY_FIELDS,"operations":EVERYDAY_FIELDS})
HEADER_ROW, KEY_ROW, FIRST_ROW = 5, 6, 7
MAX_EXCEL_ROW = 1048576
MIN_VALIDATION_ROWS = 2000
FONT_NAME = "微软雅黑"
INK, NAVY, BLUE, TEAL = "20384B", "173C55", "315F7D", "277B79"
MUTED, PALE, WHITE = "647A8D", "F3F6F8", "FFFFFF"
LINE, WEEKEND, HOLIDAY, MAKEUP, FUTURE = "DDE5EB", "EDF1F4", "FFF0D8", "E3F2F0", "E4E8EC"
FILL = {"基本": NAVY, "排期": BLUE, "完成": TEAL, "协调": BLUE,
        "补充": MUTED, "分级": MUTED, "来源": MUTED, "变更": MUTED, "系统": MUTED}
COLLAPSED_GROUPS = {"补充", "分级", "来源", "变更", "系统", "辅助"}
READONLY_TABLES = {"services", "participants", "dependencies", "day_records"}
ADVANCED_TABLES = {"calendar_years", "calendar_overrides", "calendars", "calendar_days"}
PROJECT_TABLES = {"operations", "daily_confirmations", "leave_plans", "resource_weeks", "weekly_commitments", "wbs", "meetings", "registers", "interfaces", "project_documents", "deliverables"}
CONTEXT_FIELDS = {
    **{t:{"project_id"} for t in ("operations","daily_confirmations","leave_plans","resource_weeks","weekly_commitments")},
    "projects": {"service_id", "calendar_id"},
    "wbs": {"scope", "project_id", "service_id"},
    "meetings": {"project_id", "service_id"},
    "registers": {"project_id"},
    "interfaces": {"project_id"},
    "project_documents": {"project_id"},
    "deliverables": {"project_id"},
}
EMPTY_GRID_MESSAGE = "先在项目通讯录录入成员，导入后重新导出月历"
TABLE_PURPOSES = {
    "operations":"独立运维、零星优化与配置修改；不计入项目任务完成率。",
    "daily_confirmations":"每天按上午/下午确认地点、投入方式和项目及运维净可用量；未回复不是缺勤。",
    "leave_plans":"未来休假计划独立保存，不代表已经发生的实际出勤。",
    "resource_weeks":"净可用量包含项目及对应运维；已扣请假不能重复扣减，未知留空。",
    "calendar_years":"官方年度公告及完整例外核验，缺年不推测。",
    "calendar_overrides":"公司或指定成员日历例外，确认后才生效。",
    "weekly_commitments":"跨周任务只承诺本周中间成果，不重写任务最终日期。",
    "people": "本项目统一通讯录。员工、供应商及其他参与人共用一张表，维护公司、职能部门、项目职责和联系方式。",
    "calendars": "定义工作日历的适用地区、时区和日期覆盖范围。节假日与调休在日期表维护。",
    "calendar_days": "连续真实日期及工作日规则。工作日由默认周历和明确的节假日、调休、公司例外共同确定。",
    "services": "兼容数据，只读保留。当前项目无需维护产品或服务台账。",
    "projects": "一个工作区维护一个完整项目。填写目标、PO、PM、关键日期、整体结论和业务影响。",
    "wbs": "项目→交付物→功能模块→任务全层级一张表。层级类型定节点种类，父级WBS挂层级树；成员用分号分隔；灰底派生值只读，工期为含首尾自然日，实际人天需手填核实；仅任务行参与执行统计。",
    "meetings": "会议议题与纪要。会前计划与已召开分开；行动记录为关联来源会议的工作项。",
    "participants": "兼容参与关系只读保留。人员与参与窗口统一在项目通讯录维护。",
    "dependencies": "可选技术依赖只读明细。日常等待事项在工作任务记录，需要跟进的事项放入提醒与风险。",
    "registers": "记录提醒、风险、已发生问题、变更和决定。明确优先级、负责人、最迟时间及下一步。",
    "day_records": "只读明细，包含历史月份。日常修改请在计划月历或实际月历完成；说明等高级修改使用Python数据接口。",
    "interfaces": "维护本项目的系统接口：来源、目标、地址、请求方式、用途、负责人和当前状态。只记录鉴权方式，不存密钥。",
    "project_documents": "集中维护项目文档链接，包括飞书文档、需求、设计、测试和验收材料；记录维护人、状态及版本。",
    "deliverables": "记录实际提交和验收。PM飞书地址自动读取通讯录，提交、验收和任务完成分别记录。",
}


def excel_fields(table):
    """Stored and read-only projection fields in the reference workbook order."""
    fields = [*TABLES[table]["fields"], *DERIVED_FIELDS.get(table, [])]
    by_name = {field.name: field for field in fields}
    priority = REFERENCE_ORDER.get(table, ())
    ordered = [by_name[name] for name in priority]
    ordered.extend(field for field in fields if field.name not in priority and field.name != "version")
    if "version" in by_name:
        ordered.append(by_name["version"])
    return ordered


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"不能序列化 {type(value).__name__}")


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _fail(message, sheet=None, cell=None, code="excel_format"):
    raise ExcelFormatError(message, code=code, sheet=sheet, cell=cell)


def _put(cell, value):
    """Write user text as literal text, including text beginning with '='."""
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _normal_value(field, value, sheet=None, cell=None):
    if _empty(value):
        return None
    if field.dtype == "refs":
        if isinstance(value, str):
            entries = [entry.strip() for entry in re.split(r"[;；\n]", value) if entry.strip()]
        elif isinstance(value, list):
            entries = value
        else:
            _fail("多人关联应为编号列表，或使用分号分隔‘编号 · 姓名’", sheet, cell, "invalid_reference")
        result = []
        for entry in entries:
            if not isinstance(entry, str):
                _fail("每个人员关联必须使用文本编号", sheet, cell, "invalid_reference")
            identifier = entry.strip().split(" · ", 1)[0]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", identifier):
                _fail("多人关联请使用分号分隔的稳定编号，不用姓名或逗号代替", sheet, cell, "invalid_reference")
            if identifier in result:
                _fail(f"重复的人员编号：{identifier}", sheet, cell, "duplicate_reference")
            result.append(identifier)
        return result or None
    if field.enum:
        if not isinstance(value, str):
            _fail("请选择文本下拉选项", sheet, cell, "invalid_choice")
        value = value.strip()
        if value in ENUMS[field.enum]:
            return value
        reverse = {v: k for k, v in ENUMS[field.enum].items()}
        if value in reverse:
            return reverse[value]
        _fail(f"无效选项：{value}。请使用该列下拉选项", sheet, cell, "invalid_choice")
    if field.ref:
        if not isinstance(value, str):
            _fail("关联值应为稳定编号，或从下拉选取‘编号 · 名称’", sheet, cell, "invalid_reference")
        value = value.strip().split(" · ", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value):
            _fail("单选关联必须使用稳定编号；多人请填写任务成员列", sheet, cell, "invalid_reference")
        return value
    if field.dtype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            if value.strip().lower() in ("是", "true", "1"):
                return True
            if value.strip().lower() in ("否", "false", "0"):
                return False
        _fail("布尔字段只允许是/否", sheet, cell, "invalid_boolean")
    if field.dtype == "integer":
        if isinstance(value, bool):
            _fail("该字段需要整数", sheet, cell, "invalid_integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value.strip())
        _fail("该字段需要整数，不能填写小数或说明文字", sheet, cell, "invalid_integer")
    if field.dtype == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            _fail("该字段需要非负小数，例如0.5；未知请留空", sheet, cell, "invalid_decimal")
        try:
            number = float(value.strip() if isinstance(value, str) else value)
        except (ValueError, OverflowError):
            _fail("该字段需要非负小数，例如0.5", sheet, cell, "invalid_decimal")
        if not isfinite(number) or number < 0:
            _fail("该字段需要有限非负数，不能填负数、NaN或无穷大", sheet, cell, "invalid_decimal")
        return number
    if field.dtype == "date":
        if isinstance(value, datetime):
            if any((value.hour, value.minute, value.second, value.microsecond)):
                _fail("日期字段不能夹带时间；请使用日期时间字段", sheet, cell, "invalid_date")
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            try:
                return date.fromisoformat(value.strip()).isoformat()
            except ValueError:
                pass
        _fail("请输入真实日期，格式YYYY-MM-DD", sheet, cell, "invalid_date")
    if field.dtype == "datetime":
        if not isinstance(value, str):
            _fail("日期时间使用含时区文本，例如2026-11-05T16:00:00+08:00", sheet, cell, "invalid_datetime")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            return parsed.isoformat()
        except ValueError:
            _fail("日期时间必须真实且包含时区，例如2026-11-05T16:00:00+08:00", sheet, cell, "invalid_datetime")
    if not isinstance(value, str):
        _fail("文本字段请填写文本；编号/电话号码应先设置为文本以保留前导零", sheet, cell, "invalid_text")
    value = value.strip()
    if len(value) > field.max_length:
        _fail(f"文本超过{field.max_length}字符", sheet, cell, "text_too_long")
    if field.name == "id" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value):
        _fail("编号只允许字母数字、下划线、点、冒号和连字符", sheet, cell, "invalid_id")
    return value


def _canonical_data(data):
    unknown = set(data) - set(TABLES)
    if unknown:
        _fail("导出含未知数据表：" + ", ".join(sorted(unknown)))
    result = {}
    for table, meta in TABLES.items():
        rows = []
        seen = set()
        for row in data.get(table, []):
            norm = {f.name: _normal_value(f, row.get(f.name), meta["sheet"], f.name) for f in meta["fields"]}
            if not norm.get("id"):
                _fail("导出数据库记录必须已有编号；新增请在Excel空白行录入", meta["sheet"])
            if norm["id"] in seen:
                _fail("导出记录存在重复编号：" + norm["id"], meta["sheet"])
            seen.add(norm["id"])
            rows.append(norm)
        result[table] = rows
    return result


def _record_label(table, row):
    label = row.get("name") or row.get("title") or row.get("label") or row.get("date") or row.get("code") or row["id"]
    return f"{row['id']} · {label}"


def _font(*, bold=False, color=INK, size=11):
    return Font(name=FONT_NAME, size=size, bold=bold, color=color)


def _display_asof(as_of, timezone_name):
    parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone_name))
    return parsed.strftime("%Y-%m-%d %H:%M") + f"（{timezone_name}）"


def _base_sheet(ws):
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 90
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_options.horizontalCentered = False
    ws.oddFooter.center.text = "第 &P 页 / 共 &N 页"
    ws.sheet_properties.outlinePr.summaryRight = True


def _opening(ws, title, lines, end_col):
    _base_sheet(ws)
    # Titles occupy a free span; actual input columns and headings remain unmerged.
    for r, text in enumerate([title, *lines], 1):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=end_col)
        _put(ws.cell(r, 1), text)
        ws.cell(r, 1).font = _font(size=16 if r == 1 else 11, bold=r == 1,
                                 color=NAVY if r == 1 else MUTED)
        ws.cell(r, 1).alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[r].height = 28 if r == 1 else 32


def _head(cell, value, fill=NAVY):
    _put(cell, value)
    cell.font = _font(color=WHITE, bold=True)
    cell.fill = PatternFill("solid", fgColor=fill)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.border = Border(right=Side(style="thin", color=WHITE))


def _width(field):
    if field.name == "id": return 18
    if field.name == "version": return 8
    if field.dtype == "refs": return 44
    if field.ref: return 24
    if field.dtype == "date": return 15
    if field.dtype == "datetime": return 31
    if field.dtype in ("integer", "boolean", "decimal"): return 16
    if field.dtype == "multiline": return 42
    if field.enum: return max(17, min(32, max(len(v) * 1.7 for v in ENUMS[field.enum].values())))
    if field.name in ("name", "title"): return 34
    if field.name in ("phone",): return 23
    if field.name in ("feishu_url", "url", "spec_url", "address", "document_url", "pm_feishu_url"): return 44
    if field.name == "email": return 32
    return 25


def _column_hidden(table, field):
    if table in ("wbs","operations"):
        return field.name not in EVERYDAY_FIELDS
    return (field.group in COLLAPSED_GROUPS or field.name == "version"
            or field.name in CONTEXT_FIELDS.get(table, set()))


def _wrapped_line_count(value, width):
    """Allow for full-width Chinese glyphs and explicit line breaks."""
    usable = max(8, (width - 2) * .88)
    return sum(max(1, ceil(sum(2 if east_asian_width(char) in {"W", "F"} else 1
                               for char in line) / usable))
               for line in str(value).split("\n"))


def _choice_lists(wb, data):
    ws = wb.create_sheet(LIST_SHEET)
    names = {}
    choices = [("bool", ["是", "否"])]
    choices += [(f"enum_{name}", list(values.values())) for name, values in ENUMS.items()]
    choices += [(f"ref_{table}", [_record_label(table, row) for row in rows]) for table, rows in data.items()]
    choices += [("grid_planned", [ENUMS["attendance_status"][v] for v in PLANNED_STATUSES]),
                ("grid_actual", [ENUMS["attendance_status"][v] for v in ACTUAL_STATUSES])]
    for col, (name, values) in enumerate(choices, 1):
        _put(ws.cell(1, col), name)
        for row, value in enumerate(values or [""], 2):
            _put(ws.cell(row, col), value)
        defined_name = "pm_" + name
        letter = get_column_letter(col)
        wb.defined_names.add(DefinedName(defined_name,
            attr_text=f"'{LIST_SHEET}'!${letter}$2:${letter}${max(2, len(values) + 1)}"))
        names[name] = defined_name
    ws.sheet_state = "veryHidden"
    return names


def _validation(ws, formula, target, prompt="请选择下拉选项；粘贴内容仍将在导入时校验", *, multiple=False):
    dv = DataValidation(type="list", formula1="=" + formula, allow_blank=True)
    dv.errorStyle = "stop"
    dv.showErrorMessage = not multiple
    dv.errorTitle = "输入不符合字段定义"
    dv.error = "请从下拉选项中选择。新关联对象先录入对应主表，再导入并重新导出。"
    dv.showInputMessage = True
    dv.promptTitle = "填写说明"
    dv.prompt = prompt[:250]
    dv.showDropDown = False
    ws.add_data_validation(dv)
    dv.add(target)
    return dv


def _display_value(field, value, labels):
    if value is None:
        return None
    if field.dtype == "refs":
        return "; ".join(labels.get(field.ref, {}).get(item, item) for item in value)
    if field.enum:
        return ENUMS[field.enum][value]
    if field.ref:
        return labels.get(field.ref, {}).get(value, value)
    if field.dtype == "boolean":
        return "是" if value else "否"
    if field.dtype == "date":
        return date.fromisoformat(value)
    if field.name == "delay_state":
        return DELAY_LABELS.get(value, value)
    return value


def _table_sheet(wb, table, rows, names, labels, derived=None):
    meta = TABLES[table]
    fields = excel_fields(table)
    derived = derived or {}
    ws = wb.create_sheet(meta["sheet"])
    readonly = table in READONLY_TABLES
    _opening(ws, meta["sheet"], [TABLE_PURPOSES[table],
        "*为始终必填；点击列分组 + 展开计划、验收及补救责任等条件字段；编号与版本不要修改。",
        "第7行起持续追加。新行自动关联本项目；灰底字段由系统生成，导入拒绝手工修改，更新后重新导出。"], len(fields))
    ws.freeze_panes = "C7" if len(fields) > 6 else "B7"
    ws.row_dimensions[HEADER_ROW].height = 38
    ws.row_dimensions[KEY_ROW].hidden = True
    ws.print_title_rows = "1:6"
    ws.print_title_cols = "A:B"
    # A wide input form must remain readable on screen and when printed.
    # Keep its native font and allow horizontal print pages instead of
    # compressing twenty-plus visible columns to 4pt on one sheet of paper.
    visible_width = sum(_width(f) for f in fields if not _column_hidden(table, f))
    if visible_width > 220:
        ws.sheet_properties.pageSetUpPr.fitToPage = False
        ws.page_setup.fitToWidth = 0
        ws.page_setup.scale = 90
    if readonly or table in ADVANCED_TABLES:
        ws.sheet_state = "hidden"
    last_style_row = max(FIRST_ROW + len(rows) + 14, 25)
    validation_last_row = min(MAX_EXCEL_ROW, FIRST_ROW + max(MIN_VALIDATION_ROWS, len(rows) + 1000) - 1)
    for col, field in enumerate(fields, 1):
        letter = get_column_letter(col)
        _head(ws.cell(HEADER_ROW, col), field.label + (" *" if field.required else ""), FILL.get(field.group, NAVY))
        _put(ws.cell(KEY_ROW, col), field.name)
        ws.column_dimensions[letter].width = _width(field)
        if _column_hidden(table, field):
            ws.column_dimensions[letter].outlineLevel = 1
            ws.column_dimensions[letter].hidden = True
        if not readonly and field.editable:
            choice = "enum_" + field.enum if field.enum else "ref_" + field.ref if field.ref else "bool" if field.dtype == "boolean" else None
            target = f"{letter}{FIRST_ROW}:{letter}{validation_last_row}"
            if choice:
                prompt = ("可选一个编号；多人请用分号分隔‘编号 · 姓名’。Excel下拉不累加多选，也可请智能体按稳定编号维护多人关联。"
                          if field.dtype == "refs" else field.rule or field.widget)
                _validation(ws, names[choice], target, prompt, multiple=field.dtype == "refs")
            elif field.dtype == "date":
                dv = DataValidation(type="date", operator="between", formula1="1", formula2="2958465", allow_blank=True)
                dv.showErrorMessage = True
                dv.errorStyle = "stop"
                dv.errorTitle, dv.error = "请输入真实日期", "日期格式为YYYY-MM-DD。不要将日期和说明混在同一格。"
                ws.add_data_validation(dv)
                dv.add(target)
            elif field.dtype == "integer" and field.editable:
                dv = DataValidation(type="whole", operator="between", formula1="-2147483648", formula2="2147483647", allow_blank=True)
                dv.showErrorMessage = True
                dv.errorStyle = "stop"
                dv.errorTitle, dv.error = "请输入整数", "不能输入小数或文字。业务范围由后端进一步校验。"
                ws.add_data_validation(dv)
                dv.add(target)
            elif field.dtype == "decimal":
                dv = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
                dv.showErrorMessage = True
                dv.errorStyle = "stop"
                dv.errorTitle, dv.error = "请输入非负小数", "例如0.5；未知留空，不从出勤推算任务人天。"
                ws.add_data_validation(dv)
                dv.add(target)
        for row_index in range(FIRST_ROW, last_style_row + 1):
            cell = ws.cell(row_index, col)
            cell.font = _font(color=MUTED if not field.editable else INK)
            cell.fill = PatternFill("solid", fgColor=PALE if not field.editable else WHITE)
            cell.alignment = Alignment(vertical="center", wrap_text=True,
                                       horizontal="right" if field.dtype in ("integer", "decimal") else "center" if field.dtype in ("date", "boolean") else "left",
                                       indent=1 if field.dtype in ("integer", "decimal") else 0)
            cell.protection = Protection(locked=readonly or not field.editable)
            if field.dtype == "date": cell.number_format = "yyyy-mm-dd"
            elif field.dtype == "integer": cell.number_format = "0"
            elif field.name == "todo_progress_pct": cell.number_format = '0.00"%"'
            elif field.dtype == "decimal": cell.number_format = "0.00"
            else: cell.number_format = "@"
    for row_index, record in enumerate(rows, FIRST_ROW):
        visible_lines = 1
        projection = derived.get(record["id"], {})
        display_record = {**record, **{field.name: projection.get(field.name)
                                       for field in DERIVED_FIELDS.get(table, [])}}
        for col, field in enumerate(fields, 1):
            value = _display_value(field, display_record.get(field.name), labels)
            _put(ws.cell(row_index, col), value)
            if isinstance(value, str) and field.name in {"feishu_url", "url", "spec_url", "address", "document_url", "pm_feishu_url"} and re.match(r"^https?://", value, re.I):
                ws.cell(row_index, col).hyperlink = value
                ws.cell(row_index, col).font = Font(name=FONT_NAME, size=11, color=BLUE, underline="single")
            if isinstance(value, str) and not _column_hidden(table, field):
                visible_lines = max(visible_lines, _wrapped_line_count(value, _width(field)))
        ws.row_dimensions[row_index].height = min(409, max(30, visible_lines * 16 + 8))
    for row_index in range(FIRST_ROW + len(rows), last_style_row + 1):
        ws.row_dimensions[row_index].height = 25
    ws.print_area = f"A1:{get_column_letter(len(fields))}{max(FIRST_ROW, FIRST_ROW + len(rows) - 1)}"
    return ws


def _grid_sheet(wb, data, *, kind, month, calendar, days, names, as_of):
    ws = wb.create_sheet(GRID_NAMES[kind])
    dates = [r["date"] for r in days]
    dates_set = set(dates)
    record_index = {}
    for r in data["day_records"]:
        key = (r["person_id"], r["date"], r["period"], r["record_kind"])
        if key in record_index:
            _fail("每日记录联合键重复，不能绘制月历", ws.title)
        record_index[key] = r
    people_with_history = {r["person_id"] for r in data["day_records"] if r["date"] in dates_set}
    people = [r for r in data["people"] if r.get("active") or r["id"] in people_with_history]
    end_col = len(dates) + 3
    _opening(ws, f"{GRID_NAMES[kind]}  {month}", [
        f"日历：{calendar['name']}　数据截至：{_display_asof(as_of, calendar['timezone'])}",
        "每人每天按上午、下午记录。空白表示未录入，不自动判缺勤；节假日与调休依据日期表。",
        "清空已有记录会改为‘待确认’，保留历史。灰底未来时段以导出时间为准，导入时按当前时间重新校验。"], end_col)
    for col, title in enumerate(("人员编号", "姓名", "时段"), 1):
        _head(ws.cell(HEADER_ROW, col), title)
        _head(ws.cell(KEY_ROW, col), "", fill=BLUE)
    ws.column_dimensions["A"].width = 17
    ws.column_dimensions["A"].hidden = True
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 8
    ws.row_dimensions[HEADER_ROW].height = 27
    ws.row_dimensions[KEY_ROW].height = 41
    now = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(ZoneInfo(calendar["timezone"]))
    for col, day in enumerate(days, 4):
        d = date.fromisoformat(day["date"])
        _head(ws.cell(HEADER_ROW, col), d)
        ws.cell(HEADER_ROW, col).number_format = "d"
        label = "一二三四五六日"[d.isoweekday() - 1]
        if day["day_type"] == "makeup_workday": label += "\n调班"
        elif day["day_type"] == "holiday": label += "\n假期"
        elif day["day_type"] == "company_override": label += "\n例外"
        elif not day["is_workday"]: label += "\n休"
        _head(ws.cell(KEY_ROW, col), label, fill=BLUE)
        ws.column_dimensions[get_column_letter(col)].width = 8.5
        if d == now.date():
            ws.cell(HEADER_ROW, col).fill = PatternFill("solid", fgColor=TEAL)
    row_map = []
    for person_index, person in enumerate(people):
        for offset, period in enumerate(("AM", "PM")):
            row = FIRST_ROW + person_index * 2 + offset
            row_map.append({"row": row, "person_id": person["id"], "name": person["name"], "period": period})
            for col, value in enumerate((person["id"], person["name"], ENUMS["period"][period]), 1):
                _put(ws.cell(row, col), value)
                ws.cell(row, col).font = _font()
                ws.cell(row, col).fill = PatternFill("solid", fgColor=PALE)
                ws.cell(row, col).alignment = Alignment(vertical="center", horizontal="left" if col != 3 else "center")
            ws.row_dimensions[row].height = 29
            for col, day in enumerate(days, 4):
                cell = ws.cell(row, col)
                rec = record_index.get((person["id"], day["date"], period, kind))
                _put(cell, ENUMS["attendance_status"][rec["status"]] if rec else None)
                fill = HOLIDAY if day["day_type"] == "holiday" else MAKEUP if day["day_type"] == "makeup_workday" else WHITE if day["is_workday"] else WEEKEND
                d = date.fromisoformat(day["date"])
                if kind == "actual" and (d > now.date() or (d == now.date() and period == "PM" and now.hour < 12)):
                    fill = FUTURE
                cell.fill = PatternFill("solid", fgColor=fill)
                cell.font = _font(size=11)
                cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
                cell.number_format = "@"
                cell.protection = Protection(locked=False)
            if period == "PM":
                for cell in ws[row][:end_col]:
                    cell.border = Border(bottom=Side(style="thin", color=LINE))
    last_row = FIRST_ROW + len(row_map) - 1
    if row_map:
        _validation(ws, names["grid_" + kind], f"D{FIRST_ROW}:{get_column_letter(end_col)}{last_row}")
    else:
        _put(ws.cell(FIRST_ROW, 2), EMPTY_GRID_MESSAGE)
        ws.cell(FIRST_ROW, 2).font = _font(color=MUTED)
        ws.merge_cells(start_row=FIRST_ROW, start_column=2, end_row=FIRST_ROW, end_column=end_col)
    ws.freeze_panes = "D7"
    ws.print_title_rows = "1:6"
    ws.print_area = f"B1:{get_column_letter(end_col)}{max(FIRST_ROW, last_row)}"
    return {"kind": kind, "calendar_id": calendar["id"], "month": month,
            "dates": dates, "rows": row_map,
            "headers": [ws.cell(KEY_ROW, c).value for c in range(4, end_col + 1)]}


def _dictionary(wb):
    ws = wb.create_sheet("字段字典")
    _opening(ws, "字段字典", ["字段定义由同一份Python schema生成，和数据库校验、下拉选项保持一致。",
        "条件必填和关系规则需由后端校验；Excel下拉不能阻止所有粘贴或修改。",
        "本页只读。人员统一在项目通讯录维护；日期是真实日期，日期时间文本必须带时区。"], 8)
    headers = ("数据表", "字段", "中文名称", "类型/控件", "必填/默认", "关联/选项", "条件与规则", "填写分组")
    for col, title in enumerate(headers, 1): _head(ws.cell(5, col), title)
    widths = (19, 25, 22, 23, 23, 48, 68, 13)
    for col, width in enumerate(widths, 1): ws.column_dimensions[get_column_letter(col)].width = width
    row = 6
    for table, meta in TABLES.items():
        for field in excel_fields(table):
            options = (f"{field.ref}.id" + ("；多人用分号分隔" if field.dtype == "refs" else "")
                       if field.ref else "；".join(f"{k}={v}" for k, v in ENUMS[field.enum].items()) if field.enum else "—")
            req = "系统生成／只读" if not field.editable else "是" if field.required else "条件/可选"
            if field.default is not None: req += f"；默认{field.default}"
            values = (meta["sheet"], field.name, field.label, f"{field.dtype} / {field.widget}", req, options, field.rule or "—", field.group)
            for col, value in enumerate(values, 1):
                cell = _put(ws.cell(row, col), value)
                cell.font = _font()
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                if field.name == "id": cell.fill = PatternFill("solid", fgColor=PALE)
            ws.row_dimensions[row].height = 58 if len(options) > 60 else 42
            row += 1
    ws.freeze_panes = "D6"
    ws.auto_filter.ref = f"A5:H{row - 1}"
    ws.print_title_rows = "1:5"
    ws.print_area = f"A1:H{row - 1}"
    return ws


def _home(wb, data, *, calendar, month, as_of, tracking):
    ws = wb.active
    ws.title = "首页"
    project = data["projects"][0] if data["projects"] else {}
    _opening(ws, "pmmaster 项目管理工作台", [
        f"当前项目：{project.get('name') or '请在项目概况填写'}　月历：{month}",
        f"数据截至：{_display_asof(as_of, calendar['timezone'])}",
        "保存修改后，通过工作台导入或Python命令提交，再导出最新Excel；浏览器汇报读取同一数据库。"], 8)
    for col, width in enumerate((24, 24, 24, 23, 23, 24, 24, 24), 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    for col, title in ((1,"叶子任务类型"),(2,"任务数量"),(4,"叶子任务状态"),(5,"任务数量")):
        _head(ws.cell(6, col), title)
    leaf_ids = set(tracking["leaf_ids"])
    items = [item for item in data["wbs"] if item["id"] in leaf_ids]
    for row, (kind, label) in enumerate(ENUMS["work_type"].items(), 7):
        _put(ws.cell(row, 1), label)
        _put(ws.cell(row, 2), sum(r.get("work_type") == kind for r in items) if items else "待录入")
    for row, (state, label) in enumerate(ENUMS["work_status"].items(), 7):
        _put(ws.cell(row, 4), label)
        _put(ws.cell(row, 5), sum(r.get("status") == state for r in items) if items else "待录入")
    summary_row=7+max(len(ENUMS["work_type"]),len(ENUMS["work_status"]))+1
    navigation_row=summary_row+2
    _put(ws.cell(summary_row, 1), "未关闭提醒与风险")
    _put(ws.cell(summary_row, 2), sum(r.get("status") not in {"closed", "rejected"} for r in data["registers"]) if data["registers"] else "未登记")
    _put(ws.cell(summary_row, 4), "未完成P0任务")
    _put(ws.cell(summary_row, 5), sum(r.get("priority") == "P0" and r.get("status") not in {"done", "cancelled"} for r in items) if items else "待录入")
    instructions = [
        ("整体进度", "项目概况", "填写一个项目的目标、PO、PM、关键日期、整体结论与业务影响。"),
        ("项目人员", "项目通讯录", "员工、供应商、业务与其他参与人统一维护职责、参与窗口、电话、飞书和邮箱。"),
        ("交付分解", "WBS总表", "项目、交付物、功能模块与任务全层级一张表；层级类型+父级WBS挂树，仅任务行参与执行统计。"),
        ("运维与零星事项", "运维与优化事项", "单独登记优化、配置修改和运维；延期说明、解决措施与项目交付分开统计。"),
        ("每日签到", "每日工作安排", "非员工确认驻场/远程、专职/兼项及半日可用量；未回复待确认，不推断缺勤。"),
        ("未来休假", "未来休假计划", "维护多日或半日休假、确认状态及交接，不反复复制到每日文本。"),
        ("周资源安排", "人员周资源安排", "确认项目及对应运维净容量、两类分配和缓冲；已扣休假不再重复扣。"),
        ("本周成果", "本周成果承诺", "周一确认成果与期限；跨周任务填中间成果，周五对照结果与证据。"),
        ("交付验收", "交付物验收", "维护提交物、链接、提交时间、验收结果和意见；PM飞书地址自动读取通讯录。"),
        ("计划月历（可选）", "计划月历", "辅助查看半日可用安排；每日地点/兼项与未来休假使用上方专门入口。"),
        ("实际出勤（可选）", "实际月历", "仅需额外实际出勤记录时使用；当天签到安排不能自动写成已经发生的出勤。"),
        ("系统接口", "接口清单", "维护接口地址、请求方式、用途、双方负责人、就绪日期和关联任务。"),
        ("项目资料", "项目文档", "集中登记飞书等文档链接、维护负责人、状态和版本。"),
        ("会议与行动", "会议", "会前议题和会后纪要分开；待办作为工作项关联会议。"),
        ("提醒与风险", "提醒与风险", "记录待跟进内容、风险与问题，明确优先级、处理人、期限和下一步。"),
    ]
    for col, title in enumerate(("使用场景", "入口", "维护内容"), 1): _head(ws.cell(navigation_row, col), title)
    ws.merge_cells(start_row=navigation_row,start_column=3,end_row=navigation_row,end_column=8)
    for row, (scenario, target, note) in enumerate(instructions, navigation_row+1):
        _put(ws.cell(row, 1), scenario)
        _put(ws.cell(row, 2), target)
        ws.cell(row, 2).hyperlink = f"#'{target}'!A1"
        ws.cell(row, 2).font = Font(name=FONT_NAME, size=11, color=BLUE, underline="single")
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=8)
        _put(ws.cell(row, 3), note)
        ws.row_dimensions[row].height = 36
    notices = [
        "首次使用：先填项目通讯录，再填项目概况。新增人员入库后重新导出，下拉选项与月历行才会更新。",
        "任务数量按叶子项统计，父子不重复计数；todo进度不代表项目整体完成度。实际人天需核实填报，不从出勤推算。",
        "高级字段可展开列分组；字段字典在末页。日历配置与日期表为隐藏配置页，节假日和调休据此维护。",
        "下拉预设至少2000行并随重新导出扩展。保留编号、版本和日期表头；隐藏列用于简化界面，不构成权限控制。",
    ]
    notice_start=navigation_row+len(instructions)+3
    for row, text in enumerate(notices, notice_start):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        _put(ws.cell(row, 1), text)
        ws.row_dimensions[row].height = 30
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is not None and cell.row not in (1, 6, navigation_row):
                if not cell.hyperlink: cell.font = _font(color=MUTED if cell.row >= notice_start else INK)
                cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.print_area = f"A1:H{notice_start+len(notices)-1}"
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.tabColor = NAVY


def _cell_fact(cell):
    """Semantic value and Excel type, stable across an XLSX save/reload."""
    value = cell.value
    if _empty(value):
        return ["blank", None]
    if isinstance(value, datetime) and not any((value.hour, value.minute, value.second, value.microsecond)):
        value = value.date().isoformat()
    elif isinstance(value, (date, datetime)):
        value = value.isoformat()
    return [cell.data_type, value]


def _derived_cell_baselines(ws, table, rows):
    columns = {field.name: index for index, field in enumerate(excel_fields(table), 1)}
    return {record["id"]: {field.name: _cell_fact(ws.cell(row, columns[field.name]))
                           for field in DERIVED_FIELDS.get(table, [])}
            for row, record in enumerate(rows, FIRST_ROW)}


def _sheet_value_signature(ws):
    values = []
    for row in ws.iter_rows():
        for cell in row:
            if not _empty(cell.value):
                values.append([cell.coordinate, *_cell_fact(cell)])
    return sha256(_dump(values).encode()).hexdigest()


def export_workbook(data, path, *, month, calendar_id, as_of):
    """Export a typed input workbook and one selected calendar month.

    The full database snapshot remains in hidden metadata for change detection;
    only the chosen month's view is visible, while all historic day records stay
    preserved. Dates must already be generated for the complete selected month.
    """
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        _fail("月份必须为YYYY-MM", code="invalid_month")
    try:
        year, m = map(int, month.split("-"))
        _, n_days = monthrange(year, m)
        start = date(year, m, 1)
    except ValueError:
        _fail("月份无效", code="invalid_month")
    try:
        stamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if stamp.tzinfo is None: raise ValueError
    except (ValueError, AttributeError):
        _fail("数据截至时间必须包含时区", code="invalid_datetime")
    as_of = stamp.isoformat()
    data = _canonical_data(data)
    if len(data["projects"]) > 1:
        _fail("一个工作区只维护一个项目；请先拆分项目数据再导出", code="single_project_required")
    calendar = next((r for r in data["calendars"] if r["id"] == calendar_id), None)
    if not calendar:
        _fail(f"不存在日历：{calendar_id}", code="missing_calendar")
    try:
        ZoneInfo(calendar["timezone"])
    except Exception as exc:
        _fail(f"时区不可用：{calendar['timezone']}。Windows可安装tzdata。{exc}", code="invalid_timezone")
    day_index = {r["date"]: r for r in data["calendar_days"] if r["calendar_id"] == calendar_id}
    dates = [date(year, m, d).isoformat() for d in range(1, n_days + 1)]
    missing = [d for d in dates if d not in day_index]
    if missing:
        _fail(f"所选月份日期表不完整，缺少{missing[0]}等{len(missing)}天；请先生成或导入日历", code="calendar_gap")
    days = [day_index[d] for d in dates]
    labels = {t: {r["id"]: _record_label(t, r) for r in rows} for t, rows in data.items()}
    wb = Workbook()
    wb.properties.title = "pmmaster 项目管理工作台"
    wb.properties.creator = "pmmaster"
    wb.properties.description = "Python和SQLite支持的可编辑Excel前端；含字段定义、关联下拉及真实日期月历。"
    names = _choice_lists(wb, data)
    tracking = tracking_data(data, as_of=as_of)
    projections = {"wbs": tracking["tasks"], "deliverables": tracking.get("deliverables", {})}
    derived_cells = {}
    for table in ORDER:
        sheet_rows = [r for r in data[table] if not r.get("migrated_operation_id")] if table=="wbs" else data[table]
        ws = _table_sheet(wb, table, sheet_rows, names, labels, projections.get(table))
        if table in DERIVED_FIELDS:
            derived_cells[table] = _derived_cell_baselines(ws, table, sheet_rows)
    grids = {}
    for kind in GRID_NAMES:
        grids[GRID_NAMES[kind]] = _grid_sheet(wb, data, kind=kind, month=month, calendar=calendar,
                                            days=days, names=names, as_of=as_of)
    _dictionary(wb)
    _home(wb, data, calendar=calendar, month=month, as_of=as_of, tracking=tracking)
    visible = ["首页", TABLES["projects"]["sheet"], TABLES["wbs"]["sheet"],
               TABLES["operations"]["sheet"], TABLES["daily_confirmations"]["sheet"], TABLES["leave_plans"]["sheet"],
               TABLES["resource_weeks"]["sheet"], TABLES["weekly_commitments"]["sheet"], TABLES["people"]["sheet"], "计划月历", "实际月历",
               TABLES["deliverables"]["sheet"], TABLES["interfaces"]["sheet"], TABLES["project_documents"]["sheet"],
               TABLES["meetings"]["sheet"], TABLES["registers"]["sheet"], "字段字典"]
    front = visible + [TABLES[t]["sheet"] for t in ORDER if TABLES[t]["sheet"] not in visible] + [LIST_SHEET]
    wb._sheets = [wb[s] for s in front]
    for title in ("计划月历", "实际月历"):
        wb[title].sheet_properties.tabColor = BLUE
    for title in (TABLES["projects"]["sheet"], TABLES["people"]["sheet"], TABLES["wbs"]["sheet"]):
        wb[title].sheet_properties.tabColor = TEAL
    wb["字段字典"].sheet_properties.tabColor = MUTED
    readonly = ("首页", "字段字典", *[TABLES[t]["sheet"] for t in ORDER if t in READONLY_TABLES], LIST_SHEET)
    meta = {"contract": CONTRACT, "schema_hash": SCHEMA_HASH, "as_of": as_of,
            "month": month, "calendar_id": calendar_id, "data": data, "grids": grids, "single_project": True,
            "derived_cells": derived_cells,
            "readonly_signatures": {s: _sheet_value_signature(wb[s]) for s in readonly},
            "sheet_names": front + [META_SHEET]}
    raw = _dump(meta)
    ms = wb.create_sheet(META_SHEET)
    _put(ms.cell(1, 1), CONTRACT)
    _put(ms.cell(2, 1), sha256(raw.encode()).hexdigest())
    # Well below Excel's 32,767 UTF-16 code unit limit even for non-BMP names.
    for row, offset in enumerate(range(0, len(raw), 15000), 3):
        _put(ms.cell(row, 1), raw[offset: offset + 15000])
    ms.sheet_state = "veryHidden"
    wb.active = 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=".pm-excel-", suffix=".xlsx", dir=path.parent, delete=False)
    temp = Path(handle.name)
    handle.close()
    try:
        wb.save(temp)
        os.replace(temp, path)
    finally:
        wb.close()
        if temp.exists(): temp.unlink()


def _read_meta(wb):
    if META_SHEET not in wb.sheetnames:
        _fail("缺少导出元数据，不能确认字段版本或修改来源；请从Python重新导出模板", code="missing_metadata")
    ws = wb[META_SHEET]
    if ws.cell(1, 1).value != CONTRACT:
        _fail("Excel模板格式不兼容，请从pmmaster重新导出", META_SHEET, "A1", "schema_version")
    parts = []
    for row in range(3, ws.max_row + 1):
        value = ws.cell(row, 1).value
        if not isinstance(value, str):
            _fail("元数据分块损坏", META_SHEET, f"A{row}", "metadata_corrupt")
        parts.append(value)
    raw = "".join(parts)
    if sha256(raw.encode()).hexdigest() != ws.cell(2, 1).value:
        _fail("元数据校验不一致，不能安全导入", META_SHEET, code="metadata_corrupt")
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        _fail("元数据不是有效JSON", META_SHEET, code="metadata_corrupt")
    if meta.get("contract") != CONTRACT or meta.get("schema_hash") != SCHEMA_HASH:
        _fail("字段定义已变化，请使用当前程序重新导出模板", code="schema_version")
    return meta


def _check_no_formulas(wb):
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    _fail("导入不执行Excel公式。请粘贴已核实的值；要保存以=开头的文字请按文本输入", ws.title, cell.coordinate, "formula_not_allowed")


def _check_table(ws, table):
    fields = excel_fields(table)
    for col, field in enumerate(fields, 1):
        expected = field.label + (" *" if field.required else "")
        if ws.cell(HEADER_ROW, col).value != expected:
            _fail(f"字段标题被改动，应为‘{expected}’", ws.title, f"{get_column_letter(col)}{HEADER_ROW}", "header_changed")
        if ws.cell(KEY_ROW, col).value != field.name:
            _fail(f"字段键被改动，应为{field.name}", ws.title, f"{get_column_letter(col)}{KEY_ROW}", "header_changed")
    for row in ws.iter_rows(min_row=HEADER_ROW, min_col=len(fields) + 1):
        for cell in row:
            if not _empty(cell.value):
                _fail("发现未定义的新列，程序不会静默忽略该列", ws.title, cell.coordinate, "unknown_column")


def _read_table(ws, table, baseline, derived_baseline=None):
    fields = excel_fields(table)
    stored_fields = TABLES[table]["fields"]
    stored_names = {field.name for field in stored_fields}
    columns = {field.name: index for index, field in enumerate(fields, 1)}
    derived_baseline = derived_baseline or {}
    _check_table(ws, table)
    base_index = {r["id"]: r for r in baseline}
    changes = []
    seen = set()
    for row in range(FIRST_ROW, ws.max_row + 1):
        values = [ws.cell(row, c).value for c in range(1, len(fields) + 1)]
        if all(_empty(v) for v in values):
            continue
        record = {f.name: _normal_value(f, values[i], ws.title, f"{get_column_letter(i + 1)}{row}")
                  for i, f in enumerate(fields) if f.name in stored_names}
        record_id = record.get("id")
        if record_id and record_id in seen:
            _fail(f"编号重复：{record_id}", ws.title, f"{get_column_letter(columns['id'])}{row}", "duplicate_id")
        if record_id: seen.add(record_id)
        original = base_index.get(record_id)
        for field in DERIVED_FIELDS.get(table, []):
            cell = ws.cell(row, columns[field.name])
            if original is None:
                if not _empty(cell.value):
                    _fail("派生字段不能手填；请留空，导入后重新导出由Python计算", ws.title,
                          cell.coordinate, "readonly_changed")
            else:
                expected = derived_baseline.get(record_id, {}).get(field.name)
                if expected is None:
                    _fail("派生字段快照缺失，请重新导出", ws.title, cell.coordinate, "metadata_corrupt")
                if _cell_fact(cell) != expected:
                    _fail("只读派生值被修改；请还原导出值，更新任务后重新导出计算", ws.title,
                          cell.coordinate, "readonly_changed")
        if original is None:
            if record.get("version") is not None:
                _fail("新增记录不能填写版本；也不能修改已有记录的编号", ws.title,
                      f"{get_column_letter(columns['version'])}{row}", "identity_changed")
            if table == "wbs" and record.get("scope") is None:
                record["scope"] = "project"
            for field in stored_fields:
                if record[field.name] is None and field.default is not None:
                    record[field.name] = deepcopy(field.default)
            # Core will generate the stable ID and the initial version.
            record.pop("version", None)
            if record_id is None: record.pop("id", None)
            changes.append(record)
        else:
            for field in stored_fields:
                if not field.editable and record[field.name] != original.get(field.name):
                    _fail("系统字段不能修改；请保留导出时的版本", ws.title,
                          f"{get_column_letter(columns[field.name])}{row}", "readonly_changed")
            if any(record[f.name] != original.get(f.name) for f in stored_fields if f.name != "version"):
                record["version"] = original.get("version")
                changes.append(record)
    return changes


def _bind_single_project(changes, baseline, calendar_id):
    """Fill hidden context only on new rows; never rewrite existing facts.

    A new project may be created in the same Excel import as its WBS, tasks,
    interfaces and documents. Give that one project a stable ID before binding
    its new dependent rows, so the PM need not expose implementation columns.
    """
    project_index = {r["id"]: r for r in baseline["projects"]}
    for row in changes.get("projects", []):
        if "version" not in row:
            if not row.get("id"):
                row["id"] = "PRJ-" + uuid.uuid4().hex[:10]
            if row.get("calendar_id") is None:
                row["calendar_id"] = calendar_id
        project_index[row["id"]] = row
    if len(project_index) > 1:
        _fail("一个工作区只维护一个项目，请在项目概况保留同一项目编号更新", TABLES["projects"]["sheet"], code="single_project_required")
    project_id = next(iter(project_index), None)
    for table in PROJECT_TABLES:
        for row in changes.get(table, []):
            if "version" in row:
                continue
            if not project_id:
                _fail("请先在项目概况填写当前项目；也可以在同一文件中填写项目概况后一起导入", TABLES[table]["sheet"], code="missing_project")
            if row.get("project_id") is None:
                row["project_id"] = project_id
            elif row["project_id"] != project_id:
                _fail("新增记录只能关联当前项目，项目编号无需手工修改", TABLES[table]["sheet"], code="foreign_project")
            if table == "wbs" and row.get("scope") != "project":
                _fail("本模板的工作任务统一属于当前项目，请保留隐藏归属字段的默认值", TABLES[table]["sheet"], code="single_project_required")


def _read_grid(ws, info, baseline):
    dates = info["dates"]
    kind = info["kind"]
    allowed = PLANNED_STATUSES if kind == "planned" else ACTUAL_STATUSES
    reverse = {ENUMS["attendance_status"][code]: code for code in allowed}
    date_index = {}
    for col, expected_date in enumerate(dates, 4):
        value = ws.cell(HEADER_ROW, col).value
        if isinstance(value, datetime):
            if any((value.hour, value.minute, value.second, value.microsecond)):
                _fail("月历日期头不能包含时间", ws.title, ws.cell(HEADER_ROW, col).coordinate, "calendar_header_changed")
            value = value.date()
        if not isinstance(value, date) or value.isoformat() != expected_date:
            _fail(f"月历日期头不能修改，应为{expected_date}；切换月份请重新导出", ws.title,
                  ws.cell(HEADER_ROW, col).coordinate, "calendar_header_changed")
        if ws.cell(KEY_ROW, col).value != info["headers"][col - 4]:
            _fail("星期/调休标记被改动，请在日期表维护并重新导出", ws.title,
                  ws.cell(KEY_ROW, col).coordinate, "calendar_header_changed")
        date_index[col] = expected_date
    for col, expected in enumerate(("人员编号", "姓名", "时段"), 1):
        if ws.cell(HEADER_ROW, col).value != expected:
            _fail("月历结构列被改动", ws.title, ws.cell(HEADER_ROW, col).coordinate, "header_changed")
    record_index = {(r["person_id"], r["date"], r["period"], r["record_kind"]): r for r in baseline}
    expected_rows = {r["row"]: r for r in info["rows"]}
    changes = []
    if not expected_rows:
        for row in ws.iter_rows(min_row=FIRST_ROW):
            for cell in row:
                if cell.coordinate == "B7" and cell.value == EMPTY_GRID_MESSAGE:
                    continue
                if not _empty(cell.value):
                    _fail("空月历不能直接新增人员；请先维护项目通讯录并重新导出", ws.title,
                          cell.coordinate, "unknown_calendar_row")
    for row, person in expected_rows.items():
        expected = (person["person_id"], person["name"], ENUMS["period"][person["period"]])
        for col, value in enumerate(expected, 1):
            if ws.cell(row, col).value != value:
                _fail("月历人员/时段键不能修改；新增或调整人员后重新导出月历", ws.title,
                      ws.cell(row, col).coordinate, "calendar_person_changed")
        for col, d in date_index.items():
            cell = ws.cell(row, col)
            key = (person["person_id"], d, person["period"], kind)
            original = record_index.get(key)
            value = cell.value
            if _empty(value):
                if not original:
                    continue
                code = "unknown"
            elif not isinstance(value, str) or value.strip() not in {*reverse, *allowed}:
                _fail("该月历不接受此状态；计划安排和实际出勤使用不同下拉选项", ws.title,
                      cell.coordinate, "invalid_attendance_status")
            else:
                code = reverse.get(value.strip(), value.strip())
            if original and code == original["status"]:
                continue
            if original:
                record = deepcopy(original)
                record["status"] = code
            else:
                record = {"person_id": person["person_id"], "calendar_id": info["calendar_id"],
                          "date": d, "period": person["period"], "record_kind": kind,
                          "status": code, "notes": None}
            changes.append(record)
    # Prevent pasted rows/columns from being silently ignored.
    expected_last_row = max(expected_rows, default=FIRST_ROW)
    last_col = len(dates) + 3
    for row in ws.iter_rows(min_row=HEADER_ROW):
        for cell in row:
            if cell.column > last_col and not _empty(cell.value):
                _fail("月历出现未定义的额外日期列", ws.title, cell.coordinate, "unknown_column")
            if cell.row > expected_last_row and not _empty(cell.value):
                _fail("月历出现额外人员行；请在项目通讯录维护后重新导出", ws.title, cell.coordinate, "unknown_calendar_row")
    return changes


def read_workbook(path):
    """Read only changes/new records; leave missing rows and historic months intact.

    This does not write SQLite or bypass its validations.  Pass the returned
    batch to Store.apply_batch and export a fresh file after a successful import.
    """
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        _fail("仅支持本程序生成的.xlsx；宏文件和旧版.xls不直接导入", code="unsupported_file")
    wb = load_workbook(path, data_only=False, keep_links=False)
    try:
        _check_no_formulas(wb)
        meta = _read_meta(wb)
        expected = set(meta["sheet_names"])
        actual = set(wb.sheetnames)
        if expected != actual:
            missing, extra = sorted(expected - actual), sorted(actual - expected)
            _fail(f"工作表结构变化；缺少：{missing or '无'}；未定义新增：{extra or '无'}。不会静默忽略。", code="sheet_structure_changed")
        for sheet, signature in meta["readonly_signatures"].items():
            if _sheet_value_signature(wb[sheet]) != signature:
                _fail("只读快照被修改；每日出勤请通过月历维护，汇总和字典请重新导出", sheet, code="readonly_changed")
        changes = {}
        for table in ORDER:
            if table in READONLY_TABLES:
                continue
            rows = _read_table(wb[TABLES[table]["sheet"]], table, meta["data"][table],
                               meta.get("derived_cells", {}).get(table))
            if rows: changes[table] = rows
        records = []
        for sheet, info in meta["grids"].items():
            records.extend(_read_grid(wb[sheet], info, meta["data"]["day_records"]))
        if records: changes["day_records"] = records
        _bind_single_project(changes, meta["data"], meta["calendar_id"])
        return changes
    finally:
        wb.close()
