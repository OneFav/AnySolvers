"""collaborative research admission and immutable module-owned notes/findings/requests, not a model loop."""
import datetime as dt
import fcntl
import uuid

from tools import common as c
from tools.module_runtime import invoke, output_schema
from modules.research.notebook_collaboration import sessions as base
from modules.research.notebook_collaboration import journal


def records(state):
    return state.get('extensions', {}).get('research_exchange', {})


def audit(run, cfg, state):
    if (not cfg['modules']['selections']['capabilities'].get('researcher_authored') or
            not cfg['modules']['evaluation']['capabilities'].get('short_state_transactions')):
        raise ValueError('collaborative research requires compatible researcher-authored selections and session evaluation')
    result = {}
    for key, ref in records(state).items():
        path = c.contained(run, ref['path'])
        if c.sha(path) != ref['sha']:
            raise ValueError('sealed collaborative research record changed: ' + key)
        data = c.read_json(path)
        if data['run_id'] != cfg['run_id'] or data['record_id'] != key:
            raise ValueError('collaborative research record identity mismatch')
        result[key] = data
    for key, data in result.items():
        if data['kind'] == 'response':
            request = result.get(data['payload'].get('request_id'))
            if (not request or request['kind'] != 'request' or key != request['record_id']+'-response'
                    or data['session_id'] != request['session_id']):
                raise ValueError('response does not belong to its request/session')
        elif key.endswith('-response'):
            raise ValueError('reserved response ID used by a non-response record')
    for session in state['sessions']:
        verify_start(run, state, session)
        expected = {}
        for key, request in result.items():
            if request['kind'] == 'request' and request['session_id'] == session['session_id']:
                response = result.get(key + '-response')
                expected[key] = {'started_at': request['at'],
                                 'finished_at': response['at'] if response else None}
        if session.get('evaluation_waits', {}) != expected:
            raise ValueError('evaluation timing differs from sealed request/response records')
    journal.audit(run, cfg, state)
    if state.get('extensions', {}).get('selection_reports'):
        invoke(cfg, 'selections', 'report_audit', run, cfg, state)
    return result


def active(state, sid):
    sessions = [s for s in state['sessions'] if s['session_id'] == sid]
    if len(sessions) != 1 or sessions[0]['status'] != 'active' or state.get('selection'):
        raise ValueError('a unique active research session is required')
    return sessions[0]


def _admit(cfg, state, sid, now=None):
    session = active(state, sid)
    now = now or c.utc()
    timing = base.research_time(cfg, session, now)
    if not timing['activated']:
        raise ValueError('research session must be activated after native spawn')
    if timing['pending_preflight_ids']:
        raise ValueError('pending evaluation preflight requires reconciliation')
    if ((dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(cfg['created_at'])).total_seconds()
            >= cfg['budgets']['run_wall_s'] or timing['exhausted']):
        raise ValueError('research admission deadline reached')
    return session


def admit(cfg, state, session_id):
    """Evaluation calls the bound research method's admission policy."""
    audit(c.run_path(cfg['run_id']), cfg, state)
    return _admit(cfg, state, session_id)


def begin_preflight(run, cfg, state, sid):
    """Open a tool-owned wait before environment checking, without reserving a call."""
    now = c.utc()
    session = _admit(cfg, state, sid, now)
    if any(wait['finished_at'] is None for wait in session['evaluation_waits'].values()):
        raise ValueError('resolve the existing request before admitting another')
    token = uuid.uuid4().hex
    session['preflight_waits'][token] = {'started_at': now, 'finished_at': None}
    c.save_state(run, state)
    return token


def finish_preflight(run_id, sid, token):
    with c.mutation(c.run_path(run_id), wait=True):
        run, cfg, state = c.load_run(run_id)
        session = active(state, sid)
        wait = session['preflight_waits'][token]
        if wait['finished_at'] is None:
            wait['finished_at'] = c.utc()
            base.research_time(cfg, session, wait['finished_at'])
            c.save_state(run, state)


