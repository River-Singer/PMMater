"""Stable remote identities survive formula display changes and sourced writes."""
import copy
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pm_core.actions import Ledger, digest, execute_action
from pm_core.feishu import BitableAdapter, FeishuClient, FeishuError
from pm_core.integration_cli import config_template, run
from pm_core.schedule_history import source_identity
from pm_core.schema import TABLES


class Remote:
    def __init__(self, config):
        self.config = config
        self.rows = {t: [] for t in config['tables']}
        self.writes = []
        self.fail_after_write = False
        self.missing_identity = False
        self.timeout = False
        self.generated_display = None

    def transport(self, method, path, body, query, raw):
        table = next(t for t in self.rows if '/' + self.config['tables'][t]['table_id'] + '/' in path)
        if path.endswith('/fields'):
            if self.fail_after_write and self.writes:
                raise FeishuError('Readback denied', code=403)
            mappings = list(self.config['tables'][table]['fields'].values())
            if self.config['tables'][table].get('display_field'):
                mappings.append(self.config['tables'][table]['display_field'])
            codes = {'text': 1, 'number': 2, 'select': 3, 'checkbox': 7, 'date': 5,
                     'datetime': 5, 'relation': 21, 'person': 11, 'link': 15, 'formula': 20}
            items = [{'field_name': s['name'], 'type': codes[s['type']]} for s in mappings]
            return {'code': 0, 'data': {'items': items}}
        if path.endswith('/records/search'):
            return {'code': 0, 'data': {'items': copy.deepcopy(self.rows[table])}}
        self.writes.append((method, path, copy.deepcopy(body), dict(query)))
        if self.timeout:
            raise TimeoutError()
        if method == 'POST':
            row = {'record_id': 'recCreated', 'fields': copy.deepcopy(body['fields'])}
            display = self.config['tables'][table].get('display_field')
            if display:
                row['fields'][display['name']] = self.generated_display
            self.rows[table].append(row)
            return {'code': 0, 'data': {} if self.missing_identity else {'record': row}}
        row = next(r for r in self.rows[table] if path.endswith('/' + r['record_id']))
        row['fields'].update(body['fields'])
        return {'code': 0, 'data': {'record': copy.deepcopy(row)}}


def configuration(*tables):
    config = config_template(profile='mother-template-20261111')
    config.update(base_token='base', project_id='')
    config['tables'] = {t: config['tables'][t] for t in tables}
    config['confirmed_empty_tables'] = [t for t in TABLES if t not in tables]
    for t in tables:
        config['tables'][t]['table_id'] = 'tbl_' + t
    return config


