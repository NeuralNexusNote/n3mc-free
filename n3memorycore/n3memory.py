"""N3MemoryCore (Free): CLI + resident FastAPI memory server.

Everything is saved by hooks (UserPromptSubmit + Stop). Saving is immediate and physical
(INSERT + COMMIT per record, PRAGMA synchronous=FULL): the "Immutable Memory" invariant.
"""
import os as _os
import sys as _sys

if __name__ == "__main__":
    # Safe import path. Started as `python -m <package>.<module>`, Python has put the current folder
    # first on sys.path, so a file there named like a module imported below would run inside this
    # program. Drop that entry, unless this package itself lives in the current folder (a source
    # checkout run in place). Nothing else may be imported before this block.
    try:
        _cwd = _os.path.normcase(_os.path.abspath(_os.getcwd()))
        _pkg_parent = _os.path.normcase(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if _sys.path and _os.path.normcase(_os.path.abspath(_sys.path[0])) == _cwd and _pkg_parent != _cwd:
            del _sys.path[0]
    except Exception:  # noqa: BLE001
        pass

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from .paths import (
    HOME_DIR, MEMORY_DIR, DB_PATH, PID_FILE, AUDIT_LOG, CONFIG_FILE, NOTICES_FILE, TURN_ID_FILE,
    SERVER_LOG, SERVER_START_FAILED_FILE, CONTEXT_FILE, CONTEXT_RETIRED_MARKER, claude_paths, python_cmd,
)
from .core.database import (
    get_connection, init_db, migrate_schema, insert_memory, insert_memory_once,
    check_exact_duplicate, search_vector, find_unindexed_memories,
    strip_fts_punctuation, serialize_vector, bigram_text, get_meta, set_meta, delete_meta,
    count_memories, get_memories_by_turn_id,
)
from .core.processor import (
    embed_passage, embed_query, cosine_sim_from_l2, hybrid_search,
    chunk_text, add_chunk_prefixes, purify_text, render_memory_context, sanitize_surrogates,
)
from .core import processor as _processor
from .core import database as _database

try:
    from uuid_extensions import uuid7 as _uuid7
except Exception:  # noqa: BLE001
    _uuid7 = None


def _new_id():
    return str(_uuid7()) if _uuid7 else str(uuid.uuid1())


# ============================================================ constants

DEFAULT_CONFIG = {
    "owner_id": None,
    "local_id": None,
    "bind_host": "127.0.0.1",
    "server_port": 18520,
    "embed_model": "intfloat/multilingual-e5-base",
    "half_life_days": 90,
    "bm25_min_threshold": 0.1,
    "search_result_limit": 20,
    "min_score": 0.2,
    "search_query_max_chars": 2000,
    "vector_min_cos": 0.85,
    "relative_min_score_ratio": 0.6,
    "min_results": 3,
    "pair_top_n": 3,
    "pair_max_chars": 3000,
    "version_order_enabled": True,
    "version_sim_threshold": 0.98,
    "version_min_chars": 100,
    "reply_sim_rerank_enabled": False,
    "reply_sim_alpha": 0.3,
    "reply_sim_floor": 0.80,
    "reply_sim_span": 0.15,
}

_INT_RANGES = {
    "server_port": (1, 65535), "search_result_limit": (1, 1365),
    "pair_top_n": (1, None), "pair_max_chars": (1, None), "search_query_max_chars": (1, None),
    "version_min_chars": (1, None), "min_results": (0, None),
}
_UNIT_KEYS = {"min_score", "vector_min_cos", "relative_min_score_ratio", "version_sim_threshold",
              "reply_sim_alpha", "reply_sim_floor"}

_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")
AGENT_NAME = "claude-code"
NOTICE_INTERVAL_SECONDS = 86400
AUDIT_ROTATE_BYTES = 50 * 1024 * 1024
HTTP_TIMEOUT = 30
SERVER_WAIT_SECONDS = 60

_FOREIGN_SERVER_ROOT = None
_QUICK_CHECKED = False
_SERVER_PORT = None          # the port this server process listens on (Host check)
_SERVER_STATE = {}
_CFG_CACHE = None


def _norm(p):
    return _os.path.normcase(_os.path.abspath(p))


def _reconfigure_streams():
    for name in ("stdin", "stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is not None and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def _ensure_dirs():
    _os.makedirs(MEMORY_DIR, exist_ok=True)


def _atomic_write_json(path, obj):
    d = _os.path.dirname(path)
    _os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + _os.path.basename(path) + ".")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
        _os.replace(tmp, path)
    except BaseException:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================ audit + notices

def _audit_append(rec):
    try:
        _ensure_dirs()
        with open(AUDIT_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: could not write audit.log ({type(e).__name__}: {e})", file=sys.stderr)


def _audit_stage(hook, stage, **extra):
    _audit_append({"ts": datetime.now(timezone.utc).isoformat(), "hook": hook, "stage": stage, **extra})


def _notices_read():
    try:
        with open(NOTICES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _notices_write(d):
    try:
        _atomic_write_json(NOTICES_FILE, d)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: could not write notices.json ({e})", file=sys.stderr)


def _notify(key, message):
    print(f"N3MC: {message}", file=sys.stderr)
    d = _notices_read()
    ent = d.get(key)
    if isinstance(ent, dict) and ent.get("message") == message:
        return
    d[key] = {"message": message, "last_shown": None}
    _notices_write(d)
    _audit_append({"ts": datetime.now(timezone.utc).isoformat(), "hook": "notice", "key": key, "message": message})


def _clear_notice(key):
    d = _notices_read()
    if key in d:
        del d[key]
        _notices_write(d)


def _take_notices():
    d = _notices_read()
    now, out, changed = time.time(), [], False
    for key, ent in d.items():
        if not isinstance(ent, dict):
            continue
        last = ent.get("last_shown")
        if last is None or now - float(last) >= NOTICE_INTERVAL_SECONDS:
            out.append(ent.get("message", ""))
            ent["last_shown"] = now
            changed = True
    if changed:
        _notices_write(d)
    return [m for m in out if m]


# ============================================================ config

def _config_value_ok(key, value):
    if key not in DEFAULT_CONFIG or DEFAULT_CONFIG[key] is None:
        return True
    default = DEFAULT_CONFIG[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(value, bool):
        return False
    if key in _INT_RANGES:
        lo, hi = _INT_RANGES[key]
        return isinstance(value, int) and value >= lo and (hi is None or value <= hi)
    if not isinstance(value, (int, float)):
        return False
    if key in _UNIT_KEYS:
        return 0 <= value <= 1
    if key in ("half_life_days", "reply_sim_span"):
        return value > 0
    if key == "bm25_min_threshold":
        return value >= 0
    return True


def _read_config_file():
    """Read config.json; a file touched in the last 2 s is retried (it may still be being written)."""
    attempts = 1
    try:
        if time.time() - _os.path.getmtime(CONFIG_FILE) <= 2:
            attempts = 20
    except OSError:
        pass
    last = None
    for i in range(attempts):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(0.05)
    raise last


def _recover_id(column):
    try:
        if not _os.path.exists(DB_PATH):
            return None
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            row = conn.execute(f"SELECT {column} FROM memories GROUP BY {column} "
                               "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001
        return None


def _load_config():
    global _CFG_CACHE
    _ensure_dirs()
    cfg = dict(DEFAULT_CONFIG)
    if not _os.path.exists(CONFIG_FILE):
        cfg["owner_id"] = _recover_id("owner_id") or str(uuid.uuid4())
        cfg["local_id"] = _recover_id("local_id") or str(uuid.uuid4())
        try:
            with open(CONFIG_FILE, "x", encoding="utf-8") as f:
                f.write(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
        except FileExistsError:
            return _load_config()
        _clear_notice("config_unreadable")
        _clear_notice("config_type")
        _CFG_CACHE = cfg
        return cfg
    try:
        raw = _read_config_file()
        if not isinstance(raw, dict):
            raise ValueError("config.json is not a JSON object")
    except Exception as e:  # noqa: BLE001
        _notify("config_unreadable", f"{CONFIG_FILE} could not be read ({e}). It was left untouched; default "
                "settings are in use. Fix the file to apply your settings.")
        cfg["owner_id"] = _recover_id("owner_id") or str(uuid.uuid4())
        cfg["local_id"] = _recover_id("local_id") or str(uuid.uuid4())
        _CFG_CACHE = cfg
        return cfg
    _clear_notice("config_unreadable")
    bad = []
    merged = {}
    for k, v in raw.items():
        if k in DEFAULT_CONFIG and not _config_value_ok(k, v):
            bad.append(k)
        else:
            merged[k] = v
    for k, v in DEFAULT_CONFIG.items():
        if k not in merged:
            merged[k] = v
    for idk in ("owner_id", "local_id"):
        if not isinstance(merged.get(idk), str) or not merged.get(idk):
            merged[idk] = _recover_id(idk) or str(uuid.uuid4())
    if bad:
        _notify("config_type", "These config.json items have the wrong type or are out of range and were "
                f"ignored (defaults used): {', '.join(sorted(bad))}")
    else:
        _clear_notice("config_type")
        # fill in missing keys / ids and save (the file is valid)
        out = dict(raw)
        changed = False
        for k, v in merged.items():
            if k not in out or (k in ("owner_id", "local_id") and out.get(k) != v):
                out[k] = v
                changed = True
        if changed:
            try:
                _atomic_write_json(CONFIG_FILE, out)
            except Exception as e:  # noqa: BLE001
                print(f"N3MC: could not update config.json ({e})", file=sys.stderr)
    _CFG_CACHE = merged
    return merged


def get_config():
    return _CFG_CACHE if _CFG_CACHE is not None else _load_config()


# ============================================================ time bounds

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_time_bound(value, upper=False):
    """Parse --since/--until into a UTC ISO string comparable with stored timestamps."""
    v = (value or "").strip()
    try:
        if _DATE_ONLY_RE.match(v):
            d = datetime.fromisoformat(v)
            dt = d.replace(hour=23, minute=59, second=59, microsecond=999999) if upper else d
        else:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00").replace("z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()  # naive values are read as local time
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"cannot read '{value}' (use 2026-11-13 or 2026-11-13T09:00)")


# ============================================================ DB open / corruption

class _DbCorrupt(Exception):
    pass


def _is_corruption_error(e):
    if not isinstance(e, sqlite3.DatabaseError) or isinstance(e, sqlite3.OperationalError):
        return False
    msg = str(e).lower()
    return "file is not a database" in msg or "malformed" in msg


def _connect_checked(pragma=None):
    conn = get_connection(DB_PATH)
    if pragma:
        try:
            rows = conn.execute(f"PRAGMA {pragma}").fetchall()
        except BaseException:
            conn.close()
            raise
        if not (len(rows) == 1 and str(rows[0][0]).lower() == "ok"):
            conn.close()
            raise _DbCorrupt(f"PRAGMA {pragma} returned {[tuple(r) for r in rows][:3]}")
    return conn


def _quarantine_db():
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    n, suffix = 1, ""
    while _os.path.exists(f"{DB_PATH}.corrupt-{stamp}{suffix}.bak"):
        n += 1
        suffix = f"-{n}"
    bak = f"{DB_PATH}.corrupt-{stamp}{suffix}.bak"
    _os.replace(DB_PATH, bak)
    for ext in ("-wal", "-shm"):
        if _os.path.exists(DB_PATH + ext):
            _os.replace(DB_PATH + ext, bak + ext)
    msg = (f"The memory DB failed its integrity check and was set aside as {bak}. A new empty DB is in use. "
           f"To recover records: sqlite3 {bak} .recover | sqlite3 recovered.db")
    print("N3MC WARNING: " + msg, file=sys.stderr)
    _notify("db_corrupt", msg)
    return bak


def _open_db(pragma=None):
    """Open the DB (optionally integrity-checked), quarantining a corrupt file. Schema is initialised."""
    _ensure_dirs()
    try:
        conn = _connect_checked(pragma)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, _DbCorrupt) or _is_corruption_error(e):
            _quarantine_db()
            conn = _connect_checked(None)
        else:
            raise
    try:
        init_db(conn)
    except Exception as e:  # noqa: BLE001
        conn.close()
        if _is_corruption_error(e):
            _quarantine_db()
            conn = _connect_checked(None)
            init_db(conn)
        else:
            raise
    return conn


def _open_db_direct():
    """DB for the direct (no server) paths: quick_check at most once per process."""
    global _QUICK_CHECKED
    pragma = None
    if not _QUICK_CHECKED:
        _QUICK_CHECKED = True
        pragma = "quick_check"
    return _open_db(pragma)


def _read_db_if_exists():
    if not _os.path.exists(DB_PATH):
        return None
    conn = get_connection(DB_PATH)
    return conn


# ============================================================ code version

def compute_code_version():
    h = hashlib.sha256()
    base = _os.path.dirname(_os.path.abspath(__file__))
    for rel in ("n3memory.py", _os.path.join("core", "database.py"), _os.path.join("core", "processor.py")):
        try:
            with open(_os.path.join(base, rel), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"?")
    try:
        with open(CONFIG_FILE, "rb") as f:
            raw = f.read()
        try:
            h.update(json.dumps(json.loads(raw.decode("utf-8")), sort_keys=True).encode("utf-8"))
        except Exception:  # noqa: BLE001
            h.update(raw)
    except OSError:
        pass
    return h.hexdigest()[:16]


# ============================================================ storing

def _store_record(conn, cfg, content, agent_name=None, local_id=None, session_id=None, turn_id=None,
                  embed=True):
    """Save one record. Returns the response dict used by /buffer."""
    content = purify_text(content or "")
    if not content.strip():
        return {"status": "skipped", "count": 0, "reason": "empty"}
    if check_exact_duplicate(conn, content, turn_id=turn_id):
        return {"status": "skipped", "count": 0, "reason": "exact_duplicate"}
    vec = None
    if embed:
        try:
            vec = embed_passage(content, cfg.get("embed_model"))
        except Exception as e:  # noqa: BLE001
            print(f"N3MC: embedding unavailable ({type(e).__name__}); saved without a vector", file=sys.stderr)
    before = count_memories(conn)
    rid = insert_memory_once(conn, _new_id(), content, datetime.now(timezone.utc).isoformat(),
                             cfg["owner_id"], local_id or cfg["local_id"], agent_name, session_id,
                             turn_id=turn_id, embedding=vec)
    if rid is None:
        return {"status": "skipped", "count": 0, "reason": "exact_duplicate"}
    if count_memories(conn) <= before:
        return {"status": "error", "message": "record count did not change after INSERT"}
    return {"status": "ok", "count": 1}


# ============================================================ repair

def _repair(conn, cfg, max_records=None):
    """Repair un-indexed records and run one-time migrations. Returns the number of records changed."""
    changed = 0
    budget = max_records if max_records else None
    # --- meta: model of the stored vectors
    if get_meta(conn, "embed_model") is None:
        model = cfg["embed_model"]
        has_vec = conn.execute("SELECT 1 FROM memories_vec LIMIT 1").fetchone() is not None
        if has_vec:
            try:
                with open(_os.path.join(MEMORY_DIR, "vec_model.txt"), "r", encoding="utf-8") as f:
                    model = f.read().strip() or model
            except OSError:
                pass
        set_meta(conn, "embed_model", model)
        set_meta(conn, "embed_dim", _database.EMBED_DIM)
        set_meta(conn, "embed_model_at", datetime.now(timezone.utc).isoformat())
        conn.commit()
    recorded = get_meta(conn, "embed_model")
    if recorded != cfg["embed_model"]:
        n = count_memories(conn)
        _notify("embed_model_mismatch",
                f"The stored vectors were made with {recorded}, but config.json sets embed_model to "
                f"{cfg['embed_model']}. Search quality is degraded until they are re-made: run n3mc --reembed "
                f"(it re-embeds all {n} records with {cfg['embed_model']}), or set embed_model back to {recorded}.")
    else:
        _clear_notice("embed_model_mismatch")
    processed = 0
    # --- one-time FTS punctuation clean-up (keyset batches)
    if get_meta(conn, "fts_punct_cleaned") is None:
        last, finished = 0, True
        while True:
            rows = conn.execute(
                "SELECT m.rowid AS rid, m.content AS c, f.content AS fc FROM memories m "
                "JOIN memories_fts f ON f.rowid = m.rowid WHERE m.rowid > ? ORDER BY m.rowid LIMIT 200",
                (last,)).fetchall()
            if not rows:
                break
            for r in rows:
                want = strip_fts_punctuation(r["c"])
                if r["fc"] != want:
                    conn.execute("UPDATE memories_fts SET content=? WHERE rowid=?", (want, r["rid"]))
                    changed += 1
                last = r["rid"]
                processed += 1
            conn.commit()
            if budget is not None and processed >= budget:
                finished = False
                break
        if finished:
            set_meta(conn, "fts_punct_cleaned", "1")
            conn.commit()
    # --- one-time bigram index fill
    if get_meta(conn, "fts_bigram") is None:
        while True:
            rows = conn.execute(
                "SELECT m.rowid AS rid, m.content AS c FROM memories m LEFT JOIN memories_fts_bi b "
                "ON b.rowid = m.rowid WHERE b.rowid IS NULL ORDER BY m.rowid LIMIT 200").fetchall()
            if not rows:
                break
            for r in rows:
                conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                             (r["rid"], bigram_text(r["c"])))
            conn.commit()
        set_meta(conn, "fts_bigram", "1")
        conn.commit()
    # --- missing vectors / FTS rows
    remaining = budget
    while remaining is None or remaining > 0:
        n_batch = 200 if remaining is None else min(200, remaining)
        batch = find_unindexed_memories(conn, n_batch)
        if not batch:
            break
        if conn.in_transaction:
            conn.commit()
        vecs = {}
        for r in batch:  # embeddings are computed outside any write transaction (F11)
            if r["no_vec"]:
                try:
                    vecs[r["rowid"]] = embed_passage(r["content"], cfg.get("embed_model"))
                except Exception:  # noqa: BLE001
                    vecs[r["rowid"]] = None
        fixed = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for r in batch:
                rid, did = r["rowid"], False
                if conn.execute("SELECT 1 FROM memories_fts WHERE rowid=?", (rid,)).fetchone() is None:
                    conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                                 (rid, strip_fts_punctuation(r["content"])))
                    did = True
                    if conn.execute("SELECT 1 FROM memories_fts_bi WHERE rowid=?", (rid,)).fetchone() is None:
                        conn.execute("INSERT INTO memories_fts_bi(rowid, content) VALUES (?, ?)",
                                     (rid, bigram_text(r["content"])))
                v = vecs.get(rid)
                if v is not None and conn.execute("SELECT 1 FROM memories_vec WHERE rowid=?", (rid,)).fetchone() is None:
                    conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                 (rid, serialize_vector(v)))
                    did = True
                fixed += 1 if did else 0
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        changed += fixed
        if remaining is not None:
            remaining -= len(batch)
        if fixed == 0:
            break
    return changed


# ============================================================ FastAPI server

def create_app():
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    from typing import Optional

    class BufferReq(BaseModel):
        content: str = ""
        agent_name: Optional[str] = None
        local_id: Optional[str] = None
        session_id: Optional[str] = None
        turn_id: Optional[str] = None

    class SearchReq(BaseModel):
        query: str = ""
        session_id: Optional[str] = None
        since: Optional[str] = None
        until: Optional[str] = None

    class RepairReq(BaseModel):
        max_records: Optional[int] = None

    class ShutdownReq(BaseModel):
        root: str = ""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if "cfg" not in _SERVER_STATE:
            _SERVER_STATE["cfg"] = get_config()
        _SERVER_STATE.setdefault("session_id", str(uuid.uuid4()))
        yield

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if "origin" in request.headers:
            return JSONResponse({"detail": "forbidden (Origin header)"}, status_code=403)
        port = _SERVER_PORT or get_config()["server_port"]
        if request.headers.get("host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return JSONResponse({"detail": "forbidden (Host header)"}, status_code=403)
        return await call_next(request)

    def cfg():
        if "cfg" not in _SERVER_STATE:
            _SERVER_STATE["cfg"] = get_config()
        _SERVER_STATE.setdefault("session_id", str(uuid.uuid4()))
        return _SERVER_STATE["cfg"]

    @app.get("/health")
    def health():
        return {"status": "ok", "root": HOME_DIR,
                "code_version": _SERVER_STATE.get("code_version") or compute_code_version(),
                "pid": _os.getpid()}

    @app.post("/buffer")
    def buffer(req: BufferReq):
        c = cfg()
        conn = None
        try:
            conn = _open_db()
            return _store_record(conn, c, req.content, req.agent_name, req.local_id,
                                 req.session_id or _SERVER_STATE["session_id"], req.turn_id)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
        finally:
            if conn:
                conn.close()

    @app.post("/search")
    def search(req: SearchReq):
        c = cfg()
        try:
            since = _parse_time_bound(req.since, False) if req.since else None
            until = _parse_time_bound(req.until, True) if req.until else None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        conn = _open_db()
        try:
            res = hybrid_search(conn, (req.query or "")[:c["search_query_max_chars"]], c,
                                session_id=req.session_id or _SERVER_STATE["session_id"],
                                since=since, until=until)
        finally:
            conn.close()
        for r in res["results"]:
            r.pop("rowid", None)
            r.pop("_version_group", None)
        return res

    @app.post("/repair")
    def repair(req: Optional[RepairReq] = None):
        c = cfg()
        conn = _open_db()
        try:
            n = _repair(conn, c, req.max_records if req else None)
        finally:
            conn.close()
        return {"status": "ok", "count": n}

    @app.get("/list")
    def list_all():
        conn = _open_db()
        try:
            rows = conn.execute("SELECT id, content, timestamp, agent_name FROM memories ORDER BY rowid").fetchall()
        finally:
            conn.close()
        return {"records": [dict(r) for r in rows], "total": len(rows)}

    @app.post("/shutdown")
    def shutdown(req: ShutdownReq):
        if _norm(req.root or "") != _norm(HOME_DIR):
            raise HTTPException(status_code=403, detail="root mismatch")
        threading.Timer(0.3, _exit_server).start()
        return {"status": "ok"}

    @app.delete("/delete/{memory_id}")
    def delete(memory_id: str):
        raise HTTPException(status_code=403, detail="Pro feature")

    @app.post("/gc")
    def gc(request: Request):
        raise HTTPException(status_code=403, detail="Pro feature")

    @app.post("/import")
    def import_(request: Request):
        raise HTTPException(status_code=403, detail="Pro feature")

    return app


def _exit_server():
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            if f.read().strip() == str(_os.getpid()):
                _os.unlink(PID_FILE)
    except OSError:
        pass
    _os._exit(0)


def cmd_run_server():
    global _SERVER_PORT
    import uvicorn
    cfg = get_config()
    _SERVER_PORT = cfg["server_port"]
    if cfg["bind_host"] != "127.0.0.1":
        print(f"N3MC WARNING: bind_host is {cfg['bind_host']}; the API has no authentication and is reachable "
              "from the network. Use 127.0.0.1.", file=sys.stderr)
    _SERVER_STATE["cfg"] = cfg
    _SERVER_STATE["session_id"] = str(uuid.uuid4())
    _SERVER_STATE["code_version"] = compute_code_version()
    conn = _open_db("integrity_check")
    conn.close()
    try:
        _processor.get_model(cfg["embed_model"])  # preload before listening (first run may take minutes)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: embedding model not loaded ({type(e).__name__}: {e}); continuing without vectors",
              file=sys.stderr)
    try:
        with open(PID_FILE, "x", encoding="utf-8") as f:
            f.write(str(_os.getpid()))
    except FileExistsError:
        try:
            with open(PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(_os.getpid()))
        except OSError:
            pass
    try:
        uvicorn.run(create_app(), host=cfg["bind_host"], port=cfg["server_port"], log_level="error")
    finally:
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                if f.read().strip() == str(_os.getpid()):
                    _os.unlink(PID_FILE)
        except OSError:
            pass
    return 0


# ============================================================ HTTP client

def _port():
    return get_config()["server_port"]


def _opener():
    # Never go through a proxy: the server is on 127.0.0.1 (F16).
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(method, path, body=None, timeout=HTTP_TIMEOUT, port=None):
    url = f"http://127.0.0.1:{port or _port()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    with _opener().open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path, body, timeout=HTTP_TIMEOUT, port=None):
    return _http("POST", path, body, timeout, port)


def _get(path, timeout=HTTP_TIMEOUT, port=None):
    return _http("GET", path, None, timeout, port)


def _get_health(port=None):
    try:
        h = _get("/health", timeout=5, port=port)
        return h if isinstance(h, dict) and h.get("status") == "ok" else None
    except Exception:  # noqa: BLE001
        return None


def _health_decision(health):
    if not health:
        return "none"
    if not health.get("root"):
        return "legacy"
    if _norm(health["root"]) != _norm(HOME_DIR):
        return "foreign"
    if health.get("code_version") != compute_code_version():
        return "stale"
    return "ok"


def _request_shutdown(port=None):
    try:
        _post("/shutdown", {"root": HOME_DIR}, timeout=5, port=port)
        return True
    except Exception:  # noqa: BLE001
        return False


def _wait_port_free(port=None, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if _get_health(port) is None:
            return True
        time.sleep(0.3)
    return False


def _read_pid():
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:  # noqa: BLE001
        return None


def _remove_pid_if(pid):
    if _read_pid() == pid:
        try:
            _os.unlink(PID_FILE)
        except OSError:
            pass


def _home_of_env(env):
    h = env.get("N3MC_HOME")
    return _os.path.abspath(_os.path.expanduser(h or _os.path.join("~", ".n3mc")))


def _is_own_server_process(pid=None, health=None):
    """True only if psutil confirms the process is this folder's `--run-server`."""
    try:
        import psutil
        if pid is None and health:
            pid = health.get("pid")
        if pid is None:
            port = _port()
            for c in psutil.net_connections(kind="inet"):
                if c.status == "LISTEN" and c.laddr and c.laddr.port == port and c.pid:
                    pid = c.pid
                    break
        if pid is None:
            return False
        p = psutil.Process(int(pid))
        cmd = p.cmdline()
        if "n3memorycore.n3memory" not in " ".join(cmd) or "--run-server" not in cmd:
            return False
        return _norm(_home_of_env(p.environ())) == _norm(HOME_DIR)
    except Exception:  # noqa: BLE001
        return False


def _listen_ports(pid):
    try:
        import psutil
        p = psutil.Process(int(pid))
        conns = p.net_connections(kind="inet") if hasattr(p, "net_connections") else p.connections(kind="inet")
        return sorted({c.laddr.port for c in conns if c.status == "LISTEN" and c.laddr})
    except Exception:  # noqa: BLE001
        return []


def _terminate_process(pid):
    try:
        import psutil
        p = psutil.Process(int(pid))
        p.terminate()
        try:
            p.wait(5)
        except Exception:  # noqa: BLE001
            p.kill()
        return True
    except Exception:  # noqa: BLE001
        return False


def _stop_stale_server(health=None):
    """Stop this folder's old server: /shutdown first; kill only after psutil confirms it is ours."""
    port = _port()
    if _request_shutdown():
        return _wait_port_free(port)
    if _is_own_server_process(health=health):
        pid = (health or {}).get("pid")
        if pid and _terminate_process(pid):
            _remove_pid_if(pid)
            return _wait_port_free(port)
    return False


_SERVER_STARTING = False
_SERVER_OK = None


def _foreign(health, unknown=False):
    global _FOREIGN_SERVER_ROOT
    root = health.get("root") if health else None
    _FOREIGN_SERVER_ROOT = root or "(unknown: the server did not report its folder)"
    _notify("port_conflict", f"Another program or N3MC folder ({_FOREIGN_SERVER_ROOT}) is using port {_port()}. "
            f"Records are saved directly and memory search is off. Set a different server_port in {CONFIG_FILE}.")
    return False


def _clear_server_notices():
    for k in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(k)
    try:
        _os.unlink(SERVER_START_FAILED_FILE)
    except OSError:
        pass


def _start_server():
    global _SERVER_STARTING, _FOREIGN_SERVER_ROOT
    _ensure_dirs()
    try:
        if _os.path.exists(SERVER_LOG) and _os.path.getsize(SERVER_LOG) > 1024 * 1024:
            open(SERVER_LOG, "wb").close()
        log = open(SERVER_LOG, "ab")
        kwargs = {}
        if _os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([*python_cmd(), "-m", "n3memorycore.n3memory", "--run-server"],
                                stderr=log, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: could not start the memory server ({type(e).__name__}: {e})", file=sys.stderr)
        return False
    try:
        with open(PID_FILE, "x", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except FileExistsError:
        try:
            with open(PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(proc.pid))
        except OSError:
            pass
    end = time.time() + SERVER_WAIT_SECONDS
    while time.time() < end:
        h = _get_health()
        d = _health_decision(h)
        if d in ("ok", "stale"):
            _clear_server_notices()
            return True
        if d in ("foreign", "legacy"):
            _foreign(h)
            return False
        if proc.poll() is not None:
            _notify("server_start_failed", f"The N3MC memory server could not start (exit code {proc.returncode}). "
                    f"See {SERVER_LOG} for the reason.")
            try:
                with open(SERVER_START_FAILED_FILE, "w", encoding="utf-8") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
            except OSError:
                pass
            _remove_pid_if(proc.pid)
            return False
        time.sleep(0.5)
    _SERVER_STARTING = True  # still loading the model
    return False


def _launch():
    try:
        if time.time() - _os.path.getmtime(SERVER_START_FAILED_FILE) < 300:
            return False
    except OSError:
        pass
    return _start_server()


def _pid_is_own(pid):
    try:
        import psutil  # noqa: F401
    except ImportError:
        try:
            return time.time() - _os.path.getmtime(PID_FILE) < 60
        except OSError:
            return False
    return _is_own_server_process(pid=pid)


def _shutdown_other_port(pid):
    cur = _port()
    for p in _listen_ports(pid):
        if p == cur:
            continue
        h = _get_health(port=p)
        if h and h.get("root") and _norm(h["root"]) == _norm(HOME_DIR):
            _request_shutdown(port=p)
            end = time.time() + 15
            while time.time() < end and _pid_is_own(pid):
                time.sleep(0.3)
            _remove_pid_if(pid)
            return True
    return False


def ensure_server():
    """True if this folder's up-to-date server answers; False -> work without a server."""
    global _FOREIGN_SERVER_ROOT, _SERVER_STARTING
    _FOREIGN_SERVER_ROOT = None
    _SERVER_STARTING = False
    h = _get_health()
    d = _health_decision(h)
    if d == "ok":
        _clear_server_notices()
        return True
    if d == "stale":
        if _stop_stale_server(h):
            return _launch()
        _notify("stale_server", "The old memory server could not be stopped. Check the server_port in each "
                "N3MC folder's config.json, then restart it by hand.")
        return True  # same root, same DB: keep using it
    if d == "foreign":
        return _foreign(h)
    if d == "legacy":
        if _is_own_server_process(health=h) and _stop_stale_server(h):
            return _launch()
        return _foreign(h)
    # no server answers
    pid = _read_pid()
    if pid is not None:
        if _pid_is_own(pid):
            if _shutdown_other_port(pid):
                return _launch()
            end = time.time() + SERVER_WAIT_SECONDS
            while time.time() < end:
                h = _get_health()
                d = _health_decision(h)
                if d in ("ok", "stale"):
                    return True
                if d in ("foreign", "legacy"):
                    return _foreign(h)
                time.sleep(0.5)
            _SERVER_STARTING = True
            return False
        try:
            _os.unlink(PID_FILE)
        except OSError:
            pass
    return _launch()


def _use_server():
    global _SERVER_OK
    if _SERVER_OK is None:
        try:
            _SERVER_OK = bool(ensure_server())
        except Exception as e:  # noqa: BLE001
            print(f"N3MC: server check failed ({type(e).__name__}: {e})", file=sys.stderr)
            _SERVER_OK = False
    return _SERVER_OK


def _unavailable_reason():
    if _FOREIGN_SERVER_ROOT:
        return f"another N3MC folder's server is using port {_port()}"
    if _SERVER_STARTING:
        return "the memory server is still starting"
    return "the memory server is not running"


# ============================================================ saving / searching

_PROCESS_SESSION_ID = str(uuid.uuid4())


def _env_session():
    return _os.environ.get("N3MC_SESSION_ID") or None


def _buffer_direct(content, agent_name=None, session_id=None, turn_id=None, local_id=None):
    """Save without the server (no embedding; a later --repair adds the vector). Never silent on failure."""
    try:
        conn = _open_db_direct()
        try:
            r = _store_record(conn, get_config(), content, agent_name, local_id,
                              session_id or _env_session() or _PROCESS_SESSION_ID, turn_id, embed=False)
        finally:
            conn.close()
        if r.get("status") == "error":
            raise RuntimeError(r.get("message"))
        _clear_notice("save_failed")
        return r
    except Exception as e:  # noqa: BLE001
        print("⚠️ Physical save failed. Current memories may be lost.", file=sys.stderr)
        _notify("save_failed", f"A record could not be saved ({type(e).__name__}: {e}). Current memories may be "
                "lost; if it happens again, check that no other program holds the memory database.")
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def _save_record(content, agent_name=None, session_id=None, turn_id=None, local_id=None):
    if _use_server():
        body = {"content": content}
        for k, v in (("agent_name", agent_name), ("session_id", session_id), ("turn_id", turn_id),
                     ("local_id", local_id)):
            if v:
                body[k] = v
        try:
            r = _post("/buffer", body)
            if isinstance(r, dict) and r.get("status") in ("ok", "skipped"):
                return r
        except Exception as e:  # noqa: BLE001
            print(f"N3MC: server save failed ({type(e).__name__}); saving directly", file=sys.stderr)
    return _buffer_direct(content, agent_name, session_id, turn_id, local_id)


def _save_text(role, text, turn_id, session_id=None):
    """Purify the whole message, chunk it, tag the chunks and save each. Returns (saved, failed)."""
    text = purify_text(text or "")
    if not text.strip():
        return 0, 0
    saved = failed = 0
    for rec in add_chunk_prefixes(role, chunk_text(text, 400, 40)):
        r = _save_record(rec, AGENT_NAME, session_id, turn_id)
        if r.get("status") == "ok":
            saved += 1
        elif r.get("status") == "error":
            failed += 1
    return saved, failed


def _placeholder(body_line, notices):
    out = ["# Recalled Memory Context", ""]
    for n in notices:
        out += [f"> ⚠️ N3MC notice: {n}", ""]
    out += [body_line, ""]
    return "\n".join(out)


def _search_text(query, session_id=None, since=None, until=None):
    cfg = get_config()
    query = sanitize_surrogates(query or "")[:cfg["search_query_max_chars"]]
    notices = _take_notices()
    if not query.strip():
        return _placeholder("_No relevant memories found._", notices)
    if not _use_server():
        return _placeholder(f"_(memory search unavailable: {_unavailable_reason()})_", notices)
    body = {"query": query}
    if session_id:
        body["session_id"] = session_id
    if since:
        body["since"] = since
    if until:
        body["until"] = until
    try:
        r = _post("/search", body)
    except Exception as e:  # noqa: BLE001
        return _placeholder(f"_(memory search unavailable: {type(e).__name__}: {e})_", notices)
    return render_memory_context(query, r.get("results", []), r.get("pairs", {}), r.get("pairs_omitted", {}),
                                 notices)


# ============================================================ hook helpers

def _read_stdin_text():
    try:
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return sys.stdin.read()


def _extract_text(prompt):
    """Text of a prompt. A JSON array of typed parts (multimodal) yields only its text parts."""
    if prompt is None:
        return ""
    parts = None
    if isinstance(prompt, list):
        parts = prompt
    elif isinstance(prompt, str):
        s = prompt.strip()
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list) and v and all(isinstance(p, dict) and isinstance(p.get("type"), str)
                                                     for p in v):
                    parts = v
            except Exception:  # noqa: BLE001
                parts = None
        if parts is None:
            return prompt
    else:
        return str(prompt)
    out = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
           and isinstance(p.get("text"), str)]
    return "\n".join(t for t in out if t)


def _turn_id_path(session_id):
    if session_id:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id))[:64]
        return _os.path.join(MEMORY_DIR, f"turn_id_{safe}.txt")
    return TURN_ID_FILE


