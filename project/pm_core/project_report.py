"""Six-page pmmaster report from the same tracking projection as the UI.

The report is an immutable capture of facts. It contains both the default
project-local today-to-Sunday view and a full-project view; neither classifies
priority as delay, counts summary tasks twice, or infers task effort from attendance.
"""
from collections import Counter
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import math

from .calendar import workday_distance
from .design_tokens import chart_colors, css_root
from .schema import ENUMS
from .store import parse_datetime
from .tracking import tracking_data

COLORS = chart_colors("work_status")
WORK_COLORS = chart_colors("work_type")
PRIORITY_COLORS = {(None if key == "ungraded" else key): value for key, value in chart_colors("priority").items()}


def h(value):
    return escape(str("—" if value is None or value == "" else value), quote=True)


def _link(url, label):
    try:
        raw = str(url or "")
        parsed = urlsplit(raw)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not any(ord(c) < 32 for c in raw)
    except ValueError:
        valid = False
    if not valid:
        return h(label)
    return f'<a href="{escape(raw, quote=True)}" target="_blank" rel="noopener noreferrer">{h(label)} ↗</a>'


def single_project(data):
    projects = data.get("projects", [])
    if len(projects) != 1:
        raise ValueError("单项目汇报需要且仅能有一条项目概况")
    project = projects[0]
    unrelated = [w["id"] for w in data.get("wbs", []) if w.get("scope")=="project" and w.get("project_id") != project["id"]]
    if unrelated:
        raise ValueError("存在未归属当前项目的工作，请先确认归属：" + ", ".join(unrelated[:8]))
    return project


def weekly_facts(data, as_of, timezone_name="America/Vancouver"):
    """Use the project's calendar for business periods; retain the old signature.

    timezone_name remains accepted for callers' schedule configuration. The
    project report explicitly uses the project calendar, while separate weekly
    commitment reports may keep their own configured schedule timezone.
    """
    project = single_project(data)
    tracking = tracking_data(data, as_of)
    zone = tracking["timezone"]
    now = parse_datetime(as_of).astimezone(ZoneInfo(zone))
    start = now.date() - timedelta(days=now.weekday())
    end = date.fromisoformat(tracking["week_end"])
    a, b, today = start.isoformat(), end.isoformat(), tracking["today"]
    all_works = [w for w in data.get("wbs", []) if w.get("scope")=="project" and w.get("project_id")==project["id"]]
    ids = set(tracking["leaf_ids"])
    works = [w for w in all_works if w["id"] in ids]
    current = [w for w in works if w["status"] not in ("done", "cancelled")]
    focus = [w for w in works if w["id"] in set(tracking["focus_ids"])]
    due = [w for w in current if (w.get("forecast_finish") and w["forecast_finish"] <= b) or tracking["tasks"][w["id"]]["delay_state"] == "overdue"]
    accepted = [w for w in works if w["status"] == "done" and w.get("actual_finish") and a <= w["actual_finish"] <= today]
    working = [w for w in focus if w["status"] in ("in_progress", "blocked", "in_review")]
    meetings = []
    for meeting in data.get("meetings", []):
        if meeting.get("project_id") != project["id"]:
            continue
        day = parse_datetime(meeting["scheduled_at"]).astimezone(ZoneInfo(zone)).date().isoformat()
        if a <= day <= b:
            meetings.append(meeting)
    all_ids = {w["id"] for w in all_works}
    reminders = [r for r in data.get("registers", []) if r["status"] not in ("closed", "rejected") and (r.get("project_id") == project["id"] or r.get("work_item_id") in all_ids)]
    action_ids = set()
    meeting_ids = {m["id"] for m in meetings}
    for work in all_works:
        if work.get("meeting_id") in meeting_ids:
            action_ids.add(work["id"])
            action_ids.update(tracking["tasks"][work["id"]]["leaf_descendant_ids"])
    return {"project":project, "tracking":tracking, "timezone":zone, "now":now, "start":start, "end":end,
            "all_works":all_works, "works":works, "current":current, "focus":focus, "due":due, "accepted":accepted,
            "working":working, "meetings":meetings, "actions":[w for w in works if w["id"] in action_ids],
            "reminders":reminders, "p0":[w for w in current if w.get("priority") == "P0"]}


def _badge(work):
    priority = work.get("priority")
    return f'<span class="priority {h(priority or "ungraded")}">{h(priority or "待分级")}</span>'


def _delay_badge(facts):
    state = facts.get("delay_state", "unknown")
    text = facts.get("delay_state_label", "日期待确认")
    if state in {"overdue", "late_done"}:
        text += f" · {facts.get('delay_days', 0)}天"
    return f'<span class="delay-badge {h(state)}">{h(text)}</span>'


def _kpi(label, value, *, tone="neutral", sub=None, meter=None, goto=None):
    """Stat tile: neutral value ink, status carried by an accent bar plus label."""
    bar = f'<span class="meter"><i style="width:{max(0, min(100, meter)):.1f}%"></i></span>' if meter is not None else ''
    attrs = f' data-goto="{int(goto)}" role="button" tabindex="0"' if goto is not None else ''
    return (f'<div class="kpi tone-{h(tone)}"{attrs}><span class="kpi-label">{h(label)}</span>'
            f'<b class="kpi-value">{h(value)}</b>{bar}' + (f'<small>{h(sub)}</small>' if sub else '') + '</div>')


def _method_note(text, summary="统计口径说明"):
    """Keep definitions in the report without letting them crowd the facts."""
    return f'<details class="method-note"><summary>{h(summary)}</summary><p>{h(text)}</p></details>'


def _donut(counts, title):
    total = sum(counts.values())
    radius = 62
    circumference = 2 * math.pi * radius
    offset = 0
    segments, legend = [], []
    for key, label in ENUMS["work_status"].items():
        count = counts.get(key, 0)
        if not count:
            continue
        length = count / total * circumference
        segments.append(f'<circle cx="88" cy="88" r="{radius}" fill="none" stroke="{COLORS[key]}" stroke-width="16" stroke-dasharray="{length:.3f} {circumference-length:.3f}" stroke-dashoffset="{-offset:.3f}" transform="rotate(-90 88 88)"/>')
        offset += length
        legend.append(f'<li><i style="background:{COLORS[key]}"></i><span>{h(label)}</span><b>{count}</b></li>')
    svg = f'<svg viewBox="0 0 176 176" role="img" aria-label="{h(title)}，共{total}个叶子执行项"><circle cx="88" cy="88" r="62" fill="none" stroke="var(--hover)" stroke-width="16"/>'+''.join(segments)+f'<text x="88" y="90" text-anchor="middle" class="donut-number">{total}</text><text x="88" y="112" text-anchor="middle" class="donut-label">叶子执行项</text></svg>'
    return f'<div class="chart-card"><h3>{h(title)}</h3><div class="donut-wrap">{svg}<ul class="chart-legend">'+(''.join(legend) or '<li>尚无记录</li>')+'</ul></div><p class="note">父任务不重复计数；条数不等于完成百分比。</p></div>'


def _priority_chart(works):
    counts = Counter(w.get("priority") for w in works)
    peak = max(counts.values(), default=1)
    rows = []
    for key, text in (("P0", "紧急"), ("P1", "高"), ("P2", "一般"), ("P3", "低"), (None, "待分级")):
        count = counts.get(key, 0)
        rows.append(f'<div class="bar-row"><span>{h(key or "—")} <small>{text}</small></span><div class="bar-bed"><i style="width:{count/max(1,peak)*100:.2f}%;background:{PRIORITY_COLORS[key]}"></i></div><b>{count}</b></div>')
    return '<div class="chart-card"><h3>全项目未完成 · 优先级分布</h3><div class="bar-chart">'+''.join(rows)+'</div><p class="note">未取消、未确认完成的叶子项；P0 独立于延期判断。</p></div>'


def waterfall_calendar(data):
    """Project-local Monday buckets; unknown holidays never become five days."""
    from .project_calendar import workday_status
    project = single_project(data)
    works = [w for w in data.get("wbs", []) if w.get("scope") == "project" and w.get("node_kind") == "task"]
    dates = [date.fromisoformat(w[f]) for w in works for f in
             ("baseline_start", "baseline_finish", "forecast_start", "forecast_finish", "actual_start", "actual_finish", "remediation_expected", "remediation_actual") if w.get(f)]
    dates.extend(date.fromisoformat(project[f]) for f in ("baseline_start", "baseline_finish", "forecast_finish") if project.get(f))
    if not dates:
        return {"weeks": [], "anchor_known": False, "verified_workweeks": 0, "pending_weeks": 0}
    start = date.fromisoformat(project["baseline_start"]) if project.get("baseline_start") else min(dates)
    end = max(dates + [start])
    origin = start - timedelta(days=start.weekday())
    lo = min(dates + [start]); lo -= timedelta(days=lo.weekday())
    hi = end + timedelta(days=6-end.weekday())
    # Calendar analysis may include a forecast beyond reminder_end. Do not mutate
    # that notification window or treat its outside-window false as a holiday.
    calendar_data = dict(data, projects=[dict(project, reminder_start=start.isoformat(), reminder_end=end.isoformat())])
    weeks = []
    for offset in range(0, (hi-lo).days+1, 7):
        a = lo + timedelta(days=offset); b = a + timedelta(days=6)
        ra, rb = max(a, start), min(b, end)
        statuses = [workday_status(calendar_data, project["id"], (ra+timedelta(days=n)).isoformat())
                    for n in range(max(0, (rb-ra).days+1))]
        unknown = sum(not s["verified"] for s in statuses)
        known = sum(s["verified"] and s["is_workday"] is True for s in statuses)
        index = (a-origin).days//7+1
        label = f"WK{index:02d}" if project.get("baseline_start") and index > 0 else f"计划前 {1-index} 周" if index <= 0 else f"周 {index}"
        weeks.append({"label": label, "start": a.isoformat(), "end": b.isoformat(),
                      "range_start": ra.isoformat() if statuses else None, "range_end": rb.isoformat() if statuses else None,
                      "workdays": None if unknown else known, "known_workdays": known, "unknown_days": unknown,
                      "in_project": bool(statuses)})
    return {"start": start.isoformat(), "end": end.isoformat(), "axis_start": lo.isoformat(), "axis_end": hi.isoformat(),
            "anchor_known": bool(project.get("baseline_start")), "weeks": weeks,
            "verified_workweeks": sum(w["workdays"] is not None and w["workdays"] > 0 for w in weeks),
            "pending_weeks": sum(w["workdays"] is None for w in weeks),
            "zero_workweeks": sum(w["in_project"] and w["workdays"] == 0 for w in weeks)}


