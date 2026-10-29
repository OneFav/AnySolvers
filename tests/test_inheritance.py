"""Sparse run configuration checks; never launch agents, candidates or Docker."""
import argparse
import copy
import fcntl
import os
import unittest

import jsonschema
import yaml

import test_contracts as helpers
from tools import common, execution, run


class InheritanceTests(unittest.TestCase):
    # Borrow the disposable fixture without inheriting its unrelated test cases.
    setUp = helpers.ContractTests.setUp
    command = helpers.ContractTests.command
    snapshot = helpers.ContractTests.snapshot
    write_plugin = helpers.ContractTests.write_plugin

    def init_batch(self, reference, config_path=None, **flags):
        helpers.ensure_study(self.root, reference.split('/')[0],
                             config_path or 'configs/smoke.yaml')
        return run.init(reference, config_path, **flags)

    def draft(self, run_id, **overrides):
        study_id = run_id.split('/')[0]
        helpers.ensure_study(self.root, study_id)
        path = self.root / 'runs' / run_id / 'run.yaml'
        path.parent.mkdir(parents=True)
        document = {'schema_version': 8, 'extends': 'configs/smoke.yaml',
                    'initialization': f'runs/{study_id}/initialization/manifest.json',
                    'budgets': {'researchers': 3, 'rounds_per_researcher': 4}}
        document.update(overrides)
        path.write_text(yaml.safe_dump(document), encoding='utf-8')
        return path

    def files(self):
        return {str(path.relative_to(self.root)): common.sha(path)
                for path in self.root.rglob('*') if path.is_file()}

    def launch(self, cfg, slot):
        return execution.resolve(cfg, slot, 'researcher')['spawn_options']

    def test_default_profile_resolves_researcher_to_sol_high(self):
        _, cfg, _ = self.snapshot()
        for slot in ('research',):
            with self.subTest(slot=slot):
                self.assertEqual(self.launch(cfg, slot), {
                    'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'fork_turns': 'none'})
        self.assertNotIn('defaults', cfg['agent'])

    def test_init_keeps_run_sparse_and_inherits_other_budgets(self):
        base = common.read_yaml(self.root / 'configs/smoke.yaml')
        self.init_batch('sparse-run/001', 'configs/smoke.yaml', researchers=5, rounds=6)
        document = common.read_yaml(self.root / 'runs/sparse-run/001/run.yaml')
        self.assertEqual(document['schema_version'], 8)
        self.assertEqual(document['extends'], 'configs/smoke.yaml')
        self.assertEqual(document['budgets'], {'researchers': 5, 'rounds_per_researcher': 6})
        for inherited in ('task', 'agent', 'compute', 'final'):
            self.assertNotIn(inherited, document)
        _, cfg, state = common.load_run('sparse-run/001')
        self.assertEqual(cfg['schema_version'], 5)
        self.assertEqual(cfg['budgets'], {**base['budgets'], 'researchers': 5,
                                         'rounds_per_researcher': 6})
        self.assertEqual(cfg['compute'], base['compute'])
        self.assertEqual(state['config_sha'], common.digest(cfg))
        shared = self.root / 'runs/sparse-run/initialization'
        self.assertEqual(document['initialization'], str((shared / 'manifest.json').relative_to(self.root)))
        self.assertTrue(cfg['task']['path'].startswith('runs/sparse-run/initialization/task/'))
        self.assertNotEqual(cfg['task']['path'], base['task'])
        for name in ('task', 'datasets', 'ideas'):
            self.assertFalse((self.root / 'runs/sparse-run/001' / name).exists())
        for kind, ids in (('dataset', {'anchor-a', 'final-a'}), ('ideas', {'ideas-a'})):
            self.assertEqual(set(state['deliveries'][kind]), ids)
            self.assertTrue(all(record['scope'] == 'initialization'
                                for record in state['deliveries'][kind].values()))

    def test_draft_symlink_is_rejected_without_changing_target(self):
        target = self.draft('original-draft/001')
        original = target.read_bytes()
        directory = self.root / 'runs' / 'linked-draft/001'
        helpers.ensure_study(self.root, 'linked-draft')
        directory.mkdir(parents=True)
        (directory / 'run.yaml').symlink_to(target)
        with self.assertRaises(ValueError):
            run.init('linked-draft/001')
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((directory / 'state.json').exists())

    def test_concurrent_initialization_cannot_overwrite_a_draft(self):
        path = self.draft('locked-draft/001')
        before = self.files()
        descriptor = os.open(path.parent.parent, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'initialization.*active'):
                run.init('locked-draft/001')
            self.assertEqual(self.files(), before)
        finally:
            os.close(descriptor)
        run.init('locked-draft/001')
        common.load_run('locked-draft/001')

    def test_single_default_override_applies_to_all_profiles(self):
        self.draft('default-override/001', agent={'defaults': {'model': 'gpt-6-astra'}})
        run.init('default-override/001')
        cfg = common.load_run('default-override/001')[1]
        for slot in ('research',):
            with self.subTest(slot=slot):
                self.assertEqual(self.launch(cfg, slot), {
                    'model': 'gpt-6-astra', 'reasoning_effort': 'high', 'fork_turns': 'none'})
        self.assertTrue(cfg['agent']['native_subagents'])
        self.assertEqual(cfg['agent']['backend'], 'current-conversation')

    def test_profile_fields_override_defaults_and_preserve_base_fields(self):
        base_path = self.root / 'configs/smoke.yaml'
        base = common.read_yaml(base_path)
        base['agent']['profiles']['research'] = {'model': 'gpt-6.1-terra'}
        base_path.write_text(yaml.safe_dump(base), encoding='utf-8')
        self.draft('profile-override/001', agent={
            'defaults': {'reasoning_effort': 'medium'},
            'profiles': {'research': {'reasoning_effort': 'high'}}})
        run.init('profile-override/001')
        cfg = common.load_run('profile-override/001')[1]
        self.assertIsNone(execution.resolve(cfg, 'task')['spawn_options'])
        self.assertEqual(self.launch(cfg, 'research'), {
            'model': 'gpt-6.1-terra', 'reasoning_effort': 'high', 'fork_turns': 'none'})

    def test_other_legal_top_level_overrides_merge_without_expanding_run(self):
        base = common.read_yaml(self.root / 'configs/smoke.yaml')
        path = self.draft('other-overrides/001', purpose='research',
                          compute={'limits': {'wall_s': 9}}, final={'authorized': False})
        run.init('other-overrides/001')
        cfg = common.load_run('other-overrides/001')[1]
        self.assertEqual(cfg['purpose'], 'research')
        self.assertFalse(cfg['final']['authorized'])
        self.assertEqual(cfg['compute'], {
            **base['compute'], 'limits': {**base['compute']['limits'], 'wall_s': 9}})
        document = common.read_yaml(path)
        self.assertEqual(document['compute'], {'limits': {'wall_s': 9}})
        self.assertEqual(document['final'], {'authorized': False})
        self.assertNotIn('task', document)

    def test_base_version_checks_original_bytes_and_rejects_edit_or_deletion(self):
        base_path = self.root / 'configs/smoke.yaml'
        original = base_path.read_bytes() + b'\n# Preserve this exact source revision.\n'
        base_path.write_bytes(original)
        self.init_batch('frozen-base/001', 'configs/smoke.yaml')
        _, frozen_cfg, frozen_state = common.load_run('frozen-base/001')
        reference = frozen_state['configuration']['base']
        self.assertEqual(reference, {'path': 'configs/smoke.yaml', 'sha': common.sha(base_path)})
        self.assertFalse((self.root / 'assets').exists())
        changed = common.read_yaml(base_path)
        changed['budgets']['session_wall_s'] += 7
        base_path.write_text(yaml.safe_dump(changed), encoding='utf-8')
        self.init_batch('new-base/001', 'configs/smoke.yaml')
        _, new_cfg, new_state = common.load_run('new-base/001')
        self.assertEqual(new_cfg['budgets']['session_wall_s'], changed['budgets']['session_wall_s'])
        self.assertNotEqual(new_state['configuration']['base']['sha'], reference['sha'])
        with self.assertRaisesRegex(ValueError, 'base configuration changed'):
            common.load_run('frozen-base/001')
        base_path.unlink()
        for run_id in ('frozen-base/001', 'new-base/001'):
            with self.subTest(run=run_id), self.assertRaisesRegex(ValueError, 'base configuration missing'):
                common.load_run(run_id)
        base_path.write_bytes(original)
        self.assertEqual(common.load_run('frozen-base/001')[1], frozen_cfg)

    def test_corrupted_archive_does_not_change_effective_configuration(self):
        _, _, state = self.snapshot()
        reference = state['configuration']['base']
        archive = self.root / 'assets/objects'
        archive.mkdir(parents=True)
        (archive / reference['sha']).write_text('damaged base\n')
        self.assertEqual(self.snapshot()[2], state)

    def test_sparse_overrides_reject_null_and_unknown_fields(self):
        invalid = [
            {'budgets': None},
            {'budgets': {'researchers': None}},
            {'agent': {'defaults': {'model': None}}},
            {'agent': {'profiles': {'research': {'reasoning_effort': None}}}},
            {'unknown_setting': True},
            {'compute': {'limits': {'unknown_limit': 1}}},
            {'budgets': {'rounds_per_researchers': 2}},
        ]
        for index, overrides in enumerate(invalid):
            with self.subTest(overrides=overrides):
                run_id = f'invalid-override-{index}/001'
                self.draft(run_id, **overrides)
                with self.assertRaises((ValueError, jsonschema.ValidationError)):
                    run.init(run_id)
                self.assertFalse((self.root / 'runs' / run_id / 'state.json').exists())

    def test_preinit_base_launch_is_read_only_without_task(self):
        path = self.root / 'configs/preinit.yaml'
        base = common.read_yaml(self.root / 'configs/smoke.yaml')
        base['task'] = 'tasks/not-created/task.json'
        base['agent']['defaults']['reasoning_effort'] = 'medium'
        path.write_text(yaml.safe_dump(base), encoding='utf-8')
        before = self.files()
        result = run.command(argparse.Namespace(command='launch-spec',
                             config=str(path.relative_to(self.root)), run=None,
                             plugin='task', role='self'))
        self.assertIsNone(result['spawn_options'])
        self.assertEqual(self.files(), before)

    def test_batch_task_override_is_rejected_without_mutation(self):
        self.draft('task-override/001', task='tasks/not-created/task.json')
        before = self.files()
        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            run.init('task-override/001')
        self.assertEqual(self.files(), before)

    def test_config_command_returns_effective_config_without_writing(self):
        _, expected, _ = self.snapshot()
        before = self.files()
        result = self.command('config')
        self.assertEqual(result, expected)
        self.assertEqual(self.files(), before)

    def test_initialized_run_cannot_be_previewed_as_an_unsealed_draft(self):
        path = f'runs/{self.run_id}/run.yaml'
        with self.assertRaisesRegex(ValueError, 'initialized|--run'):
            run.command(argparse.Namespace(command='launch-spec', config=path, run=None,
                                           plugin='research', role='self'))

    def test_manual_changes_after_draft_initialization_are_rejected(self):
        path = self.draft('sealed-draft/001')
        run.init('sealed-draft/001')
        before = common.read_json(path.parent / 'state.json')
        document = common.read_yaml(path)
        document['budgets']['researchers'] += 1
        path.write_text(yaml.safe_dump(document), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'configuration changed|configuration.*mismatch'):
            common.load_run('sealed-draft/001')
        self.assertEqual(common.read_json(path.parent / 'state.json'), before)
        with self.assertRaisesRegex(ValueError, 'run exists|initialized'):
            run.init('sealed-draft/001')

    def test_binding_keeps_inheritance_sparse_and_records_changed_digest(self):
        run_dir, old_cfg, old_state = self.snapshot()
        before = common.read_yaml(run_dir / 'run.yaml')
        replacement = self.write_plugin('replacement.md', 'replacement-cases', ['dataset'])
        self.command('bind', slot='datasets', path=replacement)
        _, cfg, state = self.snapshot()
        document = common.read_yaml(run_dir / 'run.yaml')
        self.assertEqual(document['extends'], before['extends'])
        self.assertEqual(document['budgets'], before['budgets'])
        self.assertEqual(document['modules'], {'datasets': {'use':replacement}})
        for inherited in ('task', 'agent', 'compute', 'final', '_pins'):
            self.assertNotIn(inherited, document)
        self.assertEqual(state['configuration']['base'], old_state['configuration']['base'])
        self.assertEqual(cfg['budgets'], old_cfg['budgets'])
        self.assertEqual(cfg['agent'], old_cfg['agent'])
        self.assertNotEqual(state['config_sha'], old_state['config_sha'])
        self.assertEqual(state['config_sha'], common.digest(cfg))
        events = [common.parse_json(line) for line in (run_dir / 'events.jsonl').read_text().splitlines()]
        binding = [entry for entry in events if entry['kind'] == 'module_bound'][-1]
        self.assertEqual(binding['slot'], 'datasets')
        self.assertEqual(binding['old'], old_cfg['modules']['datasets']['sha'])
        self.assertEqual(binding['new'], cfg['modules']['datasets']['sha'])
        self.assertIn('previous_config', binding)

    def test_runs_share_fixed_base_without_sharing_mutable_state(self):
        created = run.init('contract-study', 'configs/smoke.yaml', researchers=1, rounds=1)
        self.assertEqual(created['run_id'], 'contract-study/002')
        second_dir, second_cfg, second_state = common.load_run('contract-study/002')
        before = {name: (second_dir / name).read_bytes()
                  for name in ('run.yaml', 'state.json', 'events.jsonl')}
        self.command('begin-session', actor='only-first-run', role='researcher', idea='idea-a')
        _, first_cfg, first_state = self.snapshot()
        self.assertEqual(first_state['configuration']['base'], second_state['configuration']['base'])
        self.assertEqual(len(first_state['sessions']), 1)
        self.assertEqual(common.load_run('contract-study/002')[2]['sessions'], [])
        self.assertEqual(common.load_run('contract-study/002')[1], second_cfg)
        self.assertNotEqual(first_cfg['budgets']['researchers'], second_cfg['budgets']['researchers'])
        self.assertEqual(first_cfg['initialization'], second_cfg['initialization'])
        self.assertEqual(first_cfg['task'], second_cfg['task'])
        for kind in ('dataset', 'ideas'):
            self.assertEqual(first_state['deliveries'][kind], second_state['deliveries'][kind])
        self.assertEqual(before, {name: (second_dir / name).read_bytes() for name in before})

    def test_historical_runs_are_read_only_and_not_rewritten(self):
        _, current_cfg, current_state = self.snapshot()
        for version in (2, 3, 4, 6):
            with self.subTest(schema_version=version):
                historical_id = f'flat-schema{version}'
                historical_dir = self.root / 'runs' / historical_id
                historical_dir.mkdir()
                cfg, state = copy.deepcopy(current_cfg), copy.deepcopy(current_state)
                cfg['run_id'] = historical_id
                cfg.pop('initialization')
                for records in state['deliveries'].values():
                    records.clear()
                if version == 6:
                    document = {'schema_version': 6, 'extends': 'configs/smoke.yaml',
                                'task': cfg['task']['path']}
                    lock = state['configuration']
                    lock.pop('initialization')
                    lock['run_id'] = historical_id
                    lock['document_sha'] = common.digest(document)
                else:
                    cfg['schema_version'] = 3 if version == 4 else version
                    cfg['plugins'] = cfg.pop('modules')
                    if version == 4:
                        base_path = self.root / 'configs/legacy-schema3.yaml'
                        base_path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
                        document = {'schema_version': 4, 'extends': 'configs/legacy-schema3.yaml'}
                        state['configuration'] = {
                            **{key: copy.deepcopy(cfg[key]) for key in
                               ('task', 'plugins', '_pins', 'run_id', 'created_at')},
                            'base': common.file_ref(base_path),
                            'document_sha': common.digest(document)}
                    else:
                        document = cfg
                        state.pop('configuration')
                state['config_sha'] = common.digest(cfg)
                (historical_dir / 'run.yaml').write_text(yaml.safe_dump(document), encoding='utf-8')
                common.write_json(historical_dir / 'state.json', state)
                (historical_dir / 'events.jsonl').write_text('', encoding='utf-8')
                before = {name: (historical_dir / name).read_bytes()
                          for name in ('run.yaml', 'state.json', 'events.jsonl')}
                original_id = self.run_id
                self.run_id = historical_id
                try:
                    self.assertTrue(self.command('check')['historical_read_only'])
                    self.assertTrue(self.command('status')['historical_read_only'])
                    if version != 6:
                        self.assertIsNone(self.command('plugin', slot='research')['execution'])
                    with self.assertRaisesRegex(ValueError, 'read-only'):
                        self.command('begin-session', actor='new-author')
                    with self.assertRaisesRegex(ValueError, 'read-only'):
                        run.init(historical_id, 'configs/smoke.yaml')
                    self.assertEqual(before, {name: (historical_dir / name).read_bytes()
                                              for name in before})
                    if version == 6:
                        changed = dict(document, purpose=cfg['purpose'])
                        (historical_dir / 'run.yaml').write_text(yaml.safe_dump(changed))
                        with self.assertRaisesRegex(ValueError, 'historical configuration changed'):
                            self.command('check')
                        (historical_dir / 'run.yaml').write_bytes(before['run.yaml'])
                finally:
                    self.run_id = original_id


if __name__ == '__main__':
    unittest.main()
