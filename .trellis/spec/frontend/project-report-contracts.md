# Project report presentation contracts

## 1. Scope / Trigger

Applies to `pm_core/project_report.py`, `operating.py` and generated demo HTML. Reports support an aggregate-first WBS view and a reviewable delay/date-change section. Report Waterfall uses the same light neutral canvas as the rest of the report (user decision 2026-11-11, replacing the earlier black panel); progress bars remain colored, and desktop content is wider. The user removed the local web service: supported user entry points are agent conversation, Excel and standalone HTML. Report tabs, hierarchy expansion and scope switching remain embedded in each HTML file and work when it is opened directly.

## 2. Signatures

- `render_waterfall(data, works, as_of)` renders focus and complete-project scope views from the same captured facts.
- `export_project_report(data, path, *, as_of, kind="summary", timezone_name="America/Vancouver", demo=False, review_phase="pre_meeting", schedule_audit=None)` writes standalone HTML. The legacy timezone argument does not override the project's timezone.
- `render_management_section(data, *, as_of, phase="pre_meeting", period="week", lookahead_weeks=6, schedule_audit=None, decisions_html="")` renders business evidence, not serialized implementation objects. `decisions_html` (open registers + P0) is placed directly after the delay review.
- `export_management_report(...)` and `management_text.export_schedule_audit_report(audit, path, *, title, note=None)` write standalone pages with the shared `REPORT_CSS`; `schedule-audit --html` uses the latter.
- `pm_core/design_tokens.py` is the single source for colors, font, type scale and Waterfall parameters. `css_root()` opens `REPORT_CSS`; `chart_colors(name)` feeds inline chart colors; `python -m pm_core.design_tokens [--check]` writes or verifies `references/report-design-tokens.json`.
- `python -m pm_core.demo_reports [--check]` rebuilds `demo/` HTML (and the monthly JSON) from `demo_facts.json`, a temporary copy of `demo.sqlite3` history and `日期变更审议.json` at the fixed cutoff 2026-11-11 17:00 Asia/Shanghai; it rewrites only files whose content changed.
- `waterfall_calendar(data)` returns project-relative weekly buckets, verified working-day counts and unknown-calendar coverage for the standalone report.

## 3. Contracts

- Each first-level WBS group shows completion counts, dates and delay state even when collapsed. Expandable descendants retain exact dates, owners, source links, status and remedy details. Hierarchy can exceed two levels; do not force source data into an artificial two-level tree.
- Aggregate each distinct eligible leaf once. Parent task status and child completion are separate from deliverable acceptance. Count ratios are explicitly task-count measures, not effort-weighted project progress.
- Complete-project timeline uses project-relative WK01, WK02… buckets aligned to local Monday–Sunday from the week containing project start. Annotate date ranges and verified working days. Preserve zero-workday holiday weeks; unknown calendar coverage is not five assumed workdays. Focus scope uses daily ticks.
- A closed group must remain useful; empty/missing dates do not become fabricated dates or bars. Scope toggles retain stable hierarchy and distinguish visible-window counts from whole-project counts.
- Use the report's light neutral surfaces for the Waterfall (white rows, warm-gray group/parent bands, hairline borders; no pure-black panel). Marks map to status tokens: overdue `--critical`, risk `--warning` stripes, actual `--good`, late delivery `--good-bg` with a `--critical` double border, baseline `--wf-baseline` (critical/warning/good are validated at ΔE ≥ 8 under red-green color blindness; the earlier coral overdue failed). The data-cutoff line is `--ink`, not a status color; retain distinct colored progress bars and status marks (with matching legends). Distinguish planned/forecast/actual/delay using labels, line position and patterns as well as color. The background must not return to a blue tint. Content width should use the desktop viewport; contain horizontal overflow within the chart on smaller screens.
- Delay review shows reason, impact, remedy, owner, deadline, options and actual decision separately. Link known task sources and meeting minutes through the existing safe URL renderer. Escape all text; reject executable URL schemes.
- Schedule audit rows include exact before/after values, observed evidence and unknown attribution where applicable. Show no-history limitations, not a clean-history claim. Preserve captured premeeting/postmeeting versions.
- No color literals in `REPORT_CSS`: write `var(--token)`. New colors are added to `design_tokens.py` first. Font sizes stay on `TYPE_SCALE`. `tests/test_design_tokens.py` enforces these constraints and that the exported token file is in sync. Human/AI authoring rules live in `project/docs/HTML报告制作说明.md`.
- Standalone HTML must open without a local server. Keep navigation and chart controls self-contained; data edits happen through agent commands or Excel import and produce a new captured report. The removed web service, server routes and web-service launcher must not reappear as installation or verification prerequisites. `run_demo.bat` remains an optional offline artifact generator; local scheduled report generation also remains a supported command-based capability.
- Core information first: the overview opens with the baseline→forecast date card, KPI tiles (leaf completion, overdue, risk, late delivery, P0, open registers) and a short attention list (P0 first, then overdue by days). Tiles keep neutral value ink; status is carried by the accent bar plus label, never color alone.
- Delay review is a KPI strip (explained / remedy / landed / decision coverage) followed by one card per item. Missing facts render as a muted `待确认`/`待指定` gap chip; an item with no linked decision shows only `未登记审议事项` instead of empty sub-fields.
- Definitions and method notes live in a collapsed `统计口径说明` / `图例与统计口径` details block (hidden in print), not repeated as fine print around every chart.
- `schedule_paragraphs` (documents) and `render_schedule_audit` (HTML) share `_audit_parts`; the HTML shows field and same-cutoff deviation changes as before → after rows and must keep the same facts.
- Weekly reports patch page 03 via the `data-section="2"` … `page-foot` markers and insert the banner before `<nav>`; keep those markers stable.

