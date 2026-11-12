"""Demo HTML must be what the shipped renderers produce, not a hand-edited copy."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pm_core.demo_reports import stale_outputs, render_demo, DEMO_CUTOFF
from pm_core.management_text import export_schedule_audit_report, schedule_paragraphs

DEMO = Path(__file__).resolve().parents[1] / "demo"


class DemoReportsTests(unittest.TestCase):
    def test_demo_outputs_match_current_renderers(self):
        self.assertEqual([], stale_outputs(DEMO), "演示文件与当前渲染代码不一致；请运行 python -m pm_core.demo_reports")

    def test_regeneration_never_modifies_demo_database(self):
        before = (DEMO / "demo.sqlite3").read_bytes()
        with TemporaryDirectory() as out:
            render_demo(DEMO, out)
            summary = (Path(out) / "本周项目汇报.html").read_text(encoding="utf-8")
        self.assertEqual(before, (DEMO / "demo.sqlite3").read_bytes())
        self.assertIn("2026-11-11 17:00", summary)
        self.assertEqual("2026-11-11T09:00:00+00:00", DEMO_CUTOFF)

    def test_overview_leads_with_dates_kpis_and_attention_list(self):
        source = (DEMO / "本周项目汇报.html").read_text(encoding="utf-8")
        overview = source.split('data-section="1"')[0]
        for label in ("项目完成日期", "未结延期", "延期风险", "P0 未完成", "当前最需关注", "预测延后 1 个工作日"):
            self.assertIn(label, overview)
        attention = overview.split("当前最需关注")[1]
        self.assertLess(attention.index("采购对账接口异常复核"), attention.index("导出中文编码缺陷修复"), "P0 排在最前")

    def test_date_change_page_keeps_before_after_and_escapes_text(self):
        audit = json.loads((DEMO / "日期变更审议.json").read_text(encoding="utf-8"))
        audit["signals"][0]["title"] = "<b>注入</b>"
        with TemporaryDirectory() as out:
            path = Path(out) / "audit.html"
            export_schedule_audit_report(audit, path, title="<i>标题</i>")
            html = path.read_text(encoding="utf-8")
        self.assertNotIn("<b>注入</b>", html)
        self.assertIn("&lt;b&gt;注入&lt;/b&gt;", html)
        self.assertNotIn("<i>标题</i>", html)
        self.assertIn("2026-11-05", html)
        self.assertIn("2026-11-13", html)
        self.assertIn('class="field-change deviation reduced"', html)
        # Plain-text paragraphs for documents keep the complete sentence.
        self.assertTrue(any(p.startswith("同一对照时点的完成偏差：6 → 0") for p in schedule_paragraphs(audit)))

    def test_no_history_audit_page_states_unknown_coverage(self):
        with TemporaryDirectory() as out:
            path = Path(out) / "none.html"
            export_schedule_audit_report(None, path)
            html = path.read_text(encoding="utf-8")
        self.assertIn("历史覆盖未知", html)
        self.assertNotIn("需核对 0 项", html)


if __name__ == "__main__":
    unittest.main()
