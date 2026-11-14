"""Exercise the wire commands, cache, detached worker and recovery with a local SSH stand-in."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_environment_runtime import RuntimeFixture
from tools import measure
from modules.evaluation.queued_python import ssh
from modules.evaluation.queued_python.measure import reconcile


class SSHExecutionTests(RuntimeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.remote = self.root / 'remote'
        self.remote.mkdir()
        commands = self.root / 'commands'
        commands.mkdir()
        client = commands / 'ssh'
        # Run the actual quoted remote command in a separate process and working directory.
        client.write_text(f'#!{sys.executable}\n' + """import os, sys
from pathlib import Path
assert sys.argv[-2] == 'fixture'
root = Path(os.environ['TEST_REMOTE'])
os.chdir(root)
os.environ['HOME'] = str(root)
os.execv('/bin/sh', ['sh', '-c', sys.argv[-1]])
""")
        client.chmod(0o755)
        env = patch.dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
                         TEST_REMOTE=str(self.remote), ANYSOLVERS_SSH_HOST='fixture',
                         ANYSOLVERS_SSH_PYTHON=sys.executable)
        env.start()
        self.addCleanup(env.stop)

    def test_remote_static_baseline_and_cache(self):
        run_id, _, _ = self.initialize()
        results = self.evaluate_and_check_cache(run_id, 6)
        self.assertEqual([row['result']['score'] for row in results], [5, 7])
        self.assertEqual(len(list((self.remote/'.cache/anysolvers/jobs').iterdir())), 1)
        with patch.dict(os.environ, ANYSOLVERS_SSH_HOST=''):
            local = measure.evaluate(run_id, 'baseline', 'anchor', 'local-eval')
            repeated = measure.evaluate(run_id, 'baseline', 'anchor', 'baseline-check')
            self.assertEqual(repeated['execution']['mode'], 'ssh')
            self.assertTrue(repeated['cached'])
        self.assertEqual(local['execution']['mode'], 'local')
        self.assertEqual(local['score'], 6)

    def test_retry_sends_only_remaining_cases_to_ssh(self):
        from modules.evaluation.queued_python import executor
        from modules.evaluation.queued_python.measure import retry
        run_id, _, _ = self.initialize()
        execute = executor.execute
        def stop(source, row, *args, **kwargs):
            if row['values'][0] == 7:
                raise RuntimeError('interrupt after first case')
            return execute(source, row, *args, **kwargs)
        with patch.dict(os.environ, ANYSOLVERS_SSH_HOST=''), patch.object(executor, 'execute', side_effect=stop):
            measure.evaluate(run_id, 'baseline', 'anchor', 'migrated')
        self.assertEqual(retry(run_id, 'migrated')['score'], 6)
        job, = (self.remote/'.cache/anysolvers/jobs').iterdir()
        self.assertEqual(len((job/'records.jsonl').read_text().splitlines()), 2)
        self.assertEqual(len(measure.details(run_id, 'migrated')['records']), 4)

    def test_stopped_remote_worker_recovers_journal_before_local_retry(self):
        from modules.evaluation.queued_python.measure import retry
        run_id, _, _ = self.initialize()
        receive = ssh.collect
        def disconnect(*args, **kwargs):
            receive(*args, **kwargs)
            raise ConnectionError('client disconnected')
        with patch.object(ssh, 'collect', side_effect=disconnect):
            with self.assertRaisesRegex(RuntimeError, 'reconcile-evaluation'):
                measure.evaluate(run_id, 'baseline', 'anchor', 'remote-fault')
        job, = (self.remote/'.cache/anysolvers/jobs').iterdir()
        # Emulate a detached worker dying after the second attempt was logged.
        (job/'done.json').unlink()
        journal = job/'records.jsonl'
        journal.write_bytes(b''.join(journal.read_bytes().splitlines(keepends=True)[:3]))
        with patch.dict(os.environ, ANYSOLVERS_SSH_HOST=''):
            repaired = retry(run_id, 'remote-fault')
        self.assertEqual(repaired['prefix']['cases'], 1)
        self.assertEqual(repaired['score'], 6)
        self.assertEqual(len(measure.details(run_id, 'remote-fault')['records']), 4)

    def test_lost_reply_fetches_original_episode_and_reuses_assets(self):
        run_id, _, rows = self.initialize(episode=True)
        fetch = ssh.collect
        def lose_receipt(*args, **kwargs):
            fetch(*args, **kwargs)
            raise ConnectionError('reply lost')
        with patch.object(ssh, 'collect', side_effect=lose_receipt):
            with self.assertRaisesRegex(RuntimeError, 'reconcile-evaluation'):
                measure.evaluate(run_id, 'baseline', 'anchor', 'remote-eval')
        jobs = self.remote / '.cache/anysolvers/jobs'
        job, = jobs.iterdir()
        receipt = json.loads((job/'done.json').read_bytes())
        with patch.dict(os.environ, ANYSOLVERS_SSH_HOST=''), patch.object(ssh, 'prepare', side_effect=AssertionError):
            resumed = measure.evaluate(run_id, 'baseline', 'anchor', 'remote-eval')
            self.assertTrue(resumed['cached'])
            self.assertTrue(reconcile(run_id, 'remote-eval')['cached'])
        details = measure.details(run_id, 'remote-eval')
        self.assertEqual(details['summary']['score'], 1)
        self.assert_episode_visibility([r for r in details['records'] if r['type']=='result'], rows)
        self.assertEqual(details['summary']['finished_at'], receipt['finished_at'])
        self.assertEqual(list(jobs.iterdir()), [job])
        # A second batch reuses the same cache independently of evaluation IDs.
        from tools import run
        second = run_id[:-1] + '2'
        run.init(second, 'configs/runtime-test.yaml')
        original = ssh.call
        def cached(job, op, *args, **kwargs):
            self.assertNotEqual(op, 'put')
            return original(job, op, *args, **kwargs)
        with patch.object(ssh, 'call', side_effect=cached):
            self.assertEqual(measure.evaluate(second, 'baseline', 'anchor', 'remote-eval')['score'], 1)
