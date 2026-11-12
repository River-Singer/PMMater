"""Read-only task readiness and traceable impacts; never an automatic scheduler."""
from datetime import date

from .operating import context, operation_delay
from .project_calendar import workday_status
from .store import parse_datetime


READINESS = {'missing_data': '资料待补', 'awaiting_feedback': '承接待确认',
             'ready': '可执行 / 跟进中', 'closed': '已完成'}


def _present(value):
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _available(person, today):
    return bool(person and person.get('active') and
                (not person.get('starts_on') or person['starts_on'] <= today) and
                (not person.get('ends_on') or person['ends_on'] >= today))


def _contactable(person, mode=None):
    uid = person.get('feishu_user_id')
    if mode == 'feishu':
        return bool(uid and person.get('feishu_id_type', 'open_id') == 'open_id')
    return bool(uid or ('@' in (person.get('email') or '')) or
                sum(c.isdigit() for c in (person.get('phone') or '')) >= 5 or person.get('contact'))


def workflow_review(data, *, now, config=None):
    """Project-wide leaf facts, ancestors and explicitly linked resource exposures.

    An owner's dated progress response means feedback exists, not a formal
    acceptance event. Relationships express possible propagation, never slack
    or a calculated critical path. Incomplete leaves remain in the denominator.
    """
    config = config or {}
    project, instant, local, zone = context(data, now)
    today = local.date().isoformat()
    stale_days = config.get('stale_days', 3)
    if isinstance(stale_days, bool) or not isinstance(stale_days, int) or stale_days < 0:
        raise ValueError('stale_days must be a nonnegative integer')
    people = {p['id']: p for p in data.get('people', [])}
    idx = {w['id']: w for w in data.get('wbs', []) if w.get('project_id') == project['id']
           and w.get('scope') == 'project' and not w.get('migrated_operation_id')}
    children = {wid: [] for wid in idx}
    for wid, w in idx.items():
        if w.get('parent_id') in idx:
            children[w['parent_id']].append(wid)
    ancestors = {}
    for wid in idx:
        found = []; seen = {wid}; current = idx[wid].get('parent_id')
        while current in idx:
            if current in seen:
                raise ValueError('WBS parent cycle: cannot establish a reliable impact path')
            seen.add(current); found.append(current); current = idx[current].get('parent_id')
        ancestors[wid] = found
    leaf_ids = [wid for wid, w in idx.items() if w.get('node_kind') == 'task'
                and not children[wid] and w.get('status') != 'cancelled']
    successors = {wid: [] for wid in idx}
    dependency_gaps = []
    for dep in data.get('dependencies', []):
        a, b = dep.get('predecessor_id'), dep.get('successor_id')
        if a not in idx and b not in idx:
            continue
        if a not in idx or b not in idx:
            dependency_gaps.append('依赖 ' + str(dep.get('id')) + ' 的端点未完整读取')
            continue
        if idx[a].get('status') == 'cancelled' or idx[b].get('status') == 'cancelled':
            continue
        successors[a].append(dict(id=dep.get('id'), from_id=a, to_id=b,
                                  dependency_type=dep.get('dependency_type', 'FS'),
                                  lag_workdays=dep.get('lag_workdays', 0)))
    visiting = set(); visited = set()
    def check_dependencies(wid):
        if wid in visiting:
            raise ValueError('Dependency cycle: cannot establish a reliable impact path')
        if wid in visited: return
        visiting.add(wid)
        for edge in successors[wid]: check_dependencies(edge['to_id'])
        visiting.remove(wid); visited.add(wid)
    for wid in idx: check_dependencies(wid)

    def node(wid):
        return {'id': wid, 'title': idx[wid].get('title', wid), 'owner_id': idx[wid].get('owner_id')}

    def impact_for(ids, *, has_risk=True):
        roots = list(dict.fromkeys(wid for wid in ids if wid in idx and idx[wid].get('status') != 'cancelled'))
        ancestor_ids = list(dict.fromkeys(a for wid in roots for a in ancestors[wid]))
        queue = roots + ancestor_ids; reached = set(queue); downstream = []; edges = []
        while queue:
            source = queue.pop(0)
            for edge in successors[source]:
                if edge not in edges: edges.append(edge)
                target = edge['to_id']
                if target not in downstream and target not in roots: downstream.append(target)
                for next_id in [target, *ancestors[target]]:
                    if next_id not in reached: reached.add(next_id); queue.append(next_id)
        affected_leaves = [wid for wid in leaf_ids if idx[wid].get('status') != 'done'
                           and any(root == wid or root in ancestors[wid] for root in roots)]
        dependency_leaves = [wid for wid in leaf_ids if idx[wid].get('status') != 'done'
                             and any(root == wid or root in ancestors[wid] for root in downstream)]
        downstream_ancestors = list(dict.fromkeys(a for wid in downstream for a in ancestors[wid]))
        linked_ids = reached | set(affected_leaves + dependency_leaves)
        deliveries = [{k: d.get(k) for k in ('id', 'title', 'acceptance_status', 'work_item_id')}
                      for d in data.get('deliverables', []) if d.get('project_id') == project['id']
                      and d.get('work_item_id') in linked_ids]
        evidence = []
        for wid in roots:
            w = idx[wid]; finish = _day(w.get('forecast_finish'))
            if not finish or w.get('status') in ('done', 'cancelled'): continue
            for aid in ancestors[wid]:
                baseline = _day(idx[aid].get('baseline_finish'))
                if baseline and finish > baseline:
                    evidence.append({'task_id': wid, 'target_id': aid, 'title': idx[aid].get('title', aid),
                                     'comparison_date': baseline.isoformat(), 'forecast_finish': finish.isoformat()})
            baseline = _day(project.get('baseline_finish'))
            if baseline and finish > baseline:
                evidence.append({'task_id': wid, 'target_id': project['id'], 'title': project.get('name', project['id']),
                                 'comparison_date': baseline.isoformat(), 'forecast_finish': finish.isoformat()})
        assessment = ('已有子项预测超出项目原计划完成日，需复核交付安排' if any(e['target_id'] == project['id'] for e in evidence)
                      else '影响范围待定位，不能将缺口自动分摊给全部任务' if not roots
                      else '当前未发现该任务的进度风险信号；保留交付关联供后续影响评估。' if not has_risk
                      else '存在逐级交付风险；是否影响项目完成日及天数待评估')
        return {'task_ids': roots, 'affected_leaf_ids': affected_leaves, 'dependency_leaf_ids': dependency_leaves,
                'ancestors': [node(x) for x in ancestor_ids],
                'successors': [node(x) for x in downstream], 'dependency_edges': edges,
                'downstream_ancestors': [node(x) for x in downstream_ancestors],
                'deliverables': deliveries, 'date_evidence': evidence,
                'project_assessment': assessment,
                'limitations': ['依赖路径是潜在传导范围，不等于所有任务已受阻；未计算关键路径或延期天数。', *dependency_gaps]}

    tasks = []
    for wid in leaf_ids:
        w = idx[wid]; owner = people.get(w.get('owner_id'), {})
        closed = w.get('status') == 'done'
        missing = [f for f in ('title', 'description', 'acceptance_criteria', 'owner_id',
                               'forecast_start', 'forecast_finish', 'acceptor_id') if not _present(w.get(f))]
        if w.get('owner_id') and not _available(owner, today): missing.append('owner_active')
        if owner and not _contactable(owner, config.get('mode')): missing.append('owner_contact')
        if w.get('acceptor_id') and not _available(people.get(w['acceptor_id']), today): missing.append('acceptor_active')
        start, finish = _day(w.get('forecast_start')), _day(w.get('forecast_finish'))
        if w.get('forecast_start') and not start: missing.append('forecast_start')
        if w.get('forecast_finish') and not finish: missing.append('forecast_finish')
        if start and finish and start > finish: missing.append('plan_date_order')
        feedback_at = None
        try:
            if w.get('progress_confirmed_at'): feedback_at = parse_datetime(w['progress_confirmed_at'])
        except (ValueError, TypeError): pass
        owner_feedback = bool(owner and w.get('progress_confirmed_by_id') == w.get('owner_id') and
                              _present(w.get('progress_note')) and feedback_at and feedback_at <= instant)
        feedback = {'state': 'owner_responded' if owner_feedback else 'pending',
                    'label': '负责人本人已反馈' if owner_feedback else '承接待确认',
                    'at': w.get('progress_confirmed_at'),
                    'reason': '现有进度反馈不等同于独立接单事件；需确认当前计划与责任。'}
        relevant = w.get('status') in ('in_progress', 'blocked', 'in_review') or (start and start <= local.date())
        stale = bool(not closed and relevant and (not owner_feedback or
                     (local.date() - feedback_at.astimezone(local.tzinfo).date()).days >= stale_days))
        state = 'closed' if closed else 'missing_data' if missing else 'awaiting_feedback' if not owner_feedback else 'ready'
        facts = operation_delay(w, today)
        signals = []
        if not closed:
            if missing: signals.append('资料未齐，不计为已就绪')
            if not owner_feedback: signals.append('尚无当前负责人本人有效反馈')
            if stale: signals.append('进展待维护或确认已过期')
            if w.get('status') == 'blocked': signals.append('任务受阻：' + (w.get('blocker_reason') or '原因待补'))
            if facts['delay_state'] == 'overdue': signals.append('已超过任务对照完成日')
            elif facts['delay_state'] == 'risk': signals.append('任务存在延期风险')
        tasks.append({'id': wid, 'title': w.get('title', wid), 'owner_id': w.get('owner_id'),
                      'owner_name': owner.get('name'), 'status': w.get('status'), 'source_url': w.get('source_url'),
                      'readiness': state, 'readiness_label': READINESS[state],
                      'missing_fields': [] if closed else list(dict.fromkeys(missing)), 'feedback': feedback,
                      'progress_stale': stale, 'signals': signals, 'delay_state': facts['delay_state'],
                      'forecast_finish': w.get('forecast_finish'), 'impact': impact_for([wid], has_risk=bool(signals))})

    def counts(rows):
        return {'total': len(rows), 'done': sum(t['status'] == 'done' for t in rows),
                'open': sum(t['status'] != 'done' for t in rows),
                **{key: sum(t['readiness'] == key for t in rows) for key in ('missing_data', 'awaiting_feedback', 'ready')},
                'blocked': sum(t['status'] == 'blocked' for t in rows),
                'overdue': sum(t['delay_state'] == 'overdue' for t in rows)}
    parents = []
    for wid, w in idx.items():
        if not children[wid] or w.get('status') == 'cancelled': continue
        leaves = [t for t in tasks if wid in ancestors[t['id']]]
        parents.append({'id': wid, 'title': w.get('title', wid), 'status': w.get('status'),
                        'owner_id': w.get('owner_id'), 'leaf_ids': [t['id'] for t in leaves],
                        'counts': counts(leaves),
                        'inconsistent_completion': w.get('status') == 'done' and any(t['status'] != 'done' for t in leaves)})
    from .constraints import resource_constraints
    constraints = []; operations_constraints = []
    for c in resource_constraints(data, now=now):
        if c.get('scope') == 'operations':
            impact = impact_for([])
            impact['project_assessment'] = '仅关联运维事项，尚无项目交付关联，不计入项目影响。'
            operations_constraints.append(dict(c, impact=impact))
        else:
            constraints.append(dict(c, impact=impact_for(c['work_item_ids'])))
    summary = dict(counts(tasks), resource_constraints=len(constraints),
                   operations_resource_constraints=len(operations_constraints))
    return {'project_id': project['id'], 'as_of': instant.isoformat(), 'timezone': zone,
            'summary': summary, 'tasks': tasks, 'parents': parents, 'constraints': constraints,
            'operations_constraints': operations_constraints,
            'limitations': ['任务完成占比按唯一有效叶子计数，资料待补仍在分母；不代表工时完成率。',
                            '本人进度反馈仅证明已反馈，不代替正式接单、最新重新指派或计划变更确认。', *dependency_gaps]}


