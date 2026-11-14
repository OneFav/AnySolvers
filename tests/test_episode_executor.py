"""Interactive transport tests use a fixed host fixture, never candidate code."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.evaluation.queued_python import executor, runner


# Fixed protocol peer for transport unit tests. The supplied candidate source
# is only staged; it is never imported or executed on the host.
PEER = """import json, sys
counter = 0
for line in sys.stdin:
    counter += 1
    observation = json.loads(line)
    print(json.dumps({'action': {'count': counter, 'seen': observation}}), flush=True)
"""


def environment(steps=2):
    def reset(case):
        return {"time": 0, "private": case["future"]}, {"time": 0}

    def step(state, action):
        if action["count"] != state["time"] + 1:
            raise ValueError("Lost persistent candidate memory")
        if action["seen"] != {"time": state["time"]}:
            raise ValueError("Unexpected observation")
        state["time"] += 1
        return state, {"time": state["time"]}, state["time"] == steps

    def score_episode(state, trace, case):
        assert state["private"] == case["future"]
        assert len(trace) == steps
        return {"score": state["time"], "metrics": {"completed": steps}}

    return SimpleNamespace(reset=reset, step=step, score_episode=score_episode)


class EpisodeSandboxTests(unittest.TestCase):
    @contextmanager
    def peer(self, peer=PEER):
        with tempfile.TemporaryDirectory() as directory:
            original = subprocess.Popen
            processes = []
            def start(args, **kwargs):
                process = original([sys.executable, "-u", "-c", peer], **kwargs)
                processes.append(process)
                return process
            with patch.object(runner.subprocess, "Popen", side_effect=start) as launch:
                yield Path(directory) / "lock", processes, launch

    def run_episode(self, lock, env=None, **kwargs):
        return executor.execute_episode("raise RuntimeError('candidate must never run on host')",
            {"future": "PRIVATE_FUTURE_ORDERS"},
            kwargs.pop("limits", executor.DEFAULT_LIMITS), env or environment(), lock,
            max_steps=kwargs.pop("max_steps", 3), action_wall_s=kwargs.pop("action_wall_s", 1), **kwargs)

    def test_persistent_process_only_receives_observations(self):
        with self.peer() as (lock, processes, launch):
            result = self.run_episode(lock)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["score"], 2)
            self.assertEqual(result["steps"], 2)
            self.assertEqual([row["action"]["count"] for row in result["trace"]], [1, 2])
            self.assertEqual(result["trace"][0], {"step": 0, "observation": {"time": 0},
                "action": {"count": 1, "seen": {"time": 0}}, "next_observation": {"time": 1}, "done": False})
            self.assertNotIn("PRIVATE_FUTURE_ORDERS", json.dumps(result))
            self.assertTrue(result["trace"][-1]["done"])
            self.assertTrue(result["cleanup_confirmed"])
            launch.assert_called_once()
            self.assertIsNotNone(processes[0].poll())

    def test_next_case_uses_fresh_candidate_process(self):
        for _ in range(2):
            with self.peer() as (lock, processes, launch, *_):
                result = self.run_episode(lock)
                self.assertTrue(result["valid"], result)
                self.assertEqual(result["trace"][0]["action"]["count"], 1)

    def test_future_event_is_only_visible_after_environment_releases_it(self):
        env = environment()
        env.reset = lambda case: ({"time": 0, "future": case["future"]}, {"time": 0, "orders": []})

        def step(state, action):
            state["time"] += 1
            return state, {"time": state["time"], "orders": [state["future"]]}, state["time"] == 2

        env.step = step
        env.score_episode = lambda state, trace, case: {"score": 2, "metrics": {}}
        with self.peer() as (lock, *_):
            result = self.run_episode(lock, env)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["trace"][0]["action"]["seen"], {"time": 0, "orders": []})
        self.assertEqual(result["trace"][1]["action"]["seen"],
                         {"time": 1, "orders": ["PRIVATE_FUTURE_ORDERS"]})

    def test_trusted_callbacks_cannot_mutate_recorded_actions_or_trace(self):
        env = environment()
        original_step, original_score = env.step, env.score_episode

        def step(state, action):
            transition = original_step(state, action)
            action.clear()
            return transition

        def score(state, trace, case):
            result = original_score(state, trace, case)
            trace.clear()
            return result

        env.step, env.score_episode = step, score
        with self.peer() as (lock, *_):
            result = self.run_episode(lock, env)
        self.assertTrue(result["valid"], result)
        self.assertEqual([row["action"]["count"] for row in result["trace"]], [1, 2])

    def test_illegal_action_and_max_steps_are_invalid_results(self):
        bad = environment()
        bad.step = lambda state, action: (_ for _ in ()).throw(ValueError("blocked cell"))
        for env, steps, expected in [(bad, 3, "invalid_action: blocked cell"),
                                    (environment(steps=4), 2, "max_steps_exceeded")]:
            with self.subTest(expected=expected), self.peer() as (lock, _, _):
                result = self.run_episode(lock, env, max_steps=steps)
                self.assertFalse(result["valid"])
                self.assertFalse(result["infrastructure_error"], result)
                self.assertEqual(result["error"], expected)
                self.assertTrue(result["cleanup_confirmed"])

    def test_action_deadline_and_combined_output_limit(self):
        fixtures = [("import time; time.sleep(20)", .05, "action_timeout"),
                    ("import sys; sys.stderr.write('x'*70000); sys.stderr.flush()", 1, "output_limit"),
                    ("import sys; sys.stdout.write('{\\\"action\\\":NaN}\\n'); sys.stdout.flush()", 1, "invalid_action:")]
        for peer, timeout, expected in fixtures:
            with self.subTest(expected=expected), self.peer(peer) as (lock, processes, _):
                result = self.run_episode(lock, action_wall_s=timeout)
                self.assertFalse(result["valid"])
                self.assertTrue(result["error"].startswith(expected), result)
                self.assertTrue(result["cleanup_confirmed"])
                self.assertIsNotNone(processes[0].poll())

    def test_each_response_has_its_own_deadline(self):
        peer = PEER.replace("import json, sys", "import json, sys, time").replace(
            "counter += 1", "counter += 1\n    if counter == 2: time.sleep(2)")
        with self.peer(peer) as (lock, *_):
            result = self.run_episode(lock, action_wall_s=.1)
        self.assertEqual(result["error"], "action_timeout")
        self.assertEqual(result["steps"], 1)
        self.assertFalse(result["infrastructure_error"])
        self.assertTrue(result["cleanup_confirmed"])

    def test_episode_total_wall_deadline_remains_in_force(self):
        peer = PEER.replace("import json, sys", "import json, sys, time").replace(
            "counter += 1", "counter += 1\n    time.sleep(.4)")
        with self.peer(peer) as (lock, *_):
            result = self.run_episode(lock, environment(steps=3), action_wall_s=.6,
                                      limits={**executor.DEFAULT_LIMITS, "wall_s": 1})
        self.assertEqual(result["error"], "wall_timeout")
        self.assertEqual(result["steps"], 2)
        self.assertTrue(result["cleanup_confirmed"])

    def test_output_cap_accumulates_across_actions(self):
        peer = "import json,sys\nfor line in sys.stdin:\n print(json.dumps({'action':'x'*40000}),flush=True)\n"
        env = environment()
        env.step = lambda state, action: (state, {}, False)
        with self.peer(peer) as (lock, *_):
            result = self.run_episode(lock, env)
        self.assertEqual(result["error"], "output_limit")
        self.assertEqual(result["steps"], 1)

    def test_large_or_nonfinite_observation_is_environment_error(self):
        for observation in ({"private": "x" * 70000}, {"value": float("nan")}, [], None, "not an object"):
            env = environment()
            env.reset = lambda case: ({}, observation)
            with self.subTest(observation_type=type(observation)), self.peer() as (lock, *_):
                result = self.run_episode(lock, env)
                self.assertFalse(result["valid"])
                self.assertTrue(result["infrastructure_error"])
                self.assertTrue(result["cleanup_confirmed"])

    def test_transition_observation_must_be_an_object(self):
        env = environment()
        env.step = lambda state, action: (state, ["not an object"], True)
        with self.peer() as (lock, *_):
            result = self.run_episode(lock, env)
        self.assertTrue(result["infrastructure_error"])
        self.assertIn("observation must be a JSON object", result["error"])
        self.assertTrue(result["cleanup_confirmed"])

    def test_trusted_callback_deadline_restores_alarm(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(runner._EpisodeFailure, "environment_timeout"):
            runner._trusted_call(time.monotonic() + .05, time.sleep, 2)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_non_main_thread_rejected_before_launch(self):
        with self.peer() as (lock, _, launch), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.run_episode, lock)
            with self.assertRaisesRegex(ValueError, "main thread"):
                future.result()
            launch.assert_not_called()

    def test_environment_timeout_still_cleans_up(self):
        env = environment()
        env.reset = lambda case: time.sleep(2)
        original = runner._trusted_call
        with self.peer() as (lock, processes, launch), \
             patch.object(runner, "_trusted_call", side_effect=lambda deadline, callback, *args:
                          original(min(deadline, time.monotonic() + .05), callback, *args)):
            result = self.run_episode(lock, env)
            self.assertEqual(result["error"], "environment_timeout")
            self.assertTrue(result["infrastructure_error"])
            self.assertTrue(result["cleanup_confirmed"])
            launch.assert_called_once()
            self.assertIsNotNone(processes[0].poll())

    def test_invalid_protocol_limits_are_rejected_before_launch(self):
        with self.peer() as (lock, _, launch):
            for args in ({"max_steps": 0}, {"max_steps": True}, {"action_wall_s": float("nan")},
                         {"action_wall_s": 0}, {"action_wall_s": 11}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    self.run_episode(lock, **args)
            launch.assert_not_called()

    def test_admission_deadline_prevents_new_process(self):
        with self.peer() as (lock, _, launch):
            result = self.run_episode(lock, deadline_monotonic=time.monotonic() - 1)
            self.assertTrue(result["admission_closed"])
            self.assertTrue(result["cleanup_confirmed"])
            launch.assert_not_called()
