"""N3MemoryCore — main CLI + FastAPI server entry.

Run modes:
  python n3memory.py --server              # FastAPI server (background launched by CLI)
  python n3memory.py --buffer TEXT         # Save (also: --buffer - to read stdin)
  python n3memory.py --search QUERY        # Hybrid retrieval; writes memory_context.md
  python n3memory.py --list                # All records
  python n3memory.py --repair              # Index repair + one-time FTS migration
  python n3memory.py --stop                # Session-end @import setup
  python n3memory.py --hook-submit         # UserPromptSubmit hook entry (stdin JSON)

See N3MemoryCore_v1.2.0_Free_EN.md for the full spec.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# UTF-8 console on Windows cp932 (spec §3 Clean CLI)
for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

# Ensure core/ is on the path before importing.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "core"))

from core import database as db  # noqa: E402
from core import processor as proc  # noqa: E402


def _sanitize_for_sqlite(value):
    """Defense-in-depth wrapper for SQLite-bound text — strips lone UTF-16
    surrogates so the complete-preservation contract is not silently violated
    by a UnicodeEncodeError in `cursor.execute()`. See CHANGELOG v1.2.0."""
    return proc.sanitize_surrogates(value)


# ---------------------------------------------------------------------------
# Mojibake recovery (one-time, on first server startup after upgrade)
# ---------------------------------------------------------------------------
# Pre-1.2.0 builds on Windows misdecoded UTF-8 stdin via cp932, persisting
# mojibake (e.g. 「縺薙ｓ縺ｫ縺｡縺ｯ」 instead of 「こんにちは」). The recovery
# attempts a best-effort cp932 → utf-8 roundtrip; rows that round-trip
# cleanly are rewritten with a `[recovered] ` prefix so the operator can
# audit them, and the FTS index is resynced. Lossy `errors='replace'` rows
# from the original misdecoding cannot be fully restored — the prefix marks
# them as "we tried."
_MOJIBAKE_HINT_RE = re.compile(
    "[" + "".join(["縺", "繧", "菫", "繝", "髢", "蜈", "邨", "閧", "笆"]) + "]"
)


def _looks_mojibake(text):
    """Heuristic: 2+ hits of common cp932→utf-8 misdecoded code points."""
    if not text or not isinstance(text, str):
        return False
    return len(_MOJIBAKE_HINT_RE.findall(text)) >= 2


def _try_recover_mojibake(text):
    """Best-effort cp932 → utf-8 roundtrip. Returns recovered string or None."""
    if not _looks_mojibake(text):
        return None
    try:
        round_tripped = text.encode("cp932", errors="replace").decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return None
    # Sanity: recovery must reduce mojibake hint count and produce valid text.
    if _looks_mojibake(round_tripped):
        return None
    if not round_tripped.strip():
        return None
    return round_tripped


def run_mojibake_recovery(db_path):
    """One-time scan + best-effort cp932→utf-8 roundtrip on existing rows.

    Idempotent via marker file `MOJIBAKE_RECOVERED_MARKER`. Returns the count
    of rows rewritten."""
    if MOJIBAKE_RECOVERED_MARKER.exists():
        return 0
    if not db_path.exists():
        MOJIBAKE_RECOVERED_MARKER.write_text("ok", encoding="utf-8")
        return 0
    try:
        conn = db.init_db(db_path)
    except Exception as e:
        print(f"[N3MC] mojibake recovery: DB open failed: {e}", file=sys.stderr)
        return 0
    recovered = 0
    try:
        rows = conn.execute(
            "SELECT rowid, id, content FROM memories WHERE content IS NOT NULL"
        ).fetchall()
        for r in rows:
            recovered_text = _try_recover_mojibake(r["content"])
            if recovered_text is None:
                continue
            new_content = "[recovered] " + recovered_text
            try:
                # Preserve original timestamp — don't push recovered rows to "now"
                # because that would zero out the time_decay accumulated against
                # the original conversation moment.
                conn.execute(
                    "UPDATE memories SET content = ? WHERE rowid = ?",
                    (new_content, r["rowid"]),
                )
                conn.execute(
                    "DELETE FROM memories_fts WHERE rowid = ?", (r["rowid"],)
                )
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                    (r["rowid"], db.strip_fts_punctuation(new_content)),
                )
                conn.commit()
                recovered += 1
            except Exception as e:
                conn.rollback()
                print(
                    f"[N3MC] mojibake recovery: rewrite failed for {r['id']}: {e}",
                    file=sys.stderr,
                )
        if recovered:
            print(
                f"[N3MC] mojibake recovery: rewrote {recovered} row(s) "
                f"with [recovered] prefix",
                file=sys.stderr,
            )
        MOJIBAKE_RECOVERED_MARKER.write_text("ok", encoding="utf-8")
    finally:
        conn.close()
    return recovered


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
N3MC_ROOT = HERE
MEMORY_DIR = N3MC_ROOT / ".memory"
DB_PATH = MEMORY_DIR / "n3memory.db"
PID_FILE = MEMORY_DIR / "n3mc.pid"
AUDIT_LOG = MEMORY_DIR / "audit.log"
MEMORY_CONTEXT_MD = MEMORY_DIR / "memory_context.md"
TURN_ID_FILE = MEMORY_DIR / "turn_id.txt"
FTS_PUNCT_MARKER = MEMORY_DIR / "fts_punct_cleaned"
VEC_E5V2_MARKER = MEMORY_DIR / "vec_e5v2_migrated"
MOJIBAKE_RECOVERED_MARKER = MEMORY_DIR / "mojibake_recovered"
CONFIG_FILE = N3MC_ROOT / "config.json"


# ---------------------------------------------------------------------------
# Config (spec §3 / §3.5)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "owner_id": None,
    "local_id": None,
    "server_host": "127.0.0.1",
    "server_port": 18520,
    "dedup_threshold": 0.95,
    "half_life_days": 90,
    "bm25_min_threshold": 0.1,
    "search_result_limit": 20,
    "context_char_limit": 3000,
    "min_score": 0.2,
    "search_query_max_chars": 2000,
}


def _recover_id_from_db(field: str) -> Optional[str]:
    """spec §3.5: recover most-frequent owner_id/local_id from DB."""
    if not DB_PATH.exists():
        return None
    try:
        conn = db.get_connection(DB_PATH)
        row = conn.execute(
            f"SELECT {field} FROM memories WHERE {field} IS NOT NULL "
            f"GROUP BY {field} ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _load_config() -> dict:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            text = CONFIG_FILE.read_text(encoding="utf-8")
            if text.strip():
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
        except json.JSONDecodeError as e:
            print(
                f"[N3MC] WARNING: config.json is corrupted ({e}). "
                f"Recovering from DB.",
                file=sys.stderr,
            )

    changed = False
    if not cfg.get("owner_id"):
        cfg["owner_id"] = _recover_id_from_db("owner_id") or str(uuid.uuid4())
        changed = True
    if not cfg.get("local_id"):
        cfg["local_id"] = _recover_id_from_db("local_id") or str(uuid.uuid4())
        changed = True
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
            changed = True

    if changed:
        try:
            CONFIG_FILE.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            print(f"[N3MC] WARNING: failed to write config.json: {e}", file=sys.stderr)
    return cfg


# ---------------------------------------------------------------------------
# Audit log (spec §5 — written BEFORE anything can fail)
# ---------------------------------------------------------------------------
def write_audit(hook: str, raw: Any, payload: Any = None) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize before json.dumps — lone surrogates are valid in Python str
    # but produce un-decodable JSON when the file is later read on another
    # platform (e.g., to inspect audit trail or migrate to Pro).
    safe_raw = _sanitize_for_sqlite(raw) if isinstance(raw, str) else \
               json.dumps(_sanitize_for_sqlite(raw), ensure_ascii=False)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": hook,
        "raw": safe_raw,
        "payload": _sanitize_for_sqlite(payload),
    }
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[N3MC] audit log write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Multimodal payload helpers (spec §5)
# ---------------------------------------------------------------------------
_BASE64_IMG_RE = re.compile(r'data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]+')


def strip_images(payload: Any) -> Any:
    """Remove base64 image data so audit/save layers stay text-only."""
    if isinstance(payload, str):
        return _BASE64_IMG_RE.sub("[image omitted]", payload)
    if isinstance(payload, list):
        return [strip_images(x) for x in payload]
    if isinstance(payload, dict):
        return {k: strip_images(v) for k, v in payload.items()}
    return payload


def extract_text(message: Any) -> str:
    """Pull text content from a Claude Code prompt that may be multimodal."""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for item in message:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    txt = item.get("text") or ""
                    if txt:
                        parts.append(txt)
                elif "text" in item and isinstance(item["text"], str):
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    if isinstance(message, dict):
        if message.get("type") == "text":
            return message.get("text") or ""
        if "text" in message and isinstance(message["text"], str):
            return message["text"]
    return ""


# ---------------------------------------------------------------------------
# HTTP client / server lifecycle
# ---------------------------------------------------------------------------
def _server_url(cfg: dict) -> str:
    host = cfg.get("server_host", "127.0.0.1")
    port = cfg.get("server_port", 18520)
    return f"http://{host}:{port}"


def _ping_health(cfg: dict, timeout: float = 1.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"{_server_url(cfg)}/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _post(cfg: dict, path: str, body: dict, timeout: float = 30.0) -> Optional[dict]:
    import urllib.request
    import urllib.error
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_server_url(cfg)}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"status": "error", "message": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _get(cfg: dict, path: str, timeout: float = 30.0) -> Optional[dict]:
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"{_server_url(cfg)}{path}", timeout=timeout
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _port_open(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            return str(pid) in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _start_server(cfg: dict) -> int:
    """Spawn the server as a background subprocess. Returns PID."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc_h = subprocess.Popen(
        [sys.executable, str(__file__), "--server"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=(os.name != "nt"),
        creationflags=creationflags,
    )
    return proc_h.pid


