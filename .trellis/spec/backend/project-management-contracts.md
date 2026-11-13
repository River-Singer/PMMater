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
- `workflow_review(data, *, now, config=None)` projects readiness, current-owner feedback, unique-leaf parent rollups and traceable task/resource exposures without changing source records.
- `plan_workflow_followups(data, *, now, config=None)` returns `{review,items,pending,reason}` for the project-local daily `workflow_time` (default `10:00`); the host must invoke the tick.
- `execute_action(action, backend, ledger, policy, *, client=None, dry_run=True)` executes a sourced business action.
- `project_group_actions(data, plan, config, *, now=None)` normalizes source/obligation identities and returns `{recipients,pending}` without transport or business writes.
- `action_batches(actions, config)` returns bounded `{actions,text}` batches; the preview and live dispatch use this renderer.
- `dispatch_tick(data_provider, config, ledger, client, *, now, dry_run=True)` rereads facts, plans scoped reminders and records individual occurrences and message batches.
- `Ledger.reconcile(key, *, outcome, evidence)` accepts `applied` or `not_applied` only for failed/uncertain executions, after actual verification.
- `audit_schedule_changes(data, *, history=None, prior_data=None, now, policy=None)` returns `coverage`, `signals`, `original_commitments`, `alert_count` and `as_of` without writing.

See `python -m pm_core.cli --help` for the command surface and `references/` for end-user workflows.

## 3. Contracts

### Release licensing

The original PMMater/pmmaster materials use the custom noncommercial source-available license in `LICENSE`; preserve third-party and platform/statutory rights. Do not describe it as an OSI-approved open-source license. The author/contact is `allenxujianyang@outlook.com`. Keep root `LICENSE` and `project/LICENSE` byte-identical. Both release build and check must reject missing, empty or divergent notices; the exact packaged notice participates in the manifest. Only the root package `LICENSE` is an extensionless allowlist exception. Never rewrite historical archives to insert a later license; issue a new release name. Run root `tests/` packaging checks when changing this contract.

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

### Daily task readiness and linked impacts

The 10:00 routine covers incomplete project leaves, including undated intake. Missing title, description, acceptance criteria, current forecast interval, valid owner/contact or acceptance responsibility is `missing_data`; lack of a dated nonfuture progress note attributed to the current owner is `awaiting_feedback`. Such feedback is labeled as an owner response, never as an authenticated formal assignment acceptance or confirmation of later reassignments. Completed leaves stop ordinary followups, but a missing actual-start/finish/completion-evidence record has a distinct `workflow_task_audit`; cancelled leaves are excluded. Planned future work may have basic gaps, but its old response is not automatically a daily progress obligation. Progress freshness uses `progress_confirmed_at`, never an arbitrary edit timestamp. No state or baseline is rewritten, and incomplete leaves remain in the project denominator.

Parent rollups count each descendant leaf once. A done parent with unfinished leaves is an inconsistency for review, not a reason to overwrite either status. Ordered ancestors, explicit dependency edges/successors, downstream ancestors and linked deliverables preserve the causal scope; source and successor leaf sets are separate and deduplicated. Graph cycles fail explicitly. A source forecast after an ancestor/project baseline is reported as that exact date mismatch. Dependency reachability alone never proves a hard block, critical path or a quantified project delay.

Task paths with no current risk signals (including completed tasks) preserve their relationships without claiming active propagated risk. A task forecast finish is a delivery forecast, never the deadline for missing-info submission. Such reminders identify the forecast separately. The configured same-day `response_cutoff` supplies an independent reply deadline, never an invented action/delivery DDL. Resource schedule windows likewise remain evidence, not invented commitment deadlines.

`workflow.constraints` contains project resource exposures; `operations_constraints` keeps operations-only sources separate and outside project risk counts. An unlinked project resource stays `needs_mapping` with an empty task path, never spreads to all tasks. Resource constraints use existing resource/leave/interface/register evidence. Task-hour demand, substitute capacity and procurement need-by dates cannot be invented. Employees are excluded only from resource confirmation, not from task followups or resource risk analysis. Human-resource fact coordination goes to the PM; execution and decision obligations retain their recorded responsible person.

