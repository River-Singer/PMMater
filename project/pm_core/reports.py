"""Read-only summaries and project waterfall views generated from validated data."""
from collections import Counter
from datetime import date, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
import json
from .schema import ENUMS, suggested_priority
from .calendar import workday_distance
from .store import parse_datetime

def summary(data,as_of):
    instant=parse_datetime(as_of)
    calendars={r["id"]:r for r in data.get("calendars",[])}
    projects={r["id"]:r for r in data.get("projects",[])}
    services={r["id"]:r for r in data.get("services",[])}
    def cutoff_for(w):
        context=projects.get(w.get("project_id")) or services.get(w.get("service_id")) or {}
        zone=calendars.get(context.get("calendar_id"),{}).get("timezone","UTC")
        return instant.astimezone(ZoneInfo(zone)).date().isoformat()
    people={p["id"]:p for p in data.get("people",[])}
    parents={w.get("parent_id") for w in data.get("wbs",[]) if w.get("scope")=="project"}
    items=[w for w in data.get("wbs",[]) if w.get("scope")=="project" and w.get("node_kind")=="task" and w["id"] not in parents]
    open_items=[w for w in items if w["status"] not in ("done","cancelled")]
    late=[w for w in open_items if w.get("forecast_finish") and w["forecast_finish"]<cutoff_for(w)]
    # Leaf-only counting must not remove explicit dependencies involving a
    # parent task or deliverable. Dependency evidence uses the complete WBS.
    items_by_id={w['id']:w for w in data.get('wbs',[]) if w.get('scope')=='project' and not w.get('migrated_operation_id')}
    dependency_impacts=[]
    for r in data.get("dependencies",[]):
        pre=items_by_id.get(r["predecessor_id"]);suc=items_by_id.get(r["successor_id"])
        if not pre or not suc: continue
        if suc.get("actual_start") and pre["status"]!="done" and suc["status"]!="cancelled":
            dependency_impacts.append({"dependency_id":r["id"],"predecessor_id":pre["id"],"successor_id":suc["id"],"message":"前置当前未完成，后续已有开始事实；请评估返工/回归影响，保留历史"})
    result={"as_of":as_of,"total_work_items":len(items),"confirmed_done":sum(w["status"]=="done" for w in items),
            "blocked":sum(w["status"]=="blocked" for w in items),"overdue":len(late),
            "by_scope":dict(Counter(w["scope"] for w in items)),"by_priority":dict(Counter(w.get("priority") or "untriaged" for w in open_items)),"dependency_impacts":dependency_impacts,
            "workload_counts":[{"person_id":pid,"name":people.get(pid,{}).get("name",pid),"open_items":count} for pid,count in Counter(w["owner_id"] for w in open_items if w.get("owner_id")).items()],
            "notes":["工作项数量不是项目完成百分比，也不是人员工时负荷率。","报告为命令执行时生成的快照；依赖检查不自动求解关键路径或重排工期。"]}
    operations=[w for w in data.get("operations",[]) if not w.get("promoted_wbs_id")]
    result["operations"]={"total":sum(w.get("status")!="cancelled" for w in operations),"done":sum(w.get("status")=="done" for w in operations),"open":sum(w.get("status") not in ("done","cancelled") for w in operations)}
    return result

