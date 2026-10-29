"""Session-scoped researcher tools. No model calls, no score-writing by agents."""
import fcntl

from tools import common as c
from tools.module_runtime import invoke
from modules.research.notebook_collaboration import runtime as r


def session(state, sid):
    found = [s for s in state['sessions'] if s['session_id'] == sid]
    if len(found) != 1:
        raise ValueError('unknown researcher session')
    return found[0]


def visible_evidence(run, cfg, state, sid, eid):
    owner = session(state, sid)
    summary = r._development(run, cfg, state, eid)  # excludes final, verifies sealed metrics
    if summary['candidate_id'] == 'baseline':
        return summary
    candidate = state['deliveries']['candidate'][summary['candidate_id']]['manifest']
    if session(state, candidate['session_id'])['actor'] == owner['actor']:
        return summary
    for record in r.audit(run, cfg, state).values():
        if record['kind'] == 'finding' and any(
                e['evaluation_id'] == eid for e in record['payload']['trusted_evidence']):
            return summary
    raise ValueError('private evaluation is not published to this researcher')


def _deliver(run, cfg, state, sid, kind, relative):
    from tools.run import deliver
    # A final hypothesis/iteration is part of closing a session after admission ends.
    if kind == 'iteration':
        r.active(state, sid)
    else:
        r._admit(cfg, state, sid)
    data = c.read_json(c.contained(run, relative))
    author = 'session_id' if kind == 'candidate' else 'author_session'
    if data.get(author) != sid:
        raise ValueError('delivery author differs from caller session')
    slot = 'research' if kind == 'candidate' else 'selections'
    if kind == 'iteration':
        for eid in data['evidence_ids']:
            visible_evidence(run, cfg, state, sid, eid)
        own = {s['session_id'] for s in state['sessions'] if s['actor'] == session(state, sid)['actor']}
        parent = state['deliveries']['candidate'].get(data['parent_candidate'])
        if parent and parent['manifest']['session_id'] not in own:
            published = {v['payload']['candidate']['candidate_id'] for v in r.audit(run,cfg,state).values()
                         if v['kind']=='finding' and v['payload']['candidate']}
            if data['parent_candidate'] not in published:
                raise ValueError('iteration parent is private to another researcher')
        for iid in data['history_iteration_ids']:
            if state['deliveries']['iteration'][iid]['manifest']['author_session'] not in own:
                raise ValueError('private iteration history belongs to another researcher')
    key = data[kind + '_id']
    existing = state['deliveries'][kind].get(key)
    if existing:
        if existing['manifest_path'] != relative or existing['manifest'] != data:
            raise ValueError('delivery ID already belongs to a different artifact')
        return {'kind': kind, 'id': key, 'cached': True}
    return deliver(run, cfg, state, kind, relative, slot)


def direct(run_id, operation, session_id=None, relative=None, request_id=None,
           dataset_id=None, evaluation_id=None, probe=False, case_id=None, details=False):
    arguments = (run_id, operation, session_id, relative, request_id, dataset_id,
                 evaluation_id, probe, case_id, details)
    if operation != 'evaluate' or not request_id:
        return _direct(*arguments)
    c.identifier(request_id)
    locks = c.run_path(run_id) / 'research/.requests'
    locks.mkdir(exist_ok=True)
    # Cover preflight as well as registration, so duplicate callers share one wait.
    with (locks / (request_id + '.client.lock')).open('a+') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        return _direct(*arguments)


