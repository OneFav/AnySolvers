"""Submit a sealed dataset job and fetch its receipt through ordinary OpenSSH."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from tools import common


def target():
    host = os.environ.get('ANYSOLVERS_SSH_HOST', '').strip()
    if not host:
        return {'mode': 'local'}
    if host.startswith('-') or any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError('ANYSOLVERS_SSH_HOST must be an SSH host or alias')
    return {'mode': 'ssh', 'host': host, 'python': os.getenv('ANYSOLVERS_SSH_PYTHON') or 'python3'}


def command(destination, args, *, data=None, stdin=None, stdout=None, timeout=30):
    process = subprocess.run(
        ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '--', destination['host'],
         shlex.join([destination['python'], '-B', *args])],
        input=data, stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout)
    if process.returncode:
        raise RuntimeError('SSH: ' + (process.stderr.decode(errors='replace')[-1500:] or str(process.returncode)))
    return process.stdout


def preflight(destination):
    if destination['mode'] == 'ssh':
        command(destination, ['-c', 'import sys; print(sys.version_info.major)'])


WORKER = Path(__file__).with_name('ssh_worker.py')
# The first request can install the small transport helper on an empty host.
_BOOT = """from pathlib import Path
import hashlib, os, sys, tempfile
root = Path.home() / '.cache/anysolvers/objects'
root.mkdir(mode=0o700, parents=True, exist_ok=True)
key = sys.argv[1]
if not (root/key).is_file():
    data = sys.stdin.buffer.read()
    if hashlib.sha256(data).hexdigest() != key:
        raise ValueError('worker digest mismatch')
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as f:
        f.write(data)
    os.replace(f.name, root/key)
"""
_CALL = """import runpy,sys
from pathlib import Path
path = Path.home()/'.cache/anysolvers/objects'/sys.argv.pop(1)
runpy.run_path(str(path), run_name='__main__')
"""


def call(job, operation, *args, **kwargs):
    return command(job['destination'], ['-c', _CALL, job['worker'], operation, *args], **kwargs)


def prepare(job, task, source, cases_file, cases_sha, mode, limits, run_end, evaluation_end, reserve):
    directory = WORKER.parent
    blobs = {}
    evaluator = task.benchmark['evaluator_file']
    assets = {name: task.file_hashes[name] for name in [evaluator, *task.benchmark['support_files']]}
    for name, digest in assets.items():
        blobs[digest] = (task.evaluator_path if name == evaluator and hasattr(task, 'evaluator_path')
                         else common.contained(task.root, name))
    spec = dict(key=job['key'], worker=job['worker'], assets=assets, evaluator=evaluator,
                cases=cases_sha, source=hashlib.sha256(source.encode()).hexdigest(),
                limits=limits, protocol=task.protocol, mode=mode, reserve_s=reserve)
    blobs[cases_sha] = cases_file
    blobs[spec['source']] = source.encode()
    for name in ('runner', 'retention'):
        file = directory / (name + '.py')
        spec[name] = common.sha(file)
        blobs[spec[name]] = file
    command(job['destination'], ['-c', _BOOT, job['worker']], data=WORKER.read_bytes())
    missing = json.loads(call(job, 'missing', data=json.dumps(list(blobs)).encode()))
    for key in missing:
        blob = blobs[key]
        if isinstance(blob, bytes):
            call(job, 'put', key, data=blob, timeout=max(1, evaluation_end - time.monotonic()))
        else:
            with blob.open('rb') as stream:
                call(job, 'put', key, stdin=stream, timeout=max(1, evaluation_end - time.monotonic()))
    spec.update(run_seconds=run_end - time.monotonic(), evaluation_seconds=evaluation_end - time.monotonic())
    return spec


def collect(job, stream, *, wait_until=None):
    """Read only. A lost connection or client restart never submits another job."""
    while True:
        stream.seek(0)
        stream.truncate()
        timeout = 30 if wait_until is None else max(30, wait_until - time.monotonic() + 10)
        call(job, 'fetch', job['id'], stdout=stream, timeout=timeout)
        stream.seek(0)
        status = json.loads(stream.readline())
        if status['status'] == 'done':
            receipt = status['receipt']
            if receipt['key'] != job['key']:
                raise ValueError('SSH receipt does not match the submitted job')
            return receipt
        if status['status'] != 'running' or wait_until is None or time.monotonic() >= wait_until:
            raise ValueError('SSH job ' + job['id'] + ': ' + status['status'])
        time.sleep(min(1, max(0, wait_until - time.monotonic())))
