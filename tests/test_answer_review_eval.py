"""Check paired measurements and scoring offline through the actual workflow.

Semantic quality labels are external data, never taken from the production judge.
No provider credentials, embeddings, catalogue HTTP or persisted index are needed.
"""

from contextlib import nullcontext
import unittest
from unittest.mock import Mock

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from backend.generation.answers import AnswerDraft, Claim
from backend.generation.llm import LLMConfig
from backend.retrieval.retrieval import SearchResult
from backend.workflow.review_policy import LEGACY_REVIEW_POLICY
from backend.workflow.state import AnswerReview, Decomposition, RouteDecision
from evals.experiments.answer_review import run_case
from evals.measurement import UsageRecorder, validate_cases
from evals.experiments.review_metrics import summarize
from evals.experiments.review_report import render_report


class ReviewEvaluationTests(unittest.TestCase):
    """Protect fair pairing, measurement boundaries and non-circular scoring."""

    def run_fixture(self, repair=False, probe=None):
        """Use a small real graph with scripted responses and deterministic evidence."""
        source = SearchResult('r1', 'rule', 'First strike', 'First strike acts earlier.', 1, 1, 'test')
        retriever = Mock()
        retriever.search.return_value = [source]
        retriever.get.return_value = []
        draft = AnswerDraft(can_answer=True,
                            claims=[Claim(text='Earlier damage.', source_ids=['r1'])], assumptions=[])
        approved = AnswerReview(supported=True, unsupported_claims=[], missing_information=[], followup_queries=[])
        rejected = AnswerReview(supported=False, unsupported_claims=['Missing support.'],
                                missing_information=[], followup_queries=['first strike'])
        outputs = [RouteDecision(language='en', english_query='first strike', route='rules',
                                 card_names=[], clarification=''), Decomposition(queries=['first strike'])]
        if probe != 'empty_initial_context':
            outputs.append(draft)
        if repair or probe == 'empty_initial_context':
            outputs.extend([rejected, draft, approved])
        else:
            outputs.append(approved)
        llm = Mock(config=LLMConfig())
        llm.generate.side_effect = outputs
        case = {'id': 'case', 'question': 'What is first strike?', 'cohort': 'natural',
                'expectation': 'answer', 'probe': probe}
        # These paired v1 probes intentionally exercise the legacy experiment policy.
        return run_case(case, retriever, llm, repetition=1, context_budget=1000,
                        review_policy=LEGACY_REVIEW_POLICY,
                        api_factory=lambda: nullcontext(Mock(max_pages=1)))

    def test_three_paired_snapshots_count_prefixes_not_separate_runs(self):
        for repair in (False, True):
            with self.subTest(repair=repair):
                sample = self.run_fixture(repair)
                variants = sample['variants']
                self.assertEqual(variants['baseline']['model_calls'], 3)
                self.assertEqual(variants['review_only']['model_calls'], 4)
                self.assertEqual(variants['final']['model_calls'], 6 if repair else 4)
                self.assertEqual(variants['final']['search_calls'], 4 if repair else 2)
                self.assertEqual(variants['baseline']['answer'], variants['final']['answer'])
                self.assertEqual(variants['review_only']['can_answer'], not repair)
                self.assertEqual(sample['additional_retrievals'], int(repair))
                self.assertFalse(sample['context_budget_exceeded'])
                self.assertEqual(variants['final']['calls_with_usage'], 0)

    def test_probes_are_isolated_and_empty_context_restores_retrieval(self):
        invalid = self.run_fixture(probe='invalid_citation')
        self.assertTrue(invalid['probe_applied'])
        self.assertEqual(invalid['variants']['baseline']['reason'], 'invalid_citations_or_answer')
        self.assertFalse(invalid['variants']['final']['can_answer'])
        normal = self.run_fixture()
        self.assertTrue(normal['variants']['baseline']['can_answer'])
        empty = self.run_fixture(probe='empty_initial_context')
        self.assertEqual(empty['variants']['baseline']['reason'], 'empty_context')
        searches = [c for c in empty['retrieval_calls'] if c['operation'] == 'search']
        self.assertEqual([c['withheld'] for c in searches], [True, True, False, False])
        self.assertEqual(empty['variants']['baseline']['real_search_calls'], 0)
        self.assertEqual(empty['variants']['final']['real_search_calls'], 2)
        self.assertTrue(empty['variants']['final']['can_answer'])
        self.assertEqual(empty['variants']['final']['model_calls'], 5)

    def test_usage_callback_retains_counts_without_message_content(self):
        usage = UsageRecorder()
        message = AIMessage(content='Private content must not be recorded.', usage_metadata={
            'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13})
        usage.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
        self.assertEqual(usage.records, [{'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13}])

    def test_quality_requires_external_labels_and_separates_safe_rejection(self):
        sample = self.run_fixture()
        self.assertIsNone(summarize([sample])['natural']['quality'])
        judgments = {'case:r1': {'baseline': 'incorrect', 'review_only': 'abstained',
                                 'final': 'abstained', 'note': 'The cited text does not support the extra premise.'}}
        quality = summarize([sample], judgments)['natural']['quality']
        self.assertEqual(quality['corrected_answers'], 0)
        self.assertEqual(quality['safer_rejections'], 1)
        judgments['case:r1']['final'] = 'correct'
        quality = summarize([sample], judgments)['natural']['quality']
        self.assertEqual(quality['corrected_answers'], 1)
        self.assertEqual(quality['task_success_rate']['final'], 1)
        judgments['case:r1'].update(baseline='incomplete', final='incorrect')
        quality = summarize([sample], judgments)['natural']['quality']
        self.assertEqual(quality['introduced_wrong_answers'], 1)
        with self.assertRaises(ValueError):
            summarize([sample], {'other:r1': judgments['case:r1']})

    def test_measurements_report_real_limit_violations_instead_of_hiding_them(self):
        sample = self.run_fixture()
        sample['cohort'] = 'controlled'
        sample['additional_retrievals'] = 2
        sample['executed_repair_queries'] = ['a', 'b', 'c', 'd']
        sample['context_budget_exceeded'] = True
        metrics = summarize([sample])['controlled']
        self.assertEqual(metrics['round_limit_violations'], 1)
        self.assertEqual(metrics['query_limit_violations'], 1)
        self.assertEqual(metrics['context_budget_violations'], 1)

    def test_portable_report_escapes_saved_answer_content(self):
        sample = self.run_fixture()
        sample['question'] = '<script>untrusted()</script>'
        html, summary = render_report({'samples': [sample], 'settings': {'repeats': 1}})
        self.assertNotIn('<script>untrusted()</script>', html)
        self.assertIn('&lt;script&gt;untrusted()', html)
        self.assertIsNone(summary['natural']['quality'])
        self.assertIn('Sin valorar', html)

    def test_missing_reference_fails_before_any_paid_operation(self):
        retriever = Mock()
        retriever.get.return_value = []
        bank = {'version': 'test', 'cases': [{'id': 'case', 'question': 'Question',
                'criteria': ['Grounded answer'], 'cohort': 'natural', 'expectation': 'answer',
                'reference_ids': ['missing']}]}
        with self.assertRaisesRegex(ValueError, 'Missing reference'):
            validate_cases(bank, retriever)
        retriever.search.assert_not_called()
