"""Measurement bookkeeping tests with a mocked sandbox; never start Docker."""
from concurrent.futures import ThreadPoolExecutor
import copy
import datetime as dt
import threading
import time
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import common, measure, sandbox
from modules.evaluation.queued_sandbox import measure as implementation


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="anysolvers-v2-measure-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run = self.root / "runs/test"
        self.run.mkdir(parents=True)
        (self.run / "research").mkdir()
        source = self.run / "candidate.py"
        source.write_text("raise RuntimeError('must never execute on host')")
        cases = self.run / "cases.jsonl"
        cases.write_text(json.dumps({"case_id": "first", "input": {"value": 2}}) + "\n" +
                         json.dumps({"case_id": "second", "input": {"value": 4}}) + "\n")
        candidate = {"manifest": {"candidate_id": "candidate", "task_id": "test", "source_file": "candidate.py"},
                     "files": {"candidate.py": common.sha(source)}}
        dataset = {"manifest": {"dataset_id": "dataset", "task_id": "test", "cases_file": "cases.jsonl", "purpose": "anchor"},
                   "files": {"cases.jsonl": common.sha(cases)}}
        self.state = {"deliveries": {"candidate": {"candidate": candidate}, "dataset": {"dataset": dataset}},
                      "evaluations": {}, "selection": None}
        self.config = {"created_at": common.utc(), "task": {"task_id": "test", "files": {}},
                       "compute": {"image": sandbox.PINNED_IMAGE, "limits": dict(sandbox.DEFAULT_LIMITS)},
                       "budgets": {"run_wall_s": 3600, "evaluation_calls": 2, "probe_calls": 1,
                                   "max_cases_per_dataset": 12}, "final": {"authorized": True}}
        trusted = SimpleNamespace(validate_input=lambda case: None, score=lambda solution, case: {"score": 0},
                                  protocol={"kind": "function"}, signature="unit-fixture", baseline_path=None)
        self.environment = trusted
        for patcher in (patch.object(common, "ROOT", self.root),
                        patch.object(common, "load_run", side_effect=lambda run_id: (self.run, self.config, self.state)),
                        patch.object(common, "validate", return_value=None),
                        patch.object(common, "benchmark_environment", return_value=trusted)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def valid(self, score=2):
        return {"valid": True, "score": score, "solution": ["ok"], "metrics": {},
                "infrastructure_error": False, "cleanup_confirmed": True, "wall_s": .2, "queue_wait_s": .1}

    def run_evaluation(self, evaluation_id="eval", probe=False):
        return measure.evaluate("test", "candidate", "dataset", evaluation_id, probe)

    def test_baseline_bytes_rechecked_after_environment_load(self):
        source = self.run / 'baseline.py'
        source.write_text('def solve(case): return 0')
        self.environment.baseline_path = source
        self.environment.baseline_sha = common.sha(source)
        source.write_text('changed after environment loading')
        with patch.object(sandbox, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'sealed file changed'):
                measure.evaluate('test', 'baseline', 'dataset', 'changed-baseline')
            execute.assert_not_called()
        self.assertEqual(self.state['evaluations'], {})

    def test_episode_budget_mismatch_rejected_before_attempt(self):
        self.environment.protocol = {'kind': 'episode', 'max_steps': 2, 'action_wall_s': 999}
        with patch.object(sandbox, 'execute_episode') as execute:
            with self.assertRaisesRegex(ValueError, 'action budget'):
                self.run_evaluation()
            execute.assert_not_called()
        self.assertEqual(self.state['evaluations'], {})

    def test_input_validator_cannot_modify_input_sent_to_candidate(self):
        self.environment.validate_input = lambda case: case.update(value=999)
        with patch.object(sandbox, 'execute', return_value=self.valid()) as execute:
            self.run_evaluation()
        self.assertEqual([call.args[1]['value'] for call in execute.call_args_list], [2, 4])

    def test_success_cached_without_budget_or_execution(self):
        with patch.object(sandbox, "execute", side_effect=[self.valid(2), self.valid(4)]) as execute:
            summary = self.run_evaluation()
            cached = self.run_evaluation()
            alias = self.run_evaluation("new-id")
        self.assertEqual(execute.call_count, 2)
        self.assertTrue(cached.pop("cached"))
        self.assertEqual(summary, cached)
        self.assertEqual(summary["score"], 3)
        self.assertTrue(summary["all_valid"])
        self.assertEqual(summary["queue_wait_s"], .2)
        self.assertEqual(len(self.state["evaluations"]), 1)
        self.assertEqual(alias["evaluation_id"], "eval")
        self.assertTrue(alias["cached"])
        records = measure.details("test", "eval")["records"]
        self.assertEqual([row["type"] for row in records], ["attempt", "result", "attempt", "result"])
        self.assertNotIn("input", records[0])
        self.assertEqual(len(measure.details("test", "eval", "first")["records"]), 2)

    def test_incomplete_or_different_request_never_replayed(self):
        with patch.object(sandbox, "execute", return_value=self.valid()):
            summary = self.run_evaluation()
        with patch.object(sandbox, "execute") as execute:
            self.config["compute"]["limits"]["cpu_s"] += 1
            with self.assertRaisesRegex(ValueError, "different request"):
                self.run_evaluation()
            self.config["compute"]["limits"]["cpu_s"] -= 1
            summary["status"] = "running"
            common.write_json(self.run / self.state["evaluations"]["eval"], summary)
            with self.assertRaisesRegex(ValueError, "manual reconciliation"):
                self.run_evaluation()
            execute.assert_not_called()

    def test_caps_and_deadline_reject_before_sandbox(self):
        with patch.object(sandbox, "execute", return_value=self.valid()) as execute:
            self.run_evaluation("probe", probe=True)
            self.config["compute"]["limits"]["cpu_s"] += 1
            with self.assertRaisesRegex(ValueError, "probe_calls"):
                self.run_evaluation("another-probe", probe=True)
            self.run_evaluation("second")
            self.config["compute"]["limits"]["cpu_s"] += 1
            with self.assertRaisesRegex(ValueError, "evaluation_calls"):
                self.run_evaluation("third")
            self.assertEqual(execute.call_count, 4)
        self.state["evaluations"] = {}
        self.config["created_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        with patch.object(sandbox, "execute") as execute:
            with self.assertRaisesRegex(ValueError, "run_wall_s"):
                self.run_evaluation("late")
            execute.assert_not_called()

    def test_unknown_cleanup_stops_and_blocks_further_execution(self):
        failed = {**self.valid(), "infrastructure_error": True, "cleanup_confirmed": False, "error": "cleanup"}
        with patch.object(sandbox, "execute", return_value=failed) as execute:
            summary = self.run_evaluation()
            with self.assertRaisesRegex(ValueError, "manual reconciliation"):
                self.run_evaluation()
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["all_valid"])
        self.assertIsNone(summary["score"])

    def test_one_invalid_case_prevents_mean(self):
        invalid = {**self.valid(), "valid": False, "score": None}
        with patch.object(sandbox, "execute", side_effect=[self.valid(), invalid]):
            summary = self.run_evaluation()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["valid_cases"], 1)
        self.assertFalse(summary["all_valid"])
        self.assertIsNone(summary["score"])

    def test_unexpected_helper_error_has_unknown_timing(self):
        with patch.object(sandbox, "execute", side_effect=RuntimeError("unexpected")):
            summary = self.run_evaluation()
        self.assertIsNone(summary["wall_s"])
        self.assertIsNone(summary["queue_wait_s"])
        self.assertFalse(summary["cleanup_confirmed"])
        self.assertTrue(summary["infrastructure_error"])
        self.assertIn("eval", self.state["evaluation_hashes"])

    def test_changed_summary_or_journal_blocks_cache(self):
        with patch.object(sandbox, "execute", return_value=self.valid()):
            self.run_evaluation()
        path = self.run / self.state["evaluations"]["eval"]
        original = path.read_bytes()
        summary = common.read_json(path)
        summary["score"] = 100
        common.write_json(path, summary)
        with self.assertRaisesRegex(ValueError, "changed evaluation summary"):
            self.run_evaluation()
        path.write_bytes(original)
        (path.parent / "results.jsonl").write_text("changed")
        with self.assertRaisesRegex(ValueError, "journal changed"):
            self.run_evaluation()

    def test_final_gate_and_mutation_lock(self):
        self.state["deliveries"]["dataset"]["dataset"]["manifest"]["purpose"] = "final"
        with patch.object(sandbox, "execute") as execute:
            with self.assertRaisesRegex(ValueError, "frozen selection"):
                self.run_evaluation()
            self.state["selection"] = {"candidate_id": "other", "source_sha": "wrong"}
            with self.assertRaisesRegex(ValueError, "frozen candidate"):
                self.run_evaluation()
            execute.assert_not_called()
        self.state["deliveries"]["dataset"]["dataset"]["manifest"]["purpose"] = "anchor"
        with common.mutation(self.run), patch.object(sandbox, "execute") as execute:
            with self.assertRaisesRegex(ValueError, "active"):
                self.run_evaluation()
            execute.assert_not_called()

    def _parallel_candidates(self, count):
        self.config['budgets']['evaluation_calls'] = None
        self.config['compute']['max_parallel_evaluations'] = 3
        for index in range(count):
            candidate = copy.deepcopy(self.state['deliveries']['candidate']['candidate'])
            candidate['manifest']['candidate_id'] = 'candidate-' + str(index)
            self.state['deliveries']['candidate']['candidate-' + str(index)] = candidate

    def _parallel_evaluate(self, index):
        return implementation.evaluate('test', 'candidate-' + str(index), 'dataset',
                                       'eval-' + str(index), research_session='session')

    def test_three_evaluations_overlap_and_fourth_waits_without_spending_evaluation_time(self):
        self._parallel_candidates(4)
        entered, release, guard = threading.Event(), threading.Event(), threading.Lock()
        active, peak, first_entries = 0, 0, []
        def execute(*args, **kwargs):
            nonlocal active, peak
            kwargs['evaluation_slot_token'].check(args[5])
            with guard:
                active += 1
                peak = max(peak, active)
                if args[1]['value'] == 2:
                    first_entries.append(threading.get_ident())
                    if len(first_entries) == 3:
                        entered.set()
            try:
                if args[1]['value'] == 2:
                    self.assertTrue(release.wait(3))
                return self.valid()
            finally:
                with guard:
                    active -= 1
        with patch.object(implementation, '_research_admission'), \
             patch.object(sandbox, 'execute', side_effect=execute), ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(self._parallel_evaluate, i) for i in range(4)]
            try:
                self.assertTrue(entered.wait(3))
                time.sleep(.15)
                self.assertEqual(len(first_entries), 3)
            finally:
                release.set()
            results = [future.result(3) for future in futures]
        self.assertEqual(peak, 3)
        self.assertEqual(len(self.state['evaluations']), 4)
        self.assertTrue(all(result['all_valid'] for result in results), results)
        queued = max(results, key=lambda result: result['admission_queue_wait_s'])
        self.assertGreaterEqual(queued['admission_queue_wait_s'], .14)
        self.assertLess(queued['evaluation_elapsed_s'], .1)

    def test_duplicate_snapshot_waits_and_reuses_one_completed_measurement(self):
        self.config['budgets']['evaluation_calls'] = None
        entered, release = threading.Event(), threading.Event()
        def execute(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.valid()
        with patch.object(implementation, '_research_admission'), \
             patch.object(sandbox, 'execute', side_effect=execute) as mocked, ThreadPoolExecutor(2) as pool:
            first = pool.submit(implementation.evaluate, 'test', 'candidate', 'dataset', 'first-eval',
                                research_session='session')
            self.assertTrue(entered.wait(3))
            second = pool.submit(implementation.evaluate, 'test', 'candidate', 'dataset', 'second-eval',
                                 research_session='session')
            time.sleep(.1)
            self.assertFalse(second.done())
            release.set()
            result, cached = first.result(3), second.result(3)
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(result['all_valid'])
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['evaluation_id'], 'first-eval')
        self.assertEqual(len(self.state['evaluations']), 1)

    def test_null_limits_allow_multiple_evaluations_and_case_count(self):
        self.config['budgets'].update(evaluation_calls=None, max_cases_per_dataset=None)
        with patch.object(sandbox, 'execute', return_value=self.valid()) as execute:
            for index in range(4):
                self.config['compute']['limits']['cpu_s'] += 1
                self.assertTrue(self.run_evaluation('unlimited-' + str(index))['all_valid'])
        self.assertEqual(execute.call_count, 8)

    def test_evaluation_deadline_stops_before_shrinking_next_case_budget(self):
        self.config['compute']['evaluation_wall_s'] = 81
        original_limits = dict(self.config['compute']['limits'])
        clock = [1000.0]
        def execute(*args, **kwargs):
            self.assertEqual(args[3], original_limits)
            clock[0] += 2.0
            return self.valid()
        with patch.object(implementation.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(sandbox, 'execute', side_effect=execute) as mocked:
            result = self.run_evaluation()
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result['error'], 'evaluation_wall_budget')
        self.assertEqual(result['completed_cases'], 1)
        self.assertFalse(result['all_valid'])
        self.assertEqual(result['evaluation_elapsed_s'], 2)
        self.assertIsNone(result['score'])

    def test_known_run_expiry_before_registration_is_marked_not_started(self):
        self.config['created_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        with self.assertRaises(implementation.EvaluationNotStarted) as failure:
            self.run_evaluation()
        self.assertTrue(failure.exception.evaluation_not_started)
        self.assertEqual(self.state['evaluations'], {})


if __name__ == "__main__":
    unittest.main()
