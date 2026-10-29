"""Validate development evidence referenced by researcher-authored ideas."""
from tools.common import contained, identifier, read_json, sha


def _open_research(state):
    # Ideas can be reviewed while another researcher is active. They never admit work.
    if state.get('selection'):
        raise ValueError('selection already frozen')


def _ideas(state):
    return {idea['idea_id']: idea
            for record in state['deliveries']['ideas'].values()
            for idea in record['manifest']['ideas']}


def _summary(run, state, eid):
    if eid not in state['evaluations']:
        raise ValueError('unknown evaluation reference')
    summary = read_json(contained(run, state['evaluations'][eid]))
    if summary['evaluation_id'] != eid:
        raise ValueError('evaluation ID mismatch')
    return summary


def _development(state, summary):
    dataset = state['deliveries']['dataset'].get(summary['dataset_id'])
    if not dataset:
        raise ValueError('unknown evaluation dataset')
    purpose = dataset['manifest']['purpose']
    if summary['dataset_purpose'] == 'final' or purpose == 'final':
        return False
    if purpose not in ('anchor', 'challenge') or purpose != summary['dataset_purpose']:
        raise ValueError('evaluation dataset purpose mismatch')
    return True


def _research_evidence(run, state, eid):
    summary = _summary(run, state, eid)
    if summary['status'] not in ('completed', 'failed'):
        raise ValueError('referenced evaluation not complete')
    if not _development(state, summary):
        raise ValueError('final evaluation cannot be research feedback')
    seal = state.get('evaluation_hashes', {}).get(eid)
    if (not seal or sha(contained(run,state['evaluations'][eid])) != seal['summary_sha']
            or sha(contained(run,summary['details_file'])) != seal['results_sha']):
        raise ValueError('unsealed evaluation requires reconciliation')
    if summary['candidate_id'] != 'baseline':
        candidate = state['deliveries']['candidate'].get(summary['candidate_id'])
        if not candidate:
            raise ValueError('unknown evaluation candidate')
        if candidate['files'][candidate['manifest']['source_file']] != summary['source_sha']:
            raise ValueError('evaluation source hash mismatch')
    return summary


def _ended_development(run, state):
    for eid in sorted(state['evaluations']):
        summary = _summary(run, state, eid)
        if summary['status'] in ('completed', 'failed') and _development(state, summary):
            yield _research_evidence(run, state, eid)


def validate_delivery(run, cfg, state, data, add, entry):
    """Check provenance; hypotheses and algorithm changes come from the researcher."""
    _open_research(state)
    identifier(data['iteration_id'])
    ideas = _ideas(state)
    iterations = state['deliveries'].get('iteration', {})
    if not any(data[key] for key in ('source_idea_ids', 'evidence_ids', 'history_iteration_ids')):
        raise ValueError('iteration needs an idea, development evidence, or history reference')
    for idea_id in data['source_idea_ids']:
        if idea_id not in ideas:
            raise ValueError('unknown idea reference')
    for iteration_id in data['history_iteration_ids']:
        if iteration_id == data['iteration_id'] or iteration_id not in iterations:
            raise ValueError('unknown history iteration reference')
    evidence = {eid: _research_evidence(run, state, eid) for eid in data['evidence_ids']}
    parent = data['parent_candidate']
    if parent is not None:
        if parent not in state['deliveries']['candidate']:
            raise ValueError('unknown parent candidate')
        previous = [s for s in _ended_development(run, state) if s['candidate_id'] == parent]
        if previous and not any(s['candidate_id'] == parent for s in evidence.values()):
            raise ValueError('parent development evaluation must be included in evidence_ids')


def _pick(record, keys):
    return {key: record[key] for key in keys if key in record}


def context(run, cfg, state, actor=None):
    """Return compact development inputs, without drafting a hypothesis or admitting work."""
    _open_research(state)
    if actor is not None:
        identifier(actor)
    evaluations = []
    for summary in _ended_development(run, state):
        item = _pick(summary, ('evaluation_id', 'candidate_id', 'source_sha', 'dataset_id',
                              'dataset_purpose', 'status', 'probe', 'all_valid',
                              'infrastructure_error', 'cleanup_confirmed', 'score',
                              'total_cases', 'completed_cases', 'valid_cases', 'wall_s', 'error'))
        item['evidence_role'] = ('anchor_comparison' if summary['status'] == 'completed'
            and summary['all_valid'] and not summary['infrastructure_error']
            and summary['cleanup_confirmed'] and summary['dataset_purpose'] == 'anchor'
            and not summary['probe'] else 'diagnostic')
        evaluations.append(item)
    candidates = []
    for cid, record in sorted(state['deliveries']['candidate'].items()):
        manifest = record['manifest']
        item = _pick(manifest, ('candidate_id', 'session_id', 'parent_id', 'rationale'))
        item['candidate_id'] = cid
        item['source_sha'] = record['files'][manifest['source_file']]
        candidates.append(item)
    ideas = _ideas(state)
    result = {
        'ideas': [ideas[iid] for iid in sorted(ideas)],
        'candidates': candidates,
        'evaluations': evaluations,
        'iterations': [record['manifest'] for _, record in
                       sorted(state['deliveries'].get('iteration', {}).items())],
    }
    if actor is not None:
        result['actor_history'] = [
            _pick(session, ('session_id', 'actor', 'generation', 'status', 'started_at',
                            'finished_at', 'idea_id', 'iteration_id', 'parent_candidate'))
            for session in state['sessions'] if session['actor'] == actor]
    return result