def _put(run, cfg, state, kind, rid, sid, payload):
    c.identifier(rid)
    if kind != 'response' and rid.endswith('-response'):
        raise ValueError('response suffix is reserved for trusted responses')
    if rid in records(state):
        raise ValueError('collaborative research record ID exists; never overwrite it')
    path = c.contained(run, 'research/exchange/' + kind + '/' + rid + '.json')
    if path.exists():
        raise ValueError('unregistered collaborative research file requires reconciliation')
    at = c.utc()
    if kind == 'request':
        session = _admit(cfg, state, sid, at)
        session['evaluation_waits'][rid] = {'started_at': at, 'finished_at': None}
    elif kind == 'response':
        session = active(state, sid)
        wait = session['evaluation_waits'][payload['request_id']]
        if wait['finished_at'] is not None:
            raise ValueError('evaluation wait already closed')
        wait['finished_at'] = at
        base.research_time(cfg, session, at)
    data = dict(schema_version=1, run_id=cfg['run_id'], record_id=rid, kind=kind,
                session_id=sid, at=at, payload=payload)
    journal.prepare(run, cfg, data)
    c.write_json(path, data)
    state.setdefault('extensions', {}).setdefault('research_exchange', {})[rid] = {
        'path': str(path.relative_to(run)), 'sha': c.sha(path), 'kind': kind}
    c.save_state(run, state)
    c.event(run, 'exchange-' + kind, record_id=rid, session_id=sid, sha=c.sha(path))
    return data


def starting_content(state, session):
    if session.get('idea_id'):
        found = [idea for ref in state['deliveries']['ideas'].values()
                 for idea in ref['manifest']['ideas'] if idea['idea_id'] == session['idea_id']]
        if len(found) != 1:
            raise ValueError('round starting idea source is missing or ambiguous')
        return 'initial', session['idea_id'], found[0]
    return 'iteration', session['iteration_id'], state['deliveries']['iteration'][session['iteration_id']]['manifest']


def seal_start(run, state, session):
    kind, source_id, content = starting_content(state, session)
    snapshot = dict(schema_version=1, session_id=session['session_id'], actor=session['actor'],
                    generation=session['generation'], kind=kind, idea_id=source_id,
                    content=content, content_sha=c.digest(content))
    relative = 'research/ideas/' + session['session_id'] + '.json'
    path = c.contained(run, relative)
    if path.exists() and c.read_json(path) != snapshot:
        raise ValueError('unregistered round starting idea differs; reconciliation required')
    c.write_json(path, snapshot)
    session['starting_idea'] = dict(path=relative, sha=c.sha(path), **snapshot)


def verify_start(run, state, session):
    start = session.get('starting_idea')
    if not start:
        raise ValueError('round starting idea is missing')
    kind, source_id, content = starting_content(state, session)
    expected = dict(schema_version=1, session_id=session['session_id'], actor=session['actor'],
                    generation=session['generation'], kind=kind, idea_id=source_id,
                    content=content, content_sha=c.digest(content))
    path = c.contained(run, start['path'])
    if (start['path'] != 'research/ideas/' + session['session_id'] + '.json'
            or c.sha(path) != start['sha'] or c.read_json(path) != expected
            or {k: v for k, v in start.items() if k not in ('path', 'sha')} != expected):
        raise ValueError('round starting idea binding changed')


def reconcile(run, cfg, state):
    """Coordinator-only metadata recovery. Never starts an evaluation or native agent."""
    recovered = journal.reconcile(run, cfg, state)
    audit(run, cfg, state)
    return dict(recovered_records=recovered, execution_started=False)


def _development(run, cfg, state, eid):
    s = invoke(cfg, 'selections', 'evidence', run, state, eid)
    seal = state.get('evaluation_hashes', {}).get(eid)
    if not seal or c.sha(c.contained(run, state['evaluations'][eid])) != seal['summary_sha']:
        raise ValueError('unsealed evaluation')
    return {k: s[k] for k in ('evaluation_id', 'candidate_id', 'source_sha', 'dataset_id',
                              'dataset_purpose', 'status', 'all_valid', 'score', 'probe',
                              'valid_cases', 'total_cases', 'infrastructure_error', 'cleanup_confirmed')}


def _candidate(state, cid):
    if cid not in state['deliveries']['candidate']:
        raise ValueError('unknown candidate')
    rec = state['deliveries']['candidate'][cid]
    return dict(candidate_id=cid, source_file=rec['manifest']['source_file'],
                source_sha=rec['files'][rec['manifest']['source_file']])


def _notebook(all_records, sid):
    found = [x for x in all_records.values() if x['kind'] == 'notebook' and x['session_id'] == sid]
    return found[-1] if found else None


