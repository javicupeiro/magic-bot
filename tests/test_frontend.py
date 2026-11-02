"""Check static delivery; run an offline HTTP fixture for browser UI checks.

`uv run python tests/test_frontend.py --serve` serves the real frontend with a
scripted backend on loopback. No model, catalogue, credentials or SQLite is used.
"""

import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

# Direct script execution starts with tests/ on sys.path.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.api.app import create_app


class FrontendDeliveryTests(unittest.TestCase):
    def test_same_origin_assets_preserve_api_and_do_not_serve_project_files(self):
        with TemporaryDirectory() as directory:
            with TestClient(create_app(memory_path=Path(directory) / "memory.sqlite")) as client:
                self.assertEqual(client.get("/").json()["links"]["ui"], "/ui/")
                for asset in ("", "chat.js", "stream.js", "styles.css"):
                    self.assertEqual(client.get(f"/ui/{asset}").status_code, 200)
                self.assertIn('type="module"', client.get("/ui/").text)
                self.assertEqual(client.get("/ui/.env").status_code, 404)
                self.assertEqual(client.get("/ui/%2e%2e/.env").status_code, 404)
                self.assertEqual(client.get("/conversations").json(), [])


def browser_fixture():
    """Return scripted contract responses for repeatable browser behavior checks."""
    app = FastAPI()
    histories = {}
    posts = []

    @app.get("/conversations")
    async def conversations():
        return [{"conversation_id": key} for key in histories]

    @app.post("/conversations", status_code=201)
    async def create():
        identifier = str(uuid4())
        histories[identifier] = []
        return {"conversation_id": identifier}

    @app.get("/conversations/{identifier}/messages")
    async def history(identifier: str):
        return {"conversation_id": identifier, "messages": histories[identifier]}

    @app.get("/sample.svg")
    async def image():
        return Response('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="160">'
                        '<rect width="120" height="160" fill="#245b43"/></svg>', media_type="image/svg+xml")

    @app.get("/_stats")
    async def stats():
        return {"posts": posts}

    @app.get("/_checks.js")
    async def checks():
        return FileResponse(ROOT / "tests/frontend_check.js", media_type="text/javascript")

    @app.post("/conversations/{identifier}/messages")
    async def send(identifier: str, body: dict):
        message = body["message"]
        posts.append(message)
        if message == "/busy":
            return JSONResponse(status_code=409, content={"detail": {"code": "conversation_busy"}})
        turn = str(uuid4())
        sequence = 0

        def event(kind, **fields):
            nonlocal sequence
            sequence += 1
            payload = dict(type=kind, conversation_id=identifier, turn_id=turn, sequence=sequence, **fields)
            return f"id: {turn}:{sequence}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

        async def stream():
            yield event("turn_started")
            await asyncio.sleep(.15)
            steps = ["route", "cards", "cards.prepare", "cards.model", "cards.tools", "cards.collect",
                     "cards.model", "cards.finish", "check_cards"]
            if message == "/clarify":
                steps = ["route", "clarify", "remember"]
            elif message != "/cards":
                steps += ["decompose", "retrieve", "generate", "validate", "review", "retrieve_more",
                          "repair", "validate", "review", "finalize", "remember"]
            for index, step in enumerate(steps):
                step_id = str(index)
                yield event("step_started", step=step, step_id=step_id)
                if step == "cards.tools":
                    yield event("tool", step=step, step_id=step_id, tool="get_card_by_name", status="started")
                    yield event("tool", step=step, step_id=step_id, tool="get_card_by_name", status="completed", duration_ms=0)
                if step == "cards.model":
                    yield event("model_usage", step=step, step_id=step_id, input_tokens=3, output_tokens=2, total_tokens=5)
                if message == "/error" and step == "retrieve":
                    yield event("error", step=step, step_id=step_id, code="step_failed", message="Workflow step failed")
                    yield event("error", code="execution_failed", message="Turn execution failed")
                    return
                if message == "/cut" and step == "cards.tools":
                    return
                yield event("step_completed", step=step, step_id=step_id, duration_ms=10)
            source = dict(source_id="702.2", kind="rule", title="Deathtouch <img src=x onerror=alert(1)>",
                          excerpt="Daño mortal · 日本語\nEvidence <script>unsafe()</script>", page_start=17,
                          page_end=18, version="2026-09")
            result = dict(answer="Encontré la carta. First strike deals damage first.", sources=[source], cards=[
                dict(name="Sample", mana_cost="{R}", cmc=1, text="First strike <b>text</b>", image_url="http://127.0.0.1:8014/sample.svg"),
                dict(name="Missing image", text="Deathtouch", image_url="http://127.0.0.1:8014/missing.png"),
                dict(name="Unsafe image", text="Text remains visible", image_url="javascript:alert(1)")])
            if message == "/cards":
                result["sources"] = []
                result["answer"] = "Found Sample."
            if message == "/clarify":
                result = dict(answer="Which card do you mean?", sources=[], cards=[])
            histories[identifier] += [{"role": "user", "content": message}, {"role": "assistant", "content": result["answer"]}]
            yield event("sources", sources=result["sources"])
            # Fragment non-ASCII prose and frame delimiters at arbitrary byte offsets.
            frame = event("answer", result=result, duration_ms=123)
            for offset in range(0, len(frame), 7):
                yield frame[offset:offset + 7]

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.mount("/ui", StaticFiles(directory=ROOT / "frontend", html=True))
    return app


if __name__ == "__main__":
    if "--serve" in sys.argv:
        import uvicorn
        uvicorn.run(browser_fixture(), host="127.0.0.1", port=8014)
    else:
        unittest.main()
