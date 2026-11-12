"""Design tokens: the single source for colors, type and radii in every generated HTML
report (REPORT_CSS).

    python -m pm_core.design_tokens            # write references/report-design-tokens.json
    python -m pm_core.design_tokens --check    # exit 1 if the export differs from this module

Change a value here, run the command, then `python -m pm_core.demo_reports` to rebuild the demo.
REPORT_CSS must not contain color literals: it references
the CSS variables generated below, and tests reject anything else.
"""
from pathlib import Path
import argparse
import json
import sys

# (group, key, css variable, value, what it is for)
COLOR_TOKENS = (
    ("surface", "page", "--page", "#f4f4f2", "页面底色"),
    ("surface", "card", "--card", "#ffffff", "卡片、表格、Waterfall 任务行的白底"),
    ("surface", "soft", "--soft", "#f8f8f6", "次级底色：表头、空状态、详情面板、Waterfall 表头/父任务行/图例"),
    ("surface", "soft-2", "--soft-2", "#f1f1ee", "进度条槽、中性标签底"),
    ("surface", "hover", "--hover", "#efefec", "行悬停、选中行、环形图底环"),
    ("surface", "band", "--band", "#f3f3f0", "Waterfall 分组汇总行"),
    ("surface", "band-strong", "--band-strong", "#ebebe7", "Waterfall 项目根汇总行"),
    ("border", "line", "--line", "#e6e5e0", "卡片与表格描边"),
    ("border", "line-2", "--line-2", "#efeeea", "行间分隔线"),
    ("border", "line-strong", "--line-strong", "#d6d5cf", "中性强调条、缺失信息虚线框、时间轴刻度线"),
    ("border", "neutral", "--neutral", "#c9c8c1", "中性填充：缓冲容量、详情面板侧边、滚动条"),
    ("border", "grid", "--grid", "#0000000d", "时间轴网格线与悬停阴影（5% 黑）"),
    ("text", "ink", "--ink", "#1d1d1b", "标题、数值、正文；也是选中按钮底色与数据截止线"),
    ("text", "ink-2", "--ink-2", "#4f4e4a", "次级正文"),
    ("text", "muted", "--muted", "#77766f", "说明文字、表头"),
    ("text", "faint", "--faint", "#9a9993", "占位与缺失提示（待确认/待指定）"),
    ("text", "link", "--link", "#2a6fc4", "链接"),
    ("status.critical", "solid", "--critical", "#d03b3b", "已延期·未完成、P0、需优先核对；Waterfall 事实延期条"),
    ("status.critical", "ink", "--critical-ink", "#a82d2d", "critical 状态的文字"),
    ("status.critical", "bg", "--critical-bg", "#fcefee", "critical 状态的标签底"),
    ("status.critical", "line", "--critical-line", "#e7a3ad", "critical 状态的浅描边（P0 区块、延期交付标签）"),
    ("status.serious", "solid", "--serious", "#e0784c", "延期交付：晚于对照日期才完成"),
    ("status.serious", "ink", "--serious-ink", "#a5512e", "serious 状态的文字"),
    ("status.serious", "bg", "--serious-bg", "#fdf2ec", "serious 状态的标签底；容量已排满"),
    ("status.warning", "solid", "--warning", "#e5a50a", "延期风险、待确认、资料缺口；Waterfall 延期风险斜纹"),
    ("status.warning", "ink", "--warning-ink", "#8a6200", "warning 状态的文字"),
    ("status.warning", "bg", "--warning-bg", "#fdf6e3", "warning 状态的标签底"),
    ("status.warning", "line", "--warning-line", "#ecd9a8", "warning 的浅描边与斜纹浅色"),
    ("status.good", "solid", "--good", "#2ba68c", "已确认完成、已验收；Waterfall 实际完成条"),
    ("status.good", "ink", "--good-ink", "#1d7a64", "good 状态的文字"),
    ("status.good", "bg", "--good-bg", "#eaf6f2", "good 状态的标签底；延期交付条的浅底"),
    ("status.info", "solid", "--info", "#4f86d9", "进行中、待决定事项、项目投入容量"),
    ("status.info", "ink", "--info-ink", "#2b5ea8", "info 状态的文字"),
    ("status.info", "bg", "--info-bg", "#eef3fb", "info 状态的标签底"),
    ("waterfall", "baseline", "--wf-baseline", "#8f8e88", "原计划细线"),
    ("waterfall", "acceptance", "--wf-acceptance", "#6e55c4", "验收节点菱形描边"),
    ("waterfall", "remediation", "--wf-remediation", "#3d72c4", "预期/实际补救菱形"),
    ("capacity", "ops", "--cap-ops", "#58b5c3", "容量条中的运维投入（项目投入用 --info，缓冲用 --neutral）"),
)

