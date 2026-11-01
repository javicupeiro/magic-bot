"""Route one question through a rules pipeline or a native card-tool subgraph.

The cards subgraph contains observable model/tools/collect/finish steps. Its
result is checked before an interaction proceeds to rules retrieval. No state is
persisted between invocations yet; conversation persistence is planned for a later iteration.
"""

from langgraph.graph import END, START, StateGraph

from backend.cards.agent import build_card_graph
from backend.cards.mtg_api import MtgApiClient
from backend.generation.llm import LLM
from backend.workflow.nodes import WorkflowNodes
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

    END is LangGraph's termination marker, not an executable application node.
    """
    return END if state["blocked"] or state["decision"].route == "cards" else "decompose"


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
                   "reason": merged.get("reason")}
        update["trace"] = [*state.get("trace", []), summary]
        return update
    return run


def build_graph(nodes: WorkflowNodes):
    """Register nodes and edges, then return an executable compiled graph.

    add_edge always follows the same path. add_conditional_edges calls a Python
    routing function and follows its returned destination. The cards node is a compiled
    subgraph with a bounded model/tool cycle.
    """
    # 1. Declare the shared fields, then register named state transformations.
    graph = StateGraph(WorkflowState)
    for name in ("route", "check_cards", "decompose", "retrieve", "expand", "generate", "validate", "clarify"):
        graph.add_node(name, _traced(name, getattr(nodes, name)))
    graph.add_node("cards", build_card_graph(nodes.api, nodes.llm, max_calls=nodes.max_calls))
    graph.add_edge("cards", "check_cards")
    # 2. Every invocation starts with classification; branch only where needed.
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", after_route, ["cards", "decompose", "clarify"])
    graph.add_conditional_edges("check_cards", after_cards, [END, "decompose"])
    # 3. Once card resolution succeeds, the rules pipeline follows fixed steps.
    for source, target in (("decompose", "retrieve"), ("retrieve", "expand"),
                           ("expand", "generate"), ("generate", "validate"),
                           ("validate", END), ("clarify", END)):
        graph.add_edge(source, target)
    return graph.compile()


def initial_state(question, llm):
    """Start each independent turn with empty evidence and explicit provenance."""
    return {"question": question, "cards": [], "verified_cards": [], "exchanges": [],
            "model": llm.config.model, "reasoning_effort": llm.config.reasoning_effort,
            "tool_messages": [], "card_names": [], "queries": [], "context": [],
            "citations": [], "blocked": False, "reason": None, "trace": []}


class QuestionWorkflow:
    """Public entry point coordinating dependencies, limits and resource ownership.

    retriever supplies search() and get(). llm uses native LangChain messages. api_factory creates a context-managed catalogue client per question.
    Tests inject fake services through these same parameters without changing the
    graph. k applies per query and source kind; context_budget counts serialized
    evidence characters, not tokens or the complete provider request size.
    """

    def __init__(self, retriever, *, llm=None, api_factory=MtgApiClient,
                 max_calls=4, k=5, version=None, context_budget=24000):
        """Validate application limits before opening clients or invoking a model."""
        if type(max_calls) is not int or not 1 <= max_calls <= 10:
            raise ValueError("max_calls must be an integer between 1 and 10")
        if type(k) is not int or k <= 0:
            raise ValueError("k must be a positive integer")
        if type(context_budget) is not int or context_budget < 100:
            raise ValueError("context_budget must be an integer of at least 100 characters")
        self.retriever, self.llm = retriever, llm if llm is not None else LLM()
        self.api_factory = api_factory
        self.settings = dict(max_calls=max_calls, k=k, version=version, context_budget=context_budget)

    def answer(self, question: str) -> WorkflowState:
        """Create fresh state and execute one complete question.

        Original text and model provenance are stored before the first node.
        Collection fields start empty so terminal routes share a predictable
        result shape. The context manager releases owned HTTP connections on
        success and on exceptions; an externally injected pool stays caller-owned.
        """
        if not question.strip():
            raise ValueError("Question must not be empty")
        with self.api_factory() as api:
            nodes = WorkflowNodes(self.retriever, self.llm, api, **self.settings)
            return build_graph(nodes).invoke(initial_state(question, self.llm), {"recursion_limit": 64})

    def stream(self, question: str):
        """Yield real node updates, including namespaced card subgraph steps.

        The HTTP client stays open until iteration ends. Consumers should close
        the iterator if they stop early. This is execution data, not reasoning text.
        """
        if not question.strip():
            raise ValueError("Question must not be empty")
        with self.api_factory() as api:
            nodes = WorkflowNodes(self.retriever, self.llm, api, **self.settings)
            yield from build_graph(nodes).stream(
                initial_state(question, self.llm), {"recursion_limit": 64},
                stream_mode="updates", subgraphs=True,
            )
