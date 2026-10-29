"""Resolve main-session modules and explicit research-worker launch settings."""
from tools import common as c


def validate_execution(cfg):
    c.validate('agent', cfg['agent'])
    profiles = cfg['agent']['profiles']
    for name, settings in profiles.items():
        if settings['model'].casefold() in ('inherit', 'auto', 'default'):
            raise ValueError('profile must specify an explicit model: ' + name)
    for slot, entry in cfg['modules'].items():
        policy = entry['execution']
        c.validate('execution', policy)
        if policy['mode'] != 'main':
            raise ValueError('module execution must be main; subagents are research delegates only')
        if slot != 'research' and policy['delegates']:
            raise ValueError('only research may declare subagent delegates: ' + slot)
        references = list(policy['delegates'].values())
        if references and not cfg['agent']['native_subagents']:
            raise ValueError('native subagents are disabled: ' + slot)
        for profile in references:
            if profile not in profiles:
                raise ValueError('unknown agent profile: ' + slot + '/' + profile)


def template(config_path):
    from tools.configuration import prepare
    cfg = prepare(config_path)
    # initialize runs before task.json exists; this path must not read/import the task.
    from tools.module_runtime import resolve_bindings
    cfg['modules'] = resolve_bindings(cfg['modules'])
    validate_execution(cfg)
    return cfg


def resolve(cfg, slot, role='self'):
    if cfg['schema_version'] != 5:
        raise ValueError('historical run is read-only; explicit launch settings were not frozen')
    validate_execution(cfg)
    if slot not in cfg['modules']:
        raise ValueError('unknown plugin slot: ' + slot)
    c.identifier(role)
    entry = cfg['modules'][slot]
    policy = entry['execution']
    if role == 'self':
        profile = policy.get('profile')
    else:
        if role not in policy['delegates']:
            raise ValueError('undeclared subagent role: ' + slot + '/' + role)
        profile = policy['delegates'][role]

    def options(name):
        return dict(cfg['agent']['profiles'][name], fork_turns='none')

    return {'slot': slot, 'module_id': entry['id'], 'module_sha': entry['sha'],
            'config_sha': c.digest(cfg), 'role': role,
            'mode': 'subagent' if profile else 'main', 'profile': profile,
            'spawn_options': options(profile) if profile else None,
            'allowed_delegates': {name: options(ref) for name, ref in policy['delegates'].items()}
                                if role == 'self' else {},
            'platform_support': 'must_check_before_spawn' if profile else 'not_applicable_to_self',
            'instruction': ('Pass every spawn_options field unchanged to the native spawn tool, '
                            'adding task_name and the authorized task message. Stop if unsupported; '
                            'never fall back to inherited settings. Delegates may not redelegate.'
                            if profile else
                            'Execute self in the current conversation. Only declared delegate roles '
                            'may be spawned, after resolving their explicit launch settings.')}
