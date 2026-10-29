"""Prepare and load trusted executable benchmarks; never execute candidate code.

The host agent writes/adapts the package before registration. Registration validates
local files only: it neither downloads data nor manufactures a replacement dataset.
"""
import copy
import importlib.util
import math
import os
from pathlib import Path
from types import SimpleNamespace

from tools import common as c


_SCHEMA = Path(__file__).parent / 'schemas'


def _schema(name, data):
    c.validate_schema(c.read_json(_SCHEMA / (name + '.json')), data)


def _benchmark(root, data):
    if data['benchmark_file'] == '@anchor':
        raise ValueError('shared benchmark must be resolved from the anchor delivery')
    benchmark = c.read_json(c.contained(root, data['benchmark_file']))
    _schema('benchmark', benchmark)
    if benchmark['task_id'] != data['task_id']:
        raise ValueError('benchmark task mismatch')
    declared = [data['benchmark_file'], *(benchmark[key] for key in
                ('evaluator_file', 'baseline_file', 'tests_file')), *benchmark['support_files']]
    if len({str(c.contained(root, path)) for path in declared}) != len(declared):
        raise ValueError('benchmark files must have distinct declarations')
    return benchmark


def inventory(root, data):
    """Explicit local dependencies, excluding the dataset manifest itself."""
    _schema('dataset', data)
    names = {data['cases_file'], *data['source']['files']}
    for path in (data['generator']['code_file'], data['source']['adapter_file']):
        if path is not None:
            names.add(path)
    if data['benchmark_file'] != '@anchor':
        benchmark = _benchmark(root, data)
        names.add(data['benchmark_file'])
        names.update(benchmark[key] for key in ('evaluator_file', 'baseline_file', 'tests_file'))
        names.update(benchmark['support_files'])
    for path in names:
        c.contained(root, path)
    return names