def _direct(run_id, operation, session_id=None, relative=None, request_id=None,
            dataset_id=None, evaluation_id=None, probe=False, case_id=None, details=False):
    run = c.run_path(run_id)
    if operation == 'audit':
        return r.exchange(run_id, 'audit')
    if operation == 'evaluate':
        # A missing Docker socket permission is a caller-environment problem, not
        # an attempted algorithm case. Only its trusted wait clock is recorded.
        with c.mutation(run, wait=True):
            _, cfg, state = c.load_run(run_id)
            records = r.audit(run, cfg, state)
            needs_preflight = request_id not in records
            if needs_preflight:
                r._admit(cfg, state, session_id)
                if not relative or not request_id or not dataset_id:
                    raise ValueError('evaluate requires a candidate manifest, request ID and dataset')
                c.identifier(request_id)
                dataset = state['deliveries']['dataset'].get(dataset_id)
                if not dataset or dataset['manifest']['purpose'] not in ('anchor','challenge'):
                    raise ValueError('research requests permit development datasets only')
                image = cfg['compute']['image']
                preflight_token = r.begin_preflight(run, cfg, state, session_id)
        if needs_preflight:
            try:
                invoke(cfg, 'evaluation', 'preflight', image)
            finally:
                r.finish_preflight(run_id, session_id, preflight_token)
    # Registration and state updates are short transactions; never hold during solving.
    with c.mutation(run, wait=True):
        run, cfg, state = c.load_run(run_id)
        current = session(state, session_id)
        records = r.audit(run, cfg, state)
        if operation == 'evaluate':
            if not relative or not request_id or not dataset_id:
                raise ValueError('evaluate requires a candidate manifest, request ID and dataset')
            c.identifier(request_id)
            eid = evaluation_id or request_id + '-eval'
            c.identifier(eid)
            data = c.read_json(c.contained(run, relative))
            payload = dict(id=request_id, session_id=session_id,
                candidate_id=data.get('candidate_id'), dataset_id=dataset_id,
                evaluation_id=eid, probe=probe)
            existing = records.get(request_id)
            if existing and (existing['kind'] != 'request' or existing['payload'] != payload):
                raise ValueError('request ID belongs to a different request')
            if existing:
                # load_run has already reverified all sealed candidate files.
                sealed = state['deliveries']['candidate'].get(payload['candidate_id'])
                if not sealed or sealed['manifest_path'] != relative or sealed['manifest'] != data:
                    raise ValueError('request candidate manifest changed')
                response = records.get(request_id + '-response')
                if response:
                    return response
            else:
                dataset = state['deliveries']['dataset'].get(dataset_id)
                if not dataset or dataset['manifest']['purpose'] not in ('anchor', 'challenge'):
                    raise ValueError('research requests permit development datasets only')
                _deliver(run, cfg, state, session_id, 'candidate', relative)
                r.reserve(run, cfg, state, session_id, payload)
        elif operation == 'iteration':
            return _deliver(run, cfg, state, session_id, 'iteration', relative)
        elif operation == 'result':
            if request_id:
                request = records.get(request_id)
                if not request or request['kind'] != 'request':
                    raise ValueError('unknown request')
                if session(state, request['session_id'])['actor'] != current['actor']:
                    raise ValueError('private request belongs to another researcher')
                response = records.get(request_id + '-response')
                if response:
                    if response['payload']['evaluation'] is None:
                        return dict(request_id=request_id, status=response['payload']['status'],
                                    error=response['payload']['error'], response_registered=True)
                    evaluation_id = response['payload']['evaluation']['evaluation_id']
                else:
                    eid = request['payload']['evaluation_id']
                    path = state['evaluations'].get(eid)
                    status = c.read_json(c.contained(run, path))['status'] if path else 'queued'
                    return dict(request_id=request_id, status=status, response_registered=False)
            if not evaluation_id:
                raise ValueError('result requires request ID or evaluation ID')
            summary = visible_evidence(run, cfg, state, session_id, evaluation_id)
            value = dict(status=summary['status'], summary=summary)
            if details or case_id:
                value['records'] = invoke(cfg, 'evaluation', 'details', run_id, evaluation_id, case_id)['records']
            return value
        elif operation == 'findings':
            findings = [v for v in records.values() if v['kind'] == 'finding']
            if request_id:
                findings = [v for v in findings if v['record_id'] == request_id]
                if not findings:
                    raise ValueError('unknown published finding')
                return findings[0]
            # An index first; full claims and evidence only on explicit read.
            return {'findings': [dict(id=v['record_id'], session_id=v['session_id'],
                type=v['payload']['author_claim']['type'],
                claim=v['payload']['author_claim']['claim'][:240],
                evidence_ids=[e['evaluation_id'] for e in v['payload']['trusted_evidence']],
                candidate_id=(v['payload']['candidate'] or {}).get('candidate_id')) for v in findings]}
        elif operation not in ('context', 'checkpoint', 'share', 'reuse'):
            raise ValueError('unknown researcher operation')
    if operation == 'evaluate':
        return r.process(run_id, request_id, session_id=session_id)
    return r.exchange(run_id, 'notebook' if operation == 'checkpoint' else operation,
                 session_id=session_id, relative=relative, request_id=request_id)
