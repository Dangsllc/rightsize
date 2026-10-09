"""An async document-review API (upload, poll, create job, poll, results) through a declarative config."""

import asyncio
import itertools
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from rightsize_core.systems.declarative import DeclarativeSystem
from rightsize_pack_compliance.pack import PACK

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/systems/examples/document-review-api.yaml"


def mock_api() -> FastAPI:
    app = FastAPI()
    ids = itertools.count(1)
    documents, reviews, seen = {}, {}, {}

    def auth(header):
        if header != "Bearer secret":
            raise HTTPException(401)

    @app.get("/standards")
    def standards(authorization: str = Header(None)):
        auth(authorization)
        return [{"id": "std-soc2", "code": "soc2"}, {"id": "std-hipaa", "code": "hipaa-security"}]

    @app.post("/documents", status_code=201)
    async def upload(file: UploadFile = File(...), name: str = Form(None), authorization: str = Header(None)):
        auth(authorization)
        did = f"d{next(ids)}"
        documents[did] = {"text": (await file.read()).decode(), "polls": 0, "name": name}
        return {"id": did, "state": "processing"}

    @app.get("/documents/{did}")
    def document(did: str, authorization: str = Header(None)):
        auth(authorization)
        documents[did]["polls"] += 1
        return {"id": did, "state": "ready" if documents[did]["polls"] > 1 else "processing"}

    @app.post("/reviews", status_code=201)
    def create(body: dict, authorization: str = Header(None)):
        auth(authorization)
        assert body["standard_id"] == "std-hipaa"
        seen["model"] = body.get("model")
        rid = f"r{next(ids)}"
        reviews[rid] = {"document": body["document_id"], "polls": 0}
        return {"id": rid, "state": "queued"}

    @app.get("/reviews/{rid}")
    def status(rid: str, authorization: str = Header(None)):
        auth(authorization)
        reviews[rid]["polls"] += 1
        return {"id": rid, "state": "done" if reviews[rid]["polls"] > 2 else "running"}

    @app.get("/reviews/{rid}/results")
    def results(rid: str, authorization: str = Header(None)):
        auth(authorization)
        return {"results": [
            {"requirement": "§ 164.312(a)", "rating": "partially_met", "score": 0.7, "excerpt": "q"},
            {"requirement": "164.312(b)", "rating": "not_met", "score": 0.9, "flagged_for_human": True},
        ]}

    app.state.seen = seen
    return app


@pytest.fixture(scope="module")
def api():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    app = mock_api()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/standards", timeout=0.3)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True


def test_async_flow(api, monkeypatch):
    url, app = api
    cfg = yaml.safe_load(CONFIG.read_text())
    for step in cfg["tasks"]["control_classification"]["steps"]:
        if "poll" in step:
            step["poll"]["every_s"] = 0.05
    monkeypatch.setenv("REVIEW_API_URL", url)
    monkeypatch.setenv("REVIEW_API_TOKEN", "secret")
    sysobj = DeclarativeSystem(cfg, PACK.transforms)
    sysobj.configure(model="claude-haiku-4-5")
    inputs = {"document_id": "clinic-00", "document": "# Manual\nText", "document_sha256": "x", "units": [], "groups": {}}

    async def go():
        await sysobj.preflight()
        await sysobj.setup()
        try:
            return await sysobj.run("control_classification", inputs)
        finally:
            await sysobj.teardown()
            await sysobj.aclose()

    out = asyncio.run(go())
    assert out.error is None, out.error
    a = out.prediction["assessments"]
    assert a[0] == {"control_code": "164.312(a)", "status": "partial", "confidence": 0.7, "evidence": "q", "abstained": False}
    assert a[1]["abstained"] is True and a[1]["status"] == "gap"
    assert app.state.seen["model"] == "claude-haiku-4-5"
    assert os.environ["REVIEW_API_TOKEN"] not in str(out.model_dump())
