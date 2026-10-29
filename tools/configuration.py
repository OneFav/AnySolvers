"""One shared base plus sparse run overrides; expanded configuration lives in memory."""
import copy
import yaml

from tools import common as c


def merge(base, overrides):
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize(base):
    c.validate('config_template', base)
    result = copy.deepcopy(base)
    agent = result['agent']
    defaults = agent.pop('defaults', {})
    agent['profiles'] = {name: merge(defaults, values)
                         for name, values in agent['profiles'].items()}
    c.validate('agent', agent)
    return result


def read_base(path):
    raw = c.read_yaml(c.workspace_file(path))
    if raw.get('schema_version') != 5:
        raise ValueError('base requires schema_version 5 and explicit agent profiles; chained extends is unsupported')
    c.validate('config_template', raw)
    return raw


def overlay(document, base):
    name = {6:'legacy_run_document',7:'legacy_batch_document'}.get(document.get('schema_version'),'run_document')
    c.validate(name, document)
    # Run document version describes storage, not the effective configuration schema.
    values = {k: v for k, v in document.items() if k not in ('schema_version', 'extends', 'initialization')}
    return normalize(merge(base, values))


def prepare(config_path):
    """Read a base or a not-yet-initialized run draft. No task reads or asset writes."""
    path = c.workspace_file(config_path)
    raw = c.read_yaml(path)
    if raw.get('schema_version') == 8:
        if (path.parent/'state.json').exists():
            raise ValueError('initialized run requires --run so its frozen base version is used')
        c.validate('run_document', raw)
        return overlay(raw, read_base(raw['extends']))
    return normalize(read_base(config_path))


def frozen_base(lock):
    raw = yaml.load(c.read_pinned(lock['base'], 'base configuration').decode('utf-8'),
                    Loader=c.StrictLoader)
    c.validate('config_template', raw)
    return raw


def effective(document, lock):
    if document['extends'] != lock['base']['path']:
        raise ValueError('base configuration reference changed')
    cfg = overlay(document, frozen_base(lock))
    if document['schema_version'] in (7,8):
        if document.get('initialization') != lock['initialization']['path']:
            raise ValueError('shared initialization reference changed')
        cfg['initialization'] = copy.deepcopy(lock['initialization'])
    elif cfg['task'] != lock['task']['path']:
        raise ValueError('task reference differs from frozen task')
    if set(cfg['modules']) != set(lock['modules']):
        raise ValueError('module slots differ from frozen bindings')
    from tools.module_runtime import entry_path
    for slot, binding in cfg['modules'].items():
        frozen = lock['modules'][slot]
        if str(entry_path(binding['use']).relative_to(c.ROOT)) != frozen['path']:
            raise ValueError('module reference differs from frozen binding: ' + slot)
        if merge(frozen['defaults'], binding.get('params',{})) != frozen['params']:
            raise ValueError('module parameters differ from frozen binding: ' + slot)
    cfg.update({key: copy.deepcopy(lock[key])
                for key in ('task', 'modules', '_pins', 'run_id', 'created_at')})
    c.validate('config', cfg)
    return cfg


def legacy_effective(document, lock):
    """Legacy inherited run can be inspected only with its original working base present."""
    if c.digest(document) != lock['document_sha'] or document['extends'] != lock['base']['path']:
        raise ValueError('historical configuration changed')
    base = yaml.load(c.read_pinned(lock['base'], 'historical base configuration').decode(), Loader=c.StrictLoader)
    cfg = merge(base, {k:v for k,v in document.items() if k not in ('schema_version','extends')})
    defaults = cfg['agent'].pop('defaults',{})
    cfg['agent']['profiles'] = {k:merge(defaults,v) for k,v in cfg['agent']['profiles'].items()}
    cfg.update({key:copy.deepcopy(lock[key]) for key in ('task','plugins','_pins','run_id','created_at')})
    return cfg