def ensure_server(cfg: dict) -> bool:
    """Make sure the FastAPI server is up. Spec §3."""
    if _ping_health(cfg):
        return True

    # Try atomic PID create.
    try:
        fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            pid = _start_server(cfg)
            f.write(str(pid))
    except FileExistsError:
        # Another process is starting it — wait up to 60s for /health.
        existing_pid = _read_pid()
        if existing_pid and not _pid_alive(existing_pid):
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                pass
            return ensure_server(cfg)
        for _ in range(120):
            if _ping_health(cfg, timeout=0.3):
                return True
            time.sleep(0.5)
        return _ping_health(cfg, timeout=1.0)

    # Wait for our own start.
    for _ in range(120):
        if _ping_health(cfg, timeout=0.3):
            return True
        time.sleep(0.5)
    return _ping_health(cfg, timeout=1.0)


# ---------------------------------------------------------------------------
# FastAPI server (in-process)
# ---------------------------------------------------------------------------
try:
    from pydantic import BaseModel as _BaseModel

    class _BufferReq(_BaseModel):
        content: str
        agent_name: Optional[str] = "claude-code"
        local_id: Optional[str] = None
        turn_id: Optional[str] = None

    class _SearchReq(_BaseModel):
        query: str
except Exception:  # pragma: no cover
    _BufferReq = None
    _SearchReq = None


