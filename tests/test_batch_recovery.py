"""Recover a single batch across local and SSH workers using disposable assets."""
import argparse
import unittest
from unittest.mock import patch
import yaml

from test_environment_runtime import RuntimeFixture, FUNCTION_EVALUATOR
from tools import common as c, measure, run
from modules.evaluation.queued_python import executor
from modules.evaluation.queued_python.measure import retry


class RecoveryTests(RuntimeFixture, unittest.TestCase):
    def test_default_number_and_replacement_session(self):
        rid, _, _ = self.initialize()
        directory, cfg, _ = c.load_run(rid)
        session_args = dict(run=rid, actor='alice')
        def command(name, **kw):
            return run.command(argparse.Namespace(command=name, **session_args, **kw))
        command('begin-session', module='research', role='researcher', idea='unused-placeholder')
        old = command('activate-session', native_id='first')
        document = c.read_yaml(directory/'run.yaml')
        document['budgets']['session_wall_s'] = 5000
        (directory/'run.yaml').write_text(yaml.safe_dump(document))
        self.assertEqual(run.init(rid.split('/')[0])['run_id'], rid)
        self.assertFalse(directory.with_name('002').exists())
        self.assertEqual(c.load_run(rid)[1]['created_at'], cfg['created_at'])
        session = command('resume-session', native_id='next', stopped_at=old['activated_at'])
        self.assertEqual(session['session_id'], old['session_id'])
        self.assertEqual(session['previous_native_ids'], ['first'])
        self.assertEqual(command('resume-session', native_id='next', stopped_at=None), session)
        self.assertEqual(run.init(rid.split('/')[0], 'configs/runtime-test.yaml', new_batch=True)['run_id'],
                         rid[:-1] + '2')

    def test_previous_executor_summary_remains_readable_after_migration(self):
        rid, _, _ = self.initialize()
        summary = measure.evaluate(rid, 'baseline', 'anchor', 'old-executor')
        directory, _, state = c.load_run(rid)
        path = directory/state['evaluations']['old-executor']
        # Older numbered batches predate the transport and per-case identity fields.
        for key in ('execution', 'case_key', 'config_sha'):
            summary.pop(key)
        summary['request_sha256'] = 'a' * 64
        c.write_json(path, summary)
        state['evaluation_hashes']['old-executor']['summary_sha'] = c.sha(path)
        c.save_state(directory, state)
        with patch.object(executor, 'execute', side_effect=AssertionError('sealed result executed again')):
            run.resume(rid)
            self.assertTrue(measure.evaluate(rid, 'baseline', 'anchor', 'old-executor')['cached'])
            self.assertEqual(retry(rid, 'old-executor')['score'], 6)

    def test_fault_then_rescore_preserves_older_attempts(self):
        rid, initialization, _ = self.initialize()
        directory = c.run_path(rid)
        execute = executor.execute
        def fail_second(source, row, *args, **kwargs):
            if row['values'][0] == 7:
                raise RuntimeError('fixture worker lost')
            return execute(source, row, *args, **kwargs)
        with patch.object(executor, 'execute', side_effect=fail_second):
            first = measure.evaluate(rid, 'baseline', 'anchor', 'eval')
        self.assertTrue(first['infrastructure_error'])
        old_digest = c.sha(directory/first['details_file'])
        with patch.object(executor, 'execute', wraps=execute) as worker:
            repaired = retry(rid, 'eval')
            self.assertEqual(worker.call_count, 1)
            self.assertTrue(retry(rid, 'eval')['cached'])
        self.assertEqual(repaired['score'], 6)
        self.assertEqual(len((directory/repaired['details_file']).read_text().splitlines()), 2)
        self.assertEqual(len(measure.details(rid, 'eval')['records']), 4)
        self.assertEqual(c.sha(directory/first['details_file']), old_digest)
        scorer = self.write(self.root, 'fixed.py', FUNCTION_EVALUATOR.replace(
            "'score': solution", "'score': min(5, solution)"))
        self.assertEqual(run.resume(rid, scorer)['stale_evaluations'], ['eval'])
        self.assertTrue(measure.details(rid, 'eval')['requires_rescoring'])
        with patch.object(executor, 'execute', wraps=execute) as worker:
            self.assertEqual(retry(rid, 'eval')['score'], 5)
            self.assertEqual(worker.call_count, 2)
        self.assertEqual(run.resume(rid, scorer)['stale_evaluations'], [])
        self.assertEqual((initialization/'datasets/environment/evaluate.py').read_text(), FUNCTION_EVALUATOR)
        self.assertEqual(len(c.load_run(rid)[2]['evaluation_history']['eval']), 2)
        self.assertFalse(directory.with_name('002').exists())