# Inline chart colors. Identity colors: each entity keeps its color on every report page.
CHART = {
    # Waterfall forecast bars and type dots; "other" covers types without a color of their own (e.g. configuration).
    "work_type": {"feature": "#9b87e7", "improvement": "#58b5c3", "bug": "#d9ae67", "incident": "#899cd8",
                  "request": "#7faed2", "action": "#9eb88d", "other": "#a28ae8"},
    # Leaf-status donut and the Monday commitment bar.
    "work_status": {"done": "#2ba68c", "in_review": "#a587d2", "in_progress": "#648cce", "blocked": "#d78a80",
                    "ready": "#d5ad6b", "intake": "#a3b0c2", "cancelled": "#b7c1ce"},
    # Open-work priority bars.
    "priority": {"P0": "#dc6678", "P1": "#d8a05b", "P2": "#6993ce", "P3": "#a0adbe", "ungraded": "#bec8d5"},
    # Friday commitment result bar.
    "commitment": {"accepted_on_time": "#2ba68c", "accepted_late": "#e0784c", "pending": "#648cce", "overdue": "#d03b3b",
                   "deferred": "#d5ad6b", "cancelled": "#b7b6af", "missing": "#a587d2"},
}
COMMITMENT_LABELS = {"accepted_on_time": "按期确认", "accepted_late": "延期后确认", "pending": "尚未到期／待完成",
                     "overdue": "原承诺已过期", "deferred": "预测移出本周", "cancelled": "已取消", "missing": "记录缺失"}

FONT_FAMILY = '"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,Arial,sans-serif'
OTHER_VARS = (
    ("--radius", "14px", "页面主卡片圆角"),
    ("--font", FONT_FAMILY, "全站字体"),
    ("--wf-forecast-filter", "saturate(1.15) brightness(.92)", "浅色画布上加深预测条的工作类型色"),
    ("--wf-cutoff-opacity", ".42", "数据截止线（--ink）的不透明度"),
)
# Font sizes (px) a report may use, with the role of each step.
TYPE_SCALE = {
    34: "环形图中心数字", 32: "指标卡数值", 30: "完成日期", 26: "页面结论 h1", 24: "窄屏完成日期", 22: "页标题 h2 / 报告名",
    20: "窄屏 h2", 19: "打印 h2", 16: "区块标题 h3", 15: "卡片标题、导语", 14: "正文", 13: "表格与卡片正文",
    12: "说明、标签（正文下限）", 11: "图内标注", 10: "仅 Waterfall 刻度副标",
}
RADIUS_SCALE = {"999px": "胶囊标签", "14px": "页面主卡片（--radius）", "12px": "卡片、面板、表格外框", "10px": "次级块",
                "8px": "按钮、提示块", "4px": "条形图数据端、优先级标签", "2px": "Waterfall 条"}
USAGE_RULES = (
    "颜色只用本文件的 token；在 CSS 中写 var(--名称)，不要写新的色值。需要新颜色时先在 pm_core/design_tokens.py 增加 token。",
    "状态色是保留色：critical=已延期/P0，serious=延期交付，warning=延期风险/待确认，good=已完成/已验收，info=进行中/待决定。不要把它们当普通配色或系列色。",
    "状态不能只靠颜色表达：必须同时有文字标签、位置或纹理（延期风险用斜纹，延期交付用双线框）。",
    "数值和正文用 --ink / --ink-2 / --muted，不用状态色给数字上色；状态用色条、圆点或标签承载。",
    "同一实体在所有报告页面保持同一颜色（工作类型、工作状态、优先级见 chart）。",
    "页面与 Waterfall 都是浅色中性底，不使用纯黑或带蓝色调的大面积背景。",
    "缺失的事实显示为 --faint 色的“待确认/待指定”虚线框，不留空白，也不编造内容。",
    "字号只用 typography.scale 中的档位；正文不小于 12px。",
)


def _type_vars():
    return [("--type-" + key, value) for key, value in CHART["work_type"].items()]


