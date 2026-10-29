"""Trusted active-research clocks with fake time and mocked measurements only."""
import argparse
import copy
import datetime as dt
import unittest
from unittest.mock import patch

import test_collaboration as fixtures
import test_contracts as h
from tools import common as c, research, run
from modules.research.notebook_collaboration import runtime, sessions
from modules.evaluation.queued_sandbox import sandbox
from modules.evaluation.queued_sandbox.measure import EvaluationNotStarted


class FakeClock:
    def __init__(self):
        self.now = dt.datetime.now(dt.timezone.utc)

    def utc(self):
        return self.now.isoformat()

    def advance(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


class ActiveResearchTimeTests(unittest.TestCase):
    command = h.ContractTests.command
    snapshot = h.ContractTests.snapshot
    write_candidate = h.ContractTests.write_candidate
    candidate = fixtures.CollaborationTests.candidate
    draft = fixtures.CollaborationTests.draft
    request = fixtures.CollaborationTests.request
    note = fixtures.CollaborationTests.note
    iteration = fixtures.CollaborationTests.iteration
    fake = fixtures.CollaborationTests.fake

    def setUp(self):
        h.ContractTests.setUp(self)
        self.clock = FakeClock()
        for module in (c, run):
            patched = patch.object(module, 'utc', side_effect=self.clock.utc)
            patched.start()
            self.addCleanup(patched.stop)
        cfg = c.read_yaml(self.root / 'configs/smoke.yaml')
        cfg['budgets'].update(session_wall_s=2400, run_wall_s=20000)
        c.write_json(self.root / 'configs/clock.yaml', cfg)
        self.run_id = 'contract-study/002'
        run.init(self.run_id, 'configs/clock.yaml')
        self.session = run.command(argparse.Namespace(command='begin-session', run=self.run_id,
            actor='r1', plugin='research', role='researcher', idea='idea-a'))

    def activate(self, native_id='native-clock'):
        self.session = run.command(argparse.Namespace(command='activate-session',
            run=self.run_id, actor='r1', native_id=native_id))
        return self.session

    def timing(self):
        return research.command(self.run_id, 'context',
                                session_id=self.session['session_id'])['research_time']

    def test_begin_does_not_start_clock_and_activation_cannot_reset_it(self):
        self.clock.advance(5000)
        self.assertIsNone(self.session['activated_at'])
        self.assertEqual(self.timing()['active_research_s'], 0)
        _, cfg, state = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'activated'):
            runtime.admit(cfg, state, self.session['session_id'])
        self.activate()
        activated_at = self.session['activated_at']
        self.clock.advance(50)
        self.activate()
        self.assertEqual(self.session['activated_at'], activated_at)
        self.assertEqual(self.timing()['active_research_s'], 50)
        self.assertEqual(self.timing()['launch_wait_s'], 5000)
        with self.assertRaisesRegex(ValueError, 'different native'):
            self.activate('replacement-native')

    def test_queued_failed_evaluation_is_excluded_and_cached_reply_cannot_credit_twice(self):
        self.activate()
        self.candidate()
        self.clock.advance(17)
        self.request()
        self.clock.advance(3000)  # Queueing alone exceeds the session budget.
        self.assertEqual(self.timing()['active_research_s'], 17)

        def failed(*args, **kwargs):
            self.clock.advance(100)
            result = self.fake()
            result.update(valid=False, score=None, infrastructure_error=True)
            return result

        with patch.object(sandbox, 'execute', side_effect=failed):
            response = research.command(self.run_id, 'process', request_id='req1')
        self.assertEqual(response['payload']['evaluation']['status'], 'failed')
        self.assertEqual(self.timing()['active_research_s'], 17)
        self.assertEqual(self.timing()['evaluation_wait_s'], 3100)
        self.clock.advance(8)
        with patch.object(sandbox, 'execute', side_effect=AssertionError('must not replay')):
            duplicate = research.command(self.run_id, 'process', request_id='req1')
        self.assertEqual(duplicate, response)
        self.assertEqual(self.timing()['active_research_s'], 25)
        self.assertEqual(self.timing()['evaluation_wait_s'], 3100)
        # Reloaded state retains tool times, so another process derives the same credit.
        _, cfg, state = self.snapshot()
        self.assertEqual(sessions.research_time(cfg, state['sessions'][0]), self.timing())

    def test_expiry_blocks_new_work_but_preserves_native_and_allows_closing_artifacts(self):
        self.activate()
        self.candidate()
        self.clock.advance(2399)
        self.request()
        self.clock.advance(3000)
        with patch.object(sandbox, 'execute', side_effect=self.fake):
            research.command(self.run_id, 'process', request_id='req1')
        self.clock.advance(1)
        self.assertTrue(self.timing()['exhausted'])
        self.assertEqual(self.snapshot()[2]['sessions'][0]['status'], 'active')
        with self.assertRaisesRegex(ValueError, 'admission deadline'):
            self.candidate('c2')
        manifest, _ = self.write_candidate('c3', session_id=self.session['session_id'])
        with patch.object(sandbox, 'preflight') as preflight:
            with self.assertRaisesRegex(ValueError, 'admission deadline'):
                research.command(self.run_id, 'evaluate', session_id=self.session['session_id'],
                    relative=manifest, request_id='too-late', dataset_id='anchor-a')
            preflight.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'admission deadline'):
            self.draft('reuse', dict(id='late-reuse', finding_id='unknown', reason='try'))
        self.iteration()
        # Exercise the researcher-direct iteration path, not just coordinator delivery.
        data = c.read_json(self.snapshot()[0] / 'research/selections/iter1.json')
        data['iteration_id'] = 'closing-iteration'
        relative = 'research/selections/closing-iteration.json'
        c.write_json(self.snapshot()[0] / relative, data)
        research.command(self.run_id, 'iteration', session_id=self.session['session_id'], relative=relative)
        self.draft('share', dict(id='closing-note', type='hypothesis', claim='Untested next idea',
                                candidate_id=None, evidence_ids=[], parent_findings=[]))
        self.note('closing-iteration')
        result = self.command('end-session', actor='r1', status='completed', native_id='native-clock')
        self.assertEqual(result['native_id'], 'native-clock')
        self.assertEqual(result['elapsed_s'], 2400)
        self.assertTrue(result['over_budget'])

    def test_pending_request_blocks_end_and_clock_tampering_is_detected(self):
        self.activate()
        self.candidate()
        self.request()
        self.note()
        self.clock.advance(3000)
        with self.assertRaisesRegex(ValueError, 'pending requests'):
            self.command('end-session', actor='r1', status='completed', native_id='native-clock')
        directory, cfg, state = self.snapshot()
        state['sessions'][0]['evaluation_waits']['req1']['started_at'] = self.clock.utc()
        c.save_state(directory, state)
        with self.assertRaisesRegex(ValueError, 'timing differs'):
            research.command(self.run_id, 'audit')

    def test_sealed_result_can_recover_its_response_after_run_admission_expires(self):
        self.activate()
        self.candidate()
        self.clock.advance(9)
        self.request()
        _, cfg, _ = self.snapshot()
        # Simulate interruption between evaluator commit and response commit.
        with patch.object(sandbox, 'execute', side_effect=self.fake):
            runtime.invoke(cfg, 'evaluation', 'evaluate', self.run_id, 'c1', 'anchor-a', 'eval1',
                           research_session=self.session['session_id'])
        self.clock.advance(25000)
        with patch.object(sandbox, 'execute', side_effect=AssertionError('must not replay')):
            response = research.command(self.run_id, 'process', request_id='req1')
        self.assertTrue(response['payload']['cached'])
        self.assertEqual(response['payload']['evaluation']['evaluation_id'], 'eval1')
        self.assertEqual(self.timing()['active_research_s'], 9)
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], [])

    def test_run_expiry_before_any_attempt_closes_request_without_consuming_a_call(self):
        self.activate()
        self.candidate()
        self.clock.advance(9)
        self.request()
        self.clock.advance(25000)
        with patch.object(sandbox, 'execute', side_effect=AssertionError('must not start')):
            response = research.command(self.run_id, 'process', request_id='req1')
        self.assertEqual(response['payload']['status'], 'not_started')
        self.assertIsNone(response['payload']['evaluation'])
        self.assertEqual(self.snapshot()[2]['evaluations'], {})
        result = research.command(self.run_id, 'result', session_id=self.session['session_id'], request_id='req1')
        self.assertEqual(result['status'], 'not_started')
        self.assertTrue(result['response_registered'])
        self.assertEqual(self.timing()['active_research_s'], 9)
        self.note()
        self.command('end-session', actor='r1', status='completed', native_id='native-clock')

    def test_only_trusted_no_attempt_denial_can_close_an_unstarted_request(self):
        self.activate()
        self.candidate()
        self.request()
        self.clock.advance(3000)
        with patch.object(runtime, 'invoke', side_effect=RuntimeError('unknown interruption')):
            with self.assertRaisesRegex(RuntimeError, 'unknown interruption'):
                runtime.process(self.run_id, 'req1')
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], ['req1'])
        with self.assertRaisesRegex(ValueError, 'pending requests'):
            self.command('end-session', actor='r1', status='failed', native_id='native-clock')
        with patch.object(runtime, 'invoke', side_effect=EvaluationNotStarted('queue admission expired')):
            response = runtime.process(self.run_id, 'req1')
        self.assertEqual(response['payload']['status'], 'not_started')
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], [])
        self.assertEqual(self.timing()['evaluation_wait_s'], 3000)

    def test_preflight_failure_is_excluded_without_candidate_or_evaluation_reservation(self):
        self.activate()
        self.clock.advance(11)
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])

        def denied(image):
            self.clock.advance(3000)
            raise RuntimeError('mock environment denied')

        with patch.object(sandbox, 'preflight', side_effect=denied), patch.object(sandbox, 'execute') as execute:
            with self.assertRaisesRegex(RuntimeError, 'mock environment denied'):
                research.command(self.run_id, 'evaluate', session_id=self.session['session_id'],
                    relative=manifest, request_id='denied', dataset_id='anchor-a')
            execute.assert_not_called()
        state = self.snapshot()[2]
        self.assertEqual(state['deliveries']['candidate'], {})
        self.assertEqual(state['evaluations'], {})
        self.assertEqual(self.timing()['active_research_s'], 11)
        self.assertEqual(self.timing()['evaluation_wait_s'], 3000)
        self.assertEqual(self.timing()['pending_preflight_ids'], [])
        self.clock.advance(2)
        self.assertEqual(self.timing()['active_research_s'], 13)

    def test_orphan_preflight_blocks_further_work_and_end_until_reconciled(self):
        self.activate()
        directory, cfg, state = self.snapshot()
        with c.mutation(directory):
            token = runtime.begin_preflight(directory, cfg, state, self.session['session_id'])
        self.clock.advance(3000)
        _, cfg, state = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'preflight requires reconciliation'):
            runtime.admit(cfg, state, self.session['session_id'])
        with self.assertRaisesRegex(ValueError, 'pending evaluation waits'):
            self.command('end-session', actor='r1', status='failed', native_id='native-clock')
        runtime.finish_preflight(self.run_id, self.session['session_id'], token)
        self.assertEqual(self.timing()['active_research_s'], 0)

    def test_unlimited_call_budget_and_union_of_overlapping_tool_intervals(self):
        self.activate()
        directory, cfg, state = self.snapshot()
        cfg = copy.deepcopy(cfg)
        cfg['budgets']['evaluation_calls'] = None
        self.assertIsNone(runtime.remaining(cfg, state, {}))
        # Starting a second researcher also supports the explicit unlimited setting.
        with patch.object(c, 'save_state'), patch.object(c, 'event'):
            second = sessions.begin_session(directory, cfg, state, 'r2', 'researcher', idea_id='idea-a')
        self.assertIsNone(second['activated_at'])
        session = state['sessions'][0]
        start = session['activated_at']
        self.clock.advance(50)
        session['preflight_waits'] = {'tool-a': {'started_at': start, 'finished_at': self.clock.utc()}}
        session['evaluation_waits'] = {'tool-b': {'started_at': start, 'finished_at': self.clock.utc()}}
        self.clock.advance(10)
        timing = sessions.research_time(cfg, session)
        self.assertEqual(timing['evaluation_wait_s'], 50)
        self.assertEqual(timing['active_research_s'], 10)


if __name__ == '__main__':
    unittest.main()
