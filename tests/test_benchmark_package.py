"""Dataset initialization owns trusted rules, real source linkage and episodes."""
import json
from pathlib import Path
import tempfile
import unittest

from modules.datasets.stratified_holdout import package as p
from tools import common as c
from tools.module_runtime import read_module


FUNCTION = '''def validate_input(case):
    if type(case) is not dict or set(case) != {'value'} or type(case['value']) is not int:
        raise ValueError('bad input')
def score(solution,case):
    if type(solution) is not int or solution != case['value']:
        raise ValueError('bad solution')
    return {'score':solution,'metrics':{}}
'''
EPISODE = '''def validate_input(case):
    if type(case) is not dict or set(case) != {'target'} or type(case['target']) is not int or case['target'] < 1:
        raise ValueError('bad input')
def reset(case):
    return {'remaining':case['target']}, {'remaining':case['target']}
def step(state,action):
    if action != 'move': raise ValueError('bad action')
    state['remaining'] -= 1
    return state, {'remaining':state['remaining']}, state['remaining'] == 0
def score_episode(state,trace,case):
    if len(trace) != case['target']: raise ValueError('bad trace')
    return {'score':len(trace),'metrics':{'complete':state['remaining']==0}}
'''


class BenchmarkPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = read_module('modules/datasets/stratified_holdout')
        self.cfg = {'task': {'task_id':'tiny'}, 'budgets':{'max_cases_per_dataset':5},
                    'modules':{'datasets':self.entry}}
        self.state = {'deliveries':{'dataset':{}}}
        self.files = {}
        self.make()

    def write(self, path, content):
        target = self.root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(content if isinstance(content,str) else json.dumps(content))
        return target

    def make(self, episode=False):
        benchmark = {'schema_version':1,'benchmark_id':'tiny-env','task_id':'tiny',
            'protocol':{'kind':'episode','max_steps':5,'action_wall_s':.5} if episode else {'kind':'function'},
            'evaluator_file':'datasets/environment/evaluate.py','baseline_file':'datasets/environment/baseline.py',
            'tests_file':'datasets/environment/fixtures.json','support_files':[]}
        self.write('datasets/environment/benchmark.json',benchmark)
        self.write(benchmark['evaluator_file'],EPISODE if episode else FUNCTION)
        self.write(benchmark['baseline_file'],"def solve(observation): return 'move'\n" if episode else "def solve(case): return case['value']\n")
        fixtures = {'schema_version':1,'valid':[{'case':{'target':2},'actions':['move','move'],
                      'expected':{'score':2,'metrics':{'complete':True}}}],
                    'invalid_inputs':[{}], 'invalid_actions':[{'case':{'target':2},'actions':['wrong']}]} if episode else {
                    'schema_version':1,'valid':[{'case':{'value':2},'solution':2,'expected':{'score':2,'metrics':{}}}],
                    'invalid_inputs':[{}], 'invalid_solutions':[{'case':{'value':2},'solution':3}]}
        self.write(benchmark['tests_file'],fixtures)
        self.row = {'case_id':'case-one','input':{'target':2} if episode else {'value':2}}
        self.write('datasets/anchor/cases.jsonl',json.dumps(self.row)+'\n')
        self.data = {'schema_version':1,'dataset_id':'anchor','task_id':'tiny','purpose':'anchor',
            'cases_file':'datasets/anchor/cases.jsonl','benchmark_file':'datasets/environment/benchmark.json',
            'generator':{'plugin_id':self.entry['id'],'code_file':None,'seed':7,'parameters':{}},'parents':[],
            'source':{'kind':'simulation' if episode else 'synthetic','reference':'test','version':'1',
                      'description':'authored fixture','license':'test','files':[],'adapter_file':None}}

    def add(self,path):
        target = c.contained(self.root,path)
        self.files[path] = c.sha(target)
        return target

    def deliver(self):
        p.validate_delivery(self.root,self.cfg,self.state,self.data,self.add,self.entry)
        return p.load_environment(self.root,self.data,self.files)

    def test_function_rules_and_all_asset_hashes(self):
        env = self.deliver()
        self.assertEqual(env.score(2,{'value':2})['score'],2)
        self.assertIn('datasets/environment/baseline.py',self.files)
        self.write('datasets/environment/baseline.py','changed')
        with self.assertRaisesRegex(ValueError,'changed'):
            p.load_environment(self.root,self.data,self.files)

    def test_python_tests_file_is_rejected_with_fixture_guidance_before_import(self):
        from unittest.mock import patch
        self.write('datasets/environment/fixtures.json', 'raise RuntimeError("not JSON")\n')
        with patch.object(p, '_load', side_effect=AssertionError('must reject before importing')):
            with self.assertRaisesRegex(ValueError, 'JSON fixtures object.*not executable Python'):
                self.deliver()

    def test_episode_fixtures_exercise_trusted_dynamics(self):
        self.make(episode=True)
        env = self.deliver()
        self.assertEqual(env.protocol['kind'],'episode')
        broken = c.read_json(self.root/'datasets/environment/fixtures.json')
        broken['valid'][0]['actions'] = ['move']
        self.write('datasets/environment/fixtures.json',broken)
        with self.assertRaisesRegex(ValueError,'termination'):
            self.deliver()

    def test_imported_raw_adapter_linkage(self):
        self.write('datasets/raw/official.json',[{'value':2}])
        self.write('datasets/anchor/adapter.py',
            "import json\ndef load_cases(raw_paths):\n    return [{'case_id':'case-one','input':json.loads(raw_paths[0].read_text())[0]}]\n")
        self.data['source'].update(kind='real',files=['datasets/raw/official.json'],
                                   adapter_file='datasets/anchor/adapter.py')
        self.deliver()
        self.write('datasets/raw/official.json',[{'value':3}])
        with self.assertRaisesRegex(ValueError,'do not match'):
            self.deliver()

    def test_missing_real_source_does_not_synthesize(self):
        self.data['source']['kind']='real'
        with self.assertRaises(Exception):
            self.deliver()
        self.assertFalse((self.root/'datasets/raw').exists())

    def test_different_scoring_environment_rejected(self):
        self.deliver()
        self.state['deliveries']['dataset']['anchor']={'manifest':dict(self.data),'files':dict(self.files)}
        self.data = dict(self.data,dataset_id='final',purpose='final',cases_file='datasets/final/cases.jsonl',
                         benchmark_file='datasets/other/benchmark.json')
        self.write(self.data['cases_file'],json.dumps({'case_id':'case-two','input':{'value':3}})+'\n')
        bench=c.read_json(self.root/'datasets/environment/benchmark.json')
        bench['evaluator_file']='datasets/other/evaluate.py'
        self.write(self.data['benchmark_file'],bench)
        self.write(bench['evaluator_file'],FUNCTION+'\n# new scoring version\n')
        with self.assertRaisesRegex(ValueError,'same benchmark'):
            self.deliver()

    def test_invalid_action_fixture_cannot_pass_by_ending_early(self):
        self.make(episode=True)
        fixtures=c.read_json(self.root/'datasets/environment/fixtures.json')
        fixtures['invalid_actions'][0]={'case':{'target':1},'actions':['move']}
        self.write('datasets/environment/fixtures.json',fixtures)
        with self.assertRaisesRegex(ValueError,'terminated without rejecting'):
            self.deliver()

    def test_inventory_explicit_shared_benchmark_and_traversal_rejection(self):
        refs=p.inventory(self.root,self.data)
        self.assertIn('datasets/environment/fixtures.json',refs)
        self.data['benchmark_file']='@anchor'
        self.assertEqual(p.inventory(self.root,self.data),{'datasets/anchor/cases.jsonl'})
        with self.assertRaisesRegex(ValueError,'only available'):
            self.deliver()
        self.data['source']['files']=['../escape']
        with self.assertRaisesRegex(ValueError,'contained'):
            p.inventory(self.root,self.data)

    def test_input_overlap_remains_rejected_with_shared_environment(self):
        self.deliver()
        self.state['deliveries']['dataset']['anchor']={'manifest':dict(self.data),'files':dict(self.files)}
        self.data=dict(self.data,dataset_id='final',purpose='final')
        with self.assertRaisesRegex(ValueError,'overlap'):
            self.deliver()

    def test_support_signature_binds_paths_to_contents(self):
        benchmark_path = self.root/'datasets/environment/benchmark.json'
        benchmark = c.read_json(benchmark_path)
        paths = ['datasets/environment/speed.json', 'datasets/environment/cost.json']
        benchmark['support_files'] = paths
        self.write('datasets/environment/benchmark.json', benchmark)
        self.write(paths[0], {'value': 2})
        self.write(paths[1], {'value': 3})
        initial = self.deliver().signature
        self.write(paths[0], {'value': 3})
        self.write(paths[1], {'value': 2})
        self.assertNotEqual(self.deliver().signature, initial)

    def test_support_cannot_redeclare_evaluator(self):
        benchmark = c.read_json(self.root/'datasets/environment/benchmark.json')
        benchmark['support_files'] = ['datasets/environment/./evaluate.py']
        self.write('datasets/environment/benchmark.json', benchmark)
        with self.assertRaisesRegex(ValueError, 'distinct declarations'):
            self.deliver()
