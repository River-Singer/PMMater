"""Pure, bounded project-group actions shared by preview and live dispatch.

Source text stays a plain-text post element; only FeishuClient creates mentions.
Dates describe their recorded meaning, never an inferred response commitment.
"""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
import re

from .schema import TABLES
from .store import parse_datetime


TYPES = {'decision': '决策', 'execution': '执行', 'filling': '补填', 'coordination': '协调'}
LABELS = {f.name: f.label for meta in TABLES.values() for f in meta['fields']}
LABELS.update({'weekly_commitments.outcome': '本周可验证成果', 'weekly_commitments.due_date': '周成果期限',
    'weekly_commitments.verification': '周成果验证方式', 'owner_active': '有效负责人',
    'owner_contact': '负责人联系方式或飞书身份', 'acceptor_active': '有效验收责任人',
    'plan_date_order': '当前计划起止顺序', 'work_item_ids': '受影响任务关联', 'capacity': '容量与任务安排依据',
    'coverage': '替补与交接安排', 'person_identity': '人员身份', 'task_effort': '任务所需工时',
    'backup_coverage': '替补承担范围', 'resource_delivery': '资源落实依据',
    'resource_weeks.net_available_hours': '本周已核实净可用容量',
    'plan_date': '计划会日期', 'plan_meeting_id': '计划会实例', 'review_date': '总结会日期',
    'review_meeting_id': '总结会实例'})


def _deadline(value, day, zone):
    if not value: return None
    try:
        value = str(value)
        if re.fullmatch(r'\d{2}:\d{2}', value):
            time.fromisoformat(value)
            return day + ' ' + value + ' ' + zone
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            date.fromisoformat(value)
            return value
        parse_datetime(value)
        return value
    except (TypeError, ValueError):
        return None


def route_item(data, item, *, day, require_identity=False):
    """Resolve real responsibility before any recipient filtering; never infer authority."""
    from .workflow import _available, _contactable
    people = {p['id']: p for p in data.get('people', [])}
    project = next(iter(data.get('projects', [])), {})
    row = next((r for r in data.get(item.get('table'), []) if r['id'] == item.get('record_id')), {})
    decision = item.get('kind') == 'decision_due' or (item.get('kind') == 'workflow_resource' and
               item.get('constraint', {}).get('kind') == 'decision')
    responsible = row.get('decision_by_id') if decision else item.get('responsible_id', item.get('recipient_id'))
    if item.get('kind') == 'workflow_resource' and not decision:
        responsible = row.get('owner_id') if item.get('constraint', {}).get('kind') in ('interface', 'register') else project.get('pm_id')
    def usable(pid):
        p = people.get(pid, {})
        return _available(p, day) and (not require_identity or _contactable(p, 'feishu'))
    recipient = responsible if usable(responsible) else project.get('pm_id')
    routed = dict(item, responsible_id=responsible, recipient_id=recipient,
                  routing_note='' if responsible == recipient and usable(recipient) else
                  '请PM协调指定可联络的' + ('决定人；本提醒不授予决策权。' if decision else '责任人，并确认处理期限。'))
    if not usable(recipient): routed['routing_status'] = 'pending_route'
    return routed


