"""Selection-authored method prose with tool-owned, sealed development results."""
from tools import common as c
from tools.module_runtime import output_schema
from modules.selections.evidence_iteration import evidence as base


def _refs(state):
    return state.get('extensions', {}).get('selection_reports', {})


def _session(state, sid):
    matches = [s for s in state['sessions'] if s['session_id'] == sid]
    if len(matches) != 1 or matches[0]['status'] not in ('completed', 'failed'):
        raise ValueError('method report requires a settled researcher session')
    return matches[0]


def _selected(run, state, sid):
    """The last sealed checkpoint chooses the round outcome, never a score sort."""
    session = _session(state, sid)
    notes = []
    for rid, ref in state.get('extensions', {}).get('research_exchange', {}).items():
        if ref['kind'] != 'notebook':
            continue
        path = c.contained(run, ref['path'])
        if c.sha(path) != ref['sha']:
            raise ValueError('sealed checkpoint changed')
        note = c.read_json(path)
        if note['session_id'] == sid:
            if note['record_id'] != rid or note['payload']['actor'] != session['actor']:
                raise ValueError('checkpoint identity differs from report session')
            notes.append((note, dict(record_id=rid, path=ref['path'], sha=ref['sha'])))
    if not notes:
        if session['status'] == 'completed':
            raise ValueError('completed session needs a sealed final checkpoint')
        return None, None, None
    note, checkpoint = notes[-1]
    cid = note['payload']['candidate_id']
    if cid is None:
        return checkpoint, None, None
    rec = state['deliveries']['candidate'].get(cid)
    if rec is None:
        raise ValueError('checkpoint references an unknown final candidate')
    owners = [s for s in state['sessions'] if s['session_id'] == rec['manifest']['session_id']]
    if (len(owners) != 1 or owners[0]['actor'] != session['actor']
            or owners[0]['generation'] > session['generation']):
        raise ValueError('final candidate must belong to this actor in this or an earlier round')
    return checkpoint, cid, rec


def _facts(run, cfg, state, sid):
    session = _session(state, sid)
    checkpoint, cid, rec = _selected(run, state, sid)
    evaluations = []
    for eid in sorted(state['evaluations']):
        summary = base._summary(run, state, eid)
        # Terminal results must never enter or change research reports.
        if not base._development(state, summary) or cid is None or summary['candidate_id'] != cid:
            continue
        summary = base._research_evidence(run, state, eid)
        if not summary['cleanup_confirmed']:
            raise ValueError('evaluation cleanup must be reconciled before sealing a method report')
        keys = ('evaluation_id', 'dataset_id', 'dataset_purpose', 'status', 'probe',
                'score', 'all_valid', 'valid_cases', 'total_cases', 'completed_cases',
                'infrastructure_error', 'cleanup_confirmed', 'error')
        measured = (summary['status'] == 'completed' and summary['all_valid']
                    and not summary['infrastructure_error'] and summary['cleanup_confirmed'])
        evaluations.append({
            **base._pick(summary, keys), 'result_kind': 'measured' if measured else 'diagnostic',
            'summary_path': state['evaluations'][eid],
            'evidence_hashes': state['evaluation_hashes'][eid]})
    candidate = None
    if rec is not None:
        source = rec['manifest']['source_file']
        if c.sha(c.contained(run, source)) != rec['files'][source]:
            raise ValueError('method report candidate source changed')
        candidate = dict(candidate_id=cid, source_file=source, source_sha=rec['files'][source],
                         source_session_id=rec['manifest']['session_id'], evaluations=evaluations,
                         evaluation_status='evaluated' if evaluations else 'not_evaluated')
    return dict(schema_version=2, session_id=sid, actor=session['actor'],
                generation=session['generation'], starting_idea=base._pick(session['starting_idea'], ('path','sha','idea_id')),
                checkpoint=checkpoint, outcome='sealed_method' if cid else 'no_sealed_method',
                objective=cfg['task']['objective'], candidate=candidate)


