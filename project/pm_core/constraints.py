"""Resource exposure from explicit facts, never an automatic schedule or verdict."""
from datetime import date, timedelta

from .resources import capacity_summary
from .store import parse_datetime


def resource_constraints(data, *, now):
    """Return unresolved resource conditions for the configured single project.

    ``work_item_ids`` are direct, explicit links (including parent WBS links).
    The workflow projection owns downstream/ancestor expansion. Missing effort,
    resource-use dates and handover evidence stay unknown, not estimated delay.
    """
    from .operating import context
    project, instant, local, _ = context(data, now)
    pid, today = project['id'], local.date().isoformat()
    monday = (local.date() - timedelta(days=local.weekday())).isoformat()
    sunday = (date.fromisoformat(monday) + timedelta(days=6)).isoformat()
    people = {p['id']: p for p in data.get('people', [])}
    all_works = {w['id']: w for w in data.get('wbs', [])
                 if w.get('project_id') == pid and w.get('scope') == 'project'
                 and not w.get('migrated_operation_id')}
    works = {wid: w for wid, w in all_works.items()
             if w.get('status') not in ('done', 'cancelled')}
    all_operations = {w['id']: w for w in data.get('operations', []) if w.get('project_id') == pid}
    operations = {wid: w for wid, w in all_operations.items()
                  if w.get('status') not in ('done', 'cancelled') and not w.get('promoted_wbs_id')}
    participants = [p for p in data.get('participants', [])
                    if p.get('work_item_id') in works and
                    p.get('role') in ('collaborator', 'interface')]
    results = []

    def confirmed(timestamp):
        if not timestamp:
            return False
        try:
            parsed = parse_datetime(timestamp)
            return parsed.tzinfo is not None and parsed <= instant
        except (ValueError, TypeError):
            return False

    def time_match(work, start, end):
        a, b = work.get('forecast_start'), work.get('forecast_finish')
        # An overdue unfinished forecast does not certify the new remaining window.
        if not a or not b or a > b or b < today:
            return 'timing_unknown'
        return 'overlap' if a <= end and start <= b else None

    def person_links(person_id, start, end):
        links = {}
        for wid, work in works.items():
            if work.get('node_kind') != 'task':
                continue
            direct = person_id == work.get('owner_id') or person_id in (work.get('member_ids') or [])
            rels = [r for r in participants if r.get('person_id') == person_id
                    and r['work_item_id'] == wid and
                    (not r.get('starts_on') or r['starts_on'] <= end) and
                    (not r.get('ends_on') or r['ends_on'] >= start)]
            if not direct and not rels:
                continue
            match = time_match(work, start, end)
            if match == 'overlap' and not direct:
                rels = [r for r in rels if
                        max(start, work['forecast_start'], r.get('starts_on') or start) <=
                        min(end, work['forecast_finish'], r.get('ends_on') or end)]
                if not rels:
                    continue
            if match:
                links[wid] = match
        return links

    def explicit_links(row, *, reverse=False):
        ids = {row.get('work_item_id')}
        if reverse:
            ids.update(wid for wid, w in works.items()
                       if row['id'] in (w.get('decision_id'), w.get('register_id')))
        op = operations.get(row.get('operation_id'))
        if op and op.get('related_wbs_id'):
            ids.add(op['related_wbs_id'])
        return sorted(ids.intersection(works))

    def belongs(row, links):
        return row.get('project_id') == pid or (not row.get('project_id') and bool(links))

    def add(table, source, kind, title, reason, task_ids, *, owner_id=None,
            deadline=None, deadline_basis=None, evidence=None, next_action='',
            fields=(), assessment='risk', limitations=()):
        ids = sorted(set(task_ids).intersection(works))
        operation_ids = [source['operation_id']] if source.get('operation_id') in all_operations else []
        scope = 'operations' if operation_ids and not ids else 'project'
        limits = list(limitations)
        if scope == 'operations':
            limits.append('仅明确关联运维事项，没有项目WBS连接；不计入项目任务影响或项目待定位风险。')
        elif not ids:
            limits.append('尚未定位到未关闭项目任务；不能认定影响全部任务。')
        results.append({
            'id': f'{table}:{source["id"]}:{kind}', 'kind': kind,
            'title': title, 'source_table': table, 'source_id': source['id'],
            'source_url': source.get('source_url') or source.get('spec_url'),
            'owner_id': owner_id or project.get('pm_id'), 'work_item_ids': ids,
            'scope': scope, 'operation_ids': operation_ids,
            'reason': reason, 'deadline': deadline, 'deadline_basis': deadline_basis,
            'next_action': next_action, 'required_fields': list(fields),
            'assessment': assessment if ids or scope == 'operations' else 'needs_mapping',
            'evidence': evidence or {}, 'limitations': limits,
        })

    limits_hours = ['仅有人员/项目容量，没有逐任务所需工时，不能推算具体任务延期天数。',
                    '净可用量已扣除外部占用或休假时，不重复扣减。']
    for leave in data.get('leave_plans', []):
        if leave.get('project_id') != pid or leave.get('status') == 'cancelled':
            continue
        start, end = leave.get('start_date'), leave.get('end_date')
        if end and end < today:
            continue
        links = person_links(leave.get('person_id'), start or today, end or start or today)
        is_confirmed = leave.get('status') == 'confirmed' and confirmed(leave.get('confirmed_at'))
        overlapping = [wid for wid, state in links.items() if state == 'overlap']
        label = people.get(leave.get('person_id'), {}).get('name', leave.get('person_id') or '未定位成员')
        reason = '已确认休假' if is_confirmed else '休假计划尚待确认'
        reason += '与任务计划日期重叠，交接落实情况待评估。' if overlapping else '；未确认与任务的时间重叠，需核对剩余计划和交接。'
        evidence = {k: leave.get(k) for k in ('person_id', 'start_date', 'start_period', 'end_date',
                    'end_period', 'status', 'confirmed_at', 'backup_id', 'impact', 'source')}
        evidence['task_timing'] = links
        add('leave_plans', leave, 'leave', label + '：休假与交接', reason, links,
            owner_id=project.get('pm_id'), deadline_basis='休假窗口见来源事实；最晚交接承诺待PM确认',
            evidence=evidence, assessment='risk' if is_confirmed else 'pending_confirmation',
            next_action='核实受影响任务的剩余工作、替补容量及交接承诺，决定调序或调整预测。',
            fields=('backup_id', 'impact'), limitations=[
                '交接人字段只表示人选，不证明已承接且有可用容量。',
                '仅比较日期范围；保留半日事实，不把半日休假放大为全天损失。',
                '未提供任务级工时，不能将休假直接换算为延期天数。'])

    capacity_rows = capacity_summary(data, pid)['people']
    current_recorded = set()
    for row in capacity_rows:
        start = row.get('week_start')
        if not start:
            continue
        end = (date.fromisoformat(start) + timedelta(days=6)).isoformat()
        if end < today:
            continue
        if start == monday:
            current_recorded.add(row.get('person_id'))
        is_confirmed = confirmed(row.get('confirmed_at'))
        status = row['status'] if is_confirmed else 'pending'
        if status == 'available':
            continue
        links = person_links(row.get('person_id'), start, end)
        reason = {'overallocated': '已登记分配超过已确认净可用容量。',
                  'buffer_conflict': '已登记分配挤占已确认缓冲，需协调取舍。',
                  'pending': '净可用量、分配或确认依据尚不完整，不能视为资源已落实。'}[status]
        label = people.get(row.get('person_id'), {}).get('name', row.get('person_id') or '未定位成员')
        evidence = {k: row.get(k) for k in ('person_id', 'week_start', 'net_available_hours',
                    'project_hours', 'operations_hours', 'buffer_hours', 'confirmed_at', 'notes')}
        evidence.update(capacity_status=status, task_timing=links,
                        person_type=people.get(row.get('person_id'), {}).get('person_type'))
        add('resource_weeks', row, 'capacity', label + '：周资源落实', reason, links,
            owner_id=project.get('pm_id'), deadline_basis='所属周见来源事实；最晚落实时间待PM确认',
            evidence=evidence, assessment='pending_confirmation' if status == 'pending' else 'risk',
            next_action='PM核对净容量、项目/运维分配与任务优先级，确认具体受影响任务和调配方案。',
            fields=('net_available_hours', 'project_hours', 'operations_hours', 'buffer_hours', 'confirmed_at'),
            limitations=limits_hours)

    # Coverage gap: only the current week, not a fabricated full-project capacity plan.
    assigned = {w.get('owner_id') for w in works.values() if w.get('node_kind') == 'task'}
    assigned.update(p for w in works.values() for p in (w.get('member_ids') or []))
    assigned.update(r.get('person_id') for r in participants)
    for person_id in sorted(assigned - current_recorded - {None}):
        links = person_links(person_id, monday, sunday)
        if not links:
            continue
        label = people.get(person_id, {}).get('name', person_id)
        source = {'id': f'missing:{person_id}:{monday}'}
        add('resource_weeks', source, 'capacity_unknown', label + '：本周容量依据缺失',
            '未找到已分配成员本周的资源记录；不能据此假设全勤或满额可用。', links,
            owner_id=project.get('pm_id'), deadline_basis='待PM确认最晚落实时间',
            evidence={'person_id': person_id, 'week_start': monday, 'record_exists': False,
                      'person_type': people.get(person_id, {}).get('person_type'), 'task_timing': links},
            assessment='pending_confirmation', fields=('resource_weeks.net_available_hours',),
            next_action='由PM核实任务所需资源及已确认可用量；员工免例行资源催填，不能因此推定可用。',
            limitations=limits_hours + ['这是一项覆盖缺口，没有虚构资源记录；仅核对当周，未来容量另需复核。'])

    for row in data.get('daily_confirmations', []):
        day = row.get('date')
        if row.get('project_id') != pid or not day or day < today:
            continue
        values = [row.get(k) for k in ('available_hours', 'project_hours', 'operations_hours')]
        known = all(v is not None for v in values) and row.get('response_status') in ('confirmed', 'corrected') and confirmed(row.get('confirmed_at'))
        shortage = known and (values[1] + values[2] > values[0] or values[0] == 0)
        if known and not shortage:
            continue  # Shared/remote work alone does not demonstrate a capacity shortage.
        links = person_links(row.get('person_id'), day, day)
        evidence = {k: row.get(k) for k in ('person_id', 'date', 'period', 'work_location',
                    'allocation_mode', 'available_hours', 'project_hours', 'operations_hours',
                    'response_status', 'confirmed_at', 'source')}
        evidence['task_timing'] = links
        add('daily_confirmations', row, 'daily_availability', '当日安排与任务资源核对',
            '已确认的该时段可用量不足或为零，任务影响需核实。' if shortage else '该时段资源安排或确认依据不完整，尚不能判断可用量。',
            links, owner_id=project.get('pm_id'), deadline_basis='安排日期/时段见来源事实；最晚落实承诺待确认',
            evidence=evidence, assessment='risk' if shortage else 'pending_confirmation',
            fields=('available_hours', 'project_hours', 'operations_hours'),
            next_action='核对该时段实际需要推进的任务和替补/调序方案；不按地点或兼项直接推算工期。',
            limitations=limits_hours + ['任务没有半日执行时点；日期重叠仅提示暴露，不证明该时段必需工作。'])

    for row in data.get('interfaces', []):
        if row.get('project_id') != pid or row.get('status') in ('ready', 'retired'):
            continue
        links = explicit_links(row)
        expected = row.get('expected_ready')
        add('interfaces', row, 'interface', row.get('name', row['id']),
            '接口尚未就绪；预计就绪 ' + (expected or '待补') + '，最晚需用时点待确认。', links,
            owner_id=row.get('owner_id'),
            evidence={k: row.get(k) for k in ('status', 'expected_ready', 'work_item_id', 'operation_id',
                      'owner_id', 'counterparty_id', 'notes', 'spec_url')},
            next_action='确认接口消费节点、最晚需用时间、交付承诺与替代/模拟方案。',
            fields=('expected_ready', 'work_item_id', 'notes'),
            limitations=['关联任务可能是接口开发任务本身，不能假定接口必须在该任务开始前就绪。',
                         '预计就绪日不等于最晚需用时间；未计算关键路径或项目延期天数。'])

    for row in data.get('registers', []):
        links = explicit_links(row, reverse=True)
        if not belongs(row, links) or row.get('status') in ('closed', 'rejected'):
            continue
        decision = row.get('kind') == 'decision'
        if decision and row.get('status') == 'approved' and row.get('decision') and row.get('decision_by_id') and confirmed(row.get('decided_at')):
            continue
        kind = 'decision' if decision else 'register'
        add('registers', row, kind, row.get('title', row['id']),
            '决定结论尚未落实或依据不完整。' if decision else '开放事项尚未关闭，需核对所需条件或物资是否落实。',
            links, owner_id=row.get('decision_by_id') if decision else row.get('owner_id'),
            deadline=row.get('deadline_at'), deadline_basis='登记的最迟处理时间',
            evidence={k: row.get(k) for k in ('kind', 'status', 'impact', 'proposal', 'next_action',
                      'decision', 'decided_at', 'closure_evidence', 'work_item_id', 'operation_id')},
            fields=('decision', 'decided_at') if decision else ('next_action', 'closure_evidence'),
            next_action=row.get('next_action') or ('确认决定人、所需结论、最晚决定时间及受影响任务。' if decision else
                        '补齐任务关联、资源落实/到货期限、责任人及验证依据；有实质阻碍再登记受阻事实。'),
            assessment='pending_confirmation' if decision else 'risk',
            limitations=['开放登记不自动等于硬阻塞；物资等条件按明确登记事实评估，不按标题关键词推断。'])
    return sorted(results, key=lambda row: row['id'])