def _build_app(cfg: dict):
    from fastapi import FastAPI, HTTPException

    state: dict[str, Any] = {"cfg": cfg, "session_id": str(uuid.uuid4())}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        # DB integrity check on startup (spec §3.5)
        if DB_PATH.exists():
            try:
                tmp_conn = db.get_connection(DB_PATH)
                if not db.check_integrity(tmp_conn):
                    tmp_conn.close()
                    print(
                        "[N3MC] DB integrity_check failed — quarantining as .corrupt.bak",
                        file=sys.stderr,
                    )
                    db.quarantine_corrupt_db(DB_PATH)
                else:
                    tmp_conn.close()
            except Exception as e:
                print(f"[N3MC] integrity check error: {e}", file=sys.stderr)
        # Ensure schema once at startup. Per-request connections handle
        # SQLite's thread affinity restriction.
        bootstrap = db.init_db(DB_PATH)
        bootstrap.close()
        # One-time mojibake recovery for upgrades from pre-1.2.0 installs.
        # Idempotent via marker file (CHANGELOG v1.2.0).
        try:
            run_mojibake_recovery(DB_PATH)
        except Exception as e:
            print(f"[N3MC] mojibake recovery failed: {e}", file=sys.stderr)
        try:
            proc._get_model()
        except Exception as e:
            print(f"[N3MC] embedding model load failed: {e}", file=sys.stderr)
        yield

    def _conn():
        return db.init_db(DB_PATH)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/buffer")
    def buffer(req: _BufferReq):
        # Sanitize at the entry point — Windows subprocess pipes can deliver
        # lone UTF-16 surrogates that crash sqlite3 binding (CHANGELOG v1.2.0).
        content = _sanitize_for_sqlite(req.content or "")
        if not content.strip():
            raise HTTPException(status_code=400, detail="empty content")
        conn = _conn()
        try:
            if db.check_exact_duplicate(conn, content):
                return {"status": "ok", "count": 0, "duplicate": "exact"}
            try:
                embedding = proc.embed_passage(content)
            except Exception as e:
                print(f"[N3MC] embedding failed at save: {e}", file=sys.stderr)
                embedding = None

            # Vector-similarity dedup (spec §5)
            if embedding is not None:
                hits = db.search_vector(conn, embedding, k=1)
                if hits:
                    cs = proc.cosine_sim_from_l2(hits[0]["distance"])
                    if cs >= cfg.get("dedup_threshold", 0.95):
                        return {"status": "ok", "count": 0, "duplicate": "near"}

            mem_id, _rowid = db.insert_memory(
                conn,
                content,
                embedding,
                owner_id=cfg["owner_id"],
                local_id=req.local_id or cfg["local_id"],
                agent_name=req.agent_name,
                turn_id=req.turn_id,
            )
            return {"status": "ok", "count": 1, "id": mem_id}
        finally:
            conn.close()

    @app.post("/search")
    def search(req: _SearchReq):
        q = (req.query or "")[: cfg.get("search_query_max_chars", 2000)]
        if not q.strip():
            return {"results": [], "pairs": {}}
        conn = _conn()
        try:
            try:
                qvec = proc.embed_query(q)
            except Exception as e:
                print(f"[N3MC] embed_query failed: {e}", file=sys.stderr)
                qvec = None
            k_fetch = max(50, cfg.get("search_result_limit", 20) * 5)
            vec_hits = db.search_vector(conn, qvec, k=k_fetch) if qvec else []
            fts_hits = db.search_fts(conn, q, k=k_fetch)

            merged: dict[int, dict] = {}
            for h in vec_hits:
                cs = proc.cosine_sim_from_l2(h["distance"])
                merged[h["rowid"]] = {**h, "cos_sim": cs, "bm25_raw": None}
            for h in fts_hits:
                row = merged.setdefault(
                    h["rowid"], {**h, "cos_sim": 0.0}
                )
                row["bm25_raw"] = h["bm25_raw"]
                for f in ("id", "content", "timestamp", "owner_id", "local_id",
                          "agent_name", "turn_id"):
                    row.setdefault(f, h.get(f))

            bm25_max_abs = max(
                (abs(r["bm25_raw"]) for r in merged.values() if r.get("bm25_raw") is not None),
                default=1.0,
            )
            threshold = cfg.get("bm25_min_threshold", 0.1)
            half_life = cfg.get("half_life_days", 90)
            local_id = cfg.get("local_id")

            scored = []
            for r in merged.values():
                kw = proc.keyword_relevance(r.get("bm25_raw"), bm25_max_abs, threshold)
                decay = proc.time_decay(r.get("timestamp", ""), half_life)
                local_b = proc.local_bias(r.get("local_id"), local_id)
                score = proc.compute_score(
                    r["cos_sim"], kw, decay, local_b=local_b
                )
                scored.append({
                    "id": r.get("id"),
                    "content": r.get("content"),
                    "timestamp": r.get("timestamp"),
                    "agent_name": r.get("agent_name"),
                    "turn_id": r.get("turn_id"),
                    "score": round(score, 4),
                    "cos_sim": round(r["cos_sim"], 4),
                    "keyword": round(kw, 4),
                    "time_decay": round(decay, 4),
                })

            min_score = cfg.get("min_score", 0.2)
            scored = [s for s in scored if s["score"] >= min_score]
            scored.sort(key=lambda x: x["score"], reverse=True)
            scored = scored[: cfg.get("search_result_limit", 20)]

            pairs: dict[str, list[dict]] = {}
            for s in scored:
                tid = s.get("turn_id")
                if tid and tid not in pairs:
                    rows = db.get_memories_by_turn_id(conn, tid)
                    ordered_user = [dict(r) for r in rows if (r["content"] or "").startswith("[user")]
                    ordered_claude = [dict(r) for r in rows if (r["content"] or "").startswith("[claude")]
                    pairs[tid] = ordered_user + ordered_claude
            return {"results": scored, "pairs": pairs}
        finally:
            conn.close()

    @app.post("/repair")
    def repair():
        conn = _conn()
        try:
            repaired = 0
            offset = 0
            batch = 200
            while True:
                unindexed = db.find_unindexed_memories(conn, limit=batch, offset=offset)
                if not unindexed:
                    break
                for u in unindexed:
                    if u["fts_missing"]:
                        conn.execute(
                            "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                            (u["rowid"], db.strip_fts_punctuation(u["content"] or "")),
                        )
                    if u["vec_missing"]:
                        try:
                            emb = proc.embed_passage(u["content"] or "")
                            conn.execute(
                                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                (u["rowid"], db.serialize_vector(emb)),
                            )
                        except Exception as e:
                            print(f"[N3MC] repair embed failed for {u['id']}: {e}",
                                  file=sys.stderr)
                    repaired += 1
                conn.commit()
                offset += batch

            if not FTS_PUNCT_MARKER.exists():
                offset = 0
                while True:
                    rows = list(conn.execute(
                        "SELECT rowid, content FROM memories ORDER BY rowid ASC "
                        "LIMIT ? OFFSET ?", (batch, offset)
                    ))
                    if not rows:
                        break
                    for r in rows:
                        cleaned = db.strip_fts_punctuation(r["content"] or "")
                        conn.execute(
                            "DELETE FROM memories_fts WHERE rowid = ?", (r["rowid"],)
                        )
                        conn.execute(
                            "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                            (r["rowid"], cleaned),
                        )
                    conn.commit()
                    offset += batch
                FTS_PUNCT_MARKER.write_text("ok", encoding="utf-8")

            if not VEC_E5V2_MARKER.exists():
                VEC_E5V2_MARKER.write_text("ok", encoding="utf-8")

            return {"status": "ok", "count": repaired}
        finally:
            conn.close()

    @app.get("/list")
    def list_all():
        conn = _conn()
        try:
            rows = db.get_all_memories(conn)
            return {
                "records": [
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "timestamp": r["timestamp"],
                        "agent_name": r["agent_name"],
                        "turn_id": r["turn_id"],
                    }
                    for r in rows
                ],
                "total": len(rows),
            }
        finally:
            conn.close()

    @app.delete("/delete/{memory_id}")
    def delete_one(memory_id: str):
        conn = _conn()
        try:
            ok = db.delete_memory(conn, memory_id)
            if not ok:
                raise HTTPException(status_code=404, detail="not found")
            return {"status": "ok"}
        finally:
            conn.close()

    return app


