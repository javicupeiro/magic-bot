"""Serve local conversations and POST SSE without blocking the async HTTP loop.

Each accepted turn owns a synchronous workflow in one worker thread. SQLite
checkpoints and the existing process-local turn lock remain the source of truth.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from queue import Empty, Full, Queue
import sqlite3
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.api.events import EventAdapter, ExecutionStopped, encode_sse, public_answer
from backend.api.schemas import (
    ApiHealth, ApiHelp, ApiVersion, ChatRequest, Conversation, ConversationHistory, HistoryMessage,
)
from backend.retrieval.retrieval import Retriever
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.memory import ConversationBusy, DEFAULT_MEMORY_PATH, open_memory


class LazyRetriever:
    """Open the default index only when a rules node needs it, inside its worker."""

    def __init__(self):
        self.instance = None

    def _get(self):
        if self.instance is None:
            self.instance = Retriever(Path("data/chroma"), "magic-rules")
        return self.instance

    def search(self, *args, **kwargs):
        return self._get().search(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._get().get(*args, **kwargs)


def create_app(*, memory_path=DEFAULT_MEMORY_PATH, workflow_factory=None):
    """Build an injectable local app; production authentication/multiworker is excluded.

    workflow_factory receives no arguments and returns a new QuestionWorkflow
    using memory_path. Tests substitute deterministic dependencies here.
    """
    memory_path = Path(memory_path)
    factory = workflow_factory or (lambda: QuestionWorkflow(
                                        LazyRetriever(),
                                        memory_path=memory_path))

    @asynccontextmanager
    async def lifespan(app):
        """Register local storage and stop workers before closing the application."""
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(memory_path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS api_conversations (id TEXT PRIMARY KEY)")
        # Capacity bounds concurrent executions; waiting HTTP tasks do not occupy
        # the event loop. Shutdown waits for in-flight synchronous calls to return.
        app.state.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chat")
        app.state.stops = set()
        try:
            yield
        finally:
            for stop in tuple(app.state.stops):
                stop.set()
            await asyncio.to_thread(app.state.executor.shutdown, wait=True, cancel_futures=True)

    package_version = version("magic-bot")
    app = FastAPI(title="Magic bot local API", version=package_version, lifespan=lifespan)

    @app.get("/", response_model=ApiHelp)
    async def api_help():
        return ApiHelp(name="magic-bot", message="Welcome to the Magic bot local API.", links={
            "docs": "/docs", "openapi": "/openapi.json", "health": "/health",
            "version": "/version", "conversations": "/conversations", "ui": "/ui/",
        })

    @app.get("/health", response_model=ApiHealth)
    async def health():
        """Confirm the HTTP application responds without probing external services."""
        return ApiHealth(status="ok")

    @app.get("/version", response_model=ApiVersion)
    async def application_version():
        return ApiVersion(name="magic-bot", version=package_version)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Framework validation bodies may echo user input. Keep errors public too.
        return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request",
                                                               "message": "Invalid request contract"}})

    def require_conversation(conversation_id):
        """Check registered IDs without allocating a workflow or invoking a model."""
        with sqlite3.connect(memory_path) as connection:
            exists = connection.execute("SELECT 1 FROM api_conversations WHERE id = ?",
                                        (str(conversation_id),)).fetchone()
        if not exists:
            raise HTTPException(404, detail={"code": "conversation_not_found"})

    @app.post("/conversations", status_code=201, response_model=Conversation)
    def create_conversation():
        identifier = str(uuid4())
        with sqlite3.connect(memory_path) as connection:
            connection.execute("INSERT INTO api_conversations VALUES (?)", (identifier,))
        return Conversation(conversation_id=identifier)

    @app.get("/conversations", response_model=list[Conversation])
    def list_conversations():
        """List registered conversation IDs in creation order, including empty ones."""
        with sqlite3.connect(memory_path) as connection:
            rows = connection.execute("SELECT id FROM api_conversations ORDER BY rowid").fetchall()
        return [Conversation(conversation_id=row[0]) for row in rows]

    @app.get("/conversations/{conversation_id}/messages", response_model=ConversationHistory)
    def history(conversation_id: UUID):
        """Read completed prose directly from the native SQLite checkpoint."""
        require_conversation(conversation_id)
        with open_memory(memory_path) as saver:
            checkpoint = saver.get_tuple({"configurable": {"thread_id": str(conversation_id)}})
            values = checkpoint.checkpoint["channel_values"] if checkpoint else {}
            messages = [HistoryMessage(role="user" if message.type == "human" else "assistant",
                                       content=message.content)
                        for message in values.get("messages", []) if message.type in {"human", "ai"}]
        return ConversationHistory(conversation_id=str(conversation_id), messages=messages)

    @app.post("/conversations/{conversation_id}/messages")
    async def chat(conversation_id: UUID, body: ChatRequest, request: Request):
        """Start one worker and establish HTTP status before returning SSE headers."""
        await asyncio.to_thread(require_conversation, conversation_id)
        identifier = str(conversation_id)
        queue = Queue(maxsize=256)
        stop = Event()
        app.state.stops.add(stop)

        def put(item):
            """Bound slow consumers' memory and cooperate with disconnect/shutdown."""
            while not stop.is_set():
                try:
                    queue.put(item, timeout=0.1)
                    return
                except Full:
                    continue
            raise ExecutionStopped()

        def execute():
            """Own/close the iterator in its thread, including on early stream exit."""
            adapter = EventAdapter(identifier, put)
            try:
                workflow = factory()
                iterator = workflow.stream(
                    body.message,
                    conversation_id=identifier,
                    callbacks=[adapter],
                    blocking=False)
                try:
                    for namespace, update in iterator:
                        if stop.is_set():
                            raise ExecutionStopped()
                        adapter.update(namespace, update)
                finally:
                    iterator.close()
                result = public_answer(adapter.state)
                adapter.send("sources", sources=result.sources)
                adapter.send("answer", result=result,
                             duration_ms=round((monotonic() - adapter.started) * 1000, 3))
            except ConversationBusy:
                put(409)
            except ExecutionStopped:
                pass
            except Exception:
                # Never expose exception text: provider errors can include payloads.
                if not stop.is_set():
                    if adapter.sequence == 0:
                        put(500)
                    else:
                        adapter.send("error", code="execution_failed", message="Turn execution failed")
            finally:
                if not stop.is_set():
                    try:
                        put(None)
                    except ExecutionStopped:
                        pass

        future = app.state.executor.submit(execute)

        async def next_item():
            """Poll without blocking the event loop; disconnect cancels consumption."""
            while True:
                if await request.is_disconnected():
                    raise ExecutionStopped()
                try:
                    return queue.get_nowait()
                except Empty:
                    await asyncio.sleep(0.02)

        try:
            first = await next_item()
            if isinstance(first, int):
                code = "conversation_busy" if first == 409 else "execution_failed"
                raise HTTPException(first, detail={"code": code})
        except BaseException:
            stop.set()
            future.cancel()
            app.state.stops.discard(stop)
            raise

        async def events():
            """One fetch stream, one execution; no replay or automatic POST retry."""
            try:
                item = first
                while item is not None:
                    yield encode_sse(item)
                    item = await next_item()
            except ExecutionStopped:
                return
            finally:
                stop.set()
                future.cancel()
                app.state.stops.discard(stop)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Serve only frontend assets; API routes and secrets stay outside this directory.
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    if frontend.is_dir():
        app.mount("/ui", StaticFiles(directory=frontend, html=True), name="ui")

    return app


app = create_app()
