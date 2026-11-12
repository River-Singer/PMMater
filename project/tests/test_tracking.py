from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pm_core.fixtures import demo_data
from pm_core.store import Store,ValidationError
from pm_core.tracking import tracking_data

NOW="2026-11-05T14:00:00+00:00"

class TrackingModelTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(":memory:")
        self.store.apply_batch(demo_data(),now=NOW)
        self.addCleanup(self.store.close)
    def row(self,table,id): return next(x for x in self.store.data()[table] if x["id"]==id)
    def test_member_arrays_roundtrip_and_invalid_reference_is_atomic(self):
        row=self.row("wbs","WI-002")
        self.assertEqual(row["member_ids"],["PER-DEV","PER-QA","PER-VENDOR"])
        count=len(self.store.history())
        row["member_ids"]=["PER-DEV","NO-SUCH-PERSON"]
        with self.assertRaises(ValidationError):
            self.store.apply_batch({"wbs":[row],"people":[{"id":"PER-NEW","name":"临时新成员"}]},now=NOW)
        self.assertEqual(len(self.store.history()),count)
        self.assertFalse(any(x["id"]=="PER-NEW" for x in self.store.data()["people"]))
    def test_duplicate_members_and_invalid_effort_are_rejected(self):
        for value in (["PER-DEV","PER-DEV"],"PER-DEV;PER-QA"):
            row=self.row("wbs","WI-002");row["member_ids"]=value
            with self.assertRaises(ValidationError): self.store.apply_batch({"wbs":[row]},now=NOW)
        for value in (True,-.5,float("nan"),float("inf"),"1.5"):
            row=self.row("wbs","WI-005");row["actual_person_days"]=value
            with self.assertRaises(ValidationError): self.store.apply_batch({"wbs":[row]},now=NOW)
        row=self.row("wbs","WI-005");row["actual_person_days"]=2.75
        self.store.apply_batch({"wbs":[row]},now=NOW)
        self.assertEqual(self.row("wbs","WI-005")["actual_person_days"],2.75)
    def test_parent_cycle_and_different_wbs_are_rejected(self):
        parent=self.row("wbs","WI-002");parent["parent_id"]="WI-002-1"
        with self.assertRaises(ValidationError): self.store.apply_batch({"wbs":[parent]},now=NOW)
        project=dict(self.row("projects","PRJ-DEMO"));project.pop("version",None);project.update(id="PRJ-OTHER",name="另一项目")
        self.store.apply_batch({"projects":[project]},now=NOW)
        other={"id":"WI-EXT","node_kind":"task","title":"另一项目任务","scope":"project",
            "project_id":"PRJ-OTHER","work_type":"action","status":"intake","priority":"P2",
            "owner_id":"PER-PM","is_acceptance_node":False,"delay_risk":"none"}
        self.store.apply_batch({"wbs":[other]},now=NOW)
        child=self.row("wbs","WI-002-3");child["parent_id"]="WI-EXT"
        with self.assertRaises(ValidationError): self.store.apply_batch({"wbs":[child]},now=NOW)
    def test_unfinished_children_and_failed_deliverable_cannot_be_accepted(self):
        parent=self.row("wbs","WI-002")
        parent.update(status="in_review")
        self.store.apply_batch({"wbs":[parent]},now=NOW)
        parent=self.row("wbs","WI-002")
        parent.update(status="done",actual_finish="2026-11-05",completion_evidence="test")
        with self.assertRaises(ValidationError) as err: self.store.apply_batch({"wbs":[parent]},now=NOW)
        self.assertTrue(any(e["code"]=="unfinished_children" for e in err.exception.errors))
        delivery=self.row("deliverables","DEL-001");delivery["acceptance_status"]="rejected"
        with self.assertRaises(ValidationError) as err: self.store.apply_batch({"deliverables":[delivery]},now=NOW)
        self.assertTrue(any(e["code"]=="acceptance_gate" for e in err.exception.errors))
    def test_delivery_needs_real_review_and_safe_link(self):
        for changes in ({"acceptance_status":"accepted"},{"document_url":"javascript:alert(1)"},{"submitted_at":"2026-11-06T09:00:00+08:00"}):
            row=self.row("deliverables","DEL-002");row.update(changes)
            with self.assertRaises(ValidationError): self.store.apply_batch({"deliverables":[row]},now=NOW)
    def test_remediation_actual_is_real_but_does_not_erase_lateness(self):
        facts=tracking_data(self.store.data(),NOW)
        self.assertEqual(facts["tasks"]["WI-005"]["delay_state"],"overdue")
        self.assertTrue(facts["tasks"]["WI-005"]["is_delayed"])
        row=self.row("wbs","WI-005");row["remediation_actual"]="2026-11-06"
        with self.assertRaises(ValidationError): self.store.apply_batch({"wbs":[row]},now=NOW)
    def test_multi_level_todo_counts_leaves_and_does_not_fake_acceptance(self):
        facts=tracking_data(self.store.data(),NOW);parent=facts["tasks"]["WI-002"]
        self.assertEqual((parent["todo_count"],parent["todo_done_count"],parent["todo_progress_pct"]),(3,1,33.33))
        self.assertEqual(parent["stage_acceptance_result"],"待验收")
        self.assertEqual(parent["task_count_aux"],0)
        self.assertEqual(len(facts["leaf_ids"]),10)
        self.assertNotIn("WI-002",facts["leaf_ids"])
        self.assertEqual(facts["tasks"]["WI-003"]["stage_acceptance_result"],"不合格")
    def test_duration_is_inclusive_and_attendance_is_not_effort(self):
        facts=tracking_data(self.store.data(),NOW)
        self.assertEqual(facts["tasks"]["WI-001"]["actual_duration_days"],2)
        self.assertEqual(facts["tasks"]["WI-002-1"]["planned_duration_days"],2)
        self.assertIn("不等于",facts["tasks"]["WI-002"]["attendance_summary"])
        self.assertIsNone(self.row("wbs","WI-002")["actual_person_days"])
    def test_due_day_is_not_late_and_project_midnight_is_used(self):
        data=self.store.data()
        a=tracking_data(data,"2026-11-05T15:59:00+00:00")
        b=tracking_data(data,"2026-11-05T16:00:00+00:00")
        c=tracking_data(data,"2026-11-06T16:00:00+00:00")
        self.assertEqual(a["today"],"2026-11-05")
        self.assertEqual(b["today"],"2026-11-06")
        self.assertEqual(b["tasks"]["WI-003"]["delay_state"],"risk")
        self.assertEqual(c["tasks"]["WI-003"]["delay_state"],"overdue")
        self.assertEqual(c["tasks"]["WI-003"]["delay_days"],1)
    def test_priority_alone_is_not_delay_and_missing_date_is_unknown(self):
        facts=tracking_data(self.store.data(),NOW)
        self.assertEqual(facts["tasks"]["WI-007"]["delay_state"],"on_track")
        self.assertEqual(facts["tasks"]["WI-006"]["delay_state"],"unknown")
        self.assertIsNone(facts["tasks"]["WI-006"]["is_delayed"])
    def test_late_delivery_is_retained_after_completion(self):
        data=self.store.data();next(w for w in data["wbs"] if w["id"]=="WI-001")["baseline_finish"]="2026-11-02"
        facts=tracking_data(data,NOW)
        self.assertEqual(facts["tasks"]["WI-001"]["delay_state"],"late_done")
        self.assertEqual(facts["tasks"]["WI-001"]["delay_days"],1)
    def test_today_to_sunday_excludes_old_completed_and_keeps_carryover(self):
        facts=tracking_data(self.store.data(),NOW)
        self.assertEqual(facts["week_end"],"2026-11-08")
        self.assertNotIn("WI-001",facts["focus_ids"])
        self.assertNotIn("WI-004",facts["focus_ids"])
        self.assertIn("WI-005",facts["focus_ids"])
        self.assertIn("WI-002",facts["focus_ids"])
        self.assertIn("结转",facts["tasks"]["WI-005"]["window_reason"])
    def test_calculated_fields_cannot_be_injected_into_facts(self):
        row=self.row("wbs","WI-005");row["is_delayed"]=False
        with self.assertRaises(ValidationError): self.store.apply_batch({"wbs":[row]},now=NOW)

class LegacyDeliveryMigrationTests(unittest.TestCase):
    def test_legacy_backup_preserves_people_versions_and_event_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"project.sqlite3"
            con=sqlite3.connect(path)
            con.executescript((Path(__file__).parent/"fixtures/legacy_delivery_schema.sql").read_text())
            con.execute("INSERT INTO people(id,name,person_type,active,version) VALUES('PER-OLD','旧记录','employee',1,3)")
            con.execute("INSERT INTO event_log(transaction_id,recorded_at,actor,entity,record_id,before_json,after_json) VALUES('old','2026-11-03T00:00:00+00:00','PM','people','PER-OLD',NULL,'{}')")
            con.execute("PRAGMA user_version=4");con.commit();con.close()
            with Store(path) as store:
                self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0],7)
                self.assertEqual(store.data()["people"][0]["version"],3)
                self.assertEqual(len(store.history()),1)
                self.assertEqual(store.data()["deliverables"],[])
                self.assertIn("member_ids",{r[1] for r in store.conn.execute('PRAGMA table_info(wbs)')})
            self.assertEqual(len(list(Path(tmp).glob('*.before-v5-*.bak'))),1)

if __name__=="__main__": unittest.main()
