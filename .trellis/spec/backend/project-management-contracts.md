# Single-project management contracts

## 1. Scope / Trigger

Applies to `project/pm_core`, generated workbook/schema/docs, and Feishu/runtime adapters. These are executable cross-layer contracts introduced by schema v7, not claims that a tenant or scheduler is deployed. Use the runtime schema catalogue in `schema.py` as the source of truth. User-facing package documentation is Chinese; this developer specification is English.

## 2. Signatures

- `Store.apply_batch(batch, *, actor="local-user", now=None, dry_run=False)` validates a whole proposed state in one transaction. Updates require the current integer `version`; creates omit it or use 0.
- `Store.delete_record(table, record_id, *, version, actor, now=None, dry_run=False)` checks references and records the deleted before-image.
- `Store.promote_operation(operation_id, *, version, decision_id, actor, parent_id=None, now=None)` atomically retires the operation and creates its project intake record.
- `ensure_project_calendar(store, project_id, *, actor="calendar-maintainer", now=None)` generates the project interval and annual verification records.
- `workday_status(data, project_id, day_iso, person_id=None)` returns `is_workday`, `verified`, and `reason`; unknown is not false.
- `plan_daily_confirmations(data, project_id, now, *, skip_confirmed_leave=True)` returns eligible, pending and skipped people/periods.
- `confirm_day(store, project_id, person_id, day, *, periods=("AM", "PM"), values, actor, now=None)` writes arrangement confirmations, not actual attendance.
- `capacity_summary(data, project_id, week_start=None)` returns confirmed capacity rows and derived availability.
- `plan_followups(data, *, now, config=None)` and `management_review(data, *, now, period="week", prior_data=None, lookahead_weeks=6, schedule_audit=None)` are pure projections.
- `execute_action(action, backend, ledger, policy, *, client=None, dry_run=True)` executes a sourced business action.
- `dispatch_tick(data_provider, config, ledger, client, *, now, dry_run=True)` rereads facts, plans scoped reminders and records individual occurrences.
- `Ledger.reconcile(key, *, outcome, evidence)` accepts `applied` or `not_applied` only for failed/uncertain executions, after actual verification.
- `audit_schedule_changes(data, *, history=None, prior_data=None, now, policy=None)` returns `coverage`, `signals`, `original_commitments`, `alert_count` and `as_of` without writing.

See `python -m pm_core.cli --help` for the command surface and `references/` for end-user workflows.

## 3. Contracts

### Canonical data and migrations

One complete data object has the tables from `schema.TABLES` and at most one project. Project delivery records reside in `wbs`; operations, optimization and configuration work reside in `operations`. Never include completed operations in project WBS or weekly commitment denominators. v6 migration preserves original IDs, event history and saved report snapshots. Historical nonproject WBS aliases with `migrated_operation_id` are immutable and excluded from active execution projections. New nonproject WBS writes fail explicitly.

A leaf-only index is a statistics projection, not a relationship index. Resolve explicit dependencies against the complete active project WBS, including parent tasks; otherwise predecessor constraints pointing at a parent silently disappear. Keep parent/child nonduplication in counts without deleting nodes needed by graph validation and reminders.

All entry points use the same state validation. Excel must preserve business IDs, versions and metadata fingerprints. Generated `data/schema.json`, `docs/schema.sql`, `docs/字段字典.md` and `项目管理模板.xlsx` must match the catalogue. Exported derived cells are snapshots; do not claim they recalculate when somebody edits input cells.

### Calendar and resource facts

Every clock has an offset and uses the project's IANA timezone. Chat/computer timezone is irrelevant. Reminder bounds use `reminder_start/end` with `baseline_start/finish` fallback. An annual manifest needs complete status, source URL, verification instant and exception hash. A natural date row alone does not certify holidays. Known official years are bundled separately; unverified future years stop workday dispatch. Confirmed company overrides layer over official days; personal leave never changes the whole project calendar.

Daily arrangements, future leave plans, weekly resource allocation and actual attendance are different facts. AM/PM inputs are unique; an all-day response with numeric hours is rejected rather than doubled. Missing reply is pending, never absence. Net capacity C already includes project and related operations availability after external commitments/leave; remaining capacity is C−P−O−B. Unknown or unconfirmed components produce null and pending, not zero. Do not infer hours or performance from location, task count or duration.

An answered arrangement also needs non-null `available_hours` (zero is valid) and `future_leave_change` (`no_change` or `changed`). A changed leave response needs `future_leave_note` or a same-member/project `leave_plan_id`. The planner returns missing fields per period so transport only asks for unanswered information.

Human-resource confirmation uses `people.person_type`, not project role: only `vendor`/`external` are eligible, `employee` is excluded and missing/unknown types need identity review before any confirmation request. Capacity projections retain all existing resource/leave facts and original confirmation status; `confirmation_scope`/`confirmation_required` distinguish data completeness from who must reply. Do not synthesize missing employee weeks into pending capacity requests. `confirm_day` only creates new daily confirmations for known nonemployees; it may correct existing employee/unknown historical rows without creating additional periods. Task, delay, risk and acceptance followups remain independent of this filter. General resource/leave fact maintenance is not prohibited for employees.

