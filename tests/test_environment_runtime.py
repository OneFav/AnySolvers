"""Full initialization -> sealed batch -> runtime measurement integration.

The CSV is hand-authored test input, not empirical research data. Its source.kind
is 'real' solely to exercise the provided-data import route. Default tests mock
only sandbox execution; opt-in tests run the exact same packages in Docker.
No model sessions, stored repository runs, network downloads, or formal research.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools import common as c, measure, run, study
from modules.evaluation.queued_sandbox import sandbox


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_EVALUATOR = '''def validate_input(case):
    if type(case) is not dict or set(case) != {'values'} or type(case['values']) is not list:
        raise ValueError('values required')
    if not case['values'] or any(type(v) is not int for v in case['values']):
        raise ValueError('nonempty integer values required')
def score(solution, case):
    validate_input(case)
    if type(solution) is not int or solution not in case['values']:
        raise ValueError('select one value')
    return {'score': solution, 'metrics': {'selected_value': solution}}
'''
FUNCTION_BASELINE = '''def solve(case):
    return max(case['values'])
'''
CSV_ADAPTER = '''import csv
def load_cases(raw_paths):
    rows = []
    for path in raw_paths:
        with path.open(newline='') as stream:
            for row in csv.DictReader(stream):
                rows.append({'case_id': row['case_id'], 'input': {'values': [int(row['a']), int(row['b'])]}})
    return rows
'''
EPISODE_EVALUATOR = '''def validate_input(case):
    if type(case) is not dict or set(case) != {'release_time', 'target', 'horizon', 'future_order'}:
        raise ValueError('scenario fields required')
    if any(type(case[k]) is not int or case[k] < 1 for k in ('release_time', 'target', 'horizon')):
        raise ValueError('positive integer parameters required')
    if type(case['future_order']) is not str or not case['future_order']:
        raise ValueError('private future order required')
    if case['release_time'] + case['target'] > case['horizon']:
        raise ValueError('fixture horizon cannot serve this order')
def observe(state):
    orders = []
    if state['time'] >= state['release_time']:
        orders = [{'id': state['future_order'], 'target': state['target']}]
    return {'time': state['time'], 'position': state['position'], 'orders': orders}
def reset(case):
    validate_input(case)
    state = dict(case, time=0, position=0)
    return state, observe(state)
def step(state, action):
    if type(action) is not dict or set(action) != {'move', 'sequence'}:
        raise ValueError('move and sequence required')
    if type(action['move']) is not int or action['move'] not in (0, 1):
        raise ValueError('invalid grid movement')
    if type(action['sequence']) is not int or action['sequence'] != state['time'] + 1:
        raise ValueError('persistent policy memory lost')
    if state['time'] < state['release_time'] and action['move']:
        raise ValueError('no movement before an order is observed')
    state = dict(state, time=state['time'] + 1, position=state['position'] + action['move'])
    if state['position'] > state['target']:
        raise ValueError('outside target cell')
    return state, observe(state), state['time'] == state['horizon']
def score_episode(state, trace, case):
    if state['time'] != case['horizon'] or len(trace) != case['horizon']:
        raise ValueError('incomplete episode')
    served = int(state['position'] == case['target'])
    return {'score': served, 'metrics': {'served': served, 'ticks': state['time']}}
'''
EPISODE_BASELINE = '''counter = 0
def solve(observation):
    global counter
    assert set(observation) == {'time', 'position', 'orders'}, 'private scenario was exposed'
    counter += 1
    assert counter == observation['time'] + 1, 'process must persist within each episode'
    move = int(bool(observation['orders']) and observation['position'] < observation['orders'][0]['target'])
    return {'move': move, 'sequence': counter}
'''


class RuntimeFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='anysolvers-runtime-integration-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('configs', 'modules', 'tools'):
            shutil.copytree(PACKAGE_ROOT / name, self.root / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for module in (c, run):
            patcher = patch.object(module, 'ROOT', self.root)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding='utf-8')
        else:
            c.write_json(path, content)
        return path

    def initialize(self, episode=False):
        task_id = 'future-grid' if episode else 'csv-choice'
        study_id = 'runtime-episode' if episode else 'runtime-import'
        task_relative = 'test-inputs/' + task_id + '/task.json'
        self.write(self.root, task_relative, {
            'schema_version': 2, 'task_id': task_id, 'version': '1.0.0', 'description_file': 'task.md',
            'objective': {'name': 'served_orders' if episode else 'selected_value',
                          'direction': 'maximize', 'aggregation': 'mean_if_all_valid'}})
        self.write(self.root, 'test-inputs/' + task_id + '/task.md',
            '# Test-only problem definition\n' + ('Serve released orders; future orders are private.\n'
                                                  if episode else 'Select one value from each provided row.\n'))
        cfg = c.read_yaml(self.root / 'configs/smoke.yaml')
        cfg['task'] = task_relative
        cfg['final']['authorized'] = False
        self.write(self.root, 'configs/runtime-test.yaml', yaml.safe_dump(cfg))
        study.create(study_id, 'configs/runtime-test.yaml')
        initialization, manifest = study.load(study_id)
        self.assertEqual({p.name for p in (initialization / 'task').iterdir()}, {'task.json', 'task.md'})
        self.assertEqual(list((initialization / 'datasets').iterdir()), [])

        benchdir = 'datasets/environment/'
        benchmark = {'schema_version': 1, 'benchmark_id': task_id + '-environment', 'task_id': task_id,
                     'protocol': {'kind': 'episode', 'max_steps': 6, 'action_wall_s': 3} if episode else {'kind': 'function'},
                     'evaluator_file': benchdir + 'evaluate.py', 'baseline_file': benchdir + 'baseline.py',
                     'tests_file': benchdir + 'fixtures.json', 'support_files': []}
        self.write(initialization, benchdir + 'benchmark.json', benchmark)
        self.write(initialization, benchmark['evaluator_file'], EPISODE_EVALUATOR if episode else FUNCTION_EVALUATOR)
        self.write(initialization, benchmark['baseline_file'], EPISODE_BASELINE if episode else FUNCTION_BASELINE)
        if episode:
            rows = [
                {'case_id': 'later-order', 'input': {'release_time': 2, 'target': 3, 'horizon': 5,
                                                    'future_order': 'PRIVATE_FUTURE_LATE'}},
                {'case_id': 'earlier-order', 'input': {'release_time': 1, 'target': 2, 'horizon': 3,
                                                      'future_order': 'PRIVATE_FUTURE_EARLY'}}]
            actions = [{'move': int(t >= 2), 'sequence': t + 1} for t in range(5)]
            fixtures = {'schema_version': 1, 'valid': [{'case': rows[0]['input'], 'actions': actions,
                'expected': {'score': 1, 'metrics': {'served': 1, 'ticks': 5}}}], 'invalid_inputs': [{}],
                'invalid_actions': [{'case': rows[0]['input'], 'actions': [{'move': 2, 'sequence': 1}]}]}
            source = {'kind': 'simulation', 'reference': 'hand-authored future-release grid fixture',
                      'version': '1', 'description': 'Two deterministic simulation test scenarios',
                      'license': 'Local test fixture', 'files': [], 'adapter_file': None}
        else:
            self.write(initialization, 'datasets/raw/import.csv', 'case_id,a,b\nimport-z,2,5\nimport-a,7,3\n')
            self.write(initialization, 'datasets/raw/adapter.py', CSV_ADAPTER)
            rows = [{'case_id': 'import-z', 'input': {'values': [2, 5]}},
                    {'case_id': 'import-a', 'input': {'values': [7, 3]}}]
            fixtures = {'schema_version': 1, 'valid': [{'case': {'values': [2, 5]}, 'solution': 5,
                'expected': {'score': 5, 'metrics': {'selected_value': 5}}}], 'invalid_inputs': [{'values': []}],
                'invalid_solutions': [{'case': {'values': [2, 5]}, 'solution': 99}]}
            source = {'kind': 'real', 'reference': 'hand-authored import-fixture; not empirical data',
                      'version': '1', 'description': 'Tests provided CSV import only; no real-world claim',
                      'license': 'Local test fixture', 'files': ['datasets/raw/import.csv'],
                      'adapter_file': 'datasets/raw/adapter.py'}
        self.write(initialization, benchmark['tests_file'], fixtures)
        self.write(initialization, 'datasets/anchor/cases.jsonl', ''.join(json.dumps(row) + '\n' for row in rows))
        self.write(initialization, 'datasets/anchor/manifest.json', {
            'schema_version': 1, 'dataset_id': 'anchor', 'task_id': task_id, 'purpose': 'anchor',
            'cases_file': 'datasets/anchor/cases.jsonl', 'benchmark_file': benchdir + 'benchmark.json',
            'source': source, 'parents': [],
            'generator': {'plugin_id': manifest['modules']['datasets']['id'], 'code_file': None,
                          'seed': None, 'parameters': {'method': 'explicit test input'}}})
        study.deliver(study_id, 'dataset', 'datasets/anchor/manifest.json', 'datasets')
        self.write(initialization, 'ideas/placeholder.json', {'schema_version': 1, 'ideas': [{
            'idea_id': 'unused-placeholder', 'hypothesis': 'No research is started in this integration test.',
            'mechanism': 'Evaluate the declared baseline only.', 'first_change': 'None; fixture only.',
            'risks': 'No algorithm quality or empirical claim.', 'source_refs': []}]})
        study.deliver(study_id, 'ideas', 'ideas/placeholder.json', 'ideas')
        study.seal(study_id)
        run_id = study_id + '/001'
        run.init(run_id, 'configs/runtime-test.yaml')
        directory, cfg, state = c.load_run(run_id)
        record = state['deliveries']['dataset']['anchor']
        self.assertEqual(record['scope'], 'initialization')
        self.assertFalse((directory / 'datasets').exists())
        self.assertEqual(state['sessions'], [])
        self.assertIn(benchmark['baseline_file'], record['files'])
        if not episode:
            self.assertIn('datasets/raw/import.csv', record['files'])
            self.assertIn('datasets/raw/adapter.py', record['files'])
        return run_id, initialization, rows

    def evaluate_and_check_cache(self, run_id, expected):
        summary = measure.evaluate(run_id, 'baseline', 'anchor', 'baseline-check')
        self.assertTrue(summary['all_valid'], summary)
        self.assertEqual(summary['score'], expected)
        self.assertEqual(summary['valid_cases'], 2)
        self.assertTrue(summary['cleanup_confirmed'])
        with patch.object(sandbox, 'execute', side_effect=AssertionError('cached execution repeated')), \
             patch.object(sandbox, 'execute_episode', side_effect=AssertionError('cached episode repeated')):
            cached = measure.evaluate(run_id, 'baseline', 'anchor', 'baseline-check')
            alias = measure.evaluate(run_id, 'baseline', 'anchor', 'same-request-new-id')
        self.assertTrue(cached['cached'])
        self.assertTrue(alias['cached'])
        self.assertEqual(alias['evaluation_id'], 'baseline-check')
        self.assertEqual(len(c.load_run(run_id)[2]['evaluations']), 1)
        records = measure.details(run_id, 'baseline-check')['records']
        self.assertEqual([r['type'] for r in records], ['attempt', 'result', 'attempt', 'result'])
        return [r for r in records if r['type'] == 'result']

    def assert_episode_visibility(self, records, rows):
        cases = {row['case_id']: row['input'] for row in rows}
        for record in records:
            case = cases[record['case_id']]
            result = record['result']
            trace = result['trace']
            self.assertEqual([t['action']['sequence'] for t in trace], list(range(1, case['horizon'] + 1)))
            self.assertEqual(result['score'], 1)
            self.assertTrue(trace[-1]['done'])
            for t in trace:
                observation = t['observation']
                self.assertEqual(set(observation), {'time', 'position', 'orders'})
                if observation['time'] < case['release_time']:
                    self.assertEqual(observation['orders'], [])
                    self.assertNotIn(case['future_order'], json.dumps(observation))
                else:
                    self.assertEqual(observation['orders'][0]['id'], case['future_order'])


class EnvironmentRuntimeTests(RuntimeFixture, unittest.TestCase):
    def test_provided_csv_initializes_runtime_and_baseline_route(self):
        run_id, initialization, rows = self.initialize()
        self.assertEqual([r['case_id'] for r in rows], ['import-z', 'import-a'])
        self.assertEqual([r['input']['values'] for r in rows], [[2, 5], [7, 3]])

        def execute(source, case, image, limits, scorer, lock, **kwargs):
            self.assertEqual(source, FUNCTION_BASELINE)
            # Known fixture solution; do not import or run submitted baseline code.
            solution = 5 if case['values'] == [2, 5] else 7
            result = scorer(solution, case)
            return dict(result, valid=True, solution=solution, infrastructure_error=False,
                        cleanup_confirmed=True, wall_s=.01, queue_wait_s=0.0)

        with patch.object(sandbox, 'execute', side_effect=execute) as execute_call, \
             patch.object(sandbox, 'execute_episode', side_effect=AssertionError('wrong protocol')):
            records = self.evaluate_and_check_cache(run_id, 6)
        self.assertEqual(execute_call.call_count, 2)
        self.assertEqual([r['result']['score'] for r in records], [5, 7])

    def test_episode_initializes_persistent_observation_only_runtime_route(self):
        run_id, _, rows = self.initialize(episode=True)

        def execute(source, case, image, limits, environment, lock, **kwargs):
            self.assertEqual(source, EPISODE_BASELINE)
            self.assertEqual(kwargs['max_steps'], 6)
            self.assertEqual(kwargs['action_wall_s'], 3)
            state, observation = environment.reset(copy.deepcopy(case))
            trace = []
            for t in range(case['horizon']):
                # Fixed fixture actions only; submitted source is never run on host.
                action = {'move': int(t >= case['release_time']), 'sequence': t + 1}
                before = copy.deepcopy(observation)
                state, observation, done = environment.step(state, action)
                trace.append({'step': t, 'observation': before, 'action': action,
                              'next_observation': copy.deepcopy(observation), 'done': done})
            score = environment.score_episode(state, trace, case)
            return dict(score, valid=True, solution=None, trace=trace, infrastructure_error=False,
                        cleanup_confirmed=True, wall_s=.01, queue_wait_s=0.0)

        with patch.object(sandbox, 'execute_episode', side_effect=execute) as execute_call, \
             patch.object(sandbox, 'execute', side_effect=AssertionError('wrong protocol')):
            records = self.evaluate_and_check_cache(run_id, 1)
        self.assertEqual(execute_call.call_count, 2)
        self.assert_episode_visibility(records, rows)


@unittest.skipUnless(os.environ.get('ANYSOLVERS_V2_DOCKER_TEST') == '1', 'real Docker integration requires opt-in')
class EnvironmentDockerIntegrationTests(RuntimeFixture, unittest.TestCase):
    def test_csv_package_through_real_sandbox(self):
        run_id, _, _ = self.initialize()
        records = self.evaluate_and_check_cache(run_id, 6)
        self.assertEqual([r['result']['score'] for r in records], [5, 7])
        self.assertTrue(all(r['result']['isolation_verified'] for r in records))

    def test_episode_package_through_real_sandbox(self):
        run_id, _, rows = self.initialize(episode=True)
        records = self.evaluate_and_check_cache(run_id, 1)
        self.assert_episode_visibility(records, rows)
        self.assertTrue(all(r['result']['isolation_verified'] for r in records))


if __name__ == '__main__':
    unittest.main()