def begin_session(run, cfg, state, actor, role, idea_id=None, iteration_id=None, now=None):
    all_records = audit(run, cfg, state)
    previous = [s for s in state['sessions'] if s['actor'] == actor]
    note = None
    if previous:
        note = _notebook(all_records, previous[-1]['session_id'])
        if note is None:
            raise ValueError('fresh-context continuation requires a previous notebook')
        if note['payload']['next_iteration_id'] != iteration_id:
            raise ValueError('continuation must use the researcher notebook iteration')
    if previous:
        invoke(cfg, 'selections', 'require_reports', run, cfg, state, previous[-1]['session_id'])
    session = base.prepare_session(run, cfg, state, actor, role, idea_id, iteration_id, now)
    session['resume_notebook'] = None if note is None else records(state)[note['record_id']]
    session['context_policy'] = 'fresh-native-session-with-notebook'
    seal_start(run, state, session)
    base.commit_session(run, state, session)
    c.event(run, 'exchange-session-inputs', session_id=session['session_id'], notebook=session['resume_notebook'])
    return session


def activate_session(run, cfg, state, actor, native_id, now=None):
    audit(run, cfg, state)
    return base.activate_session(run, cfg, state, actor, native_id, now)


def end_session(run, cfg, state, actor, status, native_id=None, now=None):
    all_records = audit(run, cfg, state)
    candidates = [s for s in state['sessions'] if s['actor'] == actor and s['status'] == 'active']
    if len(candidates) != 1:
        raise ValueError('no unique active session')
    sid = candidates[0]['session_id']
    if status == 'completed' and _notebook(all_records, sid) is None:
        raise ValueError('completed collaborative research sessions must checkpoint a notebook')
    pending = [k for k, v in all_records.items() if v['kind'] == 'request' and v['session_id'] == sid
               and k + '-response' not in all_records]
    if pending:
        raise ValueError('pending requests require reconciliation: ' + ','.join(pending))
    if native_id and any(s['session_id'] != sid and s.get('native_id') == native_id for s in state['sessions']):
        raise ValueError('collaborative research continuation must use a fresh native session')
    return base.end_session(run, cfg, state, actor, status, native_id, now)


def validate_delivery(run, cfg, state, data, add, entry):
    all_records = audit(run, cfg, state)
    session = _admit(cfg, state, data['session_id'])
    if data['task_id'] != cfg['task']['task_id'] or data['candidate_id'] == 'baseline':
        raise ValueError('task mismatch or reserved candidate ID')
    parent = data['parent_id']
    if parent:
        rec = state['deliveries']['candidate'].get(parent)
        if not rec:
            raise ValueError('unknown parent candidate')
        owner = next(s for s in state['sessions'] if s['session_id'] == rec['manifest']['session_id'])
        if owner['actor'] != session['actor'] and not any(
            r['kind'] == 'reuse' and r['session_id'] == session['session_id']
            and r['payload']['candidate']['candidate_id'] == parent for r in all_records.values()):
            raise ValueError('cross-actor parent requires a published finding and reuse record')
    if add(data['source_file']).stat().st_size > 65536:
        raise ValueError('source too large')


def _draft(run, cfg, kind, relative, sid):
    path = c.contained(run, relative)
    if not path.is_relative_to(run / 'research/inbox') or path.stat().st_size > 32768:
        raise ValueError('small request drafts must live under research/inbox')
    data = c.read_json(path)
    c.validate_schema(output_schema(cfg['modules']['research'], kind), data)
    if data['session_id'] != sid:
        raise ValueError('draft author differs from active session')
    return data