def run_server() -> None:
    """Boot the FastAPI server with uvicorn (entry of `--server`)."""
    cfg = _load_config()
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    # PID file already written by ensure_server's parent; refresh to current PID.
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    import uvicorn
    app = _build_app(cfg)
    try:
        uvicorn.run(
            app,
            host=cfg.get("server_host", "127.0.0.1"),
            port=cfg.get("server_port", 18520),
            log_level="warning",
        )
    finally:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# CLI implementations
# ---------------------------------------------------------------------------
def _buffer_direct(content: str, cfg: dict, *, agent_name: str = "claude-code",
                   turn_id: Optional[str] = None) -> bool:
    """Fallback: insert directly into SQLite without an embedding (spec §5).

    Spec §3.5: this path runs PRAGMA integrity_check before opening — a
    corrupted DB is quarantined to .corrupt.bak and a fresh one is created
    so the write can still proceed (rather than silently dropping the user's
    content). Also strips lone surrogates to prevent UnicodeEncodeError on
    cursor.execute() (CHANGELOG v1.2.0)."""
    content = _sanitize_for_sqlite(content)
    try:
        if DB_PATH.exists():
            try:
                probe = db.get_connection(DB_PATH)
                ok = db.check_integrity(probe)
                probe.close()
                if not ok:
                    print(
                        "[N3MC] DB integrity_check failed in fallback path — "
                        "quarantining as .corrupt.bak",
                        file=sys.stderr,
                    )
                    db.quarantine_corrupt_db(DB_PATH)
            except Exception as e:
                print(f"[N3MC] integrity check error in fallback: {e}",
                      file=sys.stderr)
        conn = db.init_db(DB_PATH)
        if db.check_exact_duplicate(conn, content):
            conn.close()
            return False
        db.insert_memory(
            conn, content, None,
            owner_id=cfg["owner_id"], local_id=cfg["local_id"],
            agent_name=agent_name, turn_id=turn_id,
        )
        conn.close()
        return True
    except Exception as e:
        print(f"[N3MC] _buffer_direct failed: {e}", file=sys.stderr)
        print("⚠️ Physical save failed. Current memories may be lost.",
              file=sys.stderr)
        return False


