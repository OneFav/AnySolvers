"""No Docker access by default; opt in with ANYSOLVERS_V2_DOCKER_TEST=1."""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import json
import multiprocessing
import queue
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import sandbox


def hold_evaluation_slot(lock, admitted, release, deadline=None):
    # Fixed trusted synchronization fixture; no Docker or candidate execution.
    try:
        with sandbox.evaluation_slot(Path(lock), deadline_monotonic=deadline):
            admitted.put(('entered', os.getpid()))
            release.wait(4)
    except Exception as exc:
        admitted.put(('error', str(exc)))


def inspected(name="sample", source=Path("/private/tmp/solver.py"), wrapper=Path("/private/tmp/entrypoint.py")):
    limits = sandbox.DEFAULT_LIMITS
    return {
        "Name": "/" + name, "Image": sandbox.PINNED_IMAGE,
        "Config": {"User": "65534:65534", "Labels": {sandbox.LABEL: name},
                   "Entrypoint": ["python"], "Cmd": ["-I", "-B", "/entrypoint.py"]},
        "HostConfig": {
            "ReadonlyRootfs": True, "NetworkMode": "none", "NanoCpus": 1_000_000_000,
            "Memory": limits["memory_mb"] * 1024**2, "MemorySwap": limits["memory_mb"] * 1024**2,
            "PidsLimit": limits["pids"], "CapDrop": ["ALL"], "CapAdd": None,
            "SecurityOpt": ["no-new-privileges"], "Privileged": False, "Devices": [],
            "DeviceRequests": None, "PidMode": "", "IpcMode": "private", "UTSMode": "", "Init": True,
            "Ulimits": [{"Name": name, "Soft": soft, "Hard": soft}
                        for name, soft in (("cpu", 2), ("nofile", 64), ("core", 0))],
            "LogConfig": {"Type": "none"},
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m,mode=1777"}},
        "Mounts": [{"Type": "bind", "Destination": target, "Source": str(path), "RW": False}
                   for target, path in (("/solver.py", source), ("/entrypoint.py", wrapper))]}


class SandboxUnitTests(unittest.TestCase):
    def test_configuration_rejects_each_changed_boundary(self):
        check = lambda value: sandbox._configuration_checks(value, sandbox.PINNED_IMAGE,
                Path("/private/tmp/solver.py"), Path("/private/tmp/entrypoint.py"), sandbox.DEFAULT_LIMITS, "sample")
        self.assertTrue(all(check(inspected()).values()))
        mutations = [("NanoCpus", 2_000_000_000), ("MemorySwap", -1), ("PidsLimit", 0),
                     ("Ulimits", []), ("Tmpfs", {}), ("ReadonlyRootfs", False),
                     ("NetworkMode", "host"), ("Privileged", True), ("PidMode", "host"),
                     ("DeviceRequests", [{"Capabilities": [["gpu"]]}]),
                     ("SecurityOpt", ["seccomp=unconfined"]), ("CapAdd", ["SYS_ADMIN"])]
        for field, value in mutations:
            with self.subTest(field=field):
                info = inspected()
                info["HostConfig"][field] = value
                with self.assertRaises(sandbox.InfrastructureError):
                    check(info)
        for mutation in (lambda x: x["Mounts"].append(copy.deepcopy(x["Mounts"][0])),
                         lambda x: x["Mounts"][0].update(RW=True),
                         lambda x: x["Config"].update(User="0:0"),
                         lambda x: x["Config"]["Labels"].clear()):
            info = inspected()
            mutation(info)
            with self.assertRaises(sandbox.InfrastructureError):
                check(info)

    def test_preflight_rejects_missing_seccomp_and_wrong_image(self):
        info = {"SecurityOptions": ["name=seccomp,profile=builtin"], "NCPU": 2,
                "MemTotal": 512 * 1024**2, "ServerVersion": "test"}
        images = [{"Id": sandbox.PINNED_IMAGE, "Architecture": "arm64"}]
        with patch.object(sandbox, "_json_command", side_effect=[info, images]):
            self.assertEqual(sandbox.preflight(sandbox.PINNED_IMAGE)["image_id"], sandbox.PINNED_IMAGE)
        info["SecurityOptions"] = []
        with patch.object(sandbox, "_json_command", side_effect=[info, images]):
            with self.assertRaises(sandbox.InfrastructureError):
                sandbox.preflight(sandbox.PINNED_IMAGE)
        with self.assertRaises(ValueError):
            sandbox.preflight("python:latest")

    def test_unrelated_lock_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock"
            with sandbox._lock(lock):
                self.assertEqual(lock.read_bytes(), sandbox._LOCK_MARKER)
            lock.write_bytes(b"another project")
            with self.assertRaises(ValueError):
                with sandbox._lock(lock):
                    self.fail("entered unrelated lock")
            self.assertEqual(lock.read_bytes(), b"another project")
        with self.assertRaises(ValueError):
            with sandbox._lock(sandbox.PROJECT_ROOT.parent / "AnySolvers/runs/.lock"):
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
            with sandbox._lock(lock):
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
            with sandbox.evaluation_slot(lock) as token:
                token.check(lock)
                with self.assertRaisesRegex(ValueError, 'not held'):
                    token.check(lock.with_name('other.lock'))
            with self.assertRaisesRegex(ValueError, 'not held'):
                token.check(lock)

    def test_three_slot_capacity_requires_three_cpus_and_complete_memory_reservation(self):
        facts = {'cpus': 3, 'memory_bytes': 3 * 128 * 1024**2}
        sandbox._check_capacity(facts, 3, sandbox.DEFAULT_LIMITS)
        for insufficient in ({**facts, 'cpus': 2}, {**facts, 'memory_bytes': 256 * 1024**2}):
            with self.assertRaisesRegex(sandbox.InfrastructureError, 'capacity'):
                sandbox._check_capacity(insufficient, 3, sandbox.DEFAULT_LIMITS)
        for bad in (0, 4, True):
            with self.assertRaises(ValueError):
                with sandbox.evaluation_slot(Path(tempfile.gettempdir()) / 'unused', bad):
                    self.fail('invalid capacity accepted')

    def test_cleanup_never_deletes_a_name_without_its_unique_label(self):
        with patch.object(sandbox, "_command", side_effect=["", "sample\n"]) as command:
            with self.assertRaises(sandbox.InfrastructureError):
                sandbox._cleanup("sample")
        self.assertFalse(any(call.args[0][0] == "rm" for call in command.call_args_list))
        self.assertIn("label=anysolvers.v2=sample", command.call_args_list[0].args[0])

    def test_bounded_io_handles_blocked_input_and_output(self):
        # Fixed test fixtures, never candidate code, run in these host subprocesses.
        fixtures = [("import time; time.sleep(5)", b"x" * 1_000_000, "wall_timeout", .1),
                    ("import sys; sys.stdout.write('x'*10000)", b"{}", "output_limit", 2)]
        for program, payload, expected, duration in fixtures:
            with self.subTest(expected=expected):
                process = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                streams, reason = sandbox._read_bounded(process, payload, time.monotonic() + duration, 64)
                self.assertEqual(reason, expected)
                self.assertLessEqual(sum(map(len, streams.values())), 64)
                self.assertIsNotNone(process.poll())

    def test_invalid_limits_fail_before_docker(self):
        limits = {**sandbox.DEFAULT_LIMITS, "pids": True}
        with patch.object(sandbox, "_command") as command:
            with self.assertRaises(ValueError):
                sandbox.execute("", {}, sandbox.PINNED_IMAGE, limits, lambda *args: {}, Path("unused"))
            command.assert_not_called()

    def test_deadline_checked_after_queue_before_docker(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sandbox, "preflight") as preflight:
            result = sandbox.execute("def solve(case): return []", {}, sandbox.PINNED_IMAGE,
                sandbox.DEFAULT_LIMITS, lambda *args: {"score": 1}, Path(directory) / "lock",
                deadline_monotonic=time.monotonic() - 1)
        self.assertTrue(result["admission_closed"])
        self.assertTrue(result["cleanup_confirmed"])
        self.assertFalse(result["infrastructure_error"])
        self.assertGreaterEqual(result["queue_wait_s"], 0)
        preflight.assert_not_called()

    def _mock_run(self, stdout, scorer=None, cleanup_error=False, limits=None):
        with tempfile.TemporaryDirectory() as directory:
            # Test staging is disposable even on hosts without /private/tmp.
            staging = Path(directory) / "staging"
            staging.mkdir()
            process = unittest.mock.Mock(returncode=0)

            def docker_json(args, timeout=10):
                if args[0:2] == ["inspect", "--format"]:
                    return {"ExitCode": 0, "Running": False, "OOMKilled": False}
                return [inspected(args[1], staging / "solver.py", staging / "entrypoint.py")]

            with patch.object(sandbox, "preflight", return_value={}), \
                 patch.object(sandbox.Path, "is_dir", return_value=True), \
                 patch.object(sandbox.tempfile, "mkdtemp", return_value=str(staging)), \
                 patch.object(sandbox, "_command", return_value=""), \
                 patch.object(sandbox, "_json_command", side_effect=docker_json), \
                 patch.object(sandbox.subprocess, "Popen", return_value=process), \
                 patch.object(sandbox, "_read_bounded", return_value=({"stdout": stdout, "stderr": b""}, None)), \
                 patch.object(sandbox, "_cleanup", side_effect=sandbox.InfrastructureError("cleanup") if cleanup_error else None):
                result = sandbox.execute("raise RuntimeError('MUST NOT RUN ON HOST')", {}, sandbox.PINNED_IMAGE,
                    limits or sandbox.DEFAULT_LIMITS, scorer or (lambda solution, case: {"score": 1, "metrics": {}}),
                    Path(directory) / "lock")
            if not cleanup_error:
                self.assertFalse(staging.exists())
            return result

    def test_static_scorer_timeout_is_bounded_and_classified_as_infrastructure(self):
        def hangs(solution, case):
            time.sleep(3)
            return {'score': 1}
        began = time.monotonic()
        result = self._mock_run(b'{"solution": []}', hangs,
                                limits={**sandbox.DEFAULT_LIMITS, 'wall_s': 1})
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertFalse(result['valid'])
        self.assertTrue(result['infrastructure_error'])
        self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(result['error'], 'environment_timeout')
        self.assertGreaterEqual(result['wall_s'], .9)

    def test_static_scoring_off_main_thread_is_rejected_before_docker(self):
        with patch.object(sandbox, 'preflight') as preflight, ThreadPoolExecutor(1) as pool:
            future = pool.submit(sandbox.execute, '', {}, sandbox.PINNED_IMAGE,
                                 sandbox.DEFAULT_LIMITS, lambda *args: {'score': 1}, Path('unused'))
            with self.assertRaisesRegex(ValueError, 'main thread'):
                future.result(1)
            preflight.assert_not_called()

    def test_compact_success_and_failure_diagnostics(self):
        result = self._mock_run(b'{"solution": [1, 2]}')
        self.assertTrue(result["valid"])
        self.assertTrue(result["cleanup_confirmed"])
        self.assertEqual(result["solution"], [1, 2])
        self.assertNotIn("diagnostic", result)
        for output in (b"bad json", b'{"score": 100}', b'{"solution": NaN}', b'{"solution": 1e999}'):
            with self.subTest(output=output):
                result = self._mock_run(output)
                self.assertFalse(result["valid"])
                self.assertFalse(result["infrastructure_error"])
                self.assertTrue(result["cleanup_confirmed"])
                self.assertIn("diagnostic", result)
        result = self._mock_run(b'{"solution": []}', lambda *args: {"score": float("inf")})
        self.assertFalse(result["valid"])
        result = self._mock_run(b'{"solution": []}', cleanup_error=True)
        self.assertTrue(result["infrastructure_error"])
        self.assertFalse(result["cleanup_confirmed"])


@unittest.skipUnless(os.environ.get("ANYSOLVERS_V2_DOCKER_TEST") == "1", "real Docker probes require explicit opt-in")
class DockerProbes(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="anysolvers-v2-test-")
        self.addCleanup(self.directory.cleanup)
        self.lock = Path(self.directory.name) / "sandbox.lock"

    def run_candidate(self, source, limits=None, scorer=None):
        result = sandbox.execute(source, {"value": 7}, sandbox.PINNED_IMAGE,
            limits or sandbox.DEFAULT_LIMITS, scorer or (lambda solution, case: {"score": 1}), self.lock)
        self.assertTrue(result["cleanup_confirmed"], result)
        self.assertFalse(result["infrastructure_error"], result)
        return result

    def test_function_and_boundaries(self):
        result = self.run_candidate('''def solve(case):
    import os, pathlib, socket
    readonly = False
    try:
        pathlib.Path('/solver.py').write_text('bad')
    except OSError:
        readonly = True
    network = False
    try:
        socket.create_connection(('1.1.1.1', 443), timeout=0.1).close()
        network = True
    except OSError:
        pass
    pathlib.Path('/tmp/probe').write_text('ok')
    return [case['value'], os.getuid(), readonly, network]
''')
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["solution"], [7, 65534, True, False])
        self.assertTrue(result["isolation_verified"])

    def test_errors_output_and_wall_limits(self):
        fixtures = [
            ("def solve(case):\n    raise RuntimeError('probe')", None, "candidate_runtime_error"),
            ("def solve(case):\n    return {1, 2}", None, "candidate_runtime_error"),
            ("def solve(case):\n    print('x'*100000)\n    return []", None, "output_limit"),
            ("def solve(case):\n    import time\n    time.sleep(20)", {**sandbox.DEFAULT_LIMITS, "wall_s": 3}, "wall_timeout"),
        ]
        for source, limits, error in fixtures:
            with self.subTest(error=error):
                result = self.run_candidate(source, limits)
                self.assertFalse(result["valid"], result)
                self.assertEqual(result["error"], error)


if __name__ == "__main__":
    unittest.main()
