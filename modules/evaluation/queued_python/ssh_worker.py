"""Remote end of the SSH transport. All operations use the user's Python and standard library."""
import datetime as dt
import fcntl
import hashlib
from importlib.machinery import SourceFileLoader
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback


CACHE = Path.home() / '.cache' / 'anysolvers'
BLOBS = CACHE / 'objects'
JOBS = CACHE / 'jobs'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def publish(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink()
            raise
    temporary.replace(path)


def load_module(name, path):
    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def materialize(root, files):
    for rel, digest in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BLOBS / digest, path)


def run_job(job):
    spec = json.loads((job / 'request.json').read_bytes())
    started = time.monotonic()
    receipt = dict(key=spec['key'], error=None, infrastructure_error=False)
    try:
        with tempfile.TemporaryDirectory(dir=job) as directory:
            root = Path(directory)
            materialize(root, spec['assets'])
            os.chdir(root)
            evaluator = load_module('benchmark', root / spec['evaluator'])
            runner = load_module('runner', BLOBS / spec['runner'])
            retain = load_module('trace_retention', BLOBS / spec['retention']).retain
            source = (BLOBS / spec['source']).read_text(encoding='utf-8')
            with (BLOBS / spec['cases']).open(encoding='utf-8') as rows, (job / 'records.jsonl').open('x') as records:
                def record(value):
                    records.write(json.dumps(value, allow_nan=False) + '\n')
                    records.flush()
                    os.fsync(records.fileno())

                for index, line in enumerate(row for row in rows if row.strip()):
                    if index < spec.get("skip_cases", 0):
                        continue
                    remaining = spec['evaluation_seconds'] - (time.monotonic() - started)
                    if time.monotonic() - started >= spec['run_seconds']:
                        receipt['error'] = 'run_wall_budget'
                        break
                    if remaining < spec['limits']['wall_s'] + spec['reserve_s']:
                        receipt['error'] = 'evaluation_wall_budget'
                        break
                    row = json.loads(line)
                    case = row['input']
                    canonical = json.dumps(case, sort_keys=True, separators=(',', ':'),
                                           allow_nan=False).encode()
                    record(dict(type='attempt', case_id=row['case_id'], input_sha=hashlib.sha256(canonical).hexdigest(),
                                source_sha=spec['source'], at=now()))
                    result = runner.run_case(source, case, spec['limits'], evaluator, spec['protocol'])
                    record(dict(type='result', case_id=row['case_id'], result=retain(result, spec['mode']), at=now()))
                    if result['infrastructure_error'] or not result['cleanup_confirmed']:
                        receipt.update(infrastructure_error=True, error=result.get('error', 'execution failed'))
                        break
            if time.monotonic() - started > spec['evaluation_seconds'] and not receipt['error']:
                receipt['error'] = 'evaluation_wall_budget'
    except BaseException as exc:
        receipt.update(error=str(exc)[:1500], infrastructure_error=True)
        traceback.print_exc()
    receipt['elapsed_s'] = time.monotonic() - started
    receipt['finished_at'] = now()
    publish(job / 'done.json', json.dumps(receipt).encode())


def main():
    if sys.argv[1:] and sys.argv[1] == 'work':
        run_job(JOBS / sys.argv[2])
        return
    op, *args = sys.argv[1:]
    BLOBS.mkdir(parents=True, exist_ok=True, mode=0o700)
    if op == 'missing':
        print(json.dumps([key for key in json.load(sys.stdin) if not (BLOBS / key).is_file()]))
    elif op == 'put':
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=BLOBS, delete=False) as stream:
            temp = Path(stream.name)
            try:
                for chunk in iter(lambda: sys.stdin.buffer.read(1024 * 1024), b''):
                    stream.write(chunk)
                    digest.update(chunk)
                if digest.hexdigest() != args[0]:
                    raise ValueError('asset changed during upload')
                stream.flush()
                temp.replace(BLOBS / args[0])
            finally:
                temp.unlink(missing_ok=True)
    elif op == 'submit':
        job = JOBS / args[0]
        spec = json.load(sys.stdin)
        JOBS.mkdir(exist_ok=True, mode=0o700)
        try:
            job.mkdir(mode=0o700)
        except FileExistsError:
            if json.loads((job / 'request.json').read_bytes()) != spec:
                raise ValueError('job ID belongs to another request')
            return
        with (job / 'worker.log').open('wb') as log, (job / 'lease').open('a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            publish(job / 'request.json', json.dumps(spec).encode())
            subprocess.Popen([sys.executable, '-B', str(BLOBS / spec['worker']), 'work', job.name],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                             pass_fds=(lease.fileno(),))
    elif op == 'fetch':
        job = JOBS / args[0]
        interrupted = None
        if not (job / 'done.json').is_file():
            status = 'unknown'
            if (job / 'lease').exists():
                with (job / 'lease').open('r') as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        status = 'running'
                    else:
                        if (job / 'request.json').exists() and not (job / 'done.json').exists():
                            spec = json.loads((job / 'request.json').read_bytes())
                            interrupted = dict(key=spec['key'], error='worker_interrupted',
                                               infrastructure_error=True, elapsed_s=None, finished_at=now())
            if not interrupted and not (job / 'done.json').exists():
                print(json.dumps({'status': status}))
                return
        print(json.dumps({'status': 'done', 'receipt': interrupted or json.loads((job / 'done.json').read_bytes())}), flush=True)
        journal = job / 'records.jsonl'
        if journal.exists():
            with journal.open('rb') as stream:
                shutil.copyfileobj(stream, sys.stdout.buffer)
    else:
        raise ValueError('unknown SSH operation')


if __name__ == '__main__':
    main()