def render_waterfall(data, works, as_of):
    """Render both frozen views from one authoritative tracking projection."""
    project = single_project(data)
    tracking = tracking_data(data, as_of)
    works = [w for w in works if w.get("scope") == "project" and w.get("project_id") == project["id"]]
    by_id = {w["id"]:w for w in works}
    people = {p["id"]:p for p in data.get("people", [])}
    nodes = [n for n in data.get("wbs", []) if n.get("scope") == "project" and n["project_id"] == project["id"] and n.get("node_kind") != "task"]
    today = date.fromisoformat(tracking["today"])
    leaf_ids = set(tracking["leaf_ids"])
    person = lambda pid: h(people.get(pid, {}).get("name", "待指定"))
    calendar = waterfall_calendar(data)

    def view(scope):
        focus = scope == "focus"
        visible = set(tracking["focus_ids"]) & set(by_id) if focus else set(by_id)
        # Parents are visual context and never additional counted execution items.
        for wid in list(visible):
            parent = by_id[wid].get("parent_id")
            seen = set()
            while parent in by_id and parent not in seen:
                seen.add(parent); visible.add(parent); parent = by_id[parent].get("parent_id")
        included = [w for w in works if w["id"] in visible and w.get("node_kind") == "task"]
        roots = [w for w in included if w.get("parent_id") not in visible]
        children = {wid:[] for wid in visible}
        for w in included:
            if w.get("parent_id") in children:
                children[w["parent_id"]].append(w)
        if focus:
            lo, hi = today, date.fromisoformat(tracking["week_end"])
        else:
            lo = date.fromisoformat(calendar.get("axis_start", tracking["today"]))
            hi = date.fromisoformat(calendar.get("axis_end", tracking["week_end"]))
        span = max(1, (hi-lo).days+1)
        pos = lambda value: (date.fromisoformat(value)-lo).days/span*100
        center = lambda value: pos(value)+50/span
        def in_view(value): return bool(value) and lo <= date.fromisoformat(value) <= hi
        def clip(start, end):
            if not start or not end:
                return None
            a, b = date.fromisoformat(start), date.fromisoformat(end)
            if b < a or b < lo or a > hi:
                return None
            return ((max(a,lo)-lo).days/span*100, max(1, (min(b,hi)-max(a,lo)).days+1)/span*100)
        step = 1 if focus else 7
        if focus:
            ticks = ''.join(f'<span style="left:{i/span*100:.3f}%">{(lo+timedelta(days=i)).strftime("%m/%d")}<small>{"一二三四五六日"[(lo+timedelta(days=i)).weekday()]}</small></span>' for i in range(span))
        else:
            ticks = ''.join(f'<span data-week="{h(w["label"])}" data-workdays="{h(w["workdays"] if w["workdays"] is not None else "unknown")}" style="left:{pos(w["start"]):.3f}%"><b>{h(w["label"])}</b><small>{h((w["range_start"] or w["start"])[5:])}—{h((w["range_end"] or w["end"])[5:])}</small><small>{"日历待核验" if w["workdays"] is None else str(w["workdays"])+" 个工作日" if w["in_project"] else "项目计划前"}</small></span>' for w in calendar["weeks"])
        def track(w, derived=None):
            derived = derived or tracking["tasks"][w["id"]]
            state = derived["delay_state"]
            color = WORK_COLORS.get(w.get("work_type"), WORK_COLORS["other"])
            pieces = []
            def add_bar(start, end, cls, caption):
                bounds = clip(start, end)
                if bounds:
                    left, width = bounds
                    pieces.append(f'<i class="{cls}" style="left:{left:.3f}%;width:{width:.3f}%;--lane-color:{color}" title="{h(caption)} {h(start)} 至 {h(end)}（可见窗口内）"></i>')
                    return True
                return False
            add_bar(w.get("baseline_start"), w.get("baseline_finish"), "original", "原计划")
            forecast_start = w.get("forecast_start") or w.get("baseline_start")
            forecast_state = state if state in {"overdue","risk","unknown"} else "on_track"
            forecast_drawn = add_bar(forecast_start, w.get("forecast_finish"), "forecast "+forecast_state, "当前预测")
            if not forecast_start and in_view(w.get("forecast_finish")):
                point_color = "var(--critical)" if state == "overdue" else "var(--warning)" if state == "risk" else color  # same as .forecast.overdue / .forecast.risk
                pieces.append(f'<i class="forecast-point" style="left:{center(w["forecast_finish"]):.3f}%;background:{point_color}" title="预测完成点 {h(w["forecast_finish"])}；开始日未设"></i>')
                forecast_drawn = True
            if w.get("actual_start") and w.get("actual_finish"):
                add_bar(w["actual_start"],w["actual_finish"],"actual "+("late_done" if state=="late_done" else "on_track"),"实际起止")
            elif in_view(w.get("actual_start")):
                pieces.append(f'<i class="actual-start" style="left:{center(w["actual_start"]):.3f}%" title="实际开始 {h(w["actual_start"])}；尚无实际完成"></i>')
            if state == "overdue" and not forecast_drawn:
                pieces.append(f'<i class="carryover" title="未结逾期结转；原日期未改写"></i><small class="carryover-text">逾期结转 {derived["delay_days"]}天</small>')
            milestone = w.get("actual_finish") or w.get("forecast_finish") or w.get("baseline_finish")
            if w.get("is_acceptance_node") and in_view(milestone):
                pieces.append(f'<i class="acceptance-node" style="left:{center(milestone):.3f}%" title="验收节点 {h(milestone)} · {h(derived["stage_acceptance_result"])}"></i>')
            for key, cls, label in (("remediation_expected","remediation expected","预期补救"),("remediation_actual","remediation actual-remediation","实际补救")):
                if in_view(w.get(key)):
                    pieces.append(f'<i class="{cls}" style="left:{center(w[key]):.3f}%" title="{label} {h(w[key])} · {h(w.get("remediation_action"))}"></i>')
            if not pieces:
                pieces.append('<span class="unplanned">日期未设或不在本窗口</span>')
            today_line = f'<em class="today" style="left:{center(tracking["today"]):.3f}%" title="数据截止日 {tracking["today"]}"></em>' if in_view(tracking["today"]) else ''
            return f'<div class="timeline-track" style="--tick-width:{step/span*100:.4f}%">'+''.join(pieces)+today_line+'</div>'
        def details(w):
            f = tracking["tasks"][w["id"]]
            members = '、'.join(people.get(pid,{}).get("name",pid) for pid in w.get("member_ids") or [])
            backups = '、'.join(people.get(pid,{}).get("name",pid) for pid in w.get("backup_member_ids") or [])
            todo = f'{f["todo_done_count"]} / {f["todo_count"]} · {f["todo_progress_pct"]}%' if f["todo_count"] else "无子Todo"
            return (f'<div class="trace-detail"><div class="trace-detail-grid"><div><small>确认人 / 协调人</small>{person(w.get("acceptor_id"))} / {person(w.get("coordinator_id"))}</div>'
                    f'<div><small>原计划起止</small>{h(w.get("baseline_start"))} → {h(w.get("baseline_finish"))}</div><div><small>当前预测起止</small>{h(w.get("forecast_start") or w.get("baseline_start"))} → {h(w.get("forecast_finish"))}</div><div><small>实际起止</small>{h(w.get("actual_start"))} → {h(w.get("actual_finish") or "尚无实际完成")}</div>'
                    f'<div><small>成员 / 备用成员</small>{h(members)} / {h(backups)}</div><div><small>任务阶段 / 阶段验收</small>{h(ENUMS["work_stage"].get(w.get("stage"),"未设置"))} / {h(f["stage_acceptance_result"])}</div>'
                    f'<div><small>预计 / 实际工期（自然日）</small>{h(f["planned_duration_days"])} / {h(f["actual_duration_days"])}</div><div><small>有效叶子Todo</small>{h(todo)}</div><div><small>实际投入（手工记录）</small>{h(w.get("actual_person_days"))} 人天</div></div>'
                    + detail_facts(w, f) + '</div>')
        def detail_facts(w, f):
            """Filled facts as labelled lines; empty fields collapse into one gap line."""
            remedy = w.get("remediation_action")
            if remedy or w.get("remediation_expected") or w.get("remediation_actual"):
                remedy = f'{h(remedy)}<br>预期 {h(w.get("remediation_expected"))} / 实际 {h(w.get("remediation_actual"))}'
            items = [("完成标准", h(w.get("acceptance_criteria")) if w.get("acceptance_criteria") else None),
                     ("完成依据", h(w.get("completion_evidence")) if w.get("completion_evidence") else None),
                     ("下一步 / 等待", h(w.get("next_action") or w.get("waiting_on")) if (w.get("next_action") or w.get("waiting_on")) else None),
                     ("延期依据", f'{h(f["commitment_finish"])} · {h(f["commitment_source"])} · {h(f["delay_state_label"])}'),
                     ("风险说明", h(w.get("delay_risk_reason") or w.get("blocker_reason")) if (w.get("delay_risk_reason") or w.get("blocker_reason")) else None),
                     ("补救措施", remedy or None),
                     ("相关交付物", h(f["related_deliverables"]) if f["related_deliverables"] else None),
                     ("相关风险 / 问题", h(f["related_risks"]) if f["related_risks"] else None)]
            lines = ''.join(f'<p><b>{label}：</b>{value}</p>' for label, value in items if value)
            gaps = [label for label, value in items if not value]
            gap_line = '<p class="detail-gaps"><b>未填写：</b>' + ' · '.join(gaps) + '</p>' if gaps else ''
            return lines + gap_line + f'<p class="attendance-text"><b>考勤摘要：</b>{h(f["attendance_summary"])}</p>'
        def task(w, depth=0):
            f = tracking["tasks"][w["id"]]
            color = WORK_COLORS.get(w.get("work_type"), WORK_COLORS["other"])
            child_rows = children.get(w["id"], [])
            parent_note = f' · Todo {f["todo_done_count"]}/{f["todo_count"]}' if f["is_summary"] else ''
            finish = w.get("actual_finish") if w["status"] == "done" else w.get("forecast_finish")
            label = ("◇ " if w.get("is_acceptance_node") else "") + w["title"]
            row = (f'<summary><span class="task-label" style="padding-left:{12+min(depth,8)*13}px"><i class="lane-dot" style="background:{color}"></i><b>{h(label)}</b><small>{person(w.get("owner_id"))} · {h(ENUMS["work_stage"].get(w.get("stage"),ENUMS["work_type"][w["work_type"]]))}{h(parent_note) if parent_note else ""}</small></span>'
                   f'<span class="task-state">{_badge(w)}<small>{h(ENUMS["work_status"][w["status"]])}</small><span class="delay-text {h(f["delay_state"])}">{h(f["delay_state_label"])}</span></span>{track(w)}'
                   f'<span class="trace-date">{h(finish)}<small>{"实际完成" if w["status"]=="done" else "当前预计"}</small>{"<small>验收节点</small>" if w.get("is_acceptance_node") else ""}</span></summary>')
            attr = f'data-work-id="{h(w["id"])}" data-delay-state="{h(f["delay_state"])}"'
            if child_rows:
                content = '<details class="parent-explanation"><summary>查看父任务的验收、成员与补救依据</summary>'+details(w)+'</details>'
                row, aggregate_attr = group_row(w, descendants(w["id"]), depth, task_parent=True)
                return f'<details class="trace-parent" {attr} {aggregate_attr}>'+row+content+''.join(task(c,depth+1) for c in child_rows)+'</details>'
            return f'<details class="trace-task" {attr}>'+row+details(w)+'</details>'
        all_nodes = nodes + included
        def descendants(wid):
            result = {wid}
            while True:
                expanded = result | {n["id"] for n in all_nodes if n.get("parent_id") in result}
                if result == expanded:
                    return result
                result = expanded
        def group_row(node, ids, depth, task_parent=False):
            leaves = [by_id[wid] for wid in leaf_ids & visible & ids if by_id[wid]["status"] != "cancelled"]
            done = [w for w in leaves if w["status"] == "done" and w.get("actual_finish") and w.get("completion_evidence")]
            states = Counter(tracking["tasks"][w["id"]]["delay_state"] for w in leaves)
            def boundary(field, latest=False, fallback=None):
                values = [w.get(field) or (w.get(fallback) if fallback else None) for w in leaves]
                known = sorted(v for v in values if v)
                return (known[-1] if latest else known[0]) if known else None
            dates = {"baseline_start": boundary("baseline_start"), "baseline_finish": boundary("baseline_finish", True),
                     "forecast_start": boundary("forecast_start", fallback="baseline_start"), "forecast_finish": boundary("forecast_finish", True),
                     "actual_start": boundary("actual_start"), "actual_finish": boundary("actual_finish", True) if leaves and len(done) == len(leaves) else None}
            state = "overdue" if states["overdue"] else "risk" if states["risk"] else "unknown" if states["unknown"] else "on_track"
            partial = sum(not w.get("forecast_finish") for w in leaves)
            progress = f"{len(done)}/{len(leaves)}" + (f" · {len(done)/len(leaves)*100:.0f}%" if leaves else " · 待确认")
            deliveries = [d for d in data.get("deliverables", []) if d.get("work_item_id") in ids]
            accepted = sum(d.get("acceptance_status") == "accepted" for d in deliveries)
            acceptance = f"交付物验收 {accepted}/{len(deliveries)}" if deliveries else "未关联交付物"
            attr = f'data-group-id="{h(node["id"])}" data-leaf-count="{len(leaves)}" data-done-count="{len(done)}" data-overdue-count="{states["overdue"]}"'
            title = node.get("code") or node["id"]
            scope_note = "本窗口叶子项" if focus else "全部叶子项"
            ratio = len(done)/len(leaves)*100 if leaves else 0
            label = (f'<span class="task-label" style="padding-left:{12+min(depth,8)*13}px"><b>{h(title)}　{h(node["title"])}</b>'
                     f'<span class="mini-meter" title="{scope_note}确认完成 {progress}"><i style="width:{ratio:.1f}%"></i></span>'
                     f'<small>{scope_note} · 确认完成 {progress} · {acceptance}</small>'
                     f'<small>子项基线 {h(dates["baseline_start"])} → {h(dates["baseline_finish"])}</small></span>')
            status = (f'<span class="group-state"><b class="{"has-overdue" if states["overdue"] else ""}">未结延期 {states["overdue"]}</b><small>延期风险 {states["risk"]} · 延期交付 {states["late_done"]}</small>'
                      '<small class="expand-hint">展开二级任务与依据</small></span>')
            lane = track(dates, {"delay_state": state, "delay_days": max([tracking["tasks"][w["id"]]["delay_days"] for w in leaves] or [0])})
            date_label = f'<span class="trace-date">{h(dates["forecast_finish"] or "待确认")}<small>子项最晚预计</small>'+(f'<small>另有 {partial} 项日期待补</small>' if partial else '')+'</span>'
            return '<summary class="trace-group">'+label+status+lane+date_label+'</summary>', attr
        def branch(parent=None, depth=0):
            result = []
            for node in sorted((n for n in nodes if n.get("parent_id")==parent),key=lambda n:n["id"]):
                ids = descendants(node["id"])
                if not any(w["id"] in ids for w in included):
                    continue
                own = [w for w in included if w.get("parent_id")==node["id"]]
                row, attr = group_row(node, ids, depth)
                opened = ' open' if node.get("node_kind") == "project" else ''
                result.append(f'<details class="wbs-branch" {attr} data-node-kind="{h(node.get("node_kind"))}"{opened}>'+row+''.join(task(w,depth+1) for w in own)+branch(node["id"],depth+1)+'</details>')
            return ''.join(result)
        body = branch()
        node_ids = {n["id"] for n in nodes}
        extras = [w for w in roots if w.get("parent_id") not in node_ids]
        if extras:
            body += ''.join(task(w) for w in extras)
        if not body:
            body = '<div class="trace-empty">当前窗口没有匹配任务；完整项目仍可展开查看。</div>'
        scoped_leaves = [wid for wid in leaf_ids & visible if by_id[wid]["status"] != "cancelled"]
        states = Counter(tracking["tasks"][wid]["delay_state"] for wid in scoped_leaves)
        scope_title = "今天至本周日，含未结逾期结转" if focus else "完整项目"
        workweek_note = '' if focus else f'<span>已核验可用工作周 {calendar["verified_workweeks"]} · 零工作日周 {calendar.get("zero_workweeks",0)} · 待核验 {calendar["pending_weeks"]} 周</span>'
        summary = f'<div class="trace-scope-summary"><b>{scope_title}</b><span>{lo.isoformat()} — {hi.isoformat()} · {h(tracking["timezone"])}</span><span>叶子执行项 {len(scoped_leaves)}</span><span>未结延期 {states["overdue"]}</span><span>延期风险 {states["risk"]}</span><span>延期交付 {states["late_done"]}</span>{workweek_note}</div>'
        width = max(1000, len(calendar["weeks"])*175) if not focus else 1000
        return f'<div class="trace-scope-view" data-trace-view="{scope}" {"hidden" if focus else ""}>{summary}<div class="trace-panel"><div class="trace-heading"><b>WATERFALL · 报告快照</b><span>一级汇总 → 二级任务 → 事实依据</span></div><div class="trace-scroll"><div class="trace-inner" style="min-width:{width}px"><div class="trace-axis"><span>WBS / 负责人 / 叶子进度</span><span>状态 / 延期</span><div class="ticks">'+ticks+'</div><span>预计 / 实际完成</span></div>'+body+'</div></div><div class="trace-legend"><span><i class="original-key"></i>原计划（细线）</span><span><i class="forecast-key"></i>预测（彩条）</span><span><i class="actual-key"></i>实际（青绿）</span><span><i class="overdue-key"></i>事实延期（红色）</span><span><i class="risk-key"></i>延期风险（斜纹）</span><span><i class="late-key"></i>延期交付（双线）</span><span>◇ 验收节点</span><span>◇ / ◆ 预期 / 实际补救</span><span><i class="today-key"></i>数据截止日</span></div></div></div>'
    return ('<div class="trace-view-controls"><span class="seg"><button type="button" data-trace-scope="focus">本周 · 按日</button><button type="button" data-trace-scope="full" class="active">完整项目 · WK</button></span>'
            '<span class="seg"><button type="button" data-trace-expand="all">展开全部任务</button><button type="button" data-trace-expand="summary">收起至一级汇总</button></span><small>点击任务行查看日期、验收与补救依据</small></div>'+view("focus")+view("full")+
            _method_note('WK按项目起始所在周编号，周一至周日；调休按项目日历。首尾周仅统计项目日期范围内的工作日，假期周保留；项目开始日未设时周编号待确认。汇总条表示子项日期范围，父任务不重复计数；叶子进度不等于工时加权项目完成率，也不代替交付物验收。延期优先对照原计划完成日，未设原计划时对照当前预计日。斜纹表示延期风险；P0 只表示优先级。补救日期不抹掉历史延期。展开任务可查看完整日期、验收与人天依据。', "图例与统计口径"))


