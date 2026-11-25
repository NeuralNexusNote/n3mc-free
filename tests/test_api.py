"""Layer 3: the FastAPI endpoints through TestClient. The client fixture loads the real model."""
import pytest
from fastapi.testclient import TestClient

from n3memorycore import n3memory
from n3memorycore.core import processor
from n3memorycore.core.database import get_connection, init_db, insert_memory
from tests.conftest import TEST_PORT, use_model


@pytest.fixture
def client(monkeypatch, real_model, isolate):
    use_model(monkeypatch, real_model)
    with TestClient(n3memory.app, base_url=f"http://127.0.0.1:{TEST_PORT}") as c:
        yield c


def _buffer(client, content, **kw):
    return client.post("/buffer", json={"content": content, **kw}).json()


class TestHealth:
    def test_health_ok(self, client, isolate):
        h = client.get("/health").json()
        assert h["status"] == "ok"
        assert n3memory._same_path(h["root"], str(isolate["data"]))
        assert len(h["code_version"]) == 16
        assert isinstance(h["pid"], int)


class TestBuffer:
    def test_save_content(self, client):
        assert _buffer(client, "坂本龍馬は土佐藩出身の志士である。") == {"status": "ok", "count": 1}

    def test_empty_content_skipped(self, client):
        assert _buffer(client, "   ") == {"status": "skipped", "count": 0, "reason": "empty"}

    def test_agent_name_saved(self, client):
        _buffer(client, "named record", agent_name="claude-code")
        recs = client.get("/list").json()["records"]
        assert recs[0]["agent_name"] == "claude-code"

    def test_exact_dedup(self, client):
        assert _buffer(client, "same text")["status"] == "ok"
        assert _buffer(client, "same text") == {"status": "skipped", "count": 0, "reason": "exact_duplicate"}

    def test_code_block_purified(self, client):
        _buffer(client, "here:\n```python\nprint('x')\n```\ndone")
        recs = client.get("/list").json()["records"]
        assert recs[0]["content"] == "here:\n[code omitted]\ndone"


class TestSearch:
    def test_empty_db_search(self, client):
        r = client.post("/search", json={"query": "anything"}).json()
        assert r["results"] == [] and r["pairs"] == {}

    def test_save_then_search(self, client):
        _buffer(client, "坂本龍馬は薩長同盟の成立に関わった。")
        _buffer(client, "今日の昼食はカレーライスだった。")
        r = client.post("/search", json={"query": "坂本龍馬"}).json()
        assert "坂本龍馬" in r["results"][0]["content"]

    def test_empty_query(self, client):
        _buffer(client, "something")
        r = client.post("/search", json={"query": ""}).json()
        assert r["results"] == []

    def test_score_returned(self, client):
        _buffer(client, "The Eiffel Tower is in Paris.")
        r = client.post("/search", json={"query": "Eiffel Tower"}).json()
        row = r["results"][0]
        for key in ("id", "content", "score", "timestamp", "turn_id", "weak", "agent_name", "session_id",
                    "filter_score", "cos_sim", "keyword_relevance"):
            assert key in row
        assert 0 < row["score"] <= 1


class TestRepair:
    def test_repair_runs(self, client):
        c = get_connection(n3memory.DB_PATH)
        init_db(c)
        insert_memory(c, "x1", "a record saved without a vector", "2026-01-01T00:00:00+00:00", "o")
        c.close()
        assert client.post("/repair").json()["count"] >= 1
        assert client.post("/repair").json()["count"] == 0

    def test_count_excludes_unrepaired_records(self, client, monkeypatch):
        client.post("/repair")
        c = get_connection(n3memory.DB_PATH)
        insert_memory(c, "x2", "no vector and no model", "2026-01-01T00:00:00+00:00", "o")
        c.close()

        def broken(*a, **k):
            raise RuntimeError("model unavailable")
        monkeypatch.setattr(processor, "get_model", broken)
        assert client.post("/repair").json()["count"] == 0


class TestList:
    def test_list_returns_records(self, client):
        r = client.get("/list").json()
        assert r == {"records": [], "total": 0}

    def test_list_after_save(self, client):
        _buffer(client, "first")
        _buffer(client, "second")
        r = client.get("/list").json()
        assert r["total"] == 2
        assert [x["content"] for x in r["records"]] == ["first", "second"]


class TestDelete:
    def test_delete_returns_403(self, client):
        _buffer(client, "keep me")
        rid = client.get("/list").json()["records"][0]["id"]
        assert client.delete(f"/delete/{rid}").status_code == 403
        assert client.get("/list").json()["total"] == 1

    def test_delete_nonexistent_also_403(self, client):
        assert client.delete("/delete/nope").status_code == 403

    def test_delete_detail_is_pro(self, client):
        assert client.delete("/delete/nope").json() == {"detail": "Pro feature"}


class TestGC:
    def test_gc_returns_403(self, client):
        assert client.post("/gc").status_code == 403

    def test_gc_detail_is_pro(self, client):
        assert client.post("/gc").json() == {"detail": "Pro feature"}


class TestImport:
    def test_import_returns_403(self, client):
        assert client.post("/import").status_code == 403

    def test_import_with_data_returns_403(self, client):
        assert client.post("/import", json={"records": [{"content": "x"}]}).status_code == 403

    def test_import_detail_is_pro(self, client):
        assert client.post("/import").json() == {"detail": "Pro feature"}
