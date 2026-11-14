"""Budgeted, non-replaying measurements; local and SSH execution."""
from __future__ import annotations

import datetime as dt
import contextlib
import copy
import fcntl
import hashlib
import json
import itertools
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from tools import common
from modules.evaluation.queued_python import executor, retention, ssh
from tools.module_runtime import invoke

CASE_OVERHEAD_RESERVE_S = 1


class EvaluationNotStarted(ValueError):
    """Admission ended before this invocation registered a measurement."""
    evaluation_not_started = True


def resource_contract():
    return {'case_overhead_reserve_s': CASE_OVERHEAD_RESERVE_S,
            'max_parallel_evaluations': executor.MAX_PARALLEL_EVALUATIONS}


def preflight():
    ssh.preflight(ssh.target())


def _append(path, record):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _remaining(config):
    created = dt.datetime.fromisoformat(config["created_at"].replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    elapsed = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
    return config["budgets"]["run_wall_s"] - max(0, elapsed)


def _read_pinned(root, relative, expected):
    data = common.contained(root, relative).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("sealed file changed: " + relative)
    return data


def _inputs(run, config, state, candidate_id, dataset_id):
    dataset = state["deliveries"]["dataset"][dataset_id]
    manifest = dataset["manifest"]
    common.validate("dataset", manifest, config if 'modules' in config else None)
    if manifest["dataset_id"] != dataset_id or manifest["task_id"] != config["task"]["task_id"]:
        raise ValueError("dataset identity or task mismatch")
    relative = manifest["cases_file"]
    data = _read_pinned(common.delivery_root(run, dataset), relative, dataset["files"][relative])
    cases = [common.parse_json(line) for line in data.decode().splitlines() if line.strip()]
    if (not cases or (config["budgets"]["max_cases_per_dataset"] is not None
            and len(cases) > config["budgets"]["max_cases_per_dataset"])):
        raise ValueError("dataset exceeds case budget or is empty")
    task = common.benchmark_environment(run, config, state, dataset)
    if task.protocol['kind'] == 'episode' and task.protocol['action_wall_s'] > config['compute']['limits']['wall_s']:
        raise ValueError('episode action budget exceeds the configured case wall budget')
    seen = set()
    for case in cases:
        common.validate("case_row", case, config if 'modules' in config else None)
        if case["case_id"] in seen:
            raise ValueError("duplicate case_id")
        seen.add(case["case_id"])
        task.validate_input(copy.deepcopy(case["input"]))
    if candidate_id == "baseline":
        source = _read_pinned(task.baseline_path.parent, task.baseline_path.name, task.baseline_sha)
        candidate = {"baseline_sha": hashlib.sha256(source).hexdigest()}
    else:
        candidate = state["deliveries"]["candidate"][candidate_id]
        candidate_manifest = candidate["manifest"]
        common.validate("candidate", candidate_manifest, config if 'modules' in config else None)
        if candidate_manifest["candidate_id"] != candidate_id or candidate_manifest["task_id"] != config["task"]["task_id"]:
            raise ValueError("candidate identity or task mismatch")
        relative = candidate_manifest["source_file"]
        source = _read_pinned(common.delivery_root(run, candidate), relative, candidate["files"][relative])
    source_sha = hashlib.sha256(source).hexdigest()
    request = {"candidate_id": candidate_id, "candidate": candidate, "source_sha": source_sha,
               "dataset_id": dataset_id, "dataset": dataset,
               "compute": config["compute"], "execution": ssh.target(), "task": config["task"],
               "benchmark_signature": task.signature, "trace_retention": retention.policy(config)}
    return source.decode(), source_sha, manifest, cases, task, request


def _case_key(request):
    return common.digest({key:request[key] for key in ('source_sha','benchmark_signature','trace_retention')}
                         | {'limits':request['compute']['limits']})


def _final_gate(config, state, manifest, candidate_id, source_sha, probe):
    # Lightweight legacy/test configurations have no module bindings. Effective
    # run configurations dispatch through the pinned final module implementation.
    if "modules" not in config:
        from modules.final.frozen_holdout.policy import gate
        return gate(config, state, manifest, candidate_id, source_sha, probe)
    from tools import module_runtime
    return module_runtime.invoke(config, "final", "gate", config, state, manifest,
                                 candidate_id, source_sha, probe)


def _previous(run, state, owner=None):
    summaries = {}
    for evaluation_id, relative in state["evaluations"].items():
        path = common.contained(run, relative)
        summary = common.read_json(path)
        if summary["status"] not in ("completed", "failed"):
            if (summary["status"] == "running" and evaluation_id != owner
                    and summary.get("cleanup_confirmed") and not summary.get("infrastructure_error")
                    and evaluation_active(run, evaluation_id)):
                summaries[evaluation_id] = summary
                continue
            raise ValueError("incomplete evaluation requires manual reconciliation; execution is blocked")
        if summary["cleanup_confirmed"] is not True or summary.get("infrastructure_error"):
            raise ValueError("unknown cleanup requires manual reconciliation; execution is blocked")
        seal = state.get("evaluation_hashes", {}).get(evaluation_id)
        if not seal or common.sha(path) != seal["summary_sha"]:
            raise ValueError("unsealed or changed evaluation summary")
        journal_sha = common.sha(common.contained(run, summary["details_file"]))
        if journal_sha != summary["results_sha"] or journal_sha != seal["results_sha"]:
            raise ValueError("completed evaluation journal changed")
        summaries[evaluation_id] = summary
    return summaries


def _evaluation_lock_path(run, evaluation_id):
    common.identifier(evaluation_id)
    return common.job_runtime(run) / 'evaluations' / (evaluation_id + '.lock')


def evaluation_active(run, evaluation_id):
    """A running record is live only while its own process holds its lease."""
    path = _evaluation_lock_path(run, evaluation_id)
    if not path.exists():
        return False
    with path.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


@contextlib.contextmanager
def _request_lock(path, deadline=None, shared=False, wait=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        while True:
            try:
                fcntl.flock(stream, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise ValueError('a measurement is active; use the researcher evaluate tool to queue')
                if deadline is not None and time.monotonic() >= deadline:
                    raise EvaluationNotStarted('run_wall_s admission deadline reached while queued')
                time.sleep(.025)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _research_admission(run, config, state, session_id, evaluation_id,
                        candidate_id, dataset_id, probe):
    if session_id is None:
        return
    invoke(config, 'research', 'admit', config, state, session_id)
    records = invoke(config, 'research', 'audit', run, config, state)
    matching = [r for r in records.values() if r['kind'] == 'request'
                and r['session_id'] == session_id
                and r['payload']['evaluation_id'] == evaluation_id
                and r['payload']['candidate_id'] == candidate_id
                and r['payload']['dataset_id'] == dataset_id
                and r['payload']['probe'] == probe]
    if len(matching) != 1:
        raise ValueError('research evaluation requires its registered request')
    candidate = state['deliveries']['candidate'].get(candidate_id)
    dataset = state['deliveries']['dataset'].get(dataset_id)
    if (not candidate or candidate['manifest']['session_id'] != session_id
            or not dataset or dataset['manifest']['purpose'] not in ('anchor', 'challenge')):
        raise ValueError('research evaluation requires own candidate and development dataset')


def _evaluation_budget(config, count):
    value = config['compute'].get('evaluation_wall_s',
                                  (config['compute']['limits']['wall_s'] + CASE_OVERHEAD_RESERVE_S) * count)
    if type(value) is not int or value <= 0:
        raise ValueError('evaluation_wall_s must be a positive integer')
    return value


def _record_score(summary, scores, result):
    summary["completed_cases"] += 1
    for field in ("wall_s", "queue_wait_s"):
        summary[field] = (summary[field] + result[field]
                          if summary[field] is not None and result[field] is not None else None)
    summary["infrastructure_error"] |= result["infrastructure_error"]
    summary["cleanup_confirmed"] &= result["cleanup_confirmed"]
    accepted = result["valid"] and not result["infrastructure_error"] and result["cleanup_confirmed"]
    if accepted and type(result["score"]) in (int, float) and math.isfinite(result["score"]):
        scores.append(result["score"])
        summary["valid_cases"] += 1
    if result["infrastructure_error"] or not result["cleanup_confirmed"] or result.get("admission_closed"):
        summary.update(status="failed", error=result.get("error", "execution_failure"))


def _finish(summary, scores):
    if summary["status"] == "running":
        summary["status"] = "completed"
    summary["all_valid"] = (summary["status"] == "completed" and len(scores) == summary["total_cases"]
                            and not summary["infrastructure_error"] and summary["cleanup_confirmed"])
    if summary["all_valid"]:
        summary["score"] = math.fsum(score / len(scores) for score in scores)


class RemoteReceiptPending(RuntimeError):
    """The original remote job may still be running. Only fetch it on recovery."""


def _records(run, summary, tolerate_partial=False):
    """Follow prior verified case prefixes; each attempt stores only its new rows."""
    prefix = summary.get('prefix')
    if prefix:
        parent = common.read_json(common.contained(run, prefix['summary']))
        yield from itertools.islice(_records(run, parent, True), 2 * prefix['cases'])
    with common.contained(run, summary['details_file']).open('rb') as stream:
        for line in stream:
            if tolerate_partial and not line.endswith(b'\n'):
                break
            yield common.parse_json(line)


def _settled(run, summary, cases):
    records = iter(_records(run, summary, True))
    for case in cases:
        attempt = next(records, None)
        if attempt is None:
            break
        if (attempt['type'] != 'attempt' or attempt['case_id'] != case['case_id']
                or attempt['input_sha'] != common.digest(case['input'])
                or attempt['source_sha'] != summary['source_sha']):
            raise ValueError('case journal differs from registered evaluation')
        row = next(records, None)
        if row is None:
            break
        if row['type'] != 'result' or row['case_id'] != case['case_id']:
            raise ValueError('result differs from registered attempt')
        result = row['result']
        if result['infrastructure_error'] or not result['cleanup_confirmed'] or result.get('admission_closed'):
            break
        yield result


def retry(run_id, evaluation_id, remote_stopped=False):
    run, _, state = common.load_run(run_id)
    common.identifier(evaluation_id)
    if evaluation_id not in state['evaluations']:
        raise ValueError('unknown evaluation ID')
    original = common.read_json(common.contained(run, state['evaluations'][evaluation_id]))
    return evaluate(run_id, original['candidate_id'], original['dataset_id'], evaluation_id,
                    original['probe'], repair=True, remote_stopped=remote_stopped)


def _collect_remote(summary, journal, cases, wait_until=None, prefix_scores=(), *, job_dir):
    job = summary['remote_job']
    # Validate the entire returned journal before replacing the local copy.
    with tempfile.TemporaryFile(dir=job_dir) as received, tempfile.NamedTemporaryFile(dir=journal.parent, delete=False) as output:
        staging = Path(output.name)
        try:
            receipt = ssh.collect(job, received, wait_until=wait_until)
            scores = list(prefix_scores)
            final = dict(summary, status='running', completed_cases=0, valid_cases=0, cleanup_confirmed=True,
                         wall_s=0., queue_wait_s=0., infrastructure_error=False)
            if summary.get('prefix'):
                for key in ('completed_cases', 'valid_cases', 'wall_s', 'queue_wait_s'):
                    final[key] = summary[key]
            for index, line in enumerate(received, start=final['completed_cases']):
                result_line = received.readline()
                if receipt['error'] == 'worker_interrupted' and not result_line.endswith(b'\n'):
                    break
                attempt = common.parse_json(line)
                if index >= len(cases) or not result_line:
                    raise ValueError('unexpected SSH case count or incomplete case')
                result = common.parse_json(result_line)
                row = cases[index]
                if (attempt['type'] != 'attempt' or result['type'] != 'result'
                        or attempt['case_id'] != row['case_id'] or result['case_id'] != row['case_id']
                        or attempt['source_sha'] != summary['source_sha']
                        or attempt['input_sha'] != common.digest(row['input'])):
                    raise ValueError('SSH record differs from submitted case')
                _record_score(final, scores, result['result'])
                output.write(line)
                output.write(result_line)
            if receipt['error']:
                final.update(status='failed', error=receipt['error'], infrastructure_error=receipt['infrastructure_error'])
            elif final['completed_cases'] != len(cases):
                raise ValueError('incomplete SSH results')
            _finish(final, scores)
            final['remote_receipt'] = receipt
            final['finished_at'] = receipt['finished_at']
            final['evaluation_elapsed_s'] = receipt['elapsed_s']
            output.flush()
            os.fsync(output.fileno())
            staging.replace(journal)
            summary.update(final)
        finally:
            staging.unlink(missing_ok=True)


def _recover_remote(run, summary_file, cfg, pending, cases, wait_until=None):
    journal = common.contained(run, pending['details_file'])
    scores = [r['score'] for r in _settled(run, pending, cases[:pending.get('prefix', {}).get('cases', 0)])
              if r['valid']]
    _collect_remote(pending, journal, cases, wait_until, scores, job_dir=common.job_runtime(run))
    pending['results_sha'] = common.sha(journal)
    receipt = dict(config_sha=pending.get('config_sha', common.digest(cfg)), summary=pending)
    common.write_json(summary_file.parent / 'completion.json', dict(receipt=receipt, sha=common.digest(receipt)))


def _remote_evaluation(summary, directory, journal, task, source, run, dataset, config, cases,
                       run_deadline, evaluation_end):
    job = dict(id=uuid.uuid4().hex, key=summary['request_sha256'], destination=summary['execution'],
               worker=common.sha(ssh.WORKER))
    relative = dataset['manifest']['cases_file']
    spec = ssh.prepare(job, task, source, common.contained(common.delivery_root(run, dataset), relative),
                       dataset['files'][relative], summary['trace_retention'], config['compute']['limits'],
                       run_deadline, evaluation_end,
                       CASE_OVERHEAD_RESERVE_S)
    spec['skip_cases'] = summary['completed_cases']
    summary['remote_job'] = job
    common.write_json(directory / 'summary.json', summary)
    try:
        ssh.call(job, 'submit', job['id'], data=json.dumps(spec).encode())
    except (RuntimeError, OSError, TimeoutError, subprocess.SubprocessError):
        # A lost acknowledgement can still mean the job started. Query its original ID.
        pass
    try:
        scores = [r['score'] for r in _settled(run, summary, cases[:summary['completed_cases']]) if r['valid']]
        _collect_remote(summary, journal, cases, evaluation_end + 5, scores, job_dir=common.job_runtime(run))
    except Exception as exc:
        raise RemoteReceiptPending(
            f"SSH job {job['id']}: {exc}. Retrieve with tools/run.py reconcile-evaluation "
            f"{config['run_id']} --evaluation {summary['evaluation_id']}") from exc


def evaluate(run_id, candidate_id, dataset_id, evaluation_id, probe=False, task_dir=None, research_session=None, repair=False, remote_stopped=False):
    common.run_identifier(run_id)
    for value in (candidate_id, dataset_id, evaluation_id):
        common.identifier(value)
    run = common.run_path(run_id)
    queued_at = time.monotonic()
    with common.mutation(run, wait=research_session is not None):
        _, cfg, state = common.load_run(run_id)
        _research_admission(run, cfg, state, research_session, evaluation_id,
                            candidate_id, dataset_id, probe)
        if task_dir is not None and Path(task_dir).resolve() != common.contained(common.ROOT, cfg["task"]["path"]).parent:
            raise ValueError('task-local entry point does not match this run')
        _, source_sha, manifest, _, _, initial_request = _inputs(run, cfg, state, candidate_id, dataset_id)
        _final_gate(cfg, state, manifest, candidate_id, source_sha, probe)
        initial_sha = common.digest({**initial_request, 'probe': probe})
        admission_deadline = time.monotonic() + max(0, _remaining(cfg))
    # The old run-wide lock remains a shared compatibility gate. Independent
    # snapshots hold separate leases, while equal snapshots and IDs serialize.
    with contextlib.ExitStack() as leases:
        leases.enter_context(_request_lock(common.job_runtime(run) / '.measurement.lock',
                                           admission_deadline, shared=True, wait=research_session is not None))
        leases.enter_context(_request_lock(common.job_runtime(run) / 'snapshots' / (initial_sha + '.lock'),
                                           admission_deadline, wait=research_session is not None))
        leases.enter_context(_request_lock(_evaluation_lock_path(run, evaluation_id), admission_deadline,
                                           wait=research_session is not None))
        # On a lost SSH connection, fetch the original job, even if the caller's
        # environment now selects a different host. Never dispatch a second job.
        with common.mutation(run, wait=research_session is not None):
            _, config, state = common.load_run(run_id)
            relative = state['evaluations'].get(evaluation_id)
            existing = common.read_json(common.contained(run, relative)) if relative else {}
        stale = evaluation_id in state.get("stale_evaluations", [])
        if existing and not repair:
            # An explicit ID always identifies its original machine.
            request = {**initial_request, 'execution': existing.get('execution'), 'probe': probe}
            if stale:
                raise ValueError('score requires retry-evaluation')
            if common.digest(request) != existing['request_sha256']:
                same_intent = (existing['candidate_id'] == candidate_id and existing['dataset_id'] == dataset_id
                               and existing['probe'] == probe and existing['source_sha'] == source_sha
                               and ('execution' not in existing or existing.get('case_key') == _case_key(request)))
                if not same_intent:
                    raise ValueError('evaluation ID belongs to a different request')
            if evaluation_id in state.get('evaluation_hashes', {}):
                _previous(run, {**state, 'evaluations': {evaluation_id:relative}}, owner=evaluation_id)
                return {**existing, 'cached': True}
        if repair and not stale and existing.get('status') == 'completed':
            if evaluation_id in state.get('evaluation_hashes', {}):
                return {**existing, 'cached': True}
        if repair and not stale and relative and evaluation_id not in state.get('evaluation_hashes', {}):
            path = common.contained(run, relative)
            if (path.parent/'completion.json').is_file():
                saved = common.read_json(path.parent/'completion.json')['receipt']['summary']
                if not saved['infrastructure_error'] and saved['cleanup_confirmed']:
                    with common.mutation(run, wait=True):
                        _, cfg, current = common.load_run(run_id)
                        _seal_receipt(run, cfg, current, evaluation_id, path)
                    if saved['status'] == 'completed':
                        return {**saved, 'cached': True}
        if existing.get('remote_job') and existing['status'] == 'running' and not (repair and remote_stopped):
            _, _, _, cases, _, _ = _inputs(run, config, state, candidate_id, dataset_id)
            summary_file = common.contained(run, relative)
            if not (summary_file.parent / 'completion.json').exists():
                _recover_remote(run, summary_file, config, existing, cases,
                                time.monotonic() + existing['evaluation_wall_s'] + 5)
            with common.mutation(run, wait=True):
                _, config, state = common.load_run(run_id)
                _seal_receipt(run, config, state, evaluation_id, summary_file, allow_failed=repair)
                recovered = common.read_json(summary_file)
                if not repair or (not stale and recovered['status'] == 'completed'):
                    return {**recovered, 'cached': True}
        with common.mutation(run, wait=research_session is not None):
            run, config, state = common.load_run(run_id)
            _research_admission(run, config, state, research_session, evaluation_id,
                                candidate_id, dataset_id, probe)
            if task_dir is not None and Path(task_dir).resolve() != common.contained(common.ROOT, config["task"]["path"]).parent:
                raise ValueError("task-local entry point does not match this run")
            source, source_sha, manifest, cases, task, request = _inputs(run, config, state, candidate_id, dataset_id)
            _final_gate(config, state, manifest, candidate_id, source_sha, probe)
            request_sha = common.digest({**request, "probe": probe})
            if request_sha != initial_sha:
                raise ValueError('evaluation inputs changed while queued')
            prior_state = state
            if repair:
                # Other orphaned evaluations can be repaired independently.
                prior_state = {**state, 'evaluations': {}}
            previous = _previous(run, prior_state, owner=evaluation_id)
            for summary in previous.values():
                if summary["evaluation_id"] not in state.get("stale_evaluations", []) and summary["request_sha256"] == request_sha:
                    return {**summary, "cached": True}
            budgets = config["budgets"]
            requests = invoke(config, 'research', 'audit', run, config, state) if 'modules' in config else {}
            queued = [r['payload'] for k,r in requests.items() if r['kind']=='request'
                      and k+'-response' not in requests and r['payload']['evaluation_id'] not in state['evaluations']
                      and r['payload']['evaluation_id'] != evaluation_id]
            if (not repair and budgets["evaluation_calls"] is not None
                    and len(state["evaluations"]) + len(queued) >= budgets["evaluation_calls"]):
                raise ValueError("evaluation_calls budget exhausted")
            if probe and not repair and (sum(bool(common.read_json(common.contained(run, path))["probe"])
                             for path in state["evaluations"].values()) + sum(q['probe'] for q in queued)) >= budgets["probe_calls"]:
                raise ValueError("probe_calls budget exhausted")
            remaining = _remaining(config)
            if remaining <= 0:
                raise EvaluationNotStarted("run_wall_s budget exhausted; no new measurement admitted")
            run_deadline = time.monotonic() + remaining
            evaluation_budget = _evaluation_budget(config, len(cases))
            capacity = config['compute'].get('max_parallel_evaluations', 3)
            kind = "final_evaluation" if manifest["purpose"] == "final" else "evaluation"
            directory = common.contained(run, common.output_relative(run_id, kind, evaluation_id))
            scores = []
            settled = ()
            if repair:
                existing = common.read_json(common.contained(run, relative))
                # Transport and aggregate budget changes preserve per-case evidence.
                case_key = _case_key(request)
                same_cases = existing.get('case_key') == case_key
                if 'case_key' not in existing:
                    same_cases = common.digest({**request, 'execution':existing.get('execution'),
                                                'probe':probe}) == existing['request_sha256']
                if same_cases and not stale:
                    settled = _settled(run, existing, cases)
                old_file = common.contained(run, relative)
                history = state.setdefault('evaluation_history', {}).setdefault(evaluation_id, [])
                history.append(dict(summary=relative, summary_sha=common.sha(old_file),
                                    results_sha=common.sha(common.contained(run, existing['details_file']))))
                directory = directory / ('retry-' + uuid.uuid4().hex[:12])
                state.setdefault('evaluation_hashes', {}).pop(evaluation_id, None)
                if stale:
                    state['stale_evaluations'].remove(evaluation_id)
            if not repair and directory.is_dir():
                # No solver starts before registration. Adopt only an empty draft of this request.
                names = {p.name for p in directory.iterdir()}
                if not names <= {'results.jsonl', 'summary.json'}:
                    raise ValueError('unregistered evaluation contains execution evidence')
                if 'results.jsonl' in names and (directory/'results.jsonl').stat().st_size:
                    raise ValueError('unregistered evaluation has recorded attempts')
                if 'summary.json' in names:
                    draft = common.read_json(directory/'summary.json')
                    if draft['request_sha256'] != request_sha or draft.get('remote_job'):
                        raise ValueError('unregistered evaluation belongs to another request')
            directory.mkdir(parents=True, exist_ok=not repair)
            common.sync_directory(directory.parent)
            summary_file, journal = directory / "summary.json", directory / "results.jsonl"
            summary = {"evaluation_id": evaluation_id, "status": "running", "request_sha256": request_sha,
                       "candidate_id": candidate_id, "source_sha": source_sha, "dataset_id": dataset_id,
                       "dataset_purpose": manifest["purpose"], "probe": probe, "task_id": config["task"]["task_id"],
                       "trace_retention": retention.policy(config), "execution": request["execution"],
                       "total_cases": len(cases), "completed_cases": 0, "valid_cases": 0, "all_valid": False,
                       "score": None, "infrastructure_error": False, "cleanup_confirmed": True,
                       "wall_s": 0.0, "queue_wait_s": 0.0, "admission_queue_wait_s": time.monotonic() - queued_at,
                       "evaluation_wall_s": evaluation_budget, "evaluation_elapsed_s": 0.0,
                       "started_at": common.utc(), "finished_at": None,
                       "details_file": str(journal.relative_to(run))}
            summary['case_key'] = _case_key(request)
            summary['config_sha'] = common.digest(config)
            for result in settled:
                _record_score(summary, scores, result)
            if summary['completed_cases']:
                summary['prefix'] = {'summary':relative, 'cases':summary['completed_cases']}
            common.write_json(summary_file, summary)
            journal.touch(exist_ok=True)
            state["evaluations"][evaluation_id] = str(summary_file.relative_to(run))
            common.save_state(run, state)
            common.event(run, "evaluation_started", evaluation_id=evaluation_id, probe=probe)
        evaluation_started = None
        try:
            slot = leases.enter_context(executor.evaluation_slot(
                common.runtime_locks() / "evaluation.lock", capacity, deadline_monotonic=run_deadline))
            summary['admission_queue_wait_s'] = time.monotonic() - queued_at
            with common.mutation(run, wait=True):
                _, fresh, current = common.load_run(run_id)
                _research_admission(run, fresh, current, research_session, evaluation_id,
                                    candidate_id, dataset_id, probe)
                # Inspect every other live/finished evaluation after waiting. An
                # infrastructure fault that happened in the queue still blocks.
                others = {**current, 'evaluations': {key: value for key, value
                          in current['evaluations'].items() if key != evaluation_id}}
                if not repair:
                    _previous(run, others)
            evaluation_started = time.monotonic()
            deadline = min(run_deadline, evaluation_started + evaluation_budget)
            if request['execution']['mode'] == 'ssh':
                _remote_evaluation(summary, directory, journal, task, source, run,
                                   current['deliveries']['dataset'][dataset_id], config, cases,
                                   run_deadline, evaluation_started + evaluation_budget)
            else:
                for case in cases[summary["completed_cases"] :]:
                    now = time.monotonic()
                    if now >= run_deadline:
                        summary.update(status="failed", error='run_wall_budget')
                        break
                    # Never shrink a case's fixed limits to spend the last seconds.
                    if evaluation_started + evaluation_budget - now < (config['compute']['limits']['wall_s']
                                                                      + CASE_OVERHEAD_RESERVE_S):
                        summary.update(status="failed", error='evaluation_wall_budget')
                        break
                    _append(journal, {"type": "attempt", "case_id": case["case_id"], "at": common.utc(),
                                      "input_sha": common.digest(case["input"]), "source_sha": source_sha})
                    try:
                        if task.protocol['kind'] == 'episode':
                            result = executor.execute_episode(source, case["input"],
                                config["compute"]["limits"], task, common.runtime_locks() / "evaluation.lock",
                                max_steps=task.protocol['max_steps'], action_wall_s=task.protocol['action_wall_s'],
                                deadline_monotonic=deadline, evaluation_slot_token=slot, job_dir=common.job_runtime(run))
                        else:
                            result = executor.execute(source, case["input"],
                                config["compute"]["limits"], task.score, common.runtime_locks() / "evaluation.lock",
                                deadline_monotonic=deadline, evaluation_slot_token=slot, job_dir=common.job_runtime(run))
                    except Exception as exc:
                        # An unexpected helper failure has unknown cleanup state; never retry.
                        result = {"valid": False, "score": None, "solution": None, "metrics": {},
                                  "infrastructure_error": True, "cleanup_confirmed": False,
                                  "wall_s": None, "queue_wait_s": None, "error": str(exc)[:1000]}
                    result = retention.retain(result, retention.policy(config))
                    _append(journal, {"type": "result", "case_id": case["case_id"], "at": common.utc(), "result": result})
                    _record_score(summary, scores, result)
                    if summary["status"] == "failed":
                        break
                    common.write_json(summary_file, summary)
                if (evaluation_started is not None and time.monotonic() > evaluation_started + evaluation_budget
                        and summary['status'] == 'running'):
                    summary.update(status='failed', error='evaluation_wall_budget')
                _finish(summary, scores)
        except RemoteReceiptPending:
            raise
        except executor._AdmissionClosed as exc:
            summary.update(status="failed", error=str(exc), all_valid=False, score=None)
        except Exception as exc:
            summary.update(status="failed", infrastructure_error=True, all_valid=False,
                           score=None, error=str(exc)[:1000])
        if evaluation_started is not None:
            summary['evaluation_elapsed_s'] = time.monotonic() - evaluation_started
        with common.mutation(run, wait=True):
            _, fresh_config, state = common.load_run(run_id)
            if fresh_config != config:
                raise ValueError("configuration changed while measuring; reconcile before retry")
            summary["finished_at"] = common.utc()
            summary["results_sha"] = common.sha(journal)
            receipt = dict(config_sha=common.digest(config), summary=summary)
            common.write_json(directory / 'completion.json',
                              dict(receipt=receipt, sha=common.digest(receipt)))
            common.write_json(summary_file, summary)
            state.setdefault("evaluation_hashes", {})[evaluation_id] = {
                "summary_sha": common.sha(summary_file), "results_sha": summary["results_sha"]}
            common.save_state(run, state)
            common.event(run, "evaluation_finished", evaluation_id=evaluation_id, status=summary["status"])
        return summary


def reconcile(run_id, evaluation_id):
    """Publish an original terminal receipt after a lost state commit; never solve."""
    common.identifier(evaluation_id)
    run = common.run_path(run_id)
    # Hold the same lease as execution, not just a racy process-status check.
    with _request_lock(_evaluation_lock_path(run, evaluation_id)):
        with common.mutation(run):
            _, cfg, state = common.load_run(run_id)
            if evaluation_id not in state['evaluations']:
                raise ValueError('unknown evaluation; no original registered intent')
            summary_file = common.contained(run, state['evaluations'][evaluation_id])
            receipt_file = summary_file.parent / 'completion.json'
            if not receipt_file.is_file():
                pending = common.read_json(summary_file)
                if pending.get('remote_job') and pending['status'] == 'running':
                    dataset = state['deliveries']['dataset'][pending['dataset_id']]
                    relative = dataset['manifest']['cases_file']
                    data = _read_pinned(common.delivery_root(run, dataset), relative, dataset['files'][relative])
                    rows = [common.parse_json(line) for line in data.splitlines() if line.strip()]
                    _recover_remote(run, summary_file, cfg, pending, rows)
            return _seal_receipt(run, cfg, state, evaluation_id, summary_file)


def _seal_receipt(run, cfg, state, evaluation_id, summary_file, allow_failed=False):
    receipt_file = summary_file.parent / 'completion.json'
    if not receipt_file.is_file():
        raise ValueError('unknown execution: no terminal receipt; replay is forbidden')
    saved = common.read_json(receipt_file)
    receipt = saved['receipt']
    known_configs = [common.digest(cfg), *state.get('previous_configurations', [])]
    if saved['sha'] != common.digest(receipt) or receipt['config_sha'] not in known_configs:
        raise ValueError('completion receipt changed or belongs to another implementation')
    summary = receipt['summary']
    current = common.read_json(summary_file)
    for key in ('evaluation_id', 'request_sha256', 'candidate_id', 'source_sha',
                'dataset_id', 'dataset_purpose', 'probe', 'details_file', 'started_at'):
        if summary[key] != current[key]:
            raise ValueError('completion receipt differs from original evaluation intent')
    if (summary['evaluation_id'] != evaluation_id
            or summary['status'] not in ('completed', 'failed') or not summary['finished_at']
            or (not allow_failed and (summary['infrastructure_error'] or not summary['cleanup_confirmed']))):
        raise ValueError('unknown cleanup or infrastructure error requires manual reconciliation')
    journal = common.contained(run, summary['details_file'])
    if common.sha(journal) != summary['results_sha']:
        raise ValueError('completion receipt journal changed')
    records = list(_records(run, summary))
    if len(records) != 2 * summary['completed_cases']:
        raise ValueError('unmatched evaluation attempt; replay is forbidden')
    for attempt, result in zip(records[::2], records[1::2]):
        if (attempt['type'] != 'attempt' or result['type'] != 'result'
                or attempt['case_id'] != result['case_id']
                or (not allow_failed and (result['result']['infrastructure_error']
                                          or not result['result']['cleanup_confirmed']))):
            raise ValueError('unsettled evaluation attempt; replay is forbidden')
    cached = evaluation_id in state.get('evaluation_hashes', {})
    if cached and current != summary:
        raise ValueError('sealed evaluation differs from completion receipt')
    common.write_json(summary_file, summary)
    state.setdefault('evaluation_hashes', {})[evaluation_id] = dict(
        summary_sha=common.sha(summary_file), results_sha=summary['results_sha'])
    common.save_state(run, state)
    return dict(evaluation_id=evaluation_id, reconciled=True, cached=cached, execution_started=False)



def details(run_id, evaluation_id, case_id=None):
    common.identifier(evaluation_id)
    run, _, state = common.load_run(run_id)
    summary = common.read_json(common.contained(run, state["evaluations"][evaluation_id]))
    journal = common.contained(run, summary["details_file"])
    if summary.get("results_sha") and common.sha(journal) != summary["results_sha"]:
        raise ValueError("evaluation journal changed")
    records = list(_records(run, summary))
    if case_id is not None:
        records = [record for record in records if record["case_id"] == case_id]
        if not records:
            raise ValueError("unknown case ID")
    return {"summary": summary, "records": records,
            "requires_rescoring": evaluation_id in state.get('stale_evaluations', [])}
