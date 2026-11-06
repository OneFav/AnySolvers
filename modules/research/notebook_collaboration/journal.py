"""Durable exchange intents; recovery only publishes already validated metadata.

All calls hold the run mutation lock. Evaluation side effects have a separate
completion receipt and are never replayed by this journal.
"""
from tools import common as c


def prepare(run, cfg, record):
    path = c.contained(run, 'research/journal/' + c.identifier(record['record_id']) + '.json')
    if path.exists():
        raise ValueError('existing tool intent requires reconciliation')
    intent = dict(config_sha=c.digest(cfg), record=record, record_sha=c.digest(record))
    c.write_json(path, intent)
    # Persist the newly created journal directory entry as well.
    c.sync_directory(path.parent.parent)


def intents(run, cfg, state):
    directory = c.contained(run, 'research/journal')
    for path in sorted(directory.glob('*.json')):
        path = c.contained(directory, path.name)
        value = c.read_json(path)
        record = value['record']
        rid = c.identifier(record['record_id'])
        owners = [s for s in state['sessions'] if s['session_id'] == record['session_id']]
        if (path.name != rid + '.json' or len(owners) != 1
                or value['config_sha'] != owners[0]['config_sha']
                or value['record_sha'] != c.digest(record) or record['run_id'] != cfg['run_id']
                or record['kind'] not in ('request', 'response', 'notebook', 'finding', 'reuse')):
            raise ValueError('tool intent identity or content changed')
        yield record


def audit(run, cfg, state):
    refs = state.get('extensions', {}).get('research_exchange', {})
    found = set()
    for record in intents(run, cfg, state):
        rid = record['record_id']
        ref = refs.get(rid)
        if not ref:
            raise ValueError('pending tool intent requires reconciliation: ' + rid)
        relative = 'research/exchange/' + record['kind'] + '/' + rid + '.json'
        if (ref['path'] != relative or ref['kind'] != record['kind']
                or c.read_json(c.contained(run, relative)) != record):
            raise ValueError('tool intent differs from committed record')
        found.add(rid)
    if found != set(refs):
        raise ValueError('committed exchange record is missing its durable intent')


def reconcile(run, cfg, state):
    refs = state.setdefault('extensions', {}).setdefault('research_exchange', {})
    recovered = []
    # Preserve submission order: responses may follow their pending requests.
    for record in sorted(intents(run, cfg, state), key=lambda row: (row['at'], row['record_id'])):
        rid, kind, sid = record['record_id'], record['kind'], record['session_id']
        if rid in refs:
            continue
        sessions = [s for s in state['sessions'] if s['session_id'] == sid]
        if len(sessions) != 1 or sessions[0]['status'] != 'active' or state.get('selection'):
            raise ValueError('tool intent no longer belongs to an active session')
        session = sessions[0]
        relative = 'research/exchange/' + kind + '/' + rid + '.json'
        path = c.contained(run, relative)
        if path.exists() and c.read_json(path) != record:
            raise ValueError('tool result differs from durable intent')
        if kind == 'request':
            session['evaluation_waits'][rid] = dict(started_at=record['at'], finished_at=None)
        elif kind == 'response':
            request_id = record['payload']['request_id']
            request = refs.get(request_id)
            if not request or request['kind'] != 'request' or rid != request_id + '-response':
                raise ValueError('response intent lacks its original request')
            session['evaluation_waits'][request_id]['finished_at'] = record['at']
        c.write_json(path, record)
        refs[rid] = dict(path=relative, sha=c.sha(path), kind=kind)
        c.save_state(run, state)
        recovered.append(rid)
    audit(run, cfg, state)
    return recovered
