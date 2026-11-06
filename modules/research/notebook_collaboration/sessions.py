"""Per-researcher admission and provenance; the main session executes MODULE.md's loop.

No model is called here. Separate researchers can be active together, while the
short state mutation lock protects bookkeeping and each actor advances in order.
"""
import copy
import datetime as dt

from tools import common as c
from tools.execution import resolve
from tools.module_runtime import invoke


def research_time(cfg, session, now=None):
    """Derive elapsed time from tool-owned intervals, never caller-reported durations."""
    until = dt.datetime.fromisoformat(now or session.get('finished_at') or c.utc())
    registered = dt.datetime.fromisoformat(session['started_at'])
    activated = session.get('activated_at')
    if until < registered:
        raise ValueError('session clock precedes registration')
    if not activated:
        return dict(activated=False, active_research_s=0.0, evaluation_wait_s=0.0,
                    wall_elapsed_s=0.0, launch_wait_s=(until-registered).total_seconds(),
                    remaining_s=cfg['budgets']['session_wall_s'], exhausted=False,
                    pending_request_ids=[], pending_preflight_ids=[])
    start = dt.datetime.fromisoformat(activated)
    if start < registered or until < start:
        raise ValueError('invalid research activation clock')
    intervals = []
    pending_requests, pending_preflights = [], []
    for kind, waits in (('request', session.get('evaluation_waits', {})),
                        ('preflight', session.get('preflight_waits', {}))):
        for key, wait in waits.items():
            began = dt.datetime.fromisoformat(wait['started_at'])
            ended = dt.datetime.fromisoformat(wait['finished_at']) if wait['finished_at'] else until
            if began < start or ended < began or ended > until:
                raise ValueError('invalid trusted evaluation clock interval')
            intervals.append((began, ended))
            if wait['finished_at'] is None:
                (pending_requests if kind == 'request' else pending_preflights).append(key)
    # The union also makes overlapping trusted intervals safe against double credit.
    merged = []
    for began, ended in sorted(intervals):
        if merged and began <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], ended))
        else:
            merged.append((began, ended))
    excluded = sum((end-begin).total_seconds() for begin, end in merged)
    wall = (until-start).total_seconds()
    elapsed = max(0.0, wall-excluded)
    return dict(activated=True, active_research_s=elapsed, evaluation_wait_s=excluded,
                wall_elapsed_s=wall, launch_wait_s=(start-registered).total_seconds(),
                remaining_s=max(0.0, cfg['budgets']['session_wall_s']-elapsed),
                exhausted=elapsed >= cfg['budgets']['session_wall_s'],
                pending_request_ids=pending_requests, pending_preflight_ids=pending_preflights)


def _open(run, cfg, state, now):
    if state.get('selection'):
        raise ValueError('selection already frozen')
    elapsed = (dt.datetime.fromisoformat(now)
               - dt.datetime.fromisoformat(cfg['created_at'])).total_seconds()
    if elapsed >= cfg['budgets']['run_wall_s']:
        raise ValueError('run admission deadline reached')
    for eid, path in state['evaluations'].items():
        summary = c.read_json(c.contained(run, path))
        if summary['status'] == 'running' and summary.get('cleanup_confirmed') and not summary.get('infrastructure_error'):
            # Each evaluation owns a lease while its cases may share sandbox slots.
            if invoke(cfg, 'evaluation', 'evaluation_active', run, eid):
                continue
        if (summary['status'] not in ('completed', 'failed')
                or not summary.get('cleanup_confirmed')
                or summary.get('infrastructure_error')):
            raise ValueError('evaluation requires reconciliation before new research')


