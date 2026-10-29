"""Validate researcher-authored algorithm ideas; never schedule or call a subagent."""
from tools import common as c
from modules.selections.evidence_iteration import evidence as base


def evidence(run, state, evaluation_id):
    return base._research_evidence(run, state, evaluation_id)


def validate_delivery(run, cfg, state, data, add, entry):
    base.validate_delivery(run, cfg, state, data, add, entry)
    owner = [s for s in state['sessions'] if s['session_id'] == data['author_session']]
    if len(owner) != 1 or (add is not None and owner[0]['status'] != 'active'):
        raise ValueError('iteration must be delivered by its active researcher session')
    # add=None is validation of an already sealed iteration at later admission.
    if add is None and data['iteration_id'] not in state['deliveries']['iteration']:
        raise ValueError('continuation needs a sealed researcher iteration')


def context(run, cfg, state, actor=None):
    value = base.context(run, cfg, state, actor)
    if actor is not None:
        own = {s['session_id'] for s in state['sessions'] if s['actor'] == actor}
        # Sharing goes through findings; private experiments/notes are not broadcast.
        shared = set()
        published_evaluations = set()
        for ref in state.get('extensions', {}).get('research_exchange', {}).values():
            if ref['kind'] != 'finding':
                continue
            path = c.contained(run, ref['path'])
            if c.sha(path) != ref['sha']:
                raise ValueError('finding changed')
            rec = c.read_json(path)['payload']
            published_evaluations.update(e['evaluation_id'] for e in rec['trusted_evidence'])
            if rec['candidate']:
                shared.add(rec['candidate']['candidate_id'])
        own_candidates = {r['candidate_id'] for r in value['candidates'] if r['session_id'] in own}
        cids = own_candidates | shared
        value['candidates'] = [r for r in value['candidates'] if r['candidate_id'] in cids]
        value['evaluations'] = [r for r in value['evaluations'] if r['candidate_id'] in own_candidates
                               or r['evaluation_id'] in published_evaluations or r['candidate_id']=='baseline']
        value['iterations'] = [r for r in value['iterations'] if r['author_session'] in own]
    return value
