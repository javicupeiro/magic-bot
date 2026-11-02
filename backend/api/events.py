"""Adapt native graph callbacks and updates into safe, correlated SSE events."""

import json
from threading import RLock
from time import monotonic
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

from backend.api.schemas import PublicAnswer, PublicCard, PublicEvent
from backend.generation.answers import Citation

ROOT_STEPS = {"route", "cards", "check_cards", "decompose", "retrieve", "expand",
              "generate", "validate", "review", "retrieve_more", "repair", "finalize",
              "clarify", "remember"}
CARD_STEPS = {"prepare", "model", "tools", "collect", "finish"}
TOOL_NAMES = {"get_card_by_name", "search_cards"}


class ExecutionStopped(Exception):
    """The HTTP consumer disconnected; stop at the next cooperative boundary."""


def public_answer(state):
    """Pick fields individually; never recursively serialize the graph state."""
    return PublicAnswer(answer=state["answer"], cards=[
        PublicCard.model_validate({key: card[key] for key in PublicCard.model_fields if key in card})
        for card in state.get("cards", [])], sources=[
            Citation.model_validate(source) for source in state.get("citations", [])])


def encode_sse(event):
    """JSON keeps multiline prose inside one SSE data line."""
    payload = event.model_dump(mode="json", exclude_none=True)
    return f'id: {event.turn_id}:{event.sequence}\nevent: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'


class EventAdapter(BaseCallbackHandler):
    """Observe real starts/errors and match native namespace completions to them.

    LangGraph metadata identifies nodes; public step IDs replace namespace and
    provider call IDs. A lock orders callbacks from parallel native tool runs.
    """

    run_inline = True
    raise_error = True

    def __init__(self, conversation_id, emit):
        self.conversation_id = conversation_id
        self.turn_id = str(uuid4())
        self.emit = emit
        self.sequence = 0
        self.lock = RLock()
        self.runs = {}
        self.tools = {}
        self.parents = {}
        self.state = {}
        self.started = monotonic()

    def send(self, kind, **fields):
        """Serialize concurrent notifications into one monotonic event sequence."""
        with self.lock:
            self.sequence += 1
            self.emit(PublicEvent(type=kind, conversation_id=self.conversation_id,
                                  turn_id=self.turn_id, sequence=self.sequence, **fields))

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        """Observe only allowlisted node boundaries, retaining wrapper ancestry."""
        with self.lock:
            self.parents[run_id] = parent_run_id
            if parent_run_id is None:
                self.send("turn_started")
                return
            metadata = kwargs.get("metadata") or {}
            name = kwargs.get("name")
            namespace = metadata.get("langgraph_checkpoint_ns", "")
            nested = namespace.startswith("cards:")
            allowed = CARD_STEPS if nested else ROOT_STEPS
            if name != metadata.get("langgraph_node") or name not in allowed:
                return
            step = f"cards.{name}" if nested else name
            identifier = str(uuid4())
            self.runs[run_id] = (namespace, name, step, identifier, monotonic())
            self.send("step_started", step=step, step_id=identifier)

    def update(self, namespace, updates):
        """Only native updates complete steps; root fields retain this turn's result."""
        with self.lock:
            for name, update in updates.items():
                if not namespace and isinstance(update, dict):
                    for key in ("answer", "cards", "citations"):
                        if key in update:
                            self.state[key] = update[key]
                prefix = "|".join(namespace)
                for run_id, entry in list(self.runs.items()):
                    ns, node, step, identifier, started = entry
                    # Each checkpoint namespace ends with the node's task UUID.
                    parent_ns = ns.rsplit("|", 1)[0] if "|" in ns else ""
                    if node == name and parent_ns == prefix:
                        self.send("step_completed", step=step, step_id=identifier,
                                  duration_ms=round((monotonic() - started) * 1000, 3))
                        del self.runs[run_id]
                        break

    def _step(self, run_id):
        """Follow native parent runs through structured-output/tool wrappers."""
        while run_id is not None:
            entry = self.runs.get(run_id)
            if entry:
                return {"step": entry[2], "step_id": entry[3]}
            run_id = self.parents.get(run_id)
        return {}

    def on_chain_error(self, error, *, run_id, **kwargs):
        """Identify the failed public step without reflecting exception text."""
        with self.lock:
            entry = self.runs.get(run_id)
            if entry:
                self.send("error", **self._step(run_id), code="step_failed", message="Workflow step failed",
                          duration_ms=round((monotonic() - entry[4]) * 1000, 3))

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        """Map a native tool run to its public step, excluding arguments/call IDs."""
        with self.lock:
            name = (serialized or {}).get("name")
            if name not in TOOL_NAMES:
                return
            fields = self._step(parent_run_id)
            self.tools[run_id] = (name, fields, monotonic())
            self.send("tool", tool=name, status="started", **fields)

    def _tool_end(self, run_id, status):
        """Finish the recorded tool occurrence, including elapsed wall time."""
        with self.lock:
            record = self.tools.pop(run_id, None)
            if record:
                name, fields, started = record
                self.send("tool", tool=name, status=status, **fields,
                          duration_ms=round((monotonic() - started) * 1000, 3))

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._tool_end(run_id, "completed")

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._tool_end(run_id, "failed")

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        """Retain correlation only; never inspect the model input messages."""
        with self.lock:
            self.tools[run_id] = ("model", self._step(parent_run_id), monotonic())

    def on_llm_end(self, response, *, run_id, **kwargs):
        """Publish measured token counts only, excluding generated messages."""
        with self.lock:
            record = self.tools.pop(run_id, None)
            fields = record[1] if record else {}
            for generations in response.generations:
                for generation in generations:
                    usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                    if usage:
                        tokens = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                                  if isinstance(usage.get(key), int)}
                        self.send("model_usage", **fields, **tokens)
