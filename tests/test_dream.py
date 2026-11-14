"""Domain wiki, independent evidence collection and opt-in selection context."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dream import sources, wiki
import test_collaboration as fixtures
from tools import common as c, research


class WikiTests(unittest.TestCase):
    def test_same_page_consolidation_repeat_and_concurrent_human_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'dream/wiki'
            wiki.initialize(root)
            name = 'topics/限时搜索.md'
            first = '# 限时搜索\n\n## 有证据的发现\n\nA 减少超时。\n'.encode()
            self.assertEqual(wiki.publish(root, name, 'new', first), 'updated')
            path = root / name
            original_sha = c.sha(path)
            path.write_bytes(first + '人工修正：限于大实例。\n'.encode())
            with self.assertRaisesRegex(ValueError, 'reread'):
                wiki.publish(root, name, original_sha, first)
            revised = path.read_bytes() + 'B 中观察到反例。\n'.encode()
            self.assertEqual(wiki.publish(root, name, c.sha(path), revised), 'updated')
            previous = path.stat().st_mtime_ns
            self.assertEqual(wiki.publish(root, name, c.sha(path), revised), 'unchanged')
            self.assertEqual(path.stat().st_mtime_ns, previous)
            wiki.initialize(root)
            self.assertEqual(list((root/'topics').iterdir()), [path])
            self.assertEqual(path.read_bytes(), revised)
            with self.assertRaises(ValueError):
                wiki.page_path(root, '../DREAM.md')


class DomainTests(unittest.TestCase):
    setUpBase = fixtures.CollaborationTests.setUpBase
    command = fixtures.CollaborationTests.command
    snapshot = fixtures.CollaborationTests.snapshot
    write_candidate = fixtures.CollaborationTests.write_candidate
    fake = fixtures.CollaborationTests.fake
    draft = fixtures.CollaborationTests.draft
    request = fixtures.CollaborationTests.request
    setUp = fixtures.CollaborationTests.setUp

    def test_opt_in_through_both_entry_points(self):
        sid = self.session['session_id']
        def context(**kwargs):
            return research.command(self.run_id, 'context', session_id=sid, **kwargs)['development']
        before = context()
        self.assertEqual(context(wiki_query='邻域'), before)
        root = self.root / 'dream/wiki'
        self.assertFalse(root.exists())
        wiki.initialize(root)
        page = root / 'topics/search.md'
        page.write_text('# 限时搜索\n\n## 有证据的发现\n邻域过大会超时。[^obs]\n\n'
                        '## 待验证的猜想\n缩小邻域可能有效。\n\n[^obs]: example/001 eval-id hash\n')
        self.assertEqual(context(), before)
        self.assertEqual(context(wiki_query='knapsack'), before)
        match = context(wiki_query='邻域')['wiki_suggestions']
        self.assertEqual([r['section'] for r in match], ['有证据的发现', '待验证的猜想'])
        self.assertEqual(match[0]['sources'], ['example/001 eval-id hash'])
        self.assertEqual(match, self.command('iteration-context', actor='r1', wiki_query='邻域')['wiki_suggestions'])
        self.assertEqual(context(), before)
        page.unlink()
        page.symlink_to(self.root / 'runs' / self.run_id / 'state.json')
        self.assertEqual(context(wiki_query='邻域'), before)

    def test_final_handoff_checks_inline_citation_and_records_worker(self):
        with self.assertRaisesRegex(ValueError, 'delivered report'):
            self.command('final-dream')
        fixtures.CollaborationTests.candidate(self)
        fixtures.CollaborationTests.evaluate(self)
        fixtures.CollaborationTests.share(self)
        fixtures.CollaborationTests.note(self)
        self.command('end-session', actor='r1', status='completed', native_id='/root/old')
        directory = self.snapshot()[0]
        (directory/'final/report.md').write_text('Private final report.')
        c.write_json(directory/'final/report.json', dict(schema_version=1, report_id='report',
                     report_file='final/report.md', evaluation_ids=[]))
        delivery = self.command('deliver', kind='report', manifest='final/report.json', module='final')
        self.assertEqual(delivery['next']['operation'], 'final-dream')
        job = self.command('final-dream')
        self.assertEqual(job['spawn_options']['fork_turns'], 'none')
        self.assertNotIn('Private final report', str(job))
        self.command('start-final-dream', native_id='/root/dream', replaces=None)
        self.assertEqual(self.command('final-dream')['status'], 'active')
        with self.assertRaisesRegex(ValueError, 'existing'):
            self.command('start-final-dream', native_id='/root/other', replaces=None)
        with self.assertRaisesRegex(ValueError, 'registered'):
            self.command('finish-final-dream', native_id='/root/other', page=None)
        citation = job['materials']['evaluations']['eval1']['citation']
        root = self.root/'dream/wiki'
        wiki.initialize(root)
        page = 'topics/搜索.md'
        note = f'[^a]: {citation}\n'
        path = root/page
        path.write_text('## 有证据的发现\n\n无引用。\n\n'+note)
        with self.assertRaisesRegex(ValueError, 'cite'):
            self.command('finish-final-dream', native_id='/root/dream', page=page)
        path.write_text('## 有证据的发现\n\n观察到结果。[^a]\n\n'+note)
        receipt = self.command('finish-final-dream', native_id='/root/dream', page=page)
        self.assertEqual(receipt['citations'], [citation])
        self.assertEqual(self.command('final-dream'), {'status':'completed', 'receipt':receipt})

    def test_unsupported_ideas_labeled_in_filename_body_and_search(self):
        root = self.root/'dream/wiki'
        wiki.initialize(root)
        name = 'topics/无证据猜想-分组预算.md'
        body = '# 无证据猜想：分组预算\n\n## 无证据猜想\n\n分组预算能增加覆盖吗？目前没有直接证据。\n'.encode()
        for wrong_name, wrong_text in [(name, b'# budget'), ('topics/budget.md', body)]:
            with self.assertRaisesRegex(ValueError, '无证据猜想'):
                wiki.publish(root, wrong_name, 'new', wrong_text)
        wiki.publish(root, name, 'new', body)
        self.assertNotIn('wiki_suggestions', self.command('iteration-context', actor='r1'))
        result = self.command('iteration-context', actor='r1', wiki_query='预算')['wiki_suggestions']
        self.assertEqual([(s['section'], s['sources']) for s in result], [('无证据猜想', [])])

    def test_share_a_specific_sealed_method_report(self):
        fixtures.CollaborationTests.candidate(self)
        fixtures.CollaborationTests.evaluate(self)
        sid = self.session['session_id']
        self.draft('notebook', dict(id='close', candidate_id='c1', evidence_ids=['eval1'],
                   learned='Local note', failed_or_uncertain='Open question', next_iteration_id=None))
        self.command('end-session', actor='r1', status='completed', native_id='/root/old')
        listing = sources.collect(self.run_id)
        self.assertEqual(listing['report_sessions'], [sid])
        self.assertEqual(listing['evaluations'], {})
        shared = sources.collect(self.run_id, report_ids=[sid])
        self.assertEqual(shared['reports'][sid]['method']['candidate_id'], 'c1')
        self.assertEqual(shared['evaluations']['eval1']['score'], 1)
        self.assertNotIn('Local note', str(shared))

    def test_only_shared_evidence_with_portable_citation_and_revision_check(self):
        fixtures.CollaborationTests.candidate(self)
        fixtures.CollaborationTests.evaluate(self)
        self.assertFalse(sources.collect(self.run_id)['evaluations'])
        fixtures.CollaborationTests.share(self)
        item = sources.collect(self.run_id, 'eval1', cases=True)
        batch, eid, expected = item['citation'].split()
        self.assertEqual([r['case_id'] for r in item['cases']], ['case-a'])
        self.assertEqual(sources.collect(batch, eid, expected)['score'], item['score'])
        with self.assertRaisesRegex(ValueError, 'revision changed'):
            sources.collect(batch, eid, 'outdated')
        state_file = self.root / 'runs' / batch / 'state.json'
        state = c.read_json(state_file)
        state['evaluations']['final-secret'] = 'final/evaluations/missing/summary.json'
        state['stale_evaluations'] = [eid]
        c.write_json(state_file, state)
        self.assertEqual(sources.collect(batch)['evaluations'], {eid: {'available': False}})
        with self.assertRaisesRegex(ValueError, 'no current'):
            sources.collect(batch, eid)
        with tempfile.TemporaryDirectory() as tmp:
            moved = Path(tmp)
            shutil.copytree(self.root/'runs', moved/'runs')
            with patch.object(c, 'ROOT', moved):
                self.assertEqual(sources.collect(batch)['evaluations'], {eid: {'available': False}})
            tool = Path(wiki.__file__)
            subprocess.run([sys.executable, '-B', tool, 'init'], check=True,
                           env={**os.environ, 'ANYSOLVERS_PROJECT_ROOT': tmp})
            self.assertTrue((moved/'dream/wiki/README.md').exists())
            self.assertFalse((moved/'dream/wiki/sources.yaml').exists())
