"""Human-facing schedule evidence and Chinese document paragraphs."""
from html import escape
from .schema import TABLES, ENUMS


def _text(value):
    return '待确认' if value is None or value == '' else str(value)


def _capacity_text(row, field):
    value = row.get(field)
    if value is None and not row.get('confirmation_required', True):
        return '未记录'
    return _text(value)


def _workflow_parts(data, workflow):
    """One human-facing evidence model for the document and standalone HTML."""
    if workflow is None:
        return None
    people = {r['id']: r.get('name', r['id']) for r in data.get('people', [])}
    works = {r['id']: r for r in data.get('wbs', [])}
    operations = {r['id']: r for r in data.get('operations', [])}
    labels = {f.name: f.label for meta in TABLES.values() for f in meta['fields']}
    labels.update({'available_hours': '净可用小时', 'remaining_hours': '剩余小时',
                   'confirmed_at': '确认时间', 'confirmation_status': '确认状态',
                   'overlap_start': '重叠开始', 'overlap_end': '重叠结束',
                   'person_id': '成员', 'backup_id': '交接人', 'work_item_ids': '关联任务',
                   'task_ids': '任务', 'source_status': '来源状态', 'allocation_mode': '投入方式',
                   'needed_hours': '所需工时', 'task_hours': '任务工时', 'record_exists': '已有资源记录',
                   'task_timing': '任务日期关系', 'capacity_status': '容量情况',
                   'owner_active': '负责人当前有效', 'owner_contact': '负责人可联系渠道',
                   'acceptor_active': '验收人当前有效', 'plan_date_order': '计划开始不晚于完成'})
    def named(identifier):
        row = works.get(identifier, {})
        return str(row.get('title') or identifier) + ' [' + str(identifier) + ']'
    def brief(value, limit=80):
        value = ' '.join(_text(value).split())
        return value if len(value) <= limit else value[:limit-1] + '…'
    def field_label(field, table):
        meta = next((f for f in TABLES.get(table, {}).get('fields', []) if f.name == field), None)
        return meta.label if meta else labels.get(field, field)
    def evidence(value, table=None, field=None):
        if isinstance(value, dict):
            return '；'.join((named(k) if k in works else field_label(k, table)) + '：' + evidence(v, table, k) for k, v in value.items()) or '待补依据'
        if isinstance(value, list):
            return '、'.join(evidence(v, table, field) for v in value) or '未登记'
        if isinstance(value, bool):
            return '是' if value else '否'
        if field in ('person_id', 'owner_id', 'backup_id', 'counterparty_id'):
            return people.get(value, _text(value))
        if field == 'work_item_id' and value in works:
            return named(value)
        if field == 'capacity_status':
            return {'available': '可安排', 'overallocated': '超出净可用量', 'buffer_conflict': '挤占缓冲', 'pending': '待确认'}.get(value, _text(value))
        if field in works:
            return {'overlap': '与当前计划日期重叠', 'timing_unknown': '剩余执行时间待确认'}.get(value, _text(value))
        schema_field = next((f for f in TABLES.get(table, {}).get('fields', []) if f.name == field), None)
        enum = schema_field.enum if schema_field else field if field in ENUMS else None
        if enum:
            return ENUMS[enum].get(value, _text(value))
        return _text(value)
    def impact_text(impact):
        impact = impact or {}
        tasks = '、'.join(named(x) for x in impact.get('task_ids', [])) or '待定位关联任务'
        def leaf_paths(identifiers):
            paths = []
            for identifier in identifiers:
                branch, seen = [], set()
                current = identifier
                while current in works and current not in seen:
                    branch.append(named(current)); seen.add(current)
                    current = works[current].get('parent_id')
                if current:
                    branch.append('层级关系待核对')
                branch.append('项目整体（日期影响另评估）')
                paths.append(' → '.join(branch))
            return paths
        paths = leaf_paths(impact.get('affected_leaf_ids', impact.get('task_ids', [])))
        lines = ['直接关联任务：' + tasks, '逐级路径：' + ('；'.join(paths) or '尚无可定位的未完成叶子路径')]
        if impact.get('successors'):
            lines.append('依赖后继：' + '、'.join(str(x.get('title') or x['id']) + ' [' + str(x['id']) + ']' for x in impact['successors']))
        if impact.get('dependency_leaf_ids'):
            lines.append('潜在后继叶子路径（另计）：' + '；'.join(leaf_paths(impact['dependency_leaf_ids'])))
        if impact.get('deliverables'):
            lines.append('关联交付：' + '、'.join(str(x.get('title') or x['id']) + '（' + ENUMS['deliverable_acceptance'].get(x.get('acceptance_status'), '验收待确认') + '）' for x in impact['deliverables']))
        for item in impact.get('date_evidence', []):
            source = named(item['task_id']) + ' → ' if item.get('task_id') else ''
            lines.append('日期对照：' + source + _text(item.get('title') or item.get('target_id')) + '；对照 ' + _text(item.get('comparison_date')) + '；预计 ' + _text(item.get('forecast_finish')))
        lines.append('项目交付：' + _text(impact.get('project_assessment')))
        lines.extend(str(x) for x in impact.get('limitations', []))
        return '；'.join(lines)
    groups = []
    for row in workflow.get('parents', []):
        c = row.get('counts', {})
        detail = ('叶子完成 ' + str(c.get('done', 0)) + '/' + str(c.get('total', len(row.get('leaf_ids', []))))
                  + '；资料待补 ' + str(c.get('missing_data', 0)) + '；承接反馈待确认 ' + str(c.get('awaiting_feedback', 0))
                  + '；受阻 ' + str(c.get('blocked', 0)) + '；延期 ' + str(c.get('overdue', 0)))
        if row.get('inconsistent_completion'):
            detail += '；状态冲突：父级已报完成，但仍有未完成子任务，需核对'
        constraints = workflow.get('constraints', [])
        leaves = set(row.get('leaf_ids', []))
        exposed, downstream, sources = set(), set(), set()
        for source in constraints:
            scope = source.get('impact') or {}
            direct = leaves.intersection(scope.get('affected_leaf_ids', []))
            dependent = leaves.intersection(scope.get('dependency_leaf_ids', []))
            exposed.update(direct); downstream.update(dependent)
            if direct or dependent:
                sources.add(source['id'])
        detail += ('；资源直接涉及叶子 ' + str(len(exposed)) + '；潜在后继叶子 ' + str(len(downstream))
                   + '；关联资源风险 ' + str(len(sources)) + ' 项（各自去重，不相加）')
        groups.append({'title': named(row['id']), 'detail': detail})
    tasks = []
    for row in workflow.get('tasks', []):
        gaps = '、'.join(labels.get(x, x) for x in row.get('missing_fields', []))
        feedback = row.get('feedback', {})
        state = row.get('readiness_label') or row.get('readiness') or '就绪待确认'
        details = state + '；' + _text(feedback.get('label')) + '；反馈时间 ' + _text(feedback.get('at'))
        if feedback.get('reason'):
            details += '；' + str(feedback['reason'])
        signals = '；'.join(row.get('signals', []))
        action = ('需补：' + gaps if gaps else '基本资料已登记') + ('；' + signals if signals else '')
        if row.get('readiness') == 'closed':
            action = '已完成任务不再要求补填；历史资料仍按来源核查'
        if row.get('progress_stale'):
            action += '；需更新近期进展，不据此认定没有工作'
        tasks.append({'id': row['id'], 'title': named(row['id']), 'badge': state,
                      'owner': people.get(row.get('owner_id'), '待指定'), 'source_url': row.get('source_url'),
                      'fields': [('就绪与本人反馈', details), ('逐级影响', impact_text(row.get('impact'))), ('待维护事项', action)]})
    kinds = {'leave': '休假与交接', 'capacity': '人员容量', 'capacity_unknown': '人员容量待核实',
             'daily_availability': '每日可用安排', 'interface': '接口就绪', 'decision': '决策落实', 'register': '物资与其他条件 / 已登记事项'}
    resources, operations_resources = [], []
    affected = set()
    for row in workflow.get('constraints', []) + workflow.get('operations_constraints', []):
        impact = row.get('impact') or {}
        operational = row.get('scope') == 'operations'
        if not operational:
            affected.update(impact.get('affected_leaf_ids', []))
        mapping = row.get('assessment') == 'needs_mapping' or not row.get('work_item_ids')
        owner = people.get(row.get('owner_id'), '待指定')
        uncertainty = '；'.join(row.get('limitations', []))
        scope_text = '关联运维事项：' + '、'.join(str(operations.get(x, {}).get('title') or x) + ' [' + str(x) + ']' for x in row.get('operation_ids', [])) + '；尚无项目交付关联，不计入项目影响。'
        short_impact = ('仅关联运维，不计项目影响' if operational else '关联任务待定位' if mapping else
                        '直接涉及 ' + str(len(set(impact.get('affected_leaf_ids', [])))) + ' 个叶子；潜在后继 '
                        + str(len(set(impact.get('dependency_leaf_ids', [])))) + ' 个')
        if impact.get('date_evidence'):
            short_impact += '；有日期越界依据'
        target = operations_resources if operational else resources
        target.append({'id': row['id'], 'title': _text(row.get('title')), 'owner': owner,
            'badge': ('运维资源风险' if operational else '待定位风险' if mapping else '待确认风险' if row.get('assessment') == 'pending_confirmation' else '资源未落实风险'),
            'source_label': kinds.get(row.get('kind'), '资源条件') + ' · ' + TABLES.get(row.get('source_table'), {}).get('sheet', _text(row.get('source_table'))) + '/' + _text(row.get('source_id')),
            'source_url': row.get('source_url'),
            'summary': {'gap': brief(row.get('reason')), 'impact': short_impact,
                        'deadline': _text(row.get('deadline')), 'next_action': brief(row.get('next_action'))},
            'fields': [('缺口与依据', _text(row.get('reason')) + '；依据：' + evidence(row.get('evidence'), row.get('source_table'))),
                       ('影响范围' if operational else '任务 → 上级 → 项目', scope_text if operational else impact_text(impact)),
                       ('落实责任与下一步', '责任：' + owner + '；最晚落实/核对时点：' + _text(row.get('deadline')) + ('（' + str(row['deadline_basis']) + '）' if row.get('deadline_basis') else '') + '；建议下一步：' + _text(row.get('next_action')) + ('；待评估：' + uncertainty if uncertainty else ''))]})
    summary = workflow.get('summary', {})
    coverage = ('只评估已登记资源及明确任务关系；未登记的资源需求、任务工时、替补落实和接口使用时点仍需补齐。'
                '资源风险条数与受影响叶子数分别去重；未就绪任务仍计入项目待办。本人反馈记录不是正式签收或身份认证。')
    return {'summary': summary, 'groups': groups, 'tasks': tasks, 'resources': resources, 'operations_resources': operations_resources,
            'affected': len(affected), 'coverage': coverage, 'limitations': workflow.get('limitations', [])}


