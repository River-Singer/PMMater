"""Transactional SQLite repository with typed and relational business validation.

All supported writes go through apply_batch. Direct SQL is an administrator escape
hatch, not a second application write interface. Actor is an audit label, not auth.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit
import json
import math
import re
from contextlib import closing
import sqlite3
import uuid

from .schema import TABLES, ORDER, UNIQUE, ENUMS, STATE_TRANSITIONS, suggested_priority
from .calendar import workday_distance

class ValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("；".join(f"{x.get('table','')}/{x.get('id','')} {x.get('field','')}: {x['message']}" for x in errors[:12]))
    def to_dict(self): return {"ok":False,"errors":self.errors}

def utc_now(): return datetime.now(timezone.utc)

def parse_datetime(value):
    if not isinstance(value,str): raise ValueError("需要ISO日期时间文本")
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.tzinfo is None: raise ValueError("日期时间必须包含时区，如2026-11-05T16:00:00+08:00")
    return parsed

def parse_date(value):
    if not isinstance(value,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        raise ValueError("日期必须是YYYY-MM-DD")
    return date.fromisoformat(value)

def error(table, record, field, code, message):
    return {"table":table,"id":record.get("id",""),"field":field,"code":code,"message":message}

def _column_sql(f):
    sqltype="INTEGER" if f.dtype in ("integer","boolean") else "REAL" if f.dtype=="decimal" else "TEXT"
    col=f'"{f.name}" {sqltype}'
    if f.name=="id": col+=" PRIMARY KEY NOT NULL"
    elif f.name=="version": col+=" NOT NULL DEFAULT 1 CHECK(version > 0)"
    else:
        if f.required: col+=" NOT NULL"
        if f.default is not None:
            value=("1" if f.default else "0") if isinstance(f.default,bool) else str(f.default) if isinstance(f.default,(int,float)) else "'"+str(f.default).replace("'","''")+"'"
            col+=" DEFAULT "+value
    if f.dtype=="boolean": col+=f' CHECK("{f.name}" IN (0,1))'
    if f.enum:
        values=",".join("'"+k.replace("'","''")+"'" for k in ENUMS[f.enum])
        col+=f' CHECK("{f.name}" IN ({values}))'
    if f.dtype=="refs": col+=f' CHECK("{f.name}" IS NULL OR (json_valid("{f.name}") AND json_type("{f.name}")=\'array\'))'
    if f.ref and f.dtype!="refs": col+=f' REFERENCES "{f.ref}"(id) DEFERRABLE INITIALLY DEFERRED'
    return col

def _table_sql(table, name=None):
    cols=[_column_sql(f) for f in TABLES[table]["fields"]]
    for key in UNIQUE.get(table,[]): cols.append("UNIQUE("+",".join('"'+k+'"' for k in key)+")")
    return f'CREATE TABLE IF NOT EXISTS "{name or table}" ({",".join(cols)})'

def _normalise(table, incoming, old=None):
    meta=TABLES[table]
    fields={f.name:f for f in meta["fields"]}
    errs=[]
    unknown=set(incoming)-set(fields)
    for k in sorted(unknown): errs.append(error(table,incoming,k,"unknown_field","未知字段，未写入"))
    out={f.name:(old.get(f.name) if old else f.default) for f in fields.values()}
    out.update({k:v for k,v in incoming.items() if k in fields})
    if table=="wbs" and out.get("node_kind")!="task": out["scope"]="project"
    for name,f in fields.items():
        value=out.get(name)
        if isinstance(value,str): value=value.strip() or None
        if name in {"id","version"}: out[name]=value; continue
        if value is None:
            out[name]=None
            if f.required: errs.append(error(table,incoming,name,"required","必填字段为空"))
            continue
        try:
            if f.dtype=="boolean":
                if not isinstance(value,bool): raise ValueError("必须为true/false，不能将文本或数字隐式当布尔值")
            elif f.dtype=="integer":
                if isinstance(value,bool) or not isinstance(value,int): raise ValueError("必须为整数")
            elif f.dtype=="decimal":
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0: raise ValueError("必须为有限非负数，允许小数")
            elif f.dtype=="refs":
                if not isinstance(value,list) or any(not isinstance(x,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}",x) for x in value):
                    raise ValueError("多选关联必须是稳定编号数组")
                if len(value)!=len(set(value)): raise ValueError("同一多选关联不能重复成员")
                value=list(value) if value else None
            elif f.dtype=="date": parse_date(value)
            elif f.dtype=="datetime": value=parse_datetime(value).isoformat()
            else:
                if not isinstance(value,str): raise ValueError("必须为文本")
                if len(value)>f.max_length: raise ValueError(f"超过{f.max_length}字符")
            if f.enum and value not in ENUMS[f.enum]: raise ValueError("不在允许的下拉选项内")
            if f.ref and f.dtype!="refs" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}",value): raise ValueError("引用必须是稳定编号，不是姓名或逗号姓名列表")
        except (ValueError,TypeError) as exc:
            errs.append(error(table,incoming,name,"invalid_type",str(exc)))
        out[name]=value
    if out.get("id") and (not isinstance(out["id"],str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}",out["id"])):
        errs.append(error(table,incoming,"id","invalid_id","编号只允许字母数字、下划线、点、冒号和连字符"))
    if errs: raise ValidationError(errs)
    return out

def validate_state(data, *, now=None, previous=None):
    now=now or utc_now()
    if isinstance(now,str): now=parse_datetime(now)
    if now.tzinfo is None: raise ValueError("now必须带时区")
    previous=previous or {}
    idx={t:{r["id"]:r for r in data.get(t,[])} for t in ORDER}
    errs=[]
    def add(t,r,f,c,m): errs.append(error(t,r,f,c,m))
    def req(t,r,*fields):
        for f in fields:
            if r.get(f) in (None,""): add(t,r,f,"conditional_required","当前状态/类型要求填写")
    def ordered(t,r,a,b):
        if r.get(a) and r.get(b) and r[a]>r[b]: add(t,r,b,"date_order",f"不能早于{a}")
    for t,rows in data.items():
        if t not in TABLES: continue
        for r in rows:
            for f in TABLES[t]["fields"]:
                if not f.ref or not r.get(f.name): continue
                values=r[f.name] if f.dtype=="refs" else [r[f.name]]
                if not isinstance(values,list):
                    add(t,r,f.name,"invalid_type","多选关联必须是数组");continue
                for value in values:
                    if not isinstance(value,str) or value not in idx[f.ref]: add(t,r,f.name,"foreign_key",f"不存在的{f.ref}编号：{value}")
        for cols in UNIQUE.get(t,[]):
            seen={}
            for r in rows:
                key=tuple(r.get(k) for k in cols)
                if key in seen: add(t,r," + ".join(cols),"duplicate",f"联合键重复，与{seen[key]}重复")
                seen[key]=r["id"]
    # Stop before dependent validations dereference missing parents.
    if errs: raise ValidationError(errs)
    for r in data.get("people",[]):
        ordered("people",r,"starts_on","ends_on")
        if r.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",r["email"]):
            add("people",r,"email","email","邮箱格式不正确")
        if r.get("feishu_url"):
            link=urlsplit(r["feishu_url"])
            if link.scheme not in ("https","http","lark","feishu") or (link.scheme in ("http","https") and not link.netloc):
                add("people",r,"feishu_url","url","使用有效的飞书个人资料网址或客户端链接")
    for r in data.get("calendars",[]):
        ordered("calendars",r,"coverage_start","coverage_end")
        try: ZoneInfo(r["timezone"])
        except (ZoneInfoNotFoundError,ValueError): add("calendars",r,"timezone","timezone","未知IANA时区；Windows请安装tzdata")
        if bool(r.get("source_url"))!=bool(r.get("verified_at")): add("calendars",r,"source_url","provenance","来源网址与核验时间成对填写")
    if errs: raise ValidationError(errs)
    day_index={}
    for r in data.get("calendar_days",[]):
        cal=idx["calendars"][r["calendar_id"]]
        day_index[(r["calendar_id"],r["date"])]=r
        if not cal["coverage_start"]<=r["date"]<=cal["coverage_end"]: add("calendar_days",r,"date","calendar_range","日期不在日历覆盖范围内")
        if parse_date(r["date"]).isoweekday()!=r["weekday"]: add("calendar_days",r,"weekday","weekday","星期必须与真实日期一致")
        if bool(r.get("source_url"))!=bool(r.get("verified_at")): add("calendar_days",r,"source_url","provenance","来源网址与核验时间必须成对")
        if r["verification_status"]=="verified": req("calendar_days",r,"source_url","verified_at")
        if r["day_type"]=="holiday" and r["is_workday"]: add("calendar_days",r,"is_workday","day_conflict","假日不能同时是工作日；公司例外另设类型")
        if r["day_type"]=="makeup_workday" and not r["is_workday"]: add("calendar_days",r,"is_workday","day_conflict","调休工作日必须为工作日")
    for r in data.get("projects",[]):
        ordered("projects",r,"baseline_start","baseline_finish")
        ordered("projects",r,"baseline_start","forecast_finish")
    # WBS 单表树：项目→交付物→功能模块→任务共用一张表，父级自链挂树；仅任务行参与执行统计。
    wbs_rows=data.get("wbs",[])
    for r in wbs_rows:
        kind=r["node_kind"]; parent=idx["wbs"].get(r.get("parent_id"))
        if kind=="project":
            if r.get("parent_id"): add("wbs",r,"parent_id","hierarchy","层级类型为项目的是根节点，不能挂父级")
            if not r.get("project_id"): add("wbs",r,"project_id","conditional_required","项目节点必须挂所属项目")
        elif kind in ("deliverable","module"):
            if r["scope"]!="project": add("wbs",r,"scope","scope_mismatch","交付物/功能模块属于项目层级，工作归属保持项目内")
            if not parent: add("wbs",r,"parent_id","hierarchy","交付物/功能模块必须挂父级")
            elif kind=="deliverable" and parent["node_kind"]!="project": add("wbs",r,"parent_id","hierarchy","交付物的父级必须是项目节点")
            elif kind=="module" and parent["node_kind"] not in ("project","deliverable","module"): add("wbs",r,"parent_id","hierarchy","功能模块的父级必须是项目、交付物或功能模块")
        if parent and parent.get("project_id")!=r.get("project_id"): add("wbs",r,"parent_id","project_mismatch","父节点必须属于同一项目")
    def cycles(table,parent_edges):
        color={}
        def visit(node):
            if color.get(node)==1: return True
            if color.get(node)==2: return False
            color[node]=1
            for child in parent_edges.get(node,[]):
                if visit(child): return True
            color[node]=2
            return False
        for node in parent_edges:
            if visit(node):
                add(table,{"id":node},"relationship","cycle","关系中存在循环")
                break
    tree={r["id"]:([r["parent_id"]] if r.get("parent_id") else []) for r in wbs_rows}
    cycles("wbs",tree)
    children={}
    for work in wbs_rows:
        if work.get("parent_id"): children.setdefault(work["parent_id"],[]).append(work)
    def cal_for_work(r):
        if r.get("project_id"): return idx["projects"][r["project_id"]]["calendar_id"]
        if r.get("service_id"): return idx["services"][r["service_id"]]["calendar_id"]
        return None
    def local_day(calid):
        tz=idx["calendars"][calid]["timezone"] if calid else "UTC"
        return now.astimezone(ZoneInfo(tz)).date().isoformat()
    for r in wbs_rows:
        t="wbs"; kind=r["node_kind"]; scope=r["scope"]; state=r["status"]
        if scope=="project":
            req(t,r,"project_id")
            if kind=="task" and state not in ("intake","cancelled") and r["work_type"]=="feature" and not r.get("parent_id"):
                add(t,r,"parent_id","conditional_required","开发功能进入待开始及以后必须挂到交付物或功能模块下")
        else:
            for f in ("project_id","parent_id"):
                if r.get(f): add(t,r,f,"scope_mismatch","非项目工作不挂项目或WBS层级；若纳入项目请调整归属并记录原因")
            if scope=="operations": req(t,r,"service_id")
        if r.get("parent_id"):
            parent=idx["wbs"][r["parent_id"]]
            if kind=="task" and (parent.get("project_id")!=r.get("project_id") or parent.get("scope")!=r.get("scope")):
                add(t,r,"parent_id","project_mismatch","父子任务必须属于同一个项目和工作归属")
        if state=="done" and any(c["status"] not in ("done","cancelled") for c in children.get(r["id"],[])):
            add(t,r,"status","unfinished_children","还有未完成的子节点，不能确认为完成")
        if kind=="task":
            if state not in ("intake","cancelled"):
                req(t,r,"owner_id","acceptor_id","priority","forecast_finish","acceptance_criteria")
            if state in ("in_progress","in_review","done"): req(t,r,"actual_start")
            if state=="blocked": req(t,r,"blocker_reason","coordinator_id","next_action","next_due_at")
            if state=="done": req(t,r,"actual_finish","completion_evidence")
            elif r.get("actual_finish"): add(t,r,"actual_finish","incomplete_actual_finish","未确认完成不能保留实际完成日期；重新打开时应清空，历史由事件保留")
        elif state not in ("intake","cancelled"): req(t,r,"owner_id")
        if state=="cancelled": req(t,r,"cancel_reason")
        if r.get("severity") and r["work_type"] not in ("bug","incident"): add(t,r,"severity","severity_type","严重度仅用于缺陷/故障")
        suggestion=suggested_priority(r.get("impact"),r.get("urgency"))
        if r.get("priority")=="P0" or (suggestion and r.get("priority") and suggestion!=r["priority"]): req(t,r,"priority_reason")
        for a,b in (("baseline_start","baseline_finish"),("baseline_start","forecast_finish"),("forecast_start","forecast_finish"),("actual_start","actual_finish")):
            ordered(t,r,a,b)
        today=local_day(cal_for_work(r))
        for f in ("actual_start","actual_finish","remediation_actual"):
            if r.get(f) and r[f]>today: add(t,r,f,"future_actual","实际日期不能晚于可信当前日期")
        if r.get("delay_risk")=="at_risk": req(t,r,"delay_risk_reason")
        if r.get("remediation_expected"): req(t,r,"remediation_action")
        if r.get("remediation_actual"): req(t,r,"remediation_action","remediation_expected")
        if r.get("is_acceptance_node") and state=="done":
            related={r["id"]};pending=[r["id"]]
            while pending:
                for child in children.get(pending.pop(),[]):
                    if child["id"] not in related: related.add(child["id"]);pending.append(child["id"])
            deliveries=[d for d in data.get("deliverables",[]) if d["work_item_id"] in related]
            if not deliveries or any(d["acceptance_status"]!="accepted" for d in deliveries):
                add(t,r,"status","acceptance_gate","验收节点完成需要关联交付物全部验收合格")
        if r.get("owner_id") and state not in ("done","cancelled","intake") and not idx["people"][r["owner_id"]]["active"]: add(t,r,"owner_id","inactive_owner","执行中的负责人已停用，请明确交接")
        if r.get("meeting_id"):
            meeting=idx["meetings"][r["meeting_id"]]
            if meeting.get("project_id")!=r.get("project_id"): add(t,r,"meeting_id","project_mismatch","会议与工作项项目不一致")
        if r.get("register_id"):
            source=idx["registers"][r["register_id"]]
            source_project=source.get("project_id") or idx["wbs"].get(source.get("work_item_id"),{}).get("project_id")
            if source_project!=r.get("project_id"): add(t,r,"register_id","project_mismatch","来源问题/决定与工作项项目不一致")
    for t in ("projects","wbs"):
        for r in data.get(t,[]):
            old=previous.get(t,{}).get(r["id"])
            if not old: continue
            if t=="wbs" and r.get("node_kind")=="task" and r["status"]!=old["status"]:
                if r["status"] not in STATE_TRANSITIONS[old["status"]]: add(t,r,"status","state_transition",f"不允许{old['status']}直接转为{r['status']}")
                if old["status"] in ("done","cancelled"): req(t,r,"reopen_reason")
            changed=any(old.get(f) is not None and r.get(f)!=old.get(f) for f in ("baseline_start","baseline_finish"))
            if changed:
                change=idx["registers"].get(r.get("baseline_change_id"))
                projectid=r["id"] if t=="projects" else r.get("project_id")
                change_project=(change.get("project_id") or idx["wbs"].get(change.get("work_item_id"),{}).get("project_id")) if change else None
                if not change or change["kind"]!="change" or change["status"]!="approved" or change_project!=projectid:
                    add(t,r,"baseline_change_id","baseline_control","修改已存在基线需关联同项目已批准的变更记录")
                elif t=="wbs" and not projectid and change.get("work_item_id")!=r["id"]:
                    add(t,r,"baseline_change_id","baseline_control","非项目工作改基线必须引用针对本工作项批准的变更")
    for r in data.get("participants",[]):
        if bool(r.get("project_id"))==bool(r.get("work_item_id")): add("participants",r,"project_id","target_count","项目或工作项必须且只能选一项")
        ordered("participants",r,"starts_on","ends_on")
    for r in data.get("registers",[]):
        t="registers"
        if not r.get("project_id") and not r.get("work_item_id"): add(t,r,"project_id","register_context","至少关联项目或工作项")
        if r.get("project_id") and r.get("work_item_id") and idx["wbs"][r["work_item_id"]].get("project_id")!=r["project_id"]: add(t,r,"work_item_id","project_mismatch","事项关联工作项不属于该项目")
        if r["status"] in ("approved","rejected"):
            if r["kind"] not in ("change","decision"): add(t,r,"status","register_state","问题/风险使用开放、处理中或关闭，不使用批准状态")
            req(t,r,"decision_by_id","decision","decided_at")
        if r["status"]=="closed": req(t,r,"closure_evidence","closed_at")
        for f in ("decided_at","closed_at"):
            if r.get(f) and parse_datetime(r[f])>now: add(t,r,f,"future_actual","尚未发生的决定/关闭不能写成事实")
    dep_graph={r["id"]:[] for r in data.get("wbs",[])}
    for r in data.get("dependencies",[]):
        pre=idx["wbs"][r["predecessor_id"]]; suc=idx["wbs"][r["successor_id"]]
        if pre["node_kind"]!="task" or suc["node_kind"]!="task": add("dependencies",r,"predecessor_id","not_task","依赖只关联任务行；交付物/功能模块的先后由父子层级表达")
        dep_graph[pre["id"]].append(suc["id"])
        if r["lag_workdays"]<0: add("dependencies",r,"lag_workdays","negative_lag","当前版本只支持非负间隔")
        old_dep=previous.get("dependencies",{}).get(r["id"])
        old_suc=previous.get("wbs",{}).get(suc["id"])
        new_execution=(not old_dep or any(old_dep.get(k)!=r.get(k) for k in ("predecessor_id","successor_id","dependency_type","lag_workdays")) or not old_suc or old_suc.get("actual_start")!=suc.get("actual_start"))
        # Reopening a predecessor must not erase a successor's already-observed start.
        # New starts/changed relations are constrained; historical impact is reported.
        if new_execution and suc.get("actual_start") and suc["status"] not in ("cancelled","intake"):
            if pre["status"]!="done": add("dependencies",r,"predecessor_id","unfinished_predecessor","FS前置未确认完成，后续不能记录已开始")
            elif suc["actual_start"]<pre["actual_finish"]: add("dependencies",r,"successor_id","dependency_date","后续实际开始早于前置实际完成")
            elif r["lag_workdays"]:
                calid=cal_for_work(suc)
                days=[x for x in data.get("calendar_days",[]) if x["calendar_id"]==calid]
                try:
                    if workday_distance(pre["actual_finish"],suc["actual_start"],days)<r["lag_workdays"]: add("dependencies",r,"lag_workdays","dependency_lag","实际开始未满足约定工作日间隔")
                except ValueError as exc: add("dependencies",r,"lag_workdays","calendar_missing",str(exc))
    cycles("dependencies",dep_graph)
    for r in data.get("meetings",[]):
        if r["status"]=="held":
            req("meetings",r,"minutes")
            if parse_datetime(r["scheduled_at"])>now: add("meetings",r,"status","future_meeting","未来会议不能标记已召开")
        if r["status"]=="planned" and r.get("minutes"): add("meetings",r,"minutes","unconfirmed_minutes","会前内容请放议题，不能填确认纪要")
    for table, url_fields in (("interfaces",("spec_url",)),("project_documents",("url",))):
        for r in data.get(table,[]):
            for f in url_fields:
                if r.get(f):
                    link=urlsplit(r[f])
                    if link.scheme not in ("http","https") or not link.netloc or link.username or link.password:
                        add(table,r,f,"url","填写可访问的HTTP/HTTPS文档链接，不在链接中保存账号密码")
            if r.get("work_item_id") and idx["wbs"][r["work_item_id"]].get("project_id")!=r["project_id"]:
                add(table,r,"work_item_id","project_mismatch","关联任务必须属于当前项目")
            if table=="interfaces":
                if r["protocol"]=="http":
                    req(table,r,"method")
                    address=r["address"]
                    link=urlsplit(address)
                    if not (address.startswith("/") and not address.startswith("//")) and not (link.scheme in ("http","https") and link.netloc and not link.username and not link.password):
                        add(table,r,"address","url","HTTP接口使用完整HTTP/HTTPS地址或以单个/开头的接口路径")
            else:
                if r.get("meeting_id") and idx["meetings"][r["meeting_id"]].get("project_id")!=r["project_id"]:
                    add(table,r,"meeting_id","project_mismatch","关联会议必须属于当前项目")
                if r.get("updated_on") and r["updated_on"]>local_day(idx["projects"][r["project_id"]]["calendar_id"]):
                    add(table,r,"updated_on","future_actual","最后维护日期不能在未来")
    for r in data.get("deliverables",[]):
        t="deliverables";work=idx["wbs"][r["work_item_id"]]
        if work.get("project_id")!=r["project_id"]: add(t,r,"work_item_id","project_mismatch","交付物与任务必须属于同一项目")
        if r.get("document_id") and idx["project_documents"][r["document_id"]]["project_id"]!=r["project_id"]:
            add(t,r,"document_id","project_mismatch","引用文档必须属于同一项目")
        if r.get("document_url"):
            link=urlsplit(r["document_url"])
            if link.scheme not in ("http","https") or not link.netloc or link.username or link.password:
                add(t,r,"document_url","url","交付物填写HTTP/HTTPS链接，不包含账号密码")
        if r["acceptance_status"]!="draft": req(t,r,"document_url","submitted_by_id","submitted_at","acceptor_id")
        if r["acceptance_status"] in ("accepted","rejected"): req(t,r,"reviewed_at","acceptance_comments")
        elif r.get("reviewed_at"): add(t,r,"reviewed_at","unconfirmed_acceptance","尚未形成验收结果不能填写实际验收时间")
        for f in ("submitted_at","reviewed_at"):
            if r.get(f) and parse_datetime(r[f])>now: add(t,r,f,"future_actual","实际提交/验收时间不能在未来")
        if r.get("submitted_at") and r.get("reviewed_at") and parse_datetime(r["reviewed_at"])<parse_datetime(r["submitted_at"]):
            add(t,r,"reviewed_at","date_order","验收时间不能早于提交时间")
    allowed={"planned":{"available","other_work","travel","leave","off","unavailable","unknown"},"actual":{"onsite","remote","travel","leave","off","unknown"}}
    for r in data.get("day_records",[]):
        t="day_records"
        if (r["calendar_id"],r["date"]) not in day_index: add(t,r,"date","calendar_missing","日期表缺少此日期，请先生成/导入日历")
        if r["status"] not in allowed[r["record_kind"]]: add(t,r,"status","attendance_kind","状态不适用于计划/实际类别")
        local=now.astimezone(ZoneInfo(idx["calendars"][r["calendar_id"]]["timezone"]))
        if r["record_kind"]=="actual" and (r["date"]>local.date().isoformat() or (r["date"]==local.date().isoformat() and r["period"]=="PM" and local.hour<12)):
            add(t,r,"date","future_attendance","尚未发生的日期/下午时段不能录入实际出勤，请填计划安排")
    from .foundation_validation import validate_foundation
    validate_foundation(data, idx, previous, now, add, req, ordered, day_index)
    if errs: raise ValidationError(errs)

class Store:
    def __init__(self,path):
        self.path=str(path)
        if self.path!=":memory:": Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(self.path)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
    def close(self): self.conn.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def _create_schema(self):
        version=self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0,3,4,5,6,7): raise ValueError(f"数据库版本{version}不兼容；请先备份并运行显式迁移")
        if version==3: self._upgrade_v3()
        if self.conn.execute("PRAGMA user_version").fetchone()[0]==4: self._upgrade_v4()
        if self.conn.execute("PRAGMA user_version").fetchone()[0]==5: self._upgrade_v5()
        if self.conn.execute("PRAGMA user_version").fetchone()[0]==6: self._upgrade_v6()
        for t,m in TABLES.items():
            self.conn.execute(_table_sql(t))
        self.conn.execute("CREATE TABLE IF NOT EXISTS event_log(event_id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL, recorded_at TEXT NOT NULL, actor TEXT NOT NULL, entity TEXT NOT NULL, record_id TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at TEXT NOT NULL, cutoff TEXT NOT NULL, event_watermark INTEGER NOT NULL, data_json TEXT NOT NULL)")
        self.conn.execute("PRAGMA user_version=7")
        self.conn.commit()
    def _upgrade_v3(self):
        # Add contact/interface/document fields while retaining facts and audit history.
        # The backup is a SQLite snapshot, never a filesystem copy of a live WAL file.
        if self.path!=":memory:":
            backup=Path(self.path).with_name(Path(self.path).name+".before-v4-"+uuid.uuid4().hex[:8]+".bak")
            with closing(sqlite3.connect(backup)) as destination: self.conn.backup(destination)
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.conn.execute("PRAGMA user_version").fetchone()[0]>=4:
                self.conn.rollback();return
            existing={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("people","work_items"):
                if table not in existing: continue
                meta=TABLES[table] if table in TABLES else TABLES.get("wbs")
                if meta is None: continue
                columns={r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}
                for field in meta["fields"]:
                    if field.name not in columns:
                        self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN '+_column_sql(field))
            if "registers" in existing:
                columns={r[1] for r in self.conn.execute('PRAGMA table_info("registers")')}
                self.conn.execute(_table_sql("registers","registers_v4_upgrade"))
                shared=[f.name for f in TABLES["registers"]["fields"] if f.name in columns]
                names=",".join('"'+x+'"' for x in shared)
                self.conn.execute(f'INSERT INTO registers_v4_upgrade ({names}) SELECT {names} FROM registers')
                self.conn.execute('DROP TABLE registers')
                self.conn.execute('ALTER TABLE registers_v4_upgrade RENAME TO registers')
            for table in TABLES: self.conn.execute(_table_sql(table))
            errors=list(self.conn.execute("PRAGMA foreign_key_check"))
            if errors: raise ValueError("升级发现外键异常，已回滚；请检查升级前备份")
            self.conn.execute("PRAGMA user_version=4")
            self.conn.commit()
        except Exception:
            self.conn.rollback();raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
    def _upgrade_v4(self):
        if self.path!=":memory:":
            backup=Path(self.path).with_name(Path(self.path).name+".before-v5-"+uuid.uuid4().hex[:8]+".bak")
            with closing(sqlite3.connect(backup)) as destination: self.conn.backup(destination)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.conn.execute("PRAGMA user_version").fetchone()[0]>=5:
                self.conn.rollback();return
            existing={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in TABLES:
                if table not in existing:
                    self.conn.execute(_table_sql(table));continue
                columns={r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}
                for field in TABLES[table]["fields"]:
                    if field.name not in columns: self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN '+_column_sql(field))
            if list(self.conn.execute("PRAGMA foreign_key_check")): raise ValueError("数据结构升级发现外键异常，已回滚")
            self.conn.execute("PRAGMA user_version=5");self.conn.commit()
        except Exception: self.conn.rollback();raise
    def _upgrade_v5(self):
        # v6 合表：wbs_nodes + work_items 合并为 wbs 单表树；旧编号原样保留，供他表引用。
        if self.path!=":memory:":
            backup=Path(self.path).with_name(Path(self.path).name+".before-v6-"+uuid.uuid4().hex[:8]+".bak")
            with closing(sqlite3.connect(backup)) as destination: self.conn.backup(destination)
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.conn.execute("PRAGMA user_version").fetchone()[0]>=6:
                self.conn.rollback();return
            existing={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.conn.execute(_table_sql("wbs","wbs"))
            if "wbs_nodes" in existing:
                columns={r[1] for r in self.conn.execute('PRAGMA table_info("wbs_nodes")')}
                shared=[f.name for f in TABLES["wbs"]["fields"] if f.name in columns and f.name not in ("node_kind","scope","parent_id")]
                names=",".join('"'+x+'"' for x in shared)
                self.conn.execute(f'INSERT INTO wbs ({names},"node_kind","scope","parent_id") SELECT {names},CASE WHEN "parent_id" IS NULL THEN \'project\' ELSE \'deliverable\' END,\'project\',"parent_id" FROM wbs_nodes')
                while True:
                    promoted=self.conn.execute("UPDATE wbs SET node_kind='module' WHERE node_kind='deliverable' AND parent_id IN (SELECT id FROM wbs WHERE node_kind='deliverable')").rowcount
                    if not promoted: break
            if "work_items" in existing:
                columns={r[1] for r in self.conn.execute('PRAGMA table_info("work_items")')}
                if "parent_work_id" in columns and "wbs_id" in columns: parent_sql='COALESCE("parent_work_id","wbs_id")'
                elif "parent_work_id" in columns: parent_sql='"parent_work_id"'
                elif "wbs_id" in columns: parent_sql='"wbs_id"'
                else: parent_sql="NULL"
                shared=[f.name for f in TABLES["wbs"]["fields"] if f.name in columns and f.name not in ("node_kind","parent_id")]
                names=",".join('"'+x+'"' for x in shared)
                self.conn.execute(f'INSERT INTO wbs ({names},"node_kind","parent_id") SELECT {names},\'task\',{parent_sql} FROM work_items')
            for table in ("participants","dependencies","registers","interfaces","project_documents","deliverables"):
                if table not in existing: continue
                columns={r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}
                self.conn.execute(_table_sql(table,table+"_v6_upgrade"))
                shared=[f.name for f in TABLES[table]["fields"] if f.name in columns]
                names=",".join('"'+x+'"' for x in shared)
                self.conn.execute(f'INSERT INTO {table}_v6_upgrade ({names}) SELECT {names} FROM "{table}"')
                self.conn.execute(f'DROP TABLE "{table}"')
                self.conn.execute(f'ALTER TABLE {table}_v6_upgrade RENAME TO "{table}"')
            if "wbs_nodes" in existing: self.conn.execute('DROP TABLE "wbs_nodes"')
            if "work_items" in existing: self.conn.execute('DROP TABLE "work_items"')
            for table in TABLES: self.conn.execute(_table_sql(table))
            errors=list(self.conn.execute("PRAGMA foreign_key_check"))
            if errors: raise ValueError("合表升级发现外键异常，已回滚；请检查升级前备份")
            self.conn.execute("PRAGMA user_version=6")
            self.conn.commit()
        except Exception:
            self.conn.rollback();raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
    def _upgrade_v6(self):
        if self.path != ":memory:":
            backup = Path(self.path).with_name(Path(self.path).name + ".before-v7-" + uuid.uuid4().hex[:8] + ".bak")
            with closing(sqlite3.connect(backup)) as destination:
                self.conn.backup(destination)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table, meta in TABLES.items():
                self.conn.execute(_table_sql(table))
                columns = {r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}
                for field in meta["fields"]:
                    if field.name not in columns:
                        self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN ' + _column_sql(field))
            projects = list(self.conn.execute('SELECT id FROM projects'))
            project_id = projects[0][0] if len(projects) == 1 else None
            # Compatibility rows preserve references and historical snapshots. They
            # become immutable, and all new active work goes to operations.
            for raw in list(self.conn.execute("SELECT * FROM wbs WHERE scope!='project'")):
                old = dict(raw)
                fields = {f.name for f in TABLES["operations"]["fields"]}
                row = {k:v for k,v in old.items() if k in fields}
                row.update(project_id=project_id, legacy_wbs_id=old["id"])
                names = list(row)
                self.conn.execute('INSERT INTO operations (' + ','.join('"'+n+'"' for n in names) + ') VALUES (' + ','.join('?' for _ in names) + ')', [row[n] for n in names])
                self.conn.execute('UPDATE wbs SET migrated_operation_id=? WHERE id=?', (old['id'],old['id']))
            if list(self.conn.execute("PRAGMA foreign_key_check")):
                raise ValueError("v7升级关联检查失败，已回滚")
            self.conn.execute("PRAGMA user_version=7")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def promote_operation(self, operation_id, *, version, decision_id, actor, parent_id=None, now=None):
        """Atomically retire an independent item and create its project successor."""
        instant=now or utc_now()
        if isinstance(instant,str): instant=parse_datetime(instant)
        data=self.data()
        operation=next((r for r in data['operations'] if r['id']==operation_id),None)
        if not operation: raise ValueError('运维事项不存在')
        if operation.get('promoted_wbs_id'):
            return {'ok':True,'already_promoted':True,'work_item_id':operation['promoted_wbs_id']}
        if operation['version']!=version: raise ValidationError([error('operations',operation,'version','version_conflict','事项已修改')])
        if operation['status'] in ('done','cancelled'): raise ValueError('已完成/取消的事项不能直接转入执行计划')
        decision=next((r for r in data['registers'] if r['id']==decision_id),{})
        if decision.get('status')!='approved' or decision.get('project_id')!=operation.get('project_id') or not decision.get('decision') or not decision.get('decision_by_id'):
            raise ValueError('转入项目须有同项目具名已批准决定')
        wid='WBS-'+uuid.uuid4().hex[:10]
        allowed={f.name for f in TABLES['wbs']['fields']}
        row={k:v for k,v in operation.items() if k in allowed and k not in ('id','version','actual_start','actual_finish','baseline_start','baseline_finish','baseline_change_id')}
        row.update(id=wid,scope='project',node_kind='task',parent_id=parent_id,status='intake',register_id=decision_id,decision_id=decision_id)
        retired=dict(operation,status='cancelled',cancel_reason='转入正式项目计划：'+wid,promoted_wbs_id=wid,promoted_at=instant.isoformat(),promotion_decision_id=decision_id)
        result=self.apply_batch({'wbs':[row],'operations':[retired]},actor=actor,now=instant)
        return dict(result,work_item_id=wid,operation_id=operation_id)

    def delete_record(self, table, record_id, *, version, actor, now=None, dry_run=False):
        if table not in TABLES: raise ValueError("未知业务表")
        if not actor or not str(actor).strip(): raise ValueError("操作者不能为空")
        instant = now or utc_now()
        if isinstance(instant,str): instant=parse_datetime(instant)
        if instant.tzinfo is None: raise ValueError("now必须带时区")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            data=self.data()
            row=next((r for r in data[table] if r['id']==record_id),None)
            if row is None: raise ValueError("记录不存在")
            if isinstance(version,bool) or version!=row['version']:
                raise ValidationError([error(table,row,'version','version_conflict','记录已改变，请重新读取')])
            references=[]
            for name,meta in TABLES.items():
                for field in meta['fields']:
                    if field.ref!=table: continue
                    for item in data[name]:
                        value=item.get(field.name)
                        if (field.dtype=='refs' and record_id in (value or [])) or (field.dtype!='refs' and value==record_id):
                            references.append({'table':name,'id':item['id'],'field':field.name})
            if references: raise ValidationError([error(table,row,'id','referenced','存在关联引用，不能删除：'+json.dumps(references,ensure_ascii=False))])
            if table=='wbs' and row.get('migrated_operation_id'): raise ValueError('历史兼容记录不能删除')
            data[table]=[r for r in data[table] if r['id']!=record_id]
            validate_state(data,now=instant)
            if dry_run: self.conn.rollback()
            else:
                self.conn.execute(f'DELETE FROM "{table}" WHERE id=?',(record_id,))
                self.conn.execute('INSERT INTO event_log(transaction_id,recorded_at,actor,entity,record_id,before_json,after_json) VALUES(?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex,instant.astimezone(timezone.utc).isoformat(),actor,table,record_id,json.dumps(row,ensure_ascii=False),'null'))
                self.conn.commit()
            return {'ok':True,'deleted':not dry_run,'dry_run':dry_run,'table':table,'id':record_id}
        except Exception:
            self.conn.rollback(); raise

    def data(self):
        output={}
        for t,m in TABLES.items():
            rows=[dict(r) for r in self.conn.execute(f'SELECT * FROM "{t}" ORDER BY id')]
            for r in rows:
                for f in m["fields"]:
                    if f.dtype=="boolean" and r.get(f.name) is not None: r[f.name]=bool(r[f.name])
                    if f.dtype=="refs" and r.get(f.name) is not None: r[f.name]=json.loads(r[f.name])
            output[t]=rows
        return output
    def apply_batch(self,batch,*,actor="local-user",now=None,dry_run=False):
        if not isinstance(batch,dict): raise ValidationError([{"code":"batch","message":"batch必须是表名到记录数组的对象"}])
        for t,rows in batch.items():
            if t not in TABLES or not isinstance(rows,list): raise ValidationError([{"table":t,"code":"table","message":"未知表或记录不是数组"}])
        if not actor or not str(actor).strip(): raise ValueError("操作者标签不能为空")
        now=now or utc_now()
        if isinstance(now,str): now=parse_datetime(now)
        if now.tzinfo is None: raise ValueError("now必须带时区")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            before=self.data(); index={t:{r["id"]:r for r in rows} for t,rows in before.items()}
            proposed={t:dict(rows) for t,rows in index.items()}
            changes=[]; seen=set()
            for t in ORDER:
                for source in batch.get(t,[]):
                    if not isinstance(source,dict): raise ValidationError([{"table":t,"code":"record","message":"记录必须是对象"}])
                    if source.get("id") is not None and not isinstance(source.get("id"),str):
                        raise ValidationError([error(t,source,"id","invalid_id","编号必须是文本")])
                    rid=source.get("id") or f"{TABLES[t]['prefix']}-{uuid.uuid4().hex[:10]}"
                    if (t,rid) in seen: raise ValidationError([error(t,{"id":rid},"id","duplicate_batch","同一事务中编号重复")])
                    seen.add((t,rid)); old=index[t].get(rid)
                    if old and old.get("migrated_operation_id"):
                        raise ValidationError([error(t,old,"id","migrated_readonly","历史兼容记录只读，请维护独立运维事项")])
                    incoming=dict(source,id=rid)
                    supplied=incoming.get("version")
                    if old and (isinstance(supplied,bool) or not isinstance(supplied,int) or supplied!=old["version"]): raise ValidationError([error(t,incoming,"version","version_conflict",f"当前版本为{old['version']}，请重新导出并合并修改")])
                    if not old and supplied not in (None,0): raise ValidationError([error(t,incoming,"version","unknown_record","带已有版本的编号不存在，不能把改过的编号当新增")])
                    row=_normalise(t,incoming,old)
                    if t=="wbs" and row.get("scope")!="project":
                        raise ValidationError([error(t,row,"scope","operations_separate","运维/零星事项请录入独立operations表；旧JSON须先按归属转换")])
                    row["version"]=(old["version"]+1) if old else 1
                    if old and all(row[k]==old.get(k) for k in row if k!="version"): continue
                    proposed[t][rid]=row; changes.append((t,old,row))
            validate_state({t:list(v.values()) for t,v in proposed.items()},now=now,previous=index)
            tx=uuid.uuid4().hex
            if not dry_run:
                for t,old,row in changes:
                    names=list(row); quoted=",".join('"'+n+'"' for n in names)
                    updates=",".join(f'"{n}"=excluded."{n}"' for n in names if n!="id")
                    refs={f.name for f in TABLES[t]["fields"] if f.dtype=="refs"}
                    values=[json.dumps(row[n],ensure_ascii=False) if n in refs and row[n] is not None else row[n] for n in names]
                    self.conn.execute(f'INSERT INTO "{t}" ({quoted}) VALUES ({",".join("?" for _ in names)}) ON CONFLICT(id) DO UPDATE SET {updates}',values)
                    self.conn.execute("INSERT INTO event_log(transaction_id,recorded_at,actor,entity,record_id,before_json,after_json) VALUES(?,?,?,?,?,?,?)",(tx,now.astimezone(timezone.utc).isoformat(),actor,t,row["id"],json.dumps(old,ensure_ascii=False) if old else None,json.dumps(row,ensure_ascii=False)))
                self.conn.commit()
            else: self.conn.rollback()
            return {"ok":True,"dry_run":dry_run,"changed":len(changes),"records":[{"table":t,"id":r["id"],"version":r["version"]} for t,_,r in changes]}
        except Exception:
            self.conn.rollback()
            raise
    def snapshot(self,label,*,cutoff=None):
        created=utc_now().isoformat(); cutoff=cutoff or created
        parsed_cutoff=parse_datetime(cutoff)
        if parsed_cutoff>parse_datetime(created): raise ValueError("快照截止时间不能在未来")
        sid="SNAP-"+uuid.uuid4().hex[:10]
        # A captured current-state snapshot, not a fabricated historical reconstruction.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            watermark=self.conn.execute("SELECT COALESCE(MAX(event_id),0) FROM event_log").fetchone()[0]
            latest=self.conn.execute("SELECT MAX(recorded_at) FROM event_log").fetchone()[0]
            if latest and parsed_cutoff<parse_datetime(latest): raise ValueError("当前数据包含截止时间之后的更新；请读取已保存历史快照，不能倒签当前状态")
            payload=self.data()
            self.conn.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",(sid,label,created,cutoff,watermark,json.dumps(payload,ensure_ascii=False)))
            self.conn.commit()
            return {"id":sid,"label":label,"created_at":created,"cutoff":cutoff,"event_watermark":watermark}
        except Exception: self.conn.rollback(); raise
    def read_snapshot(self,sid):
        r=self.conn.execute("SELECT * FROM snapshots WHERE id=?",(sid,)).fetchone()
        if not r: raise KeyError(sid)
        result=dict(r); result["data"]=json.loads(result.pop("data_json")); return result
    def history(self,entity=None,record_id=None):
        clauses=[]; args=[]
        if entity: clauses.append("entity=?"); args.append(entity)
        if record_id: clauses.append("record_id=?"); args.append(record_id)
        where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
        return [dict(r) for r in self.conn.execute("SELECT * FROM event_log"+where+" ORDER BY event_id",args)]