def project_group_actions(data, plan, config, *, now=None):
    """Normalize and merge the same source/obligation, retaining distinct commitments."""
    zone = config['timezone']; day = plan['date']; merged = {}; pending = []
    cutoff_time = time.fromisoformat(config.get('response_cutoff', '17:00'))
    if cutoff_time.tzinfo is not None:
        raise ValueError('response_cutoff must be a project-local time without offset')
    response_cutoff = datetime.combine(date.fromisoformat(day), cutoff_time, ZoneInfo(zone))
    observed = now or plan.get('as_of') or (plan.get('workflow') or {}).get('as_of')
    observed = parse_datetime(observed) if isinstance(observed, str) else observed
    response_deadline = response_cutoff.isoformat() if observed and observed < response_cutoff else None
    task_reviews = {t['id']: t for t in (plan.get('workflow') or {}).get('tasks', [])}
    for original_person, items in plan.get('recipients', {}).items():
        for raw in items:
            require_identity = config.get('mode') != 'excel'
            item = route_item(data, dict(raw, recipient_id=raw.get('recipient_id', original_person)), day=day,
                              require_identity=require_identity)
            if item.get('routing_status'):
                pending.append(item)
                continue
            kind = item['kind']; table = item.get('table', 'daily_confirmations'); rid = item.get('record_id', original_person)
            row = next((r for r in data.get(table, []) if r['id'] == rid), {})
            if row.get('status') in ('done', 'cancelled', 'closed', 'rejected', 'retired') and kind not in ('workflow_parent_conflict', 'workflow_task_audit'):
                continue
            if table == 'registers' and row.get('kind') == 'decision' and row.get('status') == 'approved' and row.get('decision') and row.get('decision_by_id'):
                try:
                    cutoff = parse_datetime(now or plan.get('as_of') or (plan.get('workflow') or {}).get('as_of'))
                    if parse_datetime(row.get('decided_at')) <= cutoff: continue
                except (ValueError, TypeError): pass
            c = item.get('constraint', {})
            source = {'table': table, 'id': rid, 'title': item.get('title') or row.get('title') or row.get('name') or '今日安排',
                      'url': row.get('source_url') or c.get('source_url')}
            review = task_reviews.get(rid, {}) if table == 'wbs' else {}
            impact = ('涉及 ' + str(len(review['impact']['ancestors'])) + ' 级上级；' + review['impact']['project_assessment']) if review else ''
            if c: impact = c.get('impact', {}).get('project_assessment', '')
            specs = []
            fields = list(item.get('required_fields', []))
            def add(obligation, typ, action, required=None, due=None, basis=None, reference=None):
                specs.append({'obligation': obligation, 'type': typ, 'action': action,
                              'required_fields': fields if required is None else required,
                              'deadline': _deadline(due, day, zone),
                              'deadline_basis': basis if due else '尚未登记本行动处理期限，请确认具体日期/时间',
                              'date_reference': reference})
            scenarios = item.get('scenarios', [])
            if kind in ('workflow_task', 'workflow_task_audit') and scenarios:
                groups = {}
                for scenario in scenarios:
                    groups.setdefault(scenario['category'], []).append(scenario)
                for category, selected in groups.items():
                    types = {'next_action': 'execution', 'remediation': 'execution', 'acceptance': 'decision'}
                    due = row.get('next_due_at') if category == 'next_action' else row.get('remediation_expected') if category == 'remediation' else None
                    basis = '任务登记的下一步行动期限' if category == 'next_action' else '已登记的补救措施预期落实日' if category == 'remediation' else None
                    add(category, types.get(category, 'filling'), '；'.join(s['title'] for s in selected),
                        list(dict.fromkeys(f for s in selected for f in s['required_fields'])), due, basis)
                    specs[-1].update(scenario_ids=[s['id'] for s in selected],
                                     questions=[q for s in selected for q in s['questions']])
                if (review.get('delay_state') in ('risk', 'overdue') and row.get('remediation_action')
                        and not (row.get('remediation_actual') and row.get('remediation_result'))
                        and 'remediation' not in groups):
                    add('remediation', 'execution', '核实并推进登记的补救措施：' + row['remediation_action'] + '；尚未批准的建议请先确认授权。',
                        ['remediation_actual', 'remediation_result'], row.get('remediation_expected'), '已登记的补救措施预期落实日')
            elif kind == 'workflow_task':
                maintenance = [f for f in fields if f not in ('blocker_reason', 'next_action', 'delay_impact', 'remediation_action', 'remediation_expected')]
                if maintenance: add('maintenance', 'filling', '补齐任务资料并由当前负责人反馈计划/进展。', maintenance)
                if row.get('status') == 'blocked':
                    add('next_action', 'execution', row.get('next_action') or '明确并落实解除阻碍的行动。',
                        ['blocker_reason', 'next_action', 'next_due_at'], row.get('next_due_at'), '任务登记的下一步行动期限')
                if review.get('delay_state') in ('risk', 'overdue'):
                    add('delay_review', 'filling', '补充延期原因、影响及补救安排，并说明当前结果。',
                        [f for f in ('delay_reason', 'delay_impact', 'remediation_action', 'remediation_owner_id', 'remediation_expected') if not row.get(f)] or ['progress_note'],
                        reference=('任务原计划完成日', row.get('baseline_finish')))
                    if row.get('remediation_action') and not (row.get('remediation_actual') and row.get('remediation_result')):
                        add('remediation', 'execution', '核实并推进登记的补救措施：' + row['remediation_action'] + '；尚未批准的建议请先确认授权。',
                            ['remediation_actual', 'remediation_result'], row.get('remediation_expected'), '已登记的补救措施预期落实日')
            elif kind == 'workflow_resource':
                ck = c.get('kind')
                typ = 'decision' if ck == 'decision' else 'execution' if ck in ('interface', 'register') else 'coordination'
                obligation = 'decision' if ck == 'decision' else 'register_action' if ck == 'register' else c.get('id', item.get('source_key', rid))
                add(obligation, typ, c.get('next_action') or '确认资源缺口与落实安排。', due=c.get('deadline'),
                    basis=c.get('deadline_basis') or '来源登记的处理期限',
                    reference=('接口预计就绪日', row.get('expected_ready')) if ck == 'interface' else None)
            elif kind in ('decision_due', 'register_due', 'risk_review'):
                add('decision' if kind == 'decision_due' else 'register_action', 'decision' if kind == 'decision_due' else 'execution',
                    row.get('next_action') or ('审议并记录结论、决定人和决定时间。' if kind == 'decision_due' else '处理登记事项并回填结果依据。'),
                    due=row.get('deadline_at'), basis='登记事项的最迟处理时间')
            elif kind in ('blocked', 'next_action_due'):
                add('next_action', 'execution', row.get('next_action') or '明确并落实下一步解除阻碍的行动。',
                    due=row.get('next_due_at'), basis='任务登记的下一步行动期限')
            elif kind == 'remediation_due':
                add('remediation', 'execution', row.get('remediation_action') or '落实补救措施并提交结果。',
                    due=row.get('remediation_expected'), basis='已登记的补救措施预期落实日')
            elif kind in ('overdue', 'forecast_risk'):
                add('delay_review', 'filling', '补充延期原因、影响及补救安排，并说明当前结果。',
                    reference=('任务对照完成日', row.get('baseline_finish') or row.get('forecast_finish')))
            elif kind == 'due_soon':
                add('delivery_check', 'execution', '确认交付或验收准备及是否需要协调。',
                    reference=('任务对照完成日', row.get('baseline_finish') or row.get('forecast_finish')))
            elif kind == 'stale_progress': add('maintenance', 'filling', '由当前负责人更新进度及当前计划。')
            elif kind.startswith(('daily_confirmation', 'monday_plan', 'friday_result')):
                add(kind, 'filling', item.get('reason', '补充本次收集的信息。'), due=item.get('deadline'), basis='项目配置的本次收集截止时间')
            elif kind == 'awaiting_acceptance':
                add('acceptance', 'decision', '核验提交物并记录验收结论、时间及意见。')
            else:
                add(kind, 'coordination' if kind.startswith(('schedule_pending', 'workflow_parent', 'escalation', 'dependency')) else 'execution',
                    item.get('reason', '确认事项与下一步。'))
            for spec in specs:
                routed = item
                if spec['obligation'] in ('next_action', 'remediation', 'acceptance'):
                    responsible = ((row.get('coordinator_id') or row.get('owner_id')) if spec['obligation'] == 'next_action' else
                                   row.get('acceptor_id') if spec['obligation'] == 'acceptance' else row.get('remediation_owner_id')) if row else item.get('responsible_id')
                    routed = route_item(data, dict(item, responsible_id=responsible), day=day, require_identity=require_identity)
                    if routed.get('routing_status'):
                        pending.append(routed)
                        continue
                routing = routed.get('routing_note', '')
                if routing:
                    # Original execution/decision deadline is context, not an assignment SLA.
                    spec['date_reference'] = ('原事项处理期限', spec['deadline']) if spec['deadline'] else spec.get('date_reference')
                    spec.update(type='coordination', action=routing, deadline=None,
                                deadline_basis='责任指定/协调期限待PM确认')
                identity = ':'.join((table, str(rid), spec['obligation'], routed['recipient_id']))
                action = dict(routed, **spec, id=identity, source=source, impact=impact,
                              planned_finish=row.get('forecast_finish') if table in ('wbs', 'operations') else None,
                              routing_note=routing)
                action['source_kinds'] = [kind]
                action.setdefault('scenario_ids', [])
                action.setdefault('questions', [])
                if identity in merged:
                    old = merged[identity]
                    old['required_fields'] = list(dict.fromkeys(old['required_fields'] + action['required_fields']))
                    old['source_kinds'] = list(dict.fromkeys(old['source_kinds'] + [kind]))
                    old['scenario_ids'] = list(dict.fromkeys(old['scenario_ids'] + action['scenario_ids']))
                    old['questions'] = list({q['id']: q for q in old['questions'] + action['questions']}.values())
                    if action['impact']: old['impact'] = action['impact']
                    if c: old['action'] = action['action']
                else: merged[identity] = action
    grouped = {}
    for action in merged.values():
        source_labels = {f.name: f.label for f in TABLES.get(action['source']['table'], {}).get('fields', [])}
        fields = '、'.join(source_labels.get(f, LABELS.get(f, '相关信息（详见来源）')) for f in action['required_fields'])
        action['evidence_required'] = ('请回填/回复：' + fields if fields else '回复处理结果、依据及下一步；未完成请说明原因与明确时间。')
        if any(f in action['required_fields'] for f in ('actual_start', 'actual_finish', 'remediation_actual')):
            action['evidence_required'] = '实际日期仅在已发生时填写；未开始/未完成请如实说明。' + action['evidence_required']
        if action.get('routing_note'):
            action['evidence_required'] = '明确实际责任人及处理期限；决定仍由已确认的决定人作出。' if action['obligation'] == 'decision' else '明确责任人、协调安排及处理期限。'
            action['questions'] = [{'id': 'routing:assignment', 'prompt': action['evidence_required']}]
        if not action['questions']:
            action['questions'] = [{'id': 'response', 'prompt': '请回复处理进展、结果依据及所需支持；未解决请说明后续安排。'}]
        action['response_fields'] = [q['id'] for q in action['questions']]
        action['response_deadline'] = response_deadline
        action['response_deadline_basis'] = ('项目配置的当日回复截止，与交付或补救期限分开。' if response_deadline else
                                             '本轮已过回复截止或观察时点未知；需另约回复期限，不立即算迟复。')
        grouped.setdefault(action['recipient_id'], []).append(action)
    return {'recipients': grouped, 'pending': pending}


