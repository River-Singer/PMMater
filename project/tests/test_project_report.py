"""Report-level safeguards against misleading dates, counts and acceptance."""
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pm_core.fixtures import demo_data
from pm_core.project_report import export_project_report, waterfall_calendar, REPORT_SCRIPT, REPORT_CSS


class ReportHTML(HTMLParser):
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self, source):
        super().__init__()
        self.stack = []
        self.rows = {'focus': {}, 'full': {}}
        self.groups = {'focus': {}, 'full': {}}
        self.views = {}
        self.weeks = []
        self.links = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        scope = self.stack[-1][1] if self.stack else None
        work = self.stack[-1][2] if self.stack else None
        scope = a.get('data-trace-view', scope)
        if 'data-trace-view' in a:
            self.views[scope] = a
        if 'data-group-id' in a:
            self.groups[scope][a['data-group-id']] = a
        if 'data-week' in a:
            self.weeks.append((scope, a['data-week'], a['data-workdays']))
        if 'data-work-id' in a:
            work = a['data-work-id']
            self.rows[scope][work] = {'delay':a['data-delay-state'],'marks':[]}
        if tag == 'i' and work and scope:
            self.rows[scope][work]['marks'].append(set(a.get('class','').split()))
        if tag == 'a':
            self.links.append(a.get('href'))
        if tag not in self.VOID:
            self.stack.append((tag,scope,work))

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break