def _read_turn_id(session_id):
    try:
        with open(_turn_id_path(session_id), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_turn_id(session_id, turn_id):
    try:
        _ensure_dirs()
        with open(_turn_id_path(session_id), "w", encoding="utf-8") as f:
            f.write(turn_id)
    except OSError as e:
        print(f"N3MC: could not write the turn id ({e})", file=sys.stderr)


def _user_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
        return "\n".join(texts) if texts else None
    return None


def _last_assistant_text(path, current_prompt):
    """The last assistant text block before the current prompt, read from the session transcript."""
    if not path:
        return None
    try:
        size = _os.path.getsize(path)
        with open(path, "rb") as f:
            start = max(0, size - 5 * 1024 * 1024)
            f.seek(start)
            data = f.read()
        lines = data.split(b"\n")
        if start > 0 and lines:
            lines = lines[1:]
    except Exception as e:  # noqa: BLE001
        _audit_stage("UserPromptSubmit", "transcript_unreadable", reason=f"{type(e).__name__}")
        return None
    skip_prefixes = ("[Request interrupted by user", "<command-", "<local-command-") + _SYSTEM_PROMPT_PREFIXES
    first = True
    cur = (current_prompt or "").strip()
    for line in reversed(lines):
        try:
            obj = json.loads(line.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(obj, dict) or obj.get("isSidechain"):
            continue
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")
        if t == "user":
            if obj.get("isMeta"):
                continue
            text = _user_text(content)
            real = text is not None and not text.lstrip().startswith(skip_prefixes)
            if first:
                first = False
                if real and text.strip() == cur:
                    continue  # the current prompt itself (only the trailing line)
            if not real:
                continue
            return None  # an earlier real prompt: no previous reply
        first = False
        if isinstance(content, str):
            parts = [content] if content.strip() else []
        elif isinstance(content, list):
            parts = [p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"
                     and isinstance(p.get("text"), str) and p["text"].strip()]
        else:
            parts = []
        if parts:
            return parts[-1]
    return None


def cmd_hook_submit():
    raw = _read_stdin_text()
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        data = {"prompt": raw}
    if not isinstance(data, dict):
        data = {"prompt": raw}
    data = sanitize_surrogates(data)
    prompt = data.get("prompt") if data.get("prompt") is not None else data.get("message")
    text = _extract_text(prompt)
    session_id = data.get("session_id") or None
    if text.lstrip().startswith(_SYSTEM_PROMPT_PREFIXES):
        _audit_stage("UserPromptSubmit", "skipped", reason="system_prompt")
        print("# Recalled Memory Context\n\n_No relevant memories found._\n")
        return 0
    failed = 0
    if _use_server():
        try:
            _post("/repair", {"max_records": 200})
        except Exception as e:  # noqa: BLE001
            print(f"N3MC: repair skipped ({type(e).__name__})", file=sys.stderr)
    try:
        reply = data.get("last_assistant_message") or _last_assistant_text(data.get("transcript_path"), text)
        if reply:
            prev = _read_turn_id(session_id) or str(uuid.uuid4())
            failed += _save_text("claude", reply, prev, session_id)[1]
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: could not save the previous reply ({type(e).__name__}: {e})", file=sys.stderr)
    turn_id = str(uuid.uuid4())
    _write_turn_id(session_id, turn_id)
    try:
        out = _search_text(text, session_id)
    except Exception as e:  # noqa: BLE001
        out = _placeholder(f"_(memory search unavailable: {type(e).__name__}: {e})_", [])
    sys.stdout.write(out if out.endswith("\n") else out + "\n")
    sys.stdout.flush()
    if text.strip():
        try:
            failed += _save_text("user", text, turn_id, session_id)[1]
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"N3MC: could not save the message ({type(e).__name__}: {e})", file=sys.stderr)
    return 1 if failed else 0


def cmd_save_claude_turn():
    raw = _read_stdin_text()
    try:
        data = sanitize_surrogates(json.loads(raw))
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        return 0
    text = data.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        return 0
    session_id = data.get("session_id") or None
    turn_id = _read_turn_id(session_id) or str(uuid.uuid4())
    return 1 if _save_text("claude", text, turn_id, session_id)[1] else 0


# ============================================================ --stop

_BEHAVIOR_MARK = "<!-- n3mc-behavior v3 -->"
_KNOWN_BEHAVIOR_MD_HASHES = {"ade0a59b94f59f9a", "34ed39efe5a3833d", "5af3c015c06c62c1"}
_BEHAVIOR_MD_CONTENT = '''<!-- n3mc-behavior v3 -->
# N3MemoryCore Behavioral Guidelines

These guidelines apply to every Claude Code session in this project. They are
loaded automatically from `.claude/rules/` at session start.

## Fully Automatic Saving
All conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call
`--buffer` manually. Every non-empty user message and Claude response is
recorded character-for-character; there is no length filter and no skip-pattern
filter. Make NO acknowledgement when a save succeeds — silence is correct.

## Recalled Memory Context
Every turn, the UserPromptSubmit hook adds a `# Recalled Memory Context` block
to your context. Read `## Top matches` first; use
`## Previous matching Q-A exchanges` as supporting context. When several
versions of the same content appear, the one with the newest `saved` time is
the current one. If the block contains a `> ⚠️ N3MC notice:` line, tell the
user what it says.

## Weak Matches
Records under `## Possibly related (weak matches — may be unrelated)` did not
pass the relevance floors and are often unrelated. Use one only when it is
actually relevant to the question; if it is not, ignore it and do not mention
it.

## Active RAG
When prior context would help, run a search proactively. **Always invoke via
`python -m n3memorycore.n3memory --search "<keywords>"`**, NOT the bare `n3mc`
console script. Claude Code spawns a bash subprocess whose `PATH` does not
reliably include the Python `Scripts/` directory where the `n3mc` entry-point
lives, so `n3mc --search ...` works in interactive shells but fails in
production with `command not found`. `python -m` resolves through the Python
interpreter (which is on `PATH`) and is portable across venvs and platforms.
Both invocation forms are auto-allowed via `permissions.allow`.

## Cut Exchanges
A Q-A exchange may end with a line like
`_(N more chunk(s) of this exchange omitted. Full text: ...)_`. If the omitted
part is needed for your answer, run the command shown there
(`python -m n3memorycore.n3memory --show-turn <turn_id>`) and read the full
text before answering.

## Recall Acknowledgment
When recalled memory actually shapes your reply (you are recalling something
saved earlier), open the reply with a short acknowledgment in the user's
language — e.g. "Pulling this from earlier memory in this session." or
「前回の回答がメモリに保存されています。」 If no relevant memory was found,
or it did not influence your answer, do NOT announce anything.

## Fatal-Failure Warning
If a save fails physically, surface the warning prominently:
> ⚠️ Physical save failed. Current memories may be lost.
'''


def _ensure_behavior_md(cwd=None):
    p = claude_paths(cwd)["behavior_md"]
    key = "behavior_md_edited:" + hashlib.sha256(_os.path.abspath(p).encode("utf-8")).hexdigest()[:12]
    _clear_notice("behavior_md_edited")  # the v1.4.0 key
    if not _os.path.exists(p):
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        _clear_notice(key)
        return "created"
    with open(p, "rb") as f:
        raw = f.read().decode("utf-8", errors="replace")
    first = raw.split("\n", 1)[0].strip()
    if first == _BEHAVIOR_MARK:
        _clear_notice(key)
        return "current"
    h16 = hashlib.sha256(raw.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]
    if h16 in _KNOWN_BEHAVIOR_MD_HASHES:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(p, p + f".bak-n3mc-{stamp}")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(_BEHAVIOR_MD_CONTENT)
        _clear_notice(key)
        return "updated"
    _notify(key, f"The rules file {_os.path.abspath(p)} has been edited by hand, so it was not updated. "
            "Merge the new template manually if needed.")
    return "edited"


_IMPORT_LINE_RE = re.compile(r"^[ \t]*@[^\n]*memory_context\.md[ \t]*$")
_COMMENT_STARTS = ("# N3MemoryCore", "# Path resolves to", "# If you have not yet run")


def _norm_ctx_path(p):
    return _os.path.normcase(_os.path.normpath(p.replace("\\", "/")))


def _retire_memory_context(cwd=None):
    cm = claude_paths(cwd)["claude_md"]
    if _os.path.exists(cm):
        with open(cm, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
        lines = raw.splitlines(keepends=True)
        drop = set()
        for i, ln in enumerate(lines):
            body = ln.rstrip("\r\n")
            if not _IMPORT_LINE_RE.match(body):
                continue
            path = _os.path.expanduser(body.strip()[1:].strip())
            if not _os.path.isabs(path) and not re.match(r"^[A-Za-z]:[\\/]", path):
                continue
            if _norm_ctx_path(path) != _norm_ctx_path(CONTEXT_FILE):
                continue
            drop.add(i)
            j = i - 1
            while j >= 0 and lines[j].strip() == "":
                j -= 1
            k = j
            while k >= 0 and lines[k].lstrip().startswith(_COMMENT_STARTS):
                k -= 1
            if k < j:
                drop.update(range(k + 1, i))
        if drop:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copy2(cm, cm + f".bak-n3mc-{stamp}")
            new = "".join(ln for i, ln in enumerate(lines) if i not in drop)
            with open(cm, "wb") as f:
                f.write(new.encode("utf-8"))
    if not _os.path.exists(CONTEXT_RETIRED_MARKER) and _os.path.exists(CONTEXT_FILE):
        _os.unlink(CONTEXT_FILE)
        with open(CONTEXT_RETIRED_MARKER, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())


def _housekeeping():
    now = time.time()
    try:
        for name in _os.listdir(MEMORY_DIR):
            if name.startswith("turn_id_") and name.endswith(".txt"):
                fp = _os.path.join(MEMORY_DIR, name)
                if now - _os.path.getmtime(fp) > 30 * 86400:
                    _os.unlink(fp)
    except OSError:
        pass
    try:
        if _os.path.exists(AUDIT_LOG) and _os.path.getsize(AUDIT_LOG) > AUDIT_ROTATE_BYTES:
            _os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
    except OSError as e:
        print(f"N3MC: could not rotate audit.log ({e})", file=sys.stderr)


def cmd_stop():
    for step in (_ensure_behavior_md, _retire_memory_context, _housekeeping):
        try:
            step()
        except Exception as e:  # noqa: BLE001
            print(f"N3MC: {step.__name__} failed ({type(e).__name__}: {e})", file=sys.stderr)
    return 0


# ============================================================ --init

PERMISSION_RULES = [
    "Bash(python -m n3memorycore.n3memory --search *)",
    "Bash(python -m n3memorycore.n3memory --buffer *)",
    "Bash(python -m n3memorycore.n3memory --list*)",
    "Bash(python -m n3memorycore.n3memory --repair*)",
    "Bash(python -m n3memorycore.n3memory --stop*)",
    "Bash(python -m n3memorycore.n3memory --show-turn *)",
    "Bash(python -m n3memorycore.n3memory --recall-thread *)",
    "Bash(n3mc --search *)",
    "Bash(n3mc --buffer *)",
    "Bash(n3mc --list*)",
    "Bash(n3mc --repair*)",
    "Bash(n3mc --stop*)",
    "Bash(n3mc --show-turn *)",
    "Bash(n3mc --recall-thread *)",
]

_OURS = {
    "UserPromptSubmit": ("n3mc-hook", "n3memorycore.n3mc_hook"),
    "Stop": ("n3mc-stop-hook", "n3memorycore.n3mc_stop_hook"),
}


def _settings_path():
    import pathlib
    return pathlib.Path(_os.path.expanduser("~/.claude/settings.json"))


def _tokens(command):
    import shlex
    command = command.replace("\\", "/")
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def is_own_hook(command, event):
    exe_name, module = _OURS[event]
    toks = _tokens(command)
    if not toks:
        return False
    first = _os.path.basename(toks[0]).lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first == exe_name:
        return True
    for i, t in enumerate(toks[:-1]):
        if t == "-m" and toks[i + 1] == module:
            return True
    pkg_dir = _norm(_os.path.dirname(_os.path.abspath(__file__)))
    script = module.split(".")[-1] + ".py"
    for t in toks[1:]:
        if _os.path.basename(t).lower() == script:
            d = _norm(_os.path.dirname(_os.path.expanduser(t)))
            if d == pkg_dir:
                return True
    return False


def _shell_path(p):
    import pathlib
    s = pathlib.Path(p).as_posix()
    return f'"{s}"' if " " in s else s


def _resolve_cmd(script_name):
    import pathlib
    import sysconfig
    folders = [sysconfig.get_path("scripts")]
    try:
        folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError):
        pass
    for folder in folders:
        for name in (script_name + ".exe", script_name):
            cand = pathlib.Path(folder) / name
            if cand.is_file():
                return _shell_path(cand)
    exe = shutil.which(script_name)
    if exe:
        return _shell_path(exe)
    module = {"n3mc-hook": "n3memorycore.n3mc_hook", "n3mc-stop-hook": "n3memorycore.n3mc_stop_hook"}[script_name]
    return f"{_shell_path(sys.executable)} -m {module}"


def cmd_init():
    settings_path = _settings_path()
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"{settings_path} is not valid JSON ({e}). Nothing was changed. "
                  "Fix the file, then run n3mc --init again.", file=sys.stderr)
            return 1
        perms = settings.get("permissions", {}) if isinstance(settings, dict) else None
        if (not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict)
                or not isinstance(perms, dict) or not isinstance(perms.get("allow", []), list)):
            print(f"{settings_path} has an unexpected structure. Nothing was changed.", file=sys.stderr)
            return 1
        before = json.loads(raw)
    else:
        raw, settings, before = None, {}, None
    hooks = settings.setdefault("hooks", {})
    for event, (exe_name, _module) in _OURS.items():
        kept = []
        for entry in hooks.get(event, []):
            inner = [h for h in entry.get("hooks", []) if not is_own_hook(h.get("command", ""), event)]
            for h in inner:
                c = h.get("command", "")
                if any(m in c for m in ("n3mc_hook", "n3mc-hook", "n3mc_stop_hook", "n3mc-stop-hook")):
                    print(f"Another N3MC hook is registered for {event} and was left in place: {c}")
            if inner:
                kept.append({**entry, "hooks": inner})
        kept.append({"hooks": [{"type": "command", "command": _resolve_cmd(exe_name)}]})
        hooks[event] = kept
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for rule in PERMISSION_RULES:
        if rule not in allow:
            allow.append(rule)
    if settings != before:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak-n3mc-{stamp}"))
        fd, tmp = tempfile.mkstemp(dir=settings_path.parent, prefix=".settings.json.")
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        _os.replace(tmp, settings_path)
    _load_config()
    conn = _open_db()
    conn.close()
    print("次のセッションから保存できます。 (Restart Claude Code to activate the hooks.)")
    return 0