def export_project_report(data, path, *, as_of, kind="summary", timezone_name="America/Vancouver", demo=False, review_phase="pre_meeting", schedule_audit=None):
    if kind not in {"summary", "plan", "review"}:
        raise ValueError("未知报告类型")
    facts = weekly_facts(data, as_of, timezone_name)
    project, tracking, zone = facts["project"], facts["tracking"], facts["timezone"]
    people = {p["id"]:p for p in data.get("people", [])}
    work_index = {w["id"]:w for w in facts["all_works"]}
    person = lambda pid: h(people.get(pid,{}).get("name", "待指定"))
    def moment(value):
        return parse_datetime(value).astimezone(ZoneInfo(zone)).strftime("%m/%d %H:%M") if value else "—"
    def work_table(items, empty="暂无记录", show_next=False):
        if not items:
            return '<div class="empty">'+h(empty)+'</div>'
        rows = []
        for work in sorted(items,key=lambda w:(w.get("priority") or "P9",w.get("forecast_finish") or "9999",w["id"])):
            f = tracking["tasks"][work["id"]]
            parent = work_index.get(work.get("parent_id"))
            parent_note = '<small>父任务：'+h(parent["title"])+'</small>' if parent else ''
            next_note = '<small class="wrap">'+h(work.get("next_action") or work.get("blocker_reason") or work.get("acceptance_criteria"))+'</small>' if show_next else ''
            finish = work.get("actual_finish") if work["status"]=="done" else work.get("forecast_finish")
            finish_label = "实际" if work["status"]=="done" else "预计"
            rows.append(f'<tr><td>{_badge(work)}</td><td><b>{h(work["title"])}</b>{parent_note}{next_note}</td><td>{person(work.get("owner_id"))}</td><td>{h(ENUMS["work_status"][work["status"]])}<small>{_delay_badge(f)}</small></td><td>{finish_label} {h(finish)}<small>{h(work.get("actual_person_days"))} 人天（记录值）</small></td></tr>')
        return '<div class="table-scroll"><table><thead><tr><th>级别</th><th>工作与结果</th><th>负责人</th><th>状态 / 延期</th><th>日期 / 实际投入</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
    calendar_days = [d for d in data.get("calendar_days", []) if d["calendar_id"]==project["calendar_id"]]
    deviation = None
    if project.get("baseline_finish") and project.get("forecast_finish"):
        try:
            deviation = workday_distance(project["baseline_finish"],project["forecast_finish"],calendar_days)
        except ValueError:
            pass
    delta = "日期偏差待确认" if deviation is None else f"预测延后 {deviation} 个工作日" if deviation>0 else f"预测提前 {-deviation} 个工作日" if deviation<0 else "预测与原计划一致"
    counts = Counter(w["status"] for w in facts["works"] if w["status"]!="cancelled")
    project_counts = tracking["counts"]
    leaf_total = sum(counts.values())
    done_total = counts.get("done", 0)
    delta_tone = "neutral" if deviation is None else "critical" if deviation > 0 else "good"
    attention = sorted((w for w in facts["current"] if tracking["tasks"][w["id"]]["delay_state"] in ("overdue", "risk") or w.get("priority") == "P0"),
                       key=lambda w: (w.get("priority") != "P0", tracking["tasks"][w["id"]]["delay_state"] != "overdue", -tracking["tasks"][w["id"]].get("delay_days", 0), w["id"]))
    attention_rows = ''.join(
        f'<li><span class="att-badge">{_badge(w)}</span><span class="att-main"><b>{h(w["title"])}</b><small>{person(w.get("owner_id"))} · 对照 {h(tracking["tasks"][w["id"]]["commitment_finish"])} · 预计 {h(w.get("forecast_finish"))}</small></span>{_delay_badge(tracking["tasks"][w["id"]])}</li>'
        for w in attention[:5])
    more = len(attention) - 5
    attention_panel = ('<div class="panel attention"><div class="panel-head"><h3>当前最需关注</h3><button type="button" class="link-button" data-goto="4">延期与决策 →</button></div>'
                       + (f'<ul class="attention-list">{attention_rows}</ul>' if attention_rows else '<div class="empty">当前没有延期、延期风险或未完成 P0 的叶子执行项。</div>')
                       + (f'<p class="note">另有 {more} 项，见「05 延期与决策」与「02 进度 Waterfall」。</p>' if more > 0 else '<p class="note">P0 优先，其次按逾期天数排序。</p>') + '</div>')
    kpis = ''.join([
        _kpi("叶子执行项确认完成", f"{done_total}/{leaf_total}", tone="good", meter=done_total/leaf_total*100 if leaf_total else 0, sub="按条数，非工时加权", goto=1),
        _kpi("未结延期", project_counts["overdue"], tone="critical" if project_counts["overdue"] else "neutral", sub="已过对照日期仍未完成", goto=4),
        _kpi("延期风险", project_counts["risk"], tone="warning" if project_counts["risk"] else "neutral", sub="预测推迟或阻塞信号", goto=4),
        _kpi("延期交付", project_counts["late_done"], tone="serious" if project_counts["late_done"] else "neutral", sub="晚于对照日期完成", goto=1),
        _kpi("P0 未完成", len(facts["p0"]), tone="critical" if facts["p0"] else "neutral", sub="优先级，独立于延期", goto=4),
        _kpi("开放提醒与风险", len(facts["reminders"]), tone="warning" if facts["reminders"] else "neutral", sub="待处理的登记事项", goto=4)])
    overview = (f'<p class="eyebrow">01 / 项目概览</p><h1>{h(project.get("summary") or "整体结论待PM更新")}</h1><p class="lead">{h(project.get("business_impact") or "业务影响待确认")}</p>'
                f'<div class="hero"><div class="date-card"><span class="kpi-label">项目完成日期</span><div class="date-pair"><div><small>原计划完成</small><strong>{h(project.get("baseline_finish"))}</strong></div><div class="date-arrow">→</div><div><small>当前预计完成</small><strong>{h(project.get("forecast_finish"))}</strong></div></div>'
                f'<span class="date-delta tone-{delta_tone}">{h(delta)}</span><small class="date-foot">偏差按项目日历工作日计算</small></div><div class="kpi-grid">{kpis}</div></div>'
                f'<div class="overview-grid">{attention_panel}{_donut(counts,"全项目 · 叶子执行项状态")}{_priority_chart(facts["current"])}</div>')
    waterfall = '<p class="eyebrow">02 / 整体进度</p><h2>从交付工作包展开到任务与 Todo</h2>'+render_waterfall(data,facts["all_works"],as_of)
    seen, weekly_items = set(), []
    for work in facts["working"]+facts["due"]:
        if work["id"] not in seen:
            seen.add(work["id"]); weekly_items.append(work)
    accepted, pending = len(facts["accepted"]), len(weekly_items)
    pending_late = sum(tracking["tasks"][w["id"]]["delay_state"] == "overdue" for w in weekly_items)
    deliveries = [d for d in data.get("deliverables", []) if d.get("project_id")==project["id"]]
    delivery_counts = Counter(d["acceptance_status"] for d in deliveries)
    weekly_chart = ('<div class="kpi-row">'
                    + _kpi("本周已确认叶子项", accepted, tone="good" if accepted else "neutral", sub="周一至捕获日")
                    + _kpi("正在推进 / 本周待交付", pending, tone="info", sub="今天至周日，含逾期结转")
                    + _kpi("其中已延期", pending_late, tone="critical" if pending_late else "neutral", sub="已过对照日期")
                    + _kpi("交付物已验收", f"{delivery_counts.get('accepted', 0)}/{len(deliveries)}", tone="good", meter=delivery_counts.get('accepted', 0)/len(deliveries)*100 if deliveries else 0, sub=f"待验收 {delivery_counts.get('pending', 0)} · 退回 {delivery_counts.get('rejected', 0)}")
                    + '</div>'
                    + _method_note('本周完成回看从周一至捕获日；当前关注为今天至周日并含逾期结转。以上为叶子项数量，正式承诺兑现仍对照已冻结周一基线。'))
    delivery_rows = ''.join(f'<tr><td><b>{_link(d.get("document_url"),d["title"])}</b><small>{h(d.get("version_label"))} · {h(work_index.get(d["work_item_id"],{}).get("title",d["work_item_id"]))}</small></td><td><span class="acceptance {h(d["acceptance_status"])}">{h(ENUMS["deliverable_acceptance"][d["acceptance_status"]])}</span><small class="wrap">{h(d.get("acceptance_comments"))}</small></td><td>{person(d.get("submitted_by_id"))}<small>提交 {h(moment(d.get("submitted_at")))}</small></td><td>{person(d.get("acceptor_id"))}<small>验收 {h(moment(d.get("reviewed_at")))}</small></td></tr>' for d in deliveries)
    acceptance = (f'<div class="acceptance-heading"><h3>交付物验收</h3><span>项目 PM {person(project.get("pm_id"))}</span></div>'
                  +('<div class="table-scroll"><table><thead><tr><th>交付物 / 版本 / 任务</th><th>验收结果与意见</th><th>提交人</th><th>验收人</th></tr></thead><tbody>'+delivery_rows+'</tbody></table></div>' if deliveries else '<div class="empty">尚未登记交付物；阶段验收不由Todo完成率推断。</div>'))
    weekly = ('<p class="eyebrow">03 / 本周开发与交付</p><h2>执行结果与交付验收分别确认</h2>'+weekly_chart
              +'<div class="stack"><article><h3>正在推进与本周待交付 <span class="count">'+str(pending)+'</span></h3>'+work_table(weekly_items,"当前窗口暂无待交付任务",True)+'</article>'
              +'<article><h3>本周已确认交付 <span class="count">'+str(accepted)+'</span></h3>'+work_table(facts["accepted"],"本周尚无已确认完成的叶子执行项")+'</article></div>'+acceptance)
    meetings = []
    for meeting in sorted(facts["meetings"],key=lambda m:m["scheduled_at"]):
        direct = [w for w in facts["all_works"] if w.get("meeting_id")==meeting["id"]]
        action_ids = set()
        for w in direct:
            action_ids.update(tracking["tasks"][w["id"]]["leaf_descendant_ids"] or [w["id"]])
        linked = [w for w in facts["works"] if w["id"] in action_ids]
        minutes = meeting.get("minutes") if meeting["status"]=="held" else meeting.get("agenda")
        meetings.append(f'<article class="meeting-card"><div class="meeting-title"><h3>{h(meeting["title"])}</h3><span>{h(moment(meeting["scheduled_at"]))} · {h(ENUMS["meeting_status"][meeting["status"]])}</span></div><p class="minutes">{h(minutes or "内容待补充")}</p><h4>关联待办与跟进 <span class="count">{len(linked)}</span></h4>'+work_table(linked,"尚未关联待办；需要落实的结论请建立任务或子Todo",True)+'<p class="note">包含会议关联任务拆解出的叶子Todo；父任务不重复计数。</p></article>')
    meeting_page = '<p class="eyebrow">04 / 会议与待办</p><h2>会议结论落到负责人、Todo 与期限</h2>'+(''.join(meetings) or '<div class="empty">本周没有会议记录。无需为了汇报补造会议。</div>')
    risk_cards = []
    for r in sorted(facts["reminders"],key=lambda x:(x.get("priority") or "P9",x.get("deadline_at") or "9999")):
        risk_cards.append(f'<article class="risk-card kind-{h(r["kind"])}"><div class="risk-top"><span class="risk-kind">{h(ENUMS["register_type"][r["kind"]])}</span>{_badge(r)}<span class="risk-due">最迟 {h(moment(r.get("deadline_at")))}</span></div><h3>{h(r["title"])}</h3><p>{h(r.get("impact") or "影响待补充")}</p><p class="next"><b>下一步</b>{h(r.get("next_action") or r.get("proposal") or "待明确")}</p><small>处理 {person(r.get("owner_id"))} · 协调 {person(r.get("coordinator_id"))} · 决定 {person(r.get("decision_by_id"))}</small></article>')
    decisions = ('<h3 class="block-title">待决定事项与风险提醒 <span class="count">'+str(len(risk_cards))+'</span></h3><div class="risk-grid">'+(''.join(risk_cards) or '<div class="empty">当前没有开放提醒与风险。</div>')+'</div>'
                 '<div class="p0-box"><h3>全项目未完成 P0 · 优先级独立跟进 <span class="count">'+str(len(facts["p0"]))+'</span></h3>'+work_table(facts["p0"],"当前没有未完成的P0叶子执行项",True)+'</div>'
                 '<p class="note">补救落实与任务完成分别确认；延期事实不会因补救时间已填写而消失。</p>')
    interfaces = [x for x in data.get("interfaces", []) if x["project_id"]==project["id"]]
    docs = [x for x in data.get("project_documents", []) if x["project_id"]==project["id"]]
    interface_rows = ''.join(f'<tr><td><b>{h(x["name"])}</b><small>{h(x["source_system"])} → {h(x["target_system"])}</small></td><td>{h(x.get("method") or ENUMS["interface_protocol"][x["protocol"]])}<small class="address">{h(x["address"])}</small></td><td>{person(x["owner_id"])}<small>对方 {person(x.get("counterparty_id"))}</small></td><td><span class="status-chip st-{h(x["status"])}">{h(ENUMS["interface_status"][x["status"]])}</span><small>预计就绪 {h(x.get("expected_ready"))}</small></td></tr>' for x in interfaces)
    doc_cards = ''.join(f'<article class="doc-card"><small>{h(ENUMS["document_type"][x["document_type"]])} · {h(ENUMS["document_status"][x["status"]])}</small><h3>{_link(x["url"],x["title"])}</h3><p>{person(x["owner_id"])} · {h(x.get("version_label"))}</p></article>' for x in docs)
    roster = ''.join(f'<tr><td>{h(p["name"])}<small>{h(ENUMS["person_type"].get(p.get("person_type"),"成员"))}</small></td><td>{h(p.get("company"))}<small>{h(p.get("department"))}</small></td><td>{h(p.get("project_role"))}<small>{h(p.get("expertise"))}</small></td><td>{h(p.get("starts_on"))}<small>至 {h(p.get("ends_on"))}</small></td></tr>' for p in data.get("people", []))
    appendix = '<p class="eyebrow">06 / 接口与项目文档</p><h2>需要落实时，找到接口、资料和对应的人</h2><h3 class="block-title">项目接口 <span class="count">'+str(len(interfaces))+'</span></h3><div class="table-scroll"><table><thead><tr><th>接口 / 方向</th><th>方式 / 地址</th><th>项目与对方接口人</th><th>状态 / 就绪时间</th></tr></thead><tbody>'+interface_rows+'</tbody></table></div><h3 class="block-title">项目文档 <span class="count">'+str(len(docs))+'</span></h3><div class="doc-grid">'+(doc_cards or '<div class="empty">项目文档待登记。</div>')+'</div><details class="roster-details"><summary>展开项目通讯录</summary><div class="table-scroll"><table><thead><tr><th>成员</th><th>公司 / 部门</th><th>职责</th><th>参与窗口</th></tr></thead><tbody>'+roster+'</tbody></table></div></details>'
    titles = ["项目概览", "进度 Waterfall", "开发与验收", "会议与Todo", "延期与决策", "接口与文档"]
    from .operating import render_management_section
    risk_page = '<p class="eyebrow">05 / 延期与决策</p>' + render_management_section(data, as_of=as_of, phase=review_phase, schedule_audit=schedule_audit, decisions_html=decisions)
    sections = [overview, waterfall, weekly, meeting_page, risk_page, appendix]
    title = {"summary":"本周项目进度汇报", "plan":"本周计划汇报", "review":"本周总结汇报"}[kind]
    header = (f'<header><div class="top"><div><div class="brand-label">pmmaster / PROJECT DELIVERY</div><strong>{h(project["name"])}</strong><small>{h(title)} · {facts["start"].isoformat()} — {facts["end"].isoformat()}</small></div>'
              f'<div class="header-meta"><small>数据截至 <b>{facts["now"].strftime("%Y-%m-%d %H:%M")}</b> · {h(zone)}</small><small>本周关注窗口 {tracking["today"]} — {tracking["week_end"]}</small>'
              +('<span class="demo">虚构演示项目 · 不代表真实业务事实</span>' if demo else '')+'</div></div><nav>'
              +''.join(f'<button type="button" data-page="{i}" class="{"active" if i==0 else ""}"><span class="nav-index">{i+1:02d}</span>{h(text)}</button>' for i,text in enumerate(titles))+'</nav></header>')
    pages = ''.join(f'<section class="page {"active" if i==0 else ""}" data-section="{i}">{content}<div class="page-foot"><span>{h(project["name"])} · PO {person(project["po_id"])} / PM {person(project["pm_id"])}</span><span>{i+1:02d} / 06</span></div></section>' for i,content in enumerate(sections))
    output = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+h('pmmaster · '+project["name"]+' · '+title)+'</title><style>'+REPORT_CSS+'</style></head><body>'+header+'<main>'+pages+'<div class="bottom-actions"><span>此文件保留捕获时点的项目事实；更新数据后可通过智能体生成新版报告。</span><button id="print" class="print-button">打印 / 保存报告为 PDF</button></div></main><script>'+REPORT_SCRIPT+'</script></body></html>'
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(output, encoding="utf-8")
    return {"ok":True, "path":str(path), "project_id":project["id"], "kind":kind, "as_of":as_of,
            "week_start":facts["start"].isoformat(), "week_end":facts["end"].isoformat(),
            "window_start":tracking["today"], "window_end":tracking["week_end"], "timezone":zone,
            "leaf_counts":tracking["counts"], "focus_counts":tracking.get("focus_counts",{}),
            "focused_work_ids":tracking["focus_ids"], "sections":titles, "demo":demo}