class ReportFactsTests(unittest.TestCase):
    def setUp(self):
        self.data = demo_data()
        self.work = {w['id']:w for w in self.data['wbs']}
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def render(self, as_of='2026-11-05T16:30:00+00:00'):
        path = Path(self.tmp.name)/'project_report.html'
        result = export_project_report(self.data,path,as_of=as_of,
            timezone_name='America/Vancouver',demo=True)
        source = path.read_text(encoding='utf-8')
        return result,source,ReportHTML(source)

    def test_project_midnight_focus_keeps_parents_but_counts_only_leaves(self):
        result,source,html = self.render()
        self.assertEqual(result['timezone'],'Asia/Shanghai')
        self.assertEqual(result['window_start'],'2026-11-06')
        self.assertEqual(result['window_end'],'2026-11-08')
        self.assertNotIn('WI-001',html.rows['focus'])
        self.assertNotIn('WI-002-1',html.rows['focus'])
        self.assertIn('WI-002',html.rows['focus'])
        self.assertIn('WI-002-2',html.rows['focus'])
        self.assertIn('WI-005',html.rows['focus'])
        task_rows=[w for w in self.data['wbs'] if w.get('node_kind')=='task']
        self.assertEqual(len(html.rows['full']),len(task_rows))
        self.assertEqual(result['leaf_counts']['leaf_total'],len(task_rows)-1)
        self.assertEqual(result['focus_counts']['total'],len(html.rows['focus'])-1)
        self.assertIn('今天至本周日，含未结逾期结转',source)

    def test_p0_is_on_time_while_actual_overdue_and_risk_are_distinct(self):
        self.work['WI-007']['forecast_finish'] = '2026-11-08'
        _,_,html = self.render()
        rows = html.rows['focus']
        self.assertEqual(rows['WI-007']['delay'],'on_track')
        self.assertFalse(any('overdue' in classes or 'risk' in classes for classes in rows['WI-007']['marks']))
        self.assertEqual(rows['WI-005']['delay'],'overdue')
        self.assertTrue(any('forecast' in classes and 'overdue' in classes for classes in rows['WI-005']['marks']))
        self.assertEqual(rows['WI-003']['delay'],'risk')
        self.assertTrue(any('forecast' in classes and 'risk' in classes for classes in rows['WI-003']['marks']))

    def test_late_delivery_history_survives_actual_remediation_and_focus_change(self):
        self.work['WI-005'].update(status='done',actual_finish='2026-11-05',
            completion_evidence='QA已核验编码结果',remediation_actual='2026-11-05')
        result,source,html = self.render()
        self.assertNotIn('WI-005',html.rows['focus'])
        row = html.rows['full']['WI-005']
        self.assertEqual(row['delay'],'late_done')
        self.assertTrue(any('actual' in classes and 'late_done' in classes for classes in row['marks']))
        self.assertEqual(result['leaf_counts']['late_done'],1)
        self.assertIn('QA已核验编码结果',source)
        self.assertIn('补救日期不抹掉历史延期',source)

    def test_todo_completion_does_not_invent_acceptance_or_actual_effort(self):
        for wid in ('WI-002-2','WI-002-3'):
            self.work[wid].update(status='done',actual_start='2026-11-05',
                actual_finish='2026-11-05',completion_evidence='执行项已确认')
        self.work['WI-002']['actual_person_days'] = 0
        _,source,html = self.render()
        self.assertIn('3 / 3 · 100.0%',source)
        self.assertIn('待验收',source)
        self.assertIn('接口设计说明待验收版',source)
        self.assertIn('0 人天',source)
        self.assertIn('出勤不等于该任务投入人天',source)
        self.assertEqual(len(html.rows['full']),11)

    def test_report_excludes_personal_contacts_and_comments_escaped(self):
        pm = next(p for p in self.data['people'] if p['id']=='PER-PM')
        pm['feishu_url'] = 'javascript:alert(1)'
        self.data['deliverables'][0]['acceptance_comments'] = '<script>alert(2)</script>'
        _,source,html = self.render()
        self.assertFalse(any(href.startswith('javascript:') for href in html.links if href))
        self.assertIn('https://example.invalid/project/requirements',html.links)
        self.assertNotIn('PM飞书通讯地址',source)
        self.assertNotIn('example.invalid/feishu',source)
        self.assertNotIn('@example.invalid',source)
        self.assertIn('&lt;script&gt;alert(2)&lt;/script&gt;',source)
        self.assertNotIn('<script>alert(2)</script>',source)

    def test_collapsed_first_level_keeps_leaf_progress_and_separate_acceptance(self):
        _, source, html = self.render()
        self.assertIn('open', html.groups['full']['WBS-ROOT'])
        self.assertNotIn('open', html.groups['full']['WBS-BUILD'])
        full = html.groups['full']['WI-002']
        focus = html.groups['focus']['WI-002']
        self.assertEqual((full['data-leaf-count'], full['data-done-count']), ('3', '1'))
        self.assertEqual((focus['data-leaf-count'], focus['data-done-count']), ('2', '0'))
        self.assertEqual(html.groups['full']['WBS-ROOT']['data-leaf-count'], '10')
        self.assertIn('子项最晚预计', source)
        self.assertIn('交付物验收', source)
        self.assertIn('不是工时加权', source.replace('不等于工时加权', '不是工时加权'))
        self.assertIn('data-trace-expand="summary"', source)
        self.assertIn("[data-trace-view]:not([hidden])", REPORT_SCRIPT)

    def test_missing_dates_do_not_create_zero_length_bars_or_false_completion(self):
        self.work['WI-002-1'].update(actual_finish=None, completion_evidence=None)
        self.work['WI-002-2'].update(baseline_start=None, baseline_finish=None,
            forecast_start=None, forecast_finish=None, actual_start=None, actual_finish=None,
            remediation_expected=None, remediation_actual=None, status='intake')
        _, _, html = self.render()
        self.assertEqual(html.groups['full']['WI-002']['data-done-count'], '0')
        self.assertFalse(any({'original','forecast','actual'} & marks for marks in html.rows['full']['WI-002-2']['marks']))

    def test_full_scope_uses_project_start_weeks_while_focus_keeps_daily_axis(self):
        _, source, html = self.render()
        self.assertTrue(html.weeks)
        self.assertTrue(all(scope == 'full' for scope, _, _ in html.weeks))
        self.assertEqual(html.weeks[0][1], 'WK01')
        self.assertIn('WK按项目起始所在周编号', source)
        self.assertIn('日历待核验', source)
        self.assertIn('main{width:96%', REPORT_CSS)
        self.assertIn('hidden', html.views['focus'])
        self.assertNotIn('hidden', html.views['full'])
        # Light Waterfall canvas shared with the rest of the report; marks take the status tokens.
        self.assertIn('.trace-panel{background:var(--card)', REPORT_CSS)
        self.assertIn('.actual{height:7px;top:48px;background:var(--good)}', REPORT_CSS)
        self.assertIn('.forecast.overdue{background:var(--critical)', REPORT_CSS)
        self.assertIn('--card:#ffffff', REPORT_CSS)
        self.assertNotIn('#080808', REPORT_CSS)

    def test_workweeks_keep_partial_holiday_makeup_and_unknown_calendar_weeks(self):
        from datetime import date, timedelta
        p = dict(self.data['projects'][0], baseline_start='2026-10-04',
                 baseline_finish='2026-10-25', forecast_finish='2026-10-25')
        data = dict(self.data, projects=[p], wbs=[], calendar_days=[], calendar_overrides=[],
                    calendar_years=[{'calendar_id':p['calendar_id'], 'year':2026,
                        'status':'complete', 'source_url':'https://example.invalid/calendar',
                        'verified_at':'2026-01-01T00:00:00+08:00', 'exception_hash':'test'}])
        for offset in range(15):
            day = date(2026,10,4)+timedelta(days=offset)
            workday = date(2026,10,5) <= day <= date(2026,10,9)
            data['calendar_days'].append({'calendar_id':p['calendar_id'], 'date':day.isoformat(),
                'is_workday':workday, 'day_type':'workday' if workday else 'holiday'})
        data['calendar_overrides'] = [
            {'calendar_id':p['calendar_id'], 'date':'2026-10-08', 'status':'confirmed', 'person_id':None, 'is_workday':False},
            {'calendar_id':p['calendar_id'], 'date':'2026-10-11', 'status':'confirmed', 'person_id':None, 'is_workday':True},
            {'calendar_id':p['calendar_id'], 'date':'2026-10-05', 'status':'confirmed', 'person_id':'PER-PM', 'is_workday':False}]
        calendar = waterfall_calendar(data)
        self.assertEqual([w['label'] for w in calendar['weeks']], ['WK01','WK02','WK03','WK04'])
        self.assertEqual([w['workdays'] for w in calendar['weeks']], [0,5,0,None])
        self.assertEqual(calendar['weeks'][0]['range_start'], '2026-10-04')
        self.assertEqual(calendar['verified_workweeks'], 1)
        self.assertEqual(calendar['pending_weeks'], 1)


if __name__ == '__main__':
    unittest.main()
