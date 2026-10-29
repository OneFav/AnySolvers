"""Study initialization checks in disposable roots; no candidates or Docker."""
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
from tools import common, study
from test_contracts import write_dataset_files


class StudyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='anysolvers-study-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('configs', 'modules', 'tools'):
            shutil.copytree(PACKAGE_ROOT / name, self.root / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(PACKAGE_ROOT / 'tests/fixtures', self.root / 'tests/fixtures')
        import yaml
        config_file = self.root / 'configs/smoke.yaml'
        config = yaml.safe_load(config_file.read_text())
        config['task'] = 'tests/fixtures/weighted-tardiness/task.json'
        config_file.write_text(yaml.safe_dump(config))
        replacement = patch.object(common, 'ROOT', self.root)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.case = common.read_json(self.root / 'tests/fixtures/weighted-tardiness/benchmark/fixtures.json')['valid'][0]['case']
        study.create('shared-study', 'configs/smoke.yaml')
        self.initialization = self.root / 'runs/shared-study/initialization'

    def dataset(self, key='anchor-a', purpose='anchor', shift=0):
        cfg = common.read_json(self.initialization / 'manifest.json')
        relative, _ = write_dataset_files(self.initialization, cfg, self.case, key, purpose, shift)
        return relative

    def ideas(self):
        relative = 'ideas/first-ideas.json'
        common.write_json(self.initialization / relative, {'schema_version': 1, 'ideas': [{
            'idea_id': 'first', 'hypothesis': 'A hypothesis', 'mechanism': 'A mechanism',
            'first_change': 'A change', 'risks': 'A risk', 'source_refs': []}]})
        return relative

    def populated(self):
        study.deliver('shared-study', 'dataset', self.dataset())
        study.deliver('shared-study', 'ideas', self.ideas())

    def test_create_copies_only_definition_files_without_study_run_config(self):
        root, manifest = study.load('shared-study')
        self.assertEqual(root, self.initialization)
        self.assertEqual(set(manifest['modules']), {'task', 'datasets', 'ideas'})
        self.assertEqual({p.name for p in (root / 'task').iterdir()}, {'task.json', 'task.md'})
        self.assertFalse((root.parent / 'run.yaml').exists())
        self.assertEqual(list((root / 'datasets').iterdir()), [])
        shutil.rmtree(self.root / 'tests/fixtures')
        (self.root / 'configs/smoke.yaml').unlink()
        self.assertTrue(study.check('shared-study')['ok'])

    def test_seal_and_shared_records_preserve_registry(self):
        self.populated()
        study.seal('shared-study')
        _, manifest = study.load('shared-study', require_sealed=True)
        shared = study.shared_records(manifest)
        self.assertEqual(shared['dataset']['anchor-a']['scope'], 'initialization')
        self.assertNotIn('scope', manifest['deliveries']['dataset']['anchor-a'])
        shared['dataset'].clear()
        self.assertEqual(len(manifest['deliveries']['dataset']), 1)
        summary = study.check('shared-study')
        self.assertEqual(summary['deliveries'], {'dataset': 1, 'ideas': 1})
        self.assertNotIn('manifest', json.dumps(summary))
        before = (self.initialization / 'manifest.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'immutable'):
            study.deliver('shared-study', 'ideas', self.ideas())
        with self.assertRaisesRegex(ValueError, 'immutable'):
            study.seal('shared-study')
        self.assertEqual((self.initialization / 'manifest.json').read_bytes(), before)

    def test_in_place_task_registration_without_copy_or_overwrite(self):
        import yaml
        destination = self.root / 'runs/in-place/initialization/task'
        shutil.copytree(self.initialization / 'task', destination)
        before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in destination.iterdir()}
        config = common.read_yaml(self.root / 'configs/smoke.yaml')
        config['task'] = 'runs/in-place/initialization/task/task.json'
        (self.root / 'configs/in-place.yaml').write_text(yaml.safe_dump(config))
        study.create('in-place', 'configs/in-place.yaml')
        self.assertTrue(study.check('in-place')['ok'])
        self.assertEqual(before, {p.name: (p.stat().st_ino, p.read_bytes()) for p in destination.iterdir()})
        with self.assertRaisesRegex(ValueError, 'exists'):
            study.create('in-place', 'configs/in-place.yaml')

    def test_invalid_in_place_registration_keeps_user_files(self):
        import yaml
        destination = self.root / 'runs/in-place/initialization/task'
        shutil.copytree(self.initialization / 'task', destination)
        (destination / 'notes.txt').write_text('preserve this file')
        config = common.read_yaml(self.root / 'configs/smoke.yaml')
        config['task'] = 'runs/in-place/initialization/task/task.json'
        (self.root / 'configs/in-place.yaml').write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(ValueError, 'two referenced'):
            study.create('in-place', 'configs/in-place.yaml')
        self.assertEqual((destination / 'notes.txt').read_text(), 'preserve this file')
        self.assertFalse((destination.parent / 'manifest.json').exists())

    def test_sealed_generators_are_provenance_but_contracts_remain_pinned(self):
        self.populated()
        study.seal('shared-study')
        for relative in ('modules/datasets/stratified_holdout/package.py', 'modules/task/problem_spec/package.py'):
            path = self.root / relative
            path.write_text(path.read_text() + "\nraise RuntimeError('old generator must not execute')\n")
        module = self.root / 'modules/datasets/stratified_holdout/MODULE.md'
        module.write_text(module.read_text() + '\nUpdated generation method.\n')
        self.assertTrue(study.check('shared-study')['ok'])
        study.load('shared-study', require_sealed=True)
        schema = self.root / 'modules/datasets/stratified_holdout/schemas/dataset.json'
        schema.write_text(schema.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            study.load('shared-study', require_sealed=True)

    def test_seal_requires_anchor_and_ideas(self):
        with self.assertRaisesRegex(ValueError, 'anchor'):
            study.seal('shared-study')
        study.deliver('shared-study', 'dataset', self.dataset())
        with self.assertRaisesRegex(ValueError, 'ideas'):
            study.seal('shared-study')
        with self.assertRaisesRegex(ValueError, 'sealed'):
            study.load('shared-study', require_sealed=True)

    def test_failed_delivery_leaves_registry_unchanged(self):
        study.deliver('shared-study', 'dataset', self.dataset())
        before = (self.initialization / 'manifest.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'anchor'):
            study.deliver('shared-study', 'dataset', self.dataset('anchor-b', shift=1))
        with self.assertRaisesRegex(ValueError, 'overlap'):
            study.deliver('shared-study', 'dataset', self.dataset('final-a', 'final'))
        self.assertEqual((self.initialization / 'manifest.json').read_bytes(), before)
        study.deliver('shared-study', 'dataset', self.dataset('final-b', 'final', 2))

    def test_tampered_registered_delivery_and_task_rejected(self):
        self.populated()
        study.seal('shared-study')
        cases = self.initialization / 'datasets/anchor-a/cases.jsonl'
        original = cases.read_bytes()
        cases.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            study.load('shared-study')
        cases.write_bytes(original)
        _, manifest = study.load('shared-study')
        task = self.root / manifest['task']['files']['description_file']['path']
        task.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            study.check('shared-study')

    def test_symlinks_and_cross_namespace_paths_rejected(self):
        outside = self.root / 'outside.json'
        common.write_json(outside, {'schema_version': 1, 'ideas': []})
        (self.initialization / 'ideas/link.json').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            study.deliver('shared-study', 'ideas', 'ideas/link.json')
        for relative in ('../outside.json', str(outside), 'task/task.json'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                study.deliver('shared-study', 'ideas', relative)

    def test_task_symlink_or_invalid_source_does_not_leave_partial_study(self):
        task = self.root / 'tests/fixtures/weighted-tardiness/task.md'
        task.unlink()
        task.symlink_to(self.root / 'tests/fixtures/weighted-tardiness/benchmark/baseline.py')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            study.create('bad-study', 'configs/smoke.yaml')
        self.assertFalse((self.root / 'runs/bad-study').exists())

    def test_directory_lock_and_legacy_path_rejection(self):
        descriptor = os.open(self.initialization.parent, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(ValueError, 'another study'):
            study.check('shared-study')
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        legacy = self.root / 'runs/legacy'
        legacy.mkdir()
        (legacy / 'run.yaml').write_text('schema_version: 4\n')
        with self.assertRaisesRegex(ValueError, 'legacy'):
            study.load('legacy')
        with self.assertRaisesRegex(ValueError, 'exists'):
            study.create('legacy', 'configs/smoke.yaml')
        self.assertEqual(list(legacy.iterdir()), [legacy / 'run.yaml'])


if __name__ == '__main__':
    unittest.main()
