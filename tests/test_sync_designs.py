"""Deterministic contracts and interrupted file commits; no model or candidate execution."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

import test_contracts as h
from tools import common as c, run, research, measure
from modules.research.notebook_collaboration import runtime
from modules.selections.evidence_iteration import reports
from modules.evaluation.queued_sandbox import sandbox, retention


class SyncedDesigns(unittest.TestCase):
    command = h.ContractTests.command
    snapshot = h.ContractTests.snapshot
    write_candidate = h.ContractTests.write_candidate
    auto_method_reports = False

    def setUp(self):
        h.ContractTests.setUp(self)
        cfg = c.read_yaml(self.root/'configs/smoke.yaml')
        cfg['budgets']['session_wall_s'] = 120
        (self.root/'configs/sync.yaml').write_text(yaml.safe_dump(cfg))
        self.run_id = 'contract-study/002'
        run.init(self.run_id, 'configs/sync.yaml')
        self.session = self.command('begin-session', actor='alice', idea='idea-a')

    def candidate(self, cid='one'):
        relative, _ = self.write_candidate(cid, session_id=self.session['session_id'])
        self.command('deliver', kind='candidate', manifest=relative, module='research')
        return relative

    def note(self, cid=None, rid='note', iteration=None):
        data = dict(id=rid, session_id=self.session['session_id'], candidate_id=cid,
                    evidence_ids=[], learned='Fixture observation', failed_or_uncertain='Unverified',
                    next_iteration_id=iteration)
        directory = self.snapshot()[0]
        relative = 'research/inbox/'+rid+'.json'
        c.write_json(directory/relative, data)
        return research.command(self.run_id, 'notebook', session_id=self.session['session_id'], relative=relative)

    def end(self):
        return self.command('end-session', actor='alice', status='completed')

    def prose(self, cid):
        return {'candidate': None if cid is None else dict(candidate_id=cid, title='Fixture',
            method='Read the jobs and return their identifiers in input order.', limitations='No search.')}

    def submit(self, cid):
        directory, cfg, state = self.snapshot()
        return reports.report_submit(directory, cfg, state, self.session['session_id'], self.prose(cid))

    def fake(self, score=2, **extra):
        return dict(valid=True, score=score, solution={}, metrics={'coverage': 1}, wall_s=.1,
                    queue_wait_s=0, cleanup_confirmed=True, infrastructure_error=False, **extra)

    def evaluate(self, cid='one', eid='eval', result=None):
        with patch.object(sandbox, 'execute', return_value=result or self.fake()):
            return measure.evaluate(self.run_id, cid, 'anchor-a', eid)

    def test_starting_idea_binds_full_source_and_detects_tampering(self):
        directory, cfg, state = self.snapshot()
        start = state['sessions'][0]['starting_idea']
        self.assertEqual(start['content']['hypothesis'], 'A checkable improvement')
        self.assertEqual(start['content_sha'], c.digest(start['content']))
        c.write_json(directory/start['path'], {'forged': True})
        with self.assertRaisesRegex(ValueError, 'starting idea'):
            runtime.audit(directory, cfg, state)

    def test_round_report_uses_last_checkpoint_not_best_and_attaches_failures(self):
        self.candidate('best'); self.evaluate('best', 'best-eval', self.fake(0))
        self.candidate('chosen'); self.evaluate('chosen', 'chosen-eval', self.fake(1))
        failed = self.fake(); failed.update(valid=False, score=None)
        directory, cfg, _ = self.snapshot()
        relative, _ = h.write_dataset_files(directory, cfg, self.example, 'challenge-a', 'challenge', prefix='research/datasets')
        self.command('deliver', kind='dataset', manifest=relative, module='datasets')
        with patch.object(sandbox, 'execute', return_value=failed):
            measure.evaluate(self.run_id, 'chosen', 'challenge-a', 'failed-eval')
        self.note('best', 'old-note'); self.note('chosen'); self.end()
        with self.assertRaisesRegex(ValueError, 'checkpoint candidate'):
            self.submit('best')
        ref = self.submit('chosen')
        directory, cfg, state = self.snapshot()
        saved = c.read_json(directory/ref['path'])
        self.assertEqual(saved['facts']['candidate']['candidate_id'], 'chosen')
        evaluations = saved['facts']['candidate']['evaluations']
        self.assertEqual(evaluations[0]['score'], 1)
        self.assertEqual(evaluations[1]['result_kind'], 'diagnostic')
        self.assertIsNone(evaluations[1]['score'])
        self.assertEqual(saved['facts']['starting_idea']['sha'], self.session['starting_idea']['sha'])
        self.assertTrue(self.submit('chosen')['cached'])
        self.assertEqual(self.command('recommendation')['candidate_id'], 'best')
        self.command('select', candidate='best')

    def test_last_round_report_gate_and_no_method_are_explicit(self):
        self.candidate(); self.evaluate(); self.note(None); self.end()
        with self.assertRaisesRegex(ValueError, 'method report'):
            self.command('select', candidate='one')
        self.submit(None)
        directory, _, _ = self.snapshot()
        saved = c.read_json(directory/'research/reports/alice-r001.json')
        self.assertEqual(saved['facts']['outcome'], 'no_sealed_method')
        self.command('select', candidate='one')

    def test_researcher_iteration_remains_authoritative_and_report_required_for_next_round(self):
        directory = self.snapshot()[0]
        data = dict(schema_version=1, iteration_id='next', author_session=self.session['session_id'],
            parent_candidate=None, source_idea_ids=['idea-a'], evidence_ids=[], history_iteration_ids=[],
            observations='No measurement yet', hypothesis='A different priority may help',
            algorithm_changes=['Change the priority'], expected_effect='Different ordering', test_plan='Anchor',
            risks=['May fail'], rationale='Explore')
        c.write_json(directory/'research/selections/next.json', data)
        research.command(self.run_id, 'iteration', session_id=self.session['session_id'], relative='research/selections/next.json')
        self.note(iteration='next'); self.end()
        with self.assertRaisesRegex(ValueError, 'method report'):
            self.command('begin-session', actor='alice', iteration='next')
        self.submit(None)
        second = self.command('begin-session', actor='alice', iteration='next')
        self.assertEqual(second['starting_idea']['content'], data)
        self.assertEqual(second['generation'], 2)

    def test_exchange_result_recovers_after_lost_state_commit(self):
        directory, cfg, state = self.snapshot()
        with patch.object(c, 'save_state', side_effect=OSError('lost commit')):
            with self.assertRaises(OSError):
                self.note()
        with self.assertRaisesRegex(ValueError, 'reconciliation'):
            research.command(self.run_id, 'audit')
        with patch.object(sandbox, 'execute', side_effect=AssertionError('no solving')):
            recovered = self.command('reconcile-exchange')
            self.assertEqual(recovered['recovered_records'], ['note'])
            self.assertEqual(self.command('reconcile-exchange')['recovered_records'], [])
            self.assertEqual(self.note()['record_id'], 'note')
        self.assertEqual(self.snapshot()[2]['sessions'][0]['native_id'], self.session['native_id'])

    def test_intent_only_recovers_metadata_and_changed_intent_fails_closed(self):
        real = c.write_json
        def fail_result(path, value):
            if '/exchange/notebook/' in str(path):
                raise OSError('before metadata write')
            return real(path, value)
        with patch.object(c, 'write_json', side_effect=fail_result):
            with self.assertRaises(OSError): self.note()
        self.command('reconcile-exchange')
        directory = self.snapshot()[0]
        path = directory/'research/journal/note.json'
        data = c.read_json(path); data['record']['payload']['learned'] = 'changed'; real(path, data)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.command('reconcile-exchange')

    def test_request_and_response_recovery_keep_original_identity_and_wait_times(self):
        self.candidate()
        directory = self.snapshot()[0]
        relative = 'research/inbox/request.json'
        c.write_json(directory/relative, dict(id='request', session_id=self.session['session_id'],
            candidate_id='one', dataset_id='anchor-a', evaluation_id='eval', probe=False))
        with patch.object(c, 'save_state', side_effect=OSError('request commit lost')):
            with self.assertRaises(OSError):
                research.command(self.run_id, 'request', session_id=self.session['session_id'], relative=relative)
        intent = c.read_json(directory/'research/journal/request.json')['record']
        self.command('reconcile-exchange')
        self.assertEqual(self.snapshot()[2]['sessions'][0]['evaluation_waits']['request']['started_at'], intent['at'])
        real = c.save_state
        def lose_response(directory, state):
            if 'request-response' in state.get('extensions', {}).get('research_exchange', {}):
                raise OSError('response commit lost')
            return real(directory, state)
        with patch.object(c, 'save_state', side_effect=lose_response), \
                patch.object(sandbox, 'execute', return_value=self.fake()) as execute:
            with self.assertRaises(OSError): runtime.process(self.run_id, 'request')
        self.assertEqual(execute.call_count, 1)
        response = c.read_json(directory/'research/journal/request-response.json')['record']
        with patch.object(sandbox, 'execute', side_effect=AssertionError('must not replay')):
            self.command('reconcile-exchange')
            self.assertEqual(runtime.process(self.run_id, 'request'), response)
        state = self.snapshot()[2]
        self.assertEqual(list(state['evaluations']), ['eval'])
        self.assertEqual(state['sessions'][0]['evaluation_waits']['request'],
                         {'started_at':intent['at'], 'finished_at':response['at']})

    def test_completed_evaluation_receipt_recovers_without_execution(self):
        self.candidate()
        real = c.save_state
        def fail_seal(directory, state):
            if state.get('evaluation_hashes'): raise OSError('after receipt before state')
            return real(directory, state)
        with patch.object(c, 'save_state', side_effect=fail_seal):
            with self.assertRaises(OSError): self.evaluate()
        with patch.object(sandbox, 'execute', side_effect=AssertionError('no replay')):
            result = self.command('reconcile-evaluation', evaluation='eval')
            self.assertTrue(result['reconciled'])
            self.assertFalse(result['execution_started'])
            self.assertTrue(self.command('reconcile-evaluation', evaluation='eval')['cached'])
            self.assertTrue(measure.evaluate(self.run_id, 'one', 'anchor-a', 'eval')['cached'])

    def test_unknown_or_corrupt_receipt_cannot_authorize_replay(self):
        self.candidate(); self.evaluate()
        path = self.snapshot()[0]/'research/evaluations/eval/completion.json'
        data = c.read_json(path); data['receipt']['summary']['score'] = 999; c.write_json(path, data)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.command('reconcile-evaluation', evaluation='eval')
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'unknown execution'):
            self.command('reconcile-evaluation', evaluation='eval')

    def test_report_remains_development_only_after_final_measurement(self):
        self.candidate(); self.evaluate(); self.note('one'); self.end(); self.submit('one')
        path = self.snapshot()[0]/'research/reports/alice-r001.json'
        before = path.read_bytes()
        self.command('select', candidate='one')
        with patch.object(sandbox, 'execute', return_value=self.fake(999)):
            measure.evaluate(self.run_id, 'one', 'final-a', 'final-eval')
        directory, cfg, state = self.snapshot()
        self.assertEqual(reports.report_audit(directory, cfg, state), {'reports':1})
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'unknown researcher operation'):
            research.command(self.run_id, 'method-report-submit', session_id=self.session['session_id'])

    def test_full_retention_setting_is_pinned_and_executed(self):
        self.run_id = 'contract-study/003'
        directory = self.root/'runs'/self.run_id
        directory.mkdir()
        c.write_json(directory/'run.yaml', dict(schema_version=8, extends='configs/sync.yaml',
            modules={'evaluation':{'params':{'trace_retention':'full'}}}))
        run.init(self.run_id)
        self.session = self.command('begin-session', actor='alice', idea='idea-a')
        self.candidate()
        result = self.fake(trace=[{'step':1, 'action':'x'}])
        self.evaluate(result=result)
        summary = measure.details(self.run_id, 'eval')
        self.assertEqual(summary['summary']['trace_retention'], 'full')
        self.assertEqual(summary['records'][1]['result']['trace'], result['trace'])
        document = c.read_yaml(directory/'run.yaml')
        document['modules']['evaluation']['params']['trace_retention'] = 'summary'
        c.write_json(directory/'run.yaml', document)
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.snapshot()

    def test_retention_is_applied_after_scoring_and_keeps_metrics(self):
        self.candidate()
        trace = [{'step': i, 'observation': 'x'*1024, 'action': i} for i in range(20)]
        result = self.fake(trace=trace)
        self.evaluate(result=result)
        row = measure.details(self.run_id, 'eval')['records'][1]['result']
        self.assertNotIn('trace', row)
        self.assertEqual(row['metrics'], result['metrics'])
        self.assertEqual(row['trace_summary']['records'], 20)
        self.assertEqual(result['trace'], trace)
        self.assertEqual(self.snapshot()[1]['modules']['evaluation']['params'], {'trace_retention':'summary'})

    def test_external_project_move_and_engine_binding(self):
        project = self.root/'external project'
        project.mkdir()
        for name in ('configs', 'runs', 'tests'):
            shutil.copytree(self.root/name, project/name)
        with patch.object(c, 'ROOT', project), patch.object(run, 'ROOT', project):
            directory, cfg, state = self.snapshot()
            self.assertEqual(directory, project/'runs/contract-study/002')
            before = cfg['implementation_sha']
            self.assertEqual(c.workspace_file('modules/research/notebook_collaboration/MODULE.md'),
                             self.root/'modules/research/notebook_collaboration/MODULE.md')
            self.assertFalse((project/'tools').exists())
            # Fresh initialization also resolves tools/modules in the engine and
            # keeps all inputs and outputs in the external project.
            h.ensure_study(project, 'fresh-study', 'configs/sync.yaml')
            created = run.init('fresh-study', 'configs/sync.yaml')
            self.assertEqual(created['run_id'], 'fresh-study/001')
            self.assertTrue((project/'runs/fresh-study/001/state.json').is_file())
        moved = self.root/'moved project'; project.rename(moved)
        env = dict(__import__('os').environ, ANYSOLVERS_PROJECT_ROOT=str(moved))
        result = subprocess.run([sys.executable, '-B', str(self.root/'tools/run.py'),
                                 'check', self.run_id], cwd='/tmp', env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['implementation_sha'], before)
        with patch.object(c, 'ROOT', moved), patch.object(run, 'ROOT', moved):
            implementation = self.root/'tools/execution.py'
            implementation.write_text(implementation.read_text()+'\n# changed\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.snapshot()


class RetentionContracts(unittest.TestCase):
    def test_failed_file_sync_preserves_previous_committed_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            c.write_json(path, {'committed':1})
            with patch('tools.common.os.fsync', side_effect=OSError('disk sync failed')):
                with self.assertRaises(OSError): c.write_json(path, {'committed':2})
            self.assertEqual(c.read_json(path), {'committed':1})
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['state.json'])

    def test_full_trace_and_failure_window_and_nonmutation(self):
        source = dict(valid=False, score=None, trace=[{'step':i} for i in range(20)],
                      diagnostic={'stdout':'日'*5000}, metrics={'reason':'invalid'})
        before = copy.deepcopy(source)
        summary = retention.retain(source)
        self.assertEqual(source, before)
        self.assertEqual(summary['trace_sample'], source['trace'][-3:])
        self.assertTrue(summary['diagnostic_summary']['stdout']['truncated'])
        self.assertEqual(retention.retain(source, 'full')['trace'], source['trace'])
        self.assertEqual(retention.retain(summary), summary)
        with self.assertRaises(ValueError): retention.retain(source, 'bad')