def cmd_buffer(content: str, cfg: dict, *, agent_name: Optional[str],
               turn_id: Optional[str] = None) -> int:
    if not content.strip():
        return 0
    body = {"content": content, "agent_name": agent_name or "claude-code"}
    if turn_id:
        body["turn_id"] = turn_id
    if ensure_server(cfg):
        resp = _post(cfg, "/buffer", body)
        if resp and resp.get("status") == "ok":
            return 0
        print(f"[N3MC] /buffer failed: {resp}", file=sys.stderr)
    # Fallback path
    saved = _buffer_direct(content, cfg, agent_name=agent_name or "claude-code",
                           turn_id=turn_id)
    return 0 if saved else 1


_EMPTY_CONTEXT_MD = "# Recalled Memory Context\n\n_No relevant memories found._\n"


def _degraded_context_md(reason: str) -> str:
    return (
        "# Recalled Memory Context\n\n"
        f"_(memory search unavailable: {reason})_\n"
    )


def _format_memory_context(payload: dict) -> str:
    """Render /search response into memory_context.md."""
    out: list[str] = ["# Recalled Memory Context", ""]
    pairs = payload.get("pairs") or {}
    pair_ids: set[str] = set()
    for tid, rows in pairs.items():
        for r in rows:
            pair_ids.add(r.get("id"))

    if pairs:
        out.append("## Previous matching exchange(s)")
        out.append("")
        for tid, rows in pairs.items():
            for r in rows:
                out.append(f"- {r.get('content','')}")
            out.append("")

    other = [r for r in (payload.get("results") or []) if r.get("id") not in pair_ids]
    if other:
        out.append("## Other memories")
        out.append("")
        for r in other:
            score = r.get("score")
            score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "?"
            out.append(
                f"- ({score_str}) "
                f"[{r.get('agent_name') or '?'}] "
                f"{(r.get('content') or '').strip()}"
            )
    if not pairs and not other:
        out.append("_No relevant memories found._")
    return "\n".join(out) + "\n"