# ============================================================ read-only commands

def _fmt_line(r):
    return "\t".join([r["id"], r["timestamp"], r["agent_name"] or "-",
                      (r["content"] or "")[:80].replace("\r", " ").replace("\n", " ")])


def _bounds(since, until):
    try:
        s = _parse_time_bound(since, False) if since else None
        u = _parse_time_bound(until, True) if until else None
    except ValueError as e:
        print(f"--since/--until: {e}", file=sys.stderr)
        return None
    if s and u and s > u:
        print("--since is later than --until", file=sys.stderr)
        return None
    return (s, u)


def cmd_list(since=None, until=None, fmt="text"):
    b = _bounds(since, until)
    if b is None:
        return 2
    if not _os.path.exists(DB_PATH):
        print(f"No DB found at {DB_PATH}", file=sys.stderr)
        print("Total: 0 records")
        return 0
    conn = get_connection(DB_PATH)
    try:
        rows = _database.get_all_memories(conn, b[0], b[1])
    finally:
        conn.close()
    for r in rows:
        if fmt == "jsonl":
            print(json.dumps({k: r[k] for k in ("id", "timestamp", "agent_name", "content")}, ensure_ascii=False))
        else:
            print(_fmt_line(r))
    print(f"Total: {len(rows)} records")
    return 0


