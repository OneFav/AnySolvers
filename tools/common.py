"""Small file/contract helpers. No model calls or workflow scheduler."""
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import jsonschema
import yaml

ENGINE_ROOT = Path(__file__).resolve().parents[1]
# Project paths are anchored here, independent of the caller working directory.
ROOT = Path(os.environ.get("ANYSOLVERS_PROJECT_ROOT", str(ENGINE_ROOT))).expanduser().resolve()
ID = re.compile(r'^[a-z][a-z0-9-]{0,63}$')


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate key: ' + str(key))
        obj[key] = value
    return obj


def parse_json(text):
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite JSON number')
        return number
    return json.loads(text, object_pairs_hook=unique, parse_float=finite,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def read_json(path):
    return parse_json(Path(path).read_text())


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    return unique([(loader.construct_object(k), loader.construct_object(v)) for k, v in node.value])


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(path):
    return yaml.load(Path(path).read_text(), Loader=StrictLoader)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, obj):
    path = Path(path)
    data = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    atomic_write(path, data.encode('utf-8'))


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, data):
    """Durable replacement: file contents before rename, directory before acknowledgement."""
    path = Path(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        sync_directory(directory.parent)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        sync_directory(path.parent)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def logical_path(path):
    path = Path(path).absolute()
    if path.is_relative_to(ENGINE_ROOT) and path.relative_to(ENGINE_ROOT).parts[0] in ('tools', 'modules'):
        return str(path.relative_to(ENGINE_ROOT))
    return str(path.relative_to(ROOT))


def contained(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('expected a contained relative path')
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink is not allowed: ' + str(relative))
    if not current.resolve().is_relative_to(root):
        raise ValueError('path escapes root')
    return current


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('invalid identifier: ' + str(value))
    return value


def validate_schema(schema, obj):
    # Contracts are local self-contained JSON schemas; never fetch a remote reference.
    def walk(value):
        if isinstance(value, dict):
            if '$ref' in value and not value['$ref'].startswith('#'):
                raise ValueError('contract references must be local to the schema')
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(schema)
    jsonschema.Draft202012Validator(schema).validate(obj)


def validate(kind, obj, cfg=None):
    core = read_json(ENGINE_ROOT / 'tools/schemas.json')
    if kind in core['$defs']:
        schema = {'$ref':'#/$defs/'+kind, '$defs':core['$defs']}
    elif cfg is not None:
        from tools.module_runtime import output_schema
        owners = [entry for entry in cfg['modules'].values() if kind in entry['contracts']]
        if len(owners) != 1:
            raise ValueError('expected one owner for contract: ' + kind)
        schema = output_schema(owners[0], kind)
    else:
        # Legacy unit helpers have no effective cfg. Resolve the selected template
        # packages, never choose an arbitrary sibling or an inactive spare.
        from tools.module_runtime import resolve_bindings, output_schema
        entries = resolve_bindings(read_yaml(ENGINE_ROOT/'configs/smoke.yaml')['modules'])
        owners = [entry for entry in entries.values() if kind in entry['contracts']]
        if len(owners) != 1:
            raise ValueError('expected one module-owned contract: ' + kind)
        schema = output_schema(owners[0], kind)
    validate_schema(schema, obj)


def workspace_file(relative):
    parts = Path(relative).parts
    if parts and parts[0].casefold() == 'assets':
        raise ValueError('archive paths cannot be used as working inputs')
    if parts and parts[0] in ('tools', 'modules'):
        return contained(ENGINE_ROOT, relative)
    return contained(ROOT, relative)


def file_ref(path):
    """Record a working file's version without making an archival copy."""
    relative = logical_path(path)
    return {'path': relative, 'sha': sha(workspace_file(relative))}


def read_pinned(entry, label='working file'):
    path = workspace_file(entry['path'])
    if not path.is_file():
        raise ValueError(label + ' missing: ' + entry['path'])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry['sha']:
        raise ValueError(label + ' changed: ' + entry['path'])
    return data


def read_plugin(path):
    """Compatibility name; all new bindings load full module packages."""
    from tools.module_runtime import read_module
    return read_module(logical_path(path))


def benchmark_environment(run, config, state, dataset):
    """Resolve the datasets-owned runtime, including shared batch challenge refs."""
    from tools.module_runtime import invoke
    owner = dataset
    if dataset['manifest'].get('benchmark_file') == '@anchor':
        anchors = [record for record in state['deliveries']['dataset'].values()
                   if record['manifest']['purpose'] == 'anchor'
                   and record.get('scope') == 'initialization']
        if len(anchors) != 1 or dataset['manifest']['purpose'] != 'challenge':
            raise ValueError('@anchor requires a batch challenge and one shared anchor')
        owner = anchors[0]
    environment = invoke(config, 'datasets', 'load_environment',
                         delivery_root(run, owner), owner['manifest'], owner['files'])
    if environment.benchmark['task_id'] != config['task']['task_id']:
        raise ValueError('benchmark task mismatch')
    return environment


def event(run_dir, kind, **fields):
    with (Path(run_dir) / 'events.jsonl').open('a') as f:
        f.write(json.dumps({'at': utc(), 'kind': kind, **fields}, ensure_ascii=False, allow_nan=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def save_state(run_dir, state):
    # Shared manifests are a read view, not another per-batch copy of initialization.
    stored = copy.deepcopy(state)
    for kind, records in stored.get('deliveries', {}).items():
        stored['deliveries'][kind] = {key:value for key,value in records.items()
                                     if value.get('scope') != 'initialization'}
    write_json(Path(run_dir) / 'state.json', stored)


def run_path(run_id):
    return contained(ROOT / 'runs', run_identifier(run_id))


def run_identifier(value):
    """A study/number reference, or a historical flat run ID for inspection."""
    if not isinstance(value, str):
        raise ValueError('invalid run reference')
    parts = value.split('/')
    if len(parts) == 1:
        return identifier(value)
    if len(parts) != 2 or not re.fullmatch(r'(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,})', parts[1]):
        raise ValueError('expected study/001 numbered run reference')
    identifier(parts[0])
    return value


def delivery_root(run, record):
    if record.get('scope') == 'initialization':
        relative = Path(run).relative_to(ROOT / 'runs')
        run_identifier(str(relative))
        if len(relative.parts) != 2:
            raise ValueError('shared initialization requires a numbered batch')
        return contained(Path(run).parent, 'initialization')
    if 'scope' in record:
        raise ValueError('unknown delivery scope')
    return Path(run)


def output_relative(run_id, kind, filename):
    run_identifier(run_id)
    if '/' not in run_id:
        prefix = 'plans' if kind == 'plan' else ('iterations' if kind == 'iteration' else 'evaluations')
    else:
        prefix = {'plan':'research/selections','iteration':'research/selections', 'evaluation':'research/evaluations',
                  'final_evaluation':'final/evaluations'}[kind]
    return str(Path(prefix) / filename)


@contextlib.contextmanager
def mutation(run_dir, wait=False):
    with (Path(run_dir) / '.mutation.lock').open('a+') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError('another evaluation or file operation is active')
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def validate_historical_config(cfg):
    """Read-only legacy evidence: check identity/digests, never execute old protocols."""
    if cfg.get('schema_version') not in (2,3):
        raise ValueError('unsupported historical configuration')
    identifier(cfg['run_id'])
    for key in ('task','compute','agent','budgets','final','plugins','_pins'):
        if not isinstance(cfg.get(key),dict):
            raise ValueError('invalid historical configuration field: '+key)


def load_run(run_id, read_only=False, replacing_slot=None):
    run = run_path(run_id)
    document, state = read_yaml(run / 'run.yaml'), read_json(run / 'state.json')
    historical = document.get('schema_version') in (2,3,4,6,7)
    if historical:
        if not read_only:
            raise ValueError('historical run is read-only; initialize a new run')
        if document['schema_version'] in (6,7):
            from tools.configuration import effective
            lock = state['configuration']
            if digest(document) != lock.get('document_sha') or lock.get('run_id') != run_id:
                raise ValueError('historical configuration changed')
            cfg = effective(document, lock)
            validate('config', cfg)
        elif document['schema_version'] == 4:
            from tools.configuration import legacy_effective
            cfg = legacy_effective(document, state['configuration'])
        else:
            cfg = document
        if document['schema_version'] not in (6,7):
            validate_historical_config(cfg)
    else:
        validate('run_document', document)
        lock = state.get('configuration', {})
        if lock.get('run_id') != run_id or digest(document) != lock.get('document_sha'):
            raise ValueError('run configuration changed outside a recorded boundary')
        from tools.configuration import effective
        cfg = effective(document, lock)
        from tools.execution import validate_execution
        validate_execution(cfg)
    if cfg['run_id'] != run_id or digest(cfg) != state['config_sha']:
        raise ValueError('run configuration changed outside a recorded boundary')
    if historical and document['schema_version'] == 7:
        # Inspect the old shared inputs directly, without importing former modules.
        initialization = parse_json(read_pinned(cfg['initialization'], 'shared initialization manifest').decode())
        from tools.study import shared_records
        if any('scope' in record for records in state['deliveries'].values() for record in records.values()):
            raise ValueError('persisted deliveries cannot claim shared initialization scope')
        for kind, records in shared_records(initialization).items():
            if set(records) & set(state['deliveries'][kind]):
                raise ValueError('batch delivery shadows shared initialization')
            state['deliveries'][kind] = {**records, **state['deliveries'][kind]}
        for ref in cfg['task']['files'].values():
            read_pinned(ref, 'historical task file')
    if not historical:
        if any('scope' in record for records in state['deliveries'].values() for record in records.values()):
            raise ValueError('persisted deliveries cannot claim shared initialization scope')
        from tools.study import load as load_study, shared_records
        read_pinned(cfg['initialization'], 'shared initialization manifest')
        _, initialization = load_study(run_id.split('/')[0], require_sealed=True)
        for kind, records in shared_records(initialization).items():
            if set(records) & set(state['deliveries'][kind]):
                raise ValueError('batch delivery shadows shared initialization')
            state['deliveries'][kind] = {**records, **state['deliveries'][kind]}
        for entry in list(cfg['_pins'].values()) + list(cfg['task']['files'].values()):
            read_pinned(entry, 'trusted runtime or task file')
        from tools.module_runtime import verify
        for slot, entry in cfg['modules'].items():
            if slot != replacing_slot:
                verify(entry)
    for records in state['deliveries'].values():
        for record in records.values():
            if not historical and read_json(contained(delivery_root(run,record), record['manifest_path'])) != record['manifest']:
                raise ValueError('sealed delivery changed: metadata differs from manifest')
            for relative, expected in record['files'].items():
                if sha(contained(delivery_root(run,record), relative)) != expected:
                    raise ValueError('sealed delivery changed: ' + relative)
    for eid, seal in state.get('evaluation_hashes', {}).items():
        summary_path = contained(run, state['evaluations'][eid])
        if sha(summary_path) != seal['summary_sha']:
            raise ValueError('sealed evaluation summary changed: ' + eid)
        summary = read_json(summary_path)
        if sha(contained(run, summary['details_file'])) != seal['results_sha']:
            raise ValueError('sealed evaluation records changed: ' + eid)
    return run, cfg, state


def implementation_digest(cfg):
    files = {ref['path']: ref['sha'] for ref in cfg['_pins'].values()}
    for entry in cfg['modules'].values():
        files[entry['path']] = entry['sha']
        files.update({ref['path']: ref['sha'] for ref in entry['resources'].values()})
    return digest(files)


def working_file_changes(cfg):
    """Report historical working-copy differences without restoring old files."""
    changed = []
    for entry in (list(cfg['_pins'].values()) + list(cfg['task']['files'].values())
                  + list(cfg.get('plugins',cfg.get('modules',{})).values())):
        path = workspace_file(entry['path'])
        if not path.is_file() or sha(path) != entry['sha']:
            changed.append(entry['path'])
    return changed


def safe_boundary(state):
    if any(s['status'] == 'active' for s in state['sessions']):
        raise ValueError('end active research sessions first')
    if state.get('selection'):
        raise ValueError('selection already frozen')
