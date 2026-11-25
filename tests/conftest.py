"""Shared fixtures: an isolated data folder and home, dummy vectors, and no real server."""
import io
import json
import math
import os
import random
import sys
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from n3memorycore.core.database import get_connection, init_db
from n3memorycore.core.processor import get_model  # noqa: F401  (standard import of the suite)
from n3memorycore.core import database, processor
from n3memorycore import n3memory, n3mc_hook, n3mc_stop_hook

# The real HTTP helpers, kept before the autouse fixture replaces them (for tests with local fake servers).
REAL_HTTP = {"_get": n3memory._get, "_post": n3memory._post, "_request_shutdown": n3memory._request_shutdown}
REAL_HF_HOME =os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
MODEL_NAME = "intfloat/multilingual-e5-base"
TEST_PORT = 18690
DIM = 768


# ---------------------------------------------------------------------------
# Dummy vectors
# ---------------------------------------------------------------------------

def _normalize(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def fake_embed(text, dim=DIM):
    """A deterministic unit vector made from zlib.crc32 of the text."""
    rnd = random.Random(zlib.crc32(text.encode("utf-8")))
    return _normalize([rnd.uniform(-1, 1) for _ in range(dim)])


def basis(i, dim=DIM):
    v = [0.0] * dim
    v[i] = 1.0
    return v


def vec_with_cos(cos, dim=DIM, base=0, other=1):
    """A unit vector whose cosine to basis(base) is `cos`."""
    v = [0.0] * dim
    v[base] = cos
    v[other] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


class FakeModel:
    """encode() like SentenceTransformer: fixed vectors for known texts, crc32 vectors otherwise."""

    def __init__(self, mapping=None, dim=DIM, delay=0.0):
        self.mapping = dict(mapping or {})
        self.dim = dim
        self.delay = delay
        self.calls = []

    def encode(self, text, normalize_embeddings=True):
        self.calls.append(text)
        if self.delay:
            import time
            time.sleep(self.delay)
        for prefix in ("passage: ", "query: "):
            if text.startswith(prefix) and text[len(prefix):] in self.mapping:
                return list(self.mapping[text[len(prefix):]])
        if text in self.mapping:
            return list(self.mapping[text])
        return fake_embed(text, self.dim)


def use_model(monkeypatch, model):
    monkeypatch.setattr(processor, "_model", model)
    monkeypatch.setattr(processor, "_model_name", processor._configured_model)
    monkeypatch.setattr(processor, "_load_model", lambda name: model)
    return model


# ---------------------------------------------------------------------------
# Isolation (autouse)
# ---------------------------------------------------------------------------

class NoServer(Exception):
    pass


def _no_http(*a, **k):
    import urllib.error
    raise urllib.error.URLError("no server in tests")


def _no_spawn(*a, **k):
    raise OSError("process creation is disabled in tests")


def _no_model(name):
    raise RuntimeError("the embedding model is not loaded in this test")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = tmp_path / "n3mc"
    mem = data / ".memory"
    mem.mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HF_HOME", REAL_HF_HOME)
    monkeypatch.setenv("N3MC_HOME", str(data))
    for var in ("CLAUDE_PROJECT_DIR", "N3MC_SESSION_ID", "N3MC_EMBED_MODEL"):
        monkeypatch.delenv(var, raising=False)
    j = lambda name: str(mem / name)  # noqa: E731
    for attr, value in {
        "HOME_DIR": str(data), "MEMORY_DIR": str(mem), "DB_PATH": j("n3memory.db"), "PID_FILE": j("n3mc.pid"),
        "AUDIT_LOG": j("audit.log"), "NOTICES_FILE": j("notices.json"), "CONFIG_FILE": str(data / "config.json"),
        "TURN_ID_FILE": j("turn_id.txt"), "SERVER_LOG": j("server.log"),
        "SERVER_START_FAILED": j("server_start_failed"),
    }.items():
        monkeypatch.setattr(n3memory, attr, value)
    monkeypatch.setattr(n3mc_hook, "AUDIT_LOG", j("audit.log"))
    monkeypatch.setattr(n3mc_stop_hook, "AUDIT_LOG", j("audit.log"))
    monkeypatch.setattr(n3memory, "_request_shutdown", lambda port=None: False)
    monkeypatch.setattr(n3memory, "_post", _no_http)
    monkeypatch.setattr(n3memory, "_get", _no_http)
    monkeypatch.setattr(n3memory, "_spawn", _no_spawn)
    monkeypatch.setattr(n3memory, "_sleep", lambda s: None)
    for attr, value in {"_CFG": None, "_CONFIG_RAW": None, "_FOREIGN_SERVER_ROOT": None,
                        "_NO_SERVER_REASON": None, "_QUICK_CHECK_DONE": False}.items():
        monkeypatch.setattr(n3memory, attr, value)
    monkeypatch.setattr(n3memory, "_SERVER_STATE", {"cfg": None, "code_version": None, "port": None,
                                                     "session_id": "server-session", "uvicorn": None})
    monkeypatch.setattr(processor, "_model", None)
    monkeypatch.setattr(processor, "_model_name", None)
    monkeypatch.setattr(processor, "_model_failed_at", None)
    monkeypatch.setattr(processor, "_configured_model", MODEL_NAME)
    monkeypatch.setattr(processor, "_load_model", _no_model)
    (data / "config.json").write_text(json.dumps({"server_port": TEST_PORT}), encoding="utf-8")
    yield {"home": home, "data": data, "mem": mem, "tmp": tmp_path}


@pytest.fixture
def paths(isolate):
    return isolate


@pytest.fixture
def fake_model(monkeypatch):
    return use_model(monkeypatch, FakeModel())


@pytest.fixture
def conn(isolate):
    c = get_connection(n3memory.DB_PATH)
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def cfg(isolate):
    return n3memory._load_config()


def ts_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")


_counter = [0]


def add(conn, content, vec=None, ts=None, session_id=None, turn_id=None, agent_name="claude-code"):
    """Insert one record and return its rowid."""
    _counter[0] += 1
    rid = f"0190000{_counter[0]:09d}"
    return database.insert_memory(conn, rid, content, ts or ts_days_ago(0), "owner", local_id="local",
                                  agent_name=agent_name, session_id=session_id, embedding=vec, turn_id=turn_id)


def run_cli(monkeypatch, capsys, func, stdin_text=None, *args):
    """Run an n3memory command function in-process with the given stdin; return (rc, out, err)."""
    if stdin_text is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    rc = func(*args)
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture(scope="session")
def real_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL_NAME)
