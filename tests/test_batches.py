"""Cross-batch isolation using real sealed inputs and a mocked compute boundary."""
import copy
import shutil
import unittest
from unittest.mock import patch
import yaml

import test_contracts as helpers
from tools import common, measure, run, sandbox, study


class BatchTests(unittest.TestCase):
    setUp = helpers.ContractTests.setUp
    command = helpers.ContractTests.command
    snapshot = helpers.ContractTests.snapshot
    write_candidate = helpers.ContractTests.write_candidate
    deliver_candidate = helpers.ContractTests.deliver_candidate
    write_dataset = helpers.ContractTests.write_dataset
    deliver_dataset = helpers.ContractTests.deliver_dataset

    def second(self):
        return run.init('contract-study', 'configs/smoke.yaml')['run_id']

    def test_shared_inputs_do_not_share_result_cache_or_candidates(self):
        first, second = self.run_id, self.second()
        initialization = self.root/'runs/contract-study/initialization'
        before = {str(p.relative_to(initialization)):common.sha(p)
                  for p in initialization.rglob('*') if p.is_file()}
        success = {'valid':True,'score':2.0,'solution':{},'metrics':{},'wall_s':0.1,
                   'queue_wait_s':0.0,'infrastructure_error':False,'cleanup_confirmed':True}
        with patch.object(sandbox, 'execute', return_value=success) as execute:
            for ref in (first,second):
                self.run_id=ref
                self.deliver_candidate(source="raise RuntimeError('must not execute on host')\n")
                result=measure.evaluate(ref,'candidate-a','anchor-a','eval-a')
                self.assertTrue(result['all_valid'])
                cached=measure.evaluate(ref,'candidate-a','anchor-a','eval-a')
                self.assertTrue(cached['cached'])
            self.assertEqual(execute.call_count,2)
        for ref in (first,second):
            directory,cfg,state=common.load_run(ref)
            self.assertEqual(state['evaluations']['eval-a'],'research/evaluations/eval-a/summary.json')
            self.assertEqual(state['deliveries']['dataset']['anchor-a']['scope'],'initialization')
            self.assertFalse((directory/'initialization').exists())
            self.assertFalse((directory/'datasets').exists())
            stored=common.read_json(directory/'state.json')
            self.assertEqual(stored['deliveries']['dataset'],{})
            self.assertEqual(stored['deliveries']['ideas'],{})
        self.assertEqual(before,{str(p.relative_to(initialization)):common.sha(p)
                                 for p in initialization.rglob('*') if p.is_file()})

    def test_dynamic_data_and_ideas_stay_in_one_batch(self):
        second=self.second()
        self.deliver_dataset('challenge-a','challenge',shift=2)
        directory,_,_=self.snapshot()
        common.write_json(directory/'research/ideas/new.json',{'schema_version':1,'ideas':[{
            'idea_id':'new-idea','hypothesis':'New hypothesis','mechanism':'New rule',
            'first_change':'Implement rule','risks':'Might fail','source_refs':[]}]})
        self.command('deliver',kind='ideas',manifest='research/ideas/new.json',module='ideas')
        _,_,other=common.load_run(second)
        self.assertNotIn('challenge-a',other['deliveries']['dataset'])
        self.assertNotIn('new',other['deliveries']['ideas'])
        shared=study.load('contract-study',require_sealed=True)[1]
        self.assertNotIn('challenge-a',shared['deliveries']['dataset'])
        self.assertNotIn('new',shared['deliveries']['ideas'])

    def test_shared_input_tamper_invalidates_all_batches(self):
        second=self.second()
        data=self.root/'runs/contract-study/initialization/datasets/anchor-a/cases.jsonl'
        data.write_text(data.read_text()+'\n')
        for ref in (self.run_id,second):
            with self.subTest(ref=ref),self.assertRaisesRegex(ValueError,'changed'):
                common.load_run(ref)
        with self.assertRaisesRegex(ValueError,'changed'):
            self.second()
        self.assertFalse((self.root/'runs/contract-study/003').exists())

    def test_changed_manifest_cannot_create_different_initialization_in_same_study(self):
        path=self.root/'runs/contract-study/initialization/manifest.json'
        data=common.read_json(path);data['source']['sha']='0'*64
        common.write_json(path,data)
        with self.assertRaisesRegex(ValueError,'differs from an existing batch'):
            self.second()
        with self.assertRaisesRegex(ValueError,'shared initialization manifest changed'):
            self.snapshot()

    def test_numbering_does_not_reuse_drafts_and_rejects_ambiguous_refs(self):
        draft=self.root/'runs/contract-study/003';draft.mkdir()
        common.write_json(draft/'run.yaml',{'schema_version':8,'extends':'configs/smoke.yaml'})
        self.assertEqual(self.second(),'contract-study/004')
        for ref in ('contract-study/000','contract-study/01','contract-study/../001',
                    '/contract-study/001','contract-study/001/extra','contract-study/initialization'):
            with self.subTest(ref=ref),self.assertRaises(ValueError):common.run_path(ref)
        with self.assertRaises(FileNotFoundError):common.load_run('contract-study')

    def test_batch_cannot_reference_another_study_or_wrong_output_area(self):
        draft=self.root/'runs/contract-study/002';draft.mkdir()
        common.write_json(draft/'run.yaml',{'schema_version':8,'extends':'configs/smoke.yaml',
            'initialization':'runs/another-study/initialization/manifest.json'})
        with self.assertRaisesRegex(ValueError,'own study'):
            run.init('contract-study/002')
        self.assertFalse((draft/'state.json').exists())
        manifest,source=self.write_candidate()
        directory,_,_=self.snapshot()
        data=common.read_json(directory/manifest)
        common.write_json(directory/'final/candidate.json',data)
        with self.assertRaisesRegex(ValueError,'research/candidates'):
            self.command('deliver',kind='candidate',manifest='final/candidate.json',module='research')
        data['source_file']='final/not-a-research-source.py'
        (directory/data['source_file']).write_text('raise RuntimeError()')
        common.write_json(directory/manifest,data)
        with self.assertRaisesRegex(ValueError,'research/candidates'):
            self.command('deliver',kind='candidate',manifest=manifest,module='research')

    def test_persisted_shared_scope_cannot_relabel_final_data(self):
        directory,_,view=self.snapshot()
        raw=common.read_json(directory/'state.json')
        forged=copy.deepcopy(view['deliveries']['dataset']['final-a'])
        forged['manifest']['purpose']='challenge'
        raw['deliveries']['dataset']['forged-development']=forged
        # Bypass save_state intentionally to emulate corrupt on-disk metadata.
        common.write_json(directory/'state.json',raw)
        with self.assertRaisesRegex(ValueError,'persisted deliveries'):
            self.snapshot()

    def test_shared_reads_can_overlap_and_numeric_symlinks_are_rejected(self):
        second=self.second()
        directory=self.root/'runs/contract-study'
        with study._lock(directory,exclusive=False):
            self.snapshot()
            common.load_run(second)
        outside=self.root/'unrelated';outside.mkdir()
        common.write_json(outside/'state.json',{'private':'not a batch'})
        (directory/'003').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlink'):
            self.second()
        self.assertFalse((directory/'004').exists())

    def test_incompatible_batch_schema_rejected_before_state_creation(self):
        directory=self.root/'modules/datasets/incompatible'
        shutil.copytree(self.root/helpers.method_path('datasets'), directory)
        schema=common.read_json(directory/'schemas/dataset.json')
        schema['required'].append('new_required')
        common.write_json(directory/'schemas/incompatible.json',schema)
        _,header,body=(directory/'MODULE.md').read_text().split('---',2)
        metadata=yaml.safe_load(header)
        metadata['contracts']['dataset']='schemas/incompatible.json'
        (directory/'MODULE.md').write_text('---\n'+yaml.safe_dump(metadata)+'---\n'+body)
        draft=self.root/'runs/contract-study/002';draft.mkdir()
        common.write_json(draft/'run.yaml',{'schema_version':8,'extends':'configs/smoke.yaml',
            'modules':{'datasets':{'use':'modules/datasets/incompatible'}}})
        with self.assertRaisesRegex(ValueError,'incompatible output contract'):
            run.init('contract-study/002')
        self.assertEqual([p.name for p in draft.iterdir()],['run.yaml'])


if __name__=='__main__':unittest.main()