def workflow_paragraphs(data, workflow):
    parts = _workflow_parts(data, workflow)
    if parts is None:
        return ['任务就绪及资源影响尚未评估，不能据此断言资源已落实。']
    s = parts['summary']
    out = ['每日巡检与逐级影响',
           '全项目有效叶子 ' + str(s.get('total', 0)) + '；已完成 ' + str(s.get('done', 0)) + '；未结 ' + str(s.get('open', 0)) + '；资料待补 ' + str(s.get('missing_data', 0)) + '；承接反馈待确认 ' + str(s.get('awaiting_feedback', 0)) + '；可执行/跟进中 ' + str(s.get('ready', 0)) + '。',
           '延期风险与资源落实：' + str(len(parts['resources'])) + ' 项资源风险，关联 ' + str(parts['affected']) + ' 个去重叶子任务。已发生延期仍见独立的延期说明与补救措施。']
    out.extend('父级：' + r['title'] + '；' + r['detail'] for r in parts['groups'])
    for r in parts['resources'] + parts['operations_resources'] + parts['tasks']:
        out.append(r['title'] + '；' + r['badge'] + '；负责人：' + r['owner'] + '。')
        if r.get('source_label'):
            out.append('来源：' + r['source_label'] + ('；' + r['source_url'] if r.get('source_url') else '；待补来源链接'))
        elif r.get('source_url'):
            out.append('任务来源：' + r['source_url'])
        out.extend(label + '：' + text for label, text in r['fields'])
    if not parts['resources']:
        out.append('当前已登记资料中未发现未落实资源，未登记需求不代表已经落实。')
    return out + [parts['coverage']] + list(parts['limitations'])


