"""Concurrent member edits must survive a sourced action's preflight window."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from pm_core.actions import Ledger, LocalBackend, execute_action
from pm_core.feishu import BitableAdapter, FeishuClient, WriteConflict
from pm_core.fixtures import demo_data
from pm_core.store import Store


class WriteConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.store.apply_batch(demo_data())
        self.store.apply_batch({'operations':[{'id':'OPS-CAS','project_id':'PRJ-DEMO','title':'original'}]})
        self.backend=LocalBackend(self.store)
        self.ledger=Ledger(Path(self.temp.name)/'ledger.sqlite3');self.addCleanup(self.ledger.close)
        self.before=self.backend.read('operations','OPS-CAS')
        self.policy={'enabled':True,'allowed_tables':['operations'],'allowed_operations':['update']}
        self.action={'id':'CAS','operation':'update','table':'operations','record_id':'OPS-CAS',
                     'values':{'title':'meeting update'},'expected':{'title':'original'},
                     'source':{'id':'MINUTE','url':'https://example.invalid/meeting','quote':'confirmed rename','kind':'confirmed_fact'}}

    def test_action_rechecks_after_preflight_and_preserves_new_human_edit(self):
        validate=self.backend.validate_write
        def concurrent_edit(*args):
            validate(*args)
            self.store.apply_batch({'operations':[dict(self.before,title='new human edit')]})
        with patch.object(self.backend,'validate_write',side_effect=concurrent_edit):
            result=execute_action(self.action,self.backend,self.ledger,self.policy,dry_run=False)
        self.assertEqual(result['status'],'conflict')
        self.assertEqual(self.backend.read('operations','OPS-CAS')['title'],'new human edit')

    def test_store_cas_uses_reviewed_version_even_after_last_local_read(self):
        apply=self.store.apply_batch
        def concurrent_edit(batch,**kwargs):
            apply({'operations':[dict(self.before,title='new human edit')]})
            return apply(batch,**kwargs)
        with patch.object(self.store,'apply_batch',side_effect=concurrent_edit):
            with self.assertRaises(WriteConflict):
                self.backend.write('update','operations','OPS-CAS',{'title':'meeting update'},'req',expected=self.before)
        self.assertEqual(self.backend.read('operations','OPS-CAS')['title'],'new human edit')

    def test_delete_cas_preserves_record_modified_after_read(self):
        delete=self.store.delete_record
        def concurrent_edit(*args,**kwargs):
            self.store.apply_batch({'operations':[dict(self.before,title='new human edit')]})
            return delete(*args,**kwargs)
        with patch.object(self.store,'delete_record',side_effect=concurrent_edit):
            with self.assertRaises(WriteConflict):
                self.backend.write('delete','operations','OPS-CAS',{},'req',expected=self.before)
        self.assertIsNotNone(self.backend.read('operations','OPS-CAS'))

    def test_remote_write_rechecks_immediately_before_http(self):
        calls=[]
        client=FeishuClient(transport=lambda *args:calls.append(args))
        adapter=BitableAdapter(client,{'base_token':'base','timezone':'Asia/Shanghai',
            'tables':{'operations':{'table_id':'table','fields':{'id':'编号','title':'名称'}}}})
        latest=dict(self.before,title='new human edit')
        with patch.object(adapter,'read_all',return_value={'operations':[latest]}):
            with self.assertRaises(WriteConflict):
                adapter.write('update','operations','OPS-CAS',{'title':'meeting update'},'req',expected=deepcopy(self.before))
        self.assertEqual(calls,[])

    def test_remote_wbs_guards_match_local_entrypoint(self):
        calls=[]
        adapter=BitableAdapter(FeishuClient(transport=lambda *args:calls.append(args)),
            {'base_token':'base','timezone':'Asia/Shanghai','tables':{'wbs':{'table_id':'table','fields':{'id':'编号'}}}})
        data=self.store.data()
        for scope in ('operations','adhoc'):
            with self.subTest(scope=scope),patch.object(adapter,'read_all',return_value=data):
                with self.assertRaisesRegex(ValueError,'operations_separate'):
                    adapter.validate_write('create','wbs','NEW',{'title':'配置','scope':scope})
                with self.assertRaisesRegex(ValueError,'operations_separate'):
                    adapter.write('create','wbs','NEW',{'title':'配置','scope':scope},'req',expected=None)
        alias=dict(data['wbs'][0],migrated_operation_id='OPS-LEGACY')
        data['wbs']=[alias]
        for operation in ('update','delete'):
            with self.subTest(operation=operation),patch.object(adapter,'read_all',return_value=data):
                with self.assertRaisesRegex(ValueError,'migrated_readonly'):
                    adapter.validate_write(operation,'wbs',alias['id'],{'title':'attempted edit'})
                with self.assertRaisesRegex(ValueError,'migrated_readonly'):
                    adapter.write(operation,'wbs',alias['id'],{'title':'attempted edit'},'req',expected=alias)
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
