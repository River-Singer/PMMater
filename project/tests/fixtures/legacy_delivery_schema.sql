-- Legacy delivery schema for migration coverage. Business validation also runs in pm_core/store.py.
PRAGMA foreign_keys=ON;

CREATE TABLE "calendar_days" ("id" TEXT PRIMARY KEY NOT NULL,"calendar_id" TEXT NOT NULL REFERENCES "calendars"(id) DEFERRABLE INITIALLY DEFERRED,"date" TEXT NOT NULL,"weekday" INTEGER NOT NULL,"is_workday" INTEGER NOT NULL CHECK("is_workday" IN (0,1)),"day_type" TEXT NOT NULL CHECK("day_type" IN ('default_workday','default_weekend','holiday','makeup_workday','company_override')),"label" TEXT,"source_url" TEXT,"verified_at" TEXT,"verification_status" TEXT NOT NULL DEFAULT 'default_unverified' CHECK("verification_status" IN ('default_unverified','draft','verified')),"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),UNIQUE("calendar_id","date"));

CREATE TABLE "calendars" ("id" TEXT PRIMARY KEY NOT NULL,"name" TEXT NOT NULL,"timezone" TEXT NOT NULL DEFAULT 'Asia/Shanghai',"region" TEXT,"coverage_start" TEXT NOT NULL,"coverage_end" TEXT NOT NULL,"source_url" TEXT,"verified_at" TEXT,"notes" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE "day_records" ("id" TEXT PRIMARY KEY NOT NULL,"person_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"calendar_id" TEXT NOT NULL REFERENCES "calendars"(id) DEFERRABLE INITIALLY DEFERRED,"date" TEXT NOT NULL,"period" TEXT NOT NULL CHECK("period" IN ('AM','PM')),"record_kind" TEXT NOT NULL CHECK("record_kind" IN ('planned','actual')),"status" TEXT NOT NULL CHECK("status" IN ('available','other_work','onsite','remote','travel','leave','off','unavailable','unknown')),"notes" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),UNIQUE("person_id","date","period","record_kind"));

CREATE TABLE "dependencies" ("id" TEXT PRIMARY KEY NOT NULL,"predecessor_id" TEXT NOT NULL REFERENCES "work_items"(id) DEFERRABLE INITIALLY DEFERRED,"successor_id" TEXT NOT NULL REFERENCES "work_items"(id) DEFERRABLE INITIALLY DEFERRED,"dependency_type" TEXT NOT NULL DEFAULT 'FS' CHECK("dependency_type" IN ('FS')),"lag_workdays" INTEGER NOT NULL DEFAULT 0,"notes" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),UNIQUE("predecessor_id","successor_id","dependency_type"));

CREATE TABLE event_log(event_id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL, recorded_at TEXT NOT NULL, actor TEXT NOT NULL, entity TEXT NOT NULL, record_id TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL);

CREATE TABLE "interfaces" ("id" TEXT PRIMARY KEY NOT NULL,"name" TEXT NOT NULL,"project_id" TEXT NOT NULL REFERENCES "projects"(id) DEFERRABLE INITIALLY DEFERRED,"source_system" TEXT NOT NULL,"target_system" TEXT NOT NULL,"direction" TEXT NOT NULL DEFAULT 'outbound' CHECK("direction" IN ('outbound','inbound','bidirectional')),"protocol" TEXT NOT NULL DEFAULT 'http' CHECK("protocol" IN ('http','sftp','file','database','message','other')),"address" TEXT NOT NULL,"method" TEXT CHECK("method" IN ('GET','POST','PUT','PATCH','DELETE','SOAP','OTHER')),"description" TEXT NOT NULL,"owner_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"counterparty_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"status" TEXT NOT NULL DEFAULT 'designing' CHECK("status" IN ('designing','developing','testing','ready','blocked','retired')),"expected_ready" TEXT,"work_item_id" TEXT REFERENCES "work_items"(id) DEFERRABLE INITIALLY DEFERRED,"spec_url" TEXT,"auth_note" TEXT,"notes" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE "meetings" ("id" TEXT PRIMARY KEY NOT NULL,"title" TEXT NOT NULL,"project_id" TEXT REFERENCES "projects"(id) DEFERRABLE INITIALLY DEFERRED,"service_id" TEXT REFERENCES "services"(id) DEFERRABLE INITIALLY DEFERRED,"organizer_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"scheduled_at" TEXT NOT NULL,"status" TEXT NOT NULL DEFAULT 'planned' CHECK("status" IN ('planned','held','cancelled')),"agenda" TEXT,"minutes" TEXT,"evidence" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE "participants" ("id" TEXT PRIMARY KEY NOT NULL,"project_id" TEXT REFERENCES "projects"(id) DEFERRABLE INITIALLY DEFERRED,"work_item_id" TEXT REFERENCES "work_items"(id) DEFERRABLE INITIALLY DEFERRED,"person_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"role" TEXT NOT NULL CHECK("role" IN ('collaborator','interface','stakeholder','coordinator','backup')),"starts_on" TEXT,"ends_on" TEXT,"responsibility" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),UNIQUE("project_id","work_item_id","person_id","role","starts_on","ends_on"));

CREATE TABLE "people" ("id" TEXT PRIMARY KEY NOT NULL,"name" TEXT NOT NULL,"person_type" TEXT NOT NULL DEFAULT 'employee' CHECK("person_type" IN ('employee','vendor')),"company" TEXT,"department" TEXT,"project_role" TEXT,"expertise" TEXT,"phone" TEXT,"feishu_url" TEXT,"email" TEXT,"starts_on" TEXT,"ends_on" TEXT,"contact" TEXT,"backup_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"active" INTEGER NOT NULL DEFAULT 1 CHECK("active" IN (0,1)),"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE "project_documents" ("id" TEXT PRIMARY KEY NOT NULL,"title" TEXT NOT NULL,"project_id" TEXT NOT NULL REFERENCES "projects"(id) DEFERRABLE INITIALLY DEFERRED,"document_type" TEXT NOT NULL DEFAULT 'other' CHECK("document_type" IN ('charter','requirements','design','test','minutes','runbook','other')),"url" TEXT NOT NULL,"owner_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"status" TEXT NOT NULL DEFAULT 'draft' CHECK("status" IN ('draft','reviewing','approved','archived')),"version_label" TEXT,"updated_on" TEXT,"work_item_id" TEXT REFERENCES "work_items"(id) DEFERRABLE INITIALLY DEFERRED,"meeting_id" TEXT REFERENCES "meetings"(id) DEFERRABLE INITIALLY DEFERRED,"description" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE project_reports(id TEXT PRIMARY KEY,filename TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,kind TEXT NOT NULL,project_id TEXT NOT NULL);

CREATE TABLE "projects" ("id" TEXT PRIMARY KEY NOT NULL,"name" TEXT NOT NULL,"project_type" TEXT NOT NULL DEFAULT 'development' CHECK("project_type" IN ('development','enhancement','migration','research','other')),"stage" TEXT NOT NULL DEFAULT 'initiating' CHECK("stage" IN ('initiating','planning','executing','accepting','closed','paused')),"po_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"pm_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"sponsor_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"service_id" TEXT REFERENCES "services"(id) DEFERRABLE INITIALLY DEFERRED,"calendar_id" TEXT NOT NULL REFERENCES "calendars"(id) DEFERRABLE INITIALLY DEFERRED,"goal" TEXT,"baseline_start" TEXT,"baseline_finish" TEXT,"forecast_finish" TEXT,"summary" TEXT,"business_impact" TEXT,"baseline_change_id" TEXT REFERENCES "registers"(id) DEFERRABLE INITIALLY DEFERRED,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE "registers" ("id" TEXT PRIMARY KEY NOT NULL,"kind" TEXT NOT NULL CHECK("kind" IN ('reminder','issue','risk','change','decision')),"title" TEXT NOT NULL,"project_id" TEXT REFERENCES "projects"(id) DEFERRABLE INITIALLY DEFERRED,"work_item_id" TEXT REFERENCES "work_items"(id) DEFERRABLE INITIALLY DEFERRED,"priority" TEXT DEFAULT 'P2' CHECK("priority" IN ('P0','P1','P2','P3')),"owner_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"coordinator_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"decision_by_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"impact" TEXT,"deadline_at" TEXT,"status" TEXT NOT NULL DEFAULT 'open' CHECK("status" IN ('open','in_review','approved','rejected','closed')),"proposal" TEXT,"next_action" TEXT,"decision" TEXT,"decided_at" TEXT,"closure_evidence" TEXT,"closed_at" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE "services" ("id" TEXT PRIMARY KEY NOT NULL,"name" TEXT NOT NULL,"business_owner_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"technical_owner_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"calendar_id" TEXT NOT NULL REFERENCES "calendars"(id) DEFERRABLE INITIALLY DEFERRED,"description" TEXT,"active" INTEGER NOT NULL DEFAULT 1 CHECK("active" IN (0,1)),"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE TABLE snapshots(id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at TEXT NOT NULL, cutoff TEXT NOT NULL, event_watermark INTEGER NOT NULL, data_json TEXT NOT NULL);

CREATE TABLE "wbs_nodes" ("id" TEXT PRIMARY KEY NOT NULL,"project_id" TEXT NOT NULL REFERENCES "projects"(id) DEFERRABLE INITIALLY DEFERRED,"code" TEXT NOT NULL,"title" TEXT NOT NULL,"parent_id" TEXT REFERENCES "wbs_nodes"(id) DEFERRABLE INITIALLY DEFERRED,"owner_id" TEXT NOT NULL REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"acceptance_criteria" TEXT,"description" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),UNIQUE("project_id","code"));

CREATE TABLE weekly_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            config_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

CREATE TABLE weekly_occurrences (
            occurrence_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('running','done','failed')),
            report_id TEXT REFERENCES weekly_reports(id),
            lease_token TEXT,
            lease_until TEXT,
            attempts INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT
        );

CREATE TABLE weekly_reports (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('monday','friday')),
            week_start TEXT NOT NULL,
            timezone TEXT NOT NULL,
            confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            cutoff TEXT NOT NULL,
            snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
            baseline_report_id TEXT REFERENCES weekly_reports(id),
            filename TEXT NOT NULL UNIQUE,
            render_state TEXT NOT NULL CHECK(render_state IN ('pending','ready','failed')),
            metrics_json TEXT NOT NULL,
            scheduled_for TEXT,
            is_demo INTEGER NOT NULL CHECK(is_demo IN (0,1)),
            error TEXT
        );

CREATE TABLE "work_items" ("id" TEXT PRIMARY KEY NOT NULL,"title" TEXT NOT NULL,"scope" TEXT NOT NULL DEFAULT 'adhoc' CHECK("scope" IN ('project','operations','adhoc')),"work_type" TEXT NOT NULL DEFAULT 'request' CHECK("work_type" IN ('feature','improvement','bug','incident','request','action')),"project_id" TEXT REFERENCES "projects"(id) DEFERRABLE INITIALLY DEFERRED,"service_id" TEXT REFERENCES "services"(id) DEFERRABLE INITIALLY DEFERRED,"wbs_id" TEXT REFERENCES "wbs_nodes"(id) DEFERRABLE INITIALLY DEFERRED,"status" TEXT NOT NULL DEFAULT 'intake' CHECK("status" IN ('intake','ready','in_progress','blocked','in_review','done','cancelled')),"owner_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"priority" TEXT CHECK("priority" IN ('P0','P1','P2','P3')),"acceptor_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"baseline_start" TEXT,"baseline_finish" TEXT,"forecast_finish" TEXT,"actual_start" TEXT,"actual_finish" TEXT,"acceptance_criteria" TEXT,"completion_evidence" TEXT,"description" TEXT,"blocker_reason" TEXT,"waiting_on" TEXT,"coordinator_id" TEXT REFERENCES "people"(id) DEFERRABLE INITIALLY DEFERRED,"next_action" TEXT,"next_due_at" TEXT,"urgency" TEXT CHECK("urgency" IN ('immediate','soon','normal')),"impact" TEXT CHECK("impact" IN ('critical','high','medium','low')),"severity" TEXT CHECK("severity" IN ('S1','S2','S3','S4')),"priority_reason" TEXT,"meeting_id" TEXT REFERENCES "meetings"(id) DEFERRABLE INITIALLY DEFERRED,"register_id" TEXT REFERENCES "registers"(id) DEFERRABLE INITIALLY DEFERRED,"baseline_change_id" TEXT REFERENCES "registers"(id) DEFERRABLE INITIALLY DEFERRED,"cancel_reason" TEXT,"reopen_reason" TEXT,"version" INTEGER NOT NULL DEFAULT 1 CHECK(version > 0));

CREATE UNIQUE INDEX one_confirmed_weekly_plan
            ON weekly_reports(week_start, timezone)
            WHERE kind='monday' AND confirmed=1;