def render_workflow_review(data, workflow):
    from .project_report import _kpi, _link
    parts = _workflow_parts(data, workflow)
    if parts is None:
        return '<p class="note">任务就绪及资源影响尚未评估。</p>'
    h = lambda value: escape(str(value), quote=True)
    s = parts['summary']
    out = ['<section data-workflow-review><h3 class="block-title">每日巡检与逐级影响</h3><div class="kpi-row">',
           _kpi('全项目叶子完成', str(s.get('done', 0))+'/'+str(s.get('total', 0)), tone='neutral', sub='含未就绪任务，取消项不计'),
           _kpi('资料待补', s.get('missing_data', 0), tone='warning', sub='计划、明细、责任与完成标准'),
           _kpi('承接反馈待确认', s.get('awaiting_feedback', 0), tone='warning', sub='有负责人不等于已有本人反馈'),
           _kpi('可执行 / 跟进中', s.get('ready', 0), tone='info', sub='就绪不等于无阻碍'),
           _kpi('资源未落实风险', len(parts['resources']), tone='warning', sub='含关联待定位事项'),
           _kpi('资源影响叶子', parts['affected'], tone='warning', sub='去重任务数，不等于延期条数'), '</div>']
    out.append('<h3 class="block-title">延期风险与资源落实</h3><p class="note">未落实资源可能影响交付；已发生延期见上方独立审议。措施仍需负责人落实或有权人员决定。</p>')
    def card(row, attr):
        main = '<div class="dc-main"><b>'+h(row['title'])+'</b><small>'+h(row['owner'])+'</small><span class="chip warn">'+h(row['badge'])+'</span>'
        if row.get('source_label') and not row.get('summary'):
            main += '<small>来源 '+h(row['source_label'])+'</small>'
        if row.get('source_url'):
            main += '<small>'+_link(row['source_url'], '查看来源')+'</small>'
        main += '</div>'
        fields = ''.join('<div class="dc-field"><label>'+h(label)+'</label><div class="val">'+h(value)+'</div></div>' for label,value in row['fields'])
        if row.get('summary'):
            summary = row['summary']
            summary_fields = ('<div class="dc-field"><label>资源缺口</label><div class="val">'+h(summary['gap'])+'</div><small>'+h(summary['impact'])+'</small></div>'
                              '<div class="dc-field"><label>DDL</label><div class="val">'+h(summary['deadline'])+'</div></div>'
                              '<div class="dc-field"><label>下一步</label><div class="val">'+h(summary['next_action'])+'</div></div>')
            details = ('<div class="dc-field" style="grid-column:1/-1"><details data-resource-evidence><summary>证据与完整影响链</summary>'
                       '<p class="note">来源：'+h(row['source_label'])+'</p>'+fields+'</details></div>')
            return '<article class="delay-card risk" '+attr+'="'+h(row['id'])+'">'+main+summary_fields+details+'</article>'
        return '<article class="delay-card risk" '+attr+'="'+h(row['id'])+'">'+main+fields+'</article>'
    out.append('<div class="delay-list">'+(''.join(card(r,'data-constraint-id') for r in parts['resources']) or '<div class="empty">当前已登记资料中未发现未落实资源，未登记需求不代表已经落实。</div>')+'</div>')
    if parts['operations_resources']:
        out.append('<details><summary>运维资源风险（'+str(len(parts['operations_resources']))+'；未关联项目交付）</summary><p class="note">这些条件仅关联运维事项，不计入项目资源风险和受影响叶子读数。</p><div class="delay-list">'+''.join(card(r, 'data-operations-constraint-id') for r in parts['operations_resources'])+'</div></details>')
    out.append('<details><summary>父级任务进展与子任务影响</summary><div class="table-scroll"><table><thead><tr><th>父级工作包 / 任务</th><th>去重叶子进展与缺口</th></tr></thead><tbody>'
               + ''.join('<tr><td>'+h(r['title'])+'</td><td>'+h(r['detail'])+'</td></tr>' for r in parts['groups'])+'</tbody></table></div></details>')
    out.append('<details><summary>最小子任务 · 资料、本人反馈与逐级影响（'+str(len(parts['tasks']))+'）</summary><div class="delay-list">'+''.join(card(r,'data-workflow-task-id') for r in parts['tasks'])+'</div></details>')
    out.append('<details class="method-note"><summary>覆盖范围与影响口径</summary><p>'+h(parts['coverage'])+'</p>'+''.join('<p>'+h(x)+'</p>' for x in parts['limitations'])+'</details></section>')
    return ''.join(out)


