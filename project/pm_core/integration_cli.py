"""CLI surface for host-neutral workflows and optional Feishu bridge."""
from contextlib import ExitStack
from pathlib import Path
import json

from .store import Store, utc_now, parse_datetime
from .runtime import load_config
from .feishu import FeishuClient, BitableAdapter
from .actions import Ledger, LocalBackend, execute_action, digest


def add_commands(sub):
    names = ('feishu-pull', 'minutes-read', 'actions-run', 'agent-tick', 'followup-plan',
             'project-calendar', 'attendance-plan', 'attendance-confirm', 'resource-review',
             'management-review', 'report-publish', 'feishu-config-template', 'feishu-report', 'runtime-reconcile', 'schedule-audit', 'followup-evidence-import')
    for name in names:
        p = sub.add_parser(name)
        p.add_argument('--config')
        p.add_argument('--db')
        p.add_argument('--out')
        p.add_argument('--html')
        p.add_argument('--lookahead-weeks', type=int, default=6)
        p.add_argument('--file')
        p.add_argument('--ledger')
        p.add_argument('--project-id')
        p.add_argument('--person-id')
        p.add_argument('--date')
        p.add_argument('--token')
        p.add_argument('--now')
        p.add_argument('--period', choices=('week', 'month'), default='week')
        p.add_argument('--actor', default='project-agent')
        p.add_argument('--apply', action='store_true', help='执行已配置范围内动作；默认只预览')
        if name == 'feishu-config-template':
            p.add_argument('--profile', choices=('canonical', 'mother-template-20261111'), default='canonical')
    return names