## 4. Validation & Error Matrix

| Situation | Expected presentation |
|---|---|
| Parent has 3 leaf descendants, 1 done | 1/3, counted once regardless of intermediate parent rows |
| Parent acceptance still pending | keep pending acceptance even if its child tasks are done |
| Holiday week | retain WK column and actual verified workday count |
| Unknown annual calendar | show pending verification rather than inferred workdays |
| No meeting but a known task source URL | render task source; do not hide all evidence as missing meeting |
| Unsafe URL or injected markup | plain escaped text/no executable link |
| No audit history | show coverage gap; no fabricated clean audit |

## 5. Good / Base / Bad Cases

- **Good:** folded group shows 2/5 confirmed leaf tasks and two delayed tasks; expanding exposes the actual records and deadlines.
- **Base:** full-project view spans six WK buckets; focus view shows this week's daily dates, using the same captured cutoff.
- **Base:** 「展开全部任务」 opens hierarchy rows (branches and parent tasks) only; a leaf's detail panel opens on click. Empty detail fields collapse into one `未填写：…` line.
- **Bad:** a directory-only parent row, parent/child double counting, a weekday count guessed from Monday–Friday, or moving a timeline bar to erase historical lateness.

## 6. Tests Required

Assert group aggregate counts, focus/full scope behavior, parent/child nonduplication, acceptance independence, weekly labels and calendar counts. Open generated HTML, operate collapse/expand and scope controls, inspect desktop and smaller-width overflow and browser errors. Compare current demos to their fictional source snapshot. Do not rewrite historical weekly report snapshots to make them look newly captured. `tests/test_demo_reports.py` fails when a renderer changes without `python -m pm_core.demo_reports`; JSON outputs compare by value because audit key order depends on the hash seed.

## 7. Wrong vs Correct

```python
# Wrong: including summaries double-counts descendant completion.
done = sum(row['status'] == 'done' for row in all_descendants)

# Correct: use unique eligible execution leaves for task-count aggregates.
done = sum(row['status'] == 'done' for row in unique_leaf_rows)
```

Wrong: show `WK46` from the ISO calendar or relabel five equally spaced dates as working weeks. Correct: number project-relative weekly buckets and retain their calendar-date geometry, with verified workdays shown separately.