def exchange(run_id, operation, session_id=None, relative=None, request_id=None):
    if operation == 'process':
        return process(run_id, request_id)
    run = c.run_path(run_id)
    with c.mutation(run, wait=True):
        run, cfg, state = c.load_run(run_id)
        all_records = audit(run, cfg, state)
        if operation == 'audit':
            pending = [k for k, r in all_records.items() if r['kind'] == 'request' and k+'-response' not in all_records]
            return dict(ok=True, records=len(all_records), pending_requests=pending)
        session = active(state, session_id)
        if operation == 'context':
            # Private notes stay with their author. Public findings are same-batch only.
            evidence = invoke(cfg, 'selections', 'context', run, cfg, state, session['actor'])
            notes = [r for r in all_records.values() if r['kind'] == 'notebook'
                     and r['payload']['actor'] == session['actor']]
            return dict(session=session, latest_notebook=notes[-1] if notes else None,
                        finding_ids=[r['record_id'] for r in all_records.values() if r['kind']=='finding'],
                        development=evidence, remaining_evaluations=remaining(cfg, state, all_records),
                        research_time=base.research_time(cfg, session))
        data = _draft(run, cfg, operation, relative, session_id)
        rid = data['id']
        if rid in all_records:
            record = all_records[rid]
            kind = 'finding' if operation == 'share' else operation
            original = record['payload'].get('author_claim', record['payload'])
            if record['kind'] != kind or record['session_id'] != session_id or any(original.get(k) != v for k,v in data.items()):
                raise ValueError('record ID belongs to a different request')
            return record
        from modules.research.notebook_collaboration.client import visible_evidence
        if operation == 'notebook':
            for eid in data['evidence_ids']:
                visible_evidence(run, cfg, state, session_id, eid)
            if data['candidate_id']:
                cid = data['candidate_id']
                _candidate(state, cid)
                owner = state['deliveries']['candidate'][cid]['manifest']['session_id']
                if next(s for s in state['sessions'] if s['session_id'] == owner)['actor'] != session['actor']:
                    raise ValueError('notebook candidate must belong to its actor')
            nxt = data['next_iteration_id']
            if nxt and (nxt not in state['deliveries']['iteration'] or
                        state['deliveries']['iteration'][nxt]['manifest']['author_session'] != session_id):
                raise ValueError('next iteration must be authored by this researcher session')
            return _put(run, cfg, state, 'notebook', rid, session_id, {**data, 'actor':session['actor']})
        if operation == 'share':
            metrics = [visible_evidence(run, cfg, state, session_id, e) for e in data['evidence_ids']]
            if data['type'] == 'result' and not metrics:
                raise ValueError('result findings require trusted development evidence')
            candidate = _candidate(state, data['candidate_id']) if data['candidate_id'] else None
            if candidate and not any(s['candidate_id'] == candidate['candidate_id'] for s in metrics):
                raise ValueError('shared source must have linked development evidence')
            for parent in data['parent_findings']:
                if parent not in all_records or all_records[parent]['kind'] != 'finding':
                    raise ValueError('unknown parent finding')
            return _put(run, cfg, state, 'finding', rid, session_id,
                        dict(author_claim=data, trusted_evidence=metrics, candidate=candidate))
        if operation == 'reuse':
            _admit(cfg, state, session_id)
            finding = all_records.get(data['finding_id'])
            if not finding or finding['kind'] != 'finding' or not finding['payload']['candidate']:
                raise ValueError('reuse needs a same-run finding with a source snapshot')
            candidate = _candidate(state, finding['payload']['candidate']['candidate_id'])
            if candidate != finding['payload']['candidate']:
                raise ValueError('published source binding changed')
            return _put(run, cfg, state, 'reuse', rid, session_id, {**data, 'candidate':candidate})
        if operation == 'request':
            return reserve(run, cfg, state, session_id, data)
        raise ValueError('unknown collaborative research operation')


def remaining(cfg, state, all_records):
    if cfg['budgets']['evaluation_calls'] is None:
        return None
    reserved = sum(r['kind']=='request' and k+'-response' not in all_records
                   and r['payload']['evaluation_id'] not in state['evaluations']
                   for k,r in all_records.items())
    return cfg['budgets']['evaluation_calls']-len(state['evaluations'])-reserved


def reserve(run, cfg, state, sid, data):
    """Called under the short metadata lock. Queued requests reserve budget atomically."""
    c.validate_schema(output_schema(cfg['modules']['research'], 'request'), data)
    _admit(cfg, state, sid)
    all_records = audit(run, cfg, state)
    cid = data['candidate_id']; _candidate(state, cid)
    if data['session_id'] != sid or state['deliveries']['candidate'][cid]['manifest']['session_id'] != sid:
        raise ValueError('requests require a candidate from the requesting session')
    dataset = state['deliveries']['dataset'].get(data['dataset_id'])
    if not dataset or dataset['manifest']['purpose'] not in ('anchor','challenge'):
        raise ValueError('research requests permit development datasets only')
    if any(r['kind']=='request' and r['session_id']==sid and k+'-response' not in all_records for k,r in all_records.items()):
        raise ValueError('resolve the existing request before admitting another')
    if len(data['id']) > 54:
        raise ValueError('request ID must leave room for response suffix')
    if data['evaluation_id'] in state['evaluations']:
        raise ValueError('new requests require an unused evaluation ID')
    if any(r['kind']=='request' and r['payload']['evaluation_id']==data['evaluation_id'] for r in all_records.values()):
        raise ValueError('evaluation ID already reserved by a request')
    left = remaining(cfg, state, all_records)
    if left is not None and left <= 0:
        raise ValueError('evaluation_calls budget exhausted or reserved')
    if data['probe']:
        pending = sum(r['kind']=='request' and r['payload']['probe'] and k+'-response' not in all_records
                      and r['payload']['evaluation_id'] not in state['evaluations'] for k,r in all_records.items())
        used = sum(bool(c.read_json(c.contained(run,p))['probe']) for p in state['evaluations'].values())
        if pending+used >= cfg['budgets']['probe_calls']:
            raise ValueError('probe_calls budget exhausted or reserved')
    return _put(run,cfg,state,'request',data['id'],sid,data)


