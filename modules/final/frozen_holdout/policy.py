"""Authorization and frozen-selection policy for final measurements."""
from __future__ import annotations

import math

from tools.common import contained, event, read_json, safe_boundary, save_state, utc


def gate(config, state, manifest, candidate_id, source_sha, probe):
    if manifest["purpose"] != "final":
        return
    selected = state.get("selection")
    if not config["final"]["authorized"] or not selected or probe:
        raise ValueError("final measurements require authorization and frozen selection; probes are forbidden")
    if candidate_id != "baseline" and (candidate_id != selected["candidate_id"] or source_sha != selected["source_sha"]):
        raise ValueError("final measurement permits only baseline or the frozen candidate")



# Final selection compares only complete, valid fixed-anchor measurements.


def _summaries(run, state):
    for eid, path in sorted(state['evaluations'].items()):
        summary = read_json(contained(run, path))
        if summary['evaluation_id'] != eid:
            raise ValueError('evaluation ID mismatch')
        yield summary


def _eligible(run, state):
    eligible = []
    for summary in _summaries(run, state):
        if (summary['status'] == 'completed' and summary['all_valid']
                and not summary['infrastructure_error'] and summary['cleanup_confirmed']
                and summary['dataset_purpose'] == 'anchor' and not summary['probe']
                and summary['candidate_id'] != 'baseline'):
            candidate = state['deliveries']['candidate'].get(summary['candidate_id'])
            dataset = state['deliveries']['dataset'].get(summary['dataset_id'])
            if not candidate or not dataset or dataset['manifest']['purpose'] != 'anchor':
                raise ValueError('evaluation references an unknown candidate or anchor')
            if candidate['files'][candidate['manifest']['source_file']] != summary['source_sha']:
                raise ValueError('evaluation source hash mismatch')
            if (isinstance(summary['score'], bool) or not isinstance(summary['score'], (int, float))
                    or not math.isfinite(summary['score'])):
                raise ValueError('eligible evaluation needs a finite score')
            eligible.append(summary)
    return eligible


def _best(run, cfg, state):
    eligible = _eligible(run, state)
    sign = 1 if cfg['task']['objective']['direction'] == 'minimize' else -1
    return min(eligible, key=lambda s: (sign * s['score'], s['source_sha'],
                                      s['evaluation_id'])) if eligible else None


def recommendation(run, cfg, state):
    """Read-only winner using the same deterministic rule as select."""
    best = _best(run, cfg, state)
    return None if best is None else {key: best[key] for key in
        ('candidate_id', 'source_sha', 'dataset_id', 'evaluation_id', 'score')}


def select(run, cfg, state, candidate):
    safe_boundary(state)
    if any(s['status'] not in ('completed', 'failed') for s in _summaries(run, state)):
        raise ValueError('end active measurements first')
    from tools.module_runtime import invoke
    if cfg.get('modules', {}).get('selections', {}).get('capabilities', {}).get('method_reports'):
        invoke(cfg, 'selections', 'require_reports', run, cfg, state)
    best = _best(run, cfg, state)
    if best is None:
        raise ValueError('no complete valid anchor evaluation')
    if best['candidate_id'] != candidate:
        raise ValueError(f"candidate is not the best eligible anchor result; expected '{best['candidate_id']}' "
                         f"from evaluation '{best['evaluation_id']}'. Ties use source_sha then evaluation_id.")
    state['selection'] = {
        'candidate_id': candidate, 'source_sha': best['source_sha'],
        'anchor_dataset_id': best['dataset_id'], 'evaluation_id': best['evaluation_id'],
        'score': best['score'], 'at': utc(), 'config_sha': state['config_sha']}
    save_state(run, state)
    event(run, 'selected', **state['selection'])
    return state['selection']