### Weekly and reporting evidence

Baseline dates, latest forecasts and confirmed weekly intermediate outcomes are separate. Monday confirmation uses the real confirmation instant. Friday morning does not mark an afternoon deadline missed prematurely. Preserve original facts when followup reports reference a prior report via `review_of`. A report and its document payload use one source snapshot/cutoff; history absent means trend unknown.

### Schedule-edit evidence

Audit monitors project/WBS/operations baseline and forecast dates, task actual dates, and weekly due/completion timestamps. Accept Store event-log records or normalized `{table,record_id,before,after,actor,recorded_at}` events; paired snapshots are a weaker alternative. Creation and first entry of an actual date are not automatically suspicious edits. Compare previous and new values at the same project-local cutoff, retaining both lateness-at-finish and lateness-at-start in natural days. Prioritize edits of existing dates that reduce/remove reported deviation, date clearing, and unexplained backdating. Harmless forecast changes retaining the baseline are informational. Do not infer intent or call someone dishonest.

Original commitments mean first observed commitments within supplied evidence, not a reconstructed project inception. Preserve first reported actual dates and compare original/current values even after approved replanning. A current snapshot alone has unknown audit coverage. An observation timestamp is not an edit timestamp, actor labels are not authenticated identity, and polling cannot see edits reverted between observations.

Downgrade a correction/replan only with an exact target and field before/after match, approved time/evidence and `approved_by` in configured `decision_makers`. Policy `approved_changes` is trusted configuration; event-carried approvals additionally need `trusted_approval_sources`. A reused `baseline_change_id` or free-text claim of approval cannot suppress a new edit. Public/group reminders must not expose an unverified attribution; route high/medium schedule review to the configured PM channel or report a pending route.

For observed differences, prior authorization can only be proved when it predates the previous observation; an approval inside an unknown edit interval has unproven ordering. After-the-fact resolution is a distinct trusted `policy.reviewed_changes` record with exact before/after values, `reviewed_by`, `reviewed_at`, `evidence` and `kind=correction|replan`. It may produce `reviewed_correction` or `accepted_replan` while retaining `original_classification`, original commitments and `prior_authorization_proven=false`. Never relabel a later review as prior approval.

Ordinary first actual-date entry is informational unless contradicted by prior explicit progress confirmation; prior observed unfinished state is weaker evidence and must be described as such. Earlier actual dates with known zero-to-zero lateness stay informational; unknown baselines cannot prove harmlessness.

### Actions and external effects

Action source needs `id`, `url`, `quote`, `kind`; accepted kinds are `confirmed_fact` and `confirmed_decision`. Operations are query/create/update/delete/book/reschedule. Records use exact stable business IDs; updates supply `expected` for every changed field. Important changes include baseline/scope/acceptance/capacity changes and delete/reschedule; require a configured decision maker and evidence. Cancellation is an update, not deletion. Deletion checks all references and retains the before-image. Meeting input includes timezone-aware start/end, configured organizer and allowed attendees.

Persistent execution keys survive retries, process restarts and revised minutes. Never retry succeeded/running/unknown actions blindly; changed content with an existing identity is revision_pending. Event creation and invitation are separate durable steps. A successful request is not proof that participants accepted. Actual remote pre-read/write/readback is not an atomic version lock; deployment must serialize relevant writers and acknowledge concurrent-edit limits.

`backend.write(..., request_id, *, expected=None)` receives the originally reviewed full before-image. LocalBackend must preserve that original version into Store's atomic compare; rereading a newer version and silently adopting it would overwrite a concurrent human edit. Remote adapters recheck immediately before HTTP and still disclose the platform's lack of atomic CAS.

`enabled` defaults false. Omitting `--apply` is a validated preview. Feishu credentials come from host secret storage or `FEISHU_ACCESS_TOKEN`, never project JSON. All table reads must complete pagination; omitted tables require explicit confirmed-empty configuration rather than silent empty arrays. Real mentions use verified open IDs. Recipients and calendar identities must match configured scope. The code does not install an Aily scheduler or turn SKILL.md into an Aily import manifest.

### Feishu mother-template identity (2026-11-11)

Relation reads accept both legacy record arrays and the documented `{link_record_ids: [...]}` object for single/double relation fields. The list must contain nonempty remote IDs; unknown references still fail, empty arrays map to no reference, and multiple IDs fail on a single-reference field. Writes keep the supported ID-array form. Text formula results use the underlying `type=1` and text-segment `value`, not the field metadata's formula type 20. See the [official Lark record-value contract](https://github.com/larksuite/openclaw-lark/blob/main/skills/feishu-bitable/references/record-values.md).

Table mappings default to `identity="field"` and keep existing business IDs. Opt-in `identity="record_id"` uses the immutable remote record ID as canonical `id`, forbids `fields.id`, and preserves relation/person mapping to those exact IDs. Contacts' names and WBS/project formula numbers are display values, never unique lookup or history keys. `display_field` is a read-only text/formula descriptor; `feishu-pull.display_values[table][record_id]` carries its value separately from canonical `data`. Existing reports still use canonical IDs; do not claim the display map is rendered by them.

