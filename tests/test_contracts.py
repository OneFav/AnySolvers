"""File-contract checks in disposable roots; never execute candidates or Docker."""

import argparse
import copy
import datetime as dt
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
from tools import common, run, study, research

METHODS = {
    'task': 'problem_spec', 'datasets': 'stratified_holdout',
    'ideas': 'diverse_hypotheses', 'research': 'notebook_collaboration',
    'evaluation': 'queued_sandbox', 'selections': 'evidence_iteration',
    'final': 'frozen_holdout', 'visualization': 'evidence_charts',
}


def method_path(slot):
    return 'modules/' + slot + '/' + METHODS[slot]


def install_fixture_benchmark(directory, task_id='weighted-tardiness'):
    """Install one shared executable benchmark under datasets, never under task."""
    target = directory / 'datasets/runtime'
    if not target.exists():
        shutil.copytree(common.ROOT / 'tests/fixtures' / task_id / 'benchmark', target)
    return 'datasets/runtime/benchmark.json'


def write_dataset_files(directory, config, example, dataset_id, purpose, shift=0,
                        case=None, plugin_id=None, prefix='datasets'):
    case = copy.deepcopy(example if case is None else case)
    case['jobs'][0]['d'] += shift
    cases_file = f'{prefix}/{dataset_id}/cases.jsonl'
    path = directory / cases_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'case_id': 'case-a', 'input': case}) + '\n', encoding='utf-8')
    manifest_file = f'{prefix}/{dataset_id}/manifest.json'
    benchmark_file = (install_fixture_benchmark(directory) if prefix == 'datasets' else '@anchor')
    common.write_json(directory / manifest_file, {
        'schema_version': 1, 'dataset_id': dataset_id, 'task_id': 'weighted-tardiness',
        'benchmark_file': benchmark_file,
        'source': {'kind': 'synthetic', 'reference': 'hand-calculated local test fixture',
                   'version': '1.0.1', 'description': 'Deterministic scheduling contract example',
                   'license': 'Local test fixture', 'files': [], 'adapter_file': None},
        'purpose': purpose, 'cases_file': cases_file,
        'generator': {'plugin_id': plugin_id or config['modules']['datasets']['id'],
                      'code_file': None, 'seed': None,
                      'parameters': {'method': 'deterministic contract fixture', 'shift': shift}},
        'parents': [],
    })
    return manifest_file, cases_file


def ensure_study(root, study_id='contract-study', config_path='configs/smoke.yaml'):
    """Create real sealed shared fixtures once; common.ROOT must point at root."""
    directory = root / 'runs' / study_id / 'initialization'
    if directory.exists():
        return study.load(study_id, require_sealed=True)
    study.create(study_id, config_path)
    directory, manifest = study.load(study_id)
    example = common.read_json(root / 'tests/fixtures/weighted-tardiness/benchmark/fixtures.json')['valid'][0]['case']
    for dataset_id, purpose, shift in (('anchor-a', 'anchor', 0), ('final-a', 'final', 5)):
        relative, _ = write_dataset_files(directory, manifest, example, dataset_id, purpose, shift)
        study.deliver(study_id, 'dataset', relative, 'datasets')
    relative = 'ideas/ideas-a.json'
    common.write_json(directory / relative, {'schema_version': 1, 'ideas': [{
        'idea_id': 'idea-a', 'hypothesis': 'A checkable improvement',
        'mechanism': 'A documented rule', 'first_change': 'Write an independent solver',
        'risks': 'May not improve', 'source_refs': []}]})
    study.deliver(study_id, 'ideas', relative, 'ideas')
    study.seal(study_id)
    return study.load(study_id, require_sealed=True)


def checkpoint_session(test, actor, next_iteration_id=None):
    """Seal an explicit fixture notebook before completing a disposable session."""
    directory, _, state = test.snapshot()
    session = next(s for s in state['sessions'] if s['actor'] == actor and s['status'] == 'active')
    sid = session['session_id']
    rid = 'checkpoint-' + sid
    relative = 'research/inbox/' + rid + '.json'
    common.write_json(directory / relative, {
        'id': rid, 'session_id': sid, 'candidate_id': None, 'evidence_ids': [],
        'learned': 'Fixture bookkeeping only; no solver was executed.',
        'failed_or_uncertain': 'No research conclusion is claimed.',
        'next_iteration_id': next_iteration_id,
    })
    return research.command(test.run_id, 'notebook', session_id=sid, relative=relative)


class ContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="anysolvers-v2-contract-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("configs", "modules", "spare_modules", "tools"):
            shutil.copytree(PACKAGE_ROOT / name, self.root / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(PACKAGE_ROOT / 'tests/fixtures', self.root / 'tests/fixtures')
        import yaml
        config_file = self.root / 'configs/smoke.yaml'
        config = yaml.safe_load(config_file.read_text())
        config['task'] = 'tests/fixtures/weighted-tardiness/task.json'
        config_file.write_text(yaml.safe_dump(config))
        for module in (common, run):
            replacement = patch.object(module, "ROOT", self.root)
            replacement.start()
            self.addCleanup(replacement.stop)
        config_path = self.root / "configs/smoke.yaml"
        config = common.read_yaml(config_path)
        config["budgets"].update(researchers=2, rounds_per_researcher=2, session_wall_s=1)
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        ensure_study(self.root)
        self.run_id = "contract-study/001"
        run.init(self.run_id, "configs/smoke.yaml")
        fixtures = common.read_json(self.root / "tests/fixtures/weighted-tardiness/benchmark/fixtures.json")
        self.example = fixtures["valid"][0]["case"]

    def command(self, name, **kwargs):
        fixture_native = kwargs.pop('native_id', None) if name == 'begin-session' else None
        if name == 'begin-session':
            kwargs.setdefault('plugin', 'research')
            kwargs.setdefault('role', 'researcher')
        result = run.command(argparse.Namespace(command=name, run=self.run_id, **kwargs))
        if name == 'begin-session':
            # Simulate an immediate successful spawn; lifecycle tests exercise the
            # unactivated registration separately without this fixture helper.
            from modules.research.notebook_collaboration import runtime
            directory, cfg, state = self.snapshot()
            with common.mutation(directory):
                result = runtime.activate_session(directory, cfg, state, kwargs['actor'],
                    fixture_native or 'fixture-' + result['session_id'], now=result['started_at'])
        return result

    def snapshot(self):
        return common.load_run(self.run_id)

    def write_candidate(self, candidate_id="candidate-a", source=None, session_id=None):
        directory = f"research/candidates/{candidate_id}"
        run_dir, _, state = self.snapshot()
        if session_id is None:
            active = [s for s in state['sessions'] if s['status'] == 'active']
            if len(active) > 1:
                raise ValueError('test candidate needs an explicit session_id with multiple active actors')
            if active:
                session = active[0]
            else:
                actor = 'fixture-author-' + str(len(state['sessions']) + 1)
                session = self.command('begin-session',actor=actor,idea='idea-a')
            session_id = session['session_id']
        else:
            session = next(s for s in state['sessions'] if s['session_id'] == session_id)
        source_file = f"{directory}/solver.py"
        path = run_dir / source_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source or "def solve(case):\n    return {'order': [j['id'] for j in case['jobs']]}\n",
                        encoding="utf-8")
        manifest_file = f"{directory}/manifest.json"
        common.write_json(run_dir / manifest_file, {
            "schema_version": 1, "candidate_id": candidate_id, "task_id": "weighted-tardiness",
            "source_file": source_file, "entrypoint": "solve(case)", "dependencies": [],
            "parent_id": session.get('parent_candidate'), "session_id": session_id,
            "rationale": "A fixed contract-test candidate, never executed.",
        })
        return manifest_file, source_file

    def deliver_candidate(self, candidate_id="candidate-a", source=None):
        had_active = any(s['status']=='active' for s in self.snapshot()[2]['sessions'])
        manifest, source_file = self.write_candidate(candidate_id, source)
        self.command("deliver", kind="candidate", manifest=manifest, plugin="research")
        if not had_active:
            sid=common.read_json(self.snapshot()[0]/manifest)['session_id']
            session=next(s for s in self.snapshot()[2]['sessions'] if s['session_id']==sid)
            checkpoint_session(self, session['actor'])
            self.command('end-session',actor=session['actor'],status='completed',native_id=None)
        return manifest, source_file

    def write_dataset(self, dataset_id="anchor-a", purpose="anchor", shift=0,
                      case=None, plugin_id=None):
        run_dir, config, _ = self.snapshot()
        return write_dataset_files(run_dir, config, self.example, dataset_id, purpose,
                                   shift, case, plugin_id, 'research/datasets')

    def deliver_dataset(self, dataset_id='anchor-a', purpose='anchor', shift=0,
                        case=None, plugin_id=None):
        _, _, state = self.snapshot()
        record = state['deliveries']['dataset'].get(dataset_id)
        if (record and record.get('scope') == 'initialization' and case is None
                and plugin_id is None and (dataset_id, purpose, shift) in
                (('anchor-a', 'anchor', 0), ('final-a', 'final', 5))):
            return record['manifest_path'], record['manifest']['cases_file']
        manifest, cases_file = self.write_dataset(dataset_id, purpose, shift, case, plugin_id)
        self.command("deliver", kind="dataset", manifest=manifest, plugin="datasets")
        return manifest, cases_file

    def write_plugin(self, filename, plugin_id, produces):
        source = self.root / method_path('datasets')
        if filename == 'MODULE.md':
            directory = source
        else:
            directory = self.root / 'modules/datasets' / Path(filename).stem
            shutil.copytree(source, directory)
        path = directory / 'MODULE.md'
        _, meta, body = path.read_text().split('---', 2)
        metadata = yaml.safe_load(meta)
        metadata.update(id=plugin_id, version='1.0.0', produces=produces,
                        execution={'mode': 'main', 'delegates': {}})
        path.write_text("---\n" + yaml.safe_dump(metadata) + "---\nA contract-test module.\n", encoding='utf-8')
        return str(path.relative_to(self.root))

    def test_init_read_and_snapshot_references(self):
        run_dir, config, state = self.snapshot()
        self.assertEqual(run_dir, self.root / "runs" / self.run_id)
        self.assertEqual(config["run_id"], self.run_id)
        self.assertEqual(state["config_sha"], common.digest(config))
        self.assertTrue(self.command("check")["ok"])
        self.assertEqual(self.command("status")["evaluations"], 0)
        pinned = self.command("plugin", slot="datasets")
        self.assertEqual(pinned["sha"], config["modules"]["datasets"]["sha"])
        (self.root / config["modules"]["datasets"]["path"]).write_text("unbound replacement", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "module file changed"):
            self.command("plugin", slot="datasets")
        with self.assertRaisesRegex(ValueError, "run exists"):
            run.init(self.run_id, "configs/smoke.yaml")

    def test_duplicate_json_and_yaml_keys_are_rejected(self):
        for content in ('{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}'):
            with self.subTest(format="json", content=content), self.assertRaises(ValueError):
                common.parse_json(content)
        path = self.root / "duplicate.yaml"
        for content in ("a: 1\na: 2\n", "nested:\n  a: 1\n  a: 2\n"):
            path.write_text(content, encoding="utf-8")
            with self.subTest(format="yaml", content=content), self.assertRaises(ValueError):
                common.read_yaml(path)

    def test_nonfinite_json_numbers_are_rejected(self):
        for content in ('{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":[1e999]}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                common.parse_json(content)

    def test_paths_reject_escape_and_symlinks(self):
        root = self.root / "path-test"
        root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file").write_text("unrelated", encoding="utf-8")
        (root / "link").symlink_to(outside, target_is_directory=True)
        (root / "file-link").symlink_to(outside / "file")
        for relative in ("../outside/file", "sub/../../outside/file", str(outside / "file"),
                         "link/file", "file-link", "", "."):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                common.contained(root, relative)
        self.assertEqual(common.contained(root, "future/file"), root / "future/file")
        self.assertEqual((outside / "file").read_text(), "unrelated")

    def test_candidate_registration_never_executes_source_on_host(self):
        marker = self.root / "candidate-was-executed"
        source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('BAD')\nraise RuntimeError('must never run')\n"
        _, source_file = self.deliver_candidate(source=source)
        self.assertFalse(marker.exists())
        run_dir, _, state = self.snapshot()
        record = state["deliveries"]["candidate"]["candidate-a"]
        self.assertEqual(record["files"][source_file], common.sha(run_dir / source_file))
        self.assertFalse(marker.exists())

    def test_sealed_candidate_source_tamper_is_rejected(self):
        _, source_file = self.deliver_candidate()
        run_dir, _, _ = self.snapshot()
        (run_dir / source_file).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sealed delivery changed"):
            self.snapshot()

    def test_sealed_manifest_tamper_is_rejected(self):
        manifest, _ = self.deliver_candidate()
        run_dir, _, _ = self.snapshot()
        data = common.read_json(run_dir / manifest)
        data["rationale"] = "Changed after delivery."
        common.write_json(run_dir / manifest, data)
        with self.assertRaisesRegex(ValueError, "sealed delivery changed"):
            self.snapshot()

    def test_trusted_task_tamper_and_symlink_replacement_are_rejected(self):
        run_dir, config, _ = self.snapshot()
        entry = config["task"]["files"]["description_file"]
        description = self.root / entry["path"]
        original = description.read_bytes()
        description.write_bytes(original + b"\n# modified\n")
        with self.assertRaisesRegex(ValueError, "initialization task file changed|trusted runtime or task file changed"):
            self.snapshot()
        description.write_bytes(original)
        other = self.root / "same-bytes.py"
        other.write_bytes(original)
        description.unlink()
        description.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.snapshot()

    def test_final_and_development_inputs_cannot_overlap_in_either_order(self):
        for first, second in (("anchor", "final"), ("final", "challenge")):
            with self.subTest(first=first):
                study_id = f"overlap-{first}"
                study.create(study_id, "configs/smoke.yaml")
                directory, cfg = study.load(study_id)
                manifest, _ = write_dataset_files(directory, cfg, self.example, "first-data", first)
                study.deliver(study_id, "dataset", manifest, "datasets")
                manifest, _ = write_dataset_files(directory, cfg, self.example, "second-data", second)
                with self.assertRaisesRegex(ValueError, "final/development input overlap"):
                    study.deliver(study_id, "dataset", manifest, "datasets")
                self.assertEqual(list(study.load(study_id)[1]["deliveries"]["dataset"]), ["first-data"])

    def test_duplicate_rows_and_second_anchor_are_rejected(self):
        study_id = 'duplicate-data'
        study.create(study_id, 'configs/smoke.yaml')
        directory, cfg = study.load(study_id)
        manifest, cases_file = write_dataset_files(directory, cfg, self.example, 'anchor-a', 'anchor')
        path = directory / cases_file
        row = common.parse_json(path.read_text())
        path.write_text(json.dumps(row) + "\n" + json.dumps({**row, "case_id": "case-b"}) + "\n")
        with self.assertRaisesRegex(ValueError, "duplicate case or input"):
            study.deliver(study_id, 'dataset', manifest, 'datasets')
        path.write_text(json.dumps(row) + "\n")
        study.deliver(study_id, 'dataset', manifest, 'datasets')
        other, _ = write_dataset_files(directory, cfg, self.example, 'anchor-b', 'anchor', shift=1)
        with self.assertRaisesRegex(ValueError, "anchor is fixed once"):
            study.deliver(study_id, 'dataset', other, 'datasets')

    def test_batch_challenge_cannot_overlap_shared_final_or_replace_shared_anchor(self):
        manifest, _ = self.write_dataset('overlaps-final', 'challenge', shift=5)
        with self.assertRaisesRegex(ValueError, 'final/development input overlap'):
            self.command('deliver', kind='dataset', manifest=manifest, plugin='datasets')
        manifest, _ = self.write_dataset('anchor-b', 'anchor', shift=1)
        with self.assertRaisesRegex(ValueError, 'anchor/final belong to shared initialization'):
            self.command('deliver', kind='dataset', manifest=manifest, plugin='datasets')
        self.assertEqual(set(self.snapshot()[2]['deliveries']['dataset']), {'anchor-a', 'final-a'})

    def test_plugin_binding_preserves_old_provenance_and_checks_new_outputs(self):
        self.deliver_dataset()
        _, config_before, state_before = self.snapshot()
        old_record = copy.deepcopy(state_before["deliveries"]["dataset"]["anchor-a"])
        replacement = self.write_plugin("replacement.md", "cases-replacement", ["dataset"])
        result = self.command("bind", slot="datasets", path=replacement)
        _, config_after, state_after = self.snapshot()
        self.assertEqual(result["module"], "cases-replacement")
        self.assertNotEqual(config_before["modules"]["datasets"]["sha"], config_after["modules"]["datasets"]["sha"])
        self.assertEqual(state_after["deliveries"]["dataset"]["anchor-a"], old_record)
        stale, _ = self.write_dataset("stale-provenance", "challenge", shift=1,
                                      plugin_id=config_before["modules"]["datasets"]["id"])
        with self.assertRaisesRegex(ValueError, "provenance mismatch"):
            self.command("deliver", kind="dataset", manifest=stale, plugin="datasets")
        self.deliver_dataset("new-data", "challenge", shift=2)
        _, config_after, state_after = self.snapshot()
        new_record = state_after["deliveries"]["dataset"]["new-data"]
        self.assertEqual(new_record["module_id"], "cases-replacement")
        self.assertEqual(new_record["module_sha"], config_after["modules"]["datasets"]["sha"])
        self.assertEqual(new_record["config_sha"], state_after["config_sha"])

    def test_incompatible_plugin_binding_leaves_run_unchanged(self):
        run_dir, _, _ = self.snapshot()
        before = {name: (run_dir / name).read_bytes() for name in ("run.yaml", "state.json", "events.jsonl")}
        wrong = self.write_plugin("wrong-output.md", "wrong-output", ["candidate"])
        with self.assertRaisesRegex(ValueError, "incompatible output contract"):
            self.command("bind", slot="datasets", path=wrong)
        self.assertEqual(before, {name: (run_dir / name).read_bytes() for name in before})
        self.snapshot()

    def test_failed_plugin_binding_is_atomic(self):
        run_dir, _, _ = self.snapshot()
        before = {name: (run_dir / name).read_bytes() for name in ("run.yaml", "state.json", "events.jsonl")}
        replacement = self.write_plugin("replacement.md", "cases-replacement", ["dataset"])
        real_save = run.save_state
        calls = 0

        def fail_first_save(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected state-write failure")
            return real_save(*args, **kwargs)

        with patch.object(run, "save_state", side_effect=fail_first_save):
            with self.assertRaises(OSError):
                self.command("bind", slot="datasets", path=replacement)
        self.assertEqual(before, {name: (run_dir / name).read_bytes() for name in before})
        self.snapshot()

    def test_session_caps_and_active_boundary(self):
        self.command("begin-session", actor="researcher-a", idea="idea-a", native_id="native-a-1")
        self.command("begin-session", actor="researcher-b", idea="idea-a")
        with self.assertRaises(ValueError):
            self.command("begin-session", actor="researcher-a", idea="idea-a")
        replacement = self.write_plugin("replacement.md", "cases-replacement", ["dataset"])
        with self.assertRaisesRegex(ValueError, "end active research sessions"):
            self.command("bind", slot="datasets", path=replacement)
        relative='research/selections/iteration-a.json'
        common.write_json(self.snapshot()[0]/relative,{
            'schema_version':1,'iteration_id':'iteration-a','author_session':'researcher-a-r001','parent_candidate':None,
            'source_idea_ids':['idea-a'],'evidence_ids':[],'history_iteration_ids':[],
            'observations':'Only the initial rule has been considered.',
            'hypothesis':'A deterministic tie-break can reduce lateness.',
            'algorithm_changes':['Order equal deadlines by processing time.'],
            'expected_effect':'Lower lateness on tied-deadline cases.',
            'test_plan':'Evaluate the fixed development set.', 'risks':['May not improve.'],
            'rationale':'Test a concrete alternative to the initial rule.'})
        self.command('deliver',kind='iteration',manifest=relative,module='selections')
        checkpoint_session(self, 'researcher-a', 'iteration-a')
        self.command("end-session", actor="researcher-a", status="completed", native_id="native-a-1")
        self.command("begin-session", actor="researcher-a", iteration="iteration-a", native_id="native-a-2")
        followup = common.read_json(self.snapshot()[0] / relative)
        followup.update(iteration_id='iteration-b', author_session='researcher-a-r002')
        followup_path = 'research/selections/iteration-b.json'
        common.write_json(self.snapshot()[0] / followup_path, followup)
        self.command('deliver', kind='iteration', manifest=followup_path, module='selections')
        checkpoint_session(self, 'researcher-a', 'iteration-b')
        self.command("end-session", actor="researcher-a", status="failed", native_id="native-a-2")
        with self.assertRaisesRegex(ValueError, "round budget exhausted"):
            self.command("begin-session", actor="researcher-a", iteration="iteration-b")
        checkpoint_session(self, "researcher-b")
        self.command("end-session", actor="researcher-b", status="completed", native_id=None)
        with self.assertRaisesRegex(ValueError, "researcher budget exhausted"):
            self.command("begin-session", actor="researcher-c", idea="idea-a")
        self.assertEqual(len(self.snapshot()[2]["sessions"]), 3)

    def test_session_elapsed_overrun_is_recorded_without_claiming_enforcement(self):
        session = self.command("begin-session", actor="researcher-a", idea="idea-a", native_id="native-id")
        checkpoint_session(self, "researcher-a")
        finish = dt.datetime.fromisoformat(session["started_at"]) + dt.timedelta(seconds=2)
        with patch.object(run, "utc", return_value=finish.isoformat()):
            result = self.command("end-session", actor="researcher-a", status="completed", native_id="native-id")
        self.assertEqual(result["elapsed_s"], 2)
        self.assertTrue(result["over_budget"])
        self.assertEqual(result["native_id"], "native-id")

    def add_summary(self, evaluation_id, candidate="candidate-a", dataset="anchor-a", score=20, **changes):
        run_dir, config, state = self.snapshot()
        candidate_record = state["deliveries"]["candidate"][candidate]
        dataset_record = state["deliveries"]["dataset"][dataset]
        source_file = candidate_record["manifest"]["source_file"]
        kind = 'final_evaluation' if dataset_record['manifest']['purpose'] == 'final' else 'evaluation'
        evaluation_dir = common.output_relative(self.run_id, kind, evaluation_id)
        details_file = f"{evaluation_dir}/results.jsonl"
        summary = {
            "evaluation_id": evaluation_id, "status": "completed", "all_valid": True,
            "infrastructure_error": False, "cleanup_confirmed": True,
            "dataset_purpose": dataset_record["manifest"]["purpose"], "probe": False,
            "candidate_id": candidate, "source_sha": candidate_record["files"][source_file],
            "dataset_id": dataset, "score": score, "config_sha": state["config_sha"],
            "details_file": details_file,
        }
        summary.update(changes)
        relative = f"{evaluation_dir}/summary.json"
        common.write_json(run_dir / relative, summary)
        (run_dir / details_file).write_text(
            json.dumps({"case_id": "case-a", "valid": True, "score": score}) + "\n",
            encoding="utf-8",
        )
        state["evaluations"][evaluation_id] = relative
        state.setdefault("evaluation_hashes", {})[evaluation_id] = {
            "summary_sha": common.sha(run_dir / relative),
            "results_sha": common.sha(run_dir / details_file),
        }
        common.save_state(run_dir, state)
        return summary

    def prepare_selection(self):
        self.deliver_candidate("candidate-a")
        self.deliver_candidate("candidate-b", source="def solve(case):\n    return {'order': []}\n")
        self.deliver_dataset()
        self.deliver_dataset("challenge-a", "challenge", shift=1)
        self.deliver_dataset("final-a", "final", shift=5)

    def test_sealed_evaluation_summary_tamper_is_rejected(self):
        self.prepare_selection()
        self.add_summary("sealed-evaluation")
        run_dir, _, state = self.snapshot()
        summary_path = run_dir / state["evaluations"]["sealed-evaluation"]
        summary = common.read_json(summary_path)
        summary["score"] = 0
        common.write_json(summary_path, summary)
        with self.assertRaisesRegex(ValueError, "sealed evaluation summary changed"):
            self.command("select", candidate="candidate-a")
        self.assertIsNone(common.read_json(run_dir / "state.json")["selection"])

    def test_sealed_evaluation_records_tamper_is_rejected(self):
        self.prepare_selection()
        summary = self.add_summary("sealed-evaluation")
        run_dir, _, _ = self.snapshot()
        (run_dir / summary["details_file"]).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sealed evaluation records changed"):
            self.snapshot()

    def test_selection_rejects_unregistered_candidate(self):
        self.prepare_selection()
        self.add_summary("ghost-evaluation", candidate_id="ghost-candidate", score=0)
        with self.assertRaisesRegex(ValueError, "unknown candidate or anchor"):
            self.command("select", candidate="ghost-candidate")
        self.assertIsNone(self.snapshot()[2]["selection"])

    def test_selection_requires_complete_valid_anchor_evidence(self):
        self.prepare_selection()
        exclusions = [
            {"all_valid": False}, {"infrastructure_error": True},
            {"cleanup_confirmed": False}, {"probe": True},
            {"dataset": "challenge-a"}, {"dataset": "final-a"}, {"candidate_id": "baseline"},
        ]
        for index, changes in enumerate(exclusions):
            self.add_summary(f"excluded-{index}", candidate="candidate-b", score=0, **changes)
        with self.assertRaisesRegex(ValueError, "no complete valid anchor evaluation"):
            self.command("select", candidate="candidate-b")
        self.add_summary("eligible-a", score=20)
        self.add_summary("eligible-b", candidate="candidate-b", score=30)
        with self.assertRaisesRegex(ValueError, "not the best eligible"):
            self.command("select", candidate="candidate-b")
        result = self.command("select", candidate="candidate-a")
        self.assertEqual(result["evaluation_id"], "eligible-a")
        self.assertEqual(result["score"], 20)
        self.assertEqual(result["anchor_dataset_id"], "anchor-a")
        with self.assertRaisesRegex(ValueError, "selection already frozen"):
            self.command("begin-session", actor="researcher-a")

    def test_final_freeze_waits_for_inflight_measurement(self):
        self.prepare_selection()
        self.add_summary('eligible-a')
        self.add_summary('still-running', candidate='candidate-b', status='running', score=None)
        with self.assertRaisesRegex(ValueError, 'end active measurements'):
            self.command('select',candidate='candidate-a')
        self.assertIsNone(self.snapshot()[2]['selection'])

    def test_candidate_session_and_parent_must_match_recorded_generation(self):
        manifest,_=self.write_candidate()
        directory,_,_=self.snapshot()
        original=common.read_json(directory/manifest)
        for change, reason in (({'session_id':'unknown-r001'},'active research session'),
                               ({'parent_id':'invented-parent'},'unknown parent')):
            common.write_json(directory/manifest,{**original,**change})
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,reason):
                self.command('deliver',kind='candidate',manifest=manifest,module='research')
        self.assertEqual(self.snapshot()[2]['deliveries']['candidate'],{})

    def test_later_generation_cannot_bypass_iteration_with_initial_idea(self):
        self.command('begin-session',actor='author',idea='idea-a')
        checkpoint_session(self, 'author')
        self.command('end-session',actor='author',status='completed',native_id=None)
        with self.assertRaisesRegex(ValueError,'iteration'):
            self.command('begin-session',actor='author',idea='idea-a')
        self.assertEqual(len(self.snapshot()[2]['sessions']),1)


if __name__ == "__main__":
    unittest.main()
