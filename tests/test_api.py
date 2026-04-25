"""Layer 3: FastAPI endpoint tests via TestClient.

These tests exercise the in-process FastAPI app. They patch DB_PATH so the
real production DB is never touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

N3MC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(N3MC_ROOT))

import n3memory as n3


@pytest.fixture()
def api_client(tmp_path, monkeypatch, cfg, embedding_model):
    """Build the FastAPI app against an isolated DB and yield TestClient."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(n3, "DB_PATH", tmp_path / "n3memory.db")
    monkeypatch.setattr(n3, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(n3, "FTS_PUNCT_MARKER", tmp_path / "fts_punct_cleaned")
    monkeypatch.setattr(n3, "VEC_E5V2_MARKER", tmp_path / "vec_e5v2_migrated")

    app = n3._build_app(cfg)
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_ok(self, api_client):
        r = api_client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestBuffer:
    def test_saves_record(self, api_client):
        r = api_client.post("/buffer", json={"content": "Lincoln was the 16th president"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["count"] == 1

    def test_empty_content_rejected(self, api_client):
        r = api_client.post("/buffer", json={"content": "   "})
        assert r.status_code == 400

    def test_exact_dedup(self, api_client):
        api_client.post("/buffer", json={"content": "duplicate me"})
        r = api_client.post("/buffer", json={"content": "duplicate me"})
        assert r.json().get("duplicate") == "exact"


class TestSearch:
    def test_empty_db(self, api_client):
        r = api_client.post("/search", json={"query": "nothing"})
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_buffer_then_search(self, api_client):
        api_client.post("/buffer", json={"content": "Aethoria floating sky-city of Etherealists"})
        r = api_client.post("/search", json={"query": "Aethoria sky-city"})
        results = r.json()["results"]
        assert len(results) >= 1
        assert "Aethoria" in results[0]["content"]

    def test_empty_query(self, api_client):
        r = api_client.post("/search", json={"query": ""})
        assert r.json()["results"] == []


class TestRepair:
    def test_repair_fixes_unindexed(self, api_client, tmp_path):
        # Inject an un-vec-indexed row, then repair.
        from core import database as db
        conn = db.init_db(tmp_path / "n3memory.db")
        db.insert_memory(conn, "vec-missing record", None, "o", "l")
        conn.close()
        r = api_client.post("/repair", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        # Marker files created (one-time migrations)
        assert (tmp_path / "fts_punct_cleaned").exists()
        assert (tmp_path / "vec_e5v2_migrated").exists()


class TestList:
    def test_list_empty(self, api_client):
        r = api_client.get("/list")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_list_after_buffer(self, api_client):
        api_client.post("/buffer", json={"content": "one"})
        api_client.post("/buffer", json={"content": "two"})
        r = api_client.get("/list")
        assert r.json()["total"] == 2


class TestDelete:
    def test_delete_existing(self, api_client):
        r = api_client.post("/buffer", json={"content": "to-delete"})
        mid = r.json()["id"]
        d = api_client.delete(f"/delete/{mid}")
        assert d.status_code == 200

    def test_delete_nonexistent(self, api_client):
        r = api_client.delete("/delete/no-such-id")
        assert r.status_code == 404
