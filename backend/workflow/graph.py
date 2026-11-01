"""Route one question through a rules pipeline or a native card-tool subgraph.

The cards subgraph contains observable model/tools/collect/finish steps. Its
result is checked before an interaction proceeds to rules retrieval. SQLite
retains complete conversation history; each turn replaces transient evidence.
"""

from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from backend.cards.agent import build_card_graph
from backend.cards.mtg_api import MtgApiClient
from backend.generation.llm import LLM
from backend.retrieval.retrieval import DEFAULT_K
from backend.workflow.context import select_history
from backend.workflow.memory import DEFAULT_MEMORY_PATH, conversation_turn, open_memory
from backend.workflow.nodes import WorkflowNodes
from backend.workflow.review_policy import DEFAULT_REVIEW_POLICY
from backend.workflow.state import WorkflowState


def after_route(state):
    """Return the next node name after the model's structured classification.

    A rules question with named cards must resolve those cards first. Generic
    ability questions can go directly to decomposition without catalogue HTTP.
    """
    decision = state["decision"]
    if decision.route == "clarify":
        return "clarify"
    return "cards" if decision.route == "cards" or decision.card_names else "decompose"


def after_cards(state):
    """Stop card-only searches and unresolved interactions; continue resolved rules.

    Every terminal route records the completed turn before reaching END.
    """
    return "remember" if state["blocked"] or state["decision"].route == "cards" else "decompose"


def after_review(state):
    """Python approves, spends one retrieval round, or stops with abstention.

    Model queries are only proposals. The final review cannot reopen recovery,
    even when it requests more searches or contradicts the citation validator.
    """
    if state["reviews"][-1]["approved"]:
        return "finalize"
    if state.get("repairs", 0) >= 1:
        return "finalize"
    if state["additional_retrievals"] == 0 and state["repair_queries"]:
        return "retrieve_more"
    if state.get("repair_allowed", False):
        return "repair"
    return "finalize"


def _traced(name, node):
    """Wrap a node with an inspectable trace while preserving its state updates.

    inputs/outputs list field names, not their full contents. Counts, query text
    and source IDs provide a compact view of progress. Since trace has no append
    reducer, the wrapper explicitly returns the previous entries plus a new one.
    """
    def run(state):
        """Run the wrapped transformation and attach one trace entry."""
        update = node(state)
        # Summarize the resulting state without mutating the input dictionary.
        merged = {**state, **update}
        summary = {"node": name, "inputs": sorted(key for key in state if key != "trace"),
                   "outputs": sorted(update), "cards": len(merged.get("cards", [])),
                   "queries": merged.get("queries", []),
                   "sources": [source.id for source in merged.get("context", [])],
                   "reason": merged.get("reason"),
                   "additional_retrievals": merged.get("additional_retrievals", 0)}
        if name == "review":
            summary["review"] = merged["reviews"][-1]
        update["trace"] = [*state.get("trace", []), summary]
        return update
    return run


def build_graph(nodes: WorkflowNodes, checkpointer=None):
    """Register nodes and edges, then return an executable compiled graph.

    add_edge always follows the same path. add_conditional_edges calls a Python
    routing function and follows its returned destination. The cards node is a compiled
    subgraph with a bounded model/tool cycle.
    """
    # 1. Declare the shared fields, then register named state transformations.
    graph = StateGraph(WorkflowState)
    for name in ("route", "check_cards", "decompose", "retrieve", "expand", "generate", "validate", "review",
                 "retrieve_more", "repair", "finalize", "clarify"):
        graph.add_node(name, _traced(name, getattr(nodes, name)))
    graph.add_node("cards", build_card_graph(nodes.api, nodes.llm, max_calls=nodes.max_calls))
    graph.add_edge("cards", "check_cards")
    # 2. Every invocation starts with classification; branch only where needed.
    def remember(state):
        """Append only completed user/assistant prose, keeping tools turn-local."""
        return {"messages": [*state.get("messages", []), HumanMessage(state["question"]),
                             AIMessage(state["answer"])]}

    graph.add_node("remember", remember)
    graph.add_edge("remember", END)
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", after_route, ["cards", "decompose", "clarify"])
    graph.add_conditional_edges("check_cards", after_cards, ["remember", "decompose"])
    # 3. Fixed evidence stages precede the explicitly budgeted review/repair cycle.
    for source, target in (("decompose", "retrieve"), ("retrieve", "expand"),
                           ("expand", "generate"), ("generate", "validate"),
                           ("validate", "review"), ("retrieve_more", "repair"),
                           ("repair", "validate"), ("finalize", "remember"), ("clarify", "remember")):
        graph.add_edge(source, target)
    graph.add_conditional_edges("review", after_review, ["retrieve_more", "repair", "finalize"])
    return graph.compile(checkpointer=checkpointer)


