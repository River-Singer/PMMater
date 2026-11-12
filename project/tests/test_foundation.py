import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from pm_core.store import Store, ValidationError
from pm_core.fixtures import demo_data
from pm_core.attendance import plan_daily_confirmations, confirm_day
from pm_core.project_calendar import ensure_project_calendar, workday_status
from pm_core.resources import capacity_summary

NOW=datetime(2026,11,11,12,tzinfo=timezone.utc)

class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.store.apply_batch(demo_data(),now=NOW)
        person=next(r for r in self.store.data()['people'] if r['id']=='PER-VENDOR')
        self.store.apply_batch({'people':[dict(person,starts_on='2026-11-01',ends_on='2027-12-31',feishu_user_id='ou_demo')]},now=NOW)
    def tearDown(self): self.store.close()
    def test_daily_confirmation_is_not_actual_attendance_and_dedupes(self):
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        before=len(self.store.data()['day_records'])
        plan=plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)
        self.assertEqual([p['person_id'] for p in plan['eligible']],['PER-VENDOR'])
        args=dict(project_id='PRJ-DEMO',person_id='PER-VENDOR',day='2026-11-11',values={'work_location':'remote','allocation_mode':'shared','future_leave_change':'no_change','source':'message-1'},actor='member',now=NOW)
        confirm_day(self.store,**args);confirm_day(self.store,**args)
        self.assertEqual(len(self.store.data()['daily_confirmations']),2)
        self.assertEqual(len(self.store.data()['day_records']),before)
        missing=plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)['eligible'][0]
        self.assertEqual(missing['required_fields'],['available_hours'])
        for period,hours in (('AM',0),('PM',3)):
            confirm_day(self.store,**dict(args,periods=(period,),values={'available_hours':hours,'source':'message-2'}))
        self.assertEqual(plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)['eligible'],[])
        with self.assertRaises(ValueError): confirm_day(self.store,**dict(args,values={'available_hours':8,'source':'message-2'}))
    def test_unknown_calendar_prevents_reminder(self):
        self.assertFalse(plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)['calendar_ready'])
    def test_employee_and_unknown_cannot_start_daily_confirmation_but_history_can_be_corrected(self):
        args=dict(project_id='PRJ-DEMO',person_id='PER-VENDOR',day='2026-11-11',periods=('AM',),
                  values={'work_location':'remote','allocation_mode':'shared','future_leave_change':'no_change','source':'message'},actor='member',now=NOW)
        confirm_day(self.store, **args)
        person=next(r for r in self.store.data()['people'] if r['id']=='PER-VENDOR')
        self.store.apply_batch({'people':[dict(person,person_type='employee')]},now=NOW)
        confirm_day(self.store, **dict(args,values=dict(args['values'],work_location='onsite')))
        self.assertEqual('onsite',self.store.data()['daily_confirmations'][0]['work_location'])
        for periods in (('PM',),('AM','PM')):
            with self.assertRaisesRegex(ValueError,'仅面向已知非员工'):
                confirm_day(self.store, **dict(args,periods=periods))
        self.assertEqual(1,len(self.store.data()['daily_confirmations']))
        person=next(r for r in self.store.data()['people'] if r['id']=='PER-VENDOR')
        self.store.apply_batch({'people':[dict(person,person_type='unknown')]},now=NOW)
        with self.assertRaisesRegex(ValueError,'仅面向已知非员工'):
            confirm_day(self.store, **dict(args,periods=('PM',)))
    def test_leave_is_plan_and_partial_leave_preserves_other_slot(self):
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        self.store.apply_batch({'leave_plans':[{'project_id':'PRJ-DEMO','person_id':'PER-VENDOR','start_date':'2026-11-11','end_date':'2026-11-11','start_period':'AM','end_period':'AM','status':'confirmed','source':'reply','confirmed_at':NOW.isoformat(),'confirmed_by_id':'PER-VENDOR'}]},now=NOW)
        self.assertEqual(plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)['eligible'][0]['periods'],['PM'])
    def test_cross_year_makeup_and_company_person_override(self):
        project=self.store.data()['projects'][0]
        self.store.apply_batch({'projects':[dict(project,reminder_start='2026-01-01',reminder_end='2027-12-31')]},now=NOW)
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        data=self.store.data()
        self.assertFalse(workday_status(data,'PRJ-DEMO','2027-02-05')['is_workday'])
        self.assertTrue(workday_status(data,'PRJ-DEMO','2027-01-31')['is_workday'])
        self.assertTrue(workday_status(data,'PRJ-DEMO','2026-11-11')['verified'])
        self.store.apply_batch({'calendar_overrides':[{'calendar_id':project['calendar_id'],'date':'2027-02-05','person_id':'PER-VENDOR','is_workday':True,'status':'confirmed','reason':'批准例外','source_url':'https://example.invalid/decision','confirmed_at':NOW.isoformat(),'confirmed_by_id':'PER-PM'}]},now=NOW)
        self.assertFalse(workday_status(self.store.data(),'PRJ-DEMO','2027-02-05')['is_workday'])
        self.assertTrue(workday_status(self.store.data(),'PRJ-DEMO','2027-02-05','PER-VENDOR')['is_workday'])
    def test_official_refresh_removes_obsolete_exception_and_custom_gap_stays_unknown(self):
        project=self.store.data()['projects'][0]
        self.store.apply_batch({'projects':[dict(project,reminder_end='2028-02-10')]},now=NOW)
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        wrong=next(r for r in self.store.data()['calendar_days'] if r['date']=='2027-03-01')
        self.store.apply_batch({'calendar_days':[dict(wrong,is_workday=False,day_type='holiday',label='已撤销旧例外')]},now=NOW)
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        self.assertTrue(workday_status(self.store.data(),'PRJ-DEMO','2027-03-01')['is_workday'])
        year=next(r for r in self.store.data()['calendar_years'] if r['year']==2028)
        self.store.apply_batch({'calendar_years':[dict(year,status='complete',source_url='https://example.invalid/official',verified_at=NOW.isoformat(),exception_hash='externally-verified')]},now=NOW)
        self.store.conn.execute("DELETE FROM calendar_days WHERE date='2028-02-01'");self.store.conn.commit()
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        self.assertFalse(workday_status(self.store.data(),'PRJ-DEMO','2028-02-01')['verified'])

    def test_confirmed_but_unknown_details_remain_pending(self):
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        confirm_day(self.store,'PRJ-DEMO','PER-VENDOR','2026-11-11',values={'source':'incomplete-reply'},actor='member',now=NOW)
        self.assertEqual(plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)['eligible'][0]['periods'],['AM','PM'])

    def test_leave_response_must_be_explicit_and_changed_leave_has_evidence(self):
        ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        args=dict(store=self.store,project_id='PRJ-DEMO',person_id='PER-VENDOR',day='2026-11-11',periods=('AM',),actor='member',now=NOW)
        confirm_day(**args,values={'work_location':'remote','allocation_mode':'shared','available_hours':0,'source':'reply'})
        pending=plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)['eligible'][0]
        self.assertEqual(pending['missing_fields']['AM'],['future_leave_change'])
        with self.assertRaises(ValidationError):
            confirm_day(**args,values={'future_leave_change':'changed','source':'reply'})
        confirm_day(**args,values={'future_leave_change':'changed','future_leave_note':'下周三上午计划休假，待确认交接','source':'reply'})
        self.assertEqual(plan_daily_confirmations(self.store.data(),'PRJ-DEMO',NOW)['eligible'][0]['periods'],['PM'])

    def test_promotion_is_atomic_and_old_item_stops_execution(self):
        self.store.apply_batch({'operations':[{'id':'OPS-X','project_id':'PRJ-DEMO','title':'新增配置'}],
            'registers':[{'id':'REG-PROMOTE','kind':'change','title':'纳入项目','project_id':'PRJ-DEMO','owner_id':'PER-PM','decision_by_id':'PER-SP','status':'approved','decision':'批准纳入项目','decided_at':NOW.isoformat()}]},now=NOW)
        result=self.store.promote_operation('OPS-X',version=1,decision_id='REG-PROMOTE',actor='pm',now=NOW)
        row=self.store.data()['operations'][0]
        self.assertEqual(row['status'],'cancelled')
        self.assertEqual(row['promoted_wbs_id'],result['work_item_id'])
        retry=self.store.promote_operation('OPS-X',version=1,decision_id='REG-PROMOTE',actor='pm',now=NOW)
        self.assertEqual(retry['work_item_id'],result['work_item_id'])
        with self.assertRaises(ValidationError): self.store.apply_batch({'operations':[dict(row,title='不能并行维护')]},now=NOW)

    def test_unknown_year_not_fabricated(self):
        project=self.store.data()['projects'][0]
        self.store.apply_batch({'projects':[dict(project,reminder_end='2028-02-10')]},now=NOW)
        result=ensure_project_calendar(self.store,'PRJ-DEMO',now=NOW)
        self.assertIn(2028,result['pending_years'])
        self.assertFalse(workday_status(self.store.data(),'PRJ-DEMO','2028-02-01')['verified'])
    def test_capacity_counts_operations_once_unknown_stays_unknown(self):
        self.store.apply_batch({'resource_weeks':[{'project_id':'PRJ-DEMO','person_id':'PER-VENDOR','week_start':'2026-11-09','net_available_hours':6,'project_hours':4,'operations_hours':2,'buffer_hours':0,'confirmed_at':NOW.isoformat(),'confirmed_by_id':'PER-VENDOR'}]},now=NOW)
        self.assertEqual(capacity_summary(self.store.data(),'PRJ-DEMO')['people'][0]['remaining_hours'],0)
        row=self.store.data()['resource_weeks'][0]; self.store.apply_batch({'resource_weeks':[dict(row,operations_hours=None)]},now=NOW)
        self.assertIsNone(capacity_summary(self.store.data(),'PRJ-DEMO')['people'][0]['remaining_hours'])
    def test_independent_operations_deletion_conflict_and_refs(self):
        self.store.apply_batch({'operations':[{'id':'OPS-1','title':'配置调整','project_id':'PRJ-DEMO'}]},now=NOW)
        with self.assertRaises(ValidationError): self.store.delete_record('operations','OPS-1',version=2,actor='pm',now=NOW)
        self.store.apply_batch({'registers':[{'id':'REG-OPS','kind':'issue','title':'影响','project_id':'PRJ-DEMO','operation_id':'OPS-1','owner_id':'PER-PM'}]},now=NOW)
        with self.assertRaises(ValidationError): self.store.delete_record('operations','OPS-1',version=1,actor='pm',now=NOW)
        self.store.delete_record('registers','REG-OPS',version=1,actor='pm',now=NOW)
        self.store.delete_record('operations','OPS-1',version=1,actor='pm',now=NOW)
        self.assertEqual(self.store.data()['operations'],[])
    def test_workbook_roundtrip_and_independent_operations_entry(self):
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
        from pm_core.excel_io import export_workbook, read_workbook, excel_fields
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'roundtrip.xlsx'
            export_workbook(self.store.data(),path,month='2026-11',calendar_id='CAL-CN-2026',as_of=NOW.isoformat())
            self.assertEqual(read_workbook(path),{})
            wb=load_workbook(path)
            try:
                ws=wb['运维与优化事项']; fields=[f.name for f in excel_fields('operations')]
                ws.cell(7,fields.index('title')+1).value='新增独立配置'
                ws.cell(7,fields.index('delay_reason')+1).value='窗口冲突'
                visible=[f.name for i,f in enumerate(excel_fields('wbs'),1) if not wb['WBS总表'].column_dimensions[get_column_letter(i)].hidden]
                self.assertEqual(len(visible),12)
                wb.save(path)
            finally:wb.close()
            batch=read_workbook(path)
            self.assertEqual(batch['operations'][0]['project_id'],'PRJ-DEMO')
            self.store.apply_batch(batch,now=NOW)
            self.assertEqual(self.store.data()['operations'][0]['title'],'新增独立配置')

    def test_v6_migration_is_idempotent_and_snapshots_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'old.sqlite3'
            with Store(path) as store:
                store.apply_batch(demo_data(),now=NOW)
                store.conn.execute("INSERT INTO wbs(id,title,node_kind,scope,work_type,status,delay_risk,version,is_acceptance_node) VALUES('OLD-OPS','旧零星项','task','adhoc','action','intake','none',1,0)");store.conn.commit()
                store.conn.execute("INSERT INTO snapshots VALUES('OLD-SNAP','旧快照','2026-11-01T00:00:00Z','2026-11-01T00:00:00Z',0,?)",('{"legacy":true}',))
                store.conn.execute('PRAGMA user_version=6');store.conn.commit()
            with Store(path) as store:
                self.assertEqual(store.conn.execute('PRAGMA user_version').fetchone()[0],7)
                old=next(r for r in store.data()['wbs'] if r['id']=='OLD-OPS')
                self.assertEqual(old['migrated_operation_id'],'OLD-OPS')
                with self.assertRaises(ValidationError):store.apply_batch({'wbs':[dict(old,title='不能改')]},now=NOW)
                self.assertEqual(len(store.data()['operations']),1)
                self.assertEqual(store.read_snapshot('OLD-SNAP')['data'],{'legacy':True})
            with Store(path) as store:self.assertEqual(len(store.data()['operations']),1)
            self.assertEqual(len(list(Path(folder).glob('*.before-v7-*.bak'))),1)

if __name__=='__main__':unittest.main()
