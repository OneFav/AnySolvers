"""Researcher tools run deterministically with mocked sandbox execution only."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

import test_collaboration as fixtures
from test_contracts import checkpoint_session
from tools import research, common as c, run, measure
from modules.evaluation.queued_sandbox import sandbox


class DirectToolsTests(unittest.TestCase):
    setUpBase = fixtures.CollaborationTests.setUpBase
    command = fixtures.CollaborationTests.command
    snapshot = fixtures.CollaborationTests.snapshot
    write_candidate = fixtures.CollaborationTests.write_candidate
    fake = fixtures.CollaborationTests.fake
    draft = fixtures.CollaborationTests.draft
    share = fixtures.CollaborationTests.share
    iteration = fixtures.CollaborationTests.iteration

    def setUp(self):
        fixtures.CollaborationTests.setUp(self)
        preflight = patch.object(sandbox, 'preflight', return_value={'image_id': 'mocked-image'})
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)

    def evaluate(self, manifest, rid='direct1', sid=None, **changes):
        return research.command(self.run_id, 'evaluate', session_id=sid or self.session['session_id'],
                           relative=manifest, request_id=rid, dataset_id='anchor-a', **changes)

    def test_evaluate_registers_candidate_and_returns_trusted_result_without_host_execution(self):
        marker = self.root / 'candidate-imported'
        manifest, source = self.write_candidate('c1', source=
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nraise RuntimeError('never import')\n",
            session_id=self.session['session_id'])
        with patch.object(sandbox, 'execute', side_effect=self.fake) as execute:
            response = self.evaluate(manifest)
        execute.assert_called_once()
        self.assertFalse(marker.exists())
        self.assertEqual(response['payload']['evaluation']['evaluation_id'], 'direct1-eval')
        self.assertEqual(response['payload']['evaluation']['score'], 1.0)
        record = self.snapshot()[2]['deliveries']['candidate']['c1']
        self.assertEqual(record['files'][source], c.sha(self.snapshot()[0] / source))
        with patch.object(sandbox, 'execute', side_effect=AssertionError('no replay')), \
                patch.object(sandbox, 'preflight', side_effect=AssertionError('completed request needs no preflight')):
            self.assertEqual(self.evaluate(manifest), response)
        result = research.command(self.run_id, 'result', session_id=self.session['session_id'], request_id='direct1')
        self.assertEqual(result['summary']['score'], 1.0)
        detailed = research.command(self.run_id, 'result', session_id=self.session['session_id'],
                               evaluation_id='direct1-eval', details=True, case_id='case-a')
        self.assertEqual(len(detailed['records']), 2)
        self.assertEqual({row['case_id'] for row in detailed['records']}, {'case-a'})

    def test_preflight_denial_leaves_candidate_request_evaluation_and_budget_untouched(self):
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        directory, cfg, before = self.snapshot()
        files_before = {str(path.relative_to(directory)): path.read_bytes()
                        for path in directory.rglob('*') if path.is_file()}
        remaining_before = research.command(self.run_id, 'context',
                                       session_id=self.session['session_id'])['remaining_evaluations']

        def denied(image):
            self.assertEqual(image, cfg['compute']['image'])
            # Preflight must not retain the metadata lock during environment checks.
            with c.mutation(directory):
                pass
            raise RuntimeError('Docker socket permission denied')

        with patch.object(sandbox, 'preflight', side_effect=denied) as preflight, \
                patch.object(sandbox, 'execute') as execute:
            with self.assertRaisesRegex(RuntimeError, 'Docker socket permission denied'):
                self.evaluate(manifest, 'environment-denied')
        preflight.assert_called_once_with(cfg['compute']['image'])
        execute.assert_not_called()
        after = self.snapshot()[2]
        waits = after['sessions'][0]['preflight_waits']
        self.assertEqual(len(waits), 1)
        self.assertTrue(all(wait['finished_at'] is not None for wait in waits.values()))
        # Failed preflight is a trusted excluded time interval, not an evaluation.
        after['sessions'][0]['preflight_waits'] = before['sessions'][0]['preflight_waits']
        self.assertEqual(after, before)
        self.assertEqual(after['deliveries']['candidate'], {})
        self.assertEqual(after['evaluations'], {})
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], [])
        self.assertEqual(research.command(self.run_id, 'context',
                                    session_id=self.session['session_id'])['remaining_evaluations'], remaining_before)
        changed = {'state.json', 'research/.requests/environment-denied.client.lock'}
        self.assertEqual({str(path.relative_to(directory)): path.read_bytes()
                          for path in directory.rglob('*') if path.is_file()
                          and str(path.relative_to(directory)) not in changed},
                         {key: value for key, value in files_before.items() if key not in changed})

    def test_response_suffix_cannot_be_claimed_before_or_during_measurement(self):
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        other = self.command('begin-session', actor='r2', idea='idea-a')
        payload = dict(id='collision-response', type='hypothesis', claim='An untested claim.',
                       candidate_id=None, evidence_ids=[], parent_findings=[])
        with self.assertRaisesRegex(ValueError, 'response suffix'):
            self.draft('share', payload, other['session_id'])

        def execute(*args, **kwargs):
            self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], ['collision'])
            with self.assertRaisesRegex(ValueError, 'response suffix'):
                self.draft('share', payload, other['session_id'])
            return self.fake()

        with patch.object(sandbox, 'execute', side_effect=execute) as sandbox_execute:
            response = self.evaluate(manifest, 'collision')
        sandbox_execute.assert_called_once()
        self.assertEqual(response['kind'], 'response')
        self.assertEqual(response['session_id'], self.session['session_id'])
        self.assertEqual(response['payload']['request_id'], 'collision')
        self.assertEqual(response['payload']['evaluation']['evaluation_id'], 'collision-eval')
        self.assertTrue(response['payload']['evaluation']['all_valid'])
        self.assertEqual(response['payload']['evaluation']['score'], 1.0)
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], [])
        findings = research.command(self.run_id, 'findings', session_id=other['session_id'])
        self.assertEqual(findings, {'findings': []})

    def test_audit_rejects_response_with_wrong_request_or_session_linkage(self):
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        with patch.object(sandbox, 'execute', side_effect=self.fake):
            self.evaluate(manifest, 'linked')
        directory, _, state = self.snapshot()
        reference = state['extensions']['research_exchange']['linked-response']
        response_path = directory / reference['path']
        original = c.read_json(response_path)
        for changes, message in (({'session_id': 'another-r001'}, 'request/session'),
                                 ({'payload': {**original['payload'], 'request_id': 'missing'}}, 'request/session'),
                                 ({'kind': 'finding'}, 'reserved response ID')):
            with self.subTest(changes=changes):
                c.write_json(response_path, {**original, **changes})
                reference['sha'] = c.sha(response_path)
                c.save_state(directory, state)
                with self.assertRaisesRegex(ValueError, message):
                    research.command(self.run_id, 'audit')

    def test_evaluate_denies_final_before_registering_or_executing_candidate(self):
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        with patch.object(sandbox, 'execute') as execute, self.assertRaisesRegex(ValueError, 'development'):
            research.command(self.run_id, 'evaluate', session_id=self.session['session_id'],
                        relative=manifest, request_id='final-request', dataset_id='final-a')
        execute.assert_not_called()
        self.assertEqual(self.snapshot()[2]['deliveries']['candidate'], {})
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], [])

    def test_result_and_context_keep_private_development_scoped_until_published(self):
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        with patch.object(sandbox, 'execute', side_effect=self.fake):
            self.evaluate(manifest, 'req1', evaluation_id='eval1')
        other = self.command('begin-session', actor='r2', idea='idea-a')
        for kw in ({'request_id': 'req1'}, {'evaluation_id': 'eval1'}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                research.command(self.run_id, 'result', session_id=other['session_id'], **kw)
        private = research.command(self.run_id, 'context', session_id=other['session_id'])
        self.assertEqual(private['development']['candidates'], [])
        self.assertEqual(private['development']['evaluations'], [])
        self.assertEqual(research.command(self.run_id, 'findings', session_id=other['session_id']), {'findings': []})
        finding = self.share()
        findings = research.command(self.run_id, 'findings', session_id=other['session_id'])
        self.assertEqual([entry['id'] for entry in findings['findings']], ['finding1'])
        self.assertEqual(findings['findings'][0]['evidence_ids'], ['eval1'])
        self.assertEqual(research.command(self.run_id, 'findings', session_id=other['session_id'],
                                     request_id='finding1'), finding)
        result = research.command(self.run_id, 'result', session_id=other['session_id'], evaluation_id='eval1')
        self.assertEqual(result['summary']['score'], 1.0)
        # Public evidence does not transfer another actor's private request authority.
        with self.assertRaises(ValueError):
            research.command(self.run_id, 'result', session_id=other['session_id'], request_id='req1')

    def test_iteration_and_checkpoint_tools_supply_fresh_continuation(self):
        relative = 'research/selections/direct-iteration.json'
        self.iteration()
        data = c.read_json(self.snapshot()[0] / 'research/selections/iter1.json')
        data['iteration_id'] = 'direct-iteration'
        c.write_json(self.snapshot()[0] / relative, data)
        research.command(self.run_id, 'iteration', session_id=self.session['session_id'], relative=relative)
        note = dict(id='direct-note', session_id=self.session['session_id'], candidate_id=None,
                    evidence_ids=[], learned='A concrete next hypothesis is recorded.',
                    failed_or_uncertain='No performance claim.', next_iteration_id='direct-iteration')
        c.write_json(self.snapshot()[0] / 'research/inbox/direct-note.json', note)
        saved = research.command(self.run_id, 'checkpoint', session_id=self.session['session_id'],
                            relative='research/inbox/direct-note.json')
        self.command('end-session', actor='r1', status='completed', native_id=None)
        resumed = self.command('begin-session', actor='r1', iteration='direct-iteration')
        context = research.command(self.run_id, 'context', session_id=resumed['session_id'])
        self.assertEqual(context['latest_notebook'], saved)
        records = self.snapshot()[2]['extensions']['research_exchange']
        self.assertTrue(all(ref['path'].startswith('research/exchange/')
                            for ref in records.values()))
        self.assertEqual(set(self.snapshot()[2]['extensions']), {'research_exchange'})
        self.assertEqual(resumed['context_policy'], 'fresh-native-session-with-notebook')

    def test_final_result_never_enters_researcher_tools_even_after_session_end(self):
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        with patch.object(sandbox, 'execute', side_effect=self.fake):
            self.evaluate(manifest)
            checkpoint_session(self, 'r1')
            self.command('end-session', actor='r1', status='completed', native_id=None)
            self.command('select', candidate='c1')
            measure.evaluate(self.run_id, 'c1', 'final-a', 'hidden-final')
        with self.assertRaisesRegex(ValueError, 'final evaluation'):
            research.command(self.run_id, 'result', session_id=self.session['session_id'],
                        evaluation_id='hidden-final', details=True)
        with self.assertRaises(ValueError):
            research.command(self.run_id, 'context', session_id=self.session['session_id'])

    def test_pending_request_reserves_the_last_budget_slot_atomically(self):
        cfg = c.read_yaml(self.root / 'configs/collaboration-test.yaml')
        cfg['budgets']['evaluation_calls'] = 1
        c.write_json(self.root / 'configs/one-evaluation.yaml', cfg)
        self.run_id = 'contract-study/003'
        run.init(self.run_id, 'configs/one-evaluation.yaml')
        sessions = [self.command('begin-session', actor=actor, idea='idea-a') for actor in ('r1', 'r2')]
        drafts = []
        for index, owner in enumerate(sessions):
            sid = owner['session_id']
            manifest, _ = self.write_candidate('budget-c' + str(index), session_id=sid)
            self.command('deliver', kind='candidate', manifest=manifest, module='research')
            rid = 'budget-request' + str(index)
            relative = 'research/inbox/' + rid + '.json'
            c.write_json(self.snapshot()[0] / relative, dict(id=rid, session_id=sid,
                candidate_id='budget-c' + str(index), dataset_id='anchor-a',
                evaluation_id='budget-eval' + str(index), probe=False))
            drafts.append((sid, relative))
        barrier = threading.Barrier(2)

        def reserve(draft):
            barrier.wait(5)
            try:
                return research.command(self.run_id, 'request', session_id=draft[0], relative=draft[1])
            except ValueError as exc:
                return str(exc)

        with patch.object(sandbox, 'execute') as execute, ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(reserve, drafts))
        execute.assert_not_called()
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertTrue(any(isinstance(value, str) and 'reserved' in value for value in results))
        pending = research.command(self.run_id, 'audit')['pending_requests']
        self.assertEqual(len(pending), 1)
        for owner in sessions:
            context = research.command(self.run_id, 'context', session_id=owner['session_id'])
            self.assertEqual(context['remaining_evaluations'], 0)

    def test_concurrent_duplicate_request_executes_once_and_returns_same_response(self):
        manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        entered, release = threading.Event(), threading.Event()

        def execute(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test did not release measurement')
            return self.fake()

        with patch.object(sandbox, 'execute', side_effect=execute) as sandbox_execute:
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(self.evaluate, manifest, 'same-request')
                try:
                    self.assertTrue(entered.wait(10))
                    duplicate = workers.submit(self.evaluate, manifest, 'same-request')
                finally:
                    release.set()
                self.assertEqual(first.result(10), duplicate.result(10))
        sandbox_execute.assert_called_once()
        self.assertEqual(len(self.snapshot()[2]['evaluations']), 1)
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], [])

    def test_concurrent_evaluations_wait_for_single_slot_without_overlapping_execution(self):
        first_manifest, _ = self.write_candidate('c1', session_id=self.session['session_id'])
        other = self.command('begin-session', actor='r2', idea='idea-a')
        second_manifest, _ = self.write_candidate('c2', source='def solve(case):\n    return {}\n',
                                                   session_id=other['session_id'])
        entered, release = threading.Event(), threading.Event()
        guard = threading.Lock()
        active = 0
        peak = 0

        def execute(*args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
                first = not entered.is_set()
                entered.set()
            try:
                if first and not release.wait(10):
                    raise AssertionError('test did not release the first measurement')
                return self.fake()
            finally:
                with guard:
                    active -= 1

        with patch.object(sandbox, 'execute', side_effect=execute) as sandbox_execute:
            with ThreadPoolExecutor(max_workers=2) as workers:
                one = workers.submit(self.evaluate, first_manifest, 'queue1')
                try:
                    self.assertTrue(entered.wait(10))
                    two = workers.submit(self.evaluate, second_manifest, 'queue2', other['session_id'])
                    self.assertFalse(two.done())
                finally:
                    release.set()
                results = [one.result(10), two.result(10)]
        self.assertEqual(peak, 1)
        self.assertEqual(sandbox_execute.call_count, 2)
        self.assertEqual({r['payload']['evaluation']['evaluation_id'] for r in results},
                         {'queue1-eval', 'queue2-eval'})
        self.assertEqual(research.command(self.run_id, 'audit')['pending_requests'], [])


if __name__ == '__main__':
    unittest.main()
