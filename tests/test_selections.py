"""Researcher-authored algorithm iterations and independently frozen anchor selection."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import jsonschema

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
from modules.selections.evidence_iteration import policy
from modules.final.frozen_holdout import policy as final
from tools.common import read_json, sha, write_json


class SelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='anysolvers-iteration-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.run = Path(temporary.name)
        self.cfg = {'task': {'objective': {'direction': 'minimize', 'name': 'cost'}},
                    'final': {'authorized': True}}
        self.state = {'config_sha': 'fixed-config', 'sessions': [{'session_id': 'author-r001', 'actor': 'author', 'status': 'active'}], 'selection': None,
                      'evaluations': {}, 'deliveries': {k: {} for k in
                          ('candidate', 'dataset', 'ideas', 'iteration')}}
        self.schema = read_json(PACKAGE_ROOT / 'modules/selections/evidence_iteration/schemas/iteration.json')
        self.dataset('anchor-a')
        self.candidate('candidate-a')
        self.candidate('candidate-b')
        self.state['deliveries']['ideas']['ideas-a'] = {'manifest': {
            'ideas': [{'idea_id': 'idea-a', 'hypothesis': 'Reduce duplicate search states.'}]}}

    def candidate(self, cid):
        relative = 'candidates/' + cid + '/solver.py'
        path = self.run / relative
        path.parent.mkdir(parents=True)
        path.write_text("raise RuntimeError('candidate must never execute: " + cid + "')\n")
        self.state['deliveries']['candidate'][cid] = {
            'manifest': {'candidate_id': cid, 'source_file': relative, 'parent_id': None,
                         'rationale': 'Deduplicate frontier states.', 'session_id': 'author-r001'},
            'files': {relative: sha(path)}}

    def dataset(self, did, purpose='anchor'):
        self.state['deliveries']['dataset'][did] = {'manifest': {'purpose': purpose}}

    def measurement(self, eid, candidate='candidate-a', dataset='anchor-a', score=10, **changes):
        record = self.state['deliveries']['candidate'][candidate]
        summary = {
            'evaluation_id': eid, 'candidate_id': candidate, 'dataset_id': dataset,
            'dataset_purpose': self.state['deliveries']['dataset'][dataset]['manifest']['purpose'],
            'source_sha': record['files'][record['manifest']['source_file']],
            'status': 'completed', 'all_valid': True, 'infrastructure_error': False,
            'cleanup_confirmed': True, 'probe': False, 'score': score}
        summary.update(changes)
        relative = 'evaluations/' + eid + '/summary.json'
        journal_relative = summary.setdefault('details_file', 'evaluations/' + eid + '/results.jsonl')
        journal = self.run / journal_relative
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({'type': 'result', 'case_id': 'fixture-case',
                                       'result': {'valid': summary['all_valid'], 'score': score}}) + '\n')
        summary['results_sha'] = sha(journal)
        write_json(self.run / relative, summary)
        self.state['evaluations'][eid] = relative
        if summary['status'] in ('completed', 'failed'):
            self.state.setdefault('evaluation_hashes', {})[eid] = {
                'summary_sha': sha(self.run / relative), 'results_sha': sha(journal)}
        return summary

    def iteration(self, **changes):
        data = {
            'schema_version': 1, 'iteration_id': 'iteration-a', 'author_session': 'author-r001', 'parent_candidate': None,
            'source_idea_ids': ['idea-a'], 'evidence_ids': [], 'history_iteration_ids': [],
            'observations': 'Repeated frontier states may account for excess work.',
            'hypothesis': 'State deduplication will reduce expansions without changing legality.',
            'algorithm_changes': ['Cache canonical states before frontier insertion.'],
            'expected_effect': 'Fewer duplicate expansions with equivalent valid solutions.',
            'test_plan': 'Compare cached and uncached variants on anchor cases; check legality and expansion counts.',
            'risks': ['Canonicalization may merge distinct states.'],
            'rationale': 'Isolate redundant work before changing the search order.'}
        data.update(changes)
        return data

    def validate(self, data):
        jsonschema.Draft202012Validator(self.schema).validate(data)
        policy.validate_delivery(self.run, self.cfg, self.state, data, lambda path: path, None)

    def test_iteration_is_authored_input_and_does_not_write_or_schedule(self):
        data = self.iteration()
        before = copy.deepcopy(self.state)
        self.validate(data)
        self.assertEqual(self.state, before)
        self.assertFalse((self.run / 'state.json').exists())
        self.assertNotIn('hypothesis', policy.context(self.run, self.cfg, self.state))

    def test_iteration_requires_registered_active_author_and_sealed_continuation(self):
        with self.assertRaisesRegex(ValueError, 'active researcher session'):
            self.validate(self.iteration(author_session='unknown-r001'))
        self.state['sessions'][0]['status'] = 'completed'
        with self.assertRaisesRegex(ValueError, 'active researcher session'):
            self.validate(self.iteration())
        with self.assertRaisesRegex(ValueError, 'sealed researcher iteration'):
            policy.validate_delivery(self.run, self.cfg, self.state, self.iteration(), None, None)
        self.state['deliveries']['iteration']['iteration-a'] = {'manifest': self.iteration()}
        policy.validate_delivery(self.run, self.cfg, self.state, self.iteration(), None, None)

    def test_schema_rejects_empty_algorithm_and_scheduling_fields(self):
        for changes in ({'algorithm_changes': []}, {'algorithm_changes': [' ']},
                        {'hypothesis': ' '}, {'test_plan': ''}, {'actor': 'author'},
                        {'stop': True}, {'assignments': []}, {'requests': []}):
            with self.subTest(changes=changes), self.assertRaises(jsonschema.ValidationError):
                self.validate(self.iteration(**changes))

    def test_requires_provenance_and_unique_references(self):
        for changes in ({'source_idea_ids': []}, {'source_idea_ids': ['idea-a', 'idea-a']}):
            with self.subTest(changes=changes), self.assertRaises(jsonschema.ValidationError):
                self.validate(self.iteration(**changes))
        with self.assertRaisesRegex(ValueError, 'needs an idea'):
            policy.validate_delivery(self.run, self.cfg, self.state,
                                     self.iteration(source_idea_ids=[]), None, None)

    def test_checks_all_reference_types(self):
        for changes, message in (({'source_idea_ids': ['missing']}, 'unknown idea'),
                                 ({'evidence_ids': ['missing']}, 'unknown evaluation'),
                                 ({'history_iteration_ids': ['missing']}, 'unknown history'),
                                 ({'parent_candidate': 'missing'}, 'unknown parent')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.validate(self.iteration(**changes))

    def test_history_only_iteration_and_self_reference(self):
        self.state['deliveries']['iteration']['previous'] = {
            'manifest': self.iteration(iteration_id='previous')}
        self.validate(self.iteration(source_idea_ids=[], history_iteration_ids=['previous']))
        self.state['deliveries']['iteration']['iteration-a'] = {'manifest': self.iteration()}
        with self.assertRaisesRegex(ValueError, 'unknown history'):
            self.validate(self.iteration(history_iteration_ids=['iteration-a']))

    def test_unevaluated_parent_needs_no_invented_score(self):
        self.validate(self.iteration(parent_candidate='candidate-a'))

    def test_failed_and_nonbest_parents_can_be_improved(self):
        self.measurement('best-score', candidate='candidate-b', score=1)
        self.measurement('failed-score', status='failed', score=None, all_valid=False)
        self.validate(self.iteration(parent_candidate='candidate-a', evidence_ids=['failed-score']))
        self.measurement('worse-score', score=20)
        self.validate(self.iteration(parent_candidate='candidate-a', evidence_ids=['worse-score']))

    def test_parent_evidence_is_required_and_must_match_parent(self):
        self.measurement('parent-score')
        self.measurement('other-score', candidate='candidate-b')
        for evidence in ([], ['other-score']):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, 'parent development'):
                self.validate(self.iteration(parent_candidate='candidate-a', evidence_ids=evidence))
        self.validate(self.iteration(parent_candidate='candidate-a', evidence_ids=['parent-score']))

    def test_probe_and_challenge_failures_are_diagnostic_evidence(self):
        self.dataset('challenge-a', 'challenge')
        self.measurement('probe-score', probe=True)
        self.measurement('failed-score', dataset='challenge-a', status='failed', score=None,
                         all_valid=False, infrastructure_error=True)
        self.validate(self.iteration(source_idea_ids=[], evidence_ids=['probe-score', 'failed-score']))
        context = policy.context(self.run, self.cfg, self.state)
        self.assertEqual({s['evidence_role'] for s in context['evaluations']}, {'diagnostic'})
        self.state['sessions'] = []
        with self.assertRaisesRegex(ValueError, 'no complete valid anchor'):
            final.select(self.run, self.cfg, self.state, 'candidate-a')

    def test_running_evidence_rejected_and_not_in_context(self):
        self.measurement('running-score', status='running')
        self.assertEqual(policy.context(self.run, self.cfg, self.state)['evaluations'], [])
        with self.assertRaisesRegex(ValueError, 'not complete'):
            self.validate(self.iteration(evidence_ids=['running-score']))
        # A pending measurement has no score yet and cannot become a required citation.
        self.validate(self.iteration(parent_candidate='candidate-a'))

    def test_final_content_never_enters_research_context(self):
        self.dataset('final-a', 'final')
        self.measurement('secret-final', dataset='final-a', score=0, error='secret marker')
        self.measurement('dev-score', candidate='candidate-b', score=40)
        context = policy.context(self.run, self.cfg, self.state)
        self.assertEqual([s['evaluation_id'] for s in context['evaluations']], ['dev-score'])
        self.assertNotIn('secret', str(context))
        with self.assertRaisesRegex(ValueError, 'final evaluation'):
            self.validate(self.iteration(evidence_ids=['secret-final']))

    def test_final_manifest_is_checked_even_if_summary_claims_challenge(self):
        self.dataset('final-a', 'final')
        self.measurement('secret-final', dataset='final-a', dataset_purpose='challenge')
        self.assertEqual(policy.context(self.run, self.cfg, self.state)['evaluations'], [])
        with self.assertRaisesRegex(ValueError, 'final evaluation'):
            self.validate(self.iteration(evidence_ids=['secret-final']))

    def test_other_active_research_does_not_block_context_or_iteration(self):
        self.state['sessions'] = [{'session_id': 'other-r001', 'actor': 'other', 'status': 'active'},
                                  {'session_id': 'author-r001', 'actor': 'author', 'status': 'active'}]
        before = copy.deepcopy(self.state)
        self.validate(self.iteration())
        context = policy.context(self.run, self.cfg, self.state, actor='author')
        self.assertEqual(context['actor_history'], [{'session_id': 'author-r001', 'actor': 'author', 'status': 'active'}])
        self.assertEqual(self.state, before)
        self.assertNotIn('actor_history', policy.context(self.run, self.cfg, self.state))

    def test_selection_frozen_rejects_new_context_and_iteration(self):
        self.state['selection'] = {'candidate_id': 'candidate-a'}
        with self.assertRaisesRegex(ValueError, 'selection already frozen'):
            self.validate(self.iteration())
        with self.assertRaisesRegex(ValueError, 'selection already frozen'):
            policy.context(self.run, self.cfg, self.state)

    def test_actor_context_links_generations_candidates_and_development_evidence(self):
        first = {'session_id': 'author-r001', 'actor': 'author', 'generation': 1,
                 'status': 'completed', 'idea_id': 'idea-a', 'iteration_id': None,
                 'parent_candidate': None}
        second = {'session_id': 'author-r002', 'actor': 'author', 'generation': 2,
                  'status': 'active', 'idea_id': None, 'iteration_id': 'iteration-a',
                  'parent_candidate': 'candidate-a', 'native_id': 'private-native-id',
                  'spawn_options': {'model': 'private-launch-settings'}}
        self.state['sessions'] = [first, second,
                                  {'session_id': 'other-r001', 'actor': 'other', 'status': 'active'}]
        self.state['deliveries']['candidate']['candidate-a']['manifest']['session_id'] = first['session_id']
        self.state['deliveries']['candidate']['candidate-b']['manifest']['session_id'] = 'other-r001'
        self.measurement('author-score', candidate='candidate-a', details_file='private/details.jsonl')
        before = copy.deepcopy(self.state)
        context = policy.context(self.run, self.cfg, self.state, actor='author')
        self.assertEqual(context['actor_history'], [first, {key: second[key] for key in first}])
        by_session = {candidate['session_id']: candidate['candidate_id']
                      for candidate in context['candidates']}
        self.assertEqual(by_session[first['session_id']], 'candidate-a')
        evidence = context['evaluations'][0]
        self.assertEqual(evidence['candidate_id'], by_session[first['session_id']])
        self.assertEqual(evidence['evaluation_id'], 'author-score')
        self.assertNotIn('private', str(context))
        self.assertNotIn('source_file', str(context))
        self.assertEqual(self.state, before)

    def test_unsealed_terminal_metrics_are_refused_in_context_and_iterations(self):
        for status in ('completed', 'failed'):
            with self.subTest(status=status):
                self.measurement('interrupted', status=status)
                del self.state['evaluation_hashes']['interrupted']
                with self.assertRaisesRegex(ValueError, 'unsealed evaluation'):
                    policy.context(self.run, self.cfg, self.state)
                with self.assertRaisesRegex(ValueError, 'unsealed evaluation'):
                    self.validate(self.iteration(evidence_ids=['interrupted']))

    def test_changed_summary_or_journal_cannot_be_research_feedback(self):
        summary = self.measurement('sealed')
        summary_path = self.run / self.state['evaluations']['sealed']
        original = summary_path.read_bytes()
        write_json(summary_path, {**summary, 'score': 0})
        with self.assertRaisesRegex(ValueError, 'unsealed evaluation'):
            policy.context(self.run, self.cfg, self.state)
        summary_path.write_bytes(original)
        (self.run / summary['details_file']).write_text('changed journal')
        with self.assertRaisesRegex(ValueError, 'unsealed evaluation'):
            self.validate(self.iteration(evidence_ids=['sealed']))

    def test_evidence_provenance_is_checked(self):
        cases = ({'evaluation_id': 'wrong'}, {'source_sha': 'wrong'}, {'candidate_id': 'ghost'},
                 {'dataset_id': 'ghost'}, {'dataset_purpose': 'challenge'})
        for changes in cases:
            with self.subTest(changes=changes):
                self.state['evaluations'] = {}
                self.measurement('broken', **changes)
                with self.assertRaises(ValueError):
                    self.validate(self.iteration(evidence_ids=['broken']))

    def test_context_is_compact_and_comparison_label_is_not_ranking(self):
        self.measurement('good', details_file='private/details.jsonl')
        result = policy.context(self.run, self.cfg, self.state)
        self.assertEqual(result['evaluations'][0]['evidence_role'], 'anchor_comparison')
        self.assertNotIn('details_file', result['evaluations'][0])
        self.assertNotIn('source_file', result['candidates'][0])
        self.assertEqual(len(result['ideas']), 1)
        self.assertNotIn('selection', result)

    def test_final_selection_preserves_anchor_source_and_tie_rules(self):
        self.state['sessions'] = []
        a = self.measurement('score-a', score=20)
        b = self.measurement('score-b', candidate='candidate-b', score=20)
        best = min((a, b), key=lambda s: (s['score'], s['source_sha'], s['evaluation_id']))
        loser = 'candidate-b' if best['candidate_id'] == 'candidate-a' else 'candidate-a'
        with self.assertRaisesRegex(ValueError, 'not the best eligible'):
            final.select(self.run, self.cfg, self.state, loser)
        selected = final.select(self.run, self.cfg, self.state, best['candidate_id'])
        self.assertEqual(selected['evaluation_id'], best['evaluation_id'])
        self.assertEqual(read_json(self.run / 'state.json')['selection'], selected)
        self.assertEqual(read_json(self.run / 'events.jsonl')['kind'], 'selected')

    def test_final_selection_eval_id_breaks_same_source_score_tie(self):
        self.state['sessions'] = []
        self.measurement('score-z')
        self.measurement('score-a')
        self.assertEqual(final.select(self.run, self.cfg, self.state, 'candidate-a')['evaluation_id'], 'score-a')

    def test_final_selection_waits_for_active_research_and_measurements(self):
        self.state['sessions'] = []
        self.measurement('score-a')
        self.state['sessions'] = [{'actor': 'author', 'status': 'active'}]
        with self.assertRaisesRegex(ValueError, 'end active research sessions'):
            final.select(self.run, self.cfg, self.state, 'candidate-a')
        self.state['sessions'] = []
        self.measurement('running-score', status='running')
        with self.assertRaisesRegex(ValueError, 'end active measurements'):
            final.select(self.run, self.cfg, self.state, 'candidate-a')

    def test_final_selection_rejects_every_ineligible_result(self):
        self.state['sessions'] = []
        self.dataset('challenge-a', 'challenge')
        self.dataset('final-a', 'final')
        exclusions = [{'status': 'failed'}, {'all_valid': False}, {'infrastructure_error': True},
                      {'cleanup_confirmed': False}, {'probe': True}, {'dataset': 'challenge-a'},
                      {'dataset': 'final-a'}, {'candidate_id': 'baseline'}]
        for index, changes in enumerate(exclusions):
            self.measurement('excluded-' + str(index), score=0, **changes)
        with self.assertRaisesRegex(ValueError, 'no complete valid anchor'):
            final.select(self.run, self.cfg, self.state, 'candidate-a')

    def test_final_selection_rejects_bad_source_and_nonfinite_scores(self):
        self.state['sessions'] = []
        for changes, message in (({'source_sha': 'wrong'}, 'source hash mismatch'),
                                 ({'candidate_id': 'ghost'}, 'unknown candidate or anchor'),
                                 ({'score': True}, 'finite score')):
            with self.subTest(changes=changes):
                self.state['evaluations'] = {}
                self.measurement('bad', **changes)
                with self.assertRaisesRegex(ValueError, message):
                    final.select(self.run, self.cfg, self.state, 'candidate-a')

    def test_maximize_chooses_largest_eligible_anchor_score(self):
        self.state['sessions'] = []
        self.cfg['task']['objective']['direction'] = 'maximize'
        self.measurement('score-a', score=10)
        self.measurement('score-b', candidate='candidate-b', score=20)
        result = final.select(self.run, self.cfg, self.state, 'candidate-b')
        self.assertEqual(result['score'], 20)

    def test_final_gate_keeps_authorization_frozen_hash_and_probe_rules(self):
        self.state['sessions'] = []
        self.measurement('score-a')
        final.select(self.run, self.cfg, self.state, 'candidate-a')
        source = self.state['selection']['source_sha']
        final.gate(self.cfg, self.state, {'purpose': 'final'}, 'candidate-a', source, False)
        final.gate(self.cfg, self.state, {'purpose': 'final'}, 'baseline', 'baseline-hash', False)
        for candidate, source_sha, probe in (('candidate-b', source, False),
                                             ('candidate-a', 'wrong', False),
                                             ('candidate-a', source, True)):
            with self.subTest(candidate=candidate, source=source_sha, probe=probe), self.assertRaises(ValueError):
                final.gate(self.cfg, self.state, {'purpose': 'final'}, candidate, source_sha, probe)
        self.cfg['final']['authorized'] = False
        with self.assertRaises(ValueError):
            final.gate(self.cfg, self.state, {'purpose': 'final'}, 'candidate-a', source, False)


if __name__ == '__main__':
    unittest.main()