def prepare_session(run, cfg, state, actor, role, idea_id=None, iteration_id=None, now=None):
    now = now or c.utc()
    c.identifier(actor)
    _open(run, cfg, state, now)
    launch = resolve(cfg, 'research', role)
    if role == 'self' or launch['mode'] != 'subagent':
        raise ValueError('begin-session requires a declared research delegate role')
    previous = [s for s in state['sessions'] if s['actor'] == actor]
    if any(s['status'] == 'active' for s in previous):
        raise ValueError('this actor already has an active research session')
    actors = {s['actor'] for s in state['sessions']}
    if actor not in actors and len(actors) >= cfg['budgets']['researchers']:
        raise ValueError('researcher budget exhausted')
    if len(previous) >= cfg['budgets']['rounds_per_researcher']:
        raise ValueError('round budget exhausted')
    if (cfg['budgets']['evaluation_calls'] is not None
            and len(state['evaluations']) >= cfg['budgets']['evaluation_calls']):
        raise ValueError('evaluation budget exhausted')
    if bool(idea_id) == bool(iteration_id):
        raise ValueError('provide exactly one --idea or --iteration')
    if previous and not iteration_id:
        raise ValueError('later generations require an algorithm iteration')
    parent = None
    if idea_id:
        c.identifier(idea_id)
        ideas = [idea for record in state['deliveries']['ideas'].values()
                 for idea in record['manifest']['ideas'] if idea['idea_id'] == idea_id]
        if len(ideas) != 1:
            raise ValueError('unknown or ambiguous idea reference')
    else:
        c.identifier(iteration_id)
        record = state['deliveries'].get('iteration', {}).get(iteration_id)
        if record is None:
            raise ValueError('unknown iteration reference')
        if any(s.get('iteration_id') == iteration_id for s in previous):
            raise ValueError('iteration was already consumed by this actor')
        invoke(cfg, 'selections', 'validate_delivery', run, cfg, state,
               record['manifest'], None, cfg['modules']['selections'])
        parent = record['manifest']['parent_candidate']
    generation = len(previous) + 1
    session = {
        'session_id': actor + '-r' + format(generation, '03d'),
        'actor': actor, 'generation': generation, 'status': 'active',
        'started_at': now, 'activated_at': None, 'native_id': None, 'module': 'research',
        'evaluation_waits': {}, 'preflight_waits': {},
        'role': role, 'profile': launch['profile'],
        'spawn_options': copy.deepcopy(launch['spawn_options']),
        'config_sha': state['config_sha'], 'idea_id': idea_id,
        'iteration_id': iteration_id, 'parent_candidate': parent,
    }
    return session


def commit_session(run, state, session):
    state['sessions'].append(session)
    c.save_state(run, state)
    c.event(run, 'begin-session', **session)
    return session


def begin_session(run, cfg, state, actor, role, idea_id=None, iteration_id=None, now=None):
    return commit_session(run, state, prepare_session(run, cfg, state, actor, role, idea_id, iteration_id, now))


def activate_session(run, cfg, state, actor, native_id, now=None):
    """Coordinator records a successful native spawn before research is admitted."""
    c.identifier(actor)
    if not isinstance(native_id, str) or not native_id.strip():
        raise ValueError('activation requires the actual native session ID')
    found = [s for s in state['sessions'] if s['actor'] == actor and s['status'] == 'active']
    if len(found) != 1:
        raise ValueError('no unique active session for actor')
    session = found[0]
    if any(s is not session and s.get('native_id') == native_id for s in state['sessions']):
        raise ValueError('collaborative research continuation must use a fresh native session')
    if session.get('activated_at'):
        if session['native_id'] != native_id:
            raise ValueError('active session is already bound to a different native session')
        return session
    activated = now or c.utc()
    _open(run, cfg, state, activated)
    if dt.datetime.fromisoformat(activated) < dt.datetime.fromisoformat(session['started_at']):
        raise ValueError('session activation precedes registration')
    session.update(activated_at=activated, native_id=native_id)
    c.save_state(run, state)
    c.event(run, 'activate-session', session_id=session['session_id'], native_id=native_id,
            activated_at=activated)
    return session


def end_session(run, cfg, state, actor, status, native_id=None, now=None):
    c.identifier(actor)
    if status not in ('completed', 'failed'):
        raise ValueError('session must end completed or failed')
    active = [s for s in state['sessions'] if s['actor'] == actor and s['status'] == 'active']
    if len(active) != 1:
        raise ValueError('no unique active session for actor')
    session = active[0]
    if native_id is not None and native_id != session.get('native_id'):
        raise ValueError('end-session native ID differs from the activated native session')
    if status == 'completed' and not session.get('activated_at'):
        raise ValueError('completed session must have an activated native session')
    finished = now or c.utc()
    timing = research_time(cfg, session, finished)
    if timing['pending_request_ids'] or timing['pending_preflight_ids']:
        raise ValueError('pending evaluation waits require reconciliation before ending session')
    session.update(status=status, finished_at=finished, elapsed_s=timing['active_research_s'],
                   research_time=timing, over_budget=timing['exhausted'])
    c.save_state(run, state)
    c.event(run, 'end-session', **session)
    return session
