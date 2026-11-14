"""Process-based evaluation and global admission slots."""
import multiprocessing
import queue
import os
from pathlib import Path
import tempfile
import time
import unittest

from modules.evaluation.queued_python import executor


def hold_evaluation_slot(lock, admitted, release, deadline=None):
    try:
        with executor.evaluation_slot(Path(lock), deadline_monotonic=deadline):
            admitted.put(('entered', os.getpid()))
            release.wait(4)
    except Exception as exc:
        admitted.put(('error', str(exc)))


class EvaluationSlotsTests(unittest.TestCase):
    def test_unrelated_lock_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock"
            with executor._lock(lock):
                self.assertEqual(lock.read_bytes(), executor._LOCK_MARKER)
            lock.write_bytes(b"another project")
            with self.assertRaises(ValueError):
                with executor._lock(lock):
                    self.fail("entered unrelated lock")
            self.assertEqual(lock.read_bytes(), b"another project")
        with self.assertRaises(ValueError):
            with executor._lock(executor.PROJECT_ROOT.parent / "AnySolvers/runs/.lock"):
                self.fail("entered old project")

    def test_global_slots_admit_three_processes_and_queue_the_fourth(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as directory:
            lock = str(Path(directory) / 'global.lock')
            admitted, release = context.Queue(), context.Event()
            workers = [context.Process(target=hold_evaluation_slot, args=(lock, admitted, release))
                       for _ in range(4)]
            for worker in workers:
                worker.start()
            try:
                messages = [admitted.get(timeout=2) for _ in range(3)]
                self.assertTrue(all(message[0] == 'entered' for message in messages), messages)
                self.assertEqual(len({message[1] for message in messages}), 3)
                with self.assertRaises(queue.Empty):
                    admitted.get(timeout=.15)
                release.set()
                self.assertEqual(admitted.get(timeout=2)[0], 'entered')
            finally:
                release.set()
                for worker in workers:
                    worker.join(3)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join()
            self.assertTrue(all(worker.exitcode == 0 for worker in workers))
            admitted.close()

    def test_slots_respect_existing_global_exclusive_lock_and_queue_deadline(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'global.lock'
            admitted, release = context.Queue(), context.Event()
            with executor._lock(lock):
                worker = context.Process(target=hold_evaluation_slot,
                    args=(str(lock), admitted, release, time.monotonic() + .1))
                worker.start()
                try:
                    self.assertEqual(admitted.get(timeout=2), ('error', 'run_wall_budget'))
                finally:
                    release.set()
                    worker.join(3)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join()
            self.assertEqual(worker.exitcode, 0)
            admitted.close()

    def test_slot_tokens_cannot_be_reused_after_release_or_for_other_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'global.lock'
            with executor.evaluation_slot(lock) as token:
                token.check(lock)
                with self.assertRaisesRegex(ValueError, 'not held'):
                    token.check(lock.with_name('other.lock'))
            with self.assertRaisesRegex(ValueError, 'not held'):
                token.check(lock)


class ProcessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="anysolvers-v2-test-")
        self.addCleanup(self.directory.cleanup)
        self.lock = Path(self.directory.name) / "executor.lock"

    def run_candidate(self, source, limits=None, scorer=None):
        result = executor.execute(source, {"value": 7},
            limits or executor.DEFAULT_LIMITS, scorer or (lambda solution, case: {"score": 1}), self.lock)
        self.assertTrue(result["cleanup_confirmed"], result)
        self.assertFalse(result["infrastructure_error"], result)
        return result

    def test_errors_output_and_wall_limits(self):
        fixtures = [
            ("def solve(case):\n    raise RuntimeError('probe')", None, "candidate_runtime_error"),
            ("def solve(case):\n    return {1, 2}", None, "candidate_runtime_error"),
            ("def solve(case):\n    print('x'*100000)\n    return []", None, "output_limit"),
            ("def solve(case):\n    import time\n    time.sleep(20)", {**executor.DEFAULT_LIMITS, "wall_s": 3}, "wall_timeout"),
        ]
        for source, limits, error in fixtures:
            with self.subTest(error=error):
                result = self.run_candidate(source, limits)
                self.assertFalse(result["valid"], result)
                self.assertEqual(result["error"], error)


if __name__ == "__main__":
    unittest.main()