def _audit_parts(audit):
    """Single source for schedule-audit wording, shared by paragraphs and HTML."""
    if audit is None:
        return {'title': '日期变更审议', 'lead': '历史覆盖未知：当前材料没有日期变更事件或先前快照，不能判断是否曾改期。', 'unknown_history': True,
                'limitations': [], 'signals': [], 'commitments': [], 'alert_count': None}
    labels = {f.name: f.label for meta in TABLES.values() for f in meta['fields']}
    signals = []
    for signal in audit.get('signals', []):
        priority = {'high': '优先核对', 'medium': '需要核对', 'info': '变更留档'}[signal['severity']]
        comparison = signal['comparison']
        evidence = signal.get('evidence', {})
        approval = signal['approval']
        disposition = {'matched_recorded_review': '已完成事后具名复核（不等于事前批准）', 'matched_recorded_approval': '已匹配事前批准', 'approval_timing_unverified': '批准时间与实际修改先后待核实', 'reference_only': '仅有变更引用，尚未核对本次范围'}.get(approval.get('status'), '尚无匹配的批准或复核依据')
        review = None
        if approval.get('status') == 'matched_recorded_review':
            original = signal.get('original_classification', {})
            review = '复核人：' + _text(approval.get('reviewed_by')) + '；复核时间：' + _text(approval.get('reviewed_at')) + '；原提醒级别：' + {'high': '优先核对', 'medium': '需要核对', 'info': '留档'}.get(original.get('severity'), '待确认') + '。'
        signals.append({
            'severity': signal['severity'], 'priority': priority, 'title': signal['title'], 'record_id': signal['record_id'],
            'changes': [(labels.get(c['field'], c['field']), _text(c['before']), _text(c['after'])) for c in signal['changed_fields']],
            'comparison': '同一对照时点的完成偏差：' + _text(comparison['before_late_days']) + ' → ' + _text(comparison['after_late_days']) + ' 自然日；开始偏差：' + _text(comparison['before_late_start_days']) + ' → ' + _text(comparison['after_late_start_days']) + ' 自然日。',
            'deviations': [('同一对照时点完成偏差（自然日）', _text(comparison['before_late_days']), _text(comparison['after_late_days'])),
                           ('同一对照时点开始偏差（自然日）', _text(comparison['before_late_start_days']), _text(comparison['after_late_start_days']))],
            'source': '来源操作者标签：' + _text(signal.get('actor')) + '；实际变更时间：' + _text(signal.get('recorded_at')) + '；观察期间：' + _text(evidence.get('previous_observed_at')) + ' — ' + _text(evidence.get('observed_at')) + '。',
            'disposition': '依据：' + _text(evidence.get('source')) + '；处理状态：' + disposition + '；记录：' + _text(approval.get('evidence') or approval.get('note')),
            'review': review,
            'first_reviews': ['首次补录核对：' + labels.get(fact['field'], fact['field']) + '填为 ' + _text(fact['claimed']) + '；较晚依据时间 ' + _text(fact['evidence_at']) + '；当时状态 ' + ENUMS['work_status'].get(fact.get('prior_status'), _text(fact.get('prior_status'))) + '。' + fact['note'] for fact in signal.get('first_actual_review', [])],
            'next_action': signal['next_action']})
    commitments = []
    for record in audit.get('original_commitments', []):
        if any(record.get('original_' + f) != record.get('current_' + f) for f in ('baseline_start', 'baseline_finish', 'due_date') if 'original_' + f in record):
            commitments.append('保留首次观察承诺：' + _text(record.get('title') or record['record_id']) + '；' + '；'.join(labels.get(f, f) + ' ' + _text(record.get('original_' + f)) + ' → 当前 ' + _text(record.get('current_' + f)) for f in ('baseline_start', 'baseline_finish', 'due_date') if 'original_' + f in record))
    return {'title': '日期变更审议', 'lead': '需核对日期变更 ' + str(audit['alert_count']) + ' 项；以下是事实变化，不能据此推断修改意图。', 'unknown_history': False,
            'limitations': [note.replace('observed_at', '观察时点') for note in audit['coverage'].get('limitations', [])],
            'signals': signals, 'commitments': commitments, 'alert_count': audit['alert_count']}


