import copy
import json
import unittest
from pm_core.schedule_audit import audit_schedule_changes

NOW='2026-11-11T09:00:00+00:00'

class ScheduleAuditTests(unittest.TestCase):
    def setUp(self):
        self.old={'id':'W1','project_id':'P1','title':'接口任务','baseline_start':'2026-11-02','baseline_finish':'2026-11-05','forecast_finish':'2026-11-08','actual_start':'2026-11-04','actual_finish':None,'status':'in_progress','version':1}
        self.data={'projects':[{'id':'P1','calendar_id':'C1'}],'calendars':[{'id':'C1','timezone':'Asia/Shanghai'}],'wbs':[self.old.copy()],'registers':[]}
    def event(self,new,**patch):
        return {'entity':'wbs','record_id':'W1','before_json':json.dumps(self.old),'after_json':json.dumps(new),'event_id':5,'recorded_at':'2026-11-11T08:00:00+00:00','actor':'member-label',**patch}
    def run_audit(self,new,policy=None,**kw):
        self.data['wbs']=[new]
        return audit_schedule_changes(self.data,history=[self.event(new,**kw)],now=NOW,policy=policy)
    def test_plan_postponed_reduces_lateness_same_cutoff(self):
        result=self.run_audit(dict(self.old,baseline_finish='2026-11-13'))
        signal=result['signals'][0]
        self.assertEqual(signal['severity'],'high')
        self.assertEqual((signal['comparison']['before_late_days'],signal['comparison']['after_late_days']),(6,0))
        self.assertEqual(signal['changed_fields'],[{'field':'baseline_finish','before':'2026-11-05','after':'2026-11-13'}])
        self.assertEqual(signal['actor'],'member-label');self.assertFalse(signal['actor_verified'])
        self.assertEqual(result['original_commitments'][0]['original_baseline_finish'],'2026-11-05')
    def test_existing_actual_finish_backdated_differs_from_first_entry(self):
        new=dict(self.old,actual_finish='2026-11-05',status='done')
        first=self.run_audit(new)
        self.assertEqual(first['alert_count'],0)
        self.old.update(actual_finish='2026-11-08',status='done')
        edited=self.run_audit(dict(self.old,actual_finish='2026-11-05'))
        self.assertEqual(edited['signals'][0]['severity'],'high')
        original=edited['original_commitments'][0]
        self.assertEqual(original['first_reported_actual_finish'],'2026-11-08')
        self.assertEqual(original['first_reported_comparison']['late_days'],3)
    def test_start_backdating_and_start_baseline_shift_are_visible(self):
        backdated=self.run_audit(dict(self.old,actual_start='2026-11-02'))['signals'][0]
        self.assertEqual(backdated['comparison']['before_late_start_days'],2)
        self.assertEqual(backdated['comparison']['after_late_start_days'],0)
        moved=self.run_audit(dict(self.old,baseline_start='2026-11-04'))['signals'][0]
        self.assertIn('reported_deviation_reduced',moved['effects'])
    def test_date_removal_keeps_before_and_unknown_after(self):
        result=self.run_audit(dict(self.old,baseline_finish=None,forecast_finish=None))
        self.assertIn('date_evidence_removed',result['signals'][0]['effects'])
        self.assertIsNone(result['signals'][0]['comparison']['after_late_days'])
    def test_normal_forecast_change_with_retained_baseline_is_info(self):
        result=self.run_audit(dict(self.old,forecast_finish='2026-11-13'))
        self.assertEqual(result['alert_count'],0)
        self.assertEqual(result['signals'][0]['severity'],'info')
    def test_stale_baseline_approval_does_not_excuse_new_shift(self):
        self.data['registers']=[{'id':'R1','kind':'change','status':'approved','decision':'以前同意过延期'}]
        result=self.run_audit(dict(self.old,baseline_finish='2026-11-13',baseline_change_id='R1'))
        self.assertEqual(result['signals'][0]['approval']['status'],'reference_only')
        self.assertEqual(result['signals'][0]['severity'],'high')
    def test_exact_recorded_approval_preserves_original_deviation(self):
        new=dict(self.old,baseline_finish='2026-11-13')
        approval={'table':'wbs','record_id':'W1','changes':{'baseline_finish':{'before':'2026-11-05','after':'2026-11-13'}},'approved_by':'PM','approved_at':'2026-11-11T07:00:00Z','evidence':'decision:123'}
        policy={'decision_makers':['PM'],'trusted_approval_sources':['confirmed_decisions']}
        result=self.run_audit(new,approval=approval,source='confirmed_decisions',policy=policy)
        self.assertEqual(result['signals'][0]['kind'],'approved_replan')
        self.assertEqual(result['alert_count'],0)
        self.assertEqual(result['original_commitments'][0]['original_commitment_comparison']['late_days'],6)
        self.assertEqual(result['original_commitments'][0]['current_comparison']['late_days'],0)
        approval['changes']['baseline_finish']['after']='2026-11-12'
        self.assertEqual(self.run_audit(new,approval=approval,source='confirmed_decisions',policy=policy)['signals'][0]['severity'],'high')
    def test_claimed_approver_and_untrusted_task_source_cannot_suppress_alert(self):
        new=dict(self.old,baseline_finish='2026-11-13')
        approval={'table':'wbs','record_id':'W1','changes':{'baseline_finish':{'before':'2026-11-05','after':'2026-11-13'}},'approved_by':'PM','approved_at':'2026-11-11T07:00:00Z','evidence':'task text says approved'}
        for policy,source in [({},'task_text'),({'decision_makers':['PM']},'task_text'),({'decision_makers':['someone_else'],'trusted_approval_sources':['confirmed_decisions']},'confirmed_decisions')]:
            self.assertEqual(self.run_audit(new,approval=approval,policy=policy,source=source)['signals'][0]['severity'],'high')
    def test_first_actual_plus_unrelated_forecast_edit_does_not_infer_concealment(self):
        result=self.run_audit(dict(self.old,actual_finish='2026-11-04',status='done',forecast_finish='2026-11-13'))
        self.assertEqual(result['alert_count'],0)
        self.assertTrue(result['signals'][0]['comparison']['observed_deviation_reduced'])
        self.assertFalse(result['signals'][0]['comparison']['deviation_reduced'])

    def test_confirmed_weekly_due_shift_preserves_original(self):
        old={'id':'WC1','project_id':'P1','due_date':'2026-11-10','confirmed_at':'2026-11-09T01:00:00Z','status':'confirmed'}
        new=dict(old,due_date='2026-11-13')
        self.data['weekly_commitments']=[new]
        result=audit_schedule_changes(self.data,history=[{'table':'weekly_commitments','record_id':'WC1','before':old,'after':new,'recorded_at':NOW}],now=NOW)
        self.assertEqual(result['signals'][0]['severity'],'high')
        self.assertEqual(result['original_commitments'][0]['original_due_date'],'2026-11-10')
    def test_snapshots_do_not_invent_author_or_time(self):
        previous=copy.deepcopy(self.data);self.data['wbs'][0]['baseline_finish']='2026-11-15'
        result=audit_schedule_changes(self.data,prior_data={'data':previous},now=NOW)
        self.assertEqual(result['coverage']['mode'],'snapshot')
        self.assertIsNone(result['signals'][0]['actor']);self.assertIsNone(result['signals'][0]['recorded_at'])
        unknown=audit_schedule_changes(self.data,now=NOW)
        self.assertEqual(unknown['coverage']['mode'],'unknown');self.assertEqual(unknown['signals'],[])
    def test_creation_duplicates_bad_history_and_future_events(self):
        new=dict(self.old,baseline_finish='2026-11-13');self.data['wbs']=[new]
        event=self.event(new)
        result=audit_schedule_changes(self.data,history=[event,event,{'entity':'wbs','record_id':'X','before_json':'broken','after_json':'{}'},dict(event,recorded_at='2027-01-01T00:00:00Z')],now=NOW)
        self.assertEqual(len(result['signals']),1)
        self.assertEqual(result['coverage']['invalid_events'],1)
        self.assertEqual(result['coverage']['future_events_ignored'],1)
        creation=audit_schedule_changes(self.data,history=[dict(event,before_json=None)],now=NOW)
        self.assertEqual(creation['signals'],[])
    def test_candidate_weekly_planning_is_not_a_changed_commitment(self):
        old={'id':'WC1','project_id':'P1','due_date':'2026-11-10','status':'candidate'}
        new=dict(old,due_date='2026-11-13')
        self.data['weekly_commitments']=[new]
        result=audit_schedule_changes(self.data,history=[{'table':'weekly_commitments','record_id':'WC1','before':old,'after':new,'recorded_at':NOW}],now=NOW)
        self.assertEqual(result['alert_count'],0)
        self.assertEqual(result['original_commitments'],[])
    def test_observation_time_is_not_fabricated_edit_time(self):
        new=dict(self.old,baseline_finish='2026-11-15')
        event={'table':'wbs','record_id':'W1','before':self.old,'after':new,'recorded_at':None,'observed_at':NOW,'previous_observed_at':'2026-11-10T09:00:00Z','source_kind':'observation','source':'feishu_observation'}
        result=audit_schedule_changes(self.data,history=[event],now=NOW)
        self.assertEqual(result['coverage']['mode'],'observation')
        self.assertIsNone(result['signals'][0]['recorded_at'])
        self.assertEqual(result['signals'][0]['evidence']['observed_at'],NOW)
        first=audit_schedule_changes(self.data,history=[dict(event,before=None)],now=NOW)
        self.assertEqual(first['coverage']['mode'],'unknown')
        self.assertEqual(first['signals'],[])

    def test_missing_timezone_and_naive_now_do_not_fake_local_day(self):
        self.data['calendars']=[]
        result=self.run_audit(dict(self.old,baseline_finish='2026-11-13'))
        self.assertEqual(result['coverage']['missing_timezones'],1);self.assertEqual(result['signals'],[])
        with self.assertRaises(ValueError):audit_schedule_changes(self.data,now='2026-11-11T09:00:00')

    def test_harmless_actual_correction_retains_evidence_without_alert(self):
        self.old.update(actual_finish='2026-11-04',status='done')
        result=self.run_audit(dict(self.old,actual_finish='2026-11-03'))
        self.assertEqual(result['alert_count'],0)
        self.assertIn('actual_date_moved_earlier',result['signals'][0]['effects'])
        self.assertEqual(result['signals'][0]['comparison']['before_late_days'],0)
        self.assertEqual(result['signals'][0]['comparison']['after_late_days'],0)
        self.old.update(baseline_finish=None,forecast_finish=None)
        result=self.run_audit(dict(self.old,actual_finish='2026-11-03'))
        self.assertEqual(result['signals'][0]['severity'],'medium')

    def test_observation_authorization_and_post_change_review_are_distinct(self):
        new=dict(self.old,baseline_finish='2026-11-15');self.data['wbs']=[new]
        event={'table':'wbs','record_id':'W1','before':self.old,'after':new,'recorded_at':None,
               'observed_at':'2026-11-11T08:00:00Z','previous_observed_at':'2026-11-10T08:00:00Z',
               'source_kind':'observation','source':'feishu_observation'}
        changes={'baseline_finish':{'before':'2026-11-05','after':'2026-11-15'}}
        approval={'table':'wbs','record_id':'W1','changes':changes,'approved_by':'PM',
                  'approved_at':'2026-11-10T07:00:00Z','evidence':'decision:1'}
        policy={'decision_makers':['PM'],'approved_changes':[approval]}
        result=audit_schedule_changes(self.data,history=[event],now=NOW,policy=policy)
        self.assertEqual(result['signals'][0]['kind'],'approved_replan')
        approval['approved_at']='2026-11-11T07:00:00Z'
        result=audit_schedule_changes(self.data,history=[event],now=NOW,policy=policy)
        self.assertEqual(result['signals'][0]['approval']['status'],'approval_timing_unverified')
        self.assertEqual(result['alert_count'],1)
        review={'table':'wbs','record_id':'W1','changes':changes,'reviewed_by':'PM',
                'reviewed_at':'2026-11-11T08:30:00Z','evidence':'review:2','kind':'replan'}
        policy['reviewed_changes']=[review]
        result=audit_schedule_changes(self.data,history=[event],now=NOW,policy=policy)
        signal=result['signals'][0]
        self.assertEqual(signal['kind'],'accepted_replan')
        self.assertEqual(signal['severity'],'info')
        self.assertEqual(signal['original_classification']['severity'],'high')
        self.assertFalse(signal['approval']['prior_authorization_proven'])
        self.assertIsNone(signal['recorded_at'])
        self.assertEqual(result['original_commitments'][0]['original_baseline_finish'],'2026-11-05')

    def test_later_review_needs_exact_change_authority_and_valid_time(self):
        new=dict(self.old,baseline_finish='2026-11-15')
        review={'table':'wbs','record_id':'W1','changes':{'baseline_finish':{'before':'2026-11-05','after':'2026-11-15'}},
                'reviewed_by':'PM','reviewed_at':'2026-11-11T08:30:00Z','evidence':'review:2','kind':'correction'}
        self.assertEqual(self.run_audit(new,policy={'decision_makers':['PM'],'reviewed_changes':[review]})['signals'][0]['kind'],'reviewed_correction')
        for invalid in ({'reviewed_by':'stranger'},{'reviewed_at':'2026-11-11T07:00:00Z'},
                        {'reviewed_at':'2026-11-11T10:00:00Z'},{'changes':{}},{'evidence':''}):
            with self.subTest(invalid=invalid):
                result=self.run_audit(new,policy={'decision_makers':['PM'],'reviewed_changes':[dict(review,**invalid)]})
                self.assertEqual(result['alert_count'],1)
        self.assertEqual(self.run_audit(new,policy={'decision_makers':['PM']},review=review,source='task_text')['alert_count'],1)

    def test_first_actual_reviews_only_contradicting_known_progress(self):
        new=dict(self.old,actual_finish='2026-11-05',status='done')
        self.assertEqual(self.run_audit(new)['alert_count'],0)
        self.old.update(progress_confirmed_at='2026-11-10T09:00:00+08:00',progress_note='接口仍在联调')
        result=self.run_audit(dict(self.old,actual_finish='2026-11-05',status='done'))
        self.assertEqual(result['signals'][0]['severity'],'high')
        evidence=result['signals'][0]['first_actual_review'][0]
        self.assertEqual(evidence['evidence_kind'],'confirmed_progress')
        self.assertEqual(evidence['evidence_at'],'2026-11-10T09:00:00+08:00')
        # Date-only actual finish can occur later on the same day as confirmation.
        self.old['progress_confirmed_at']='2026-11-05T09:00:00+08:00'
        self.assertEqual(self.run_audit(dict(self.old,actual_finish='2026-11-05',status='done'))['alert_count'],0)

    def test_first_actual_observation_is_explicitly_weaker_than_member_confirmation(self):
        result=self.run_audit(dict(self.old,actual_finish='2026-11-05',status='done'),
            recorded_at=None,observed_at=NOW,previous_observed_at='2026-11-10T09:00:00Z',source_kind='observation')
        signal=result['signals'][0]
        self.assertEqual(signal['severity'],'medium')
        self.assertEqual(signal['first_actual_review'][0]['evidence_kind'],'observed_status')
        self.assertIsNone(signal['recorded_at'])

if __name__=='__main__':unittest.main()