def cmd_recall_thread(turn_id, before=2, after=2, fmt="text"):
    if not _os.path.exists(DB_PATH):
        print(f"No DB found at {DB_PATH}", file=sys.stderr)
        return 0
    conn = get_connection(DB_PATH)
    try:
        t = conn.execute("SELECT MIN(rowid) lo, MAX(rowid) hi FROM memories WHERE turn_id=?", (turn_id,)).fetchone()
        if t["lo"] is None:
            print("Total: 0 records")
            return 0
        prev = conn.execute("SELECT turn_id FROM memories WHERE turn_id IS NOT NULL AND turn_id != ? "
                            "GROUP BY turn_id HAVING MAX(rowid) < ? ORDER BY MAX(rowid) DESC LIMIT ?",
                            (turn_id, t["lo"], before)).fetchall()
        nxt = conn.execute("SELECT turn_id FROM memories WHERE turn_id IS NOT NULL AND turn_id != ? "
                           "GROUP BY turn_id HAVING MIN(rowid) > ? ORDER BY MIN(rowid) ASC LIMIT ?",
                           (turn_id, t["hi"], after)).fetchall()
        turns = [r[0] for r in prev] + [turn_id] + [r[0] for r in nxt]
        q = ",".join("?" * len(turns))
        rows = conn.execute(f"SELECT id, timestamp, agent_name, turn_id, content FROM memories "
                            f"WHERE turn_id IN ({q}) ORDER BY rowid", turns).fetchall()
    finally:
        conn.close()
    for r in rows:
        if fmt == "jsonl":
            print(json.dumps({k: r[k] for k in ("id", "timestamp", "agent_name", "turn_id", "content")},
                             ensure_ascii=False))
        else:
            print(_fmt_line(r))
    print(f"Total: {len(rows)} records")
    return 0


