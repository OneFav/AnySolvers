"""collaborative research invariants: real module dispatch with disposable state, mocked measurements only."""
import copy
import json
from unittest.mock import patch
import unittest
import yaml
import test_contracts as h
from tools import common as c, run, research, measure
from modules.evaluation.queued_sandbox import sandbox


class CollaborationTests(unittest.TestCase):
    setUpBase = h.ContractTests.setUp
    command = h.ContractTests.command
    snapshot = h.ContractTests.snapshot
    write_candidate = h.ContractTests.write_candidate

    def setUp(self):
        self.setUpBase()
        cfg = c.read_yaml(self.root/'configs/smoke.yaml')
        cfg['budgets']['session_wall_s'] = 600
        (self.root/'configs/collaboration-test.yaml').write_text(yaml.safe_dump(cfg))
        self.run_id = 'contract-study/002'
        run.init(self.run_id, 'configs/collaboration-test.yaml')
        self.session = self.command('begin-session', actor='r1', idea='idea-a', native_id='/root/old')

    def draft(self, kind, data, sid=None):
        data = dict(data,session_id=sid or self.session['session_id'])
        path = 'research/inbox/'+data['id']+'.json'
        c.write_json(self.snapshot()[0]/path, data)
        return research.command(self.run_id, kind, session_id=data['session_id'], relative=path)

    def candidate(self,cid='c1',parent=None,sid=None):
        path,source=self.write_candidate(cid,session_id=sid or self.session['session_id'])
        p=self.snapshot()[0]/path
        data=c.read_json(p);data['parent_id']=parent;c.write_json(p,data)
        self.command('deliver',kind='candidate',manifest=path,plugin='research')
        return path

    def request(self,cid='c1',rid='req1',eid='eval1',dataset='anchor-a',sid=None):
        return self.draft('request',dict(id=rid,candidate_id=cid,dataset_id=dataset,evaluation_id=eid,probe=False),sid)

    def fake(self,*args,**kwargs):
        return dict(valid=True,score=1.0,solution={'order':[]},metrics={},infrastructure_error=False,
                    cleanup_confirmed=True,wall_s=0.01,queue_wait_s=0.0)

    def evaluate(self,cid='c1',rid='req1',eid='eval1'):
        self.request(cid,rid,eid)
        with patch.object(sandbox,'execute',side_effect=self.fake):
            return research.command(self.run_id,'process',request_id=rid)

    def test_multiple_candidates_and_evaluations_stay_in_one_session(self):
        self.candidate();self.evaluate()
        self.candidate('c2',parent='c1');self.evaluate('c2','req2','eval2')
        state=self.snapshot()[2]
        self.assertEqual(len(state['sessions']),1)
        self.assertEqual(state['sessions'][0]['status'],'active')
        self.assertEqual(len(state['evaluations']),2)
        self.assertEqual(len(state['deliveries']['candidate']),2)
        self.assertEqual(research.command(self.run_id,'audit')['pending_requests'],[])

    def test_base_binding_allows_multiple_candidates(self):
        self.run_id='contract-study/001'
        self.session=self.command('begin-session',actor='default',idea='idea-a')
        self.candidate()
        self.candidate('c2')
        self.assertEqual(set(self.snapshot()[2]['deliveries']['candidate']), {'c1', 'c2'})

    def test_response_idempotent_no_replay(self):
        self.candidate();first=self.evaluate()
        with patch.object(sandbox,'execute',side_effect=AssertionError('must not execute')):
            self.assertEqual(first,research.command(self.run_id,'process',request_id='req1'))

    def test_distinct_actors_cannot_reserve_same_evaluation_id(self):
        self.candidate();self.request()
        other=self.command('begin-session',actor='r2',idea='idea-a')
        self.candidate('c2',sid=other['session_id'])
        with self.assertRaisesRegex(ValueError,'already reserved'):
            self.request('c2','req2','eval1',sid=other['session_id'])

    def test_request_rejects_final_wrong_author_and_pending(self):
        self.candidate()
        with self.assertRaisesRegex(ValueError,'development'):
            self.request(dataset='final-a')
        s=self.command('begin-session',actor='r2',idea='idea-a')
        with self.assertRaisesRegex(ValueError,'requesting session'):
            self.request(sid=s['session_id'])
        self.request()
        with self.assertRaisesRegex(ValueError,'existing request'):
            self.request(rid='req2',eid='eval2')
        with self.assertRaisesRegex(ValueError,'pending requests'):
            self.command('end-session',actor='r1',status='failed')

    def test_measurement_releases_metadata_lock_and_merges_new_data(self):
        self.candidate()
        def during(*args,**kwargs):
            directory=self.snapshot()[0]
            with c.mutation(directory):
                _,_,s=self.snapshot();s['during_measurement']='kept';c.save_state(directory,s)
            with self.assertRaisesRegex(ValueError,'measurement is active'):
                measure.evaluate(self.run_id,'c1','anchor-a','other')
            return self.fake()
        with patch.object(sandbox,'execute',side_effect=during):
            result=measure.evaluate(self.run_id,'c1','anchor-a','eval1')
        self.assertTrue(result['all_valid'])
        self.assertEqual(self.snapshot()[2]['during_measurement'],'kept')

    def test_infrastructure_failure_is_not_silently_cached_or_retried(self):
        self.candidate()
        failed=self.fake();failed.update(valid=False,score=None,infrastructure_error=True)
        with patch.object(sandbox,'execute',return_value=failed):
            result=measure.evaluate(self.run_id,'c1','anchor-a','eval1')
        self.assertIsNone(result['score'])
        with patch.object(sandbox,'execute',side_effect=AssertionError('must not execute')):
            with self.assertRaisesRegex(ValueError,'reconciliation'):
                measure.evaluate(self.run_id,'c1','anchor-a','eval2')

    def share(self):
        return self.draft('share',dict(id='finding1',type='result',claim='Author claim, not a score',
            candidate_id='c1',evidence_ids=['eval1'],parent_findings=[]))

    def test_findings_bind_trusted_metrics_and_source_reuse_lineage(self):
        self.candidate();self.evaluate();post=self.share()
        self.assertEqual(post['payload']['trusted_evidence'][0]['score'],1.0)
        s=self.command('begin-session',actor='r2',idea='idea-a')
        with self.assertRaisesRegex(ValueError,'reuse record'):
            self.candidate('c2',parent='c1',sid=s['session_id'])
        self.draft('reuse',dict(id='reuse1',finding_id='finding1',reason='Try the published mechanism'),s['session_id'])
        self.candidate('c2',parent='c1',sid=s['session_id'])
        self.assertEqual(self.snapshot()[2]['deliveries']['candidate']['c2']['manifest']['parent_id'],'c1')

    def test_fake_result_or_scores_rejected(self):
        with self.assertRaisesRegex(ValueError,'trusted development'):
            self.draft('share',dict(id='fake',type='result',claim='no evidence',candidate_id=None,evidence_ids=[],parent_findings=[]))
        import jsonschema
        with self.assertRaises(jsonschema.ValidationError):
            self.draft('share',dict(id='fake',type='hypothesis',claim='no evidence',candidate_id=None,evidence_ids=[],parent_findings=[],score=99))

    def note(self,next_id=None):
        return self.draft('notebook',dict(id='note1',candidate_id=None,evidence_ids=[],learned='a useful observation',
                          failed_or_uncertain='not yet tested',next_iteration_id=next_id))

    def iteration(self):
        p='research/selections/iter1.json'
        data=dict(schema_version=1,iteration_id='iter1',author_session=self.session['session_id'],parent_candidate=None,
            source_idea_ids=['idea-a'],evidence_ids=[],history_iteration_ids=[],observations='initial idea',hypothesis='new mechanism',
            algorithm_changes=['change the priority function'],expected_effect='test improvement',test_plan='anchor',risks=['may fail'],rationale='explore')
        c.write_json(self.snapshot()[0]/p,data)
        self.command('deliver',kind='iteration',manifest=p,plugin='selections')

    def test_notebook_continuation_in_fresh_context(self):
        self.iteration();note=self.note('iter1')
        self.command('end-session',actor='r1',status='completed',native_id='/root/old')
        new=self.command('begin-session',actor='r1',iteration='iter1')
        self.assertEqual(new['context_policy'],'fresh-native-session-with-notebook')
        self.assertIsNotNone(new['resume_notebook'])
        context=research.command(self.run_id,'context',session_id=new['session_id'])
        self.assertEqual(context['latest_notebook'],note)
        with self.assertRaisesRegex(ValueError,'notebook'):
            self.command('end-session',actor='r1',status='completed',native_id='/root/new')
        with self.assertRaisesRegex(ValueError,'fresh native'):
            self.command('end-session',actor='r1',status='failed',native_id='/root/old')

    def test_private_notes_not_in_other_context_and_tamper_detected(self):
        self.note()
        s=self.command('begin-session',actor='r2',idea='idea-a')
        self.assertIsNone(research.command(self.run_id,'context',session_id=s['session_id'])['latest_notebook'])
        ref=self.snapshot()[2]['extensions']['research_exchange']['note1']
        (self.snapshot()[0]/ref['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'sealed collaborative research record changed'):
            research.command(self.run_id,'audit')

    def test_new_source_rejected_after_final_freeze(self):
        self.candidate();self.evaluate();self.note()
        self.command('end-session',actor='r1',status='completed',native_id=None)
        self.command('select',candidate='c1')
        with self.assertRaisesRegex(ValueError,'active research'):
            self.share()

if __name__=='__main__':unittest.main()
