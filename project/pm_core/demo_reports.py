"""Rebuild the fictional demo HTML from the same renderers used in production.

Run from the package root:

    python -m pm_core.demo_reports            # rewrite changed demo outputs from demo facts
    python -m pm_core.demo_reports --check    # exit 1 if demo files differ from current code

Inputs are the fixed fictional capture in ``demo/``: ``demo_facts.json``, the
event history in ``demo.sqlite3`` (read from a temporary copy, never modified)
and the separate date-change example ``日期变更审议.json``. The cutoff is fixed at
2026-11-11 17:00 Asia/Shanghai so regenerated files describe the same snapshot.
Historical ``weekly_reports/`` snapshots are intentionally not rebuilt.
"""
from pathlib import Path
import argparse
import json
import shutil
import sys
import tempfile

DEMO_CUTOFF = "2026-11-11T09:00:00+00:00"
DATE_EXAMPLE_TITLE = "日期变更审议 · 虚构示例"
DATE_EXAMPLE_NOTE = "独立示例，不代表主演示库或真实人员曾改期。来源为同目录「日期观测来源.json」。"
OUTPUTS = ("本周项目汇报.html", "持续跟进与延期审议.html", "月度管理复核.html", "月度管理复核.json", "日期变更审议.html")


def _demo_dir():
    return Path(__file__).resolve().parents[1] / "demo"


def render_demo(demo_dir, out_dir):
    """Write every current demo output for the fixed fictional cutoff into out_dir."""
    from .management_text import export_schedule_audit_report
    from .operating import export_management_report, management_review
    from .project_report import export_project_report
    from .schedule_audit import audit_schedule_changes
    from .store import Store
    demo_dir, out_dir = Path(demo_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads((demo_dir / "demo_facts.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "demo.sqlite3"
        shutil.copyfile(demo_dir / "demo.sqlite3", copy)
        with Store(copy) as store:
            history = store.history()
    audit = audit_schedule_changes(data, history=history, now=DEMO_CUTOFF)
    export_project_report(data, out_dir / "本周项目汇报.html", as_of=DEMO_CUTOFF, kind="summary", demo=True, schedule_audit=audit)
    export_project_report(data, out_dir / "持续跟进与延期审议.html", as_of=DEMO_CUTOFF, kind="review", demo=True, schedule_audit=audit)
    export_management_report(data, out_dir / "月度管理复核.html", as_of=DEMO_CUTOFF, period="month", schedule_audit=audit)
    review = management_review(data, now=DEMO_CUTOFF, period="month", schedule_audit=audit)
    (out_dir / "月度管理复核.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    date_audit = json.loads((demo_dir / "日期变更审议.json").read_text(encoding="utf-8"))
    export_schedule_audit_report(date_audit, out_dir / "日期变更审议.html", title=DATE_EXAMPLE_TITLE, note=DATE_EXAMPLE_NOTE)
    return [out_dir / name for name in OUTPUTS]


def _same(current, fresh):
    if not current.is_file():
        return False
    # Text-mode reads normalise Windows/Unix newlines. JSON is compared by value:
    # audit records may list keys in a different (hash-seed dependent) order.
    a, b = current.read_text(encoding="utf-8"), fresh.read_text(encoding="utf-8")
    return json.loads(a) == json.loads(b) if current.suffix == ".json" else a == b


def _stale(demo_dir, rendered):
    return [name for name in OUTPUTS if not _same(Path(demo_dir) / name, Path(rendered) / name)]


def stale_outputs(demo_dir=None):
    """Names of demo files whose content differs from what the current code renders."""
    demo_dir = Path(demo_dir or _demo_dir())
    with tempfile.TemporaryDirectory() as scratch:
        render_demo(demo_dir, scratch)
        return _stale(demo_dir, scratch)


def refresh_demo(demo_dir=None):
    """Rewrite only the demo files whose content changed; returns their names."""
    demo_dir = Path(demo_dir or _demo_dir())
    with tempfile.TemporaryDirectory() as scratch:
        render_demo(demo_dir, scratch)
        changed = _stale(demo_dir, scratch)
        for name in changed:
            shutil.copyfile(Path(scratch) / name, demo_dir / name)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description="用当前渲染代码重建虚构演示报告")
    parser.add_argument("--demo-dir", default=str(_demo_dir()))
    parser.add_argument("--check", action="store_true", help="只检查演示文件是否与当前代码一致")
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_outputs(args.demo_dir)
        print(json.dumps({"ok": not stale, "stale": stale}, ensure_ascii=False))
        return 1 if stale else 0
    changed = refresh_demo(args.demo_dir)
    print(json.dumps({"ok": True, "updated": changed, "cutoff": DEMO_CUTOFF}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