def plan_workflow_followups(data, *, now, config=None):
    """Daily routine uses existing dispatch ledger; unresolved routing is explicit."""
    config = config or {}
    project, instant, local, zone = context(data, now)
    today = local.date().isoformat()
    working = workday_status(data, project['id'], today)
    if project.get('stage') in ('closed', 'paused') or not working.get('verified') or not working.get('is_workday'):
        return {'review': None, 'items': [], 'pending': [], 'reason': working['reason']}
    if not config.get('workflow_time', '10:00') <= local.strftime('%H:%M') < config.get('quiet_after', '18:00'):
        return {'review': None, 'items': [], 'pending': [], 'reason': 'outside_workflow_window'}
    review = workflow_review(data, now=instant, config=config)
    people = {p['id']: p for p in data.get('people', [])}; items = []; pending = []
    def add(kind, table, rid, title, owner_id, fields, reason, deadline=None, source_key=None):
        def routable(person):
            return _available(person, today) and (config.get('mode') == 'excel' or _contactable(person, config.get('mode', 'feishu')))
        person = people.get(owner_id, {})
        recipient = owner_id if routable(person) else project.get('pm_id')
        target = people.get(recipient, {})
        row = {'kind': kind, 'table': table, 'record_id': rid, 'title': title,
               'recipient_id': recipient, 'required_fields': fields, 'reason': reason,
               'deadline': deadline, 'source_key': source_key or rid, 'responsible_id': owner_id,
               'stop_condition': '发送前重新读取；关闭任务或补齐对应资料后停止'}
        if not routable(target):
            row.update(status='pending_route', reason=reason + '；负责人及PM接收身份待配置')
            pending.append(row)
        else: items.append(row)
        return row
    for t in review['tasks']:
        if t['status'] == 'done': continue
        fields = list(t['missing_fields'])
        if t['feedback']['state'] != 'owner_responded' or t['progress_stale']:
            fields += ['progress_note', 'progress_confirmed_at', 'progress_confirmed_by_id']
        if not fields and t['status'] != 'blocked' and t['delay_state'] not in ('overdue', 'risk'): continue
        if t['status'] == 'blocked': fields += ['blocker_reason', 'next_action']
        if t['delay_state'] in ('overdue', 'risk'): fields += ['delay_impact', 'remediation_action', 'remediation_expected']
        chain = ' → '.join([t['title'], *[n['title'] for n in t['impact']['ancestors']], project.get('name', project['id'])])
        schedule = ('；任务当前预计完成日：' + str(t['forecast_finish'])) if t.get('forecast_finish') else ''
        add('workflow_task', 'wbs', t['id'], t['title'], t['owner_id'], list(dict.fromkeys(fields)),
            '；'.join(t['signals']) + '。影响路径：' + chain + '。' + t['impact']['project_assessment'] +
            schedule + '；资料补填期限尚未约定，由PM确认。')
    for p in review['parents']:
        if p['inconsistent_completion']:
            add('workflow_parent_conflict', 'wbs', p['id'], p['title'], p['owner_id'], ['status'],
                '父项已标完成，但仍有 ' + str(p['counts']['open']) + ' 项有效子任务未完成；核对依据，不自动覆盖状态。')
    for c in review['constraints'] + review['operations_constraints']:
        # Human-resource facts need PM coordination; actual execution/decision
        # obligations retain their explicit responsibility, including employees.
        source = next((r for r in data.get(c['source_table'], []) if r['id'] == c['source_id']), {})
        owner = (source.get('decision_by_id') if c['kind'] == 'decision' else
                 source.get('owner_id') if c['kind'] in ('interface', 'register') else project.get('pm_id'))
        item = add('workflow_resource', c['source_table'], c['source_id'], c['title'], owner,
            c.get('required_fields', []), c['reason'] + '；' + c['impact']['project_assessment'] + '；下一步：' + c['next_action'],
            c.get('deadline'), c['id'])
        item['constraint'] = c
    return {'review': review, 'items': items, 'pending': pending, 'reason': None}