def process(run_id, request_id, session_id=None):
    """The researcher calls this in its own tool process; duplicate calls cannot replay."""
    c.identifier(request_id)
    run = c.run_path(run_id)
    locks = run/'research/.requests'
    locks.mkdir(exist_ok=True)
    with (locks/(request_id+'.lock')).open('a+') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        return _process(run_id, request_id, session_id)


def _process(run_id, request_id, session_id):
    run = c.run_path(run_id)
    with c.mutation(run, wait=True):
        run, cfg, state = c.load_run(run_id)
        all_records = audit(run, cfg, state)
        request = all_records.get(request_id)
        if not request or request['kind'] != 'request':
            raise ValueError('unknown evaluation request')
        if session_id is not None and request['session_id'] != session_id:
            raise ValueError('request author differs from caller session')
        if request_id + '-response' in all_records:
            return all_records[request_id+'-response']
        data = request['payload']
        if data['evaluation_id'] in state.get('evaluation_hashes', {}):
            # A process may stop after sealing the result but before recording its
            # response. Recover the existing evidence even after admission closes.
            summary = _development(run, cfg, state, data['evaluation_id'])
            if (summary['candidate_id'] != data['candidate_id'] or summary['dataset_id'] != data['dataset_id']
                    or summary['probe'] != data['probe']):
                raise ValueError('sealed evaluation differs from its pending request')
            payload = dict(request_id=request_id, evaluation=summary, cached=True,
                           summary_file=state['evaluations'][data['evaluation_id']])
            return _put(run, cfg, state, 'response', request_id+'-response', request['session_id'], payload)
        if ((dt.datetime.fromisoformat(c.utc()) - dt.datetime.fromisoformat(cfg['created_at'])).total_seconds()
                >= cfg['budgets']['run_wall_s'] and _unstarted(run, cfg, state, data['evaluation_id'])):
            return _not_started(run, cfg, state, request, 'run_wall_s admission deadline reached')
        _admit(cfg, state, request['session_id'])
    # Only fixed trusted code computes scores. Caller is the researcher, not a relay.
    try:
        result = invoke(cfg, 'evaluation', 'evaluate', run_id, data['candidate_id'], data['dataset_id'],
                        data['evaluation_id'], data['probe'], research_session=request['session_id'])
    except Exception as exc:
        # A trusted explicit denial is different from an interrupted/unknown attempt.
        if getattr(exc, 'evaluation_not_started', False) is not True:
            raise
        with c.mutation(run, wait=True):
            run, cfg, state = c.load_run(run_id)
            all_records = audit(run, cfg, state)
            if not _unstarted(run, cfg, state, data['evaluation_id']):
                raise
            return _not_started(run, cfg, state, all_records[request_id], str(exc))
    with c.mutation(run, wait=True):
        run, cfg, state = c.load_run(run_id)
        all_records = audit(run, cfg, state)
        if request_id + '-response' in all_records:
            return all_records[request_id+'-response']
        payload = dict(request_id=request_id, evaluation=_development(run, cfg, state, result['evaluation_id']),
                       cached=bool(result.get('cached')), summary_file=state['evaluations'][result['evaluation_id']])
        return _put(run, cfg, state, 'response', request_id+'-response', request['session_id'], payload)


def _unstarted(run, cfg, state, evaluation_id):
    directory = c.contained(run, c.output_relative(cfg['run_id'], 'evaluation', evaluation_id))
    return evaluation_id not in state['evaluations'] and not directory.exists()


def _not_started(run, cfg, state, request, error):
    rid = request['record_id']
    return _put(run, cfg, state, 'response', rid+'-response', request['session_id'],
                dict(request_id=rid, evaluation=None, status='not_started',
                     error=error[:1000], cached=False, summary_file=None))
