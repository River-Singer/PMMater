"""Design tokens stay the only place colors are defined for standalone reports."""
import json
import re
from pathlib import Path
import unittest

from pm_core import design_tokens as tokens
from pm_core.project_report import REPORT_CSS, COLORS, WORK_COLORS, PRIORITY_COLORS

ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r'#[0-9a-fA-F]{3,8}\b')


class DesignTokenTests(unittest.TestCase):
    def test_exported_tokens_match_the_module(self):
        self.assertEqual([], tokens.stale(), "token 导出未同步；请运行 python -m pm_core.design_tokens")

    def test_report_css_defines_every_token_and_has_no_other_color_literal(self):
        root = tokens.css_root()
        self.assertIn(root, REPORT_CSS)
        rest = REPORT_CSS.replace(root, '')
        self.assertEqual([], HEX.findall(rest), "REPORT_CSS 只能用 var(--token)，不能写色值")
        self.assertIsNone(re.search(r'\brgba?\(|\bhsla?\(', rest))
        used = set(re.findall(r'var\((--[a-z0-9-]+)\)', rest))
        local = {'--tick-width', '--lane-color', '--k'}  # set inline per element, not design tokens
        self.assertEqual(set(), used - set(tokens.css_vars()) - local, "使用了未定义的变量")

    def test_renderers_take_inline_colors_from_tokens(self):
        self.assertEqual(tokens.chart_colors('work_status'), COLORS)
        self.assertEqual(tokens.chart_colors('work_type'), WORK_COLORS)
        self.assertEqual(tokens.CHART['priority']['ungraded'], PRIORITY_COLORS[None])
        for name in ('operating.py', 'weekly.py', 'management_text.py', 'project_report.py'):
            source = (ROOT / 'pm_core' / name).read_text(encoding='utf-8')
            self.assertEqual([], [h for h in HEX.findall(source) if len(h) in (4, 7, 9)], name + " 不应写色值")

    def test_status_marks_stay_distinguishable(self):
        # Waterfall status marks map to status tokens; values validated for red-green color blindness.
        values = tokens.css_vars()
        self.assertEqual(('#d03b3b', '#e5a50a', '#2ba68c'), (values['--critical'], values['--warning'], values['--good']))
        self.assertIn('.forecast.risk{background:repeating-linear-gradient(135deg,var(--warning)', REPORT_CSS)
        self.assertIn('.actual.late_done{background:var(--good-bg);border:3px double var(--critical)', REPORT_CSS)

    def test_font_sizes_stay_on_the_type_scale(self):
        sizes = {int(x) for x in re.findall(r'font-size:(\d+)px', REPORT_CSS)}
        self.assertEqual(set(), sizes - set(tokens.TYPE_SCALE), "字号不在 typography.scale 中")
        self.assertIn('font:14px/1.6 var(--font)', REPORT_CSS)

    def test_authoring_guide_only_names_real_tokens(self):
        guide = (ROOT / 'docs' / 'HTML报告制作说明.md').read_text(encoding='utf-8')
        named = set(re.findall(r'`(--[a-z0-9-]+)`', guide)) - {'--check'}  # --check is a command flag
        self.assertGreater(len(named), 20)
        self.assertEqual(set(), named - set(tokens.css_vars()), "制作说明提到了不存在的 token")
        self.assertIn('references/report-design-tokens.json', guide)

    def test_export_is_self_describing(self):
        data = json.loads(tokens.default_export_path().read_text(encoding='utf-8'))
        self.assertEqual('#d03b3b', data['color']['status']['critical']['solid']['$value'])
        self.assertEqual('--critical', data['color']['status']['critical']['solid']['$extensions']['cssVar'])
        self.assertEqual('开发功能', data['chart']['work_type']['feature']['$description'])
        self.assertTrue(data['usage_rules'])


if __name__ == '__main__':
    unittest.main()