def schedule_paragraphs(audit):
    parts = _audit_parts(audit)
    out = [parts['title'], parts['lead']]
    out.extend(parts['limitations'])
    for s in parts['signals']:
        out.append(s['priority'] + '：' + s['title'] + '（' + s['record_id'] + '）')
        out.extend(label + '：' + before + ' → ' + after for label, before, after in s['changes'])
        out.extend([s['comparison'], s['source'], s['disposition']])
        if s['review']:
            out.append(s['review'])
        out.extend(s['first_reviews'])
        out.append(s['next_action'])
    out.extend(parts['commitments'])
    return out


def render_schedule_audit(audit):
    e = lambda value: escape(str(value), quote=True)
    parts = _audit_parts(audit)
    if parts['unknown_history']:
        badge = '<span class="audit-count">历史覆盖未知</span>'
    else:
        badge = '<span class="audit-count' + (' alert' if parts['alert_count'] else '') + '">需核对 ' + e(parts['alert_count']) + ' 项</span>'
    out = ['<section class="schedule-audit"><h3>' + e(parts['title']) + badge + '</h3><p class="audit-lead">' + e(parts['lead']) + '</p>']
    for s in parts['signals']:
        row = lambda label, before, after, cls='': '<div class="field-change' + cls + '"><span class="lbl">' + e(label) + '</span><span class="before">' + e(before) + '</span><span>→</span><span class="after">' + e(after) + '</span></div>'
        changes = ''.join(row(*c) for c in s['changes'])
        deviations = ''.join(row(label, before, after, ' deviation' + (' reduced' if before.isdigit() and after.isdigit() and int(after) < int(before) else ''))
                             for label, before, after in s['deviations'])
        out.append('<article class="audit-signal sev-' + e(s['severity']) + '"><div class="sig-head"><span class="sev ' + e(s['severity']) + '">' + e(s['priority']) + '</span><b>' + e(s['title']) + '</b><small>' + e(s['record_id']) + '</small></div>'
                   + '<div class="change-grid">' + changes + deviations + '</div><p>' + e(s['source']) + '</p><p>' + e(s['disposition']) + '</p>'
                   + ('<p>' + e(s['review']) + '</p>' if s['review'] else '') + ''.join('<p>' + e(x) + '</p>' for x in s['first_reviews'])
                   + '<p class="next">' + e(s['next_action']) + '</p></article>')
    out.extend('<p class="note">' + e(x) + '</p>' for x in parts['commitments'])
    if parts['limitations']:
        out.append('<details class="method-note"><summary>覆盖范围与局限</summary><p>' + '<br>'.join(e(x) for x in parts['limitations']) + '</p></details>')
    out.append('</section>')
    return ''.join(out)


