"""Layer 3: FastAPI endpoints through TestClient (fake embeddings: the model is not needed here)."""
import pytest
from fastapi.testclient import TestClient

from n3memorycore import n3memory as n3m

PORT = 18520


@pytest.fixture
def client(use_fake_embed):
    return TestClient(n3m.create_app(), base_url=f"http://127.0.0.1:{PORT}")


class TestHealth:
    def test_health_ok(self, client):
        j = client.get("/health").json()
        assert j["status"] == "ok" and j["root"] == n3m.HOME_DIR and len(j["code_version"]) == 16 and j["pid"]


class TestBuffer:
    def test_save_content(self, client):
        assert client.post("/buffer", json={"content": "[user] hello there"}).json() == {"status": "ok", "count": 1}

    def test_empty_content_skipped(self, client):
        assert client.post("/buffer", json={"content": "  "}).json()["reason"] == "empty"

    def test_agent_name_saved(self, client):
        client.post("/buffer", json={"content": "a record", "agent_name": "claude-code"})
        assert client.get("/list").json()["records"][0]["agent_name"] == "claude-code"

    def test_exact_dedup(self, client):
        client.post("/buffer", json={"content": "dup", "turn_id": "T"})
        r = client.post("/buffer", json={"content": "dup", "turn_id": "T"}).json()
        assert r["status"] == "skipped" and r["reason"] == "exact_duplicate"

    def test_code_block_purified(self, client):
        client.post("/buffer", json={"content": "before\n```\nx\ny\n```\nafter"})
        assert client.get("/list").json()["records"][0]["content"] == "before\n[code omitted]\nafter"


class TestSearch:
    def test_empty_db_search(self, client):
        assert client.post("/search", json={"query": "anything at all"}).json()["results"] == []

    def test_save_then_search(self, client):
        client.post("/buffer", json={"content": "the zebra crossed the river at dawn"})
        r = client.post("/search", json={"query": "zebra river dawn"}).json()
        assert r["results"][0]["content"].startswith("the zebra")

    def test_empty_query(self, client):
        assert client.post("/search", json={"query": ""}).json()["results"] == []

    def test_score_returned(self, client):
        client.post("/buffer", json={"content": "the zebra crossed the river at dawn"})
        r = client.post("/search", json={"query": "zebra river dawn"}).json()["results"][0]
        assert r["score"] > 0 and r["weak"] is False and "turn_id" in r

    def test_bad_since_is_400(self, client):
        assert client.post("/search", json={"query": "x", "since": "yesterday"}).status_code == 400


class TestRepair:
    def test_repair_runs(self, client):
        assert client.post("/repair", json={}).json()["status"] == "ok"

    def test_count_excludes_unrepaired_records(self, client, monkeypatch, conn, cfg):
        from .conftest import add
        add(conn, cfg, "needs a vector", embed=False)
        monkeypatch.setattr(n3m, "embed_passage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
        from n3memorycore.core import database as db
        assert client.post("/repair", json={}).json()["count"] == 0  # FTS rows exist; the vector cannot be made
        assert len(db.find_unindexed_memories(conn)) == 1

    def test_repair_adds_the_vector_when_the_model_works(self, client, conn, cfg):
        from n3memorycore.core import database as db
        from .conftest import add
        add(conn, cfg, "needs a vector", embed=False)
        assert client.post("/repair", json={}).json()["count"] == 1
        assert db.find_unindexed_memories(conn) == []

    def test_two_repairs_on_the_same_row_do_not_collide(self, client, conn, cfg, monkeypatch):
        from n3memorycore.core import database as db
        from .conftest import add, fake_embed
        add(conn, cfg, "needs a vector", embed=False)

        def embed_then_other_repair(text, name=None):
            monkeypatch.setattr(n3m, "embed_passage", lambda t, name=None: fake_embed(t))
            assert client.post("/repair", json={}).json()["status"] == "ok"  # a second repair, mid-embedding
            return fake_embed(text)

        monkeypatch.setattr(n3m, "embed_passage", embed_then_other_repair)
        assert client.post("/repair", json={}).json()["status"] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 1

    def test_repair_batch_does_not_hold_the_write_lock_while_embedding(self, client, conn, cfg, monkeypatch):
        import threading
        import time
        import sqlite3
        from .conftest import add, fake_embed
        add(conn, cfg, "slow one", embed=False)
        got = {}

        def slow(text, name=None):
            time.sleep(0.2)
            return fake_embed(text)

        def probe():
            time.sleep(0.05)
            c2 = sqlite3.connect(n3m.DB_PATH, timeout=0.5)
            t0 = time.time()
            c2.execute("BEGIN IMMEDIATE")
            got["wait"] = time.time() - t0
            c2.rollback()
            c2.close()

        monkeypatch.setattr(n3m, "embed_passage", slow)
        th = threading.Thread(target=probe)
        th.start()
        client.post("/repair", json={})
        th.join()
        assert got["wait"] < 0.5


class TestList:
    def test_list_returns_records(self, client):
        assert client.get("/list").json() == {"records": [], "total": 0}

    def test_list_after_save(self, client):
        client.post("/buffer", json={"content": "one"})
        assert client.get("/list").json()["total"] == 1


class TestProOnly:
    def test_delete_returns_403(self, client):
        r = client.delete("/delete/x")
        assert r.status_code == 403 and r.json()["detail"] == "Pro feature"

    def test_gc_returns_403(self, client):
        assert client.post("/gc").status_code == 403

    def test_import_returns_403(self, client):
        assert client.post("/import", json={"records": []}).status_code == 403