Runtime uses the verified project calendar and effective date window, suppresses paused/closed projects and quiet hours, and permits a later same-day tick after 10:00 to catch up. It neither creates a scheduler nor changes the 09:00 attendance/weekly collection entrance. Unknown routing is returned in `plan.pending`, not discarded. Real mentions still need valid open IDs and the configured recipient scope. The dispatch ledger key includes the constraint source identity, so two resource problems on one source row do not suppress each other. Successful/running/unknown occurrences are not resent. Re-reading stops resolved gaps; 10:30 stale-progress reminders overlapping the routine are consolidated, while due-date, acceptance and collection-cutoff events stay independent.

### Structured project-group actions

Each projected action carries a stable source plus obligation identity, `type` (`decision`, `execution`, `filling`, `coordination`), actual `recipient_id`/`responsible_id`, `action`, `deadline`, `deadline_basis`, `evidence_required`, `source`, `impact` and optional `planned_finish`/`date_reference`. `plan.group_actions` exposes this exact projection; deliveries contain the same bounded text used for live sends. Required-field keys are rendered through Chinese catalogue labels. Source URLs are either complete or omitted with a pointer to the full preview, never truncated into a misleading link.

Resolve field labels from the source table first (for example interface `notes` is not a capacity note), with composite/helper-field fallback. Preserve completed-parent/unfinished-child consistency actions despite normal closed-source suppression. Offline Excel previews retain valid local recipients without Feishu contact IDs and still surface missing-contact gaps; live mentions require verified identity.

Decisions use explicit `decision_by_id`, never the register owner as inferred authority. Interface and material/register execution uses the recorded owner. Next actions use the explicit coordinator with task-owner fallback; registered unfulfilled remediation uses `remediation_owner_id` immediately when a risk/overdue routine finds it, even before its expected completion date. Missing/unusable authority or responsibility becomes a PM assignment/coordination action, with the original deadline only as context. A proposed measure is not an approved decision. Employee task/decision/execution duties remain valid; only routine human-capacity confirmation is exempt. Missing PM identity produces pending routing rather than a discarded reminder. Excel mode supports an offline structured preview without Feishu IDs; actual sending still requires a valid open ID and allowlisted recipient.

Only explicit action timestamps (`deadline_at`, `next_due_at`, `remediation_expected`) or configured collection cutoff times become action DDLs. Task baseline/forecast and interface expected readiness remain separately labeled context; missing execution/assignment deadlines remain unknown. `response_deadline` is separately derived from configured `response_cutoff` for questions before that local cutoff; it must not overwrite `deadline`. A named approved decision with a conclusion and nonfuture decision timestamp stops decision requests; implementation followups require their own recorded task/action. No implicit execution obligation is invented from a decided register.

Merge legacy and routine reminders by source, obligation and recipient: duplicate decision, register execution, progress maintenance, unblock/next-action and remediation obligations collapse; different collection cutoffs and different actual actions stay distinct. Default bounds are `group_max_items=5` and `group_max_chars=3500` (valid ranges 1–10 and 1200–6000). Long text is condensed with a source pointer while full fields remain in the projection. Batches are claimed and sent independently with durable request UUIDs and per-occurrence receipts. A successful batch does not mark other batches sent; unknown/running batches and their occurrences never auto-retry. Definite failures may retry within the configured attempt limit; uncertain recovery requires evidence-based reconciliation of the batch and associated occurrences. Feishu mentions remain structured `at` plus plain `text` elements; source text can never manufacture an `at`/`@all` element.

`runtime-reconcile` intentionally updates one key at a time. For a verified message outcome, suspend the project's ticks and reconcile its `batch_key` and every associated `items[].occurrence_key` using the same real evidence before resuming. Reconciling only the batch must not bypass unknown occurrence protection. Unrelated chunks remain unchanged; changing chunk limits must never reset an occurrence's retry allowance. Persist returned keys and outcomes so this recovery is operationally possible.

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
| Undated intake or missing task owner/detail | visible readiness gap, route task followup to PM if owner cannot receive |
| Parent is done with open leaves | flag completion inconsistency, preserve original statuses |
| Resource has no explicit WBS relation | report pending mapping; never infer exposure for every task |
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