def report_audit(run, cfg, state):
    for sid, ref in _refs(state).items():
        path = c.contained(run, ref['path'])
        if c.sha(path) != ref['sha']:
            raise ValueError('sealed method report changed')
        data = c.read_json(path)
        c.validate_schema(output_schema(cfg['modules']['selections'], 'method_report'), data['prose'])
        if data['facts'] != _facts(run, cfg, state, sid):
            raise ValueError('method report evidence or session binding changed')
        _complete(run, state, sid, data['prose'])
    return dict(reports=len(_refs(state)))


def pending_reports(run, cfg, state, actor=None):
    report_audit(run, cfg, state)
    return [dict(session_id=s['session_id'], actor=s['actor'], generation=s['generation'],
                 candidate_id=_selected(run, state, s['session_id'])[1])
            for s in state['sessions'] if s['status'] in ('completed', 'failed')
            and (actor is None or s['actor'] == actor) and s['session_id'] not in _refs(state)]


def report_context(run, cfg, state, target_session):
    base._open_research(state)
    report_audit(run, cfg, state)
    facts = _facts(run, cfg, state, target_session)
    candidate = facts['candidate']
    return dict(facts=facts, source=None if candidate is None else dict(candidate_id=candidate['candidate_id'],
                source=c.contained(run, candidate['source_file']).read_text()),
                report_schema=output_schema(cfg['modules']['selections'], 'method_report'),
                instructions='Report ONLY the final candidate named in the last sealed session checkpoint. '
                'Do not select the latest submission or highest score, and do not list intermediate attempts. '
                'Read actual source, including branches, tie-breaking and stopping conditions; comments and '
                'researcher claims may differ from the implementation. Write a standalone method description using conventional terms. '
                'Explain inputs, concrete steps and outputs; do not invent names or repeat research jargon, '
                'development history or unverified notebook claims. Do not use phrases such as same as baseline '
                'or only changed X; explain the complete method. Title <=40, method <=280, '
                'limitations <=80 characters. Do not write scores in prose; the tool attaches ALL trusted '
                'development results of this exact source, including failures and probes. '
                'If the checkpoint selects no candidate, submit candidate=null; never substitute an intermediate attempt.')


def _complete(run, state, sid, data):
    cid = _selected(run, state, sid)[1]
    reported = data['candidate']
    if (reported is None) != (cid is None) or (reported is not None and reported['candidate_id'] != cid):
        raise ValueError('method report must describe only the final checkpoint candidate')


def require_reports(run, cfg, state, target_session=None):
    missing = pending_reports(run, cfg, state)
    if any(target_session is None or r['session_id'] == target_session for r in missing):
        raise ValueError('selection must seal the completed session method report first')
    return dict(reports=len(_refs(state)))


def report_submit(run, cfg, state, target_session, data):
    base._open_research(state)
    report_audit(run, cfg, state)
    c.validate_schema(output_schema(cfg['modules']['selections'], 'method_report'), data)
    _session(state, target_session)
    _complete(run, state, target_session, data)
    report = dict(prose=data, facts=_facts(run, cfg, state, target_session))
    ref = _refs(state).get(target_session)
    if ref:
        if c.read_json(c.contained(run, ref['path'])) != report:
            raise ValueError('sealed method report cannot be changed')
        return dict(**ref, cached=True)
    relative = 'research/reports/' + target_session + '.json'
    path = c.contained(run, relative)
    if path.exists() and c.read_json(path) != report:
        raise ValueError('unregistered method report differs; reconcile before retrying')
    c.write_json(path, report)
    ref = dict(path=relative, sha=c.sha(path))
    state.setdefault('extensions', {}).setdefault('selection_reports', {})[target_session] = ref
    c.save_state(run, state)
    c.event(run, 'selection-method-report', session_id=target_session, **ref)
    return dict(**ref, cached=False)
