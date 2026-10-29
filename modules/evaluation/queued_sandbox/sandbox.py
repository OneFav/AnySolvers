"""Bounded Docker bridges for solve(case) and interactive solve(observation).

Adapted mechanics: AnySolvers/src/anysolvers/sandbox/{compact,docker,
function_entrypoint}.py. No old project code, state, or imports are used.
Candidate code is never imported or executed by the host interpreter.
Interactive environments remain trusted host code and expose observations only.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

PINNED_IMAGE = "sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
DEFAULT_LIMITS = {"wall_s": 10, "cpu_s": 2, "memory_mb": 128, "pids": 32,
                  "output_bytes": 65536, "tmp_mb": 16}
LABEL = "anysolvers.v2"
MAX_PARALLEL_EVALUATIONS = 3
PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LOCK_MARKER = b"anysolvers.v2 sandbox lock\n"
_BRIDGE = '''import contextlib, json, runpy, sys
case = json.load(sys.stdin)
with contextlib.redirect_stdout(sys.stderr):
    solve = runpy.run_path('/solver.py', run_name='candidate')['solve']
    solution = solve(case)
print(json.dumps({'solution': solution}, allow_nan=False, separators=(',', ':')))
'''
_EPISODE_BRIDGE = '''import contextlib, json, runpy, sys
with contextlib.redirect_stdout(sys.stderr):
    solve = runpy.run_path('/solver.py', run_name='candidate')['solve']
for line in sys.stdin:
    observation = json.loads(line)
    with contextlib.redirect_stdout(sys.stderr):
        action = solve(observation)
    print(json.dumps({'action': action}, allow_nan=False, separators=(',', ':')), flush=True)
'''


class InfrastructureError(RuntimeError):
    """The sandbox could not establish or confirm its execution boundary."""


class _AdmissionClosed(RuntimeError):
    pass


def _command(args, timeout=10):
    if timeout <= 0:
        raise InfrastructureError("Docker operation exceeded wall budget")
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True,
                                timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InfrastructureError(f"Docker {args[0]} unavailable or timed out") from exc
    if result.returncode:
        raise InfrastructureError(f"Docker {args[0]} failed: {result.stderr[:1000]}")
    return result.stdout


def _json_command(args, timeout=10):
    try:
        return json.loads(_command(args, timeout))
    except (ValueError, TypeError) as exc:
        raise InfrastructureError(f"Malformed Docker {args[0]} response") from exc


def _check_image(image):
    if image != PINNED_IMAGE:
        raise ValueError("This protocol requires the fixed PINNED_IMAGE; no pulls or tags")


def preflight(image):
    """Read local Docker/image facts; never pull images or alter containers."""
    _check_image(image)
    info = _json_command(["info", "--format", "{{json .}}"])
    images = _json_command(["image", "inspect", image])
    try:
        security = info["SecurityOptions"]
        if not any("seccomp" in option and "builtin" in option for option in security):
            raise InfrastructureError("Docker built-in seccomp is required")
        if images[0]["Id"] != image:
            raise InfrastructureError("Installed image ID mismatch")
        if info["NCPU"] < 2 or info["MemTotal"] < 512 * 1024**2:
            raise InfrastructureError("Insufficient Docker capacity")
        return {"image_id": images[0]["Id"], "architecture": images[0]["Architecture"],
                "docker_version": info["ServerVersion"], "cpus": info["NCPU"],
                "memory_bytes": info["MemTotal"], "security": security}
    except (KeyError, IndexError, TypeError) as exc:
        raise InfrastructureError("Incomplete Docker preflight response") from exc


def _lock_file(path):
    path = Path(path).absolute()
    allowed_roots = (PROJECT_ROOT, Path(tempfile.gettempdir()).resolve(), Path("/private/tmp"))
    if not any(path.resolve().is_relative_to(root) for root in allowed_roots):
        raise ValueError("Lock must be in AnySolvers-v2 or a temporary test directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, "r+b")


def _mark_lock(handle):
    handle.seek(0)
    marker = handle.read(len(_LOCK_MARKER) + 1)
    if marker and marker != _LOCK_MARKER:
        raise ValueError("Refusing to reuse an unrelated lock file")
    if not marker:
        handle.seek(0)
        handle.write(_LOCK_MARKER)
        handle.flush()


def _acquire(handle, mode, deadline=None):
    while True:
        try:
            fcntl.flock(handle, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                raise _AdmissionClosed("run_wall_budget")
            time.sleep(.025)


@contextmanager
def _lock(path):
    # Preserve the existing exclusive global lock protocol for standalone calls.
    with _lock_file(path) as handle:
        _acquire(handle, fcntl.LOCK_EX)
        try:
            _mark_lock(handle)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class _EvaluationSlot:
    def __init__(self, path, capacity):
        self.path = Path(path).resolve()
        self.capacity = capacity
        self.pid = os.getpid()
        self.thread_id = threading.get_ident()
        self.active = True

    def check(self, path):
        if (not self.active or self.path != Path(path).resolve()
                or self.pid != os.getpid() or self.thread_id != threading.get_ident()):
            raise ValueError("Evaluation slot is not held by this executor")


@contextmanager
def evaluation_slot(path, capacity=MAX_PARALLEL_EVALUATIONS, *, deadline_monotonic=None):
    """Reserve one global dataset slot, respecting the original exclusive lock.

    The shared flock never bypasses an older standalone/exclusive holder. All
    configurations use the same three slot files, so their total cannot exceed
    three even across runs and processes. Smaller capacities use a fixed prefix.
    """
    if type(capacity) is not int or not 1 <= capacity <= MAX_PARALLEL_EVALUATIONS:
        raise ValueError("max_parallel_evaluations must be between one and three")
    path = Path(path)
    with _lock_file(path) as gate:
        _acquire(gate, fcntl.LOCK_SH, deadline_monotonic)
        try:
            # Marker content is constant; concurrent first writers are identical.
            _mark_lock(gate)
            handles = []
            held = None
            try:
                for index in range(capacity):
                    handles.append(_lock_file(path.with_name(path.name + '.slot-' + str(index))))
                while held is None:
                    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                        raise _AdmissionClosed("run_wall_budget")
                    for handle in handles:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        held = handle
                        _mark_lock(held)
                        break
                    if held is None:
                        time.sleep(.025)
                token = _EvaluationSlot(path, capacity)
                try:
                    yield token
                finally:
                    token.active = False
            finally:
                for handle in handles:
                    handle.close()
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)


def _slot_guard(path, token):
    if token is None:
        return _lock(path)
    if not isinstance(token, _EvaluationSlot):
        raise ValueError("Invalid evaluation slot")
    token.check(path)
    return nullcontext()


def _check_capacity(facts, slots, limits):
    if slots > 1 and (facts.get('cpus', 0) < slots
            or facts.get('memory_bytes', 0) < slots * limits['memory_mb'] * 1024**2):
        raise InfrastructureError("Insufficient Docker capacity for the configured evaluation slots")


def _configuration_checks(info, image, source, wrapper, limits, name):
    """Fail closed on the actual created container, before it can start."""
    try:
        host, config = info["HostConfig"], info["Config"]
        mounts = [(m["Type"], m["Destination"], str(Path(m["Source"]).resolve()), m["RW"])
                  for m in info["Mounts"]]
        expected = [("bind", "/solver.py", str(source.resolve()), False),
                    ("bind", "/entrypoint.py", str(wrapper.resolve()), False)]
        ulimits = {u["Name"]: (u["Soft"], u["Hard"]) for u in host["Ulimits"]}
        security = host["SecurityOpt"]
        checks = {
            "identity": info["Name"] == "/" + name and config["Labels"].get(LABEL) == name,
            "image": info["Image"] == image,
            "command": config["Entrypoint"] == ["python"] and config["Cmd"] == ["-I", "-B", "/entrypoint.py"],
            "nonroot": config["User"] == "65534:65534",
            "readonly_root": host["ReadonlyRootfs"] is True,
            "no_network": host["NetworkMode"] == "none",
            "one_cpu": host["NanoCpus"] == 1_000_000_000,
            "memory": host["Memory"] == host["MemorySwap"] == limits["memory_mb"] * 1024**2,
            "pids": host["PidsLimit"] == limits["pids"],
            "capabilities": host["CapDrop"] == ["ALL"] and not host.get("CapAdd"),
            "security": security in (["no-new-privileges"], ["no-new-privileges=true"]),
            "no_privileged_devices": host["Privileged"] is False and not host.get("Devices") and not host.get("DeviceRequests"),
            "private_namespaces": host.get("PidMode", "") == "" and host.get("IpcMode") == "private" and host.get("UTSMode", "") == "",
            "init": host["Init"] is True,
            "ulimits": ulimits == {"cpu": (limits["cpu_s"], limits["cpu_s"]), "nofile": (64, 64), "core": (0, 0)},
            "logs": host["LogConfig"]["Type"] == "none",
            "tmpfs": host["Tmpfs"] == {"/tmp": f"rw,noexec,nosuid,nodev,size={limits['tmp_mb']}m,mode=1777"},
            "only_two_readonly_files": sorted(mounts) == sorted(expected),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise InfrastructureError("Incomplete container configuration") from exc
    if not all(checks.values()):
        failed = ", ".join(key for key, passed in checks.items() if not passed)
        raise InfrastructureError("Sandbox configuration mismatch: " + failed)
    return checks


def _create_args(name, source, wrapper, image, limits):
    return ["create", "--pull=never", "--name", name, "--label", f"{LABEL}={name}",
            "-i", "--init", "--read-only", "--user", "65534:65534", "--network", "none",
            "--ipc", "private", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--cpus", "1", "--memory", f"{limits['memory_mb']}m",
            "--memory-swap", f"{limits['memory_mb']}m", "--pids-limit", str(limits["pids"]),
            "--ulimit", f"cpu={limits['cpu_s']}:{limits['cpu_s']}", "--ulimit", "nofile=64:64",
            "--ulimit", "core=0:0", "--log-driver", "none", "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits['tmp_mb']}m,mode=1777",
            "--mount", f"type=bind,source={source},target=/solver.py,readonly",
            "--mount", f"type=bind,source={wrapper},target=/entrypoint.py,readonly",
            "--entrypoint", "python", image, "-I", "-B", "/entrypoint.py"]


def _read_bounded(process, payload, deadline, limit):
    """Bound combined output and wall time, including a blocked input pipe."""
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    reason, offset, used = None, 0, 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream in ("stdin", "stdout", "stderr"):
                pipe = getattr(process, stream)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_WRITE if stream == "stdin" else selectors.EVENT_READ, stream)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "wall_timeout"
                    break
                for key, _ in selector.select(min(0.1, remaining)):
                    if key.data == "stdin":
                        try:
                            offset += os.write(key.fd, payload[offset:offset + 8192])
                        except BrokenPipeError:
                            offset = len(payload)
                        if offset == len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    streams[key.data].extend(chunk[:max(0, limit - used)])
                    used += len(chunk)
                    if used > limit:
                        reason = "output_limit"
                        break
                if reason:
                    break
        if not reason:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "wall_timeout"
        return {key: bytes(value) for key, value in streams.items()}, reason
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def _cleanup(name):
    owned = _command(["ps", "-a", "--filter", f"name=^/{name}$", "--filter",
                      f"label={LABEL}={name}", "--format", "{{.Names}}"])
    if name in owned.splitlines():
        _command(["rm", "-f", name])
    if _command(["ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"]).strip():
        raise InfrastructureError("Cleanup not confirmed; never delete an unlabelled container")


def _reject_constant(value):
    raise ValueError("Nonfinite JSON number: " + value)


class _EpisodeFailure(Exception):
    pass


def _finite_json(value):
    """Copy an observation/action before trusted state can mutate it."""
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return raw, json.loads(raw, parse_constant=_reject_constant)


def _observation_json(value):
    if type(value) is not dict:
        raise InfrastructureError("Episode observation must be a JSON object")
    return _finite_json(value)


def _trusted_call(deadline, callback, *args):
    """Bound trusted Python callbacks using the main thread's POSIX alarm.

    This is an execution budget, not isolation from trusted environment code.
    It cannot preempt a native extension that does not return control to Python.
    An existing alarm or a non-main thread is rejected rather than silently
    falling back to an unbounded callback. Callbacks must not change alarms.
    """
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise InfrastructureError("Trusted callbacks require the main thread and POSIX interval timers")
    if signal.getitimer(signal.ITIMER_REAL)[0] > 0:
        raise InfrastructureError("Trusted callback cannot replace an active host alarm")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _EpisodeFailure("wall_timeout")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise _EpisodeFailure("environment_timeout")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        value = callback(*args)
        if time.monotonic() >= deadline:
            raise _EpisodeFailure("environment_timeout")
        return value
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _EpisodeChannel:
    """Persistent bounded JSON-lines channel; no case or private state is sent."""

    def __init__(self, process, output_limit):
        self.process, self.limit = process, output_limit
        self.streams = {"stdout": bytearray(), "stderr": bytearray()}
        self.buffer = bytearray()
        self.used = 0
        self.selector = selectors.DefaultSelector()
        for stream in ("stdin", "stdout", "stderr"):
            os.set_blocking(getattr(process, stream).fileno(), False)
        for stream in ("stdout", "stderr"):
            self.selector.register(getattr(process, stream), selectors.EVENT_READ, stream)

    def _read(self, key):
        chunk = os.read(key.fd, 8192)
        if not chunk:
            self.selector.unregister(key.fileobj)
            return
        self.streams[key.data].extend(chunk[:max(0, self.limit - self.used)])
        self.used += len(chunk)
        if self.used > self.limit:
            raise _EpisodeFailure("output_limit")
        if key.data == "stdout":
            self.buffer.extend(chunk)

    def request(self, observation, deadline, timeout_reason):
        payload, delivered = _observation_json(observation)
        if len(payload) + 1 > self.limit:
            raise InfrastructureError("Observation exceeds the configured JSON message byte limit")
        if self.buffer:
            raise _EpisodeFailure("invalid_action: unsolicited output")
        payload += b"\n"
        offset = 0
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _EpisodeFailure(timeout_reason)
                for key, _ in self.selector.select(min(.05, remaining)):
                    if key.data == "stdin":
                        try:
                            offset += os.write(key.fd, payload[offset:offset + 8192])
                        except BrokenPipeError:
                            raise _EpisodeFailure("candidate_runtime_error")
                        if offset == len(payload):
                            self.selector.unregister(key.fileobj)
                    else:
                        self._read(key)
                if b"\n" in self.buffer:
                    line, _, extra = self.buffer.partition(b"\n")
                    self.buffer.clear()
                    if extra or offset < len(payload):
                        raise _EpisodeFailure("invalid_action: unsolicited output")
                    try:
                        message = json.loads(line, parse_constant=_reject_constant)
                        if not isinstance(message, dict) or set(message) != {"action"}:
                            raise ValueError("Each response must contain only action")
                        _, action = _finite_json(message["action"])
                    except (TypeError, ValueError, UnicodeError) as exc:
                        raise _EpisodeFailure("invalid_action: " + str(exc)[:500]) from exc
                    return delivered, action
                if self.process.poll() is not None and not any(
                        key.data == "stdout" for key in self.selector.get_map().values()):
                    raise _EpisodeFailure("candidate_runtime_error")
        finally:
            try:
                self.selector.unregister(self.process.stdin)
            except KeyError:
                pass

    def finish(self, deadline):
        self.process.stdin.close()
        while self.selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _EpisodeFailure("wall_timeout")
            for key, _ in self.selector.select(min(.05, remaining)):
                self._read(key)
        if self.buffer:
            raise _EpisodeFailure("invalid_action: unsolicited output")
        try:
            self.process.wait(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise _EpisodeFailure("wall_timeout") from exc
        if self.process.returncode:
            raise _EpisodeFailure("candidate_runtime_error")

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()


def execute_episode(source: str, case: dict, image: str, limits: dict, environment,
                    lock_path: Path, *, max_steps: int, action_wall_s: float,
                    deadline_monotonic=None, evaluation_slot_token=None) -> dict:
    """Evaluate solve(observation) against trusted host reset/step/score callbacks.

    The candidate is loaded once per episode in one persistent isolated Docker
    process. Only JSON observations cross into it, never the private case/state
    or environment code. Total output is bounded across all actions; each action
    and the full episode have separate wall deadlines. Every episode starts a
    new process. Environment callbacks run on the host main thread under a POSIX
    alarm; long non-interruptible native calls are unsupported. The environment
    implementation must itself define observation filtering, legal actions and
    the objective. Trace contains only observations/actions, never private state.
    """
    _check_image(image)
    if set(limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in limits.values()):
        raise ValueError("Expected complete, positive integer sandbox limits")
    if not isinstance(source, str) or not isinstance(case, dict):
        raise TypeError("Expected source string and private case dict")
    if type(max_steps) is not int or max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")
    if type(action_wall_s) not in (int, float) or not math.isfinite(action_wall_s) or not 0 < action_wall_s <= limits["wall_s"]:
        raise ValueError("action_wall_s must be finite, positive and within the episode wall budget")
    if not all(callable(getattr(environment, name, None)) for name in ("reset", "step", "score_episode")):
        raise TypeError("Environment requires reset, step and score_episode callbacks")
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise ValueError("Episode evaluation requires the host main thread and POSIX interval timers")
    _finite_json(case)
    queued = time.monotonic()
    with _slot_guard(lock_path, evaluation_slot_token):
        queue_wait = time.monotonic() - queued
        result = _execute_episode(source, case, image, dict(limits), environment,
                                  max_steps, action_wall_s, deadline_monotonic,
                                  evaluation_slot_token.capacity if evaluation_slot_token else 1)
        result["queue_wait_s"] = queue_wait
        return result


def _execute_episode(source, case, image, limits, environment, max_steps, action_wall_s,
                     admission_deadline, parallel_slots=1):
    started = time.monotonic()
    name = "anysolvers-v2-" + uuid.uuid4().hex
    result = {"valid": False, "score": None, "solution": None, "metrics": {},
              "wall_s": 0.0, "infrastructure_error": False, "cleanup_confirmed": False,
              "trace": [], "steps": 0}
    details = {"container": name, "image": image, "limits": limits,
               "protocol": "episode", "max_steps": max_steps, "action_wall_s": action_wall_s,
               "trusted_callback_deadline": "main_thread_posix_alarm"}
    transient, channel, process, created_attempted = None, None, None, False
    try:
        if admission_deadline is not None and time.monotonic() >= admission_deadline:
            raise _AdmissionClosed("run_wall_budget")
        details["preflight"] = preflight(image)
        _check_capacity(details["preflight"], parallel_slots, limits)
        if not Path("/private/tmp").is_dir():
            raise InfrastructureError("This protocol requires /private/tmp staging")
        transient = Path(tempfile.mkdtemp(prefix="anysolvers-v2-", dir="/private/tmp"))
        transient.chmod(0o755)
        code, wrapper = transient / "solver.py", transient / "entrypoint.py"
        for path, contents in ((code, source), (wrapper, _EPISODE_BRIDGE)):
            path.write_text(contents, encoding="utf-8")
            path.chmod(0o444)
        started = time.monotonic()
        if admission_deadline is not None and started >= admission_deadline:
            raise _AdmissionClosed("run_wall_budget")
        deadline = started + limits["wall_s"]
        created_attempted = True
        _command(_create_args(name, code, wrapper, image, limits), deadline - time.monotonic())
        inspected = _json_command(["inspect", name], deadline - time.monotonic())
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise InfrastructureError("Expected one inspected container")
        details["configuration_checks"] = _configuration_checks(inspected[0], image, code, wrapper, limits, name)
        result["isolation_verified"] = True
        reset = _trusted_call(deadline, environment.reset, _finite_json(case)[1])
        if not isinstance(reset, (tuple, list)) or len(reset) != 2:
            raise InfrastructureError("Environment reset must return (state, observation)")
        state, observation = reset
        process = subprocess.Popen(["docker", "start", "-ai", name], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        channel = _EpisodeChannel(process, limits["output_bytes"])
        for index in range(max_steps):
            action_deadline = min(deadline, time.monotonic() + action_wall_s)
            reason = "wall_timeout" if action_deadline == deadline else "action_timeout"
            delivered, action = channel.request(observation, action_deadline, reason)
            result["solution"] = action
            try:
                transition = _trusted_call(deadline, environment.step, state, _finite_json(action)[1])
            except (ValueError, TypeError, KeyError) as exc:
                raise _EpisodeFailure("invalid_action: " + str(exc)[:500]) from exc
            if not isinstance(transition, (tuple, list)) or len(transition) != 3 or type(transition[2]) is not bool:
                raise InfrastructureError("Environment step must return (state, observation, bool done)")
            state, observation, done = transition
            next_raw, next_observation = _observation_json(observation)
            if len(next_raw) + 1 > limits["output_bytes"]:
                raise InfrastructureError("Observation exceeds the configured JSON message byte limit")
            result["trace"].append({"step": index, "observation": delivered, "action": action,
                                    "next_observation": next_observation, "done": done})
            result["steps"] = len(result["trace"])
            if done:
                break
        else:
            raise _EpisodeFailure("max_steps_exceeded")
        channel.finish(deadline)
        state_info = _json_command(["inspect", "--format", "{{json .State}}", name],
                                   deadline - time.monotonic())
        if not isinstance(state_info, dict) or type(state_info.get("ExitCode")) is not int:
            raise InfrastructureError("Incomplete container exit state")
        details["state"] = state_info
        result["exit_code"] = state_info["ExitCode"]
        if state_info.get("OOMKilled"):
            raise _EpisodeFailure("memory_limit")
        if state_info["ExitCode"] or state_info.get("Running"):
            raise _EpisodeFailure("candidate_runtime_error")
        scored = _trusted_call(deadline, environment.score_episode, state,
                               _finite_json(result["trace"])[1], _finite_json(case)[1])
        score, metrics = scored["score"], scored.get("metrics", {})
        if scored.get("valid", True) is not True or type(score) not in (int, float) or not math.isfinite(score):
            raise InfrastructureError("Environment returned an invalid score")
        if not isinstance(metrics, dict):
            raise InfrastructureError("Environment metrics must be a JSON object")
        _finite_json(metrics)
        result.update(valid=True, score=score, metrics=metrics)
    except _AdmissionClosed as exc:
        result.update(error=str(exc), admission_closed=True)
    except _EpisodeFailure as exc:
        result["error"] = str(exc)
        if str(exc) == "environment_timeout":
            result["infrastructure_error"] = True
    except Exception as exc:
        # Candidate exceptions happen inside Docker; exceptions here are trusted
        # environment or infrastructure faults, not evidence of a bad algorithm.
        result.update(infrastructure_error=True, error=str(exc)[:1500])
    finally:
        result["wall_s"] = time.monotonic() - started
        if channel is not None:
            result["stdout_sha256"] = hashlib.sha256(channel.streams["stdout"]).hexdigest()
        attach_cleanup_error = None
        try:
            if channel is not None:
                channel.close()
            elif process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            attach_cleanup_error = str(exc)[:1500]
        try:
            if created_attempted:
                _cleanup(name)
            if transient is not None:
                shutil.rmtree(transient)
            result["cleanup_confirmed"] = attach_cleanup_error is None
            if attach_cleanup_error is not None:
                result.update(infrastructure_error=True, cleanup_error=attach_cleanup_error)
        except (InfrastructureError, OSError, subprocess.SubprocessError) as exc:
            result.update(infrastructure_error=True, cleanup_error=str(exc)[:1500])
            if transient is not None:
                details["temporary_directory"] = str(transient)
    if not result["valid"] or result["infrastructure_error"]:
        if channel is not None:
            details.update({key: bytes(value).decode(errors="replace") for key, value in channel.streams.items()})
        result["diagnostic"] = details
    return result


def execute(source: str, case: dict, image: str, limits: dict, scorer, lock_path: Path,
            *, deadline_monotonic=None, evaluation_slot_token=None) -> dict:
    """Run one case; scorer(solution, case) returns score and optional metrics.

    All calls must share the same v2-only lock_path. Only two ephemeral code files
    are staged in /private/tmp; cases use stdin and no evidence files are written.
    Scorers are trusted host Python code bounded by the case deadline on the
    main thread using POSIX timers; non-interruptible native callbacks are not
    supported. A scorer timeout is an infrastructure failure. Other scorer
    rejections make a solution invalid. queue_wait_s is separate from execution wall_s. An optional monotonic deadline
    stops admitting new containers after the queue; it does not kill active work.
    """
    _check_image(image)
    if set(limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in limits.values()):
        raise ValueError("Expected complete, positive integer sandbox limits")
    if not isinstance(source, str) or not isinstance(case, dict) or not callable(scorer):
        raise TypeError("Expected source string, case dict, and trusted scorer callable")
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise ValueError("Function evaluation requires the host main thread and POSIX interval timers")
    payload = json.dumps(case, allow_nan=False, separators=(",", ":")).encode()
    queued = time.monotonic()
    with _slot_guard(lock_path, evaluation_slot_token):
        queue_wait = time.monotonic() - queued
        result = _execute(source, case, image, dict(limits), scorer, payload, deadline_monotonic,
                          evaluation_slot_token.capacity if evaluation_slot_token else 1)
        result["queue_wait_s"] = queue_wait
        return result


def _execute(source, case, image, limits, scorer, payload, deadline=None, parallel_slots=1):
    started = time.monotonic()
    name = "anysolvers-v2-" + uuid.uuid4().hex
    result = {"valid": False, "score": None, "solution": None, "metrics": {},
              "wall_s": 0.0, "infrastructure_error": False, "cleanup_confirmed": False}
    details, streams, transient = {"container": name, "image": image, "limits": limits}, {}, None
    created_attempted = False
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise _AdmissionClosed("run_wall_budget")
        details["preflight"] = preflight(image)
        _check_capacity(details["preflight"], parallel_slots, limits)
        if not Path("/private/tmp").is_dir():
            raise InfrastructureError("This protocol requires /private/tmp staging")
        transient = Path(tempfile.mkdtemp(prefix="anysolvers-v2-", dir="/private/tmp"))
        transient.chmod(0o755)
        code, wrapper = transient / "solver.py", transient / "entrypoint.py"
        for path, contents in ((code, source), (wrapper, _BRIDGE)):
            path.write_text(contents, encoding="utf-8")
            path.chmod(0o444)
        started = time.monotonic()
        if deadline is not None and started >= deadline:
            raise _AdmissionClosed("run_wall_budget")
        created_attempted = True
        _command(_create_args(name, code, wrapper, image, limits), limits["wall_s"])
        inspected = _json_command(["inspect", name], limits["wall_s"] - (time.monotonic() - started))
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise InfrastructureError("Expected one inspected container")
        details["configuration_checks"] = _configuration_checks(inspected[0], image, code, wrapper, limits, name)
        result["isolation_verified"] = True
        process = subprocess.Popen(["docker", "start", "-ai", name], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        streams, reason = _read_bounded(process, payload, started + limits["wall_s"], limits["output_bytes"])
        result["wall_s"] = time.monotonic() - started
        state = _json_command(["inspect", "--format", "{{json .State}}", name])
        if not isinstance(state, dict) or type(state.get("ExitCode")) is not int:
            raise InfrastructureError("Incomplete container exit state")
        details["state"] = state
        result["exit_code"] = state["ExitCode"]
        result["stdout_sha256"] = hashlib.sha256(streams["stdout"]).hexdigest()
        if reason:
            result["error"] = reason
        elif state.get("OOMKilled"):
            result["error"] = "memory_limit"
        elif process.returncode or state["ExitCode"] or state.get("Running"):
            result["error"] = "candidate_runtime_error"
        else:
            try:
                output = json.loads(streams["stdout"], parse_constant=_reject_constant)
                if not isinstance(output, dict) or set(output) != {"solution"}:
                    raise ValueError("Output must contain only solution")
                json.dumps(output, allow_nan=False)
                scored = _trusted_call(started + limits["wall_s"], scorer, output["solution"], case)
                score, metrics = scored["score"], scored.get("metrics", {})
                if scored.get("valid", True) is not True or type(score) not in (int, float) or not math.isfinite(score):
                    raise ValueError("Scorer rejected solution or returned invalid score")
                if not isinstance(metrics, dict):
                    raise ValueError("Scorer metrics must be a JSON object")
                json.dumps(metrics, allow_nan=False)
                if 'trace' in scored:
                    _, trace = _finite_json(scored['trace'])
                    result['trace'] = trace
                result.update(valid=True, score=score, solution=output["solution"], metrics=metrics)
            except (_EpisodeFailure, InfrastructureError):
                raise
            except Exception as exc:
                result["error"] = "invalid_output: " + str(exc)[:500]
    except _EpisodeFailure as exc:
        result.update(error=str(exc), infrastructure_error=str(exc) == 'environment_timeout')
    except _AdmissionClosed as exc:
        result.update(error=str(exc), admission_closed=True)
    except (InfrastructureError, OSError, subprocess.SubprocessError) as exc:
        result.update(infrastructure_error=True, error=str(exc)[:1500])
    finally:
        result["wall_s"] = time.monotonic() - started
        try:
            if created_attempted:
                _cleanup(name)
            if transient is not None:
                shutil.rmtree(transient)
            result["cleanup_confirmed"] = True
        except (InfrastructureError, OSError) as exc:
            result.update(infrastructure_error=True, cleanup_error=str(exc)[:1500])
            if transient is not None:
                details["temporary_directory"] = str(transient)
    if not result["valid"] or result["infrastructure_error"]:
        details.update({key: value.decode(errors="replace") for key, value in streams.items()})
        result["diagnostic"] = details
    return result