def export_html(data,path,*,as_of,title="项目与日常工作进度",demo=False):
    s=summary(data,as_of)
    people={p["id"]:p["name"] for p in data.get("people",[])}
    services={p["id"]:p["name"] for p in data.get("services",[])}
    pn=lambda key:escape(people.get(key,"待指定"))
    h=lambda value:escape(str(value or "—"))
    dated=[v for w in data.get("wbs",[]) for k in ("baseline_start","baseline_finish","forecast_finish","actual_start","actual_finish") if (v:=w.get(k))]
    display_timezone=(data.get("calendars") or [{}])[0].get("timezone","UTC")
    today=parse_datetime(as_of).astimezone(ZoneInfo(display_timezone)).date()
    start=date.fromisoformat(min(dated)) if dated else today.replace(day=1)
    end=date.fromisoformat(max(dated)) if dated else today+timedelta(days=30)
    start=min(start,today);end=max(end,today+timedelta(days=3));span=max((end-start).days+1,1)
    def pos(d): return (date.fromisoformat(d)-start).days/span*100
    def bar(w):
        pieces=[]
        for a,b,cl in (("baseline_start","baseline_finish","baseline"),("actual_start","actual_finish","actual")):
            if w.get(a) and w.get(b): pieces.append(f'<i class="{cl}" style="left:{pos(w[a]):.3f}%;width:{max((date.fromisoformat(w[b])-date.fromisoformat(w[a])).days+1,1)/span*100:.3f}%"></i>')
        a=w.get("actual_start") or w.get("baseline_start")
        b=w.get("forecast_finish")
        if a and b and w["status"]!="done": pieces.append(f'<i class="forecast {"blocked" if w["status"]=="blocked" else ""}" style="left:{pos(a):.3f}%;width:{max((date.fromisoformat(b)-date.fromisoformat(a)).days+1,1)/span*100:.3f}%"></i>')
        marker=(today-start).days/span*100
        return '<div class="track">'+''.join(pieces)+f'<span class="asof" style="left:{marker:.3f}%"></span></div>'
    def task(w):
        rel=[r for r in data.get("participants",[]) if r.get("work_item_id")==w["id"]]
        people_lines="；".join(ENUMS["person_role"][r["role"]]+"："+people.get(r["person_id"],r["person_id"]) for r in rel) or "未配置额外参与人"
        suggestion=suggested_priority(w.get("impact"),w.get("urgency"))
        details=f'<div class="details"><p>负责人 {pn(w.get("owner_id"))} · 确认人 {pn(w.get("acceptor_id"))} · 协调人 {pn(w.get("coordinator_id"))}</p><p>{h(people_lines)}</p><p>完成标准：{h(w.get("acceptance_criteria"))}</p><p>问题：{h(w.get("blocker_reason"))}；下一步：{h(w.get("next_action"))}；最迟：{h(w.get("next_due_at"))}</p><p>确认依据：{h(w.get("completion_evidence"))} · 优先级建议：{h(suggestion)} · 决定理由：{h(w.get("priority_reason"))}</p></div>'
        return f'<details class="task" data-scope="{h(w["scope"])}"><summary><span><b>{h(w["title"])}</b><small>{h(w["id"])} · {pn(w.get("owner_id"))} · {h(ENUMS["work_type"][w["work_type"]])}</small></span><span>{h(ENUMS["work_status"][w["status"]])}<small>{h(w.get("priority"))}</small></span>{bar(w)}<span class="dates">{h(w.get("baseline_finish"))}<small>→ {h(w.get("actual_finish") or w.get("forecast_finish"))}</small></span></summary>{details}</details>'
    projects=[]
    for project in data.get("projects",[]):
        works=[w for w in data.get("wbs",[]) if w.get("scope")=="project" and w.get("project_id")==project["id"] and w.get("node_kind")=="task"]
        nodes=[n for n in data.get("wbs",[]) if n["project_id"]==project["id"] and n.get("node_kind")!="task"]
        by_id={r["id"]:r for r in data.get("wbs",[])}
        def node_of(w):
            p=w.get("parent_id")
            while p:
                n=by_id.get(p)
                if n is None: return None
                if n.get("node_kind")!="task": return n["id"]
                p=n.get("parent_id")
            return None
        def branch(parent=None):
            out=[]
            for n in sorted((n for n in nodes if n.get("parent_id")==parent),key=lambda n:n["id"]):
                own=[w for w in works if node_of(w)==n["id"]]
                out.append(f'<details class="wbs" open><summary>{h(n["id"])} {h(n["title"])} <span>{pn(n["owner_id"])}</span></summary>'+branch(n["id"])+''.join(task(w) for w in own)+'</details>')
            return ''.join(out)
        unsorted=[w for w in works if node_of(w) is None]
        calrows=[r for r in data.get("calendar_days",[]) if r["calendar_id"]==project["calendar_id"]]
        deviation="待确认"
        if project.get("baseline_finish") and project.get("forecast_finish"):
            try: deviation=f'{workday_distance(project["baseline_finish"],project["forecast_finish"],calrows):+d} 工作日'
            except ValueError: deviation="日历缺失，无法算工作日"
        projects.append(f'<section data-scope="project"><h2>{h(project["name"])}</h2><p class="lead">{h(project.get("summary"))}</p><p>业务影响：{h(project.get("business_impact"))}</p><div class="project-meta">PO {pn(project["po_id"])} · PM {pn(project["pm_id"])} · 基线 {h(project.get("baseline_finish"))} → 预测 {h(project.get("forecast_finish"))}（{h(deviation)}）</div>'+branch()+(''.join(task(w) for w in unsorted))+'</section>')
    for scope,label in (("operations","产品与服务日常队列"),("adhoc","临时事项")):
        work=[dict(w,scope=scope) for w in data.get("operations",[]) if w.get("scope", "operations")==scope and not w.get("promoted_wbs_id")]
        if work: projects.append(f'<section data-scope="{scope}"><h2>{label}</h2><p>独立事项表；按优先级与责任推进，不计入项目交付统计。</p>'+''.join(task(w) for w in sorted(work,key=lambda w:(w.get("priority") or "P9",w["id"])))+'</section>')
    coordination=[r for r in data.get("registers",[]) if r["status"] not in ("closed","rejected")]
    coord='<section><h2>问题与待决定事项</h2>'+''.join(f'<article><b>{h(ENUMS["register_type"][r["kind"]])} · {h(r["title"])}</b><p>{h(r.get("impact"))}</p><p>处理 {pn(r["owner_id"])} · 协调 {pn(r.get("coordinator_id"))} · 决定 {pn(r.get("decision_by_id"))} · 最迟 {h(r.get("deadline_at"))}</p></article>' for r in coordination)+'</section>'
    body=''.join(projects) or '<section><h2>待录入工作</h2><p>先填写人员、项目/产品与工作项，再导入并刷新报告。</p></section>'
    if s["dependency_impacts"]:
        body+='<section><h2>依赖变化的影响</h2>'+''.join(f'<p>{h(i["predecessor_id"])} → {h(i["successor_id"])}：{h(i["message"])}</p>' for i in s["dependency_impacts"] )+'</section>'
    content='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>pmmaster · __TITLE__</title><style>
    *{box-sizing:border-box}body{margin:0;background:#f2f5f7;color:#18364a;font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-size:14px;line-height:1.6}main{max-width:1320px;margin:28px auto;padding:0 24px}header{padding:22px 0}h1{font-size:30px;margin:10px 0}h2{font-size:22px;margin:0 0 14px}p{margin:8px 0}small{display:block;color:#5e7383;font-size:12px}section{background:white;padding:26px;margin:18px 0;border-radius:12px}.eyebrow{color:#086f73;letter-spacing:.12em}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{background:#e8f1f3;padding:15px}.stat strong{display:block;font-size:26px}.lead{font-size:19px}.project-meta{padding:10px;background:#f3f6f8;margin:14px 0}.wbs{margin:12px 0;padding-left:16px}.wbs>summary{cursor:pointer;padding:8px;font-weight:600;background:#f2f6f8}.wbs>summary span{margin-left:15px;font-weight:400}.task{border-bottom:1px solid #dde5eb}.task>summary{display:grid;grid-template-columns:minmax(150px,2fr) 85px minmax(170px,3fr) 100px;gap:12px;align-items:center;cursor:pointer;padding:14px 8px}.track{height:35px;position:relative;background:repeating-linear-gradient(to right,transparent 0,transparent calc(20% - 1px),#e3e9ee calc(20% - 1px),#e3e9ee 20%)}.track i{position:absolute;display:block;border-radius:3px}.baseline{top:4px;height:5px;background:#c1cbd5}.forecast{top:17px;height:12px;background:#df9a2c}.forecast.blocked{background:#c84947}.actual{top:17px;height:12px;background:#087e83}.asof{position:absolute;top:0;bottom:0;width:2px;background:#18364a}.details{padding:10px 18px;background:#f4f8fa}.dates{font-size:12px}nav{display:flex;gap:8px;flex-wrap:wrap}button{background:white;color:#18364a;border:1px solid #cad6df;border-radius:5px;padding:8px 14px;cursor:pointer}button[aria-pressed=true]{background:#18364a;color:white}.legend{margin:10px 0;color:#526a7b}article{padding:12px 0;border-bottom:1px solid #e1e8ed}.notice{background:#fff1d8;color:#935700;padding:8px 12px;display:inline-block}.footer{font-size:12px;color:#5e7383;margin:20px 0}@media(max-width:750px){main{padding:0 12px}.stats{grid-template-columns:repeat(2,1fr)}section{padding:16px}.task>summary{grid-template-columns:1fr 90px}.track{grid-column:1/-1}.wbs{padding-left:4px}}@media print{nav{display:none}body{background:white}section{break-inside:avoid}details{display:block}}</style></head><body><main>
    <header><div class="eyebrow">pmmaster</div><h1>__TITLE__</h1>__DEMO__<p>数据截至 __ASOF__ · Python 从同一数据库生成</p><nav><button data-filter="all" aria-pressed="true">全部工作</button><button data-filter="project" aria-pressed="false">项目交付</button><button data-filter="operations" aria-pressed="false">日常运维</button><button data-filter="adhoc" aria-pressed="false">临时事项</button></nav></header>
    <div class="stats">__STATS__</div><div class="legend">日期范围 __START__ — __END__ · 灰色细条 原计划 · 金色 预测 · 青色 已完成 · 红色 当前受阻安排 · 竖线 数据截至。条长表示时间，不表示完成百分比。</div>__BODY____COORD__<p class="footer">工作项数量不等于项目完成百分比；工作项数也不等于工时负荷。红色表示受阻任务的当前安排，不声称整个条长都是历史阻塞时长。修改Excel后运行import/refresh，页面生成新快照；本文件没有后台轮询。</p>
    </main><script>document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-filter]').forEach(x=>x.setAttribute('aria-pressed',x===b?'true':'false'));document.querySelectorAll('section[data-scope]').forEach(s=>s.hidden=b.dataset.filter!=='all'&&s.dataset.scope!==b.dataset.filter)}));</script></body></html>'''
    stats=''.join(f'<div class="stat">{label}<strong>{value}</strong></div>' for label,value in (("工作项",s["total_work_items"]),("已确认完成",s["confirmed_done"]),("当前受阻",s["blocked"]),("预测日期已过",s["overdue"])))
    for key,value in {"TITLE":h(title),"DEMO":'<p class="notice">虚构演示数据，不代表真实项目</p>' if demo else '',"ASOF":h(as_of),"STATS":stats,"START":str(start),"END":str(end),"BODY":body,"COORD":coord}.items(): content=content.replace('__'+key+'__',value)
    Path(path).parent.mkdir(parents=True,exist_ok=True);Path(path).write_text(content,encoding="utf-8")
    return s