For workflow changes assert undated intake coverage, actual current-owner response versus other/future/missing evidence, future-plan progress exemption, parent/leaf deduplication, downstream parent deliverables, date evidence without fabricated delay days, 09:59/10:00/quiet-hour and calendar boundaries, explicit unroutable cases, resolved-gap suppression, old stale-progress consolidation and distinct resource occurrence identities.

The supported Windows runtime needs timezone data from requirements. Construct timezone-sensitive fixtures with `ZoneInfo` rather than assuming a permanent UTC offset. SQLite context managers do not close connections; explicitly close backup handles before replacing/removing Windows files.

Windows archive copying compares dev/inode/size/mtime across path-stat and handle-fstat. Do not compare ctime between those APIs under Python 3.14; they may differ for the same unchanged file. Preserve replacement/mutation detection using same-handle before/after checks, byte digests and final identity checks. Regression tests must detect same-size edits with restored mtime and path replacement, not just make the copy succeed.

## 7. Wrong vs Correct

Wrong: mark the entire project blocked because one linked resource is unconfirmed. Correct: retain the source gap, its explicitly related leaves/ancestors/dependency successors and any date mismatch; report project completion impact as pending evaluation when scheduling evidence is absent.

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

## 8. Conditional questions and 17:00 escalation (r5)

`task_scenarios` is a pure WBS leaf projection. A scenario carries stable `id=task_id:code`, `code`, `title`, `category`, `role`, `required_fields`, `questions[{id,prompt}]`, `facts`, `assessment` and `stop_condition`. Question IDs are `code:topic`. Retain task-level ancestor/dependency/project impact and existing resource/decision mechanisms; do not claim identical conditional checks for operations or all schema columns.

Conditional checks cover missing current forecast dates, definition/owner/acceptance/priority gaps, unconfirmed actual start when scheduled/active, actual-state conflicts, finish today versus overdue, pending acceptance, current-owner feedback/staleness, blocked/waiting recovery, incomplete delay response and overdue/unverified remediation. Done rows only receive completion-record checks; cancelled rows exit. Actual-date blanks before the relevant event are normal. Finish comparison preserves the baseline where present; changing forecast cannot erase the original commitment.

Questions must ask whether work happened but was not recorded versus has not happened, remaining effort/difficulty/uncertainty, need for smaller verifiable work/resource support, continuing versus waiting, restart conditions/time or next check, remedy owner/expected completion and parent/project effects. Ask for proposals, never auto-extend dates, create decomposition, approve changes or infer lack of work. Work-item age thresholds beyond existing progress staleness require configured policy/history and are not silently introduced as industry defaults.

Group actions add `scenario_ids`, `questions`, `response_fields` (question IDs, **not** business columns), `response_deadline` and `response_deadline_basis`. Existing `required_fields` retain business-field meaning. Default `response_cutoff="17:00"` uses project-local aware time; questions generated after cutoff have no invented already-expired reply deadline. Preserve actual `deadline` and its basis. Long-message splitting must retain each requested question, even at supported minimum limits.

Successful ordinary group sends call `record_sent` with the actual message ID, actual aware send instant, current recipient, project/chat and exact action/questions. These immutable facts are independent of mutable ledger retry/update timestamps. Only successful sends establish that a question was asked; reconciliation does not invent missing original send evidence.

Live CLI dispatch supplies `delivery_clock=utc_now` and samples it after each successful transport response. Native hosts calling `dispatch_tick` directly must likewise supply their trusted clock; a fixed `now` without a delivery clock is for deterministic/offline execution. Never use the tick/read start as successful send time: slow reads or earlier batches must not invent a recipient's response opportunity. Response and escalation cutoff configuration uses project-local `HH:MM`.

