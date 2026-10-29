"""Previous numbered batches remain inspectable without executing former modules."""
import argparse
import unittest

from tools import common, run
from tools.configuration import effective
import test_contracts as helpers


class LegacyBatchTests(unittest.TestCase):
    setUp = helpers.ContractTests.setUp
    command = helpers.ContractTests.command
    snapshot = helpers.ContractTests.snapshot

    def old_batch(self):
        directory, _, state = self.snapshot()
        document=common.read_yaml(directory/'run.yaml')
        document['schema_version']=7
        lock=state['configuration']
        lock['document_sha']=common.digest(document)
        research=lock['modules']['research']
        research['execution']={'mode':'subagent','profile':'research','delegates':{}}
        research['handlers']={'validate_delivery':'validate.py'}
        selections=lock['modules']['selections']
        selections['produces']=['plan']
        selections['contracts']={'plan':'schemas/plan.json'}
        selections['handlers']={'next':'policy.py','select':'policy.py','validate_delivery':'policy.py'}
        state['deliveries'].pop('iteration')
        path='research/selections/old-plan.json'
        plan={'schema_version':1,'plan_id':'old-plan','decision':'continue',
              'assignments':[{'actor':'old-author','idea_id':'idea-a',
                              'parent_candidate':None,'objective':'minimize lateness'}],
              'requests':[],'evidence_ids':[],'reason':'Budget remains.'}
        common.write_json(directory/path,plan)
        state['deliveries']['plan']={'old-plan':{'manifest':plan,'manifest_path':path,
            'files':{path:common.sha(directory/path)}}}
        state['config_sha']=common.digest(effective(document,lock))
        common.write_json(directory/'run.yaml',document)
        common.save_state(directory,state)
        return directory

    def test_prior_numbered_subagent_and_plan_batch_is_read_only(self):
        directory=self.old_batch()
        # Working module changes must not cause execution or rewriting of history.
        (self.root/'modules/research/notebook_collaboration/MODULE.md').write_text('retired working module\n')
        before={str(p.relative_to(directory)):common.sha(p) for p in directory.rglob('*') if p.is_file()}
        self.assertTrue(self.command('check')['historical_read_only'])
        self.assertEqual(self.command('status')['artifacts']['plan'],['old-plan'])
        _,_,state=common.load_run(self.run_id,read_only=True)
        self.assertEqual(state['deliveries']['dataset']['anchor-a']['scope'],'initialization')
        for command,fields in [('begin-session',{'actor':'author','idea':'idea-a'}),
                               ('iteration-context',{'actor':'author'})]:
            with self.subTest(command=command),self.assertRaisesRegex(ValueError,'read-only'):
                self.command(command,**fields)
        with self.assertRaisesRegex(ValueError,'read-only'):
            run.command(argparse.Namespace(command='launch-spec',run=self.run_id,config=None,
                                           module='research',role='researcher'))
        self.assertEqual(before,{str(p.relative_to(directory)):common.sha(p)
                                 for p in directory.rglob('*') if p.is_file()})
        self.assertFalse((directory/'.mutation.lock').exists())

    def test_old_numbered_input_and_document_integrity_still_checked(self):
        directory=self.old_batch()
        shared=self.root/'runs/contract-study/initialization/datasets/anchor-a/cases.jsonl'
        previous=shared.read_bytes()
        shared.write_bytes(previous+b'\n')
        with self.assertRaisesRegex(ValueError,'sealed delivery changed'):
            self.command('check')
        shared.write_bytes(previous)
        document=common.read_yaml(directory/'run.yaml')
        document['purpose']='research'
        common.write_json(directory/'run.yaml',document)
        with self.assertRaisesRegex(ValueError,'historical configuration changed'):
            self.command('check')


if __name__ == '__main__':
    unittest.main()