def cmd_show_turn(turn_id):
    if not _os.path.exists(DB_PATH):
        print(f"No DB found at {DB_PATH}", file=sys.stderr)
        return 0
    conn = get_connection(DB_PATH)
    try:
        rows = get_memories_by_turn_id(conn, turn_id)
    finally:
        conn.close()
    rows.sort(key=lambda r: 0 if r["content"].startswith("[user") else 1)
    for r in rows:
        print(f"--- {r['id']} saved={_processor._fmt_saved(r['timestamp'])} ---")
        print(r["content"])
    print(f"Total: {len(rows)} records")
    return 0


# ============================================================ --buffer / --search / --repair / --reembed

def cmd_buffer(text, agent_id=None):
    if text == "-":
        text = _read_stdin_text()
    text = sanitize_surrogates(text or "")
    if not text.strip():
        print("Skipped (empty).")
        return 0
    r = _save_record(text, agent_id, _env_session())
    if r.get("status") == "ok":
        print("Saved 1 record.")
        return 0
    if r.get("status") == "skipped":
        print(f"Skipped ({r.get('reason')}).")
        return 0
    return 1


def cmd_search(query, since=None, until=None):
    b = _bounds(since, until)
    if b is None:
        return 2
    out = _search_text(sanitize_surrogates(query or ""), _env_session(), b[0], b[1])
    sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return 0


