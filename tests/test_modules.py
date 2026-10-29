"""Module substitution and complete orchestration contracts; no model or Docker calls."""
import argparse
import json
import shutil
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import yaml

import test_contracts as helpers
from tools import common, execution, measure, run, sandbox
from tools.module_runtime import read_module, dependency


class ModuleTests(unittest.TestCase):
    setUp = helpers.ContractTests.setUp
    command = helpers.ContractTests.command
    snapshot = helpers.ContractTests.snapshot
    write_candidate = helpers.ContractTests.write_candidate
    deliver_candidate = helpers.ContractTests.deliver_candidate
    write_dataset = helpers.ContractTests.write_dataset
    deliver_dataset = helpers.ContractTests.deliver_dataset

    def variant(self, slot, name, change=None, script=None):
        source = self.root / helpers.method_path(slot)
        directory = self.root / 'modules' / slot / name
        shutil.copytree(source, directory)
        _, meta, body = (directory/'MODULE.md').read_text().split('---', 2)
        meta = yaml.safe_load(meta)
        meta['id'] = slot + '-' + name
        if change:
            change(meta)
        if script:
            (directory/(name+'.py')).write_text(script)
        path = directory/'MODULE.md'
        path.write_text('---\n'+yaml.safe_dump(meta)+'---\n'+body)
        return str(path.relative_to(self.root))

    def ideas(self):
        record = self.snapshot()[2]['deliveries']['ideas']['ideas-a']
        self.assertEqual(record['scope'], 'initialization')
        self.assertEqual(record['manifest']['ideas'][0]['idea_id'], 'idea-a')
        return record

    def test_eight_packages_and_default_model_without_archive(self):
        _,cfg,_=self.snapshot()
        self.assertEqual(set(cfg['modules']),{'task','datasets','ideas','research','evaluation','selections','final','visualization'})
        self.assertNotIn('plugins',cfg)
        for slot, entry in cfg['modules'].items():
            self.assertEqual(entry['path'], helpers.method_path(slot)+'/MODULE.md')
            self.assertEqual(entry['kind'], slot)
            self.assertEqual(entry['params'], entry['defaults'])
        self.assertEqual(common.read_yaml(self.root/'runs'/self.run_id/'run.yaml')['schema_version'], 8)
        for slot in cfg['modules']:
            self.assertIsNone(execution.resolve(cfg,slot)['spawn_options'])
        self.assertEqual(execution.resolve(cfg,'research','researcher')['spawn_options']['model'],'gpt-6.1-sol')
        self.assertFalse((self.root/'assets').exists())
        self.assertFalse((self.root/'contracts').exists())
        self.assertFalse((self.root/'plugins').exists())
        for retired in ('contracts', 'plugins', 'pulgins'):
            self.assertFalse((helpers.PACKAGE_ROOT/retired).exists())

    def test_binding_requires_an_active_complete_method_package(self):
        before = {str(path.relative_to(self.root)): common.sha(path)
                  for path in self.root.rglob('*') if path.is_file()}
        for path in ('modules/datasets', 'modules/datasets/MODULE.md',
                     'spare_modules/datasets/feedback_challenges',
                     'spare_modules/datasets/feedback_challenges/MODULE.md'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read_module(path)
        self.assertEqual(before, {str(path.relative_to(self.root)): common.sha(path)
                                  for path in self.root.rglob('*') if path.is_file()})

    def test_dependency_cannot_cross_into_another_method(self):
        other = self.variant('datasets', 'other-method')
        entry = read_module(other)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            dependency(entry, '../stratified_holdout/package.py')
        self.assertEqual(dependency(entry, 'package.py').parent,
                         self.root/'modules/datasets/other-method')

    def test_module_parameter_override_is_local_and_validated(self):
        path=self.root/'runs'/'contract-study'/'002'/'run.yaml';path.parent.mkdir()
        common.write_json(path,{'schema_version':8,'extends':'configs/smoke.yaml',
                               'modules':{'datasets':{'params':{'seed':91,'count':3}}}})
        run.init('contract-study/002')
        _,cfg,_=common.load_run('contract-study/002')
        self.assertEqual(cfg['modules']['datasets']['params'],{'seed':91,'count':3})
        self.assertEqual(cfg['modules']['research']['params'],{})
        with self.assertRaises(Exception):read_module('modules/datasets/stratified_holdout',{'unknown_parameter':1})

    def test_changed_dependency_is_rejected_without_archive_fallback(self):
        dependency=self.root/'modules/datasets/stratified_holdout/package.py'
        dependency.write_text(dependency.read_text()+'\n# changed\n')
        with self.assertRaisesRegex(ValueError,'module dependency changed'):self.command('check')
        self.command('bind',slot='datasets',path='modules/datasets/stratified_holdout')
        self.assertTrue(self.command('check')['ok'])

    def test_feedback_challenges_package_can_add_challenge_but_cannot_supply_final(self):
        self.deliver_dataset()
        shutil.copytree(self.root/'spare_modules/datasets/feedback_challenges',
                        self.root/'modules/datasets/feedback_challenges')
        self.command('bind', slot='datasets', path='modules/datasets/feedback_challenges')
        self.deliver_dataset('challenge-a','challenge',shift=2)
        manifest,_=self.write_dataset('final-extra','final',shift=3)
        with self.assertRaisesRegex(ValueError,'anchor/final belong to shared initialization'):
            self.command('deliver',kind='dataset',manifest=manifest,module='datasets')
        self.assertEqual(len(self.snapshot()[2]['deliveries']['dataset']),3)

    def test_base_collaboration_declares_researcher_launch_without_model_call(self):
        self.assertTrue(self.snapshot()[1]['modules']['research']['capabilities']['session_experiments'])
        _,cfg,_=self.snapshot()
        self.assertIsNone(execution.resolve(cfg,'research')['spawn_options'])
        self.assertEqual(execution.resolve(cfg,'research','researcher')['spawn_options'],
                         {'model':'gpt-6.1-sol','reasoning_effort':'high','fork_turns':'none'})

    def test_evaluation_binding_reaches_new_handler(self):
        path=self.variant('evaluation','stub',lambda m:m['handlers'].update(evaluate='stub.py'),
            "def evaluate(*args):\n    return {'source':'replacement-evaluator','arguments':list(args[:4])}\n")
        self.command('bind',slot='evaluation',path=path)
        with patch.object(sandbox,'execute') as execute:
            result=measure.evaluate(self.run_id,'candidate','dataset','eval-a')
        self.assertEqual(result['source'],'replacement-evaluator');execute.assert_not_called()
        self.assertEqual(self.snapshot()[2]['evaluations'],{})  # dispatch test, not a real score

    def test_research_cli_uses_method_neutral_name_without_execution(self):
        self.assertTrue((self.root/'tools/research.py').is_file())
        self.assertFalse((self.root/'tools/aar.py').exists())
        result = subprocess.run([sys.executable, '-B', 'tools/research.py', '--help'],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('evaluate', result.stdout)
        self.assertIn('checkpoint', result.stdout)

    def test_measure_cli_import_does_not_load_default_implementation(self):
        result=subprocess.run([sys.executable,'-c',
            "import sys; from tools.measure import main; "
            "assert 'modules.evaluation.queued_sandbox.measure' not in sys.modules; "
            "assert 'modules.evaluation.queued_sandbox.sandbox' not in sys.modules"],
            cwd=self.root,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_measurement_fixes_task_and_evaluation_implementation(self):
        self.deliver_candidate();self.deliver_dataset()
        result={'valid':True,'score':2.0,'solution':{},'metrics':{},'wall_s':0.1,
                'queue_wait_s':0.0,'infrastructure_error':False,'cleanup_confirmed':True}
        with patch.object(sandbox,'execute',return_value=result):
            measure.evaluate(self.run_id,'candidate-a','anchor-a','eval-a')
        directory=self.root/'runs'/self.run_id
        before={str(p.relative_to(directory)):common.sha(p)
                for p in directory.rglob('*') if p.is_file()}
        for slot, reason in (('task', 'task belongs to shared initialization'),
                             ('evaluation', 'fixed after measurement')):
            with self.subTest(slot=slot),self.assertRaisesRegex(ValueError,reason):
                self.command('bind',slot=slot,path=helpers.method_path(slot))
        self.assertEqual(before,{str(p.relative_to(directory)):common.sha(p)
                                 for p in directory.rglob('*') if p.is_file()})

    def test_module_cannot_omit_required_operations(self):
        for slot,operation in (('evaluation','evaluate'),('evaluation','details'),
                               ('selections','context'),('final','select'),('task','prepare')):
            path=self.variant(slot,'missing-'+operation,lambda m:m['handlers'].pop(operation))
            with self.subTest(slot=slot,operation=operation),self.assertRaises(Exception):
                read_module(path)

    def test_measurement_uses_bound_input_schemas(self):
        self.deliver_candidate();self.deliver_dataset()
        run_dir,cfg,state=self.snapshot()
        from modules.evaluation.queued_sandbox.measure import _inputs
        with patch.object(common,'validate',wraps=common.validate) as validate:
            _inputs(run_dir,cfg,state,'candidate-a','anchor-a')
        relevant=[call for call in validate.call_args_list
                  if call.args[0] in ('dataset','case_row','candidate')]
        self.assertEqual({call.args[0] for call in relevant},{'dataset','case_row','candidate'})
        self.assertTrue(all(call.args[2] is cfg for call in relevant))

    def test_selection_binding_reaches_new_context_handler(self):
        path=self.variant('selections','alternate',lambda m:m['handlers'].update(context='alternate.py'),
            "def context(run,cfg,state,actor):\n"
            "    return {'source':'replacement-context','actor':actor,'ideas':[],'candidates':[],"
            "'evaluations':[],'iterations':[]}\n")
        self.command('bind',slot='selections',path=path)
        before=self.snapshot()[2]
        result=self.command('iteration-context',actor='author')
        self.assertEqual(result['source'],'replacement-context')
        self.assertEqual(result['actor'],'author')
        self.assertEqual(self.snapshot()[2],before)

    def test_final_binding_owns_terminal_select_dispatch(self):
        path=self.variant('final','alternate',lambda m:m['handlers'].update(select='alternate.py'),
            "def select(run,cfg,state,candidate):\n"
            "    return {'source':'replacement-final','candidate_id':candidate}\n")
        self.command('bind',slot='final',path=path)
        result=self.command('select',candidate='candidate-a')
        self.assertEqual(result,{'source':'replacement-final','candidate_id':'candidate-a'})
        self.assertIsNone(self.snapshot()[2]['selection'])

    def write_iteration(self, iteration_id, parent=None, evidence_ids=None, history=None, actor="author"):
        relative='research/selections/'+iteration_id+'.json'
        sessions = [s for s in self.snapshot()[2]['sessions'] if s['actor'] == actor and s['status'] == 'active']
        session = sessions[0] if sessions else self.command('begin-session', actor=actor, idea='idea-a')
        document={'schema_version':1,'iteration_id':iteration_id,'author_session':session['session_id'],
            'parent_candidate':parent,'source_idea_ids':['idea-a'],
            'evidence_ids':evidence_ids or [],'history_iteration_ids':history or [],
            'observations':'The available development evidence permits a concrete follow-up.',
            'hypothesis':'A stable alternative ordering may lower weighted tardiness.',
            'algorithm_changes':['Try an alternative deterministic ordering.'],
            'expected_effect':'Lower anchor score without changing feasibility.',
            'test_plan':'Measure on the fixed anchor using the bound evaluation module.',
            'risks':['The alternative may tie or worsen the score.'],
            'rationale':'Test a documented algorithm change against the current candidate.'}
        common.write_json(self.snapshot()[0]/relative,document)
        self.command('deliver',kind='iteration',manifest=relative,module='selections')
        return document

    def test_iteration_is_consumed_once_per_actor(self):
        self.write_iteration('iteration-a')
        helpers.checkpoint_session(self, 'author', 'iteration-a')
        self.command('end-session',actor='author',status='completed',native_id=None)
        resumed = self.command('begin-session',actor='author',role='researcher',iteration='iteration-a')
        self.assertEqual(resumed['session_id'], 'author-r002')
        helpers.checkpoint_session(self, 'author')
        self.command('end-session',actor='author',status='completed',native_id=None)
        before=self.snapshot()[2]
        with self.assertRaises(ValueError):
            self.command('begin-session',actor='author',role='researcher',iteration='iteration-a')
        self.assertEqual(self.snapshot()[2],before)
        other=self.command('begin-session',actor='other',role='researcher',iteration='iteration-a')
        self.assertEqual(other['session_id'],'other-r001')

    def test_incompatible_contract_and_outside_dependency_are_rejected(self):
        original=self.root/'modules/datasets/stratified_holdout/schemas/dataset.json'
        alt=original.with_name('different.json');schema=json.loads(original.read_text())
        schema['description']='incompatible contract version';alt.write_text(json.dumps(schema))
        path=self.variant('datasets','incompatible',lambda m:m['contracts'].update(dataset='schemas/different.json'))
        with self.assertRaisesRegex(ValueError,'incompatible output contract'):
            self.command('bind',slot='datasets',path=path)
        path=self.variant('datasets','outside',lambda m:m['files'].append('../../../tools/run.py'))
        with self.assertRaisesRegex(ValueError,'escapes'):
            self.command('bind',slot='datasets',path=path)

    def test_candidates_can_repeat_within_an_active_session_but_not_after_end(self):
        session=self.command('begin-session',actor='author',role='researcher',idea='idea-a')
        for cid in ('candidate-a', 'candidate-b'):
            manifest,_=self.write_candidate(cid,session_id=session['session_id'])
            self.command('deliver',kind='candidate',manifest=manifest,module='research')
        helpers.checkpoint_session(self, 'author')
        self.command('end-session',actor='author',status='completed',native_id=None)
        late,_=self.write_candidate('candidate-late',session_id=session['session_id'])
        before=self.snapshot()[2]
        with self.assertRaisesRegex(ValueError, 'active research session'):
            self.command('deliver',kind='candidate',manifest=late,module='research')
        self.assertEqual(self.snapshot()[2],before)
        self.assertEqual(set(before['deliveries']['candidate']),{'candidate-a','candidate-b'})

    def test_one_actor_can_iterate_while_another_is_still_researching(self):
        first=self.command('begin-session',actor='author-a',role='researcher',idea='idea-a')
        self.command('begin-session',actor='author-b',role='researcher',idea='idea-a')
        manifest,_=self.write_candidate('candidate-a',session_id=first['session_id'])
        self.command('deliver',kind='candidate',manifest=manifest,module='research')
        success={'valid':True,'score':2.0,'solution':{},'metrics':{},'wall_s':0.1,
                 'queue_wait_s':0.0,'infrastructure_error':False,'cleanup_confirmed':True}
        with patch.object(sandbox,'execute',return_value=success):
            measured=measure.evaluate(self.run_id,'candidate-a','anchor-a','eval-a')
        self.assertTrue(measured['all_valid'])
        self.write_iteration('iteration-a','candidate-a',['eval-a'],actor='author-a')
        helpers.checkpoint_session(self, 'author-a', 'iteration-a')
        self.command('end-session',actor='author-a',status='completed',native_id=None)
        resumed=self.command('begin-session',actor='author-a',role='researcher',iteration='iteration-a')
        self.assertEqual(resumed['session_id'],'author-a-r002')
        self.assertEqual(resumed['generation'],2)
        self.assertEqual(resumed['iteration_id'],'iteration-a')
        active={entry['session_id'] for entry in self.snapshot()[2]['sessions']
                if entry['status']=='active'}
        self.assertEqual(active,{'author-a-r002','author-b-r001'})
        helpers.checkpoint_session(self, 'author-a')
        self.command('end-session',actor='author-a',status='completed',native_id=None)
        before=self.snapshot()[2]
        with self.assertRaises(ValueError):
            self.command('begin-session',actor='author-a',role='researcher',iteration='iteration-a')
        self.assertEqual(self.snapshot()[2],before)
        helpers.checkpoint_session(self, 'author-b')
        self.command('end-session',actor='author-b',status='completed',native_id=None)
        self.assertTrue(self.command('check')['ok'])

    def test_complete_chain_with_mocked_execution_and_existing_code(self):
        self.deliver_dataset();self.deliver_dataset('final-a','final',shift=5);self.ideas()
        initialization = self.root/'runs/contract-study/initialization'
        shared_before = {str(p.relative_to(initialization)): common.sha(p)
                         for p in initialization.rglob('*') if p.is_file()}
        first=self.command('begin-session',actor='author',role='researcher',idea='idea-a')
        self.assertIsNone(first['parent_candidate'])
        self.deliver_candidate()
        success={'valid':True,'score':2.0,'solution':{},'metrics':{},'wall_s':0.1,
                 'queue_wait_s':0.0,'infrastructure_error':False,'cleanup_confirmed':True}
        with patch.object(sandbox,'execute',return_value=success):
            development=measure.evaluate(self.run_id,'candidate-a','anchor-a','eval-a')
            self.assertTrue(development['all_valid'])
            context=self.command('iteration-context',actor='author')
            self.assertIn('evaluations',context)
            second=self.write_iteration('iteration-b','candidate-a',['eval-a'])
            self.assertEqual(second['parent_candidate'],'candidate-a')
            helpers.checkpoint_session(self, 'author', 'iteration-b')
            self.command('end-session',actor='author',status='completed',native_id=None)
            second_session=self.command('begin-session',actor='author',role='researcher',iteration='iteration-b')
            manifest,_=self.write_candidate('candidate-b',
                "def solve(case):\n    return {'order': [j['id'] for j in reversed(case['jobs'])]}\n")
            candidate=common.read_json(self.snapshot()[0]/manifest)
            candidate['parent_id']='candidate-a'
            common.write_json(self.snapshot()[0]/manifest,candidate)
            self.command('deliver',kind='candidate',manifest=manifest,module='research')
            self.assertEqual(candidate['session_id'],second_session['session_id'])
            helpers.checkpoint_session(self, 'author')
            self.command('end-session',actor='author',status='completed',native_id=None)
            with patch.object(sandbox,'execute',return_value={**success,'score':1.0}):
                improvement=measure.evaluate(self.run_id,'candidate-b','anchor-a','eval-b')
                self.assertTrue(improvement['all_valid'])
                self.command('select',candidate='candidate-b')
                final=measure.evaluate(self.run_id,'candidate-b','final-a','eval-final')
        self.assertTrue(final['all_valid'])
        directory=self.root/'runs'/self.run_id
        (directory/'final/report.md').write_text('Contract smoke only; mocked scores, no research claims.')
        common.write_json(directory/'final/report.json',{'schema_version':1,'report_id':'report-a',
            'report_file':'final/report.md','evaluation_ids':['eval-a','eval-b','eval-final']})
        self.command('deliver',kind='report',manifest='final/report.json',module='final')
        (directory/'visualization/figure.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        common.write_json(directory/'visualization/plot.json',{'source':'eval-a','mock':True})
        common.write_json(directory/'visualization/figure.json',{'schema_version':1,'figure_id':'figure-a',
            'image_file':'visualization/figure.svg','data_file':'visualization/plot.json','evaluation_ids':['eval-a']})
        self.command('deliver',kind='figure',manifest='visualization/figure.json',module='visualization')
        self.assertTrue(self.command('check')['ok'])
        _, _, state = self.snapshot()
        self.assertEqual(state['evaluations']['eval-a'], 'research/evaluations/eval-a/summary.json')
        self.assertEqual(state['evaluations']['eval-b'], 'research/evaluations/eval-b/summary.json')
        self.assertEqual(state['selection']['candidate_id'],'candidate-b')
        self.assertEqual(state['evaluations']['eval-final'], 'final/evaluations/eval-final/summary.json')
        self.assertEqual(state['deliveries']['dataset']['final-a']['scope'], 'initialization')
        self.assertEqual(common.read_json(directory/'state.json')['deliveries']['dataset'], {})
        self.assertEqual(shared_before, {str(p.relative_to(initialization)): common.sha(p)
                                        for p in initialization.rglob('*') if p.is_file()})
        self.assertFalse((self.root/'assets').exists())


if __name__=='__main__':unittest.main()
