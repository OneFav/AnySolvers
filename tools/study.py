"""Shared, sealed study inputs; no agents, research or measurement execution."""
import contextlib
import copy
import datetime as dt
import fcntl
import os
from pathlib import Path
import re
import shutil
import tempfile

from tools import common as c
from tools.configuration import prepare
from tools.module_runtime import invoke, output_schema, read_module, verify

SLOTS = ('task', 'datasets', 'ideas')
KINDS = {'dataset': 'datasets', 'ideas': 'ideas'}
TASK_FILES = ('description_file',)
HASH = re.compile(r'^[0-9a-f]{64}$')


def _study_path(study):
    return c.contained(c.ROOT, str(Path('runs') / c.identifier(study)))


@contextlib.contextmanager
def _lock(directory, exclusive=True):
    """Lock the directory itself without adding mutable state to shared inputs."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another study initialization operation is active')
        yield
    finally:
        os.close(descriptor)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError('invalid study initialization structure')


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('invalid initialization timestamp')
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('initialization timestamp requires timezone')
    except ValueError:
        raise ValueError('invalid initialization timestamp') from None


def _ref(ref):
    _keys(ref, ('path', 'sha'))
    if not isinstance(ref['path'], str) or not isinstance(ref['sha'], str) or not HASH.fullmatch(ref['sha']):
        raise ValueError('invalid initialization file reference')
    return c.workspace_file(ref['path'])


def _file(root, relative, prefix):
    if not isinstance(relative, str):
        raise ValueError('initialization file path must be a string')
    path = c.contained(root, relative)
    if Path(relative).parts[0] != prefix or not path.is_file():
        raise ValueError('initialization file must be under ' + prefix + '/')
    return path


def _cfg(manifest):
    return {'task': manifest['task'], 'modules': manifest['modules'],
            'budgets': {'max_cases_per_dataset': manifest['max_cases_per_dataset']}}


def _validate_manifest(root, manifest, study):
    _keys(manifest, ('schema_version', 'study_id', 'status', 'created_at', 'sealed_at',
                     'task', 'modules', 'max_cases_per_dataset', 'deliveries'), ('source',))
    if manifest['schema_version'] != 1 or isinstance(manifest['schema_version'], bool) or manifest['study_id'] != study:
        raise ValueError('study initialization identity mismatch')
    if manifest['status'] not in ('draft', 'sealed'):
        raise ValueError('invalid initialization status')
    _timestamp(manifest['created_at'])
    if manifest['status'] == 'sealed':
        _timestamp(manifest['sealed_at'])
    elif manifest['sealed_at'] is not None:
        raise ValueError('draft initialization cannot have sealed_at')
    cap = manifest['max_cases_per_dataset']
    if cap is not None and (type(cap) is not int or cap < 1):
        raise ValueError('invalid initialization dataset case cap')
    _keys(manifest['modules'], SLOTS)
    for slot, entry in manifest['modules'].items():
        c.validate('module_ref', entry)
        if entry['kind'] != slot:
            raise ValueError('initialization module kind mismatch')
        if manifest['status'] == 'draft':
            verify(entry)
        else:
            # Generators are provenance after sealing; retained contracts still
            # define the shape of shared inputs used by later batch bindings.
            for kind in entry['contracts']:
                output_schema(entry, kind)
    task = manifest['task']
    _keys(task, ('task_id', 'path', 'files', 'objective'))
    c.identifier(task['task_id'])
    _keys(task['files'], (*TASK_FILES, 'manifest'))
    for ref in task['files'].values():
        path = _ref(ref)
        relative = str(path.relative_to(root)) if path.is_relative_to(root) else ''
        _file(root, relative, 'task')
        c.read_pinned(ref, 'initialization task file')
    if task['path'] != task['files']['manifest']['path']:
        raise ValueError('initialization task manifest reference mismatch')
    cfg = _cfg(manifest)
    task_path = c.workspace_file(task['path'])
    task_data = c.read_json(task_path)
    c.validate_schema(output_schema(manifest['modules']['task'], 'task'), task_data)
    expected_files = {key: c.file_ref(c.contained(task_path.parent, task_data[key]))
                      for key in TASK_FILES}
    expected_files['manifest'] = c.file_ref(task_path)
    expected_task = {'task_id': task_data['task_id'], 'path': task['path'],
                     'files': expected_files, 'objective': task_data['objective']}
    if expected_task != task:
        raise ValueError('initialization task metadata differs from copied package')
    if manifest['status'] == 'draft':
        proposed = dict(cfg, task=task['path'])
        if invoke(proposed, 'task', 'prepare', proposed) != task:
            raise ValueError('initialization task preparation differs from copied package')
    _keys(manifest['deliveries'], ('dataset', 'ideas'))
    for kind, records in manifest['deliveries'].items():
        if not isinstance(records, dict):
            raise ValueError('invalid initialization delivery registry')
        entry = manifest['modules'][KINDS[kind]]
        for key, record in records.items():
            c.identifier(key)
            _keys(record, ('manifest', 'manifest_path', 'files', 'module_sha', 'module_id', 'delivered_at'))
            _timestamp(record['delivered_at'])
            if record['module_sha'] != entry['sha'] or record['module_id'] != entry['id']:
                raise ValueError('initialization delivery module provenance mismatch')
            if not isinstance(record['files'], dict) or record['manifest_path'] not in record['files']:
                raise ValueError('initialization delivery manifest is not sealed')
            for relative, expected in record['files'].items():
                path = _file(root, relative, KINDS[kind])
                if not isinstance(expected, str) or not HASH.fullmatch(expected) or c.sha(path) != expected:
                    raise ValueError('sealed initialization delivery changed: ' + relative)
            actual = c.read_json(_file(root, record['manifest_path'], KINDS[kind]))
            if actual != record['manifest']:
                raise ValueError('initialization delivery metadata differs from sealed manifest')
            c.validate_schema(output_schema(entry, kind), actual)
            if actual.get(kind + '_id', Path(record['manifest_path']).stem) != key:
                raise ValueError('initialization delivery ID mismatch')
            if kind == 'dataset':
                expected_files = {record['manifest_path'], actual['cases_file']}
                if actual['generator']['code_file']:
                    expected_files.add(actual['generator']['code_file'])
                if actual.get('benchmark_file'):
                    benchmark_file = actual['benchmark_file']
                    if benchmark_file == '@anchor':
                        raise ValueError('shared initialization cannot reference @anchor')
                    expected_files.add(benchmark_file)
                    benchmark = c.read_json(_file(root, benchmark_file, 'datasets'))
                    c.validate_schema(output_schema(entry, 'benchmark'), benchmark)
                    expected_files.update(benchmark[k] for k in ('evaluator_file', 'baseline_file', 'tests_file'))
                    expected_files.update(benchmark['support_files'])
                    expected_files.update(actual['source']['files'])
                    if actual['source']['adapter_file']:
                        expected_files.add(actual['source']['adapter_file'])
                if set(record['files']) != expected_files:
                    raise ValueError('dataset file registry differs from manifest')
            elif set(record['files']) != {record['manifest_path']}:
                raise ValueError('ideas delivery has unexpected files')
    if manifest['status'] == 'sealed':
        _ready(manifest)
    if 'source' in manifest:
        _ref(manifest['source'])  # Provenance only; later source edits are permitted.


def _load(study, require_sealed=False):
    directory = _study_path(study)
    if (directory / 'run.yaml').exists() or (directory / 'run.yaml').is_symlink():
        raise ValueError('legacy run path cannot be used as a study')
    root = c.contained(directory, 'initialization')
    manifest = c.read_json(c.contained(root, 'manifest.json'))
    _validate_manifest(root, manifest, study)
    if require_sealed and manifest['status'] != 'sealed':
        raise ValueError('study initialization must be sealed before creating a batch')
    return root, manifest


def load(study, require_sealed=False):
    """Read and verify shared inputs, without reading their original task library."""
    with _lock(_study_path(study), exclusive=False):
        return _load(study, require_sealed)


def _adopt_prepared(study, directory, config_path):
    """Register a task prepared directly in initialization, without duplicating it."""
    root = c.contained(directory, 'initialization')
    task_root = c.contained(root, 'task')
    if (set(p.name for p in directory.iterdir()) != {'initialization'} or
            not root.is_dir() or set(p.name for p in root.iterdir()) != {'task'} or
            not task_root.is_dir()):
        raise ValueError('study path already exists; only an unregistered initialization/task is allowed')
    cfg = prepare(config_path)
    cfg['modules'] = {slot: read_module(cfg['modules'][slot]['use'], cfg['modules'][slot].get('params'))
                      for slot in SLOTS}
    task = invoke(cfg, 'task', 'prepare', cfg)
    expected = {c.workspace_file(ref['path']) for ref in task['files'].values()}
    if (len(expected) != 2 or any(not p.is_relative_to(task_root) for p in expected)
            or {p for p in task_root.rglob('*') if not p.is_dir()} != expected):
        raise ValueError('prepared task must contain exactly its two referenced files')
    manifest = {'schema_version': 1, 'study_id': study, 'status': 'draft',
                'created_at': c.utc(), 'sealed_at': None, 'task': task,
                'modules': cfg['modules'], 'max_cases_per_dataset': cfg['budgets']['max_cases_per_dataset'],
                'deliveries': {'dataset': {}, 'ideas': {}},
                'source': c.file_ref(c.workspace_file(config_path))}
    _validate_manifest(root, manifest, study)
    for name in ('datasets', 'ideas'):
        (root / name).mkdir()
    c.write_json(root / 'manifest.json', manifest)
    return {'study_id': study, 'status': 'draft', 'initialization': str(root.relative_to(c.ROOT))}


def create(study, config_path):
    """Register an in-place task, or copy two definition files once."""
    directory = _study_path(study)
    parent = directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    with _lock(parent):
        if directory.exists():
            with _lock(directory):
                return _adopt_prepared(study, directory, config_path)
        cfg = prepare(config_path)
        cfg['modules'] = {slot: read_module(cfg['modules'][slot]['use'], cfg['modules'][slot].get('params'))
                          for slot in SLOTS}
        original = invoke(cfg, 'task', 'prepare', cfg)
        source = c.file_ref(c.workspace_file(config_path))
        task_path = c.workspace_file(original['path'])
        task_data = c.read_json(task_path)
        # Copy only the manifest and its description, never its parent tree.
        files = {task_path.name: task_path}
        sources = {task_path}
        for key in TASK_FILES:
            relative = task_data[key]
            path = c.contained(task_path.parent, relative)
            if path in sources or not path.is_file():
                raise ValueError('task package must reference two distinct regular files')
            files[relative] = path
            sources.add(path)
        directory.mkdir()
        try:
            with _lock(directory):
                staging = Path(tempfile.mkdtemp(prefix='.initialization-', dir=directory))
                try:
                    for relative, path in files.items():
                        target = c.contained(staging / 'task', relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(path.read_bytes())
                    for name in ('datasets', 'ideas'):
                        (staging / name).mkdir()
                    root = directory / 'initialization'
                    staging.replace(root)
                    copied_cfg = dict(cfg, task=str((root / 'task' / task_path.name).relative_to(c.ROOT)))
                    task = invoke(copied_cfg, 'task', 'prepare', copied_cfg)
                    # Detect a concurrent source edit during copying without pinning it forever.
                    if any(c.sha(files[task_path.name if key == 'manifest' else task_data[key]]) != ref['sha']
                           for key, ref in original['files'].items()):
                        raise ValueError('source task changed during initialization')
                    if any(task['files'][key]['sha'] != ref['sha'] for key, ref in original['files'].items()):
                        raise ValueError('copied task differs from prepared source')
                    manifest = {'schema_version': 1, 'study_id': study, 'status': 'draft',
                                'created_at': c.utc(), 'sealed_at': None, 'task': task,
                                'modules': cfg['modules'], 'max_cases_per_dataset': cfg['budgets']['max_cases_per_dataset'],
                                'deliveries': {'dataset': {}, 'ideas': {}}, 'source': source}
                    _validate_manifest(root, manifest, study)
                    c.write_json(root / 'manifest.json', manifest)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
        except Exception:
            shutil.rmtree(directory)
            raise
    return {'study_id': study, 'status': 'draft',
            'initialization': str((directory / 'initialization').relative_to(c.ROOT))}


def _validate_delivery(root, manifest, kind, relative, records):
    slot = KINDS[kind]
    entry = manifest['modules'][slot]
    if kind not in entry['produces']:
        raise ValueError('initialization module does not declare output: ' + kind)
    data = c.read_json(_file(root, relative, slot))
    cfg = _cfg(manifest)
    c.validate(kind, data, cfg)
    files = {}
    def add(path):
        result = _file(root, path, slot)
        files[path] = c.sha(result)
        return result
    add(relative)
    key = c.identifier(data.get(kind + '_id', Path(relative).stem))
    if key in records[kind]:
        raise ValueError('initialization delivery ID already exists; create a new version')
    if 'validate_delivery' in entry['handlers']:
        state = {'deliveries': records, 'selection': None}
        invoke(cfg, slot, 'validate_delivery', root, cfg, state, data, add, entry)
    # Recheck all files after validation before sealing their bytes in metadata.
    if c.read_json(_file(root, relative, slot)) != data or any(
            c.sha(_file(root, name, slot)) != expected for name, expected in files.items()):
        raise ValueError('initialization delivery changed during validation')
    return key, {'manifest': data, 'manifest_path': relative, 'files': files,
                 'module_sha': entry['sha'], 'module_id': entry['id'], 'delivered_at': c.utc()}


def deliver(study, kind, relative, module=None):
    """Validate and register a dataset or ideas file inside a draft initialization."""
    if kind not in KINDS or (module is not None and module != KINDS[kind]):
        raise ValueError('study deliveries require the bound datasets or ideas module')
    with _lock(_study_path(study)):
        root, manifest = _load(study)
        if manifest['status'] != 'draft':
            raise ValueError('sealed study initialization is immutable')
        key, record = _validate_delivery(root, manifest, kind, relative, manifest['deliveries'])
        manifest['deliveries'][kind][key] = record
        _validate_manifest(root, manifest, study)
        c.write_json(root / 'manifest.json', manifest)
        return {'study_id': study, 'kind': kind, 'id': key, 'checked_files': len(record['files'])}


def _ready(manifest):
    datasets = manifest['deliveries']['dataset']
    if sum(r['manifest']['purpose'] == 'anchor' for r in datasets.values()) != 1:
        raise ValueError('study initialization requires exactly one anchor dataset')
    if not manifest['deliveries']['ideas']:
        raise ValueError('study initialization requires nonempty ideas')


def seal(study):
    """Freeze an initialization with one anchor and at least one ideas package."""
    with _lock(_study_path(study)):
        root, manifest = _load(study)
        if manifest['status'] != 'draft':
            raise ValueError('sealed study initialization is immutable')
        _ready(manifest)
        # Replay module validators against earlier deliveries, including final overlap.
        records = {'dataset': {}, 'ideas': {}}
        for kind in records:
            for key, record in manifest['deliveries'][kind].items():
                checked_key, checked = _validate_delivery(root, manifest, kind, record['manifest_path'], records)
                if checked_key != key or checked['files'] != record['files']:
                    raise ValueError('initialization changed while sealing')
                records[kind][key] = record
        manifest.update(status='sealed', sealed_at=c.utc())
        _validate_manifest(root, manifest, study)
        c.write_json(root / 'manifest.json', manifest)
        return {'study_id': study, 'status': 'sealed', 'sealed_at': manifest['sealed_at']}


def shared_records(manifest):
    """Return disposable references for batch runtime overlays; never rewrite state."""
    records = copy.deepcopy(manifest['deliveries'])
    for deliveries in records.values():
        for record in deliveries.values():
            record['scope'] = 'initialization'
    return records


def check(study):
    """Return identity, status and counts without exposing final data or manifests."""
    root, manifest = load(study)
    return {'ok': True, 'study_id': study, 'status': manifest['status'],
            'initialization': str(root.relative_to(c.ROOT)), 'task_id': manifest['task']['task_id'],
            'created_at': manifest['created_at'], 'sealed_at': manifest['sealed_at'],
            'deliveries': {kind: len(records) for kind, records in manifest['deliveries'].items()},
            'checked_files': len(manifest['task']['files']) +
                sum(len(record['files']) for records in manifest['deliveries'].values() for record in records.values())}