def _chart_labels():
    from .schema import ENUMS
    return {"work_type": {**ENUMS["work_type"], "other": "未单独配色的类型"}, "work_status": ENUMS["work_status"],
            "priority": {**ENUMS["priority"], "ungraded": "待分级"}, "commitment": COMMITMENT_LABELS}


def css_vars():
    """Every CSS variable, in declaration order."""
    pairs = [(var, value) for _, _, var, value, _ in COLOR_TOKENS] + _type_vars() + [(var, value) for var, value, _ in OTHER_VARS]
    return dict(pairs)


def css_root():
    return ":root{" + ";".join(name + ":" + value for name, value in css_vars().items()) + "}"


def chart_colors(name):
    return dict(CHART[name])


def tokens():
    """Machine-readable token set (Design Tokens style: $value / $type / $description)."""
    color = {}
    for group, key, var, value, note in COLOR_TOKENS:
        node = color
        for part in group.split("."):
            node = node.setdefault(part, {})
        node[key] = {"$value": value, "$type": "color", "$description": note, "$extensions": {"cssVar": var}}
    labels = _chart_labels()
    chart = {name: {key: {"$value": value, "$type": "color", "$description": labels[name].get(key, key),
                          **({"$extensions": {"cssVar": "--type-" + key}} if name == "work_type" else {})}
                    for key, value in group.items()} for name, group in CHART.items()}
    other = {var: {"$value": value, "$description": note} for var, value, note in OTHER_VARS}
    return {
        "$description": "pmmaster 独立 HTML 报告的设计 token。由 pm_core/design_tokens.py 生成，请勿手改。",
        "source": "pm_core/design_tokens.py",
        "regenerate": "python -m pm_core.design_tokens",
        "applies_to": ["pm_core/project_report.py (REPORT_CSS)", "pm_core/operating.py", "pm_core/management_text.py",
                       "pm_core/weekly.py", "demo/*.html"],
        "color": color,
        "chart": chart,
        "waterfall_marks": {
            "原计划": "细线，var(--wf-baseline)",
            "当前预测": "工作类型色（chart.work_type）加 var(--wf-forecast-filter)",
            "事实延期": "var(--critical) 实色条",
            "延期风险": "var(--warning) 与 var(--warning-line) 的 135° 斜纹",
            "实际完成": "var(--good) 实色条",
            "延期交付": "var(--good-bg) 底加 var(--critical) 双线框",
            "验收节点": "白底菱形，var(--wf-acceptance) 描边",
            "补救节点": "菱形，var(--wf-remediation)；预期为空心，实际为实心",
            "数据截止线": "var(--ink)，不透明度 var(--wf-cutoff-opacity)；不是状态色",
        },
        "typography": {"fontFamily": {"$value": FONT_FAMILY, "$type": "fontFamily", "$extensions": {"cssVar": "--font"}},
                       "lineHeight": 1.6, "weights": [400, 500, 600, 650, 700],
                       "scale": {str(size) + "px": role for size, role in TYPE_SCALE.items()}},
        "radius": RADIUS_SCALE,
        "layout": {"contentWidth": "96%", "breakpoints": {"narrow": "850px", "medium": "1200px"}, "print": "A4 横向，每页一节"},
        "other": other,
        "usage_rules": list(USAGE_RULES),
    }


def _root():
    return Path(__file__).resolve().parents[1]


def default_export_path():
    return _root() / "references" / "report-design-tokens.json"


def stale(export_path=None):
    """Names of generated files that differ from this module."""
    export_path = Path(export_path or default_export_path())
    out = []
    if not export_path.is_file() or json.loads(export_path.read_text(encoding="utf-8")) != tokens():
        out.append(export_path.name)
    return out


def write(export_path=None):
    export_path = Path(export_path or default_export_path())
    changed = stale(export_path)
    if export_path.name in changed:
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(json.dumps(tokens(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description="导出独立 HTML 报告设计 token")
    parser.add_argument("--out", help="token JSON 输出路径，默认 references/report-design-tokens.json")
    parser.add_argument("--check", action="store_true", help="只检查导出文件是否与本模块一致")
    args = parser.parse_args(argv)
    if args.check:
        changed = stale(args.out)
        print(json.dumps({"ok": not changed, "stale": changed}, ensure_ascii=False))
        return 1 if changed else 0
    changed = write(args.out)
    print(json.dumps({"ok": True, "updated": changed, "tokens": str(args.out or default_export_path())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
