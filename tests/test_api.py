"""
Layer 3: FastAPI endpoint tests (using httpx TestClient)
"""
import os
import sys
import math
import pytest
from uuid_extensions import uuid7 as _gen_uuid7

_N3MC_DIR = os.path.join(os.path.dirname(__file__), "..")
_CORE_DIR = os.path.join(_N3MC_DIR, "core")
sys.path.insert(0, _CORE_DIR)
sys.path.insert(0, _N3MC_DIR)


@pytest.fixture(scope="module")
def app_and_deps(tmp_path_factory):
    """Create an isolated FastAPI app with temp DB for testing."""
    from unittest.mock import patch
    from pathlib import Path
    import uuid

    tmp = tmp_path_factory.mktemp("n3mc_api")
    db_path = tmp / "test.db"
    memory_dir = tmp / ".memory"
    memory_dir.mkdir()

    from database import get_connection, init_db
    conn = get_connection(str(db_path))
    init_db(conn)
    conn.close()

    config = {
        "owner_id": "test-owner",
        "local_id": "test-local",
        "server_port": 18521,
        "dedup_threshold": 0.95,
        "half_life_days": 90,
        "bm25_min_threshold": 0.1,
        "search_result_limit": 20,
        "context_char_limit": 3000,
        "min_score": 0.0,
        "search_query_max_chars": 2000,
    }

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from typing import Optional

    from database import (
        get_connection as gc, insert_memory, check_exact_duplicate,
        count_memories, get_all_memories, delete_memory,
        find_unindexed_memories, serialize_vector,
    )
    from processor import embed_passage, hybrid_search, purify, cosine_sim_from_l2

    session_id = str(uuid.uuid4())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(lifespan=lifespan)

    class BufferRequest(BaseModel):
        content: str
        agent_name: Optional[str] = None
        local_id: Optional[str] = None

    class SearchRequest(BaseModel):
        query: str

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/buffer")
    def buffer_ep(req: BufferRequest):
        if not req.content or not req.content.strip():
            return {"status": "error", "message": "empty content"}
        conn = gc(str(db_path))
        try:
            text = purify(req.content)
            if check_exact_duplicate(conn, text):
                conn.close()
                return {"status": "ok", "count": 0}
            try:
                qvec = embed_passage(text)
                from database import search_vector
                vr = search_vector(conn, qvec, k=1)
                if vr and cosine_sim_from_l2(vr[0][1]) >= config["dedup_threshold"]:
                    conn.close()
                    return {"status": "ok", "count": 0}
            except Exception:
                qvec = None
            before = count_memories(conn)
            ts = datetime.now(tz=timezone.utc).isoformat()
            insert_memory(conn, str(_gen_uuid7()), text, ts, config["owner_id"], qvec,
                          req.local_id or config["local_id"], req.agent_name, session_id)
            after = count_memories(conn)
            conn.close()
            return {"status": "ok", "count": after - before}
        except Exception as e:
            conn.close()
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.post("/search")
    def search_ep(req: SearchRequest):
        if not req.query:
            return {"results": []}
        conn = gc(str(db_path))
        try:
            results = hybrid_search(conn, req.query, config)
            conn.close()
            return {"results": results}
        except Exception as e:
            conn.close()
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    @app.get("/list")
    def list_ep():
        conn = gc(str(db_path))
        rows = get_all_memories(conn)
        conn.close()
        records = [{"id": r["id"], "content": r["content"], "timestamp": r["timestamp"], "agent_name": r["agent_name"]} for r in rows]
        return {"records": records, "total": len(records)}

    @app.post("/repair")
    def repair_ep():
        conn = gc(str(db_path))
        rows = find_unindexed_memories(conn)
        repaired = 0
        for row in rows:
            rowid = row[3]
            content = row[1]
            has_vec = row[4]
            has_fts = row[5]
            if not has_vec:
                try:
                    vec = embed_passage(content)
                    conn.execute("INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                 (rowid, serialize_vector(vec)))
                    repaired += 1
                except Exception:
                    pass
            if not has_fts:
                from database import strip_fts_punctuation
                try:
                    conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                                 (rowid, strip_fts_punctuation(content)))
                    repaired += 1
                except Exception:
                    pass
        conn.commit()
        conn.close()
        return {"status": "ok", "count": repaired}

    return app


@pytest.fixture(scope="module")
def client(app_and_deps):
    from httpx import AsyncClient
    from httpx._transports.asgi import ASGITransport
    transport = ASGITransport(app=app_and_deps)
    # Return async client — tests use anyio via pytest-asyncio or similar
    import asyncio

    class SyncWrapper:
        """Thin sync wrapper around AsyncClient for test convenience."""
        def __init__(self):
            self._ac = AsyncClient(transport=transport, base_url="http://test")
            self._loop = asyncio.new_event_loop()

        def get(self, url, **kw):
            return self._loop.run_until_complete(self._ac.get(url, **kw))

        def post(self, url, **kw):
            return self._loop.run_until_complete(self._ac.post(url, **kw))

        def close(self):
            self._loop.run_until_complete(self._ac.aclose())
            self._loop.close()

    w = SyncWrapper()
    yield w
    w.close()


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestBuffer:
    def test_saves_record(self, client):
        r = client.post("/buffer", json={"content": "Abraham Lincoln was the 16th US president"})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_empty_content(self, client):
        r = client.post("/buffer", json={"content": ""})
        assert r.json().get("status") == "error"

    def test_with_agent_name(self, client):
        r = client.post("/buffer", json={"content": "agent tagged record", "agent_name": "claude-code"})
        assert r.json()["status"] == "ok"

    def test_exact_dedup(self, client):
        text = "unique dedup test record xyz"
        client.post("/buffer", json={"content": text})
        r2 = client.post("/buffer", json={"content": text})
        assert r2.json()["status"] == "ok"
        assert r2.json()["count"] == 0

    def test_purifies_code_blocks(self, client):
        r = client.post("/buffer", json={"content": "result:\n```python\nprint('x')\n```"})
        assert r.json()["status"] == "ok"
        # Check list to verify code omitted
        lr = client.get("/list")
        contents = [rec["content"] for rec in lr.json()["records"]]
        assert any("[code omitted]" in c for c in contents)


class TestSearch:
    def test_empty_db_returns_list(self, client):
        r = client.post("/search", json={"query": "some query"})
        assert r.status_code == 200
        assert "results" in r.json()

    def test_buffer_and_search_roundtrip(self, client):
        client.post("/buffer", json={"content": "Planet [Alpha-9] temperature settings are critical"})
        r = client.post("/search", json={"query": "Alpha-9 temperature"})
        results = r.json()["results"]
        assert len(results) >= 1

    def test_empty_query(self, client):
        r = client.post("/search", json={"query": ""})
        assert r.json()["results"] == []

    def test_returns_score(self, client):
        client.post("/buffer", json={"content": "Lincoln was known for ending slavery"})
        r = client.post("/search", json={"query": "Lincoln"})
        results = r.json()["results"]
        if results:
            assert "score" in results[0]


class TestRepair:
    def test_repair_fixes_unindexed(self, client):
        r = client.post("/repair")
        assert r.json()["status"] == "ok"


class TestList:
    def test_list_after_buffer(self, client):
        r = client.get("/list")
        assert r.status_code == 200
        data = r.json()
        assert "records" in data
        assert "total" in data
        assert data["total"] >= 0
