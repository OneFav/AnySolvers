"""Pre-launch configuration checks; no model requests, candidates or Docker."""
import argparse
import copy
from pathlib import Path
import unittest

import jsonschema
import yaml

import test_contracts as helpers
from tools import common, execution, run


class ExecutionTests(unittest.TestCase):
    setUp = helpers.ContractTests.setUp
    command = helpers.ContractTests.command
    snapshot = helpers.ContractTests.snapshot
    write_plugin = helpers.ContractTests.write_plugin

    def write_template(self, cfg):
        (self.root/'configs/explicit.yaml').write_text(yaml.safe_dump(cfg))
        return 'configs/explicit.yaml'

    def edit_plugin(self, policy):
        path = self.root/'modules/research/notebook_collaboration/MODULE.md'
        text = path.read_text()
        _, meta, body = text.split('---', 2)
        data = yaml.safe_load(meta)
        data['execution'] = policy
        path.write_text('---\n'+yaml.safe_dump(data)+'---'+body)

    def test_research_main_resolves_only_its_declared_researcher(self):
        _, cfg, _ = self.snapshot()
        plan = execution.resolve(cfg, 'research')
        self.assertIsNone(plan['spawn_options'])
        expected = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'fork_turns': 'none'}
        self.assertEqual(plan['allowed_delegates'], {'researcher': expected})
        delegate = execution.resolve(cfg, 'research', 'researcher')
        self.assertEqual(delegate['spawn_options'], expected)
        self.assertEqual(delegate['allowed_delegates'], {})
        session = self.command('begin-session', actor='author', role='researcher', idea='idea-a')
        self.assertEqual(session['spawn_options'], expected)
        self.assertEqual(session['session_id'], 'author-r001')
        self.assertEqual(session['generation'], 1)
        self.assertEqual(session['idea_id'], 'idea-a')
        self.assertIsNone(session['iteration_id'])

    def test_session_requires_the_declared_researcher_and_an_input(self):
        for kwargs in ({'role':'self','idea':'idea-a'},
                       {'role':'researcher'},
                       {'role':'researcher','idea':'not-registered'}):
            before=self.snapshot()[2]
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.command('begin-session',actor='author',**kwargs)
            self.assertEqual(self.snapshot()[2],before)

    def test_distinct_actors_can_be_active_but_an_actor_cannot_overlap(self):
        first=self.command('begin-session',actor='author-a',role='researcher',idea='idea-a')
        second=self.command('begin-session',actor='author-b',role='researcher',idea='idea-a')
        self.assertEqual({first['session_id'],second['session_id']},
                         {'author-a-r001','author-b-r001'})
        before=self.snapshot()[2]
        with self.assertRaises(ValueError):
            self.command('begin-session',actor='author-a',role='researcher',idea='idea-a')
        self.assertEqual(self.snapshot()[2],before)
        active=[entry['actor'] for entry in before['sessions'] if entry['status']=='active']
        self.assertEqual(set(active),{'author-a','author-b'})

    def test_later_session_cannot_bypass_iteration_with_an_idea(self):
        self.command('begin-session',actor='author',role='researcher',idea='idea-a')
        helpers.checkpoint_session(self, 'author')
        self.command('end-session',actor='author',status='completed',native_id=None)
        before=self.snapshot()[2]
        with self.assertRaisesRegex(ValueError,'iteration'):
            self.command('begin-session',actor='author',role='researcher',idea='idea-a')
        self.assertEqual(self.snapshot()[2],before)

    def test_main_does_not_pretend_to_select_parent_model(self):
        _, cfg, _ = self.snapshot()
        for slot in cfg['modules']:
            with self.subTest(slot=slot):
                self.assertIsNone(execution.resolve(cfg, slot)['spawn_options'])
        with self.assertRaisesRegex(ValueError, 'undeclared subagent role'):
            execution.resolve(cfg, 'visualization', 'reviewer')
        with self.assertRaises(jsonschema.ValidationError):
            common.validate('execution', {'mode':'main', 'profile':'setup', 'delegates':{}})

    def test_only_research_can_declare_internal_delegates(self):
        _, base, _ = self.snapshot()
        for slot in base['modules']:
            cfg = copy.deepcopy(base)
            cfg['modules'][slot]['execution'] = {'mode':'subagent', 'profile':'research', 'delegates':{}}
            with self.subTest(slot=slot, policy='subagent'), self.assertRaises(ValueError):
                execution.validate_execution(cfg)
            if slot != 'research':
                cfg['modules'][slot]['execution'] = {'mode':'main', 'delegates':{'researcher':'research'}}
                with self.subTest(slot=slot, policy='delegates'), self.assertRaises(ValueError):
                    execution.validate_execution(cfg)
        cfg = copy.deepcopy(base)
        cfg['agent']['profiles']['review'] = {'model':'gpt-6.1-terra','reasoning_effort':'medium'}
        cfg['modules']['research']['execution']['delegates'] = {'writer':'review'}
        self.assertEqual(execution.resolve(cfg,'research','writer')['spawn_options'],
                         {'model':'gpt-6.1-terra','reasoning_effort':'medium','fork_turns':'none'})
        self.assertEqual(execution.resolve(cfg,'research','writer')['allowed_delegates'],{})

    def test_missing_execution_or_subagent_profile_is_rejected(self):
        p = common.read_plugin(self.root/'modules/research/notebook_collaboration/MODULE.md')
        meta = {key:value for key,value in p.items() if key not in ('sha','path','execution')}
        with self.assertRaises(jsonschema.ValidationError):
            common.validate('module', meta)
        with self.assertRaises(jsonschema.ValidationError):
            common.validate('execution', {'mode':'subagent', 'delegates':{}})
        with self.assertRaises(jsonschema.ValidationError):
            common.validate('execution', {'mode':'main', 'delegates':{'self':'setup'}})

    def test_profiles_reject_invalid_explicit_model_and_effort(self):
        _, base, _ = self.snapshot()
        for settings in ({'model':None, 'reasoning_effort':'high'},
                         {'model':'gpt-6.1-sol', 'reasoning_effort':None},
                         {'model':'', 'reasoning_effort':'high'},
                         {'model':'gpt-6-astra','reasoning_effort':''},
                         {'model':'gpt-6-astra','reasoning_effort':'inherit'}):
            with self.subTest(settings=settings), self.assertRaises(jsonschema.ValidationError):
                cfg = copy.deepcopy(base)
                cfg['agent']['profiles']['research'] = settings
                execution.validate_execution(cfg)
        for model in ('inherit', 'INHERIT', 'auto', 'default'):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, 'explicit model'):
                cfg = copy.deepcopy(base)
                cfg['agent']['profiles']['research']['model'] = model
                execution.validate_execution(cfg)

    def test_undeclared_profile_and_disabled_native_agents_fail(self):
        _, base, _ = self.snapshot()
        cfg = copy.deepcopy(base)
        cfg['agent']['profiles'].pop('research')
        with self.assertRaisesRegex(ValueError, 'unknown agent profile'):
            execution.validate_execution(cfg)
        base['agent']['native_subagents'] = False
        with self.assertRaisesRegex(ValueError, 'disabled'):
            execution.resolve(base, 'research')
        for entry in base['modules'].values():
            entry['execution'] = {'mode':'main', 'delegates':{}}
        self.assertIsNone(execution.resolve(base, 'research')['spawn_options'])

    def test_initialize_before_task_exists_is_read_only(self):
        cfg = common.read_yaml(self.root/'configs/smoke.yaml')
        cfg['task'] = 'tasks/not-created/task.json'
        path = self.write_template(cfg)
        before = {str(p.relative_to(self.root)):common.sha(p) for p in self.root.rglob('*') if p.is_file()}
        result = run.command(argparse.Namespace(command='launch-spec', config=path,
                             run=None, plugin='task', role='self'))
        self.assertIsNone(result['spawn_options'])
        self.assertEqual(before, {str(p.relative_to(self.root)):common.sha(p)
                                 for p in self.root.rglob('*') if p.is_file()})

    def test_new_run_rejects_legacy_template_and_missing_profiles(self):
        base = common.read_yaml(self.root/'configs/smoke.yaml')
        cfg = copy.deepcopy(base); cfg['schema_version'] = 2
        path = self.write_template(cfg)
        with self.assertRaisesRegex(ValueError, 'schema_version|explicit agent profiles'):
            run.init('contract-study/002', path)
        cfg = copy.deepcopy(base); cfg['agent']['profiles'].pop('research')
        path = self.write_template(cfg)
        with self.assertRaisesRegex(ValueError, 'unknown agent profile'):
            run.init('contract-study/003', path)
        self.assertFalse((self.root/'runs/contract-study/002').exists())
        self.assertFalse((self.root/'runs/contract-study/003').exists())

    def test_preinit_launch_rejects_invalid_budget_even_without_task(self):
        cfg = common.read_yaml(self.root/'configs/smoke.yaml')
        cfg['task'] = 'tasks/not-created/task.json'
        cfg['budgets']['session_wall_s'] = -1
        path = self.write_template(cfg)
        with self.assertRaises(jsonschema.ValidationError):
            run.command(argparse.Namespace(command='launch-spec', config=path,
                                           run=None, plugin='task', role='self'))

    def test_invalid_dynamic_binding_does_not_change_run(self):
        run_dir, _, _ = self.snapshot()
        before = {name:(run_dir/name).read_bytes() for name in ('run.yaml','state.json','events.jsonl')}
        self.edit_plugin({'mode':'main','delegates':{'researcher':'not-configured'}})
        with self.assertRaises(ValueError):
            self.command('bind', slot='research', path='modules/research/notebook_collaboration/MODULE.md')
        self.assertEqual(before, {name:(run_dir/name).read_bytes() for name in before})

    def test_valid_binding_resolves_an_alternative_internal_research_delegate(self):
        self.edit_plugin({'mode':'main','delegates':{'writer':'research'}})
        self.command('bind', slot='research', path='modules/research/notebook_collaboration/MODULE.md')
        _, cfg, _ = self.snapshot()
        self.assertIsNone(execution.resolve(cfg, 'research')['spawn_options'])
        self.assertEqual(execution.resolve(cfg, 'research','writer')['spawn_options']['reasoning_effort'], 'high')
        session=self.command('begin-session',actor='author',role='writer',idea='idea-a')
        self.assertEqual(session['role'],'writer')
        self.assertEqual(session['profile'],'research')

    def test_historical_run_readable_but_cannot_launch_or_mutate(self):
        _, old_cfg, state = self.snapshot()
        old_id = 'synthetic-schema2'
        old_cfg.update(schema_version=2, run_id=old_id,
                       agent={'backend':'current-conversation','model':'inherit','native_subagents':True})
        old_cfg['plugins'] = old_cfg.pop('modules')
        for entry in old_cfg['plugins'].values():
            entry.pop('execution')
        state.pop('configuration')
        state['deliveries'] = {kind: {key: record for key, record in records.items()
                                     if record.get('scope') != 'initialization'}
                               for kind, records in state['deliveries'].items()}
        state['config_sha'] = common.digest(old_cfg)
        history = self.root/'runs'/old_id
        history.mkdir()
        (history/'run.yaml').write_text(yaml.safe_dump(old_cfg))
        common.write_json(history/'state.json', state)
        (history/'events.jsonl').write_text('')
        (self.root/'tools/run.py').write_text('# A different working version.\n')
        before = {str(p.relative_to(history)):common.sha(p) for p in history.rglob('*') if p.is_file()}
        self.run_id = old_id
        checked = self.command('check')
        self.assertTrue(checked['historical_read_only'])
        self.assertIn('tools/run.py', checked['working_file_changes'])
        self.assertTrue(self.command('status')['historical_read_only'])
        self.assertIsNone(self.command('plugin',slot='research')['execution'])
        with self.assertRaisesRegex(ValueError, 'read-only'):
            self.command('begin-session',actor='new-author')
        with self.assertRaisesRegex(ValueError, 'read-only'):
            run.command(argparse.Namespace(command='launch-spec', config=None, run=old_id,
                                           plugin='research',role='self'))
        self.assertEqual(before, {str(p.relative_to(history)):common.sha(p)
                                  for p in history.rglob('*') if p.is_file()})
        self.assertFalse((history/'.mutation.lock').exists())
        self.assertFalse((self.root/'assets').exists())
        (self.root/old_cfg['plugins']['research']['path']).unlink()
        self.assertTrue(self.command('check')['ok'])
        with self.assertRaisesRegex(ValueError, 'module file missing'):
            self.command('plugin', slot='research')


if __name__ == '__main__':
    unittest.main()