REPORT_SCRIPT = """document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-page]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('[data-section]').forEach(x=>x.classList.toggle('active',x.dataset.section===b.dataset.page));window.scrollTo({top:0,behavior:'smooth'})}));document.querySelectorAll('[data-trace-scope]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-trace-scope]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('[data-trace-view]').forEach(x=>x.hidden=x.dataset.traceView!==b.dataset.traceScope)}));document.querySelectorAll('[data-trace-expand]').forEach(b=>b.addEventListener('click',()=>{const view=document.querySelector('[data-trace-view]:not([hidden])');if(view)view.querySelectorAll('details').forEach(d=>d.open=(b.dataset.traceExpand==='all'&&(d.classList.contains('wbs-branch')||d.classList.contains('trace-parent')))||d.dataset.nodeKind==='project')}));document.querySelectorAll('[data-goto]').forEach(t=>{const go=()=>{const b=document.querySelector('nav [data-page="'+t.dataset.goto+'"]');if(b)b.click()};t.addEventListener('click',go);t.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go()}})});document.getElementById('print').addEventListener('click',()=>window.print());"""

REPORT_CSS = "\n" + css_root() + r'''
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:14px/1.6 var(--font)}a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
header{padding:18px 2% 0;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}.top{display:flex;justify-content:space-between;gap:24px;align-items:flex-start}.brand-label{font-size:11px;letter-spacing:.14em;color:var(--faint);margin-bottom:4px}header strong{font-size:22px;font-weight:700;letter-spacing:-.2px}header small{display:block;color:var(--muted);font-size:12px;margin-top:2px}header small b{color:var(--ink);font-weight:600}.header-meta{text-align:right}
nav{display:flex;gap:2px;flex-wrap:wrap;margin-top:14px}nav button{border:0;border-bottom:3px solid transparent;border-radius:0;background:none;color:var(--muted);padding:10px 14px 11px;font-size:14px}nav button:hover{color:var(--ink);background:var(--soft)}nav button.active{background:none;color:var(--ink);border-color:var(--ink);font-weight:650}.nav-index{font-size:11px;color:var(--faint);margin-right:8px;font-variant-numeric:tabular-nums}nav button.active .nav-index{color:var(--ink)}
button{border:1px solid var(--line);background:var(--card);color:var(--ink-2);border-radius:8px;padding:7px 13px;cursor:pointer;font:13px/1.5 inherit}button:hover{border-color:var(--neutral)}button.active{background:var(--ink);color:var(--card);border-color:var(--ink)}
main{width:96%;max-width:none;margin:22px auto;padding:0}.page{display:none;background:var(--card);padding:30px 32px;border-radius:var(--radius);min-height:640px;border:1px solid var(--line)}.page.active{display:block}
.eyebrow{color:var(--faint);font-size:12px;letter-spacing:.08em;font-weight:600;margin:0 0 4px}h1{font-size:26px;line-height:1.45;max-width:1180px;margin:6px 0 8px;font-weight:700;letter-spacing:-.3px}h2{font-size:22px;line-height:1.45;margin:4px 0 20px;font-weight:700;letter-spacing:-.2px}h3{font-size:16px;margin:0 0 12px;font-weight:650}h4{font-size:14px;margin:18px 0 8px}.lead{font-size:15px;color:var(--ink-2);max-width:1180px;margin:0 0 22px}.note{font-size:12px;color:var(--muted);margin:10px 0}.count{display:inline-block;min-width:22px;text-align:center;font-size:12px;font-weight:600;color:var(--ink-2);background:var(--soft-2);border-radius:999px;padding:1px 8px;margin-left:6px;vertical-align:2px}.block-title{margin:30px 0 12px}
.method-note{margin:14px 0 0;font-size:12px;color:var(--muted)}.method-note>summary{cursor:pointer;color:var(--muted);list-style:none;display:inline-flex;align-items:center;gap:6px}.method-note>summary::-webkit-details-marker{display:none}.method-note>summary:before{content:'ⓘ';font-size:13px}.method-note>summary:hover{color:var(--ink)}.method-note p{margin:8px 0 0;padding:12px 14px;background:var(--soft);border-radius:10px;line-height:1.75;max-width:1180px}
.kpi-row,.kpi-grid{display:grid;gap:12px}.kpi-row{grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin:0 0 6px}.kpi-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.kpi{position:relative;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px 13px 19px;min-width:0;overflow:hidden}.kpi:before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--line-strong)}.kpi[data-goto]{cursor:pointer}.kpi[data-goto]:hover{border-color:var(--neutral);box-shadow:0 2px 10px var(--grid)}.kpi[data-goto]:focus-visible{outline:2px solid var(--ink);outline-offset:2px}.kpi-label{display:block;font-size:13px;color:var(--ink-2);font-weight:500}.kpi-value{display:block;font-size:32px;line-height:1.15;font-weight:700;margin:6px 0 2px;letter-spacing:-.5px;color:var(--ink)}.kpi small{display:block;font-size:12px;color:var(--muted);margin-top:4px}.meter{display:block;height:6px;border-radius:3px;background:var(--soft-2);overflow:hidden;margin:8px 0 2px}.meter i{display:block;height:100%;border-radius:3px;background:var(--good)}
.tone-critical:before{background:var(--critical)}.tone-serious:before{background:var(--serious)}.tone-warning:before{background:var(--warning)}.tone-good:before{background:var(--good)}.tone-info:before{background:var(--info)}.kpi.tone-neutral .kpi-value{color:var(--ink-2)}
.hero{display:grid;grid-template-columns:minmax(300px,1fr) 2fr;gap:14px;margin:0 0 16px}.date-card{background:var(--soft);color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:18px 22px;display:flex;flex-direction:column;gap:10px}.date-card .kpi-label{color:var(--ink-2)}.date-pair{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-top:auto}.date-pair small{display:block;color:var(--muted);font-size:12px}.date-pair strong{font-size:30px;font-weight:700;letter-spacing:-.6px;font-variant-numeric:tabular-nums}.date-pair>div:first-child strong{color:var(--ink-2)}.date-arrow{font-size:24px;color:var(--faint)}.date-delta{align-self:flex-start;border-radius:999px;padding:5px 12px;font-size:13px;font-weight:600;background:var(--soft-2);color:var(--ink-2)}.date-delta:before{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;background:var(--faint);vertical-align:1px}.date-delta.tone-critical{background:var(--critical-bg);color:var(--critical-ink)}.date-delta.tone-critical:before{background:var(--critical)}.date-delta.tone-good{background:var(--good-bg);color:var(--good-ink)}.date-delta.tone-good:before{background:var(--good)}.date-foot{color:var(--muted)!important;font-size:11px}
.overview-grid{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:14px}.panel,.chart-card{border:1px solid var(--line);border-radius:12px;padding:18px 20px;min-width:0;background:var(--card)}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:6px}.panel-head h3{margin:0}.link-button{border:0;background:none;color:var(--link);padding:2px 0;font-size:13px}.link-button:hover{text-decoration:underline}
.attention-list{list-style:none;margin:0;padding:0}.attention-list li{display:grid;grid-template-columns:34px minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line-2)}.attention-list li:last-child{border-bottom:0}.att-main b{display:block;font-weight:600;font-size:14px;overflow-wrap:anywhere}.att-main small{display:block;font-size:12px;color:var(--muted);margin-top:2px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:14px}.donut-wrap{display:flex;align-items:center;gap:18px}.donut-wrap svg{width:150px;flex:0 0 150px}.donut-number{font-size:34px;fill:var(--ink);font-weight:700}.donut-label{font-size:11px;fill:var(--muted)}.chart-legend{list-style:none;padding:0;margin:0;flex:1;font-size:13px;min-width:0}.chart-legend li{display:flex;align-items:center;gap:9px;margin:6px 0;color:var(--ink-2)}.chart-legend i{width:10px;height:10px;border-radius:3px;flex:none}.chart-legend b{margin-left:auto;color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}.bar-row{display:grid;grid-template-columns:78px 1fr 26px;gap:12px;align-items:center;margin:12px 0;font-size:13px;color:var(--ink-2)}.bar-row small{font-size:11px;color:var(--muted)}.bar-bed{background:var(--soft-2);height:12px;border-radius:0 4px 4px 0;overflow:hidden}.bar-bed i{display:block;height:100%;border-radius:0 4px 4px 0}.bar-row b{font-weight:600;text-align:right;font-variant-numeric:tabular-nums;color:var(--ink)}
.red{color:var(--critical-ink)!important}.gold{color:var(--warning-ink)!important}.green{color:var(--good-ink)!important}
/* Waterfall shares the page's light neutral surfaces; marks are stepped darker for the light canvas and patterns keep meaning without color. */
.trace-view-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 14px}.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;padding:2px;background:var(--soft)}.seg button{border:0;background:none;border-radius:7px;padding:6px 13px}.seg button.active{background:var(--ink);color:var(--card)}.trace-view-controls small{font-size:12px;color:var(--muted)}.trace-scope-summary{display:flex;gap:8px 16px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--ink-2);margin:0 0 12px}.trace-scope-summary b{color:var(--ink);font-size:13px}
.trace-panel{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:12px;overflow:hidden}.trace-heading{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:16px}.trace-heading b{font-size:12px;letter-spacing:.1em}.trace-heading span{font-size:12px;color:var(--muted)}.trace-scroll{overflow:auto;max-width:100%;scrollbar-color:var(--neutral) var(--page)}.trace-inner{min-width:1000px}.trace-axis,.trace-task>summary,.trace-parent>summary,.wbs-branch>summary{display:grid;grid-template-columns:28% 13% 48% 11%;align-items:center;padding:0 16px}.trace-axis{height:75px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line);background:var(--soft)}.ticks{height:68px;position:relative}.ticks span{position:absolute;top:6px;bottom:0;padding-left:6px;white-space:nowrap;border-left:1px solid var(--line-strong);font-size:11px}.ticks b{font-size:13px;color:var(--ink)}.ticks small{display:block;font-size:10px;color:var(--muted)}
.wbs-branch>summary,.trace-parent>summary,.trace-task>summary{list-style:none;cursor:pointer;min-height:84px;border-bottom:1px solid var(--line-2)}.wbs-branch>summary::-webkit-details-marker,.trace-parent>summary::-webkit-details-marker,.trace-task>summary::-webkit-details-marker{display:none}.wbs-branch>summary{background:var(--band)}.wbs-branch[data-node-kind="project"]>summary{background:var(--band-strong)}.trace-parent>summary{background:var(--soft)}.trace-task>summary{background:var(--card)}.trace-task>summary:hover,.trace-parent>summary:hover,.wbs-branch>summary:hover{background:var(--hover)}.trace-group .task-label b:before,.trace-parent>summary .task-label b:before{content:'▸ ';color:var(--ink)}.wbs-branch[open]>.trace-group .task-label b:before,.trace-parent[open]>summary .task-label b:before{content:'▾ '}
.task-label{position:relative;min-width:0;font-size:13px}.task-label b{display:block;font-weight:600;padding-right:10px;overflow-wrap:anywhere}.task-label small{display:block;color:var(--muted);font-size:11px;margin-top:4px;line-height:1.6}.mini-meter{display:block;width:min(180px,80%);height:4px;border-radius:2px;background:var(--line);margin:7px 0 2px;overflow:hidden}.mini-meter i{display:block;height:100%;background:var(--good);border-radius:2px}.lane-dot{position:absolute;left:0;top:8px;width:5px;height:5px;border-radius:1px}.task-state,.group-state{padding-right:7px;font-size:12px}.task-state small,.group-state small{display:block;color:var(--muted);font-size:11px;margin-top:4px}.group-state b{font-weight:600;font-size:13px}.group-state b.has-overdue{display:inline-block;background:var(--critical-bg);color:var(--critical-ink);border-radius:4px;padding:1px 7px}.group-state .expand-hint{display:none}
.priority{font-size:11px;font-weight:700;display:inline-block;border-radius:4px;padding:1px 6px;background:var(--soft-2);color:var(--ink-2);line-height:1.5}.priority.P0{background:var(--ink);color:var(--card)}.priority.P1{background:var(--line-strong);color:var(--ink)}.priority.P3,.priority.ungraded{background:var(--soft-2);color:var(--muted)}
.delay-text{display:block;font-size:11px;color:var(--muted);margin-top:4px}.delay-text.overdue,.delay-text.late_done{color:var(--critical-ink);font-weight:650}.delay-text.risk{color:var(--warning-ink);text-decoration:underline dotted}
.timeline-track{height:74px;position:relative;overflow:hidden;background:repeating-linear-gradient(90deg,transparent 0,transparent calc(var(--tick-width) - 1px),var(--grid) calc(var(--tick-width) - 1px),var(--grid) var(--tick-width))}.timeline-track i{position:absolute;display:block;border-radius:2px;min-width:3px}.original{height:3px;top:13px;background:var(--wf-baseline)}.forecast{height:10px;top:30px;background:var(--lane-color);filter:var(--wf-forecast-filter)}.forecast.overdue{background:var(--critical);filter:none}.forecast.risk{background:repeating-linear-gradient(135deg,var(--warning) 0,var(--warning) 4px,var(--warning-line) 4px,var(--warning-line) 6px);filter:none}.forecast.unknown{background:var(--soft-2);border:1px dashed var(--faint);filter:none}.actual{height:7px;top:48px;background:var(--good)}.actual.late_done{background:var(--good-bg);border:3px double var(--critical);height:9px}.actual-start{height:7px;top:48px;background:var(--good);width:4px;transform:translateX(-50%)}.forecast-point{width:8px;height:8px;top:30px;transform:translateX(-50%);border-radius:50%!important}.acceptance-node{width:10px;height:10px;top:29px;transform:translateX(-50%) rotate(45deg);background:var(--card);border:2px solid var(--wf-acceptance);border-radius:0!important}.remediation{top:62px;width:7px;height:7px;transform:translateX(-50%) rotate(45deg);border:1.5px solid var(--wf-remediation);border-radius:0!important}.remediation.expected{background:var(--card)}.remediation.actual-remediation{background:var(--wf-remediation)}.carryover{left:0;top:30px;width:10px;height:10px;background:var(--critical);clip-path:polygon(0 50%,45% 0,100% 0,100% 100%,45% 100%)}.carryover-text{position:absolute;top:25px;left:16px;font-size:11px;color:var(--critical-ink);background:var(--card);border-radius:4px;padding:1px 6px;z-index:3;font-weight:600}.today{position:absolute;top:0;bottom:0;width:2px;background:var(--ink);opacity:var(--wf-cutoff-opacity);z-index:2}.unplanned{display:block;color:var(--faint);font-size:11px;line-height:72px;padding-left:7px}
.trace-date{text-align:right;font-size:12px;color:var(--ink);padding-left:7px;font-variant-numeric:tabular-nums}.trace-date small{display:block;font-size:10px;color:var(--muted);margin-top:4px}.trace-detail{padding:18px 24px;background:var(--soft);color:var(--ink-2);font-size:13px;border-left:3px solid var(--neutral);border-bottom:1px solid var(--line-2)}.trace-detail p{margin:9px 0;font-size:12px;overflow-wrap:anywhere}.trace-detail b{font-weight:600;color:var(--ink)}.trace-detail-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px 20px}.trace-detail-grid small{display:block;font-size:11px;color:var(--muted);margin-bottom:4px}.attendance-text{white-space:pre-line}.trace-detail p.detail-gaps{color:var(--faint)}.trace-detail p.detail-gaps b{color:inherit;font-weight:500}.parent-explanation>summary{padding:9px 26px;color:var(--muted);font-size:11px;cursor:pointer;background:var(--soft)}
.trace-legend{display:flex;gap:16px;flex-wrap:wrap;padding:13px 18px;border-top:1px solid var(--line);font-size:11px;color:var(--ink-2);background:var(--soft)}.trace-legend span{display:flex;align-items:center;gap:6px}.trace-legend i{display:block;width:18px;height:6px;border-radius:1px}.original-key{background:var(--wf-baseline);height:3px!important}.forecast-key{background:var(--type-feature)}.actual-key{background:var(--good)}.overdue-key{background:var(--critical)}.risk-key{background:repeating-linear-gradient(135deg,var(--warning) 0,var(--warning) 3px,var(--warning-line) 3px,var(--warning-line) 5px)}.late-key{border:3px double var(--critical)}.today-key{width:2px!important;height:14px!important;background:var(--ink);opacity:var(--wf-cutoff-opacity)}.trace-empty{padding:32px;color:var(--muted);font-size:13px}
.table-scroll{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;font-size:13px;text-align:left}th{background:var(--soft);color:var(--muted);font-weight:500;white-space:nowrap;font-size:12px}th,td{padding:12px 14px;border-bottom:1px solid var(--line-2);vertical-align:top}tbody tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--soft)}td{color:var(--ink-2)}td b{color:var(--ink);font-weight:600}td small{display:block;color:var(--muted);font-size:12px;margin-top:3px}.wrap{white-space:pre-wrap;overflow-wrap:anywhere}
.stack{display:grid;gap:22px;margin-top:22px}.stack article{min-width:0}.split{display:grid;grid-template-columns:1fr 1.5fr;gap:22px;margin-top:20px}.split article{min-width:0}.empty{padding:16px 18px;background:var(--soft);color:var(--muted);border-radius:10px;font-size:13px;border:1px dashed var(--line)}
.delay-badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:500;color:var(--muted);background:var(--soft-2);white-space:nowrap}.delay-badge.overdue{background:var(--critical-bg);color:var(--critical-ink)}.delay-badge.late_done{background:var(--card);border:1px solid var(--critical-line);color:var(--critical-ink)}.delay-badge.risk{background:repeating-linear-gradient(135deg,var(--warning-bg) 0,var(--warning-bg) 5px,var(--warning-line) 5px,var(--warning-line) 6px);color:var(--warning-ink)}.delay-badge.on_track{background:var(--good-bg);color:var(--good-ink)}
.acceptance-heading{margin:30px 0 12px;display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap}.acceptance-heading h3{margin:0}.acceptance-heading span{font-size:12px;color:var(--muted)}.acceptance,.status-chip{display:inline-block;font-size:12px;font-weight:500;border-radius:999px;padding:1px 9px;background:var(--soft-2);color:var(--ink-2)}.acceptance.accepted{background:var(--good-bg);color:var(--good-ink)}.acceptance.pending{background:var(--warning-bg);color:var(--warning-ink)}.acceptance.rejected{background:var(--critical-bg);color:var(--critical-ink)}.status-chip.st-blocked{background:var(--critical-bg);color:var(--critical-ink)}.status-chip.st-ready,.status-chip.st-live,.status-chip.st-done{background:var(--good-bg);color:var(--good-ink)}.status-chip.st-developing,.status-chip.st-testing,.status-chip.st-in_progress{background:var(--info-bg);color:var(--info-ink)}
.meeting-card{border:1px solid var(--line);padding:22px;border-radius:12px;margin-top:16px}.meeting-title{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.meeting-title span{color:var(--muted);font-size:12px}.minutes{white-space:pre-wrap;background:var(--soft);color:var(--ink-2);padding:14px 16px;border-left:3px solid var(--neutral);border-radius:0 8px 8px 0;font-size:13px}
.p0-box{border:1px solid var(--critical-line);padding:18px 20px;border-radius:12px;background:var(--card);margin-top:22px}.p0-box h3{color:var(--critical-ink)}.p0-box .table-scroll{background:var(--card)}
.risk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}.risk-card{border:1px solid var(--line);border-top:3px solid var(--line-strong);border-radius:12px;padding:16px 18px;display:flex;flex-direction:column}.risk-card.kind-decision{border-top-color:var(--info)}.risk-card.kind-risk,.risk-card.kind-issue{border-top-color:var(--warning)}.risk-top{display:flex;align-items:center;gap:8px}.risk-due{margin-left:auto;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}.risk-card h3{margin:10px 0 4px;font-size:15px}.risk-card p{font-size:13px;color:var(--ink-2);margin:4px 0}.risk-card p.next{margin-top:8px;padding:8px 10px;background:var(--soft);border-radius:8px}.risk-card p.next b{display:block;font-size:12px;color:var(--muted);font-weight:500}.risk-card small{font-size:12px;color:var(--muted);margin-top:auto;padding-top:10px}.risk-kind{font-size:12px;color:var(--ink-2);background:var(--soft-2);border-radius:999px;padding:1px 9px}
.doc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}.doc-card{padding:16px 18px;border:1px solid var(--line);border-radius:12px}.doc-card small,.doc-card p{font-size:12px;color:var(--muted)}.doc-card h3{margin:8px 0 6px;font-size:15px}.address{word-break:break-all}.roster-details{border-top:1px solid var(--line);margin-top:24px;padding-top:15px}.roster-details>summary{font-size:13px;color:var(--ink-2);cursor:pointer;margin-bottom:12px}
.demo{display:inline-block;color:var(--warning-ink);background:var(--warning-bg);border-radius:999px;font-size:12px;padding:3px 10px;margin-top:6px}.page-foot{display:flex;justify-content:space-between;gap:18px;color:var(--faint);font-size:11px;margin-top:30px;border-top:1px solid var(--line-2);padding-top:12px}.bottom-actions{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:16px 0 30px}.bottom-actions span{font-size:12px;color:var(--muted)}[hidden]{display:none!important}
/* Management review: delay cards, resources, stage checks, schedule audit. */
.section-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:4px}.section-head h2{margin:0}.phase-chip{font-size:12px;font-weight:600;border-radius:999px;padding:2px 10px;background:var(--info-bg);color:var(--info-ink)}.phase-chip.post{background:var(--good-bg);color:var(--good-ink)}.mgmt-note{margin:6px 0 18px}
.missing{display:inline-block;font-size:12px;color:var(--faint);border:1px dashed var(--line-strong);border-radius:6px;padding:0 6px;line-height:1.6;font-weight:400}
.delay-list{display:grid;gap:10px}.delay-card{display:grid;grid-template-columns:minmax(220px,1.25fr) repeat(3,minmax(0,1fr));gap:0;border:1px solid var(--line);border-left:4px solid var(--line-strong);border-radius:12px;overflow:hidden;background:var(--card)}.delay-card.overdue{border-left-color:var(--critical)}.delay-card.risk{border-left-color:var(--warning)}.delay-card.late_done{border-left-color:var(--serious)}.delay-card>div{padding:13px 16px;min-width:0;border-left:1px solid var(--line-2)}.delay-card>div:first-child{border-left:0}.dc-main b{display:block;font-size:14px;font-weight:650;overflow-wrap:anywhere}.dc-main small{display:block;font-size:12px;color:var(--muted);margin:2px 0 7px}.date-shift{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px;font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums}.date-shift span{color:var(--muted)}.date-shift .to{font-weight:650;color:var(--ink)}.dc-field label{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}.dc-field .val{font-size:13px;color:var(--ink);overflow-wrap:anywhere}.dc-field small{display:block;font-size:12px;color:var(--ink-2);margin-top:4px;overflow-wrap:anywhere}.dc-field small .k{color:var(--muted)}.dc-source{margin-top:6px;font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:2px 14px}.dc-source small{font-size:12px}
.people-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}.person-card{border:1px solid var(--line);border-radius:12px;padding:14px 16px}.person-card .who{display:flex;justify-content:space-between;align-items:baseline;gap:10px}.person-card .who b{font-size:15px}.person-card .who span{font-size:12px;color:var(--muted)}.person-card small{display:block;font-size:12px;color:var(--muted);margin-top:4px}.cap-bar{display:flex;gap:2px;height:12px;margin:10px 0 6px;border-radius:0 4px 4px 0;overflow:hidden;background:var(--soft-2)}.cap-bar i{display:block;height:100%}.cap-bar .cap-project{background:var(--info)}.cap-bar .cap-ops{background:var(--cap-ops)}.cap-bar .cap-buffer{background:var(--neutral)}.cap-bar .cap-free{background:var(--soft-2)}.cap-bar.unknown{background:repeating-linear-gradient(135deg,var(--soft-2) 0,var(--soft-2) 5px,var(--line) 5px,var(--line) 6px)}.cap-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--ink-2)}.cap-legend span:before{content:'';display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px;background:var(--k)}.cap-nums{font-variant-numeric:tabular-nums}
.chip-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0;font-size:13px}.chip-row>b{font-size:13px;color:var(--ink-2);font-weight:500;margin-right:2px}.chip{display:inline-block;font-size:12px;border-radius:999px;padding:2px 10px;background:var(--soft-2);color:var(--ink-2)}.chip.warn{background:var(--warning-bg);color:var(--warning-ink)}.chip.ok{background:var(--good-bg);color:var(--good-ink)}
.confirm-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;margin:8px 0 4px}.confirm-item{border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:13px}.confirm-item .head{display:flex;justify-content:space-between;gap:10px}.confirm-item small{display:block;font-size:12px;color:var(--muted);margin-top:3px}
.mgmt-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:8px}.mgmt-grid .confirm-list,.mgmt-grid .people-grid{grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}.mgmt-grid>section{border:1px solid var(--line);border-radius:12px;padding:16px 18px;min-width:0}.mgmt-grid h3{font-size:15px}.leave-list,.check-list{list-style:none;margin:0;padding:0}.leave-list li{display:grid;grid-template-columns:minmax(70px,auto) 1fr auto;gap:12px;padding:9px 0;border-bottom:1px solid var(--line-2);font-size:13px;align-items:baseline}.leave-list li:last-child,.check-list li:last-child{border-bottom:0}.leave-list small{display:block;font-size:12px;color:var(--muted)}.check-list li{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line-2);font-size:13px}.check-list .mark{width:20px;height:20px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex:none}.check-list .ok .mark{background:var(--good-bg);color:var(--good-ink)}.check-list .gap .mark{background:var(--warning-bg);color:var(--warning-ink)}.check-list .state{margin-left:auto;font-size:12px;color:var(--muted)}
.forecast-grid{table-layout:fixed;min-width:760px}.forecast-grid th:first-child{width:96px}.forecast-grid td,.forecast-grid th{text-align:center;white-space:nowrap;padding:8px 6px}.forecast-grid td:first-child,.forecast-grid th:first-child{text-align:left}.fc-cell{display:block;border-radius:8px;padding:6px 8px;background:var(--soft);font-variant-numeric:tabular-nums}.fc-cell b{display:block;font-size:14px}.fc-cell small{font-size:11px;margin:0}.fc-cell.known{background:var(--info-bg)}.fc-cell.known.full{background:var(--serious-bg)}.fc-cell.unknown{background:repeating-linear-gradient(135deg,var(--soft) 0,var(--soft) 5px,var(--line-2) 5px,var(--line-2) 6px);color:var(--muted)}.fc-cell.outside{background:none;color:var(--faint);font-size:12px}.field-change .lbl{min-width:190px!important}.fc-leave{display:block;font-size:11px;color:var(--warning-ink);margin-top:3px}
.schedule-audit{margin-top:24px;border:1px solid var(--line);border-radius:12px;padding:18px 20px}.schedule-audit h3{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.audit-count{font-size:12px;font-weight:600;border-radius:999px;padding:1px 10px;background:var(--soft-2);color:var(--ink-2)}.audit-count.alert{background:var(--critical-bg);color:var(--critical-ink)}.schedule-audit .audit-lead{font-size:13px;color:var(--ink-2);margin:0 0 10px}.audit-signal{border:1px solid var(--line);border-left:4px solid var(--line-strong);border-radius:10px;padding:12px 16px;margin:10px 0;background:var(--card)}.audit-signal.sev-high{border-left-color:var(--critical)}.audit-signal.sev-medium{border-left-color:var(--warning)}.audit-signal .sig-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.audit-signal .sig-head b{font-size:14px}.sev{font-size:12px;border-radius:999px;padding:1px 9px;background:var(--soft-2);color:var(--ink-2)}.sev.high{background:var(--critical-bg);color:var(--critical-ink)}.sev.medium{background:var(--warning-bg);color:var(--warning-ink)}.field-change{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:10px 0;font-variant-numeric:tabular-nums}.field-change .lbl{font-size:12px;color:var(--muted);min-width:90px}.field-change .before{text-decoration:line-through;color:var(--muted)}.field-change .after{font-weight:700;color:var(--ink)}.audit-signal p{margin:5px 0;font-size:12px;color:var(--ink-2)}.audit-signal p.next{color:var(--ink);font-size:13px;background:var(--soft);padding:7px 10px;border-radius:8px;margin-top:8px}
.commitment-card{margin:0 0 6px}.commitment-total{font-size:13px;color:var(--muted)}.commitment-total b{font-size:22px;color:var(--ink);margin-right:2px}.seg-bar{display:flex;gap:2px;height:16px;border-radius:5px;overflow:hidden;background:var(--soft-2);margin-top:6px}.seg-bar i{display:block;height:100%}.seg-legend{display:flex;gap:8px 20px;flex-wrap:wrap;margin-top:12px;font-size:13px;color:var(--ink-2)}.seg-legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}.seg-legend b{color:var(--ink);margin-left:2px}.baseline-missing{margin-bottom:20px;border-color:var(--warning-line);background:var(--warning-bg);color:var(--warning-ink)}.baseline-missing b{display:block;margin-bottom:2px}.checkpoint-list{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:12px}.checkpoint-list li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 16px;padding:10px 16px;border-bottom:1px solid var(--line-2);font-size:13px}.checkpoint-list li:last-child{border-bottom:0}.checkpoint-list span{font-variant-numeric:tabular-nums;color:var(--ink-2)}.checkpoint-list small{grid-column:1/-1;font-size:12px;color:var(--muted)}.report-banner{margin:10px 0 0;display:inline-block;background:var(--soft);border-radius:8px;padding:5px 10px;color:var(--ink-2)}
.standalone header{position:static;padding-bottom:18px}.standalone .page>.schedule-audit:first-child{margin-top:0;border:0;padding:0}.change-grid{display:grid;gap:2px;margin:8px 0}.field-change{margin:4px 0!important}.field-change.deviation .after{color:var(--ink)}.field-change.reduced .after{background:var(--critical-bg);color:var(--critical-ink);border-radius:6px;padding:0 8px}.standalone main{max-width:1400px}
@media(max-width:1200px){.overview-grid{grid-template-columns:1fr 1fr}.overview-grid .attention{grid-column:1/-1}.hero{grid-template-columns:1fr}.delay-card{grid-template-columns:1fr 1fr}.delay-card>div:nth-child(3){border-left:0}.delay-card>div:nth-child(n+3){border-top:1px solid var(--line-2)}}
@media(max-width:850px){main{width:auto;padding:0 10px;margin:15px auto}.mgmt-grid{grid-template-columns:1fr}.kpi-row{grid-template-columns:1fr 1fr}header{position:static}.page{padding:22px 17px}.charts,.split,.risk-grid,.doc-grid,.overview-grid{grid-template-columns:1fr}.kpi-grid{grid-template-columns:1fr 1fr}.donut-wrap{gap:15px}h1{font-size:22px}h2{font-size:20px}.top{flex-direction:column;align-items:flex-start;gap:8px}.header-meta{text-align:left}.meeting-title{display:block}.trace-detail-grid{grid-template-columns:1fr 1fr}.bottom-actions{align-items:flex-start}.date-pair strong{font-size:24px}.delay-card{grid-template-columns:1fr}.delay-card>div{border-left:0!important;border-top:1px solid var(--line-2)}.delay-card>div:first-child{border-top:0}.kpi-value{font-size:26px}nav button{padding:8px 10px}}
@media print{@page{size:A4 landscape;margin:10mm}body{background:var(--card);font-size:11px}header{position:static}header nav,.bottom-actions,.trace-view-controls{display:none}main{padding:0;margin:0;width:auto;max-width:none}.page,.page.active{display:block;border:0;border-radius:0;min-height:0;padding:9px 0;break-after:page}.page:last-of-type{break-after:auto}.trace-scroll{overflow:visible}.trace-inner{min-width:0}.trace-task>summary,.trace-parent>summary{min-height:70px}.chart-card{padding:15px}.overview-grid{grid-template-columns:1.4fr 1fr 1fr}.hero{grid-template-columns:1fr 2fr}.kpi,.delay-card,.risk-card,.meeting-card,.doc-card,.person-card,.audit-signal{break-inside:avoid}h1{font-size:22px}h2{font-size:19px}.trace-panel,.delay-badge,.chart-card,.p0-box,.date-card,.kpi,.cap-bar,.fc-cell,.meter,.mini-meter{-webkit-print-color-adjust:exact;print-color-adjust:exact}.page-foot{margin-top:15px}.roster-details:not([open]){display:none}.method-note{display:none}}
'''
