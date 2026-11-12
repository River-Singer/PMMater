"""An edited Excel must change durable facts and derived reports consistently."""
import contextlib
from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from openpyxl import load_workbook
from pm_core.cli import main
from pm_core.excel_io import export_workbook,read_workbook,excel_fields
from pm_core.fixtures import demo_data,CALENDAR_ID
from pm_core.reports import summary
from pm_core.project_report import export_project_report
from pm_core.schema import TABLES
from pm_core.store import Store,ValidationError

class EndToEndTests(unittest.TestCase):
    def test_overdue_uses_project_and_service_timezone(self):
        data=demo_data()
        self.assertEqual(summary(data,'2026-11-05T15:30:00+00:00')['overdue'],0)
        self.assertEqual(summary(data,'2026-11-05T16:30:00+00:00')['overdue'],1)

    def test_predecessor_reopen_surfaces_impact_without_erasing_history(self):
        data=demo_data()
        predecessor=next(w for w in data['wbs'] if w['id']=='WI-001')
        predecessor['status']='in_progress';predecessor['actual_finish']=None
        impacts=summary(data,'2026-11-05T14:00:00+00:00')['dependency_impacts']
        self.assertEqual(impacts[0]['successor_id'],'WI-002')
    def test_excel_database_month_history_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);db=p/'pm.sqlite3';book=p/'edit.xlsx'
            with Store(db) as store:
                store.apply_batch(demo_data(),actor='fixture',now='2026-11-05T14:00:00+00:00')
                before=store.data()
                export_workbook(before,book,month='2026-11',calendar_id=CALENDAR_ID,as_of='2026-11-05T14:00:00+00:00')
                self.assertEqual(read_workbook(book),{})
                wb=load_workbook(book);ws=wb['WBS总表']
                cols={f.name:i+1 for i,f in enumerate(excel_fields('wbs'))}
                row=next(r for r in range(7,ws.max_row+1) if ws.cell(r,1).value=='WI-002')
                ws.cell(row,cols['forecast_finish']).value=datetime(2026,11,13)
                ws.cell(row,cols['next_action']).value='按新预测更新剩余接口安排'
                grid=wb['计划月历']
                row=next(r for r in range(7,grid.max_row+1) if grid.cell(r,1).value=='PER-DEV' and grid.cell(r,3).value=='上午')
                col=next(c for c in range(4,grid.max_column+1) if grid.cell(5,c).value.date().isoformat()=='2026-11-09')
                grid.cell(row,col).value='其他工作安排'
                wb.save(book);wb.close()
                batch=read_workbook(book)
                self.assertEqual(len(batch['wbs']),1)
                self.assertEqual(len(batch['day_records']),1)
                self.assertEqual(store.apply_batch(batch,actor='PM',now='2026-11-05T14:01:00+00:00')['changed'],2)
                after=store.data()
                old_oct=[r for r in before['day_records'] if r['date'].startswith('2026-10')]
                self.assertEqual(old_oct,[r for r in after['day_records'] if r['date'].startswith('2026-10')])
                report=p/'report.html';export_project_report(after,report,as_of='2026-11-05T14:01:00+00:00',demo=True)
                content=report.read_text(encoding='utf-8')
                self.assertIn('2026-11-13',content)
                self.assertIn('按新预测更新剩余接口安排',content)
                self.assertIn('本周开发与交付',content)
                self.assertNotIn('产品与服务日常队列',content)
                self.assertIn('PER-',json.dumps(after))
                with self.assertRaises(ValidationError) as caught:
                    store.apply_batch(batch,actor='stale-PM',now='2026-11-05T14:02:00+00:00')
                self.assertEqual(caught.exception.errors[0]['code'],'version_conflict')
                self.assertEqual(len(store.history('wbs','WI-002')),2)
            with Store(db) as reopened:
                self.assertEqual(next(r for r in reopened.data()['wbs'] if r['id']=='WI-002')['forecast_finish'],'2026-11-13')

    def test_cli_writes_structured_excel_error_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);db=p/'pm.sqlite3';book=p/'bad.xlsx';errors=p/'errors.json'
            with Store(db) as store:
                store.apply_batch(demo_data(),actor='fixture',now='2026-11-05T14:00:00+00:00')
                export_workbook(store.data(),book,month='2026-11',calendar_id=CALENDAR_ID,as_of='2026-11-05T14:00:00+00:00')
                count=len(store.history())
            wb=load_workbook(book);wb['WBS总表']['B7']='=1+1';wb.save(book);wb.close()
            with contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(io.StringIO()):
                code=main(['import-excel','--db',str(db),'--file',str(book),'--errors',str(errors)])
            self.assertEqual(code,2)
            result=json.loads(errors.read_text(encoding='utf-8'))
            self.assertFalse(result['ok'])
            self.assertEqual(result['errors'][0]['sheet'],'WBS总表')
            with Store(db) as store: self.assertEqual(len(store.history()),count)

if __name__=='__main__': unittest.main()