def run(args):
    now = parse_datetime(args.now) if args.now else utc_now()
    config = load_config(args.config) if args.config else None
    command = args.command
    result = None
    if command == 'followup-evidence-import':
        if not config or not args.ledger or not args.file or not Path(args.ledger).is_file():
            raise ValueError('Reply evidence requires config, existing persistent ledger and evidence file')
        if args.now and config.get('mode') == 'feishu':
            raise ValueError('Live evidence import uses the actual capture time; --now cannot backdate observations')
        from .followup_evidence import import_evidence
        payload = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        ledger = Ledger(args.ledger)
        try:
            return import_evidence(ledger, config, payload, now=now, apply=args.apply)
        finally:
            ledger.close()
    if command == 'runtime-reconcile':
        if not args.ledger or not args.file or not args.apply:
            raise ValueError('Reconciliation requires --ledger --file --apply and actual verification evidence')
        payload = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        ledger = Ledger(args.ledger)
        try:
            return ledger.reconcile(payload['key'], outcome=payload['outcome'], evidence=payload['evidence'])
        finally:
            ledger.close()
    if command == 'feishu-config-template':
        result = config_template(profile=getattr(args, 'profile', 'canonical'))
        if args.out:
            target = Path(args.out); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    with ExitStack() as stack:
        store = None
        if args.db:
            if not Path(args.db).is_file():
                raise ValueError('Database not found; initialize first')
            store = stack.enter_context(Store(args.db))
        remote = config and config.get('mode') == 'feishu'
        if remote and args.now and command not in ('minutes-read', 'actions-run', 'report-publish'):
            raise ValueError('Live Feishu reads use the actual capture time; --now cannot backdate current records. Read a saved snapshot for historical reports.')
        client = FeishuClient(token_env=config.get('token_env', 'FEISHU_ACCESS_TOKEN')) if remote else None
        adapter = BitableAdapter(client, config) if remote and command not in ('minutes-read', 'report-publish') else None
        backend = adapter or (LocalBackend(store, args.actor) if store else None)
        project_id = args.project_id or (config or {}).get('project_id')
        if command in ('feishu-pull', 'minutes-read', 'report-publish') and not remote:
            raise ValueError('Feishu mode and config required')
        if command == 'feishu-pull':
            result = {'data': adapter.read_all(), 'display_values': adapter.display_values,
                      'captured_at': now.isoformat(), 'project_id': config['project_id']}
        elif command == 'feishu-report':
            if not adapter or not args.out:
                raise ValueError('Feishu report requires configured adapter and --out HTML path')
            from .operating import management_review
            from .project_report import export_project_report
            data = adapter.read_all()
            audit = _audit(data, config, now, stack, ledger_path=args.ledger)
            review = management_review(data, now=now, period=args.period, lookahead_weeks=args.lookahead_weeks, schedule_audit=audit)
            snapshot_id = digest(data)
            if args.period == 'month':
                from .operating import export_management_report
                result = export_management_report(data, args.out, as_of=now.isoformat(), period='month', lookahead_weeks=args.lookahead_weeks, schedule_audit=audit)
            else:
                result = export_project_report(data, args.out, as_of=now.isoformat(), timezone_name=config['timezone'], schedule_audit=audit)
            from .management_text import management_paragraphs
            paragraphs = management_paragraphs(data, review)
            report = {'title': '项目进度审议 ' + now.isoformat(), 'paragraphs': paragraphs, 'snapshot_id': snapshot_id, 'cutoff': now.isoformat()}
            sidecar = Path(args.out).with_suffix('.json')
            sidecar.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            result.update(document_payload=str(sidecar), snapshot_id=snapshot_id, cutoff=now.isoformat())
        elif command == 'minutes-read':
            if args.token not in config.get('minute_tokens', []):
                raise ValueError('Register this meeting minute token in project config before reading')
            result = client.minutes(args.token)
        elif command == 'actions-run':
            if not backend or not config or not args.file or not args.ledger:
                raise ValueError('Actions require config, source action file, runtime ledger and backend')
            payload = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            actions = payload['actions'] if isinstance(payload, dict) else payload
            if not isinstance(actions, list):
                raise ValueError('Actions must be an array')
            ledger = Ledger(args.ledger)
            stack.callback(ledger.close)
            policy = dict(config.get('policy', {}), enabled=bool(config.get('enabled')))
            result = {'actions': [execute_action(a, backend, ledger, policy, client=client,
                                                   dry_run=not args.apply) for a in actions]}
        elif command == 'agent-tick':
            from .runtime import dispatch_tick
            if not config or not backend or not args.ledger:
                raise ValueError('Tick requires config, backend and persistent ledger')
            if args.apply and not client:
                raise ValueError('Excel mode does not provide a message transport; use native host tools')
            ledger = Ledger(args.ledger)
            stack.callback(ledger.close)
            from .schedule_history import ScheduleObserver
            observer = ScheduleObserver(ledger.conn) if remote else None
            result = dispatch_tick(backend.read_all, config, ledger, client, now=now, dry_run=not args.apply,
                                   history_provider=store.history if store and not remote else None, observer=observer,
                                   delivery_clock=utc_now if remote else None)
        elif command == 'report-publish':
            if not args.file or not args.ledger:
                raise ValueError('Report requires a source report file and persistent ledger')
            report = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            for field in ('title', 'paragraphs', 'snapshot_id', 'cutoff'):
                if not report.get(field):
                    raise ValueError('Report missing ' + field)
            result = {'status': 'preview', 'report': report}
            if args.apply:
                if not config.get('enabled'):
                    raise ValueError('Project integration is disabled')
                ledger = Ledger(args.ledger)
                stack.callback(ledger.close)
                key = 'report:' + config['project_id'] + ':' + report['snapshot_id']
                state = ledger.claim(key, digest(report))
                if not state['claimed_now']:
                    result = {'status': state['status'], 'detail': state['detail'], 'duplicate': True}
                else:
                    from .feishu import FeishuError
                    try:
                        detail = client.document(report['title'],
                            ['数据截止：' + report['cutoff'], '快照：' + report['snapshot_id'], *report['paragraphs']],
                            folder_token=config.get('report_folder_token'))
                        status = 'succeeded'
                    except FeishuError as exc:
                        status, detail = 'unknown' if exc.uncertain else 'failed', {'error': str(exc)}
                    ledger.save(key, digest(report), status, detail)
                    result = {'status': status, 'detail': detail}
        else:
            if not backend:
                raise ValueError('Local --db or Feishu --config required')
            data = backend.read_all()
            if not project_id and len(data.get('projects', [])) == 1:
                project_id = data['projects'][0]['id']
            if command == 'schedule-audit':
                result = _audit(data, config, now, stack, ledger_path=args.ledger, store=store if not remote else None)
                if args.html:
                    from .management_text import export_schedule_audit_report
                    export_schedule_audit_report(result, args.html)
            elif command == 'project-calendar':
                from .project_calendar import ensure_project_calendar
                if not store or not args.apply:
                    raise ValueError('Calendar generation requires local --db and --apply; Feishu host uses generated rows after review')
                result = ensure_project_calendar(store, project_id, actor=args.actor, now=now)
            elif command == 'attendance-plan':
                from .attendance import plan_daily_confirmations
                result = plan_daily_confirmations(data, project_id, now)
            elif command == 'attendance-confirm':
                from .attendance import confirm_day
                if not store or not args.apply or not args.file or not args.person_id or not args.date:
                    raise ValueError('Confirmation requires --db --apply --file --person-id --date')
                payload = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
                result = confirm_day(store, project_id, args.person_id, args.date, periods=payload.get('periods', ['AM', 'PM']),
                                     values=payload['values'], actor=args.actor, now=now)
            elif command == 'resource-review':
                from .resources import capacity_summary
                result = capacity_summary(data, project_id, week_start=args.date)
            elif command == 'management-review':
                from .operating import management_review
                audit = _audit(data, config, now, stack, ledger_path=args.ledger, store=store if not remote else None)
                result = management_review(data, now=now, period=args.period, lookahead_weeks=args.lookahead_weeks, schedule_audit=audit)
                if args.html:
                    from .operating import export_management_report
                    export_management_report(data, args.html, as_of=now.isoformat(), period=args.period, lookahead_weeks=args.lookahead_weeks, schedule_audit=audit)
            elif command == 'followup-plan':
                from .operating import plan_followups
                result = {'items': plan_followups(data, now=now, config=config)}
    if args.out and command != 'feishu-report':
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def config_template(*, profile='canonical'):
    from .schema import TABLES, ENUMS
    if profile not in ('canonical', 'mother-template-20261111'):
        raise ValueError('Unknown mapping profile')
    tables = {}
    for table, meta in TABLES.items():
        fields = {}
        for f in meta['fields']:
            if f.name == 'version':
                continue
            spec = {'name': f.label, 'type': 'text', 'writable': f.editable}
            if f.ref:
                spec.update(type='relation', table=f.ref, many=f.dtype == 'refs')
            elif f.enum:
                spec.update(type='select', options=ENUMS[f.enum])
            elif f.dtype in ('date', 'datetime'):
                spec['type'] = f.dtype
            elif f.dtype == 'boolean':
                spec['type'] = 'checkbox'
            elif f.dtype in ('integer', 'decimal'):
                spec['type'] = 'number'
            fields[f.name] = spec
        tables[table] = {'table_id': '', 'fields': fields}
    if profile == 'mother-template-20261111':
        # A scaffold, not a schema discovery result. Other fields/tables still
        # require tenant-specific verification before use.
        for table in ('people', 'projects', 'wbs'):
            tables[table]['identity'] = 'record_id'
            tables[table]['fields'].pop('id')
        tables['people']['fields']['name']['name'] = '姓名'
        tables['people']['fields'].pop('feishu_url')
        tables['people']['confirmed_absent_fields'] = ['feishu_url']
        for table, name in (('projects', '编号'), ('wbs', 'WBS编号')):
            tables[table]['display_field'] = {'name': name, 'type': 'formula', 'writable': False}
    return {'enabled': False, 'mode': 'feishu', 'project_id': '', 'timezone': 'Asia/Shanghai',
            'base_token': '', 'token_env': 'FEISHU_ACCESS_TOKEN', 'tables': tables,
            'confirmed_empty_tables': [], 'chat_id': '', 'allowed_recipients': [],
            'minute_tokens': [], 'report_folder_token': '',
            'pm_private_chat_id': '', 'pm_private_recipient_id': '',
            'schedule_audit': {'decision_makers': [], 'trusted_approval_sources': [], 'approved_changes': [], 'reviewed_changes': []}, 'daily_time': '09:00',
            'daily_deadline': '10:30', 'collection_time': '09:00', 'plan_cutoff': '10:30',
            'result_cutoff': '10:30', 'inspection_times': ['10:30', '16:00'], 'workflow_time': '10:00', 'quiet_after': '18:00',
            'group_max_items': 5, 'group_max_chars': 3500,
            'response_cutoff': '17:00', 'escalation_time': '17:00', 'min_response_minutes': 30,
            'escalation_recipient_id': '', 'reply_evidence_sources': [],
            'policy': {'allowed_tables': [t for t in TABLES if t not in ('calendars', 'calendar_days')],
                       'allowed_operations': ['query', 'create', 'update', 'delete', 'book', 'reschedule'],
                       'decision_makers': [], 'calendar_id': '', 'organizer_id': '', 'allowed_attendees': []}}


def _audit(data, config, now, stack, *, ledger_path=None, store=None):
    from .schedule_audit import audit_schedule_changes
    policy = dict((config or {}).get('schedule_audit', {}))
    policy.setdefault('decision_makers', (config or {}).get('policy', {}).get('decision_makers', []))
    if store:
        return audit_schedule_changes(data, history=store.history(), now=now, policy=policy)
    if config and config.get('mode') == 'feishu' and ledger_path:
        from .schedule_history import ScheduleObserver, source_identity
        observer = ScheduleObserver(ledger_path); stack.callback(observer.close)
        return observer.observe(data, source_id=source_identity(config), project_id=config['project_id'], now=now, policy=policy)
    return audit_schedule_changes(data, now=now, policy=policy)