class IdentityTests(unittest.TestCase):
    def adapter(self, *tables):
        config = configuration(*tables)
        remote = Remote(config)
        adapter = BitableAdapter(FeishuClient(transport=remote.transport), config)
        return config, remote, adapter

    def test_people_need_no_number_or_removed_url_and_same_names_keep_identity(self):
        config, remote, adapter = self.adapter('people')
        remote.rows['people'] = [
            {'record_id': 'recOne', 'fields': {'姓名': '同名', '邮箱': 'one@example.invalid'}},
            {'record_id': 'recTwo', 'fields': {'姓名': '同名', '邮箱': 'two@example.invalid', '替补联系人': ['recOne']}},
        ]
        rows = adapter.read_all()['people']
        self.assertEqual(['recOne', 'recTwo'], [r['id'] for r in rows])
        self.assertIsNone(rows[0]['feishu_url'])
        self.assertEqual('recOne', rows[1]['backup_id'])
        self.assertNotIn('id', config['tables']['people']['fields'])

    def test_formula_prefix_changes_do_not_rekey_relations_or_audit_source(self):
        config, remote, adapter = self.adapter('projects', 'wbs', 'people')
        remote.rows['projects'] = [{'record_id': 'recProject', 'fields': {'编号': {'type': 1, 'value': [{'text': 'CAR'}]}}}]
        remote.rows['people'] = [{'record_id': 'recOwner', 'fields': {'姓名': '王工'}}]
        wbs = config['tables']['wbs']['fields']
        remote.rows['wbs'] = [
            {'record_id': 'recParent', 'fields': {'WBS编号': [{'text': 'WBS-CAR-0001'}]}},
            {'record_id': 'recChild', 'fields': {'WBS编号': [{'text': 'WBS-CAR-0002'}],
             wbs['parent_id']['name']: ['recParent'], wbs['project_id']['name']: ['recProject'],
             wbs['owner_id']['name']: ['recOwner']}}
        ]
        first = adapter.read_all()
        source = source_identity(config)
        self.assertEqual('CAR', adapter.display_values['projects']['recProject'])
        remote.rows['projects'][0]['fields']['编号']['value'][0]['text'] = 'NEW'
        remote.rows['wbs'][1]['fields']['WBS编号'][0]['text'] = 'WBS-NEW-0002'
        second = adapter.read_all()
        self.assertEqual(first, second)
        self.assertEqual(source, source_identity(config))
        self.assertEqual('WBS-NEW-0002', adapter.display_values['wbs']['recChild'])
        self.assertEqual('recParent', second['wbs'][1]['parent_id'])
        self.assertEqual('recOwner', second['wbs'][1]['owner_id'])

    def test_absence_must_be_explicit_optional_unmapped_and_never_identity(self):
        config = configuration('people')
        for field in ('id', 'version', 'name', 'not_a_field'):
            invalid = copy.deepcopy(config)
            invalid['tables']['people']['confirmed_absent_fields'] = [field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Only optional'):
                BitableAdapter(None, invalid)
        invalid = copy.deepcopy(config)
        invalid['tables']['people']['confirmed_absent_fields'] = ['email']
        with self.assertRaisesRegex(ValueError, 'both mapped'):
            BitableAdapter(None, invalid)
        config['tables']['people']['confirmed_absent_fields'] = []
        with self.assertRaisesRegex(ValueError, 'field coverage'):
            BitableAdapter(None, config).read_all()

    def test_link_record_ids_object_preserves_relations_and_rejects_malformed_values(self):
        _, remote, adapter = self.adapter('people')
        remote.rows['people'] = [
            {'record_id': 'recOne', 'fields': {'姓名': '甲', '替补联系人': {'link_record_ids': []}}},
            {'record_id': 'recTwo', 'fields': {'姓名': '乙', '替补联系人': {'link_record_ids': ['recOne']}}},
        ]
        rows = adapter.read_all()['people']
        self.assertIsNone(rows[0]['backup_id'])
        self.assertEqual('recOne', rows[1]['backup_id'])
        self.assertEqual(['recOne'], adapter._value('people', 'backup_id', 'recOne', encode=True))
        for invalid in ('recOne', None, [None], ['']):
            remote.rows['people'][1]['fields']['替补联系人'] = {'link_record_ids': invalid}
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'Invalid linked record IDs'):
                adapter.read_all()
        remote.rows['people'][1]['fields']['替补联系人'] = {'link_record_ids': ['recMissing']}
        with self.assertRaisesRegex(ValueError, 'Unresolved relation'):
            adapter.read_all()
        remote.rows['people'][1]['fields']['替补联系人'] = {'link_record_ids': ['recOne', 'recTwo']}
        with self.assertRaisesRegex(ValueError, 'Multiple records in a single reference'):
            adapter.read_all()

    def test_formula_fields_reject_writes_including_preview(self):
        config, remote, adapter = self.adapter('people')
        config['tables']['people']['fields']['company'].update(type='formula', writable=False)
        remote.rows['people'] = [{'record_id': 'recOne', 'fields': {'姓名': '同名',
                                    '所属公司': {'type': 1, 'value': [{'text': '公司'}]}}}]
        data = adapter.read_all()
        self.assertEqual('公司', data['people'][0]['company'])
        with self.assertRaisesRegex(ValueError, 'read-only'):
            adapter.validate_write('update', 'people', 'recOne', {'company': 'changed'})
        with self.assertRaisesRegex(ValueError, 'read-only'):
            adapter.write('update', 'people', 'recOne', {'company': 'changed'}, 'request', expected=data['people'][0])
        config['tables']['people']['fields']['company']['writable'] = True
        with self.assertRaisesRegex(ValueError, 'Formula is read-only'):
            BitableAdapter(None, config)
        self.assertFalse(remote.writes)

    def test_display_number_cannot_be_mapped_as_stable_id(self):
        config = configuration('wbs')
        config['tables']['wbs']['fields']['id'] = {'name': 'WBS编号', 'type': 'formula', 'writable': False}
        with self.assertRaisesRegex(ValueError, 'cannot map'):
            BitableAdapter(None, config)
        config['tables']['wbs']['identity'] = 'field'
        with self.assertRaisesRegex(ValueError, 'cannot be a stable id'):
            BitableAdapter(None, config)

    def test_source_identity_preserves_legacy_and_separates_new_mode(self):
        old = {'base_token': 'base', 'tables': {'wbs': {'table_id': 'tbl'}}}
        expected = 'feishu:base:' + digest({'wbs': 'tbl'})[:16]
        self.assertEqual(expected, source_identity(old))
        old['tables']['wbs']['identity'] = 'field'
        self.assertEqual(expected, source_identity(old))
        old['tables']['wbs']['identity'] = 'record_id'
        self.assertNotEqual(expected, source_identity(old))

    def test_pull_keeps_display_values_separate_from_canonical_data(self):
        config, remote, _ = self.adapter('projects')
        remote.rows['projects'] = [{'record_id': 'recProject', 'fields': {'编号': {'type': 1, 'value': [{'text': 'CAR'}]}}}]
        args = Namespace(command='feishu-pull', now=None, config='configured.json', db=None,
                         project_id=None, out=None)
        with patch('pm_core.integration_cli.load_config', return_value=config), patch(
                'pm_core.integration_cli.FeishuClient', return_value=FeishuClient(transport=remote.transport)):
            result = run(args)
        self.assertEqual('recProject', result['data']['projects'][0]['id'])
        self.assertEqual({'projects': {'recProject': 'CAR'}}, result['display_values'])
        self.assertNotIn('display_values', result['data'])

    def create_action(self):
        return {'id': 'add-person', 'operation': 'create', 'table': 'people',
                'values': {'name': '新增成员', 'person_type': 'employee', 'active': True},
                'source': {'id': 'minute-1', 'url': 'https://example.invalid/minutes',
                           'kind': 'confirmed_fact', 'quote': '增加成员'}}

    def run_create(self, remote_flags=None, action=None, *, display=False):
        config, remote, adapter = self.adapter('people')
        if display:
            config['tables']['people']['display_field'] = {'name': '展示编号', 'type': 'formula', 'writable': False}
            adapter = BitableAdapter(FeishuClient(transport=remote.transport), config)
        for k, v in (remote_flags or {}).items():
            setattr(remote, k, v)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = Ledger(Path(temp.name) / 'ledger.sqlite')
        self.addCleanup(ledger.close)
        policy = {'enabled': True, 'allowed_tables': ['people'], 'allowed_operations': ['create']}
        act = action or self.create_action()
        result = execute_action(act, adapter, ledger, policy, dry_run=False)
        repeated = execute_action(act, adapter, ledger, policy, dry_run=False)
        return remote, ledger, result, repeated

    def test_server_assigned_create_stores_real_id_and_deduplicates(self):
        remote, ledger, result, repeated = self.run_create()
        self.assertEqual('succeeded', result['status'], result)
        self.assertEqual('recCreated', result['detail']['record_id'])
        self.assertEqual('recCreated', result['detail']['after']['id'])
        self.assertEqual('recCreated', ledger.get(result['key'])['detail']['record_id'])
        self.assertTrue(repeated['duplicate'])
        self.assertEqual(1, len(remote.writes))
        self.assertNotIn('编号', remote.writes[0][2]['fields'])
        self.assertIn('client_token', remote.writes[0][3])

    def test_created_but_unconfirmed_is_unknown_and_never_recreated(self):
        for flags in ({'missing_identity': True}, {'fail_after_write': True}, {'timeout': True}):
            with self.subTest(flags=flags):
                remote, ledger, result, repeated = self.run_create(flags)
                self.assertEqual('unknown', result['status'], result)
                self.assertTrue(repeated['duplicate'])
                self.assertEqual(1, len(remote.writes))
                if flags.get('fail_after_write'):
                    self.assertEqual('recCreated', ledger.get(result['key'])['detail']['record_id'])

    def test_formula_display_is_retained_after_write_and_empty_result_is_unknown(self):
        for value in (None, '', {'type': 1, 'value': [{'text': 'WBS-CAR-0001'}]}):
            with self.subTest(value=value):
                remote, ledger, result, repeated = self.run_create({'generated_display': value}, display=True)
                expected = 'WBS-CAR-0001' if value else value
                self.assertEqual('succeeded' if value else 'unknown', result['status'], result)
                self.assertEqual(expected, result['detail']['display_value'])
                self.assertEqual('recCreated', ledger.get(result['key'])['detail']['record_id'])
                self.assertNotIn('展示编号', remote.writes[0][2]['fields'])
                self.assertTrue(repeated['duplicate'])
                self.assertEqual(1, len(remote.writes))

    def test_assigned_create_rejects_caller_identity(self):
        for extra in ({'record_id': 'fake-number'}, {'values': dict(self.create_action()['values'], id='fake-number')}):
            with self.subTest(extra=extra):
                remote, _, result, _ = self.run_create(action=dict(self.create_action(), **extra))
                self.assertEqual('pending', result['status'])
                self.assertFalse(remote.writes)

    def test_legacy_configuration_remains_default(self):
        config = config_template()
        self.assertIn('id', config['tables']['people']['fields'])
        self.assertIn('feishu_url', config['tables']['people']['fields'])
        self.assertNotIn('identity', config['tables']['people'])


if __name__ == '__main__':
    unittest.main()