def _write_memory_context(md: str) -> None:
    """Always emit fresh memory context to BOTH stdout (hook channel) and the
    @import file, even on failure. Prevents stale-context leakage to Claude."""
    MEMORY_CONTEXT_MD.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_CONTEXT_MD.write_text(md, encoding="utf-8")
    print(md)


def cmd_search(query: str, cfg: dict) -> int:
    """Run a search and refresh the memory context. Always rewrites
    memory_context.md and prints to stdout — even on empty/failure paths —
    so Claude never sees stale memory."""
    if not query or not query.strip():
        _write_memory_context(_EMPTY_CONTEXT_MD)
        return 0
    if not ensure_server(cfg):
        print("[N3MC] server unavailable — search skipped", file=sys.stderr)
        _write_memory_context(_degraded_context_md("server unreachable"))
        return 1
    resp = _post(cfg, "/search", {"query": query[: cfg.get("search_query_max_chars", 2000)]})
    if not resp or "results" not in resp:
        print(f"[N3MC] /search failed: {resp}", file=sys.stderr)
        reason = (resp or {}).get("message", "unknown error") if isinstance(resp, dict) else "unknown"
        _write_memory_context(_degraded_context_md(str(reason)[:120]))
        return 1
    _write_memory_context(_format_memory_context(resp))
    return 0


def cmd_list(cfg: dict) -> int:
    if not ensure_server(cfg):
        # Read directly from DB as a fallback so --list works offline.
        try:
            conn = db.init_db(DB_PATH)
            rows = db.get_all_memories(conn)
            for r in rows:
                head = (r["content"] or "")[:80].replace("\n", " ").replace("\r", " ")
                print(f"{r['id']}\t{r['timestamp']}\t{r['agent_name'] or '-'}\t{head}")
            print(f"Total: {len(rows)} records")
            conn.close()
            return 0
        except Exception as e:
            print(f"[N3MC] --list failed: {e}", file=sys.stderr)
            return 1
    resp = _get(cfg, "/list")
    if not resp or "records" not in resp:
        print(f"[N3MC] /list failed: {resp}", file=sys.stderr)
        return 1
    for r in resp["records"]:
        # Spec §6.2: first 80 characters of content (full content, not first line).
        head = (r["content"] or "")[:80].replace("\n", " ").replace("\r", " ")
        print(f"{r['id']}\t{r['timestamp']}\t{r.get('agent_name') or '-'}\t{head}")
    print(f"Total: {resp.get('total', 0)} records")
    return 0