def export_schedule_audit_report(audit, path, *, title='日期变更审议', note=None):
    """Standalone date-change review page with the shared report design."""
    from pathlib import Path
    from .project_report import REPORT_CSS
    e = lambda value: escape(str(value), quote=True)
    coverage = (audit or {}).get('coverage', {})
    observed = [s.get('evidence', {}) for s in (audit or {}).get('signals', []) if s.get('evidence', {}).get('observed_at')]
    period = _text(observed[0].get('previous_observed_at')) + ' — ' + _text(observed[0].get('observed_at')) if observed else None
    body = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + e(title) + '</title><style>' + REPORT_CSS + '</style></head><body class="standalone">'
            '<header><div class="top"><div><div class="brand-label">pmmaster / SCHEDULE AUDIT</div><strong>' + e(title) + '</strong>' + ('<small>' + e(note) + '</small>' if note else '') + '</div>'
            + '<div class="header-meta">' + ('<small>观察期间 ' + e(period) + '</small>' if period else '') + ('<small>来源 ' + e(coverage['source_id']) + '</small>' if coverage.get('source_id') else '') + '</div></div></header>'
            '<main><section class="page active">' + render_schedule_audit(audit) + '</section></main></body></html>')
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding='utf-8')
    return {'ok': True, 'path': str(out)}


