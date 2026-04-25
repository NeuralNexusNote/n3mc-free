"""Shared pytest fixtures for N3MemoryCore tests."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
N3MC_ROOT = HERE.parent
sys.path.insert(0, str(N3MC_ROOT))
sys.path.insert(0, str(N3MC_ROOT / "core"))


@pytest.fixture()
def isolated_db(tmp_path):
    """A fresh DB at tmp_path/.memory/n3memory.db. Schema initialized."""
    from core import database as db
    dbp = tmp_path / "n3memory.db"
    conn = db.init_db(dbp)
    yield conn, dbp
    conn.close()


@pytest.fixture()
def dummy_vec():
    """Deterministic 768-dim float vector (no embedding model needed)."""
    def _make(seed: float = 0.1):
        return [seed + (i * 1e-4) for i in range(768)]
    return _make


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """Lightweight in-memory cfg matching DEFAULT_CONFIG."""
    return {
        "owner_id": str(uuid.uuid4()),
        "local_id": str(uuid.uuid4()),
        "server_host": "127.0.0.1",
        "server_port": 18999,
        "dedup_threshold": 0.95,
        "half_life_days": 90,
        "bm25_min_threshold": 0.1,
        "search_result_limit": 20,
        "context_char_limit": 3000,
        "min_score": 0.0,
        "search_query_max_chars": 2000,
    }


@pytest.fixture(scope="session")
def embedding_model():
    """Loads e5-base-v2 once per session. Skip if unavailable."""
    try:
        from core import processor as proc
        return proc._get_model()
    except Exception as e:
        pytest.skip(f"embedding model unavailable: {e}")