def message_limits(config):
    count = config.get('group_max_items', 5); chars = config.get('group_max_chars', 3500)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise ValueError('group_max_items must be an integer between 1 and 10')
    if isinstance(chars, bool) or not isinstance(chars, int) or not 1200 <= chars <= 6000:
        raise ValueError('group_max_chars must be an integer between 1200 and 6000')
    return count, chars


def _short(value, limit):
    # Plain-text post content cannot construct at elements, even with literal markup.
    text = ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or '')).split())
    return text if len(text) <= limit else text[:limit - 9] + '…详见来源记录'


def render_action(action, number, *, limit=1100):
    """Every item keeps its deadline, basis and reply standard even when condensed."""
    part = max(35, (limit - 500) // 10)
    source = action['source']
    lines = [str(number) + '.【' + TYPES[action['type']] + '】' + _short(source['title'], part),
             '事项：' + _short(action['action'], part * 2),
             'DDL：' + _short(action.get('deadline') or '待确认', 75) + '；依据：' + _short(action['deadline_basis'], part),
             '完成/回复：' + _short(action['evidence_required'], part * 2),
             '来源：' + _short(TABLES.get(source['table'], {}).get('sheet', '来源记录') + '/' + str(source['id']), part)]
    if source.get('url'):
        url = str(source['url'])
        lines[-1] += ' ' + url if len(url) <= part and not re.search(r'[\s\x00-\x1f\x7f]', url) else '（完整链接见预览/来源记录）'
    if action.get('impact'): lines.append('影响：' + _short(action['impact'], part))
    if action.get('planned_finish'): lines.append('任务当前预计完成日：' + _short(action['planned_finish'], 75) + '（非本次回复DDL）')
    ref = action.get('date_reference')
    if ref and ref[1]: lines.append(_short(ref[0], 30) + '：' + _short(ref[1], 75) + '（对照信息）')
    if action.get('questions'):
        # Controlled prompts are never truncated: reply completeness is judged
        # only against questions the recipient actually received.
        lines.append('需回复：' + ' '.join(str(i) + ') ' + q['prompt'] for i, q in enumerate(action['questions'], 1)))
        lines.append('回复截止：' + (action.get('response_deadline') or '待另行约定') + '（不改变任务DDL）')
    return '\n'.join(lines)


def action_batches(actions, config):
    """Bound both item count and characters; full action facts remain in the preview."""
    max_items, max_chars = message_limits(config)
    header = '项目待办｜请按编号逐项处理；未约定DDL的事项请确认期限。\n'
    batches = []; batch = []; text = header
    for action in actions:
        rendered = render_action(action, len(batch) + 1, limit=min(1100, max_chars - len(header) - 10))
        if batch and (len(batch) >= max_items or len(text) + len(rendered) + 2 > max_chars):
            batches.append({'actions': batch, 'text': text.rstrip()})
            batch = []; text = header
            rendered = render_action(action, 1, limit=min(1100, max_chars - len(header) - 10))
        if len(header) + len(rendered) + 2 > max_chars:
            raise ValueError('Action could not fit configured message bound')
        batch.append(action); text += rendered + '\n\n'
    if batch: batches.append({'actions': batch, 'text': text.rstrip()})
    return batches
