"""Run with: python -m pm_core.cli --help"""
from datetime import datetime,timezone
from pathlib import Path
import argparse
import json
import sqlite3
import sys

from .calendar import build_calendar,load_calendar_overrides,current_local_date,CalendarValidationError
from .fixtures import base_data,demo_data,operating_demo_data,DEFAULT_OVERRIDES,CALENDAR_ID
from .schema import catalogue,dictionary_markdown,TABLES
from .store import Store,ValidationError,utc_now,validate_state
from .reports import summary,export_html

def write_json(path,obj):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")

def main(argv=None):
    p=argparse.ArgumentParser(description="pmmaster 单项目管理。通过智能体或 Excel 维护数据，统一校验并生成独立 HTML 汇报。")
    sub=p.add_subparsers(dest="command",required=True)
    a=sub.add_parser("init",help="创建空白业务库和真实日期表")
    a.add_argument("--db",required=True);a.add_argument("--start");a.add_argument("--end");a.add_argument("--timezone",default="Asia/Shanghai");a.add_argument("--calendar-id",default="CAL-DEFAULT");a.add_argument("--overrides")
    a=sub.add_parser("demo",help="创建完全隔离的虚构演示库/Excel/Waterfall")
    a.add_argument("--out",default="demo_run")
    a=sub.add_parser("export",help="从数据库生成可编辑Excel")
    a.add_argument("--db",required=True);a.add_argument("--out",required=True);a.add_argument("--month");a.add_argument("--calendar-id",required=True)
    for name in ("import-excel","import-json"):
        a=sub.add_parser(name,help="先校验再整批提交；--dry-run只检查")
        a.add_argument("--db",required=True);a.add_argument("--file",required=True);a.add_argument("--actor",default="local-user");a.add_argument("--dry-run",action="store_true");a.add_argument("--errors")
    a=sub.add_parser("calendar",help="扩展日期范围或导入年度/公司日历例外，保留既有未覆盖记录")
    a.add_argument("--db",required=True);a.add_argument("--calendar-id",required=True);a.add_argument("--start",required=True);a.add_argument("--end",required=True);a.add_argument("--overrides");a.add_argument("--actor",default="calendar-maintainer")
    a=sub.add_parser("summary");a.add_argument("--db",required=True)
    a=sub.add_parser("report");a.add_argument("--db",required=True);a.add_argument("--out",required=True);a.add_argument("--snapshot");a.add_argument("--demo",action="store_true")
    a=sub.add_parser("snapshot");a.add_argument("--db",required=True);a.add_argument("--label",required=True)
    a=sub.add_parser("history");a.add_argument("--db",required=True);a.add_argument("--table");a.add_argument("--id")
    a=sub.add_parser("dictionary");a.add_argument("--out",required=True)
    a=sub.add_parser("check");a.add_argument("--db",required=True)
    a=sub.add_parser("refresh",help="导入Excel并重新生成Excel和Waterfall")
    a.add_argument("--db",required=True);a.add_argument("--file",required=True);a.add_argument("--out",required=True);a.add_argument("--report",required=True);a.add_argument("--month");a.add_argument("--calendar-id",required=True);a.add_argument("--actor",default="local-user")
    a=sub.add_parser("weekly-report",help="生成周一计划/周五验收；--confirm在周一冻结承诺")
    a.add_argument("--db",required=True);a.add_argument("--out",required=True);a.add_argument("--kind",choices=("monday","friday"),required=True);a.add_argument("--confirm",action="store_true");a.add_argument("--actor",default="local-user");a.add_argument("--timezone");a.add_argument("--demo",action="store_true")
    a.add_argument("--review-of",help="周五会后版本关联的会前报告编号")
    a.add_argument("--audit-policy",help="日期变更审议的已核实批准配置JSON")
    a=sub.add_parser("weekly-config",help="读取或设置周报时区、时间与启用状态")
    a.add_argument("--db",required=True);a.add_argument("--timezone");a.add_argument("--monday-time");a.add_argument("--friday-time")
    g=a.add_mutually_exclusive_group();g.add_argument("--enable",dest="enabled",action="store_true",default=None);g.add_argument("--disable",dest="enabled",action="store_false")
    a=sub.add_parser("weekly-tick",help="检查到期周报，安全重复调用；可由Windows任务计划执行")
    a.add_argument("--db",required=True);a.add_argument("--out",required=True);a.add_argument("--demo",action="store_true")
    a=sub.add_parser("project-report",help="生成单项目的图表、彩色瀑布、本周交付、会议待办和风险汇报")
    a.add_argument("--db",required=True);a.add_argument("--out",required=True);a.add_argument("--kind",choices=("summary","plan","review"),default="summary");a.add_argument("--timezone",default="America/Vancouver");a.add_argument("--demo",action="store_true")
    a=sub.add_parser("archive-config",help="设置周日20:00 Excel留档；指定源文件则精确复制，否则导出已提交事实")
    a.add_argument("--db",required=True);a.add_argument("--timezone");a.add_argument("--sunday-time");a.add_argument("--source-workbook")
    a.add_argument("--clear-source",action="store_true")
    g=a.add_mutually_exclusive_group();g.add_argument("--enable",dest="enabled",action="store_true",default=None);g.add_argument("--disable",dest="enabled",action="store_false")
    for name in ("archive-now","archive-tick","archive-retry"):
        a=sub.add_parser(name);a.add_argument("--db",required=True);a.add_argument("--out",required=True)
        if name!="archive-tick": a.add_argument("--actor",default="local-user")
        if name=="archive-retry": a.add_argument("--id",required=True)
    a=sub.add_parser("archive-list");a.add_argument("--db",required=True)
    from .integration_cli import add_commands, run as run_integration
    integration_commands = add_commands(sub)
    args=p.parse_args(argv)
    try:
        if args.command in integration_commands:
            result=run_integration(args)
        elif args.command=="dictionary":
            out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
            write_json(out/"schema.json",catalogue());(out/"字段字典.md").write_text(dictionary_markdown(),encoding="utf-8")
            result={"ok":True,"output":str(out),"tables":len(TABLES)}
        elif args.command.startswith("archive-"):
            if not Path(args.db).is_file(): raise ValueError("数据库不存在；请先init或demo")
            from .archive import get_archive_config,save_archive_config,capture_archive,run_due_archive,retry_archive,list_archives
            if args.command=="archive-config":
                patch={key:getattr(args,key) for key in ("timezone","sunday_time","source_workbook","enabled") if getattr(args,key) is not None}
                if args.clear_source:
                    if args.source_workbook: raise ValueError("指定源文件和清除源文件不能同时使用")
                    patch["source_workbook"]=None
                result=save_archive_config(args.db,patch) if patch else get_archive_config(args.db)
            elif args.command=="archive-now": result=capture_archive(args.db,args.out,actor=args.actor)
            elif args.command=="archive-tick": result={"archives":run_due_archive(args.db,args.out)}
            elif args.command=="archive-retry": result=retry_archive(args.db,args.id,args.out,actor=args.actor)
            else: result={"archives":list_archives(args.db)}
        elif args.command in ("weekly-report","weekly-config","weekly-tick"):
            if not Path(args.db).is_file(): raise ValueError("数据库不存在；请先init或demo")
            from .weekly import get_config,save_config,generate_weekly_report,run_due
            if args.command=="weekly-config":
                patch={key:getattr(args,key) for key in ("timezone","monday_time","friday_time","enabled") if getattr(args,key) is not None}
                result=save_config(args.db,patch) if patch else get_config(args.db)
            elif args.command=="weekly-tick":
                from .archive import run_due_archive
                result={"weekly":run_due(args.db,args.out,demo=args.demo),"archives":run_due_archive(args.db,Path(args.db).resolve().parent/"excel_archives")}
            else: result=generate_weekly_report(args.db,args.out,kind=args.kind,actor=args.actor,confirm=args.confirm,timezone_name=args.timezone,demo=args.demo,review_of=args.review_of,schedule_audit_policy=json.loads(Path(args.audit_policy).read_text(encoding="utf-8-sig")) if args.audit_policy else None)
        elif args.command=="demo":
            from .excel_io import export_workbook
            out=Path(args.out);out.mkdir(parents=True,exist_ok=True);db=out/"demo.sqlite3"
            if db.exists(): raise ValueError("演示数据库已存在；请使用新的--out目录，避免覆盖已有修改")
            with Store(db) as store:
                store.apply_batch(operating_demo_data(),actor="fictional-demo")
                from .project_calendar import ensure_project_calendar
                ensure_project_calendar(store, store.data()["projects"][0]["id"], actor="fictional-demo-calendar")
                now=utc_now().isoformat();data=store.data()
                export_workbook(data,out/"演示工作台.xlsx",month="2026-11",calendar_id=CALENDAR_ID,as_of=now)
                from .project_report import export_project_report
                export_project_report(data,out/"本周项目汇报.html",as_of=now,demo=True)
                snap=store.snapshot("演示初始状态")
                write_json(out/"demo_facts.json",data)
                result={"ok":True,"output":str(out),"snapshot":snap,"note":"全部业务数据均为虚构示例；仅日历例外引用官方2026通知"}
        else:
            if args.command!="init" and not Path(args.db).is_file(): raise ValueError("数据库不存在；请先init或demo，避免误读空库")
            if args.command=="init" and Path(args.db).exists(): raise ValueError("数据库已存在；扩展日历请用calendar，不覆盖数据库")
            with Store(args.db) as store:
                if args.command=="init":
                    today=current_local_date(args.timezone);start=args.start or today[:4]+"-01-01";end=args.end or today[:4]+"-12-31"
                    result=store.apply_batch(base_data(start=start,end=end,timezone=args.timezone,calendar_id=args.calendar_id,overrides=args.overrides),actor="initialization")
                    result.update({"db":args.db,"calendar_id":args.calendar_id,"timezone":args.timezone,"today":today})
                elif args.command in ("import-excel","import-json","refresh"):
                    if args.command in ("import-excel","refresh"):
                        from .excel_io import read_workbook
                        batch=read_workbook(args.file)
                    else: batch=json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
                    result=store.apply_batch(batch,actor=args.actor,dry_run=getattr(args,"dry_run",False))
                    if args.command=="refresh":
                        from .excel_io import export_workbook
                        data=store.data();now=utc_now().isoformat();cal=next(c for c in data["calendars"] if c["id"]==args.calendar_id)
                        month=args.month or current_local_date(cal["timezone"])[:7]
                        export_workbook(data,args.out,month=month,calendar_id=args.calendar_id,as_of=now)
                        from .project_report import export_project_report
                        export_project_report(data,args.report,as_of=now);result.update({"excel":args.out,"report":args.report})
                elif args.command=="export":
                    from .excel_io import export_workbook
                    data=store.data();cal=next((c for c in data["calendars"] if c["id"]==args.calendar_id),None)
                    if not cal: raise ValueError("日历不存在")
                    month=args.month or current_local_date(cal["timezone"])[:7]
                    export_workbook(data,args.out,month=month,calendar_id=args.calendar_id,as_of=utc_now().isoformat());result={"ok":True,"excel":args.out,"month":month}
                elif args.command=="calendar":
                    data=store.data();cal=next((c for c in data["calendars"] if c["id"]==args.calendar_id),None)
                    if not cal: raise ValueError("日历不存在")
                    overrides=load_calendar_overrides(args.overrides) if args.overrides else []
                    overrides=[r for r in overrides if args.start<=r["date"]<=args.end]
                    generated=build_calendar(args.start,args.end,cal["timezone"],overrides)
                    old={r["date"]:r for r in data["calendar_days"] if r["calendar_id"]==args.calendar_id};overridden={r["date"] for r in overrides}
                    changed=[]
                    for r in generated:
                        if r["date"] in old and r["date"] not in overridden: continue
                        record={"id":f"{args.calendar_id}-{r['date']}","calendar_id":args.calendar_id,**r}
                        if r["date"] in old: record.update({"id":old[r["date"]]["id"],"version":old[r["date"]]["version"]})
                        changed.append(record)
                    cal.update(coverage_start=min(cal["coverage_start"],args.start),coverage_end=max(cal["coverage_end"],args.end))
                    result=store.apply_batch({"calendars":[cal],"calendar_days":changed},actor=args.actor)
                elif args.command=="summary": result=summary(store.data(),utc_now().isoformat())
                elif args.command=="project-report":
                    from .project_report import export_project_report
                    from .schedule_audit import audit_schedule_changes
                    data=store.data();cutoff=utc_now().isoformat()
                    audit=audit_schedule_changes(data,history=store.history(),now=cutoff)
                    result=export_project_report(data,args.out,as_of=cutoff,kind=args.kind,timezone_name=args.timezone,demo=args.demo,schedule_audit=audit)
                elif args.command=="report":
                    if args.snapshot: saved=store.read_snapshot(args.snapshot);data=saved["data"];as_of=saved["cutoff"]
                    else: data=store.data();as_of=utc_now().isoformat()
                    from .project_report import export_project_report
                    from .schedule_audit import audit_schedule_changes
                    history=store.history()
                    if args.snapshot:
                        history=[event for event in history if event["event_id"]<=saved["event_watermark"]]
                    audit=audit_schedule_changes(data,history=history,now=as_of)
                    result=export_project_report(data,args.out,as_of=as_of,demo=args.demo,schedule_audit=audit);result["output"]=args.out
                elif args.command=="snapshot": result=store.snapshot(args.label)
                elif args.command=="history": result=store.history(args.table,args.id)
                elif args.command=="check":
                    data=store.data();validate_state(data,previous={t:{r["id"]:r for r in rows} for t,rows in data.items()})
                    fk=[tuple(r) for r in store.conn.execute("PRAGMA foreign_key_check")];integrity=store.conn.execute("PRAGMA integrity_check").fetchone()[0]
                    result={"ok":not fk and integrity=="ok","foreign_key_errors":fk,"integrity":integrity,"rows":{t:len(rows) for t,rows in data.items()}}
        if isinstance(result,dict) and len(result.get("records",[]))>20:
            result["records_total"]=len(result["records"])
            result["records"]=result["records"][:20]
            result["note"]="仅显示前20条编号；全部数据已按事务处理，可通过export/history查看。"
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if isinstance(result,dict) and (result.get("state")=="failed" or any(a.get("state")=="failed" for a in result.get("archives",[]))): return 2
        return 0
    except (ValidationError,CalendarValidationError) as exc:
        result=exc.to_dict()
        if getattr(args,"errors",None): write_json(args.errors,result)
        print(json.dumps(result,ensure_ascii=False,indent=2),file=sys.stderr);return 2
    except (ValueError,KeyError,StopIteration,OSError,sqlite3.Error) as exc:
        result=exc.to_dict() if hasattr(exc,"to_dict") else {"ok":False,"error":str(exc)}
        if getattr(args,"errors",None): write_json(args.errors,result)
        print(json.dumps(result,ensure_ascii=False),file=sys.stderr);return 2

if __name__=="__main__": raise SystemExit(main())
