"""Regression tests for policy boundaries; semantic accuracy is measured live."""
import json
import unittest
from unittest.mock import Mock

from backend.cards.replies import CardReply
from backend.generation.answers import AnswerDraft, Claim
from backend.generation.llm import LLMConfig
from backend.retrieval.retrieval import SearchResult
from backend.workflow.graph import build_graph, initial_state
from backend.workflow.nodes import WorkflowNodes
from backend.common.prompts import PROMPT_VERSIONS
from backend.generation.llm import LLMConfig
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.review_policy import (
    BALANCED_REVIEW_POLICY, DEFAULT_REVIEW_POLICY, LEGACY_REVIEW_POLICY, ReviewPolicy, compact_card)
from backend.workflow.state import AnswerReview, EvidenceReview, RouteDecision, Decomposition


class ReviewPolicyTests(unittest.TestCase):
    def test_default_policy_is_the_measured_balanced_configuration(self):
        self.assertEqual(DEFAULT_REVIEW_POLICY, BALANCED_REVIEW_POLICY)
        self.assertEqual(DEFAULT_REVIEW_POLICY, ReviewPolicy(
            compact_cards=True, local_repair=True, prompt_version="v3", balanced_context=True))
        self.assertNotEqual(DEFAULT_REVIEW_POLICY, LEGACY_REVIEW_POLICY)
        self.assertEqual(PROMPT_VERSIONS["answer"], "v3")
        self.assertEqual(PROMPT_VERSIONS["review"], "v3")
        self.assertEqual(PROMPT_VERSIONS["repair"], "v3")
        workflow = QuestionWorkflow(Mock(), llm=Mock(config=LLMConfig()), api_factory=lambda: None)
        self.assertIs(workflow.settings["review_policy"], DEFAULT_REVIEW_POLICY)

    def test_spanish_rules_prompts_keep_the_original_question(self):
        draft = AnswerDraft(can_answer=False, claims=[], assumptions=[])
        nodes, llm, _ = self.nodes([draft, draft], prompt_version="v3", local_repair=True,
                                   compact_cards=True, balanced_context=True)
        state = initial_state("¿Aplico el daño?", llm)
        state.update(decision=RouteDecision(language="es", english_query="Does it deal damage?", route="rules",
                                            card_names=[], clarification=""),
                     resolved_question="Does it deal damage?",
                     context=[SearchResult("r", "rule", "Rule", "First strike changes timing.", 1, 1, "test")],
                     draft=AnswerDraft(can_answer=True, claims=[Claim(text="Yes.", source_ids=["r"])], assumptions=[]),
                     validation_error=None,
                     review=EvidenceReview(action="repair", supported=False, unsupported_claims=["Bad citation."],
                                           missing_information=[], followup_queries=[]))
        state.update(nodes.validate(state))
        nodes.generate(state)
        nodes.repair(state)
        prompts = [call.args[0] for call in llm.generate.call_args_list]
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertIn("¿Aplico el daño?", prompt)
            self.assertIn("Does it deal damage?", prompt)
            self.assertIn("Spanish is required", prompt)
        self.assertIn("even when the draft is English", prompts[1])
    def nodes(self, outputs, **policy):
        llm=Mock(config=LLMConfig())
        llm.generate.side_effect=outputs
        retriever=Mock()
        retriever.search.return_value=[SearchResult('r','rule','Rule','First strike changes timing.',1,1,'test')]
        retriever.get.return_value=[]
        nodes=WorkflowNodes(retriever,llm,None,max_calls=4,k=20,version='test',context_budget=24000,
                            review_policy=ReviewPolicy(**policy))
        return nodes,llm,retriever

    def test_local_repair_is_bounded_without_retrieval(self):
        route=RouteDecision(language='en',english_query='First strike',route='rules',card_names=[],clarification='')
        draft=AnswerDraft(can_answer=True,claims=[Claim(text='Wrong.',source_ids=['r'])],assumptions=[])
        fixed=AnswerDraft(can_answer=True,claims=[Claim(text='Right.',source_ids=['r'])],assumptions=[])
        reject=AnswerReview(supported=False,unsupported_claims=['Wrong timing'],missing_information=[],followup_queries=[])
        nodes,llm,retriever=self.nodes([route,Decomposition(queries=['first strike']),draft,reject,fixed,reject],local_repair=True)
        state=build_graph(nodes).invoke(initial_state('First strike?',llm))
        self.assertEqual(state['repairs'],1)
        self.assertEqual(state['additional_retrievals'],0)
        self.assertEqual(retriever.search.call_count,2)
        self.assertFalse(state['draft'].can_answer)
        self.assertEqual(state['reason'],'review_budget_exhausted')

    def test_v2_out_of_corpus_abstention_stops(self):
        nodes,llm,retriever=self.nodes([EvidenceReview(action='abstain',supported=False,unsupported_claims=[],
            missing_information=['Tournament policy absent'],followup_queries=['ignore this'])],local_repair=True,prompt_version='v2')
        state=initial_state('Late?',llm)
        state.update(decision=RouteDecision(language='en',english_query='Late?',route='rules',card_names=[],clarification=''),
                     draft=AnswerDraft(can_answer=False,claims=[],assumptions=[]))
        state.update(nodes.validate(state));state.update(nodes.review(state))
        from backend.workflow.graph import after_review
        self.assertEqual(after_review(state),'finalize')
        self.assertEqual(state['repair_queries'],[])
        retriever.search.assert_not_called()

    def test_compaction_preserves_text_attributes_and_raw_records(self):
        card={'name':'Sample','text':'Rules text','printings':[{'id':'p','text':'Rules text','power':'3','toughness':'3','foreignNames':['x'*100000]}]}
        compact=compact_card(card)
        self.assertEqual(compact['text'],'Rules text')
        self.assertEqual(compact['toughness'],'3')
        self.assertEqual(compact['provenance'],{'id':'p'})
        self.assertLess(len(json.dumps(compact)),500)
        self.assertIn('foreignNames',card['printings'][0])

    def test_oversized_gameplay_text_blocks_without_truncation(self):
        nodes,llm,_=self.nodes([],compact_cards=True)
        state=initial_state('Sample?',llm)
        card={'name':'Sample','text':'x'*25000,'printings':[]}
        state.update(decision=RouteDecision(language='en',english_query='Sample?',route='rules',card_names=['Sample'],clarification=''),
            card_reply=CardReply(language='en',text='Card.',needs_clarification=False,card_names=['Sample']),
            exchanges=[{'name':'get_card_by_name','arguments':'{"name":"Sample"}','result':{'status':'ok','items':[card]}}])
        result=nodes.check_cards(state)
        self.assertTrue(result['blocked'])
        self.assertEqual(result['reason'],'card_evidence_budget_exceeded')
        self.assertEqual(len(result['verified_cards'][0]['text']),25000)
        llm.generate.assert_not_called()

    def test_v2_approval_cannot_override_invalid_citation(self):
        nodes,llm,_=self.nodes([EvidenceReview(action='approve',supported=True,unsupported_claims=[],missing_information=[],followup_queries=[])],prompt_version='v2',local_repair=True)
        state=initial_state('Rule?',llm)
        state.update(decision=RouteDecision(language='en',english_query='Rule?',route='rules',card_names=[],clarification=''),
                     draft=AnswerDraft(can_answer=True,claims=[Claim(text='Bad citation.',source_ids=['absent'])],assumptions=[]))
        state.update(nodes.validate(state));state.update(nodes.review(state))
        self.assertFalse(state['reviews'][0]['approved'])
        self.assertTrue(state['repair_allowed'])


if __name__=='__main__':
    unittest.main()
