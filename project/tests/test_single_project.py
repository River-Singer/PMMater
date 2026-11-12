from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
import json
import sqlite3

from pm_core.fixtures import demo_data
from pm_core.project_report import export_project_report
from pm_core.store import Store, ValidationError

NOW=datetime(2026,11,5,18,tzinfo=timezone.utc)

class SingleProjectModelTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.db=Path(self.tmp.name)/'project.sqlite3'
        self.store=Store(self.db)
        self.store.apply_batch(demo_data(),actor='fictional-test',now=NOW)
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def apply(self,table,record):
        return self.store.apply_batch({table:[record]},actor='PM',now=NOW)
    def row(self,table,rid):
        return next(r for r in self.store.data()[table] if r['id']==rid)

    def test_roster_is_one_source_with_typed_contacts(self):
        person=self.row('people','PER-VENDOR')
        self.assertEqual(person['person_type'],'vendor')
        self.apply('people',{'id':person['id'],'version':person['version'],'phone':'+86 010 0123 4567','feishu_url':'https://example.invalid/contact/1','email':'supplier@example.invalid'})
        self.assertEqual(self.row('people',person['id'])['phone'],'+86 010 0123 4567')

    def test_bad_contact_links_and_email_do_not_write(self):
        person=self.row('people','PER-VENDOR')
        for field,value in [('email','not-an-email'),('feishu_url','javascript:alert(1)')]:
            with self.assertRaises(ValidationError):
                self.apply('people',{'id':person['id'],'version':person['version'],field:value})

    def test_http_interface_requires_method_and_address(self):
        record=self.row('interfaces','API-001')
        for field,value in [('method',None),('address','javascript:alert(1)'),('address','https://user:secret@example.invalid/api')]:
            with self.assertRaises(ValidationError):
                self.apply('interfaces',{'id':record['id'],'version':record['version'],field:value})

    def test_document_is_a_link_with_owner_and_context(self):
        doc=self.row('project_documents','DOC-001')
        for field,value in [('url','file:///somewhere'),('owner_id','PER-MISSING'),('updated_on','2100-01-01')]:
            with self.assertRaises(ValidationError):
                self.apply('project_documents',{'id':doc['id'],'version':doc['version'],field:value})

    def test_small_project_bug_can_live_without_artificial_wbs(self):
        bug=self.row('wbs','WI-005')
        self.apply('wbs',{'id':bug['id'],'version':bug['version'],'parent_id':None})
        self.assertIsNone(self.row('wbs',bug['id'])['parent_id'])
        feature=self.row('wbs','WI-002')
        with self.assertRaises(ValidationError):
            self.apply('wbs',{'id':feature['id'],'version':feature['version'],'parent_id':None})

    def test_reminder_is_actionable_without_fabricated_dependency(self):
        self.apply('registers',{'id':'REG-FOLLOW','project_id':'PRJ-DEMO','kind':'reminder','title':'确认验收参与时间','owner_id':'PER-PM','priority':'P1','next_action':'向业务接口人确认可用窗口'})
        self.assertEqual(self.row('registers','REG-FOLLOW')['kind'],'reminder')

    def test_report_escapes_user_text_and_contains_relevant_sections(self):
        person=self.row('people','PER-PM')
        self.apply('people',{'id':person['id'],'version':person['version'],'name':'<script>alert(1)</script>'})
        p=Path(self.tmp.name)/'report.html'
        result=export_project_report(self.store.data(),p,as_of=NOW.isoformat(),demo=True)
        text=p.read_text(encoding='utf-8')
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',text)
        self.assertNotIn('<script>alert(1)</script>',text)
        self.assertEqual(len(result['sections']),6)
        for label in ('本周开发与交付','会议与待办','延期与决策','延期与补救审议','全项目未完成 P0','接口与项目文档','<svg'):
            self.assertIn(label,text)
        self.assertNotIn('产品与服务日常队列',text)


class LegacyBaseUpgradeTests(TestCase):
    def test_upgrade_preserves_identity_history_snapshot_and_adds_contacts(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'legacy.sqlite3'
            con=sqlite3.connect(path)
            con.executescript((Path(__file__).parent/'fixtures/legacy_base_schema.sql').read_text(encoding='utf-8'))
            con.execute("INSERT INTO people(id,name,department,expertise,contact,active,version) VALUES(?,?,?,?,?,?,?)",('PER-OLD','原有人员','IT','接口维护','原始联系说明',1,3))
            con.execute("INSERT INTO event_log(transaction_id,recorded_at,actor,entity,record_id,after_json) VALUES(?,?,?,?,?,?)",('tx-old','2026-11-01T01:00:00+00:00','PM','people','PER-OLD','{"id":"PER-OLD","name":"原有人员"}'))
            con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",('SNAP-OLD','原有快照','2026-11-01T01:00:00+00:00','2026-11-01T01:00:00+00:00',1,'{"people":[{"id":"PER-OLD"}]}'))
            con.execute('PRAGMA user_version=3');con.commit();con.close()
            with Store(path) as store:
                person=store.data()['people'][0]
                self.assertEqual(person['id'],'PER-OLD')
                self.assertEqual(person['version'],3)
                self.assertEqual(person['person_type'],'employee')
                self.assertEqual(person['contact'],'原始联系说明')
                self.assertEqual(len(store.history()),1)
                self.assertEqual(store.read_snapshot('SNAP-OLD')['label'],'原有快照')
                self.assertEqual(store.conn.execute('PRAGMA user_version').fetchone()[0],7)
                self.assertEqual(list(store.conn.execute('PRAGMA foreign_key_check')),[])
                self.assertIn('interfaces',store.data())
            self.assertEqual(len(list(Path(tmp).glob('*.before-v4-*.bak'))),1)
