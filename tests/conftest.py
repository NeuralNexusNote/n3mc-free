"""Shared fixtures: isolated data folder, fake embeddings, dummy vectors.

Nothing here may touch the real ~/.n3mc, the real ~/.claude/settings.json or the real server (port 18520).
Never use monkeypatch.undo(): it would undo the isolation below as well.
"""
import os
import random
import urllib.error
import zlib

import pytest

from n3memorycore import n3memory as n3m
from n3memorycore import n3mc_hook, n3mc_stop_hook
from n3memorycore.core import database as db
from n3memorycore.core import processor as proc

DIM = 768
# Keep using the real Hugging Face cache even though HOME is faked below (no re-download in tests).
_REAL_HF_HOME = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")


def _unit(vec):
    n = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / n for x in vec]


def _word_vec(word):
    rnd = random.Random(zlib.crc32(word.encode("utf-8")))
    return [rnd.gauss(0, 1) for _ in range(DIM)]


def fake_embed(text):
    """Deterministic fake embedding: sum of per-token pseudo-random vectors (similar text -> high cosine)."""
    t = text.lower()
    toks = t.split()
    grams = [t[i:i + 3] for i in range(max(1, len(t) - 2))] if any(ord(c) > 0x2E80 for c in t) else toks
    acc = [0.0] * DIM
    for g in grams or [t]:
        v = _word_vec(g)
        acc = [a + b for a, b in zip(acc, v)]
    return _unit(acc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "n3mc_home"
    mem = home / ".memory"
    mem.mkdir(parents=True)
    paths = {
        "HOME_DIR": str(home), "MEMORY_DIR": str(mem), "DB_PATH": str(mem / "n3memory.db"),
        "PID_FILE": str(mem / "n3mc.pid"), "AUDIT_LOG": str(mem / "audit.log"),
        "CONFIG_FILE": str(home / "config.json"), "NOTICES_FILE": str(mem / "notices.json"),
        "TURN_ID_FILE": str(mem / "turn_id.txt"), "SERVER_LOG": str(mem / "server.log"),
        "SERVER_START_FAILED_FILE": str(mem / "server_start_failed"),
        "CONTEXT_FILE": str(mem / "memory_context.md"),
        "CONTEXT_RETIRED_MARKER": str(mem / ".memory_context_retired"),
    }
    for k, v in paths.items():
        monkeypatch.setattr(n3m, k, v)
    monkeypatch.setattr(db, "DB_PATH", paths["DB_PATH"])
    monkeypatch.setattr(n3mc_hook, "AUDIT_LOG", paths["AUDIT_LOG"])
    monkeypatch.setattr(n3mc_stop_hook, "AUDIT_LOG", paths["AUDIT_LOG"])
    fake_home = tmp_path / "user_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HF_HOME", _REAL_HF_HOME)
    monkeypatch.delenv("N3MC_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)  # set when pytest runs from a Claude Code hook
    monkeypatch.setattr(n3m, "_CFG_CACHE", None)
    monkeypatch.setattr(n3m, "_SERVER_OK", None)
    monkeypatch.setattr(n3m, "_FOREIGN_SERVER_ROOT", None)
    monkeypatch.setattr(n3m, "_QUICK_CHECKED", False)
    monkeypatch.setattr(n3m, "_SERVER_STARTING", False)
    monkeypatch.setattr(n3m, "_SERVER_PORT", None)
    monkeypatch.setattr(n3m, "_SERVER_STATE", {})

    def no_server(*a, **k):
        raise urllib.error.URLError("no server in tests")

    monkeypatch.setattr(n3m, "_post", no_server)
    monkeypatch.setattr(n3m, "_get", no_server)
    monkeypatch.setattr(n3m, "_request_shutdown", lambda *a, **k: False)
    monkeypatch.setattr(n3m, "_start_server", lambda *a, **k: False)
    yield tmp_path


@pytest.fixture
def cfg():
    return dict(n3m.get_config())


@pytest.fixture
def conn():
    c = db.get_connection()
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def use_fake_embed(monkeypatch):
    monkeypatch.setattr(proc, "embed_passage", lambda text, name=None: fake_embed(text))
    monkeypatch.setattr(proc, "embed_query", lambda text, name=None: fake_embed(text))
    monkeypatch.setattr(n3m, "embed_passage", lambda text, name=None: fake_embed(text))
    monkeypatch.setattr(n3m, "embed_query", lambda text, name=None: fake_embed(text))
    return fake_embed


def add(conn, cfg, content, *, turn_id=None, session_id=None, ts=None, embed=True, agent="claude-code"):
    """Insert one record (optionally with a fake vector)."""
    from datetime import datetime, timezone
    rid = n3m._new_id()
    db.insert_memory(conn, rid, content, ts or datetime.now(timezone.utc).isoformat(), cfg["owner_id"],
                     cfg["local_id"], agent, session_id, turn_id=turn_id,
                     embedding=fake_embed(content) if embed else None)
    return rid