`confirmed_absent_fields` explicitly lists unmapped optional fields whose value is null; required fields, `id`, `version`, unknown names and overlapping mappings are rejected. Unconfigured field/table coverage still fails closed. Text-result formula mappings (`type="formula"`, remote type 20) are always read-only, cannot provide canonical `id`, and are checked during preview as well as write. No remote formula edits, counter repair, schema mutations or automatic migration of old mappings are performed. Mother-template profile is a scaffold requiring actual field/table/enum verification, not tenant deployment or a complete discovery result.

For `create` on a record-ID table, the action omits `record_id` and `values.id`. Action/source identity plus the persistent request token deduplicates the request; a temporary validation ID never leaves the adapter. Persist the returned real `record_id` before readback. Missing create identity, write timeout, or failed readback after a successful write leaves `unknown`, never a retryable create. Query/update/delete still require the exact stable ID. Source fingerprints remain unchanged for legacy field identity; a record-ID mapping has distinct audit coverage so old observations are not silently rekeyed or joined.

With a configured `display_field`, persist the post-write value as `detail.display_value`. An empty display result is unverified and leaves the action `unknown` while retaining the real `detail.record_id`; do not recreate the record to repair a formula. Reconciliation requires checking the existing row and formula result.

## 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Stale Excel/API record version | `version_conflict`, no partial transaction |
| New nonproject WBS item | `operations_separate`; direct it to operations |
| Update historical migrated alias | `migrated_readonly` |
| Change existing baseline without approved linked decision | `baseline_control` |
| Delete referenced record | `referenced`, retain all records |
| Unknown annual calendar/project window | pending; no ordinary workday notification |
| Missing person identity or participation window | pending; do not guess a mention target |
| All-day numeric capacity | reject duplicated hours |
| Incomplete capacity | null + pending |
| Source suggestion, ambiguous target, missing authority | pending action; no external mutation |
| Current value differs from action expected | conflict; do not overwrite |
| Timeout / interrupted running write | unknown; query remote before recovery |
| Repeated successful action / reminder occurrence | reuse recorded result, do not resend |
| Failed attempts reach configured maximum (default 3) | stop; evidence-based reconciliation required |
| Incomplete remote pagination or field mapping | error; no fabricated complete snapshot |
| Actual-date first entry | retain evidence; do not equate ordinary backfill with concealment |
| Exact approved correction/replan | informational history; keep original commitment comparison |
| Old approval reference / unknown editor / no history | explicit unresolved evidence; never invent authorization or attribution |

## 5. Good / Base / Bad Cases

- **Good:** a task finishing next month commits to a verifiable interface draft this week. Friday compares that outcome to its real due time while preserving the full-task baseline.
- **Base:** a nonemployee confirms remote/shared work for AM and PM, supplies half-day availability and upcoming-leave response. The planner stops only the answered confirmation while retaining unrelated overdue-task followups.
- **Bad:** increment project progress because an operations ticket closed; treat no reply as leave; erase delay by overwriting a baseline; rerun a new meeting after invitation failure; replace unknown capacity with zero.

## 6. Tests Required

Use the existing unittest suite and deterministic aware clocks, isolated SQLite/temp paths and mocked HTTP. Assert v6 migration preservation/idempotence/backups, workbook roundtrip and catalogue synchronization, holiday/makeup/company/personal precedence, unknown years, AM/PM nonduplication, capacity nulls, operation isolation, weekly cutoffs and immutable report versions. Cover pagination/identity/field mapping, read-before/write-after conflict, source/decision authority, reference deletion, retries/revisions/uncertainty and partial calendar recovery. Open generated HTML and inspect affected workbook sheets. No live-tenant success claim from mocked tests.

The supported Windows runtime needs timezone data from requirements. Construct timezone-sensitive fixtures with `ZoneInfo` rather than assuming a permanent UTC offset. SQLite context managers do not close connections; explicitly close backup handles before replacing/removing Windows files.

Windows archive copying compares dev/inode/size/mtime across path-stat and handle-fstat. Do not compare ctime between those APIs under Python 3.14; they may differ for the same unchanged file. Preserve replacement/mutation detection using same-handle before/after checks, byte digests and final identity checks. Regression tests must detect same-size edits with restored mtime and path replacement, not just make the copy succeed.

## 7. Wrong vs Correct

```python
# Wrong: missing observation becomes a business fact.
remaining = (row.get("net_available_hours") or 0) - row["project_hours"]

# Correct: use the shared confirmed-capacity projection.
review = capacity_summary(data, project_id, week_start)
# review rows keep remaining_hours=None while facts are unconfirmed.
```

```python
# Wrong: redraw history from current values under an older date.
store.snapshot("last Monday", cutoff=old_cutoff)

# Correct: read a genuinely captured snapshot, or explicitly state it is missing.
snapshot = store.read_snapshot(saved_snapshot_id)
```
