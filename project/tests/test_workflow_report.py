import copy
from html.parser import HTMLParser
import unittest

from pm_core.fixtures import demo_data
from pm_core.management_text import _workflow_parts, render_workflow_review, workflow_paragraphs, management_paragraphs
from pm_core.operating import management_review, render_management_section


class TextReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)


class CollapsedTextReader(TextReader):
    def __init__(self):
        super().__init__()
        self.details = []

    def handle_starttag(self, tag, attrs):
        if tag == 'details':
            self.details.append({'closed': 'open' not in dict(attrs), 'summary': False})
        elif tag == 'summary' and self.details:
            self.details[-1]['summary'] = True

    def handle_endtag(self, tag):
        if tag == 'details':
            self.details.pop()
        elif tag == 'summary' and self.details:
            self.details[-1]['summary'] = False

    def handle_data(self, value):
        if not any(item['closed'] and not item['summary'] for item in self.details):
            self.parts.append(value)


class WorkflowReportTests(unittest.TestCase):
    def scenario(self):
        data = {'people': [{'id': 'P', 'name': '负责人甲'}], 'wbs': [
            {'id': 'ROOT', 'title': '总体交付'},
            {'id': 'A', 'title': '一级A', 'parent_id': 'ROOT'},
            {'id': 'B', 'title': '一级B', 'parent_id': 'ROOT'},
            {'id': 'L1', 'title': '叶子一', 'parent_id': 'A'},
            {'id': 'L2', 'title': '叶子二', 'parent_id': 'B'}]}
        impact = {'task_ids': ['A', 'B'], 'affected_leaf_ids': ['L1', 'L2'],
                  'ancestors': [{'id': 'A', 'title': '一级A'}, {'id': 'B', 'title': '一级B'}],
                  'successors': [{'id': 'NEXT', 'title': '后续验收'}],
                  'project_assessment': '项目日期影响待评估，尚无任务工时依据',
                  'date_evidence': [{'title': '交付节点', 'comparison_date': '2026-11-20', 'forecast_finish': '2026-11-23'}]}
        row = {'id': 'C1', 'kind': 'register', 'title': '物资 <script>错误</script>',
               'source_table': 'registers', 'source_id': 'R1', 'source_url': 'javascript:alert(1)',
               'owner_id': 'P', 'work_item_ids': ['A', 'B'], 'reason': '设备尚未落实',
               'deadline': '2026-11-16', 'deadline_basis': '待决定的协调时点',
               'next_action': '确认替代方案，不代表已批准', 'assessment': 'risk',
               'evidence': {'impact': '覆盖两条分支'}, 'limitations': ['缺到货确认'], 'impact': impact}
        review = {'summary': {'total': 2, 'done': 0, 'open': 2, 'missing_data': 1,
                              'awaiting_feedback': 1, 'ready': 0},
                  'parents': [{'id': 'A', 'leaf_ids': ['L1'], 'counts': {'total': 1, 'done': 0}, 'inconsistent_completion': True}],
                  'tasks': [], 'constraints': [row, dict(row, id='C2', source_id='R2')],
                  'limitations': ['缺少关键路径，不能认定整项目必然延期']}
        return data, review

    def test_shared_evidence_and_unique_leaf_count(self):
        data, workflow = self.scenario()
        parts = _workflow_parts(data, workflow)
        self.assertEqual(len(parts['resources']), 2)
        self.assertEqual(parts['affected'], 2)  # Same leaves are not counted per source.
        html = render_workflow_review(data, workflow)
        reader = TextReader(); reader.feed(html)
        visible = ''.join(reader.parts)
        paragraphs = '\n'.join(workflow_paragraphs(data, workflow))
        for card in parts['resources']:
            for label, value in card['fields']:
                self.assertIn(value, visible)
                self.assertIn(value, paragraphs)
        self.assertIn('叶子一 [L1] → 一级A [A] → 总体交付 [ROOT]', visible)
        self.assertIn('叶子二 [L2] → 一级B [B] → 总体交付 [ROOT]', visible)
        self.assertNotIn('一级A [A] → 一级B [B]', visible)
        self.assertIn('父级已报完成', visible)
        self.assertIn('资源直接涉及叶子 1；潜在后继叶子 0；关联资源风险 2 项', visible)
        self.assertNotIn('<script>', html)
        self.assertNotIn('href="javascript:', html)

    def test_unmapped_resource_stays_visible_and_unknown_is_not_ready(self):
        data, workflow = self.scenario()
        row = workflow['constraints'][0]
        row.update(assessment='needs_mapping', work_item_ids=[], impact={}, deadline=None)
        workflow['constraints'] = [row]
        output = render_workflow_review(data, workflow)
        self.assertIn('待定位风险', output)
        self.assertIn('设备尚未落实', output)
        self.assertIn('最晚落实/核对时点：待确认', output)
        self.assertEqual(_workflow_parts(data, workflow)['affected'], 0)
        self.assertIn('未登记的资源需求', output)
        self.assertIn('不能据此断言资源已落实', workflow_paragraphs(data, None)[0])

    def test_review_keeps_denominator_and_standalone_section(self):
        data = demo_data()
        before = copy.deepcopy(data)
        review = management_review(data, now='2026-11-11T10:00:00+08:00')
        self.assertEqual(review['workflow']['summary']['total'], review['counts']['wbs']['total'])
        self.assertEqual(data, before)
        html = render_management_section(data, as_of='2026-11-11T10:00:00+08:00')
        self.assertLess(html.index('延期与补救审议'), html.index('延期风险与资源落实'))
        self.assertIn('最小子任务', html)
        text = '\n'.join(management_paragraphs(data, review))
        self.assertIn('延期风险与资源落实', text)
        self.assertIn('本人反馈记录不是正式签收', text)

    def test_operations_resource_is_visible_but_does_not_count_as_project(self):
        data, workflow = self.scenario()
        data['operations'] = [{'id': 'OPS1', 'title': '日常配置维护'}]
        row = workflow['constraints'].pop()
        row.update(scope='operations', operation_ids=['OPS1'], work_item_ids=[], impact={},
                   evidence={'status': 'blocked', 'expected_ready': '2026-11-20'},
                   kind='interface', source_table='interfaces')
        workflow['constraints'] = []
        workflow['operations_constraints'] = [row]
        parts = _workflow_parts(data, workflow)
        self.assertEqual(parts['affected'], 0)
        self.assertEqual(len(parts['resources']), 0)
        html = render_workflow_review(data, workflow)
        self.assertIn('运维资源风险（1', html)
        self.assertIn('日常配置维护', html)
        self.assertIn('当前状态：受阻', html)
        self.assertNotIn('待定位风险', html)
        self.assertIn('不计入项目影响', '\n'.join(workflow_paragraphs(data, workflow)))

    def test_resource_default_view_is_compact_but_full_evidence_is_retained(self):
        data, workflow = self.scenario()
        row = workflow['constraints'][0]
        row['next_action'] = '核实设备到货并回填凭证。' * 15
        html = render_workflow_review(data, workflow)
        collapsed = CollapsedTextReader(); collapsed.feed(html)
        text = ''.join(collapsed.parts)
        self.assertIn('设备尚未落实', text)
        self.assertIn('负责人甲', text)
        self.assertIn('DDL2026-11-16', text)
        self.assertIn('直接涉及 2 个叶子', text)
        self.assertIn('下一步', text)
        self.assertIn('证据与完整影响链', text)
        self.assertNotIn('覆盖两条分支', text)
        self.assertNotIn('叶子一 [L1] → 一级A', text)
        self.assertNotIn(row['next_action'], text)
        paragraphs = '\n'.join(workflow_paragraphs(data, workflow))
        self.assertIn(row['next_action'], paragraphs)
        self.assertIn(row['next_action'], html)
        self.assertIn('覆盖两条分支', html)
        self.assertNotIn('data-resource-evidence open', html)
        row.update(deadline=None, evidence={'expected_ready': '2026-12-31'})
        workflow['constraints'] = [row]
        collapsed = CollapsedTextReader(); collapsed.feed(render_workflow_review(data, workflow))
        self.assertIn('DDL待确认', ''.join(collapsed.parts))
        self.assertNotIn('2026-12-31', ''.join(collapsed.parts))


if __name__ == '__main__':
    unittest.main()
