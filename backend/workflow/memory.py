"""Own local SQLite checkpoints and serialize turns within this Python process.

QuestionWorkflow owns the connection lifetime. Locks are shared by workflow
instances using the same database and thread; they cover the entire turn, not
just individual SQLite writes. Multiple backend processes are outside this scope.
"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import Lock

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.cards.replies import CardReply
from backend.generation.answers import AnswerDraft, Citation, Claim
from backend.ingestion.documents import Reference
from backend.retrieval.retrieval import SearchResult
from backend.workflow.state import AnswerReview, Decomposition, EvidenceReview, RouteDecision

DEFAULT_MEMORY_PATH = Path("data/conversations/checkpoints.sqlite")
_LOCK_GUARD = Lock()
_TURN_LOCKS = {}


@contextmanager
def conversation_turn(database, conversation_id, *, blocking=True):
    """Hold the turn lock; nonblocking callers receive ConversationBusy on conflict."""
    key = (str(Path(database).resolve()), conversation_id)
    with _LOCK_GUARD:
        lock, users = _TURN_LOCKS.get(key, (Lock(), 0))
        _TURN_LOCKS[key] = (lock, users + 1)
    try:
        acquired = lock.acquire(blocking=blocking)
        if not acquired:
            raise ConversationBusy("Conversation already has an active turn")
        try:
            yield
        finally:
            lock.release()
    finally:
        with _LOCK_GUARD:
            _, users = _TURN_LOCKS[key]
            if users == 1:
                del _TURN_LOCKS[key]
            else:
                _TURN_LOCKS[key] = (lock, users - 1)


class ConversationBusy(RuntimeError):
    """A nonblocking caller attempted to enter an already active conversation."""


@contextmanager
def open_memory(path):
    """Open a trusted local checkpoint file and close it even after failed turns.

    Explicit application types let the native serializer reconstruct evidence and
    Pydantic outputs without pickle or unrestricted dynamic imports.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    types = [CardReply, AnswerDraft, Citation, Claim, Reference, SearchResult,
             AnswerReview, EvidenceReview, Decomposition, RouteDecision]
    serializer = JsonPlusSerializer(allowed_msgpack_modules=types)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = SqliteSaver(connection, serde=serializer)
        saver.setup()
        yield saver
    finally:
        connection.close()
