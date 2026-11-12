"""One pure projection for WBS, acceptance, delay and today-to-Sunday reporting.

The projection never updates business facts. Passing a saved snapshot and its
capture time reproduces that snapshot's report instead of applying today's date.
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import parse_datetime

DELAY_LABELS={"overdue":"已延期·未完成","late_done":"已延期交付","risk":"有延期风险","on_track":"未见延期信号","unknown":"日期待确认","cancelled":"已取消"}


def tracking_data(data, as_of=None):
    now=as_of or datetime.now(timezone.utc)
    if isinstance(now,str): now=parse_datetime(now)
    if not isinstance(now,datetime) or now.tzinfo is None: raise ValueError("跟踪时点必须包含时区")
    projects=data.get("projects",[])
    if len(projects)>1: raise ValueError("进度跟踪只接受一个完整项目")
    project=projects[0] if projects else {}
    cal=next((c for c in data.get("calendars",[]) if c["id"]==project.get("calendar_id")),{})
    zone=cal.get("timezone") or "UTC"
    today=now.astimezone(ZoneInfo(zone)).date()
    end=today+timedelta(days=6-today.weekday())
    monday=today-timedelta(days=today.weekday())
    works=[w for w in data.get("wbs",[]) if w.get("scope")=="project" and (not project or w.get("project_id")==project["id"])]
    idx={w["id"]:w for w in works}
    people={p["id"]:p for p in data.get("people",[])}
    children={wid:[] for wid in idx}
    for w in works:
        if w.get("parent_id") in children: children[w["parent_id"]].append(w["id"])
    descendants={};visiting=set()
    def below(wid):
        if wid in descendants: return descendants[wid]
        if wid in visiting: raise ValueError("父子任务出现循环，无法计算进度")
        visiting.add(wid);found=[]
        for cid in children[wid]: found.append(cid);found.extend(below(cid))
        visiting.remove(wid);descendants[wid]=found
        return found
    for wid in idx: below(wid)
    leaf_ids=[wid for wid in idx if not children[wid] and idx[wid].get("node_kind")=="task"]
    all_deliveries=data.get("deliverables",[])
    registers=data.get("registers",[])
    daily=data.get("day_records",[])
    def duration(a,b):
        return max(0,(date.fromisoformat(b)-date.fromisoformat(a)).days+1) if a and b else None
    def name(pid): return people.get(pid,{}).get("name",pid)
    tasks={}
    for wid,w in idx.items():
        child_ids=children[wid]
        sub_ids=descendants[wid]
        leaves=[x for x in sub_ids if not children[x] and idx[x]["status"]!="cancelled"]
        done=sum(idx[x]["status"]=="done" for x in leaves)
        child_done=sum(idx[x]["status"]=="done" for x in child_ids)
        tree={"child_count":len(child_ids),"child_done_count":child_done,"tree_progress_pct":round(done/len(leaves)*100,2) if leaves else None}
        if w.get("node_kind")!="task":
            tasks[wid]=tree
            continue
        members=list(dict.fromkeys(([w["owner_id"]] if w.get("owner_id") else [])+(w.get("member_ids") or [])))
        linked_ids={wid,*sub_ids}
        deliveries=[d for d in all_deliveries if d.get("work_item_id") in linked_ids]
        related=[r for r in registers if r.get("kind") in ("risk","issue") and (r.get("work_item_id") in linked_ids or r["id"]==w.get("register_id"))]
        opened=[r for r in related if r.get("status") not in ("closed","rejected")]
        results={d["acceptance_status"] for d in deliveries}
        acceptance=("未关联交付物" if not deliveries else "不合格" if "rejected" in results else "合格" if results=={"accepted"} else "未提交" if results=={"draft"} else "待验收")
        start=w.get("forecast_start") or w.get("baseline_start")
        finish=w.get("forecast_finish") or w.get("baseline_finish")
        commitment=w.get("baseline_finish") or w.get("forecast_finish")
        source="原计划完成日" if w.get("baseline_finish") else "当前预计日（尚未设原计划）" if commitment else "未提供完成日"
        status=w["status"]
        delay_days=0
        if status=="cancelled": delay="cancelled"
        elif not commitment: delay="unknown"
        elif status=="done":
            if not w.get("actual_finish"): delay="unknown"
            elif w["actual_finish"]>commitment:
                delay="late_done";delay_days=(date.fromisoformat(w["actual_finish"])-date.fromisoformat(commitment)).days
            else: delay="on_track"
        elif commitment<today.isoformat():
            delay="overdue";delay_days=(today-date.fromisoformat(commitment)).days
        elif w.get("delay_risk")=="at_risk" or (finish and finish>commitment) or status=="blocked": delay="risk"
        else: delay="on_track"
        reasons=[]
        if status!="cancelled":
            if delay=="overdue": reasons.append("未结逾期结转")
            if commitment and today.isoformat()<=commitment<=end.isoformat(): reasons.append("承诺在本周日之前")
            if finish and today.isoformat()<=finish<=end.isoformat() and finish!=commitment: reasons.append("预计在本周日之前")
            if status in ("in_progress","blocked","in_review") and (w.get("actual_start") or start or today.isoformat())<=end.isoformat(): reasons.append("当前正在推进")
            if status=="done" and w.get("actual_finish") and today.isoformat()<=w["actual_finish"]<=end.isoformat(): reasons.append("本窗口已确认交付")
        # A completed task from earlier this week is deliberately outside the
        # today-to-Sunday view; the full project and weekly review still retain it.
        if status=="done" and (not w.get("actual_finish") or w["actual_finish"]<today.isoformat()): reasons=[]
        attendance_start=w.get("actual_start") or start or monday.isoformat()
        attendance_end=min(w.get("actual_finish") or finish or today.isoformat(),today.isoformat())
        attendance_basis="任务日期窗口" if w.get("actual_start") or start else "本周参考窗口（任务日期未定）"
        attendance=[]
        if attendance_start<=attendance_end:
            for pid in members:
                records=[r for r in daily if r["person_id"]==pid and r["record_kind"]=="actual" and attendance_start<=r["date"]<=attendance_end]
                if not records: continue
                counts=Counter(r["status"] for r in records)
                on=sum(counts[k] for k in ("onsite","remote","travel"))
                attendance.append(f"{name(pid)}：在岗{on}半日 / 请假{counts['leave']}半日 / 已录{len(records)}半日")
        attendance_text=(f"{attendance_basis} {attendance_start}—{attendance_end}\n"+"；".join(attendance)+"\n出勤不等于该任务投入人天") if attendance else attendance_basis+"尚无成员实际出勤记录；不推算任务人天"
        tasks[wid]={
            **tree,
            "planned_duration_days":duration(start,finish),"actual_duration_days":duration(w.get("actual_start"),w.get("actual_finish")),
            "stage_acceptance_result":acceptance,
            "related_deliverables":"；".join(f"{d['id']} · {d['title']}" for d in deliveries),
            "related_risks":"；".join(f"{r['id']} · {r['title']}" for r in related),"risk_count":len(opened),
            "attendance_summary":attendance_text,"todo_list":"；".join(f"{cid} · {idx[cid]['title']}" for cid in child_ids),
            "todo_count":len(leaves),"todo_done_count":done,"todo_progress_pct":round(done/len(leaves)*100,2) if leaves else None,
            "todo_completed_aux":"已完成" if leaves and done==len(leaves) else "","task_count_aux":0 if child_ids else 1,
            "is_delayed":True if delay in ("overdue","late_done") else None if delay=="unknown" else False,
            "delay_state":delay,"delay_state_label":DELAY_LABELS[delay],"delay_days":delay_days,
            "commitment_finish":commitment,"commitment_source":source,"in_week_window":bool(reasons),"window_reason":"；".join(dict.fromkeys(reasons)),
            "is_summary":bool(child_ids),"child_ids":child_ids,"leaf_descendant_ids":leaves,
            "member_ids":members,"deliverable_ids":[d["id"] for d in deliveries],"risk_ids":[r["id"] for r in related],
        }
    # Include ancestor context for focused leaves without counting parents twice.
    focus_set={wid for wid in leaf_ids if tasks[wid]["in_week_window"]}
    for wid in list(focus_set):
        parent=idx[wid].get("parent_id")
        while parent in idx and parent not in focus_set:
            focus_set.add(parent);parent=idx[parent].get("parent_id")
    for wid in idx:
        if children[wid] and any(x in focus_set for x in descendants[wid]):
            focus_set.add(wid);tasks[wid]["in_week_window"]=True
            tasks[wid]["window_reason"]="本周子任务的父级上下文"
    focus_ids=[wid for wid in idx if wid in focus_set]
    active_leaves=[wid for wid in leaf_ids if idx[wid]["status"]!="cancelled"]
    counts={"leaf_total":len(active_leaves),"focus_total":sum(wid in focus_set for wid in active_leaves),
            "overdue":sum(tasks[x]["delay_state"]=="overdue" for x in active_leaves),
            "risk":sum(tasks[x]["delay_state"]=="risk" for x in active_leaves),
            "late_done":sum(tasks[x]["delay_state"]=="late_done" for x in active_leaves),
            "done":sum(idx[x]["status"]=="done" for x in active_leaves),
            "P0":sum(idx[x].get("priority")=="P0" and idx[x]["status"]!="done" for x in active_leaves)}
    focus_counts={"total":counts["focus_total"],**{key:sum(tasks[x]["delay_state"]==key for x in active_leaves if x in focus_set) for key in ("overdue","risk","late_done","on_track","unknown")},
                  "done":sum(idx[x]["status"]=="done" for x in active_leaves if x in focus_set)}
    pm=people.get(project.get("pm_id"),{})
    delivery_fields={d["id"]:{"pm_feishu_url":pm.get("feishu_url") or ""} for d in all_deliveries if not project or d.get("project_id")==project["id"]}
    return {"as_of":now.astimezone(timezone.utc).isoformat(),"today":today.isoformat(),"week_end":end.isoformat(),"timezone":zone,
            "tasks":tasks,"deliverables":delivery_fields,"leaf_ids":leaf_ids,"focus_ids":focus_ids,"counts":counts,"focus_counts":focus_counts}
