"""Opt-in real container check; four tiny jobs, no models or research runs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from tools import sandbox


CHILD = r'''
import json, sys, time
from pathlib import Path
from tools import sandbox
queued = time.monotonic()
lock = Path(sys.argv[1])
with sandbox.evaluation_slot(lock, 3, deadline_monotonic=queued + 120) as slot:
    admitted = time.monotonic()
    result = sandbox.execute(
        "import time\ndef solve(case):\n    time.sleep(2)\n    return case['value']\n",
        {'value': 7}, sandbox.PINNED_IMAGE, sandbox.DEFAULT_LIMITS,
        lambda solution, case: {'score': float(solution == case['value']), 'metrics': {}},
        lock, evaluation_slot_token=slot)
    finished = time.monotonic()
print(json.dumps({'queued': queued, 'admitted': admitted, 'finished': finished,
                  'result': result}))
'''


@unittest.skipUnless(os.getenv('ANYSOLVERS_V2_DOCKER_TEST') == '1', 'real Docker opt-in')
class ThreeSlotDockerTests(unittest.TestCase):
    def test_four_real_jobs_use_three_slots_and_keep_case_limits(self):
        facts = sandbox.preflight(sandbox.PINNED_IMAGE)
        self.assertGreaterEqual(facts['cpus'], 4)
        self.assertGreaterEqual(facts['memory_bytes'], 1024 * 1024**2)
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='anysolvers-slots-', dir='/private/tmp') as directory:
            jobs = [subprocess.Popen([sys.executable, '-B', '-c', CHILD,
                                      str(Path(directory)/'global.lock')], cwd=root,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(4)]
            results = []
            try:
                for job in jobs:
                    stdout, stderr = job.communicate(timeout=180)
                    self.assertEqual(job.returncode, 0, stderr)
                    results.append(json.loads(stdout))
            finally:
                for job in jobs:
                    if job.poll() is None:
                        job.terminate()
                        job.wait(timeout=10)
            points = sorted([(r['admitted'], 1) for r in results] +
                            [(r['finished'], -1) for r in results])
            active = peak = 0
            for _, delta in points:
                active += delta
                peak = max(peak, active)
            self.assertEqual(peak, 3)
            self.assertGreater(max(r['admitted']-r['queued'] for r in results), 1)
            for record in results:
                result = record['result']
                self.assertTrue(result['valid'], result)
                self.assertTrue(result['isolation_verified'])
                self.assertTrue(result['cleanup_confirmed'])
                self.assertFalse(result['infrastructure_error'])
                self.assertEqual(result['score'], 1.0)


if __name__ == '__main__':
    unittest.main()
