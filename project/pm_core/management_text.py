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
