"""Read shareable development materials from the named batch for the domain wiki."""
import hashlib
from itertools import islice

from tools import common as c


def verified(path, expected):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f'sealed content changed: {path.name}')
    return data


def catalog(run, state, report_ids):
    """Claims stay separate from current measurements. Never open private notebooks."""
    extensions = state.get('extensions', {})
    public = set()
    findings = {}
    reports = {}
    for fid, ref in extensions.get('research_exchange', {}).items():
        if ref['kind'] != 'finding':
            continue
        path = c.contained(run, f'research/exchange/finding/{c.identifier(fid)}.json')
        record = c.parse_json(verified(path, ref['sha']))
        payload = record['payload']
        if record['kind'] != 'finding' or record['record_id'] != fid:
            raise ValueError('finding identity mismatch')
        evidence = [item['evaluation_id'] for item in payload['trusted_evidence']]
        public.update(evidence)
        findings[fid] = dict(claim=payload['author_claim']['claim'], evidence_ids=evidence)
    sealed_reports = extensions.get('selection_reports', {})
    for sid in sorted(set(report_ids)):
        ref = sealed_reports[sid]
        path = c.contained(run, f'research/reports/{c.identifier(sid)}.json')
        report = c.parse_json(verified(path, ref['sha']))
        if report['facts']['session_id'] != sid:
            raise ValueError('report identity mismatch')
        candidate = report['facts']['candidate']
        evidence = [e['evaluation_id'] for e in candidate['evaluations']] if candidate else []
        public.update(evidence)
        reports[sid] = dict(method=report['prose']['candidate'], evidence_ids=evidence)
    return public, findings, reports


def evaluation_path(run, eid, relative):
    path = c.contained(run, relative)
    if not path.is_relative_to(run / 'research/evaluations' / c.identifier(eid)):
        raise ValueError('expected development evaluation path')
    return path


def case_rows(run, eid, summary, seal, history):
    """A retry may inherit settled rows from earlier, separately sealed journals."""
    prefix = summary.get('prefix')
    if prefix:
        parent_path = prefix['summary']
        parent_seal = history[parent_path]
        parent = c.parse_json(verified(evaluation_path(run, eid, parent_path), parent_seal['summary_sha']))
        if parent['evaluation_id'] != eid or parent['dataset_purpose'] != summary['dataset_purpose']:
            raise ValueError('retry prefix identity mismatch')
        yield from islice(case_rows(run, eid, parent, parent_seal,
                                    {k:v for k,v in history.items() if k != parent_path}), prefix['cases'])
    data = verified(evaluation_path(run, eid, summary['details_file']), seal['results_sha'])
    for line in data.splitlines():
        row = c.parse_json(line)
        if row['type'] == 'result':
            result = row['result']
            yield {'case_id': row['case_id'], **{key: result[key] for key in
                   ('valid', 'score', 'wall_s', 'metrics', 'infrastructure_error', 'cleanup_confirmed')
                   if key in result}}


def collect(batch, evaluation=None, expected=None, cases=False, report_ids=()):
    run = c.contained(c.ROOT, 'runs/' + c.run_identifier(batch))
    state = c.read_json(c.contained(run, 'state.json'))
    public, findings, reports = catalog(run, state, report_ids)
    evaluations = {}
    entries = state['evaluations']
    for eid in ([evaluation] if evaluation else sorted(entries)):
        relative = entries[eid]
        if not relative.startswith('research/evaluations/'):
            continue
        seal = state.get('evaluation_hashes', {}).get(eid)
        if not seal or eid in state.get('stale_evaluations', []):
            if eid in public:
                evaluations[eid] = {'available': False}
            continue
        data = verified(evaluation_path(run, eid, relative), seal['summary_sha'])
        summary = c.parse_json(data)
        if summary['candidate_id'] != 'baseline' and eid not in public:
            continue
        if (summary['evaluation_id'] != eid or summary['dataset_purpose'] not in ('anchor', 'challenge')
                or summary['status'] not in ('completed', 'failed')):
            raise ValueError('expected settled development evaluation')
        if expected and expected != seal['summary_sha']:
            raise ValueError(f'evidence revision changed: {eid}; current {seal["summary_sha"]}')
        fields = ('candidate_id', 'source_sha', 'dataset_id', 'dataset_purpose', 'status', 'probe',
                  'all_valid', 'score', 'valid_cases', 'completed_cases', 'total_cases',
                  'infrastructure_error', 'cleanup_confirmed', 'evaluation_wall_s', 'error')
        item = {k:summary[k] for k in fields if k in summary}
        item['citation'] = f'{batch} {eid} {seal["summary_sha"]}'
        if cases:
            history = {v['summary']: v for v in state.get('evaluation_history', {}).get(eid, [])}
            item['cases'] = list(case_rows(run, eid, summary, seal, history))
        evaluations[eid] = item
    if evaluation:
        if evaluation not in evaluations or not evaluations[evaluation].get('citation'):
            raise ValueError('no current shareable development result for this ID')
        return evaluations[evaluation]
    return dict(findings=findings, reports=reports, evaluations=evaluations,
                report_sessions=sorted(state.get('extensions', {}).get('selection_reports', {})))