def management_paragraphs(data, review):
    people = {r['id']: r['name'] for r in data.get('people', [])}
    person = lambda value: people.get(value, '待指定')
    project = next(r for r in data['projects'] if r['id'] == review['project_id'])
    out = [project['name'] + ' · ' + ('月度管理复核' if review['period'] == 'month' else '周进度审议'),
           '统计期间：' + review['period_start'] + ' — ' + review['period_end'] + '；数据截止：' + review['as_of'] + '。',
           '项目阶段：' + ENUMS['lifecycle'].get(review['stage'], review['stage']) + '；当前预计完成：' + _text(project.get('forecast_finish')) + '。',
           '进度概览']
    for table, label in (('wbs', '项目交付'), ('operations', '运维与优化')):
        counts = review['counts'][table]
        out.append(label + '：有效事项 ' + str(counts['total']) + '；已完成 ' + str(counts['done']) + '；未结 ' + str(counts['open']) + '；本期完成 ' + str(len(counts['completed_in_period'])) + '。完成条数不是工作量完成率。')
    out.append('本周已确认成果')
    outcome_labels = {'accepted_on_time': '按期完成', 'accepted_late': '延期完成', 'overdue': '逾期待完成', 'pending': '尚待结果', 'cancelled': '已取消，保留原承诺', 'missing': '当前记录缺失'}
    for row in review.get('commitments', []):
        current = row.get('current_result') or {}
        out.append(_text(row.get('outcome')) + '；负责人：' + person(row.get('owner_id')) + '；承诺日期：' + _text(row.get('comparison_due')) + '；结果：' + outcome_labels[row['outcome_state']] + '；验证：' + _text(row.get('verification')) + '；依据：' + _text(current.get('result_evidence')) + '。')
    if not review.get('commitments'):
        out.append('尚无本周已确认成果记录，不能推算承诺兑现率。')
    out.append('延期说明与补救措施')
    for r in review['delays']:
        out.extend([('项目：' if r['table'] == 'wbs' else '运维：') + r['title'] + '；负责人：' + person(r['owner_id']) + '。',
            '状态：' + r['delay_state_label'] + '；对照日期：' + _text(r['commitment_finish']) + '；最新预计/实际：' + _text(r.get('actual_finish') or r.get('forecast_finish')) + '。',
            '说明：' + _text(r['delay_risk_reason'] if r['delay_state'] == 'risk' else r['delay_reason']) + '；影响：' + _text(r.get('delay_impact')) + '。',
            '措施：' + _text(r['remediation_action']) + '；行动负责人：' + person(r['remediation_owner_id']) + '；期限：' + _text(r['remediation_expected']) + '；落实：' + _text(r.get('remediation_result')) + '。'])
        decision = r.get('decision') or {}
        out.append('审议问题：' + _text(decision.get('title')) + '；方案：' + _text(decision.get('proposal')) + '；决定人：' + person(decision.get('decision_by_id')) + '；决定：' + _text(decision.get('decision')) + '；依据：' + _text(decision.get('closure_evidence')) + '。')
    if not review['delays']:
        out.append('当前未登记偏差，不能据此断言没有风险。')
    out.extend(workflow_paragraphs(data, review.get('workflow')))
    out.extend(schedule_paragraphs(review.get('schedule_audit')))
    out.append('人员资源与可用安排')
    for r in review['resources'].get('people', []):
        note = '员工免确认；保留既有资源事实。' if r.get('confirmation_scope') == 'employee_excluded' else '成员类型待核实，暂不发起资源确认。' if r.get('confirmation_scope') == 'identity_unknown' else ''
        out.append(person(r['person_id']) + '：净可用 ' + _capacity_text(r, 'net_available_hours') + ' 小时；项目 ' + _capacity_text(r, 'project_hours') + '、运维 ' + _capacity_text(r, 'operations_hours') + '、缓冲 ' + _capacity_text(r, 'buffer_hours') + '；可再安排 ' + _capacity_text(r, 'remaining_hours') + ' 小时。' + note)
    daily = review.get('daily_status', {})
    out.append('今日尚需确认：' + ('、'.join(person(r['person_id']) for r in daily.get('eligible', [])) or '无已核实的待回复名单') + '；未回复不等于缺勤。')
    for r in review.get('leave_plans', []):
        out.append('休假安排：' + person(r['person_id']) + '，' + r['start_date'] + ' 至 ' + r['end_date'] + '，' + ENUMS['plan_status'].get(r['status'], r['status']) + '；交接：' + person(r.get('backup_id')) + '；影响：' + _text(r.get('impact')) + '。')
    if review['period'] == 'month':
        out.append('未来 ' + str(review['lookahead_weeks']) + ' 周资源展望')
        for week in review['resource_forecast']:
            out.append(week['week_start'] + ' — ' + week['week_end'])
            out.extend(person(r['person_id']) + '：净可用 ' + _capacity_text(r, 'net_available_hours') + ' 小时；可再安排 ' + _capacity_text(r, 'remaining_hours') + ' 小时；' + r['availability_note'] for r in week['people'])
    out.append('阶段检查与待决定事项')
    out.extend(r['name'] + '：' + ('证据已登记' if r['present'] else '待补证据') for r in review['stage_checks'])
    out.append(review['stage_decision'])
    out.extend(review['limitations'])
    return out