def _load(path):
    # Support code may be explicitly loaded by this trusted file relative to
    # __file__. No candidate source or user Python path is imported here.
    name = 'anysolvers_benchmark_' + c.sha(path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _score(value):
    if type(value) is not dict or not {'score', 'metrics'} <= set(value) or set(value) - {'score', 'metrics', 'trace'}:
        raise ValueError('benchmark score requires score and metrics; optional trace only')
    if type(value['score']) not in (int, float) or not math.isfinite(value['score']):
        raise ValueError('benchmark score must be finite')
    if type(value['metrics']) is not dict:
        raise ValueError('benchmark metrics must be an object')
    c.digest(value)  # Refuse NaN/infinite or non-JSON nested metrics.
    return value


def _observation(value):
    if type(value) is not dict:
        raise ValueError('episode observation must be a JSON object')
    c.digest(value)


def _episode(env, case, actions, require_done=True):
    state, observation = env.reset(copy.deepcopy(case))
    _observation(observation)
    trace = []
    done = False
    if len(actions) > env.protocol['max_steps']:
        raise ValueError('fixture exceeds episode step limit')
    for number, action in enumerate(actions):
        if done:
            raise ValueError('fixture supplies action after episode termination')
        before = copy.deepcopy(observation)
        state, observation, done = env.step(state, copy.deepcopy(action))
        _observation(observation)
        if type(done) is not bool:
            raise ValueError('episode done must be boolean')
        trace.append({'step': number, 'observation': before, 'action': copy.deepcopy(action),
                      'next_observation': copy.deepcopy(observation), 'done': done})
    if require_done and not done:
        raise ValueError('valid episode fixture must reach termination')
    return _score(env.score_episode(state, trace, copy.deepcopy(case)))


def _must_reject(function, *args, error_contains=None):
    try:
        function(*copy.deepcopy(args))
    except (ValueError, TypeError, KeyError) as exc:
        if error_contains is not None and error_contains not in str(exc):
            raise ValueError('invalid fixture raised unexpected error: ' + str(exc)) from exc
        return
    raise ValueError('benchmark accepted an invalid fixture')


def check_fixtures(env):
    """Exercise trusted rules on explicit tiny fixtures, not candidate/baseline code."""
    fixtures = env.fixtures
    _schema('fixtures', fixtures)
    for case in fixtures['invalid_inputs']:
        wrapped = (type(case) is dict and 'case' in case and
                   set(case) <= {'name', 'case', 'error_contains'})
        _must_reject(env.validate_input, case['case'] if wrapped else case,
                     error_contains=case.get('error_contains') if wrapped else None)
    for fixture in fixtures['valid']:
        case = copy.deepcopy(fixture['case'])
        env.validate_input(case)
        if env.protocol['kind'] == 'function':
            if 'solution' not in fixture or 'actions' in fixture:
                raise ValueError('function fixture requires solution only')
            actual = _score(env.score(copy.deepcopy(fixture['solution']), case))
        else:
            if 'actions' not in fixture or 'solution' in fixture:
                raise ValueError('episode fixture requires actions only')
            actual = _episode(env, case, fixture['actions'])
        if actual != fixture['expected']:
            raise ValueError('benchmark fixture score mismatch')
    if env.protocol['kind'] == 'function':
        if not fixtures.get('invalid_solutions') or fixtures.get('invalid_actions'):
            raise ValueError('function fixtures need invalid_solutions, not invalid_actions')
        for fixture in fixtures['invalid_solutions']:
            env.validate_input(copy.deepcopy(fixture['case']))
            _must_reject(env.score, fixture['solution'], fixture['case'],
                         error_contains=fixture.get('error_contains'))
    else:
        if not fixtures.get('invalid_actions') or fixtures.get('invalid_solutions'):
            raise ValueError('episode fixtures need invalid_actions, not invalid_solutions')
        for fixture in fixtures['invalid_actions']:
            env.validate_input(copy.deepcopy(fixture['case']))
            # Only the environment's own action rejection counts. A fixture that
            # merely exceeds max_steps or acts after done is not an invalid action test.
            actions = fixture['actions']
            if len(actions) > env.protocol['max_steps']:
                raise ValueError('invalid-action fixture exceeds step limit')
            state, observation = env.reset(copy.deepcopy(fixture['case']))
            _observation(observation)
            rejected = False
            for action in actions:
                try:
                    state, observation, done = env.step(state, copy.deepcopy(action))
                except (ValueError, TypeError, KeyError) as exc:
                    if fixture.get('error_contains') is not None and fixture['error_contains'] not in str(exc):
                        raise ValueError('invalid action raised unexpected error: ' + str(exc)) from exc
                    rejected = True
                    break
                _observation(observation)
                if type(done) is not bool or done:
                    raise ValueError('invalid-action fixture terminated without rejecting its action')
            if not rejected:
                raise ValueError('benchmark accepted invalid action fixture')


def load_environment(root, data, files):
    """Load an already registered benchmark; reject changed or unregistered assets."""
    root = Path(root)
    refs = inventory(root, data)
    if data['benchmark_file'] == '@anchor':
        raise ValueError('resolve @anchor before loading an environment')
    for relative in refs:
        if relative not in files or c.sha(c.contained(root, relative)) != files[relative]:
            raise ValueError('benchmark/source file missing or changed: ' + relative)
    benchmark = _benchmark(root, data)
    module = _load(c.contained(root, benchmark['evaluator_file']))
    names = ['validate_input'] + (['score'] if benchmark['protocol']['kind'] == 'function'
                                 else ['reset', 'step', 'score_episode'])
    for name in names:
        if not callable(getattr(module, name, None)):
            raise ValueError('benchmark missing callback: ' + name)
    signature = c.digest({'benchmark_id': benchmark['benchmark_id'], 'task_id': benchmark['task_id'],
        'protocol': benchmark['protocol'],
        'assets': {key: files[benchmark[key]] for key in ('evaluator_file','baseline_file','tests_file')},
        'support': {os.path.relpath(path, start=Path(data['benchmark_file']).parent): files[path]
                    for path in benchmark['support_files']}})
    return SimpleNamespace(**{name: getattr(module, name) for name in names},
        protocol=benchmark['protocol'], benchmark=benchmark, signature=signature,
        baseline_path=c.contained(root, benchmark['baseline_file']),
        baseline_sha=files[benchmark['baseline_file']],
        fixtures=c.read_json(c.contained(root, benchmark['tests_file'])))


def _registered_environment(run, record, records):
    data = record['manifest']
    if data['benchmark_file'] == '@anchor':
        anchors = [r for r in records.values() if r['manifest']['purpose'] == 'anchor']
        if len(anchors) != 1:
            raise ValueError('shared benchmark requires exactly one anchor')
        record = anchors[0]
        data = record['manifest']
    return load_environment(c.delivery_root(run, record), data, record['files'])


def validate_delivery(run, cfg, state, data, add, entry):
    _schema('dataset', data)
    if data['task_id'] != cfg['task']['task_id']:
        raise ValueError('task mismatch')
    caps = entry['capabilities']
    if data['purpose'] not in caps.get('dataset_purposes', []):
        raise ValueError('module does not support this dataset purpose')
    if caps.get('candidate_feedback') and data['purpose'] != 'challenge':
        raise ValueError('feedback-selected data can only be challenge data')
    records = state['deliveries']['dataset']
    if data['purpose'] == 'anchor' and any(r['manifest']['purpose'] == 'anchor' for r in records.values()):
        raise ValueError('anchor is fixed once; use a challenge dataset or a new run')
    if any(parent not in records for parent in data['parents']):
        raise ValueError('unknown dataset parent')
    if data['generator']['plugin_id'] != entry['id']:
        raise ValueError('generator module provenance mismatch')
    files = {path: c.sha(add(path)) for path in sorted(inventory(run, data))}
    if data['benchmark_file'] == '@anchor':
        if data['purpose'] != 'challenge' or Path(run).name == 'initialization':
            raise ValueError('@anchor is only available to a batch challenge')
        anchors = [r for r in records.values() if r['manifest']['purpose'] == 'anchor']
        if len(anchors) != 1 or anchors[0].get('scope') != 'initialization':
            raise ValueError('@anchor requires the shared initialization anchor')
        env = _registered_environment(run, anchors[0], records)
    else:
        env = load_environment(run, data, files)
    check_fixtures(env)
    for prior in records.values():
        if _registered_environment(run, prior, records).signature != env.signature:
            raise ValueError('datasets in one study must use the same benchmark')
    rows = [c.parse_json(line) for line in c.contained(run, data['cases_file']).read_text().splitlines() if line.strip()]
    cap = cfg['budgets']['max_cases_per_dataset']
    if not rows or (cap is not None and len(rows) > cap):
        raise ValueError('dataset size outside run budget')
    ids, inputs = set(), set()
    for row in rows:
        c.validate('case_row', row, cfg)
        env.validate_input(copy.deepcopy(row['input']))
        fingerprint = c.digest(row['input'])
        if row['case_id'] in ids or fingerprint in inputs:
            raise ValueError('duplicate case or input')
        ids.add(row['case_id']); inputs.add(fingerprint)
    source = data['source']
    if any(not source[key].strip() for key in ('reference', 'version', 'description', 'license')):
        raise ValueError('source provenance fields must not be blank')
    if source['kind'] == 'real':
        adapter = _load(c.contained(run, source['adapter_file']))
        if not callable(getattr(adapter, 'load_cases', None)):
            raise ValueError('real-data adapter must expose load_cases(raw_paths)')
        converted = adapter.load_cases([c.contained(run, path) for path in source['files']])
        if not isinstance(converted, list) or c.digest(converted) != c.digest(rows):
            raise ValueError('real cases do not match the pinned raw-data adapter output')
    for prior in records.values():
        manifest = prior['manifest']
        if 'final' in (data['purpose'], manifest['purpose']):
            old = {c.digest(c.parse_json(line)['input']) for line in
                   c.contained(c.delivery_root(run, prior), manifest['cases_file']).read_text().splitlines() if line.strip()}
            if old & inputs:
                raise ValueError('final/development input overlap')
    # Trusted initialization hooks must not silently modify their inputs.
    if any(c.sha(c.contained(run, path)) != expected for path, expected in files.items()):
        raise ValueError('benchmark/source changed during initialization')
