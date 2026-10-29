"""Archive independence and explicit recovery checks in disposable roots only."""
import argparse
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_contracts as helpers
from tools import archive, common, measure, run, sandbox


class ArchiveTests(unittest.TestCase):
    setUp = helpers.ContractTests.setUp
    command = helpers.ContractTests.command
    snapshot = helpers.ContractTests.snapshot
    write_plugin = helpers.ContractTests.write_plugin
    write_candidate = helpers.ContractTests.write_candidate
    deliver_candidate = helpers.ContractTests.deliver_candidate
    write_dataset = helpers.ContractTests.write_dataset
    deliver_dataset = helpers.ContractTests.deliver_dataset

    def test_workflow_never_accesses_archive(self):
        destination = self.root / 'assets'
        real_open, real_stat = Path.open, Path.stat

        def guard(method):
            def guarded(path, *args, **kwargs):
                if path == destination or destination in path.parents:
                    raise AssertionError('normal workflow touched assets: ' + str(path))
                return method(path, *args, **kwargs)
            return guarded

        for index, condition in enumerate(('missing', 'blocked', 'damaged')):
            with self.subTest(archive=condition):
                if condition == 'blocked':
                    destination.write_text('a file blocks directory creation')
                elif condition == 'damaged':
                    destination.unlink()
                    (destination/'objects').mkdir(parents=True)
                    (destination/'objects'/'bad-object').write_text('damaged')
                with patch.object(Path, 'open', guard(real_open)), patch.object(Path, 'stat', guard(real_stat)):
                    self.run_id = f'contract-study/{index+2:03d}'
                    run.init(self.run_id, 'configs/smoke.yaml')
                    self.assertTrue(self.command('check')['ok'])
                    self.assertEqual(self.command('config')['run_id'], self.run_id)
                    self.assertEqual(self.command('status')['evaluations'], 0)
                    self.assertTrue(self.command('plugin', slot='datasets')['text'])
                    replacement = self.write_plugin(f'replacement-{index}.md', 'replacement-cases', ['dataset'])
                    self.command('bind', slot='datasets', path=replacement)
                    spec = run.command(argparse.Namespace(command='launch-spec', config=None,
                                       run=self.run_id, plugin='research', role='self'))
                    self.assertEqual(spec['mode'], 'main')
                    self.deliver_candidate()
                    self.command('begin-session', actor='record-only', idea='idea-a')
                    helpers.checkpoint_session(self, 'record-only')
                    self.command('end-session', actor='record-only', status='completed', native_id=None)
                    self.assertTrue(self.command('check')['ok'])
                if condition == 'missing':
                    self.assertFalse(destination.exists())
        self.assertEqual((destination/'objects'/'bad-object').read_text(), 'damaged')

    def test_measurement_bookkeeping_uses_working_files_with_mocked_execution(self):
        self.deliver_candidate()
        self.deliver_dataset()
        (self.root/'assets').write_text('archive unavailable')
        result = {'valid': True, 'score': 0, 'solution': {}, 'metrics': {},
                  'infrastructure_error': False, 'cleanup_confirmed': True,
                  'wall_s': 0, 'queue_wait_s': 0}
        with patch.object(sandbox, 'execute', return_value=result) as execute:
            summary = measure.evaluate(self.run_id, 'candidate-a', 'anchor-a', 'mock-only')
        execute.assert_called_once()
        self.assertEqual(summary['status'], 'completed')
        self.assertTrue(self.command('check')['ok'])
        self.assertTrue(measure.details(self.run_id, 'mock-only')['records'])

    def test_bind_can_replace_only_its_changed_slot(self):
        _, cfg, _ = self.snapshot()
        target = cfg['modules']['datasets']['path']
        self.write_plugin(Path(target).name, 'new-cases', ['dataset'])
        with self.assertRaisesRegex(ValueError, 'module file changed'):
            self.command('check')
        other = self.root/cfg['modules']['research']['path']
        original = other.read_bytes()
        other.write_bytes(original + b'\nEdited independently.\n')
        with self.assertRaisesRegex(ValueError, 'module file changed'):
            self.command('bind', slot='datasets', path=target)
        other.write_bytes(original)
        self.command('bind', slot='datasets', path=target)
        self.assertTrue(self.command('check')['ok'])
        self.assertEqual(self.command('plugin', slot='datasets')['sha'], common.sha(self.root/target))

    def test_archive_paths_cannot_be_runtime_inputs(self):
        source = 'configs/smoke.yaml'
        ref = archive.save(source)
        path = 'assets/objects/' + ref['sha']
        with self.assertRaisesRegex(ValueError, 'archive paths'):
            run.init('contract-study/002', path)
        with self.assertRaisesRegex(ValueError, 'archive paths'):
            self.command('bind', slot='datasets', path=path)

    def test_explicit_archive_is_deduplicated_and_restoration_preserves_replaced_file(self):
        relative = 'modules/datasets/stratified_holdout/MODULE.md'
        path = self.root/relative
        original = path.read_bytes()
        ref = archive.save(relative)
        self.assertEqual(archive.save(relative), ref)
        self.assertEqual(len(list((self.root/'assets/objects').iterdir())), 1)
        path.write_bytes(original + b'\nNew version.\n')
        changed = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'destination exists'):
            archive.restore(ref['sha'], relative)
        restored = archive.restore(ref['sha'], relative, replace=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(archive.read_object(restored['previous']['sha']), changed)
        self.assertTrue(self.command('check')['ok'])
        shutil.rmtree(self.root/'assets')
        self.assertTrue(self.command('check')['ok'])

    def test_restore_validates_archive_and_destination_before_changing_files(self):
        relative = 'configs/smoke.yaml'
        path = self.root/relative
        original = path.read_bytes()
        ref = archive.save(relative)
        for destination in ('../outside.yaml', 'assets/inside.yaml', 'Assets/inside.yaml',
                            'runs/old/run.yaml', 'Runs/old/run.yaml'):
            with self.subTest(path=destination), self.assertRaises(ValueError):
                archive.restore(ref['sha'], destination, replace=True)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            archive.restore('../outside', 'configs/new.yaml')
        link = self.root/'configs/link.yaml'
        link.symlink_to(path)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            archive.restore(ref['sha'], 'configs/link.yaml', replace=True)
        archive.object_path(ref['sha']).write_text('corrupted')
        with self.assertRaisesRegex(ValueError, 'corrupted'):
            archive.restore(ref['sha'], relative, replace=True)
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, 'corrupted'):
            archive.save(relative)
        self.assertTrue(self.command('check')['ok'])


if __name__ == '__main__':
    unittest.main()