def cmd_repair():
    if not _use_server():
        print(f"N3MC: --repair skipped ({_unavailable_reason()})", file=sys.stderr)
        return 0
    try:
        r = _post("/repair", {}, timeout=600)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: repair failed ({type(e).__name__}: {e})", file=sys.stderr)
        return 1
    n = r.get("count", 0)
    if n:
        print(f"Repaired {n} record(s).")
    return 0


def cmd_reembed():
    cfg = get_config()
    model = cfg["embed_model"]
    h = _get_health()
    if h and h.get("root") and _norm(h["root"]) == _norm(HOME_DIR):
        _request_shutdown()
        _wait_port_free()
    try:
        _processor.get_model(model)
        test = embed_passage("dimension probe", model)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: --reembed: could not load {model} ({type(e).__name__}: {e})", file=sys.stderr)
        return 1
    conn = _open_db()
    try:
        dim = int(get_meta(conn, "embed_dim", _database.EMBED_DIM))
        if len(test) != dim:
            print(f"N3MC: --reembed: {model} makes {len(test)}-dimensional vectors but memories_vec has {dim}; "
                  "a different dimension needs the manual procedure in §3 \"Embeddings\".", file=sys.stderr)
            return 1
        total = count_memories(conn)
        cursor = 0
        if get_meta(conn, "reembed_model") == model:
            cursor = int(get_meta(conn, "reembed_cursor", 0))
        else:
            set_meta(conn, "reembed_model", model)
            set_meta(conn, "reembed_cursor", 0)
            conn.commit()
        done = conn.execute("SELECT COUNT(*) FROM memories WHERE rowid <= ?", (cursor,)).fetchone()[0]
        while True:
            rows = conn.execute("SELECT rowid AS rid, content FROM memories WHERE rowid > ? ORDER BY rowid LIMIT 200",
                                (cursor,)).fetchall()
            if not rows:
                break
            vecs = [embed_passage(r["content"], model) for r in rows]
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for r, v in zip(rows, vecs):
                    conn.execute("DELETE FROM memories_vec WHERE rowid=?", (r["rid"],))
                    conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                 (r["rid"], serialize_vector(v)))
                cursor = rows[-1]["rid"]
                set_meta(conn, "reembed_cursor", cursor)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            done += len(rows)
            print(f"Re-embedded {done} / {total}", file=sys.stderr)
        set_meta(conn, "embed_model", model)
        set_meta(conn, "embed_dim", dim)
        set_meta(conn, "embed_model_at", datetime.now(timezone.utc).isoformat())
        delete_meta(conn, "reembed_model")
        delete_meta(conn, "reembed_cursor")
        conn.commit()
    finally:
        conn.close()
    _clear_notice("embed_model_mismatch")
    print(f"Re-embedded {total} record(s) with {model}.")
    return 0


