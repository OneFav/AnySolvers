"""Reserve project-wide evaluation slots for local and remote jobs."""
from contextlib import contextmanager, nullcontext
import fcntl
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace

from tools import common
from modules.evaluation.queued_python.runner import DEFAULT_LIMITS, run_case

MAX_PARALLEL_EVALUATIONS = 3
PROJECT_ROOT = common.ROOT
_LOCK_MARKER = b"anysolvers evaluation lock\n"


class _AdmissionClosed(RuntimeError):
    pass


def _lock_file(path):
    path = Path(path).absolute()
    allowed_roots = (common.ROOT, Path(tempfile.gettempdir()).resolve())
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



def execute(source, case, limits, scorer, lock_path, *, deadline_monotonic=None, evaluation_slot_token=None, job_dir=None):
    queued = time.monotonic()
    with _slot_guard(lock_path, evaluation_slot_token):
        wait = time.monotonic() - queued
        result = run_case(source, case, limits, SimpleNamespace(score=scorer),
                          {"kind": "function"}, deadline_monotonic, job_dir=job_dir)
        result["queue_wait_s"] = wait
        return result


def execute_episode(source, case, limits, environment, lock_path, *, max_steps, action_wall_s,
                    deadline_monotonic=None, evaluation_slot_token=None, job_dir=None):
    queued = time.monotonic()
    with _slot_guard(lock_path, evaluation_slot_token):
        wait = time.monotonic() - queued
        protocol = {"kind": "episode", "max_steps": max_steps, "action_wall_s": action_wall_s}
        result = run_case(source, case, limits, environment, protocol, deadline_monotonic, job_dir=job_dir)
        result["queue_wait_s"] = wait
        return result