`followup-evidence-import --config --ledger --file [--apply]` validates a complete envelope before writes. Default is preview; the ledger must already exist. Feishu mode uses the real clock, rejecting `--now`; offline Excel can use a deterministic demonstration clock. The envelope includes configured `source_id`, matching `project_id/chat_id`, `replies` and `coverage`. The source whitelist is **not authentication**: a controlled host must verify actual Feishu sender/message scope and complete pagination/events before mapping sender identity and invoking this interface.

A reply requires a previously successful `occurrence_key`, matching `prompt_message_id`, its actual `message_id`, internal mapped `sender_id`, aware `at`, relevant `quote`, and `kind=ack|answer`. An answer only supplies nonempty values keyed by actual sent question IDs; an ack supplies no answers. One message addressing several actions is linked separately to each occurrence; unrelated acknowledgements cannot resolve all obligations. Preserve duplicate-message idempotence and reject mismatching source, sender, time, scope or conflicting event identity. A row edit is not a group reply, and reply prose does not write business records.

Coverage observations retain unique `id`, aware `from/through/captured_at`, `complete` and actual host `evidence`. Positive replies do not need coverage. `unanswered` requires complete coverage from successful send through the reply deadline and at least `min_response_minutes` opportunity (default 30); no coverage means `reply_unverified`. `answered`, `partial`, `acknowledged`, `not_due`, `too_recent` and `resolved` remain distinct. Task/problem closure is separate from question response; known active risks remain even after all questions are answered. Owner changes preserve old communication context and must not blame a new owner for the old cutoff.

`escalation_time="17:00"` enables a workday digest via the existing group transport and ledger. `escalation_recipient_id` overrides the project PM; empty defaults to `project.pm_id`, with the same valid identity/participation/allowlist checks. Never infer it from author/license email or display name. The host must wake at both 10:00 and 17:00; configuration is not deployment. Calendar completeness, project stage/effective window and quiet hours still gate sending. Offline Excel previews without external side effects.

Reread facts and replies before the first real digest. Include actual source/owner, last known answer/missing questions, current schedule/remedy, traceable parent/project risk and requested support. Unsent/failed/unknown/unroutable/newly discovered obligations belong to notification/pending categories, never no-response counts. Completed morning questions replaced by new issues remain resolved for the old questions; new issues need their own opportunity. Never include private date-edit attribution in the group digest.

Communication rows cannot suppress current task risks by sharing a source record. Preserve one current task-risk row per task and one resource-risk row per constraint identity, including when several constraints share a source. Keep baseline/current forecast, cause, remediation and ordered relationships; multi-task resource exposure must never concatenate distinct leaves and the union of their ancestors into a fabricated dependency chain.

Freeze the digest's `as_of` and bounded chunks at first real dispatch. Preserve per-chunk request identity; successful/unknown chunks cannot auto-resend, definite failure recovery uses the original frozen evidence/cutoff. Later facts do not regenerate and resend a previously dispatched daily digest. Empty reports do not consume the day. Keep the existing batch/occurrence recovery contract and disclose that a retried digest describes its saved cutoff.

Required tests: conditional applicability and stop conditions; today/future/done/cancelled boundaries; stable questions/roles and unchanged action DDL; durable successful sends and immutable actual send times; partial/ack/answered/no coverage/complete coverage; wrong sender/scope/time/question and duplicate/conflicting imports; 16:59/17:00 and minimum response opportunity; new/changed owner/questions; failed/unknown/partial digest retry; missing PM; calendar/quiet/stage gating; no private date-edit disclosure or live-tenant claims.

### Independent formula display

The optional Feishu `执行状态（公式）` is an additional read-only member display, not a replacement canonical state field. Preserve schema v7 and `work_status.intake`/other values. Formula inputs may include the original `状态`, actual dates, completion evidence, owner and current forecasts; output must not reference itself. Cancellation/blocking/verified completion retain evidence-backed business decisions. Actual finish alone means pending confirmation, and calendar date arrival never means work started. Do not invent a WBS acceptance-result column or infer active blocking from historical free text. Real tenant names/types/options must be verified; a setup guide does not constitute deployment or permission changes.
