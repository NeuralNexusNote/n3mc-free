"""Shared fixtures. Every test runs against a throw-away data folder; nothing touches the real
~/.n3mc, the real ~/.claude/settings.json, or a real server on port 18520."""
import math
import re
import zlib

import pytest

from n3memorycore import n3memory
from n3memorycore import n3mc_hook, n3mc_stop_hook
from n3memorycore.core import database, processor

DIM = 768
_WORD = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


def fake_embed(text):
    """Deterministic stand-in for the real model: hashed bag of tokens + CJK bigrams, L2-normalised."""
    text = re.sub(r"^(passage|query): ", "", text)
    vec = [0.0] * DIM
    tokens = _WORD.findall(text)
    tokens += [a + b for a, b in zip(tokens, tokens[1:])]
    for t in tokens:
        vec[zlib.crc32(t.encode("utf-8")) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _no_server(*args, **kwargs):
    raise OSError("no server in tests")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "n3mc_home"
    mem = home / ".memory"
    mem.mkdir(parents=True)
    paths = {
        "HOME_DIR": str(home), "MEMORY_DIR": str(mem), "DB_PATH": str(mem / "n3memory.db"),
        "PID_FILE": str(mem / "n3mc.pid"), "AUDIT_LOG": str(mem / "audit.log"),
        "NOTICES_FILE": str(mem / "notices.json"), "CONFIG_FILE": str(home / "config.json"),
        "TURN_ID_FILE": str(mem / "turn_id.txt"), "SERVER_LOG": str(mem / "server.log"),
        "SERVER_START_FAILED": str(mem / "server_start_failed"),
        "CONTEXT_FILE": str(mem / "memory_context.md"),
        "CONTEXT_RETIRED": str(mem / ".memory_context_retired"),
        "VEC_MODEL_FILE": str(mem / "vec_model.txt"),
    }
    for k, v in paths.items():
        monkeypatch.setattr(n3memory, k, v)
    monkeypatch.setattr(n3mc_hook, "AUDIT_LOG", paths["AUDIT_LOG"])
    monkeypatch.setattr(n3mc_stop_hook, "AUDIT_LOG", paths["AUDIT_LOG"])
    # The real user's settings.json must never be touched by --init.
    fake_user = tmp_path / "user"
    fake_user.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_user))
    monkeypatch.setenv("HOME", str(fake_user))
    # No real HTTP, no real shutdown.
    monkeypatch.setattr(n3memory, "_post", _no_server)
    monkeypatch.setattr(n3memory, "_get", _no_server)
    monkeypatch.setattr(n3memory, "_request_shutdown", _no_server)
    # No real model.
    monkeypatch.setattr(n3memory, "embed_passage", lambda t, m=None: fake_embed("passage: " + t))
    monkeypatch.setattr(processor, "embed_query", lambda t, m=None: fake_embed("query: " + t))
    monkeypatch.setattr(processor, "embed_passage", lambda t, m=None: fake_embed("passage: " + t))
    # Per-process state.
    monkeypatch.setattr(n3memory, "_QUICK_CHECK_DONE", False)
    monkeypatch.setattr(n3memory, "_FOREIGN_SERVER_ROOT", None)
    monkeypatch.setattr(n3memory, "_SERVER_STARTING", False)
    monkeypatch.setattr(n3memory, "_EPHEMERAL_IDS", {})
    monkeypatch.setattr(n3memory, "_SAVE_FAILED_CLEARED", False)
    return tmp_path


@pytest.fixture
def cfg():
    return n3memory.load_config()


@pytest.fixture
def conn(cfg):
    c = database.get_connection(n3memory.DB_PATH)
    database.init_db(c)
    yield c
    c.close()


def add(conn, cfg, content, turn_id=None, session_id=None, agent="claude-code", ts=None, embed=True):
    """Insert one record directly (with a fake vector unless embed=False)."""
    vec = fake_embed("passage: " + content) if embed else None
    rowid = database.insert_memory(
        conn, n3memory._new_id(), content, ts or n3memory._now_iso(), cfg["owner_id"],
        cfg["local_id"], agent, session_id, vec, turn_id=turn_id)
    return rowid
