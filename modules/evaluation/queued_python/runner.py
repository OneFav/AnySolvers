"""Run one candidate in a Python subprocess on the selected machine."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time

DEFAULT_LIMITS = {"wall_s": 10, "cpu_s": 2, "output_bytes": 65536}
_BRIDGE = '''import contextlib, json, runpy, sys
import resource
resource.setrlimit(resource.RLIMIT_CPU, (int(sys.argv[2]), int(sys.argv[2])))
case = json.load(sys.stdin)
with contextlib.redirect_stdout(sys.stderr):
    solve = runpy.run_path(sys.argv[1], run_name='candidate')['solve']
    solution = solve(case)
print(json.dumps({'solution': solution}, allow_nan=False, separators=(',', ':')))
'''
_EPISODE_BRIDGE = '''import contextlib, json, runpy, sys
import resource
resource.setrlimit(resource.RLIMIT_CPU, (int(sys.argv[2]), int(sys.argv[2])))
with contextlib.redirect_stdout(sys.stderr):
    solve = runpy.run_path(sys.argv[1], run_name='candidate')['solve']
for line in sys.stdin:
    observation = json.loads(line)
    with contextlib.redirect_stdout(sys.stderr):
        action = solve(observation)
    print(json.dumps({'action': action}, allow_nan=False, separators=(',', ':')), flush=True)
'''


class InfrastructureError(RuntimeError):
    """The evaluator could not complete the case reliably."""


def _read_bounded(process, payload, deadline, limit):
    """Bound combined output and wall time, including a blocked input pipe."""
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    reason, offset, used = None, 0, 0
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


@contextmanager
def _candidate(source, bridge, cpu_s, job_dir):
    with tempfile.TemporaryDirectory(prefix="anysolvers-case-", dir=job_dir) as directory:
        path = Path(directory) / "solver.py"
        path.write_text(source, encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", bridge, str(path), str(cpu_s)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        try:
            yield process
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def _validate(limits):
    if set(limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in limits.values()):
        raise ValueError("Expected positive wall_s, cpu_s and output_bytes")


def _score(scored):
    score, metrics = scored["score"], scored.get("metrics", {})
    if scored.get("valid", True) is not True or type(score) not in (int, float) or not math.isfinite(score):
        raise ValueError("Scorer rejected solution or returned invalid score")
    if not isinstance(metrics, dict):
        raise ValueError("Scorer metrics must be a JSON object")
    _finite_json(metrics)
    return score, metrics


def _function(process, case, scorer, deadline, limit, result, streams):
    payload = json.dumps(case, allow_nan=False).encode()
    output, reason = _read_bounded(process, payload, deadline, limit)
    streams.update(output)
    if reason or process.returncode:
        raise _EpisodeFailure(reason or "candidate_runtime_error")
    try:
        message = json.loads(output["stdout"], parse_constant=_reject_constant)
        if not isinstance(message, dict) or set(message) != {"solution"}:
            raise ValueError("Output must contain only solution")
        solution = _finite_json(message["solution"])[1]
        scored = _trusted_call(deadline, scorer, solution, case)
        score, metrics = _score(scored)
        result.update(valid=True, solution=solution, score=score, metrics=metrics)
        if "trace" in scored:
            result["trace"] = _finite_json(scored["trace"])[1]
    except (_EpisodeFailure, InfrastructureError):
        raise
    except Exception as exc:
        raise _EpisodeFailure("invalid_output: " + str(exc)[:500]) from exc


def _episode(process, case, environment, protocol, deadline, limit, result, streams):
    max_steps, action_wall_s = protocol["max_steps"], protocol["action_wall_s"]
    channel = _EpisodeChannel(process, limit)
    streams.update(channel.streams)
    result.update(trace=[], steps=0)
    try:
        reset = _trusted_call(deadline, environment.reset, _finite_json(case)[1])
        if not isinstance(reset, (tuple, list)) or len(reset) != 2:
            raise InfrastructureError("Environment reset must return (state, observation)")
        state, observation = reset
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
            if len(next_raw) + 1 > limit:
                raise InfrastructureError("Observation exceeds the configured JSON message byte limit")
            result["trace"].append({"step": index, "observation": delivered, "action": action,
                                    "next_observation": next_observation, "done": done})
            result["steps"] = len(result["trace"])
            if done:
                break
        else:
            raise _EpisodeFailure("max_steps_exceeded")
        channel.finish(deadline)
        scored = _trusted_call(deadline, environment.score_episode, state,
                               _finite_json(result["trace"])[1], _finite_json(case)[1])
        score, metrics = _score(scored)
        result.update(valid=True, score=score, metrics=metrics)
    finally:
        channel.close()


def run_case(source, case, limits, environment, protocol, admission_deadline=None, *, job_dir=None):
    _validate(limits)
    if protocol["kind"] == "episode":
        if type(protocol["max_steps"]) is not int or protocol["max_steps"] < 1:
            raise ValueError("max_steps must be a positive integer")
        if not 0 < protocol["action_wall_s"] <= limits["wall_s"]:
            raise ValueError("action_wall_s must be within the case budget")
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("Evaluation requires the main thread for scorer deadlines")
    started = time.monotonic()
    result = dict(valid=False, score=None, solution=None, metrics={}, infrastructure_error=False,
                  cleanup_confirmed=False, wall_s=0.0, queue_wait_s=0.0)
    if admission_deadline is not None and started >= admission_deadline:
        result.update(error="run_wall_budget", admission_closed=True, cleanup_confirmed=True)
        return result
    deadline = started + limits["wall_s"]
    streams = {}
    episode = protocol["kind"] == "episode"
    try:
        with _candidate(source, _EPISODE_BRIDGE if episode else _BRIDGE, limits["cpu_s"], job_dir) as process:
            try:
                if episode:
                    _episode(process, case, environment, protocol, deadline, limits["output_bytes"], result, streams)
                else:
                    _function(process, case, environment.score, deadline, limits["output_bytes"], result, streams)
            except _EpisodeFailure as exc:
                result.update(error=str(exc), infrastructure_error=str(exc) == "environment_timeout")
            except Exception as exc:
                result.update(infrastructure_error=True, error=str(exc)[:1500], valid=False, score=None)
        result["cleanup_confirmed"] = True
        result["exit_code"] = process.returncode
    except Exception as exc:
        result.update(infrastructure_error=True, error=str(exc)[:1500], valid=False, score=None)
    result["wall_s"] = time.monotonic() - started
    if "stdout" in streams:
        result["stdout_sha256"] = hashlib.sha256(streams["stdout"]).hexdigest()
    if not result["valid"]:
        result["diagnostic"] = {key: value.decode(errors="replace") for key, value in streams.items()}
    return result
