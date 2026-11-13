"""Conditional questions for project WBS leaves; no inferred work or schedule edits."""
from datetime import date

from .store import parse_datetime


def _present(value):
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def task_scenarios(row, *, today, instant, feedback, missing_fields, delay_state, stale):
    """The caller selects project leaves; blank future actual dates are normal."""
    if row.get('status') == 'cancelled':
        return []
    result = []
    status = row.get('status')
    start, finish = _date(row.get('forecast_start')), _date(row.get('forecast_finish'))
    actual_start, actual_finish = _date(row.get('actual_start')), _date(row.get('actual_finish'))
    due = _date(row.get('baseline_finish')) or finish

    def add(code, title, category, fields, prompts, facts, *, assessment='information_gap', role='owner'):
        result.append({'id': row['id'] + ':' + code, 'code': code, 'title': title,
                       'category': category, 'role': role, 'required_fields': list(fields),
                       'questions': [{'id': code + ':' + key, 'prompt': prompt} for key, prompt in prompts],
                       'facts': {f: row.get(f) for f in facts}, 'assessment': assessment,
                       'stop_condition': '重新读取；本情景条件消失后停止本项追问，回复不自动代表工作完成。'})

    if status == 'done':
        missing = [f for f in ('actual_start', 'actual_finish', 'completion_evidence') if not _present(row.get(f))]
        if missing:
            add('completion_record_gap', '已报完成但事实记录未齐', 'completion_audit', missing,
                [('facts', '是否确已完成？请补真实起止日期及可核验成果；如状态误报，请说明。')],
                ['status', *missing])
        return result

    if not start:
        add('plan_start_missing', '当前开工计划缺失', 'maintenance', ['forecast_start'],
            [('start', '何时具备开工条件？请给预计开始日及尚缺前置条件。')], ['forecast_start', 'waiting_on'])
    if not finish:
        add('plan_finish_missing', '当前完成计划缺失', 'maintenance', ['forecast_finish'],
            [('estimate', '难点、剩余工作量及估算不确定性是什么？'),
             ('split_support', '是否需要拆分可验收子任务、增加资源或申请更多时间？'),
             ('finish', '请提出预计完成日及依据，说明依赖条件。')], ['forecast_finish', 'description', 'waiting_on'])
    if start and finish and start > finish:
        add('plan_order_conflict', '计划起止顺序不一致', 'maintenance', ['forecast_start', 'forecast_finish'],
            [('dates', '开始晚于完成，请核对计划和依赖依据，不覆盖原基线。')], ['forecast_start', 'forecast_finish'])
    if not actual_start and ((start and start <= today) or status in ('in_progress', 'blocked', 'in_review')):
        add('start_unconfirmed', '开工事实待确认', 'maintenance', ['actual_start', 'progress_note'],
            [('started', '已开工但漏填，还是尚未开始？已开工请补真实开始日。'),
             ('risk', '如尚未开始，原因和预计开工日是什么，是否影响交付？')],
            ['status', 'forecast_start', 'actual_start'], assessment='needs_confirmation')
    if actual_start and status in ('intake', 'ready'):
        add('started_state_conflict', '已有实际开始但状态未同步', 'maintenance', ['status', 'progress_note'],
            [('state', '实际开始已登记，当前是否在推进？请核对状态或日期依据。')], ['actual_start', 'status'])
    if actual_finish:
        add('finished_state_conflict', '实际完成与未关闭状态待核对', 'acceptance',
            ['status', 'completion_evidence'], [('acceptance', '是已做完待验收，还是日期误填？请说明验收结论及成果依据。')],
            ['actual_finish', 'status', 'completion_evidence'], role='acceptor')
    elif due and due <= today:
        overdue = due < today
        add('finish_overdue' if overdue else 'finish_today', '完成日期已过，结果待核对' if overdue else '今日到期，结果待确认',
            'delay_review' if overdue else 'delivery_check', ['actual_finish', 'progress_note', 'forecast_finish'],
            [('result', '已经完成但漏填，还是仍未完成？请提供成果或剩余工作。'),
             ('forecast', '若仍未完成，请说明原因、新的预计完成日及对交付的影响。' if overdue else
                          '能否按期交付？如有风险，请说明原因和当前预计完成日。')],
            ['baseline_finish', 'forecast_finish', 'actual_finish', 'status'],
            assessment='overdue_status' if overdue else 'due_today')
    if status == 'in_review' and not actual_finish:
        add('acceptance_pending', '待验收结果与责任确认', 'acceptance',
            ['completion_evidence', 'acceptor_id', 'progress_note'],
            [('review', '验收材料是否齐全、谁确认结果、何时反馈？卡在什么条件？')],
            ['status', 'acceptor_id', 'completion_evidence'], role='acceptor')
    definition = [f for f in ('title', 'description', 'acceptance_criteria', 'acceptor_id', 'acceptor_active') if f in missing_fields]
    if definition:
        add('definition_missing', '工作范围与完成标准待明确', 'maintenance', definition,
            [('definition', '请明确具体交付、完成标准及验收人；过大的任务是否需拆分？')], definition)
    if any(f in missing_fields for f in ('owner_id', 'owner_active', 'owner_contact')):
        add('owner_unavailable', '责任归属或联系渠道未落实', 'maintenance',
            [f for f in missing_fields if f in ('owner_id', 'owner_active', 'owner_contact')],
            [('owner', '请确认实际负责人、可联络身份和交接安排。')], ['owner_id'])
    elif feedback.get('state') != 'owner_responded':
        add('owner_response_missing', '当前负责人承接反馈待确认', 'maintenance',
            ['progress_note', 'progress_confirmed_at', 'progress_confirmed_by_id'],
            [('ack', '是否已收到并理解任务？能否承接，或需何人协调？')], ['owner_id', 'progress_confirmed_at'])
    elif stale:
        add('progress_stale', '进度反馈已过期', 'maintenance',
            ['progress_note', 'progress_confirmed_at', 'progress_confirmed_by_id'],
            [('progress', '上次确认后完成了什么，还剩什么，是否卡住或需支持？')], ['progress_note', 'progress_confirmed_at'])
    if not _present(row.get('priority')):
        add('priority_missing', '任务优先级尚未明确', 'maintenance', ['priority'],
            [('priority', '请说明紧迫性和影响，建议P0–P3优先级并由PM协调排序。')], ['priority', 'impact', 'urgency'])
    elif row.get('priority') == 'P0' and not _present(row.get('priority_reason')):
        add('priority_reason_missing', '紧急任务缺少依据', 'maintenance', ['priority_reason'],
            [('reason', 'P0的紧急依据和影响是什么，需挤占哪些工作？')], ['priority', 'priority_reason'])

    next_due = None
    try:
        next_due = parse_datetime(row.get('next_due_at')) if row.get('next_due_at') else None
    except (ValueError, TypeError):
        pass
    if status == 'blocked' or (row.get('waiting_on') and (not next_due or next_due <= instant)):
        add('blocked_recovery' if status == 'blocked' else 'waiting_checkpoint',
            '阻碍解除与恢复计划' if status == 'blocked' else '等待条件缺少有效检查点', 'next_action',
            ['blocker_reason', 'waiting_on', 'next_action', 'next_due_at'] if status == 'blocked' else ['waiting_on', 'next_action', 'next_due_at'],
            [('blocker', '卡在什么条件、谁能解除，需要什么支持？'),
             ('continuation', '当前继续做还是等待？如等待，恢复条件/预计时间或下次核对时间是什么？')],
            ['status', 'blocker_reason', 'waiting_on', 'next_action', 'next_due_at'], assessment='risk', role='coordinator')
    if delay_state in ('risk', 'overdue'):
        reason_field = 'delay_reason' if delay_state == 'overdue' else 'delay_risk_reason'
        fields = [reason_field, 'delay_impact', 'remediation_action', 'remediation_owner_id', 'remediation_expected']
        missing = [f for f in fields if not _present(row.get(f))]
        if missing:
            add('delay_recovery_gap', '延期风险说明或补救安排未完整', 'delay_review', missing,
                [('cause_impact', '延期/风险原因是什么，影响哪些上级任务、交付及项目目标？'),
                 ('remedy', '解决措施、落实人和预计补救完成日是什么？'),
                 ('continuation', '现在继续推进还是等待？如等待请明确恢复条件/时间或核对节点。')],
                ['delay_risk', *fields], assessment='risk')
    remedy = _date(row.get('remediation_expected'))
    if remedy and remedy <= today and not (row.get('remediation_actual') and row.get('remediation_result')):
        add('remediation_result_pending', '补救措施到期结果待确认', 'remediation',
            ['remediation_actual', 'remediation_result'],
            [('result', '补救措施是否已落实？请给验证结果；未落实说明原因及新的建议安排。')],
            ['remediation_action', 'remediation_expected', 'remediation_actual', 'remediation_result'],
            assessment='due_today' if remedy == today else 'risk', role='remediation_owner')
    return result
