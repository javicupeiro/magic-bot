"""Implement the work performed by each named graph node.

A node receives the accumulated state and returns a dictionary containing only
its updates. graph.py decides the next node; these methods do not call each other.
route, decompose and generate request structured model output. The cards subgraph
owns model/tool execution; check_cards checks its evidence. retrieve embeds queries;
review and repair use the same model; validation and finalization stay local.

Existing services own HTTP filtering, vector search and citation checks. This
module coordinates those services and keeps retrieved facts separate from prose.
"""

import json

from backend.generation.answers import AnswerDraft, finish_answer
from backend.generation.llm import StructuredOutputError
from backend.common.prompts import load_prompt
from backend.workflow.references import bounded_context, expand_references
from backend.workflow.state import AnswerReview, EvidenceReview, Decomposition, RouteDecision
from backend.workflow.review_policy import DEFAULT_REVIEW_POLICY, compact_card
from itertools import zip_longest


class WorkflowNodes:
    """Group node functions around the dependencies for a single graph execution.

    retriever offers semantic search and exact rule lookup; llm wraps a LangChain model; api is an already-open catalogue client owned by the caller. Limits
    apply across the relevant stages, and versioned prompts load when their node runs.
    This class holds dependencies, while the graph state holds question results.
    """

    def __init__(self, retriever, llm, api, *, max_calls, k, version, context_budget, review_policy=DEFAULT_REVIEW_POLICY):
        """Capture dependencies and limits for this run."""
        self.retriever, self.llm, self.api = retriever, llm, api
        self.max_calls, self.k, self.version = max_calls, k, version
        self.context_budget = context_budget
        self.review_policy = review_policy

    def route(self, state):
        """Read question; return a validated decision and prompt provenance.

        JSON quoting keeps user text a distinct data value inside the prompt. The
        original question stays in state; only decision.english_query is normalized.
        Prompt hashes make the instructions used for this result identifiable later.
        """
        prompt = load_prompt("route")
        decision = self.llm.generate(prompt.render(
            question_json=json.dumps(state["question"], ensure_ascii=False),
            history_json=json.dumps([message.model_dump() for message in state.get("history", [])],
                                    ensure_ascii=False)), RouteDecision)
        if not decision.english_query.strip():
            raise ValueError("Normalized question must not be empty")
        return {"decision": decision, "card_names": decision.card_names,
                "resolved_question": decision.english_query,
                "prompts": {"route": dict(prompt.metadata())}}

    def check_cards(self, state):
        """Accept exact catalogue evidence before continuing a rules interaction.

        The cards subgraph has already executed tools and produced its reply.
        This node only checks evidence; it never calls a model or the API.
        """
        decision = state["decision"]
        reply = state["card_reply"]
        blocked = reply.needs_clarification
        reason = "card_clarification" if blocked else None
        # Preserve every returned card for inspection, but select stricter
        # evidence before allowing a rules question to proceed.
        verified_cards = []
        if decision.route == "rules":
            # Require one successful exact call per requested name, or an explicit
            # clarification. A proposed correction must itself be exactly verified.
            exact_calls = [exchange for exchange in state["exchanges"]
                           if exchange["name"] == "get_card_by_name"
                           and exchange["result"].get("status") == "ok"
                           and not exchange["result"].get("partial")
                           and not exchange["result"].get("error")
                           and len(exchange["result"].get("items", [])) == 1]
            verified_cards = [card for exchange in exact_calls for card in exchange["result"]["items"]]
            resolved_names = {json.loads(exchange["arguments"])["name"].casefold()
                              for exchange in exact_calls}
            # Require coverage of every originally requested name. A successful
            # lookup for a different proposed spelling alone does not clear this.
            complete = all(name.casefold() in resolved_names for name in decision.card_names)
            has_text = all(card.get("text") for exchange in exact_calls
                           for card in exchange["result"]["items"])
            if not blocked and (not complete or not has_text):
                blocked, reason = True, "unresolved_card"
        # Return data and terminal status separately from the prose. graph.py
        # reads blocked to decide whether decomposition is allowed to run.
        text = reply.text
        if reason == "unresolved_card":
            text = ("Necesito el nombre exacto y texto verificado de las cartas para resolver la interacción."
                    if decision.language == "es" else
                    "I need exact card names and verified card text to resolve this interaction.")
        if self.review_policy.compact_cards:
            verified_cards = [compact_card(card) for card in verified_cards]
            wrapper = json.dumps({"rules": [], "verified_cards": verified_cards}, ensure_ascii=False)
            if len(wrapper) > self.context_budget:
                blocked, reason = True, "card_evidence_budget_exceeded"
                text = ("La evidencia de cartas excede el límite; reduce la interacción."
                        if decision.language == "es" else
                        "Card evidence exceeds the limit; narrow the interaction.")
        return {"verified_cards": verified_cards, "blocked": blocked,
                "reason": reason, "answer": text,
                "prompts": {**state.get("prompts", {}), **state["card_prompts"]}}

    def decompose(self, state):
        """Read normalized intent and verified cards; return distinct retrieval queries.

        The model proposes one query for a definition and up to three for a complex
        interaction. Pydantic limits list length; local checks reject whitespace-only
        strings. Deduplication preserves the model's original query order.
        """
        prompt = load_prompt("decompose")
        queries = self.llm.generate(prompt.render(
            question_json=json.dumps(state["decision"].english_query),
            cards_json=json.dumps(state["verified_cards"], ensure_ascii=False)), Decomposition).queries
        if any(not query.strip() for query in queries):
            raise ValueError("Retrieval queries must not be empty")
        return {"queries": list(dict.fromkeys(query.strip() for query in queries)),
                "prompts": {**state.get("prompts", {}), "decompose": dict(prompt.metadata())}}

    def _rule_budget(self, state):
        """Reserve card data and the final evidence object's JSON wrapper first."""
        wrapper = json.dumps({"rules": [], "verified_cards": state["verified_cards"]},
                             ensure_ascii=False)
        # context_size already counts the two brackets of the rules array.
        return self.context_budget - (len(wrapper) - 2)

    def retrieve(self, state):
        """Read queries; return deduplicated rule/glossary evidence in context.

        k is per query and per kind, not a final document count. The existing
        retriever handles embeddings and ranking. bounded_context then removes
        repeated IDs and keeps complete records within the remaining rule budget.
        """
        sources = []
        groups = []
        for query in state["queries"]:
            for kind in ("rule", "glossary"):
                hits = self.retriever.search(query, k=self.k, kind=kind, version=self.version)
                groups.append(hits)
                sources.extend(hits)
        if self.review_policy.balanced_context:
            sources = [source for row in zip_longest(*groups) for source in row if source is not None]
        # Card records and rules share the total serialized context budget.
        budget = self._rule_budget(state)
        return {"context": bounded_context(sources, max(0, int(budget * .8) if self.review_policy.balanced_context else budget))}

    def expand(self, state):
        """Read initial context; replace it with bounded one-hop-expanded context.

        References use exact rule lookup in the referring document's edition. Added
        records share the same budget as semantic hits and verified-card evidence.
        """
        budget = self._rule_budget(state)
        return {"context": expand_references(state["context"], self.retriever,
                                              budget=max(0, budget))}

    def generate(self, state):
        """Read question and evidence; return an untrusted structured answer draft.

        Only selected rules and verified cards enter this prompt. With no rules,
        abstain without another generation request. Missing structured output also
        becomes an explicit abstention; other provider/network errors propagate.
        Citations are not accepted here: validate() is the next graph stage.
        """
        if not state["context"]:
            return {"draft": AnswerDraft(can_answer=False, claims=[], assumptions=[]),
                    "reason": "empty_context"}
        evidence = {"rules": [source.to_dict() for source in state["context"]],
                    "verified_cards": state["verified_cards"]}
        prompt = load_prompt("answer")
        try:
            draft = self.llm.generate(prompt.render(
                question_json=json.dumps(state.get("resolved_question") or state["question"], ensure_ascii=False),
                language=state["decision"].language,
                context_json=json.dumps(evidence, ensure_ascii=False)), AnswerDraft)
        except StructuredOutputError:
            return {"draft": AnswerDraft(can_answer=False, claims=[], assumptions=[]),
                    "reason": "unavailable_structured_answer",
                    "prompts": {**state.get("prompts", {}), "answer": dict(prompt.metadata())}}
        return {"draft": draft, "prompts": {**state.get("prompts", {}),
                "answer": dict(prompt.metadata())}}

    def validate(self, state):
        """Read draft/context; return checked citations and provisional answer text.

        Membership and stored text are checked by validate_citations. A single
        invalid citation discards the entire draft. Markers are rendered from checked
        IDs, never copied from model-written text. This is structural validation;
        it cannot prove that the model interpreted the evidence correctly.
        """
        finished = finish_answer(state["draft"], state["context"],
                                 state["decision"].language, state.get("reason"))
        return {**finished, "candidate_draft": state["draft"]}

    def _review_values(self, state):
        """Serialize the same bounded evidence and original proposal for both roles."""
        evidence = {"rules": [source.to_dict() for source in state["context"]],
                    "verified_cards": state["verified_cards"]}
        return {"question_json": json.dumps(state.get("resolved_question") or state["question"], ensure_ascii=False),
                "context_json": json.dumps(evidence, ensure_ascii=False),
                "draft_json": state["candidate_draft"].model_dump_json(),
                "validation_json": json.dumps({"error": state["validation_error"],
                                                "reason": state.get("reason")})}

    def review(self, state):
        """Read the checked draft and evidence; record a brief model verdict.

        The same node runs after repair, but graph.py checks the state counter
        before permitting retrieval. Missing structured output fails closed.
        Python also rejects contradictory approvals and invalid citations.
        """
        prompt = load_prompt("review", self.review_policy.prompt_version)
        unavailable = False
        try:
            verdict = self.llm.generate(prompt.render(**self._review_values(state)),
                                        EvidenceReview if self.review_policy.prompt_version == "v2" else AnswerReview)
        except StructuredOutputError:
            unavailable = True
            verdict = AnswerReview(supported=False, unsupported_claims=[],
                                   missing_information=["Structured evidence review unavailable."],
                                   followup_queries=[])
        approved = (getattr(verdict, "action", "approve") == "approve"
                    and verdict.supported and not verdict.unsupported_claims
                    and not verdict.missing_information and not state["validation_error"]
                    and state["draft"].can_answer)
        # Filter rather than truncate long queries: truncation can change intent.
        queries = list(dict.fromkeys(query.strip() for query in verdict.followup_queries
                                    if 0 < len(query.strip()) <= 300))[:3]
        review_reason = "unsupported_answer"
        if approved:
            review_reason = "approved"
        elif unavailable:
            review_reason = "unavailable_review"
        record = {**verdict.model_dump(), "approved": approved,
                  "validation_error": state["validation_error"], "reason": review_reason,
                  "additional_retrievals": state["additional_retrievals"]}
        action = getattr(verdict, "action", None)
        if action in {"abstain", "approve", "repair"}:
            queries = []
        repair_allowed = (self.review_policy.local_repair and not unavailable
                          and action != "abstain" and not approved
                          and (state["candidate_draft"].can_answer or action == "repair"))
        return {"repair_allowed": repair_allowed, "review": verdict, "reviews": [*state["reviews"], record],
                "repair_queries": queries,
                "prompts": {**state.get("prompts", {}), "review": dict(prompt.metadata())}}

    def retrieve_more(self, state):
        """Spend the single additional round on at most three follow-up queries.

        Keep previously cited evidence first, then new hits, then other old hits.
        This reserves space for missing evidence without silently dropping support
        for preserved claims. Complete records still share the original budget.
        No second reference expansion or hidden retrieval loop runs here.
        """
        if state["additional_retrievals"] >= 1:
            raise ValueError("Additional retrieval budget exhausted")
        sources = []
        for query in state["repair_queries"][:3]:
            for kind in ("rule", "glossary"):
                sources.extend(self.retriever.search(query, k=self.k, kind=kind, version=self.version))
        cited_ids = {identifier for claim in state["candidate_draft"].claims
                     for identifier in claim.source_ids}
        preserved = [source for source in state["context"] if source.id in cited_ids]
        context = bounded_context([*preserved, *sources, *state["context"]],
                                  max(0, self._rule_budget(state)))
        return {"context": context, "additional_retrievals": state["additional_retrievals"] + 1}

    def repair(self, state):
        """Read objections and updated evidence; replace only the answer proposal.

        The prompt asks to preserve supported parts. This is probabilistic, so the
        next nodes must validate citations and review the complete result again.
        Structured-output failure becomes an abstention, never another attempt.
        """
        prompt = load_prompt("repair", self.review_policy.prompt_version)
        values = {**self._review_values(state), "language": state["decision"].language,
                  "review_json": state["review"].model_dump_json()}
        reason = None
        try:
            draft = self.llm.generate(prompt.render(**values), AnswerDraft)
        except StructuredOutputError:
            draft = AnswerDraft(can_answer=False, claims=[], assumptions=[])
            reason = "unavailable_structured_repair"
        return {"draft": draft, "reason": reason, "repairs": state.get("repairs", 0) + 1,
                "prompts": {**state.get("prompts", {}), "repair": dict(prompt.metadata())}}

    def finalize(self, state):
        """Publish approved prose or discard claims after rejection/budget exhaustion.

        reviews retains factual objections for inspection. User prose uses the
        shared presentation policy, rather than exposing model reasoning text.
        """
        if state["reviews"][-1]["approved"]:
            return {"reason": None}
        reason = state.get("reason") or (
            "review_budget_exhausted" if state.get("repairs", 0) or state["additional_retrievals"] else
            state["reviews"][-1]["reason"])
        finished = finish_answer(AnswerDraft(can_answer=False, claims=[], assumptions=[]),
                                 state["context"], state["decision"].language, reason)
        # Final abstention is valid itself; retain the proposal's rejection too.
        return {**finished, "validation_error": state["validation_error"]}

    def clarify(self, state):
        """Read the routing decision; finish with its clarification or a fallback.

        Classification has already called the model. This terminal node itself
        performs no model call, rule retrieval or catalogue request.
        """
        return {"answer": state["decision"].clarification or (
            "Aclara tu pregunta sobre Magic: The Gathering."
            if state["decision"].language == "es" else
            "Please clarify your Magic: The Gathering question."), "reason": "clarification"}
