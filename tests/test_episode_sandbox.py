"""Interactive transport tests use a fixed host fixture, never candidate code."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import sandbox
from test_sandbox import inspected


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
    def mocked_docker(self, peer=PEER, cleanup_error=False, mutate_config=None):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            original_popen = subprocess.Popen
            processes = []

            def start(*args, **kwargs):
                process = original_popen([sys.executable, "-u", "-c", peer], **kwargs)
                processes.append(process)
                return process

            def docker_json(args, timeout=10):
                if args[:2] == ["inspect", "--format"]:
                    return {"ExitCode": 0, "Running": False, "OOMKilled": False}
                config = inspected(args[1], staging / "solver.py", staging / "entrypoint.py")
                if mutate_config:
                    mutate_config(config)
                return [config]

            with patch.object(sandbox, "preflight", return_value={}), \
                 patch.object(sandbox.Path, "is_dir", return_value=True), \
                 patch.object(sandbox.tempfile, "mkdtemp", return_value=str(staging)), \
                 patch.object(sandbox, "_command", return_value="") as command, \
                 patch.object(sandbox, "_json_command", side_effect=docker_json), \
                 patch.object(sandbox.subprocess, "Popen", side_effect=start) as launch, \
                 patch.object(sandbox, "_cleanup", side_effect=sandbox.InfrastructureError("cleanup unknown") if cleanup_error else None) as cleanup:
                yield Path(directory) / "lock", processes, launch, cleanup, command
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def run_episode(self, lock, env=None, **kwargs):
        return sandbox.execute_episode("raise RuntimeError('candidate must never run on host')",
            {"future": "PRIVATE_FUTURE_ORDERS"}, sandbox.PINNED_IMAGE,
            kwargs.pop("limits", sandbox.DEFAULT_LIMITS), env or environment(), lock,
            max_steps=kwargs.pop("max_steps", 3), action_wall_s=kwargs.pop("action_wall_s", 1), **kwargs)

    def test_persistent_process_only_receives_observations(self):
        with self.mocked_docker() as (lock, processes, launch, cleanup, command):
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
            self.assertTrue(result["isolation_verified"])
            launch.assert_called_once()
            cleanup.assert_called_once()
            self.assertIsNotNone(processes[0].poll())
            create = command.call_args_list[0].args[0]
            self.assertEqual(create.count("--mount"), 2)
            self.assertNotIn("PRIVATE_FUTURE_ORDERS", " ".join(create))

    def test_next_case_uses_fresh_candidate_process(self):
        for _ in range(2):
            with self.mocked_docker() as (lock, processes, launch, *_):
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
        with self.mocked_docker() as (lock, *_):
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
        with self.mocked_docker() as (lock, *_):
            result = self.run_episode(lock, env)
        self.assertTrue(result["valid"], result)
        self.assertEqual([row["action"]["count"] for row in result["trace"]], [1, 2])

    def test_illegal_action_and_max_steps_are_invalid_results(self):
        bad = environment()
        bad.step = lambda state, action: (_ for _ in ()).throw(ValueError("blocked cell"))
        for env, steps, expected in [(bad, 3, "invalid_action: blocked cell"),
                                    (environment(steps=4), 2, "max_steps_exceeded")]:
            with self.subTest(expected=expected), self.mocked_docker() as (lock, _, _, cleanup, _):
                result = self.run_episode(lock, env, max_steps=steps)
                self.assertFalse(result["valid"])
                self.assertFalse(result["infrastructure_error"], result)
                self.assertEqual(result["error"], expected)
                self.assertTrue(result["cleanup_confirmed"])
                cleanup.assert_called_once()

    def test_action_deadline_and_combined_output_limit(self):
        fixtures = [("import time; time.sleep(20)", .05, "action_timeout"),
                    ("import sys; sys.stderr.write('x'*70000); sys.stderr.flush()", 1, "output_limit"),
                    ("import sys; sys.stdout.write('{\\\"action\\\":NaN}\\n'); sys.stdout.flush()", 1, "invalid_action:")]
        for peer, timeout, expected in fixtures:
            with self.subTest(expected=expected), self.mocked_docker(peer) as (lock, processes, _, cleanup, _):
                result = self.run_episode(lock, action_wall_s=timeout)
                self.assertFalse(result["valid"])
                self.assertTrue(result["error"].startswith(expected), result)
                self.assertTrue(result["cleanup_confirmed"])
                self.assertIsNotNone(processes[0].poll())
                cleanup.assert_called_once()

    def test_each_response_has_its_own_deadline(self):
        peer = PEER.replace("import json, sys", "import json, sys, time").replace(
            "counter += 1", "counter += 1\n    if counter == 2: time.sleep(2)")
        with self.mocked_docker(peer) as (lock, *_):
            result = self.run_episode(lock, action_wall_s=.1)
        self.assertEqual(result["error"], "action_timeout")
        self.assertEqual(result["steps"], 1)
        self.assertFalse(result["infrastructure_error"])
        self.assertTrue(result["cleanup_confirmed"])

    def test_episode_total_wall_deadline_remains_in_force(self):
        peer = PEER.replace("import json, sys", "import json, sys, time").replace(
            "counter += 1", "counter += 1\n    time.sleep(.4)")
        with self.mocked_docker(peer) as (lock, *_):
            result = self.run_episode(lock, environment(steps=3), action_wall_s=.6,
                                      limits={**sandbox.DEFAULT_LIMITS, "wall_s": 1})
        self.assertEqual(result["error"], "wall_timeout")
        self.assertEqual(result["steps"], 2)
        self.assertTrue(result["cleanup_confirmed"])

    def test_output_cap_accumulates_across_actions(self):
        peer = "import json,sys\nfor line in sys.stdin:\n print(json.dumps({'action':'x'*40000}),flush=True)\n"
        env = environment()
        env.step = lambda state, action: (state, {}, False)
        with self.mocked_docker(peer) as (lock, *_):
            result = self.run_episode(lock, env)
        self.assertEqual(result["error"], "output_limit")
        self.assertEqual(result["steps"], 1)

    def test_large_or_nonfinite_observation_is_environment_error(self):
        for observation in ({"private": "x" * 70000}, {"value": float("nan")}, [], None, "not an object"):
            env = environment()
            env.reset = lambda case: ({}, observation)
            with self.subTest(observation_type=type(observation)), self.mocked_docker() as (lock, *_):
                result = self.run_episode(lock, env)
                self.assertFalse(result["valid"])
                self.assertTrue(result["infrastructure_error"])
                self.assertTrue(result["cleanup_confirmed"])

    def test_transition_observation_must_be_an_object(self):
        env = environment()
        env.step = lambda state, action: (state, ["not an object"], True)
        with self.mocked_docker() as (lock, *_):
            result = self.run_episode(lock, env)
        self.assertTrue(result["infrastructure_error"])
        self.assertIn("observation must be a JSON object", result["error"])
        self.assertTrue(result["cleanup_confirmed"])

    def test_trusted_callback_deadline_restores_alarm(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(sandbox._EpisodeFailure, "environment_timeout"):
            sandbox._trusted_call(time.monotonic() + .05, time.sleep, 2)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_non_main_thread_rejected_before_docker(self):
        with self.mocked_docker() as (lock, _, launch, cleanup, _), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.run_episode, lock)
            with self.assertRaisesRegex(ValueError, "main thread"):
                future.result()
            launch.assert_not_called()
            cleanup.assert_not_called()

    def test_environment_timeout_still_removes_container(self):
        env = environment()
        env.reset = lambda case: time.sleep(2)
        original = sandbox._trusted_call
        with self.mocked_docker() as (lock, _, launch, cleanup, _), \
             patch.object(sandbox, "_trusted_call", side_effect=lambda deadline, callback, *args:
                          original(min(deadline, time.monotonic() + .05), callback, *args)):
            result = self.run_episode(lock, env)
            self.assertEqual(result["error"], "environment_timeout")
            self.assertTrue(result["infrastructure_error"])
            self.assertTrue(result["cleanup_confirmed"])
            launch.assert_not_called()
            cleanup.assert_called_once()

    def test_invalid_protocol_limits_are_rejected_before_docker(self):
        with self.mocked_docker() as (lock, _, launch, cleanup, command):
            for args in ({"max_steps": 0}, {"max_steps": True}, {"action_wall_s": float("nan")},
                         {"action_wall_s": 0}, {"action_wall_s": 11}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    self.run_episode(lock, **args)
            launch.assert_not_called()
            cleanup.assert_not_called()
            command.assert_not_called()

    def test_configuration_failure_never_starts_and_cleanup_failure_visible(self):
        with self.mocked_docker(mutate_config=lambda info: info["HostConfig"].update(NetworkMode="host")) as (lock, _, launch, cleanup, _):
            result = self.run_episode(lock)
            self.assertTrue(result["infrastructure_error"])
            launch.assert_not_called()
            cleanup.assert_called_once()
        with self.mocked_docker(cleanup_error=True) as (lock, *_):
            result = self.run_episode(lock)
            self.assertTrue(result["infrastructure_error"])
            self.assertFalse(result["cleanup_confirmed"])
            self.assertIn("cleanup unknown", result["cleanup_error"])

    def test_admission_deadline_prevents_new_container(self):
        with self.mocked_docker() as (lock, _, launch, cleanup, command):
            result = self.run_episode(lock, deadline_monotonic=time.monotonic() - 1)
            self.assertTrue(result["admission_closed"])
            self.assertTrue(result["cleanup_confirmed"])
            launch.assert_not_called()
            command.assert_not_called()
            cleanup.assert_not_called()


@unittest.skipUnless(os.environ.get("ANYSOLVERS_V2_DOCKER_TEST") == "1", "real Docker probes require explicit opt-in")
class EpisodeDockerProbe(unittest.TestCase):
    def test_observation_only_stateful_candidate(self):
        source = """counter = 0
def solve(observation):
    global counter
    counter += 1
    return {'count': counter, 'seen': observation}
"""
        with tempfile.TemporaryDirectory() as directory:
            result = sandbox.execute_episode(source, {"future": "never sent"}, sandbox.PINNED_IMAGE,
                sandbox.DEFAULT_LIMITS, environment(), Path(directory) / "lock", max_steps=3, action_wall_s=3)
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["cleanup_confirmed"], result)
        self.assertFalse(result["infrastructure_error"], result)
        self.assertEqual(result["score"], 2)
        self.assertEqual([row["action"]["count"] for row in result["trace"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