# ============================================================ main

def build_parser():
    p = argparse.ArgumentParser(prog="n3mc", description="N3MemoryCore (Free): persistent memory for Claude Code")
    g = p.add_argument_group("commands")
    g.add_argument("--init", action="store_true", help="create ~/.n3mc and register the Claude Code hooks")
    g.add_argument("--buffer", metavar="TEXT", help="save text ('-' reads stdin)")
    g.add_argument("--search", metavar="QUERY", help="search memories")
    g.add_argument("--list", action="store_true", help="list records")
    g.add_argument("--recall-thread", metavar="TURN_ID", help="show the turns around a turn_id")
    g.add_argument("--show-turn", metavar="TURN_ID", help="show the full text of one exchange")
    g.add_argument("--repair", action="store_true", help="index records that lack vectors / FTS rows")
    g.add_argument("--reembed", action="store_true", help="re-embed all records with the configured model")
    g.add_argument("--stop", action="store_true", help="session-end clean-up (does not stop the server)")
    g.add_argument("--hook-submit", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--save-claude-turn", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--run-server", action="store_true", help=argparse.SUPPRESS)
    o = p.add_argument_group("options")
    o.add_argument("--agent-id", metavar="NAME", help="agent_name for --buffer")
    o.add_argument("--since", metavar="DATE")
    o.add_argument("--until", metavar="DATE")
    o.add_argument("--before", type=int, default=2)
    o.add_argument("--after", type=int, default=2)
    o.add_argument("--format", choices=("text", "jsonl"), default="text")
    return p


def main(argv=None):
    _reconfigure_streams()
    args = build_parser().parse_args(argv)
    try:
        if args.run_server:
            return cmd_run_server()
        if args.hook_submit:
            return cmd_hook_submit()
        if args.save_claude_turn:
            return cmd_save_claude_turn()
        if args.stop:
            return cmd_stop()
        if args.init:
            return cmd_init()
        if args.reembed:
            return cmd_reembed()
        if args.repair:
            return cmd_repair()
        if args.buffer is not None:
            return cmd_buffer(args.buffer, args.agent_id)
        if args.search is not None:
            return cmd_search(args.search, args.since, args.until)
        if args.list:
            return cmd_list(args.since, args.until, args.format)
        if args.recall_thread:
            return cmd_recall_thread(args.recall_thread, args.before, args.after, args.format)
        if args.show_turn:
            return cmd_show_turn(args.show_turn)
    except KeyboardInterrupt:
        return 130
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