def cmd_repair(cfg: dict) -> int:
    if not ensure_server(cfg):
        print("[N3MC] server unavailable — repair skipped", file=sys.stderr)
        return 1
    resp = _post(cfg, "/repair", {})
    if resp and resp.get("status") == "ok":
        if resp.get("count", 0) > 0:
            print(f"[N3MC] repaired {resp['count']} record(s)", file=sys.stderr)
        return 0
    print(f"[N3MC] /repair failed: {resp}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# --stop: idempotent @import setup (spec §4)
# ---------------------------------------------------------------------------
N3MC_BEHAVIOR_MD = """# N3MemoryCore Behavioral Guidelines

These guidelines apply to every Claude Code session in this project. They are
loaded automatically from `.claude/rules/` at session start.

## Fully Automatic Saving
All conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call
`--buffer` manually. Every non-empty user message and Claude response is
recorded character-for-character; there is no length filter and no skip-pattern
filter. Make NO acknowledgement when a save succeeds — silence is correct.

## Active RAG
When prior context would help, run `--search "<keywords>"` proactively. The
command is auto-allowed via `permissions.allow`.

## Recall Acknowledgment
When `--search` results actually shape your reply (you are recalling something
saved earlier), open the reply with a short acknowledgment in the user's
language — e.g. "Pulling this from earlier memory in this session." or
「前回の回答がメモリに保存されています。」 If no relevant memory was found,
or it did not influence your answer, do NOT announce anything.

## Fatal-Failure Warning
If a save fails physically, surface the warning prominently:
> ⚠️ Physical save failed. Current memories may be lost.
"""


def _project_root_for_claude_md() -> Path:
    """Locate the project root (the directory whose .claude/ should hold CLAUDE.md).

    Prefers N3MC_ROOT itself when it has a .claude/ — i.e. the n3mc-free repo
    layout where the package and the project share one root. Otherwise falls
    back to the parent (the spec's nested-N3MemoryCore layout).
    """
    if (N3MC_ROOT / ".claude").exists():
        return N3MC_ROOT
    parent = N3MC_ROOT.parent
    if (parent / ".claude").exists():
        return parent
    return N3MC_ROOT


def _ensure_behavior_file(project_root: Path) -> None:
    rules_dir = project_root / ".claude" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    target = rules_dir / "n3mc-behavior.md"
    if target.exists():
        return
    target.write_text(N3MC_BEHAVIOR_MD, encoding="utf-8")


_LEGACY_ZONE_RE = re.compile(
    r"<!--\s*N3MC_AUTO_START\s*-->.*?<!--\s*N3MC_AUTO_END\s*-->\n?",
    re.DOTALL,
)


def _ensure_at_import(project_root: Path) -> None:
    claude_md = project_root / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    rel = MEMORY_CONTEXT_MD.resolve().as_posix()
    try:
        rel = os.path.relpath(MEMORY_CONTEXT_MD.resolve(), project_root).replace("\\", "/")
    except ValueError:
        pass
    import_line = f"@{rel}"

    if not claude_md.exists():
        claude_md.write_text(
            "# CLAUDE.md\n\n"
            "# (User-managed content above)\n\n"
            f"{import_line}\n",
            encoding="utf-8",
        )
        return

    text = claude_md.read_text(encoding="utf-8")
    new = _LEGACY_ZONE_RE.sub("", text)
    if import_line in new:
        if new != text:
            claude_md.write_text(new, encoding="utf-8")
        return
    if not new.endswith("\n"):
        new += "\n"
    new += import_line + "\n"
    claude_md.write_text(new, encoding="utf-8")


def cmd_stop(cfg: dict) -> int:
    project_root = _project_root_for_claude_md()
    try:
        _ensure_behavior_file(project_root)
        _ensure_at_import(project_root)
    except Exception as e:
        print(f"[N3MC] --stop failed: {e}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# --hook-submit: UserPromptSubmit single-process pipeline (spec §4.5)
# ---------------------------------------------------------------------------
def _label_chunks(text: str, role: str) -> list[str]:
    """Apply [role] / [role i/N] prefixes to chunks. role ∈ {user, claude}."""
    chunks = proc.chunk_text(text)
    if not chunks:
        return []
    if len(chunks) == 1:
        return [f"[{role}] {chunks[0]}"]
    n = len(chunks)
    return [f"[{role} {i+1}/{n}] {c}" for i, c in enumerate(chunks)]


def _save_with_chunks(text: str, role: str, cfg: dict, *, turn_id: Optional[str]) -> int:
    """Save a (possibly long) text with role-prefixed chunks."""
    purified = proc.purify_text(text)
    labelled = _label_chunks(purified, role)
    saved = 0
    for chunk in labelled:
        rc = cmd_buffer(chunk, cfg, agent_name="claude-code", turn_id=turn_id)
        if rc == 0:
            saved += 1
    return saved


def _read_turn_id() -> Optional[str]:
    try:
        v = TURN_ID_FILE.read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        return None


def _write_turn_id(tid: str) -> None:
    try:
        TURN_ID_FILE.write_text(tid, encoding="utf-8")
    except Exception as e:
        print(f"[N3MC] turn_id write failed: {e}", file=sys.stderr)


def _clear_turn_id() -> None:
    try:
        TURN_ID_FILE.unlink()
    except FileNotFoundError:
        pass


def cmd_hook_submit(cfg: dict, payload: dict) -> int:
    raw_message = payload.get("message", payload.get("prompt", ""))
    last_assistant = _sanitize_for_sqlite(
        payload.get("last_assistant_message", "") or ""
    )
    user_text = _sanitize_for_sqlite(extract_text(raw_message))

    # Repair (always best-effort)
    if ensure_server(cfg):
        _post(cfg, "/repair", {})

    # Save the previous Claude turn (if any) under the existing turn_id.
    prev_turn_id = _read_turn_id()
    if last_assistant:
        if not prev_turn_id:
            prev_turn_id = str(uuid.uuid4())
        _save_with_chunks(last_assistant, "claude", cfg, turn_id=prev_turn_id)
        _clear_turn_id()

    # Spec §5: image-only prompts still trigger /search. cmd_search handles
    # the empty-query case by emitting fresh "_No relevant memories found._"
    # context, which keeps memory_context.md non-stale across image-only turns.
    cmd_search(user_text, cfg)

    # Save the user message (only when there IS user text to save).
    if user_text:
        new_turn_id = str(uuid.uuid4())
        _save_with_chunks(user_text, "user", cfg, turn_id=new_turn_id)
        _write_turn_id(new_turn_id)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing & main
# ---------------------------------------------------------------------------
def _read_buffer_arg(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    return arg


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="n3memory", add_help=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--server", action="store_true", help="Run FastAPI server")
    g.add_argument("--buffer", metavar="TEXT", help="Save TEXT (use '-' for stdin)")
    g.add_argument("--search", metavar="QUERY", help="Hybrid search")
    g.add_argument("--list", action="store_true", help="List all records")
    g.add_argument("--repair", action="store_true", help="Repair indexes")
    g.add_argument("--stop", action="store_true", help="Session-end @import setup")
    g.add_argument("--hook-submit", action="store_true",
                   help="UserPromptSubmit hook entry (reads stdin JSON)")
    g.add_argument("--save-claude-turn", action="store_true",
                   help="Stop hook helper: save last_assistant_message (stdin JSON)")
    p.add_argument("--agent-id", "--agent-name", dest="agent_name",
                   default="claude-code", help="Agent display name tag")
    args = p.parse_args(argv)

    if args.server:
        run_server()
        return 0

    cfg = _load_config()

    if args.buffer is not None:
        text = _read_buffer_arg(args.buffer)
        return cmd_buffer(text, cfg, agent_name=args.agent_name)
    if args.search is not None:
        return cmd_search(args.search, cfg)
    if args.list:
        return cmd_list(cfg)
    if args.repair:
        return cmd_repair(cfg)
    if args.stop:
        return cmd_stop(cfg)
    if args.hook_submit:
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"message": raw}
        # Spec §5: audit.log captures the raw multimodal payload verbatim —
        # last-resort transcript. Images are NOT stripped here; the strip is
        # only for the downstream save path (extract_text already drops them).
        write_audit("UserPromptSubmit", raw, payload)
        return cmd_hook_submit(cfg, payload)
    if args.save_claude_turn:
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"message": raw}
        last_assistant = _sanitize_for_sqlite(
            payload.get("last_assistant_message") or ""
        )
        if not last_assistant.strip():
            return 0
        turn_id = _read_turn_id() or str(uuid.uuid4())
        _save_with_chunks(last_assistant, "claude", cfg, turn_id=turn_id)
        _clear_turn_id()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
