"""Durable weekly commitments, review reports and an idempotent local scheduler.

Business facts remain in Store. Report production never advances work-item
states. A confirmed plan captures Monday's facts; a review compares that exact
snapshot with a fresh capture, using each item's business calendar for due dates.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import os
import re
import uuid

from .design_tokens import COMMITMENT_LABELS, chart_colors
from .project_report import export_project_report, single_project, COLORS as REPORT_COLORS
from .schema import ENUMS
from .store import Store, parse_datetime, utc_now


DEFAULT_CONFIG = {
    "enabled": False,
    "timezone": "America/Vancouver",
    "monday_time": "09:00",
    "friday_time": "17:00",
}
PLAN_BASIS = (
    "本周交付候选：当前项目中，状态非待分流、已确认完成、取消，且预计完成不晚于本周日；"
    "包括逾期结转；跨周工作通过本周成果/检查点维护，不提前整项任务完成日。"
)
LEASE_SECONDS = 120


def ensure_weekly_schema(store):
    """Create additive system tables without changing the business schema."""
    store.conn.executescript("""
        CREATE TABLE IF NOT EXISTS weekly_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            config_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('monday','friday')),
            week_start TEXT NOT NULL,
            timezone TEXT NOT NULL,
            confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            cutoff TEXT NOT NULL,
            snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
            baseline_report_id TEXT REFERENCES weekly_reports(id),
            filename TEXT NOT NULL UNIQUE,
            render_state TEXT NOT NULL CHECK(render_state IN ('pending','ready','failed')),
            metrics_json TEXT NOT NULL,
            scheduled_for TEXT,
            is_demo INTEGER NOT NULL CHECK(is_demo IN (0,1)),
            error TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_weekly_plan
            ON weekly_reports(week_start, timezone)
            WHERE kind='monday' AND confirmed=1;
        CREATE TABLE IF NOT EXISTS weekly_occurrences (
            occurrence_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('running','done','failed')),
            report_id TEXT REFERENCES weekly_reports(id),
            lease_token TEXT,
            lease_until TEXT,
            attempts INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT
        );
    """)
    columns = {r[1] for r in store.conn.execute("PRAGMA table_info(weekly_reports)")}
    if "schedule_audit_json" not in columns:
        store.conn.execute("ALTER TABLE weekly_reports ADD COLUMN schedule_audit_json TEXT")
    if "review_of" not in columns:
        store.conn.execute("ALTER TABLE weekly_reports ADD COLUMN review_of TEXT REFERENCES weekly_reports(id)")
    project_data = store.data()
    projects = project_data.get("projects", [])
    calendar = next((c for c in project_data.get("calendars", []) if projects and c["id"] == projects[0].get("calendar_id")), {})
    initial_config = DEFAULT_CONFIG | ({"timezone": calendar["timezone"]} if calendar.get("timezone") else {})
    store.conn.execute(
        "INSERT OR IGNORE INTO weekly_config VALUES(1,?,?,?)",
        (json.dumps(initial_config), 1, utc_now().isoformat()),
    )
    store.conn.commit()


def _open(db_path):
    if not Path(db_path).is_file():
        raise ValueError("数据库不存在；请先建立单项目工作区")
    return Store(db_path)


def _config_values(config):
    values = {key: config.get(key, value) for key, value in DEFAULT_CONFIG.items()}
    if not isinstance(values["enabled"], bool):
        raise ValueError("周报启用状态必须为布尔值")
    if not isinstance(values["timezone"], str):
        raise ValueError("周报时区必须为IANA名称")
    try:
        ZoneInfo(values["timezone"])
    except (ValueError, KeyError) as exc:
        raise ValueError("周报时区无效") from exc
    for key in ("monday_time", "friday_time"):
        value = values[key]
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{key} 必须为24小时制 HH:MM")
    return values


def get_config(db_path):
    with _open(db_path) as store:
        ensure_weekly_schema(store)
        row = store.conn.execute("SELECT * FROM weekly_config WHERE id=1").fetchone()
        return json.loads(row["config_json"]) | {
            "version": row["version"], "updated_at": row["updated_at"]
        }


def save_config(db_path, patch):
    if not isinstance(patch, dict):
        raise ValueError("周报配置必须为对象")
    unknown = set(patch) - set(DEFAULT_CONFIG) - {"version", "updated_at"}
    if unknown:
        raise ValueError("未知周报配置字段：" + ", ".join(sorted(unknown)))
    with _open(db_path) as store:
        ensure_weekly_schema(store)
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            old = store.conn.execute("SELECT * FROM weekly_config WHERE id=1").fetchone()
            supplied = patch.get("version")
            if supplied is not None and (
                isinstance(supplied, bool) or not isinstance(supplied, int)
                or supplied != old["version"]
            ):
                raise ValueError("周报配置已更新，请刷新后重试")
            values = _config_values(json.loads(old["config_json"]) | {
                key: value for key, value in patch.items() if key in DEFAULT_CONFIG
            })
            stamp = utc_now().isoformat()
            version = old["version"] + 1
            store.conn.execute(
                "UPDATE weekly_config SET config_json=?,version=?,updated_at=? WHERE id=1",
                (json.dumps(values), version, stamp),
            )
            store.conn.commit()
            return values | {"version": version, "updated_at": stamp}
        except Exception:
            store.conn.rollback()
            raise


def _clock(now=None):
    """The optional clock is for deterministic tests, never a historical query."""
    actual = utc_now()
    instant = parse_datetime(now) if isinstance(now, str) else (now or actual)
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ValueError("周报时间必须包含时区")
    if instant > actual:
        raise ValueError("不能捕获未来的周报快照")
    return instant.astimezone(timezone.utc), actual.astimezone(timezone.utc)


def _period(instant, timezone_name):
    local = instant.astimezone(ZoneInfo(timezone_name))
    start = local.date() - timedelta(days=local.weekday())
    return local, start.isoformat(), (start + timedelta(days=6)).isoformat()


def _project_items(data):
    project = single_project(data)
    return project, [
        item for item in data.get("wbs", [])
        if item.get("project_id") == project["id"] and item.get("scope") == "project"
    ]


def _candidates(data, start, end):
    _, items = _project_items(data)
    parents={item.get("parent_id") for item in items if item.get("parent_id")}
    return [
        dict(item, carryover=item["forecast_finish"] < start)
        for item in sorted(
            items, key=lambda x: (x.get("priority") or "P9", x.get("forecast_finish") or "9999", x["id"])
        )
        if item["id"] not in parents and item["status"] not in {"intake", "done", "cancelled"}
        and item.get("forecast_finish") and item["forecast_finish"] <= end
    ]


def _baseline_row(store, start, timezone_name):
    return store.conn.execute(
        "SELECT * FROM weekly_reports WHERE kind='monday' AND confirmed=1 "
        "AND week_start=? AND timezone=?",
        (start, timezone_name),
    ).fetchone()


def preview_week(db_path, timezone_name=None, now=None):
    timezone_name = timezone_name or get_config(db_path)["timezone"]
    instant, _ = _clock(now)
    local, start, end = _period(instant, timezone_name)
    with _open(db_path) as store:
        ensure_weekly_schema(store)
        store.conn.execute("BEGIN")
        data = store.data()
        baseline = _baseline_row(store, start, timezone_name)
        candidates = _candidates(data, start, end)
        store.conn.rollback()
    return {
        "week_start": start, "week_end": end, "timezone": timezone_name,
        "candidates": candidates, "basis": PLAN_BASIS,
        "confirmed_report_id": baseline["id"] if baseline else None,
        "can_confirm": baseline is None,
        "checkpoints": [r for r in data.get("weekly_commitments", []) if r.get("week_start") == start],
        "expected_candidate_versions": {**{item["id"]: item["version"] for item in candidates}, **{"weekly_commitments:"+r["id"]: r["version"] for r in data.get("weekly_commitments", []) if r.get("week_start") == start}},
    }


def _due_today(data, item, instant, fallback):
    projects = {row["id"]: row for row in data.get("projects", [])}
    calendars = {row["id"]: row for row in data.get("calendars", [])}
    project = projects.get(item.get("project_id"), {})
    name = calendars.get(project.get("calendar_id"), {}).get("timezone", fallback)
    return instant.astimezone(ZoneInfo(name)).date().isoformat(), name


def _analysis(data, report, baseline_data=None):
    project, items = _project_items(data)
    instant = parse_datetime(report["cutoff"])
    _, start, end = _period(instant, report["timezone"])
    current = {item["id"]: item for item in items}
    candidates = _candidates(data, start, end)
    base_candidates = _candidates(baseline_data, start, end) if baseline_data else []
    comparisons = []
    for original in base_candidates:
        item = current.get(original["id"])
        due = original["forecast_finish"]
        today, due_zone = _due_today(data, item or original, instant, report["timezone"])
        outcome = "missing"
        if item:
            if item["status"] == "done":
                outcome = "accepted_on_time" if item.get("actual_finish", "9999") <= due else "accepted_late"
            elif item["status"] == "cancelled":
                outcome = "cancelled"
            elif due < today:
                outcome = "overdue"
            elif item.get("forecast_finish") and item["forecast_finish"] > end:
                outcome = "deferred"
            else:
                outcome = "pending"
        comparisons.append({
            "id": original["id"], "title": original["title"],
            "owner_id": original.get("owner_id"), "acceptor_id": original.get("acceptor_id"),
            "promised_finish": due, "forecast_finish": item.get("forecast_finish") if item else None,
            "actual_finish": item.get("actual_finish") if item else None,
            "status": item.get("status") if item else None, "outcome": outcome,
            "completion_evidence": item.get("completion_evidence") if item else None,
            "due_timezone": due_zone,
            "forecast_changed": bool(item and item.get("forecast_finish") != due),
        })
    # Explicit intermediate outcomes replace whole-task promises when available.
    from .operating import commitment_review
    checkpoints = commitment_review(data, now=instant, baseline_data=baseline_data) if baseline_data is not None else []
    project_checkpoints = [r for r in checkpoints if r.get("work_item_id") and not r.get("operation_id")]
    if project_checkpoints:
        comparisons = [{"id": r["id"], "title": r["outcome"], "owner_id": r.get("owner_id"), "acceptor_id": None,
            "promised_finish": r["comparison_due"], "forecast_finish": (r.get("current_result") or {}).get("due_date"),
            "actual_finish": ((r.get("current_result") or {}).get("completed_at") or "")[:10] or None,
            "status": (r.get("current_result") or {}).get("status"), "outcome": r["outcome_state"],
            "completion_evidence": (r.get("current_result") or {}).get("result_evidence"),
            "due_timezone": report["timezone"], "forecast_changed": (r.get("current_result") or {}).get("due_date") != r["comparison_due"]} for r in project_checkpoints]
    counts = Counter(row["outcome"] for row in comparisons)
    base_items = {row["id"]: row for row in baseline_data.get("wbs", [])} if baseline_data else {}
    committed_ids = {row["id"] for row in base_candidates}
    additions = []
    if baseline_data:
        for item in items:
            relevant = (
                item["status"] not in {"intake", "cancelled"}
                and (
                    (item["status"] != "done" and item.get("forecast_finish") and item["forecast_finish"] <= end)
                    or (item["status"] == "done" and item.get("actual_finish") and start <= item["actual_finish"] <= end)
                )
            )
            if item["id"] not in committed_ids and relevant:
                # A previously completed item does not become an unplanned delivery.
                if item["id"] in base_items and base_items[item["id"]]["status"] == "done":
                    continue
                additions.append(dict(item, addition_kind=(
                    "new_item" if item["id"] not in base_items else "added_to_week"
                )))
    meetings = []
    for meeting in data.get("meetings", []):
        day = parse_datetime(meeting["scheduled_at"]).astimezone(
            ZoneInfo(report["timezone"])
        ).date().isoformat()
        if meeting.get("project_id") == project["id"] and start <= day <= end:
            meetings.append(meeting)
    meeting_ids = {row["id"] for row in meetings}
    actions = [item for item in items if item.get("meeting_id") in meeting_ids]
    current_open = [item for item in items if item["status"] not in {"done", "cancelled"}]
    reminders = [
        row for row in data.get("registers", [])
        if row["status"] not in {"closed", "rejected"}
        and (row.get("project_id") == project["id"] or row.get("work_item_id") in current)
    ]
    plans = [
        row for row in data.get("day_records", [])
        if row["record_kind"] == "planned" and start <= row["date"] <= end
    ]
    relevant_owners = {row["owner_id"] for row in candidates if row.get("owner_id")}
    known_plan_owners = {r["owner_id"] for r in data.get("weekly_commitments", []) if r.get("week_start") == start and r.get("outcome") and r.get("due_date") and r.get("verification")}
    missing_plan = sorted(relevant_owners - known_plan_owners)
    incomplete = {
        "untriaged": sum(item["status"] == "intake" for item in items),
        "undated_active": sum(not item.get("forecast_finish") for item in current_open),
        "unassigned_active": sum(not item.get("owner_id") for item in current_open),
        "awaiting_acceptance": sum(item["status"] == "in_review" for item in current_open),
        "owners_without_week_plan": len(missing_plan),
        "explicit_unknown_plan_slots": sum(row["status"] == "unknown" for row in plans),
    }
    accepted = counts["accepted_on_time"] + counts["accepted_late"]
    baseline_missing = report["kind"] == "friday" and baseline_data is None
    denominator = len(comparisons) if baseline_data is not None else None
    committed_count = denominator
    if report["kind"] == "monday" and report["confirmed"]:
        explicit = [r for r in data.get("weekly_commitments", []) if r.get("week_start") == start and r.get("work_item_id") and not r.get("operation_id") and r.get("status") != "cancelled"]
        committed_count = len(explicit) if explicit else len(candidates)
    metrics = {
        "project_id": project["id"], "baseline_missing": baseline_missing,
        "candidate_count": len(candidates),
        "committed_count": committed_count,
        "accepted_count": accepted if baseline_data is not None else None,
        "accepted_on_time": counts["accepted_on_time"] if baseline_data is not None else None,
        "accepted_late": counts["accepted_late"] if baseline_data is not None else None,
        "acceptance_rate": round(accepted / denominator * 100, 1) if denominator else None,
        "overdue_unaccepted": counts["overdue"] if baseline_data is not None else None,
        "pending_not_due": counts["pending"] if baseline_data is not None else None,
        "deferred": counts["deferred"] if baseline_data is not None else None,
        "cancelled": counts["cancelled"] if baseline_data is not None else None,
        "missing": counts["missing"] if baseline_data is not None else None,
        "forecast_changes": sum(row["forecast_changed"] for row in comparisons) if baseline_data else None,
        "new_or_added_count": len(additions) if baseline_data is not None else None,
        "meeting_count": len(meetings), "meeting_action_count": len(actions),
        "open_reminder_count": len(reminders),
        "p0_open_count": sum(item.get("priority") == "P0" for item in current_open),
        "data_quality": incomplete,
    }
    return {
        "project": project, "candidates": candidates, "comparisons": comparisons,
        "additions": additions, "meetings": meetings, "meeting_actions": actions,
        "reminders": reminders, "missing_plan_people": missing_plan, "metrics": metrics, "checkpoints": checkpoints,
    }


def _metadata(row):
    row = dict(row)
    confirmed = bool(row.pop("confirmed"))
    result = row | {
        "status": "confirmed" if confirmed else "draft",
        "title": "周一计划 · 已冻结承诺" if confirmed else (
            "周一计划 · 草稿" if row["kind"] == "monday" else "周五验收 · 草稿"
        ),
        "week_end": (datetime.fromisoformat(row["week_start"]).date() + timedelta(days=6)).isoformat(),
        "metrics": json.loads(row["metrics_json"]),
        "is_demo": bool(row["is_demo"]),
    }
    if row.get("review_of"):
        result["title"] = "周五总结 · 会后决定与落实记录"
        result["status"] = "reviewed"
    result.pop("metrics_json")
    return result


def list_reports(db_path, limit=50):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("周报条数应为1至1000的整数")
    with _open(db_path) as store:
        ensure_weekly_schema(store)
        rows = store.conn.execute(
            "SELECT * FROM weekly_reports ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_metadata(row) for row in rows]


def _week_section(data, report, analysis):
    h = lambda value: escape(str(value if value is not None and value != "" else "—"))
    people = {row["id"]: row["name"] for row in data.get("people", [])}
    person = lambda key: h(people.get(key, "待指定"))
    if report["kind"] == "monday":
        rows = analysis["candidates"]
        checkpoints = [r for r in data.get("weekly_commitments", []) if r.get("week_start") == report["week_start"] and r.get("work_item_id") and not r.get("operation_id") and r.get("status") != "cancelled"]
        if checkpoints:
            rows = [{"id":r["id"], "title":r["outcome"], "owner_id":r.get("owner_id"), "acceptor_id":None,
                     "forecast_finish":r["due_date"], "carryover":r["due_date"] < report["week_start"],
                     "status":"ready", "acceptance_criteria":r.get("verification")} for r in checkpoints]
        counts = Counter(item["status"] for item in rows)
        heading = "本周交付承诺" if report["status"] == "confirmed" else "本周交付候选"
        labels = {key: ENUMS["work_status"][key] for key in ("ready", "in_progress", "blocked", "in_review")}
        colors = {key: REPORT_COLORS[key] for key in labels}
        table_rows = [
            "<tr><td><b>" + h(item["title"]) + "</b><small>" + h(item["id"]) + "</small></td>"
            "<td>" + person(item.get("owner_id")) + "</td>"
            "<td>" + person(item.get("acceptor_id")) + "</td>"
            "<td>" + h(item["forecast_finish"]) + ("<small>逾期结转</small>" if item["carryover"] else "") + "</td>"
            "<td>" + h(ENUMS["work_status"][item["status"]]) + "</td>"
            "<td>" + h(item.get("acceptance_criteria")) + "</td></tr>" for item in rows
        ]
        table_head = "<th>交付工作</th><th>负责人</th><th>确认人</th><th>承诺日期</th><th>当前状态</th><th>完成标准</th>"
        note = PLAN_BASIS
    elif analysis["metrics"]["baseline_missing"]:
        return (
            '<div class="empty baseline-missing"><b>本周承诺基线尚未确认。</b>'
            '没有同一周、同一时区的已确认周一快照，无法计算承诺兑现情况。'
            '以下保留当前完成与开发事实；不能把本次预测反推成周一承诺。</div>'
        )
    else:
        rows = analysis["comparisons"]
        counts = Counter(row["outcome"] for row in rows)
        heading = "本周承诺与验收结果"
        labels = dict(COMMITMENT_LABELS)
        colors = chart_colors("commitment")
        table_rows = [
            "<tr><td><b>" + h(row["title"]) + "</b><small>" + h(row["id"]) + "</small></td>"
            "<td>" + person(row["owner_id"]) + "</td>"
            "<td>" + h(row["promised_finish"]) + "</td>"
            "<td>" + h(row["actual_finish"] or row["forecast_finish"]) + "</td>"
            "<td>" + h(labels[row["outcome"]]) + (
                "<small>待确认不计完成</small>" if row["status"] == "in_review" else ""
            ) + "</td><td>" + h(row["completion_evidence"]) + "</td></tr>" for row in rows
        ]
        table_head = "<th>交付工作</th><th>原负责人</th><th>周一承诺</th><th>实际／当前预测</th><th>核对结果</th><th>确认依据</th>"
        note = (
            "分母为已冻结的本周交付工作项，取消及移出本周仍保留在分母。"
            "按该工作所属日历时区判断是否已过原到期日；当天及以后到期的工作不提前判逾期。"
            "该比例不代表项目总体完成率。"
        )
    total = len(rows)
    segments = "".join(
        f'<i style="width:{counts.get(key,0)/total*100 if total else 0:.3f}%;background:{colors[key]}" '
        f'title="{h(label)}：{counts.get(key,0)}"></i>'
        for key, label in labels.items() if counts.get(key)
    )
    legend = "".join(
        f'<span><i style="background:{colors[key]}"></i>{h(label)} <b>{counts.get(key,0)}</b></span>'
        for key, label in labels.items() if counts.get(key)
    )
    chart = (
        '<div class="chart-card commitment-card"><div class="panel-head"><h3>' + h(heading) + '</h3>'
        f'<span class="commitment-total"><b>{total}</b> 项</span></div>'
        '<div class="seg-bar" role="img" aria-label="' + h(heading) + f'，分母{total}项">'
        + segments + '</div><div class="seg-legend">'
        + (legend or "<span>没有可统计的交付工作；比例留空。</span>") + "</div></div>"
    )
    detail = (
        '<div class="table-scroll"><table><thead><tr>' + table_head + "</tr></thead><tbody>"
        + "".join(table_rows) + "</tbody></table></div>"
        if rows else '<div class="empty">本周没有符合当前口径的交付工作。</div>'
    )
    additions = analysis["additions"]
    add_html = ""
    if report["kind"] == "friday" and additions:
        add_html = (
            "<h3 class='block-title'>周一之外新增到本周的工作</h3>"
            '<div class="table-scroll"><table><thead><tr><th>工作</th><th>负责人</th>'
            '<th>当前预计</th><th>来源变化</th></tr></thead><tbody>'
            + "".join(
                "<tr><td>" + h(item["title"]) + "</td><td>" + person(item.get("owner_id"))
                + "</td><td>" + h(item.get("forecast_finish")) + "</td><td>"
                + ("新增工作" if item["addition_kind"] == "new_item" else "既有工作调入本周")
                + "</td></tr>" for item in additions
            ) + "</tbody></table></div>"
        )
    warnings = []
    if analysis["missing_plan_people"]:
        warnings.append("本周交付负责人尚无计划安排记录：" + "、".join(
            people.get(key, key) for key in analysis["missing_plan_people"]
        ) + "。空白不代表可用。")
    if report["status"] == "draft":
        warnings.append("本报告为草稿，生成报告不会替代人员确认或修改任务状态。")
    warning_html = "<p class='note'>" + h(" ".join(warnings)) + "</p>" if warnings else ""
    return (
        '<p class="eyebrow">03 / 本周计划与验收</p><h2>' + h(heading) + "</h2>"
        + chart + '<p class="note">' + h(note) + "</p>" + detail + add_html + warning_html
    )


def _write_report(db_path, out_dir, report_id):
    with _open(db_path) as store:
        ensure_weekly_schema(store)
        row = store.conn.execute("SELECT * FROM weekly_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise ValueError("周报不存在")
        report = _metadata(row)
        data = store.read_snapshot(row["snapshot_id"])["data"]
        baseline_data = None
        if row["baseline_report_id"]:
            baseline = store.conn.execute(
                "SELECT snapshot_id FROM weekly_reports WHERE id=?", (row["baseline_report_id"],)
            ).fetchone()
            baseline_data = store.read_snapshot(baseline["snapshot_id"])["data"]
    analysis = _analysis(data, row, baseline_data)
    directory = Path(out_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / report["filename"]
    temporary = directory / (".weekly-" + uuid.uuid4().hex + ".html")
    try:
        export_project_report(
            data, temporary, as_of=report["cutoff"],
            kind="plan" if report["kind"] == "monday" else "review",
            timezone_name=report["timezone"], demo=report["is_demo"],
            review_phase="post_meeting" if report.get("review_of") else "pre_meeting",
            schedule_audit=json.loads(row["schedule_audit_json"]) if row["schedule_audit_json"] else None,
        )
        content = temporary.read_text(encoding="utf-8")
        section = _week_section(data, report, analysis)
        checkpoints = [r for r in data.get("weekly_commitments", []) if r.get("week_start") == report["week_start"]]
        if checkpoints:
            section += '<h3 class="block-title">本周可验证成果 / 中间检查点 <span class="count">' + str(len(checkpoints)) + '</span></h3><ul class="checkpoint-list">' + ''.join('<li><b>'+escape(str(r.get("outcome") or "待补成果"))+'</b><span>'+escape(str(r.get("due_date") or "日期待补"))+'</span><small>'+escape(str(r.get("verification") or "验证方式待补"))+'</small></li>' for r in checkpoints) + '</ul>' 
        pattern = r'(<section class="page [^"]*" data-section="2">)(.*?)(<div class="page-foot">)'
        def replace_week(match):
            body = section
            if analysis["metrics"]["baseline_missing"]:
                # The absence of a promise baseline must not erase this week's
                # observed deliveries and ongoing development from the report.
                body = match.group(2).replace("</h2>", "</h2>" + section, 1)
            return match.group(1) + body + match.group(3)
        content, replaced = re.subn(pattern, replace_week, content, count=1, flags=re.S)
        if replaced != 1:
            raise ValueError("项目报告的本周页面标记缺失，未写入不完整周报")
        local = parse_datetime(report["created_at"]).astimezone(ZoneInfo(report["timezone"]))
        banner = (
            '<p class="note report-banner">'
            + escape(report["title"]) + " · 承诺对照周期 "
            + escape(report["week_start"] + " — " + report["week_end"] + "（" + report["timezone"] + "）") + " · 实际捕获 "
            + escape(local.strftime("%Y-%m-%d %H:%M") + " " + report["timezone"])
            + (" · 本次生成不在例行日期" if local.weekday() != (0 if report["kind"] == "monday" else 4) else "")
            + "</p>"
        )
        content = content.replace("<nav>", banner + "<nav>", 1)
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
        with _open(db_path) as store:
            store.conn.execute(
                "UPDATE weekly_reports SET render_state='ready',error=NULL WHERE id=?", (report_id,)
            )
            store.conn.commit()
            result = _metadata(store.conn.execute(
                "SELECT * FROM weekly_reports WHERE id=?", (report_id,)
            ).fetchone())
        return result | {"ok": True, "path": str(path)}
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        with _open(db_path) as store:
            store.conn.execute(
                "UPDATE weekly_reports SET render_state='failed',error=? WHERE id=?",
                (str(exc)[:1000], report_id),
            )
            store.conn.commit()
        raise


def generate_weekly_report(
    db_path, out_dir, kind="monday", actor="local-user", confirm=False,
    timezone_name=None, now=None, demo=False, expected_candidate_versions=None,
    _occurrence_key=None, _lease_token=None, _scheduled_for=None, review_of=None, schedule_audit_policy=None,
):
    if kind not in {"monday", "friday"}:
        raise ValueError("周报类型必须为monday或friday")
    if not isinstance(confirm, bool) or not isinstance(demo, bool):
        raise ValueError("确认和演示标记必须为布尔值")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError("操作者标签应为1至200字文本")
    if confirm and kind != "monday":
        raise ValueError("只有周一计划可以冻结承诺；报告生成不代替成果验收")
    timezone_name = timezone_name or get_config(db_path)["timezone"]
    instant, created = _clock(now)
    local, start, end = _period(instant, timezone_name)
    if local.date() != created.astimezone(ZoneInfo(timezone_name)).date():
        raise ValueError("只能捕获当前日期的事实，历史报告请读取已保存快照")
    if review_of and (kind != "friday" or confirm):
        raise ValueError("会后版本只能关联周五审议报告")
    with _open(db_path) as store:
        ensure_weekly_schema(store)
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _baseline_row(store, start, timezone_name) if confirm else None
            occurrence = None
            if _occurrence_key:
                occurrence = store.conn.execute(
                    "SELECT * FROM weekly_occurrences WHERE occurrence_key=?", (_occurrence_key,)
                ).fetchone()
                if occurrence is None or occurrence["lease_token"] != _lease_token:
                    raise ValueError("周报执行权已变化，请让下一次调度恢复")
                if occurrence["report_id"]:
                    existing = store.conn.execute(
                        "SELECT * FROM weekly_reports WHERE id=?", (occurrence["report_id"],)
                    ).fetchone()
            if existing:
                report_id = existing["id"]
                store.conn.commit()
            else:
                latest = store.conn.execute("SELECT MAX(recorded_at) FROM event_log").fetchone()[0]
                if latest and parse_datetime(latest) > instant:
                    raise ValueError("当前事实包含截止时间之后的更新，不能倒签周报")
                data = store.data()
                single_project(data)
                if review_of:
                    previous_review = store.conn.execute("SELECT * FROM weekly_reports WHERE id=?", (review_of,)).fetchone()
                    if not previous_review or previous_review["kind"] != "friday" or previous_review["week_start"] != start or previous_review["timezone"] != timezone_name:
                        raise ValueError("会后版本须关联同周期、同一时区的已有周五报告")
                candidates = _candidates(data, start, end)
                if expected_candidate_versions is not None:
                    expected = {**{item["id"]: item["version"] for item in candidates}, **{"weekly_commitments:"+r["id"]: r["version"] for r in data.get("weekly_commitments", []) if r.get("week_start") == start}}
                    if not isinstance(expected_candidate_versions, dict) or expected_candidate_versions != expected:
                        raise ValueError("本周候选清单已变化，请重新预览后再冻结")
                    if any(isinstance(v, bool) or not isinstance(v, int) for v in expected_candidate_versions.values()):
                        raise ValueError("候选版本必须为整数")
                baseline = _baseline_row(store, start, timezone_name) if kind == "friday" else None
                baseline_data = store.read_snapshot(baseline["snapshot_id"])["data"] if baseline else None
                report_id = "WR-" + uuid.uuid4().hex[:16]
                snapshot_id = "SNAP-" + uuid.uuid4().hex[:16]
                record = {
                    "id": report_id, "kind": kind, "week_start": start,
                    "timezone": timezone_name, "confirmed": int(confirm),
                    "actor": actor.strip(), "created_at": created.isoformat(),
                    "cutoff": instant.isoformat(), "snapshot_id": snapshot_id,
                    "baseline_report_id": baseline["id"] if baseline else None,
                    "filename": f"{start}_{kind}_{report_id}.html",
                    "render_state": "pending", "scheduled_for": _scheduled_for,
                    "is_demo": int(demo), "error": None, "review_of": review_of,
                }
                from .schedule_audit import audit_schedule_changes
                captured_audit = audit_schedule_changes(data, history=store.history(), now=instant, policy=schedule_audit_policy)
                record["schedule_audit_json"] = json.dumps(captured_audit, ensure_ascii=False)
                analysis = _analysis(data, record, baseline_data)
                record["metrics_json"] = json.dumps(analysis["metrics"], ensure_ascii=False)
                watermark = store.conn.execute("SELECT COALESCE(MAX(event_id),0) FROM event_log").fetchone()[0]
                label = ("周一确认承诺" if confirm else ("周一计划草稿" if kind == "monday" else "周五验收草稿"))
                # Capture facts and the durable report identity in ONE transaction.
                # A crash can leave a pending render, but never an orphan recapture.
                store.conn.execute(
                    "INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                    (snapshot_id, label, created.isoformat(), instant.isoformat(), watermark,
                     json.dumps(data, ensure_ascii=False)),
                )
                names = list(record)
                store.conn.execute(
                    "INSERT INTO weekly_reports(" + ",".join(names) + ") VALUES("
                    + ",".join("?" for _ in names) + ")",
                    [record[name] for name in names],
                )
                if occurrence:
                    store.conn.execute(
                        "UPDATE weekly_occurrences SET report_id=? WHERE occurrence_key=? AND lease_token=?",
                        (report_id, _occurrence_key, _lease_token),
                    )
                store.conn.commit()
        except Exception:
            store.conn.rollback()
            raise
    result = _write_report(db_path, out_dir, report_id)
    return result | {"reused": bool(existing)}


def retry_report(db_path, out_dir, report_id):
    """Re-render the saved capture; never substitute current facts."""
    return _write_report(db_path, out_dir, report_id)


def run_due(db_path, out_dir, config=None, now=None, demo=False):
    config = _config_values(get_config(db_path) if config is None else config)
    instant, _ = _clock(now)
    local, start, _ = _period(instant, config["timezone"])
    base = {
        "ok": True, "checked_at": instant.isoformat(), "timezone": config["timezone"],
        "policy": "仅恢复当日已到时间的窗口；不补造前几天的历史周报。",
    }
    if not config["enabled"]:
        return base | {"status": "disabled", "reports": []}
    weekday = local.weekday()
    if weekday not in (0, 4):
        return base | {"status": "not_due", "reports": [], "reason": "今日不是周一或周五"}
    kind = "monday" if weekday == 0 else "friday"
    hour, minute = map(int, config[kind + "_time"].split(":"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < scheduled:
        return base | {"status": "not_due", "reports": [], "scheduled_for": scheduled.isoformat()}
    key = kind + "|" + local.date().isoformat() + "|" + config["timezone"]
    token = uuid.uuid4().hex
    with _open(db_path) as store:
        ensure_weekly_schema(store)
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            old = store.conn.execute(
                "SELECT * FROM weekly_occurrences WHERE occurrence_key=?", (key,)
            ).fetchone()
            if old and old["state"] == "done":
                report = _metadata(store.conn.execute(
                    "SELECT * FROM weekly_reports WHERE id=?", (old["report_id"],)
                ).fetchone())
                store.conn.commit()
                return base | {"status": "already_generated", "reports": [report]}
            if old and old["state"] == "running" and old["lease_until"] and parse_datetime(old["lease_until"]) > instant:
                store.conn.commit()
                return base | {"status": "running", "reports": [], "occurrence_key": key}
            lease_until = (instant + timedelta(seconds=LEASE_SECONDS)).isoformat()
            if old:
                store.conn.execute(
                    "UPDATE weekly_occurrences SET state='running',lease_token=?,lease_until=?,"
                    "attempts=attempts+1,updated_at=?,error=NULL WHERE occurrence_key=?",
                    (token, lease_until, instant.isoformat(), key),
                )
            else:
                store.conn.execute(
                    "INSERT INTO weekly_occurrences VALUES(?,?,?,'running',NULL,?,?,1,?,?,NULL)",
                    (key, kind, scheduled.isoformat(), token, lease_until,
                     instant.isoformat(), instant.isoformat()),
                )
            store.conn.commit()
        except Exception:
            store.conn.rollback()
            raise
    try:
        report = generate_weekly_report(
            db_path, out_dir, kind=kind, actor="local-scheduler", confirm=False,
            timezone_name=config["timezone"], now=instant, demo=demo,
            _occurrence_key=key, _lease_token=token, _scheduled_for=scheduled.isoformat(),
        )
        with _open(db_path) as store:
            store.conn.execute(
                "UPDATE weekly_occurrences SET state='done',lease_until=NULL,updated_at=?,error=NULL "
                "WHERE occurrence_key=? AND lease_token=?",
                (utc_now().isoformat(), key, token),
            )
            store.conn.commit()
        return base | {
            "status": "generated", "reports": [report],
            "late_run": local > scheduled, "scheduled_for": scheduled.isoformat(),
        }
    except Exception as exc:
        with _open(db_path) as store:
            store.conn.execute(
                "UPDATE weekly_occurrences SET state='failed',lease_until=NULL,updated_at=?,error=? "
                "WHERE occurrence_key=? AND lease_token=?",
                (utc_now().isoformat(), str(exc)[:1000], key, token),
            )
            store.conn.commit()
        return base | {"ok": False, "status": "failed", "reports": [], "error": str(exc)}
