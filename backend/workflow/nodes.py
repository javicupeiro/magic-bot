"""Implement the work performed by each named graph node.

A node receives the accumulated state and returns a dictionary containing only
its updates. graph.py decides the next node; these methods do not call each other.
route, decompose and generate request structured model output. The cards subgraph
owns model/tool execution; check_cards checks its evidence. retrieve embeds queries;
expand, validate and clarify do not call the language model.

Existing services own HTTP filtering, vector search and citation checks. This
module coordinates those services and keeps retrieved facts separate from prose.
"""

import json

from backend.generation.answers import AnswerDraft, finish_answer
from backend.generation.llm import StructuredOutputError
from backend.common.prompts import load_prompt
from backend.workflow.references import bounded_context, expand_references
from backend.workflow.state import Decomposition, RouteDecision


class WorkflowNodes:
    """Group node functions around the dependencies for a single graph execution.

    retriever offers semantic search and exact rule lookup; llm wraps a LangChain model; api is an already-open catalogue client owned by the caller. Limits
    apply across the relevant stages, and versioned prompts load when their node runs.
    This class holds dependencies, while the graph state holds question results.
    """

    def __init__(self, retriever, llm, api, *, max_calls, k, version, context_budget):
        """Capture dependencies and limits for this run."""
        self.retriever, self.llm, self.api = retriever, llm, api
        self.max_calls, self.k, self.version = max_calls, k, version
        self.context_budget = context_budget
        self.prompts = {}

    def route(self, state):
        """Read question; return a validated decision and prompt provenance.

        JSON quoting keeps user text a distinct data value inside the prompt. The
        original question stays in state; only decision.english_query is normalized.
        Prompt hashes make the instructions used for this result identifiable later.
        """
        self.prompts["route"] = load_prompt("route")
        decision = self.llm.generate(self.prompts["route"].render(
            question_json=json.dumps(state["question"], ensure_ascii=False)), RouteDecision)
        if not decision.english_query.strip():
            raise ValueError("Normalized question must not be empty")
        return {"decision": decision, "card_names": decision.card_names,
                "prompts": {key: dict(value.metadata()) for key, value in self.prompts.items()}}

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
        return {"verified_cards": verified_cards, "blocked": blocked,
                "reason": reason, "answer": text,
                "prompts": {**state.get("prompts", {}), **state["card_prompts"]}}

    def decompose(self, state):
        """Read normalized intent and verified cards; return distinct retrieval queries.

        The model proposes one query for a definition and up to three for a complex
        interaction. Pydantic limits list length; local checks reject whitespace-only
        strings. Deduplication preserves the model's original query order.
        """
        self.prompts["decompose"] = load_prompt("decompose")
        queries = self.llm.generate(self.prompts["decompose"].render(
            question_json=json.dumps(state["decision"].english_query),
            cards_json=json.dumps(state["verified_cards"], ensure_ascii=False)), Decomposition).queries
        if any(not query.strip() for query in queries):
            raise ValueError("Retrieval queries must not be empty")
        return {"queries": list(dict.fromkeys(query.strip() for query in queries)),
                "prompts": {**state.get("prompts", {}), "decompose": dict(self.prompts["decompose"].metadata())}}

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
        for query in state["queries"]:
            for kind in ("rule", "glossary"):
                sources.extend(self.retriever.search(query, k=self.k, kind=kind, version=self.version))
        # Card records and rules share the total serialized context budget.
        budget = self._rule_budget(state)
        return {"context": bounded_context(sources, max(0, budget))}

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
        self.prompts["answer"] = load_prompt("answer")
        try:
            draft = self.llm.generate(self.prompts["answer"].render(
                question_json=json.dumps(state["question"], ensure_ascii=False),
                language=state["decision"].language,
                context_json=json.dumps(evidence, ensure_ascii=False)), AnswerDraft)
        except StructuredOutputError:
            return {"draft": AnswerDraft(can_answer=False, claims=[], assumptions=[]),
                    "reason": "unavailable_structured_answer",
                    "prompts": {**state.get("prompts", {}), "answer": dict(self.prompts["answer"].metadata())}}
        return {"draft": draft, "prompts": {**state.get("prompts", {}),
                "answer": dict(self.prompts["answer"].metadata())}}

    def validate(self, state):
        """Read the draft/context; return checked citations and final answer text.

        Membership and stored text are checked by validate_citations. A single
        invalid citation discards the entire draft. Markers are rendered from checked
        IDs, never copied from model-written text. This is structural validation;
        it cannot prove that the model interpreted the evidence correctly.
        """
        return finish_answer(state["draft"], state["context"],
                             state["decision"].language, state.get("reason"))

    def clarify(self, state):
        """Read the routing decision; finish with its clarification or a fallback.

        Classification has already called the model. This terminal node itself
        performs no model call, rule retrieval or catalogue request.
        """
        return {"answer": state["decision"].clarification or (
            "Aclara tu pregunta sobre Magic: The Gathering."
            if state["decision"].language == "es" else
            "Please clarify your Magic: The Gathering question."), "reason": "clarification"}
