"""Initialization budgets: no fixed capacity and no candidate execution."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
from tools import common, study
from tools.configuration import prepare
from tools.module_runtime import invoke, resolve_bindings
from tools.plan_resources import plan
from test_contracts import write_dataset_files


class ResourcePlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='anysolvers-resources-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        engine_patch = patch.object(common, 'ENGINE_ROOT', self.root)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        for name in ('configs', 'modules', 'tools'):
            shutil.copytree(PACKAGE_ROOT / name, self.root / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(PACKAGE_ROOT / 'tests/fixtures', self.root / 'tests/fixtures')
        config_file = self.root / 'configs/smoke.yaml'
        config = yaml.safe_load(config_file.read_text())
        config['task'] = 'tests/fixtures/weighted-tardiness/task.json'
        config['budgets']['max_cases_per_dataset'] = None
        config_file.write_text(yaml.safe_dump(config))
        replacement = patch.object(common, 'ROOT', self.root)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.case = common.read_json(self.root / 'tests/fixtures/weighted-tardiness/benchmark/fixtures.json')['valid'][0]['case']
        study.create('resources', 'configs/smoke.yaml')
        self.directory = self.root / 'runs/resources/initialization'

    def register(self, key='anchor-a', purpose='anchor', shift=0, count=1):
        manifest = common.read_json(self.directory / 'manifest.json')
        relative, cases = write_dataset_files(self.directory, manifest, self.case, key, purpose, shift)
        rows = []
        for index in range(count):
            value = copy.deepcopy(self.case)
            value['jobs'][0]['d'] += shift + index
            rows.append(json.dumps({'case_id': 'case-' + str(index), 'input': value}))
        (self.directory / cases).write_text('\n'.join(rows) + ('\n' if rows else ''))
        return study.deliver('resources', 'dataset', relative)

    def config(self):
        cfg = prepare('configs/smoke.yaml')
        cfg['modules'] = resolve_bindings(cfg['modules'])
        return cfg

    def test_null_capacity_accepts_more_than_old_limit_but_not_empty(self):
        self.register(count=13)
        self.assertIsNone(study.load('resources')[1]['max_cases_per_dataset'])
        with self.assertRaisesRegex(ValueError, 'dataset size'):
            self.register('final-a', 'final', 50, count=0)

    def test_explicit_capacity_still_rejects_oversize(self):
        path = self.directory / 'manifest.json'
        manifest = common.read_json(path)
        manifest['max_cases_per_dataset'] = 1
        common.write_json(path, manifest)
        with self.assertRaisesRegex(ValueError, 'dataset size'):
            self.register(count=2)

    def test_proposal_uses_final_count_without_parsing_final_inputs(self):
        self.register(count=2)
        self.register('final-a', 'final', 50, count=4)
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        original = common.parse_json

        def reject_final_case_parse(text):
            if '"case_id"' in text:
                raise AssertionError('case content was parsed for a resource proposal')
            return original(text)

        with patch.object(common, 'parse_json', reject_final_case_parse):
            result = plan('resources', 'configs/smoke.yaml')
        self.assertEqual(result['dataset_counts'], {'anchor-a': 2, 'final-a': 4})
        compute = result['configuration_patch']['compute']
        formula = result['formula']
        self.assertGreaterEqual(compute['evaluation_wall_s'],
                                4 * (compute['limits']['wall_s'] + formula['case_overhead_reserve_s']))
        self.assertEqual(result['basis'], 'heuristic_starter_not_measured')
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes()
                                  for p in self.root.rglob('*') if p.is_file()})

    def test_measurements_need_source_and_keep_case_and_total_limits_consistent(self):
        self.register()
        with self.assertRaisesRegex(ValueError, 'source note'):
            plan('resources', 'configs/smoke.yaml', {'wall_s': 2})
        result = plan('resources', 'configs/smoke.yaml',
                      {'wall_s': 2, 'cpu_s': 1, 'peak_memory_mb': 90,
                       'note': 'Authorized public anchor baseline sample; externally supplied.'})
        limits = result['configuration_patch']['compute']['limits']
        self.assertEqual((limits['wall_s'], limits['cpu_s'], limits['memory_mb']), (10, 4, 192))
        self.assertIn('not_remeasured', result['basis'])
        with self.assertRaisesRegex(ValueError, 'finite'):
            plan('resources', 'configs/smoke.yaml', {'wall_s': float('nan'), 'note': 'bad'})

    def test_interactive_protocol_stays_intact_and_gets_separate_limits(self):
        cfg = self.config()
        protocol = {'kind': 'episode', 'max_steps': 100, 'action_wall_s': 2}
        result = invoke(cfg, 'datasets', 'recommend_compute', protocol, {'anchor': 5},
                        cfg['compute'], invoke(cfg, 'evaluation', 'resource_contract'))
        self.assertEqual(result['protocol'], protocol)
        self.assertEqual(result['configuration_patch']['compute']['limits']['wall_s'], 120)
        self.assertEqual(result['capacity_to_check']['cpu_cores_including_host_reserve'], 4)

    def test_requires_registered_anchor(self):
        with self.assertRaisesRegex(ValueError, 'development anchor'):
            plan('resources', 'configs/smoke.yaml')


if __name__ == '__main__':
    unittest.main()