def initial_state(question, llm):
    """Prepare replaceable turn fields without erasing stored conversation messages."""
    return {"decision": None, "resolved_question": "", "draft": None,
            "candidate_draft": None, "review": None, "card_reply": None,
            "answer": "", "prompts": {}, "card_prompts": {}, "history": [],
            "question": question, "cards": [], "verified_cards": [], "exchanges": [],
            "model": llm.config.model, "reasoning_effort": llm.config.reasoning_effort,
            "tool_messages": [], "card_names": [], "queries": [], "context": [],
            "citations": [], "reviews": [], "repair_queries": [],
            "additional_retrievals": 0, "repairs": 0, "repair_allowed": False, "validation_error": None, "blocked": False, "reason": None, "trace": []}


class QuestionWorkflow:
    """Public entry point coordinating dependencies, limits and resource ownership.

    retriever supplies search() and get(). llm uses native LangChain messages. api_factory creates a context-managed catalogue client per open workflow.
    Tests inject fake services through these same parameters without changing the
    graph. k applies per query and source kind; context_budget counts serialized
    evidence characters, not tokens or the complete provider request size.
    """

    def __init__(self, retriever, *, llm=None, api_factory=MtgApiClient,
                 max_calls=4, k=DEFAULT_K, version=None, context_budget=24000,
                 memory_path=DEFAULT_MEMORY_PATH, history_budget=8000, review_policy=DEFAULT_REVIEW_POLICY):
        """Validate application limits before opening clients or invoking a model."""
        if type(max_calls) is not int or not 1 <= max_calls <= 10:
            raise ValueError("max_calls must be an integer between 1 and 10")
        if type(k) is not int or k <= 0:
            raise ValueError("k must be a positive integer")
        if type(context_budget) is not int or context_budget < 100:
            raise ValueError("context_budget must be an integer of at least 100 characters")
        if type(history_budget) is not int or history_budget < 100:
            raise ValueError("history_budget must be at least 100 characters")
        self.memory_path = Path(memory_path)
        self.history_budget = history_budget
        self._resources = None
        self.graph = None
        self.retriever, self.llm = retriever, llm if llm is not None else LLM()
        self.api_factory = api_factory
        self.settings = dict(max_calls=max_calls, k=k, version=version, context_budget=context_budget, review_policy=review_policy)

    def __enter__(self):
        """Prepare one durable graph; keep HTTP and SQLite open until exit."""
        if self._resources is not None:
            raise RuntimeError("Workflow is already open")
        with ExitStack() as resources:
            saver = resources.enter_context(open_memory(self.memory_path))
            api = resources.enter_context(self.api_factory())
            nodes = WorkflowNodes(self.retriever, self.llm, api, **self.settings)
            self.graph = build_graph(nodes, saver)
            self._resources = resources.pop_all()
        return self

    def __exit__(self, *exc):
        """Release owned resources; never retain a graph with a closed client."""
        self.graph = None
        resources, self._resources = self._resources, None
        if resources is not None:
            resources.__exit__(*exc)

    @staticmethod
    def new_conversation():
        """Allocate an opaque local thread ID without writing an empty checkpoint."""
        return str(uuid4())

    def _prepare_turn(self, question, conversation_id):
        """Load full history under the turn lock and reset every transient field."""
        if not question.strip():
            raise ValueError("Question must not be empty")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a nonempty string")
        config = {"configurable": {"thread_id": conversation_id}, "recursion_limit": 64}
        previous = self.graph.get_state(config).values
        messages = previous.get("messages", [])
        selected = select_history(messages, HumanMessage(question), self.history_budget)
        state = initial_state(question, self.llm)
        state.update(messages=messages, history=selected[:-1], conversation_id=conversation_id)
        return state, config

    def answer(self, question: str, *, conversation_id=None) -> WorkflowState:
        """Run a turn in the supplied conversation, or allocate a new conversation.

        Use a with block to reuse the compiled graph across turns. Standalone
        calls open and close resources automatically. Returned state includes the
        conversation_id needed to resume later, including after process restart.
        """
        if self.graph is None:
            with self:
                return self.answer(question, conversation_id=conversation_id)
        conversation_id = conversation_id if conversation_id is not None else self.new_conversation()
        with conversation_turn(self.memory_path, conversation_id):
            state, config = self._prepare_turn(question, conversation_id)
            return self.graph.invoke(state, config)

    def stream(self, question: str, *, conversation_id=None):
        """Yield native updates while holding resources and the conversation lock.

        Close the iterator if stopping early. Only a completed turn reaches
        remember; a later invocation starts afresh after any interrupted turn.
        Supply an ID from new_conversation() to resume an event-only session.
        """
        if self.graph is None:
            with self:
                yield from self.stream(question, conversation_id=conversation_id)
            return
        conversation_id = conversation_id if conversation_id is not None else self.new_conversation()
        with conversation_turn(self.memory_path, conversation_id):
            state, config = self._prepare_turn(question, conversation_id)
            yield from self.graph.stream(state, config, stream_mode="updates", subgraphs=True)
