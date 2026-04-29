import sys
import os

for _s_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _s_name, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

import argparse
import json
import logging
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from .paths import (
    HOME_DIR, MEMORY_DIR, DB_PATH, PID_FILE, TURN_ID_FILE,
    CONTEXT_FILE, AUDIT_LOG, CONFIG_FILE, MOJIBAKE_RECOVERED, claude_paths,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict = {
    "owner_id":              None,
    "local_id":              None,
    "server_port":           18520,
    "embed_model":           "intfloat/multilingual-e5-base",
    "dedup_threshold":       0.95,
    "half_life_days":        90,
    "bm25_min_threshold":    0.1,
    "search_result_limit":   20,
    "context_char_limit":    3000,
    "min_score":             0.2,
    "search_query_max_chars": 2000,
}


def _load_config() -> dict:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    cfg = dict(_DEFAULT_CONFIG)

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"Warning: config.json parse error: {e}", file=sys.stderr)
            # Recover owner_id / local_id from DB
            try:
                import sqlite3
                if os.path.exists(DB_PATH):
                    conn = sqlite3.connect(DB_PATH)
                    for col in ('owner_id', 'local_id'):
                        row = conn.execute(
                            f"SELECT {col} FROM memories "
                            f"GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 1"
                        ).fetchone()
                        if row:
                            cfg[col] = row[0]
                    conn.close()
            except Exception:
                pass

    changed = False
    if not cfg.get('owner_id'):
        cfg['owner_id'] = str(uuid.uuid4())
        changed = True
    if not cfg.get('local_id'):
        cfg['local_id'] = str(uuid.uuid4())
        changed = True
    for k, v in _DEFAULT_CONFIG.items():
        if k not in cfg or cfg[k] is None:
            cfg[k] = v
            changed = True

    if changed:
        _save_config(cfg)
    return cfg


def _save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(cfg: dict, path: str, payload: dict, timeout: int = 30) -> dict:
    import urllib.request
    url = f"http://127.0.0.1:{cfg['server_port']}{path}"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _get(cfg: dict, path: str, timeout: int = 30) -> dict:
    import urllib.request
    url = f"http://127.0.0.1:{cfg['server_port']}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _health_check(cfg: dict) -> bool:
    try:
        r = _get(cfg, '/health', timeout=5)
        return r.get('status') == 'ok'
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

def _process_alive(pid: int) -> bool:
    try:
        if sys.platform == 'win32':
            import ctypes
            SYNCHRONIZE = 0x100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            ret = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
            return ret == 0x102  # WAIT_TIMEOUT → still alive
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _wait_for_server(cfg: dict, max_wait: int = 60) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _health_check(cfg):
            return True
        time.sleep(0.5)
    return False


def ensure_server(cfg: dict) -> bool:
    os.makedirs(MEMORY_DIR, exist_ok=True)

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            if _process_alive(pid) and _health_check(cfg):
                return True
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

    proc = subprocess.Popen(
        [sys.executable, '-m', 'n3memorycore.n3memory', '--run-server'],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        with open(PID_FILE, 'x') as f:
            f.write(str(proc.pid))
    except FileExistsError:
        pass

    if not _wait_for_server(cfg, max_wait=60):
        print("Warning: N3MC server did not start in time.", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Direct buffer fallback (no embedding)
# ---------------------------------------------------------------------------

def _buffer_direct(
    content: str,
    cfg: dict,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> None:
    from .core.database import (
        get_connection, init_db, migrate_schema,
        insert_memory, check_exact_duplicate,
    )
    from .core.processor import sanitize_surrogates
    from uuid_extensions import uuid7

    # Strip lone UTF-16 surrogates before SQLite binding (Windows cp932 guard).
    content = sanitize_surrogates(content)

    conn = get_connection(DB_PATH)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result and result[0] != 'ok':
            conn.close()
            _handle_corrupt_db()
            conn = get_connection(DB_PATH)
            init_db(conn)

        migrate_schema(conn)
        if check_exact_duplicate(conn, content):
            return

        ts = datetime.now(timezone.utc).isoformat()
        insert_memory(
            conn, str(uuid7()), content, ts,
            owner_id=cfg['owner_id'],
            local_id=cfg.get('local_id'),
            agent_name=agent_name,
            session_id=session_id,
            turn_id=turn_id,
        )
    finally:
        conn.close()


def _handle_corrupt_db() -> None:
    import shutil
    corrupt = DB_PATH + '.corrupt.bak'
    try:
        shutil.move(DB_PATH, corrupt)
    except Exception:
        pass
    print(
        f"Warning: DB corruption detected. Renamed to {corrupt}. "
        "A new empty DB will be created.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Mojibake recovery (one-time, on first server startup after upgrade)
# ---------------------------------------------------------------------------
# Pre-1.2.0 builds on Windows misdecoded UTF-8 stdin via cp932, persisting
# mojibake (e.g. 「縺薙ｓ縺ｫ縺｡縺ｯ」 instead of 「こんにちは」). The recovery
# attempts a best-effort cp932 → utf-8 roundtrip; rows that round-trip cleanly
# are rewritten with a `[recovered] ` prefix so the operator can audit them,
# and the FTS index is resynced. Lossy `errors='replace'` rows from the
# original misdecoding cannot be fully restored — the prefix marks them as
# "we tried."
import re as _re
_MOJIBAKE_HINT_RE = _re.compile(
    "[" + "".join(["縺", "繧", "菫", "繝", "髢", "蜈", "邨", "閧", "笆"]) + "]"
)


def _looks_mojibake(text):
    if not text or not isinstance(text, str):
        return False
    return len(_MOJIBAKE_HINT_RE.findall(text)) >= 2


def _try_recover_mojibake(text):
    if not _looks_mojibake(text):
        return None
    try:
        round_tripped = text.encode('cp932', errors='replace').decode(
            'utf-8', errors='replace'
        )
    except Exception:
        return None
    if _looks_mojibake(round_tripped):
        return None
    if not round_tripped.strip():
        return None
    return round_tripped


def run_mojibake_recovery() -> int:
    """One-time scan + best-effort cp932->utf-8 roundtrip on existing rows.

    Idempotent via marker file `MOJIBAKE_RECOVERED`. Returns the count of rows
    rewritten.
    """
    if os.path.exists(MOJIBAKE_RECOVERED):
        return 0
    if not os.path.exists(DB_PATH):
        with open(MOJIBAKE_RECOVERED, 'w', encoding='utf-8') as f:
            f.write('ok')
        return 0

    from .core.database import get_connection, strip_fts_punctuation
    try:
        conn = get_connection(DB_PATH)
    except Exception as e:
        print(f"[N3MC] mojibake recovery: DB open failed: {e}", file=sys.stderr)
        return 0

    recovered = 0
    try:
        rows = conn.execute(
            "SELECT rowid, id, content FROM memories WHERE content IS NOT NULL"
        ).fetchall()
        for r in rows:
            recovered_text = _try_recover_mojibake(r['content'])
            if recovered_text is None:
                continue
            new_content = '[recovered] ' + recovered_text
            try:
                conn.execute(
                    "UPDATE memories SET content = ? WHERE rowid = ?",
                    (new_content, r['rowid']),
                )
                conn.execute(
                    "DELETE FROM memories_fts WHERE rowid = ?", (r['rowid'],)
                )
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                    (r['rowid'], strip_fts_punctuation(new_content)),
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
        with open(MOJIBAKE_RECOVERED, 'w', encoding='utf-8') as f:
            f.write('ok')
    finally:
        conn.close()
    return recovered


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI
    from pydantic import BaseModel

    class BufferRequest(BaseModel):
        content: str
        agent_name: Optional[str] = None
        local_id: Optional[str] = None
        session_id: Optional[str] = None
        turn_id: Optional[str] = None

    class SearchRequest(BaseModel):
        query: str
        session_id: Optional[str] = None

    # Global server state (populated in lifespan)
    _srv_cfg: dict = {}
    _srv_session: str = ""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _srv_cfg, _srv_session
        _srv_cfg = _load_config()
        _srv_session = os.environ.get('N3MC_SESSION_ID', str(uuid.uuid4()))
        os.makedirs(MEMORY_DIR, exist_ok=True)

        from .core.database import get_connection, init_db, migrate_schema

        conn = get_connection(DB_PATH)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()
            if ok and ok[0] != 'ok':
                conn.close()
                _handle_corrupt_db()
                conn = get_connection(DB_PATH)
            init_db(conn)
            migrate_schema(conn)
        finally:
            conn.close()

        try:
            from .core.processor import get_model
            get_model(_srv_cfg.get('embed_model'))
        except Exception as e:
            logger.warning(f"Model preload failed: {e}")

        # One-time cp932->utf-8 mojibake recovery for pre-1.2.0 rows.
        try:
            run_mojibake_recovery()
        except Exception as e:
            logger.warning(f"Mojibake recovery skipped: {e}")

        yield

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    async def ep_health():
        return {'status': 'ok'}

    @app.post('/buffer')
    async def ep_buffer(req: BufferRequest):
        from .core.database import (
            get_connection, check_exact_duplicate, insert_memory,
            search_vector,
        )
        from .core.processor import embed_passage, cosine_sim_from_l2, purify_text
        from uuid_extensions import uuid7

        content = purify_text(req.content) if req.content else ''
        if not content.strip():
            return {'status': 'skipped', 'count': 0, 'reason': 'empty'}

        local   = req.local_id   or _srv_cfg.get('local_id')
        sess    = req.session_id or _srv_session
        tid     = req.turn_id
        agent   = req.agent_name
        thresh  = _srv_cfg.get('dedup_threshold', 0.95)

        conn = get_connection(DB_PATH)
        try:
            if check_exact_duplicate(conn, content):
                return {'status': 'skipped', 'count': 0, 'reason': 'exact_duplicate'}

            vec = None
            try:
                vec = embed_passage(content)
            except Exception as e:
                logger.warning(f"Embed error: {e}")
            if vec is not None:
                try:
                    rows = search_vector(conn, vec, k=5)
                    for row in rows:
                        if cosine_sim_from_l2(row['distance']) >= thresh:
                            return {'status': 'skipped', 'count': 0, 'reason': 'cosine_duplicate'}
                except Exception as e:
                    logger.warning(f"Dedup search error: {e}")

            ts = datetime.now(timezone.utc).isoformat()
            insert_memory(
                conn, str(uuid7()), content, ts,
                owner_id=_srv_cfg['owner_id'],
                embedding=vec,
                local_id=local,
                agent_name=agent,
                session_id=sess,
                turn_id=tid,
            )
            return {'status': 'ok', 'count': 1}
        finally:
            conn.close()

    @app.post('/search')
    async def ep_search(req: SearchRequest):
        from .core.processor import hybrid_search
        sess = req.session_id or _srv_session
        result = hybrid_search(DB_PATH, req.query or '', _srv_cfg, sess)
        return result

    @app.post('/repair')
    async def ep_repair():
        from .core.database import (
            get_connection, find_unindexed_memories, serialize_vector,
            strip_fts_punctuation,
        )
        from .core.processor import embed_passage

        conn = get_connection(DB_PATH)
        repaired = 0
        try:
            marker = os.path.join(MEMORY_DIR, 'fts_punct_cleaned')
            if not os.path.exists(marker):
                offset = 0
                while True:
                    rows = conn.execute(
                        "SELECT rowid, content FROM memories_fts LIMIT 200 OFFSET ?",
                        (offset,)
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        clean = strip_fts_punctuation(row[1])
                        if clean != row[1]:
                            conn.execute(
                                "UPDATE memories_fts SET content = ? WHERE rowid = ?",
                                (clean, row[0])
                            )
                            repaired += 1
                    conn.commit()
                    offset += 200
                    if len(rows) < 200:
                        break
                with open(marker, 'w') as f:
                    f.write('done')

            # Vector model marker: records which embedding model the on-disk
            # vectors were generated by. If the user changes `embed_model` in
            # config.json without running a re-embed, the marker still points
            # to the old model — surface a warning so the user can decide
            # whether to delete the marker and re-run `--repair` to force a
            # full re-embed of every record. This is a manual upgrade path;
            # automatic re-embedding is intentionally not triggered here
            # because re-embedding a large DB can take many minutes.
            current_model = (_srv_cfg or {}).get('embed_model', 'intfloat/multilingual-e5-base')
            vec_marker = os.path.join(MEMORY_DIR, 'vec_model.txt')
            if not os.path.exists(vec_marker):
                with open(vec_marker, 'w', encoding='utf-8') as f:
                    f.write(current_model)
            else:
                with open(vec_marker, 'r', encoding='utf-8') as f:
                    recorded = f.read().strip()
                if recorded and recorded != current_model:
                    print(
                        f"Warning: embed_model in config.json ({current_model}) "
                        f"differs from the model used to build the vector index ({recorded}). "
                        f"Search quality will degrade until vectors are re-embedded. "
                        f"To rebuild: delete '{vec_marker}', then run `n3mc --repair`.",
                        file=sys.stderr,
                    )

            offset = 0
            while True:
                rows = find_unindexed_memories(conn, limit=200, offset=offset)
                if not rows:
                    break
                for row in rows:
                    rowid      = row['rowid']
                    content    = row['content']
                    has_vec    = row['vec_rowid'] is not None
                    has_fts    = row['fts_rowid'] is not None

                    if not has_vec:
                        try:
                            vec = embed_passage(content)
                            conn.execute(
                                "INSERT OR IGNORE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                (rowid, serialize_vector(vec))
                            )
                        except Exception as e:
                            logger.warning(f"Repair embed rowid={rowid}: {e}")

                    if not has_fts:
                        stripped = strip_fts_punctuation(content)
                        conn.execute(
                            "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                            (rowid, stripped)
                        )
                    repaired += 1

                conn.commit()
                offset += 200
                if len(rows) < 200:
                    break

        finally:
            conn.close()

        if repaired > 0:
            print(f"Repaired {repaired} record(s).", file=sys.stderr)
        return {'status': 'ok', 'count': repaired}

    @app.get('/list')
    async def ep_list():
        from .core.database import get_connection, get_all_memories
        conn = get_connection(DB_PATH)
        try:
            rows = get_all_memories(conn)
            records = [
                {
                    'id':         r['id'],
                    'content':    r['content'],
                    'timestamp':  r['timestamp'],
                    'agent_name': r['agent_name'],
                }
                for r in rows
            ]
            return {'records': records, 'total': len(records)}
        finally:
            conn.close()

except ImportError:
    app = None  # type: ignore


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _read_turn_id() -> Optional[str]:
    if os.path.exists(TURN_ID_FILE):
        try:
            tid = open(TURN_ID_FILE, 'r').read().strip()
            return tid or None
        except Exception:
            pass
    return None


def _write_turn_id(tid: str) -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(TURN_ID_FILE, 'w') as f:
        f.write(tid)


def _clear_turn_id() -> None:
    if os.path.exists(TURN_ID_FILE):
        open(TURN_ID_FILE, 'w').close()


def _extract_text(prompt) -> str:
    if isinstance(prompt, str):
        try:
            parts = json.loads(prompt)
        except Exception:
            return prompt
        if isinstance(parts, list):
            return ' '.join(
                p.get('text', '') for p in parts
                if isinstance(p, dict) and p.get('type') == 'text'
            )
        return prompt
    if isinstance(prompt, list):
        return ' '.join(
            p.get('text', '') for p in prompt
            if isinstance(p, dict) and p.get('type') == 'text'
        )
    return str(prompt) if prompt else ''


def _do_buffer(text: str, cfg: dict, agent_name: Optional[str] = None,
               session_id: Optional[str] = None, turn_id: Optional[str] = None) -> None:
    payload = {
        'content': text,
        'agent_name': agent_name,
        'session_id': session_id,
        'turn_id': turn_id,
    }
    try:
        _post(cfg, '/buffer', payload)
    except Exception as e:
        print(f"Warning: HTTP buffer failed ({e}), using direct write.", file=sys.stderr)
        try:
            _buffer_direct(text, cfg, agent_name=agent_name,
                           session_id=session_id, turn_id=turn_id)
        except Exception as e2:
            print(f"⚠️ Physical save failed. Current memories may be lost. {e2}",
                  file=sys.stderr)


def _do_search_and_write(query: str, cfg: dict) -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    empty_md = "# Recalled Memory Context\n\n_No relevant memories found._\n"

    if not query or not query.strip():
        with open(CONTEXT_FILE, 'w', encoding='utf-8') as f:
            f.write(empty_md)
        print(empty_md)
        return

    try:
        result = _post(cfg, '/search', {'query': query})
    except Exception as e:
        msg = f"# Recalled Memory Context\n\n_(memory search unavailable: {e})_\n"
        with open(CONTEXT_FILE, 'w', encoding='utf-8') as f:
            f.write(msg)
        print(msg)
        return

    try:
        from .core.processor import render_memory_context
        md = render_memory_context(result, query)
    except Exception as e:
        md = f"# Recalled Memory Context\n\n_(memory rendering failed: {e})_\n"
    with open(CONTEXT_FILE, 'w', encoding='utf-8') as f:
        f.write(md)
    print(md)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_buffer(text: str, cfg: dict, agent_name: Optional[str] = None) -> None:
    ensure_server(cfg)
    _do_buffer(text, cfg, agent_name=agent_name)


def cmd_search(query: str, cfg: dict) -> None:
    if not ensure_server(cfg):
        os.makedirs(MEMORY_DIR, exist_ok=True)
        msg = "# Recalled Memory Context\n\n_(memory server failed to start)_\n"
        with open(CONTEXT_FILE, 'w', encoding='utf-8') as f:
            f.write(msg)
        print(msg)
        return
    _do_search_and_write(query, cfg)


def cmd_repair(cfg: dict) -> None:
    ensure_server(cfg)
    try:
        result = _post(cfg, '/repair', {})
        count = result.get('count', 0)
        if count > 0:
            print(f"Repaired {count} record(s).", file=sys.stderr)
    except Exception as e:
        print(f"Repair failed: {e}", file=sys.stderr)


def cmd_list(cfg: dict) -> None:
    ensure_server(cfg)
    try:
        result = _get(cfg, '/list')
        for r in result.get('records', []):
            content_80 = r['content'][:80].replace('\n', ' ').replace('\r', ' ')
            agent = r.get('agent_name') or '-'
            print(f"{r['id']}\t{r['timestamp']}\t{agent}\t{content_80}")
        print(f"Total: {result.get('total', 0)} records")
    except Exception as e:
        print(f"List failed: {e}", file=sys.stderr)


def cmd_stop(cfg: dict) -> None:
    cp = claude_paths()
    os.makedirs(cp['RULES_DIR'], exist_ok=True)
    if not os.path.exists(cp['BEHAVIOR_MD']):
        _write_behavior_md(cp['BEHAVIOR_MD'])

    # Absolute path because `~/.n3mc/.memory/` is global, not project-relative.
    import_line = f"@{CONTEXT_FILE}"
    os.makedirs(cp['CLAUDE_DIR'], exist_ok=True)

    if os.path.exists(cp['CLAUDE_MD']):
        with open(cp['CLAUDE_MD'], 'r', encoding='utf-8') as f:
            content = f.read()
        import re
        content = re.sub(
            r'<!-- N3MC_AUTO_START -->.*?<!-- N3MC_AUTO_END -->',
            '', content, flags=re.DOTALL
        ).strip() + '\n'
        if import_line not in content:
            content = content.rstrip() + '\n\n' + import_line + '\n'
        with open(cp['CLAUDE_MD'], 'w', encoding='utf-8') as f:
            f.write(content)
    else:
        with open(cp['CLAUDE_MD'], 'w', encoding='utf-8') as f:
            f.write(f"# CLAUDE.md\n\n{import_line}\n")


def _write_behavior_md(path: str) -> None:
    text = """# N3MemoryCore Behavioral Guidelines

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
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def cmd_hook_submit(cfg: dict) -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    user_text   = _extract_text(data.get('message') or data.get('prompt') or '')
    last_claude = data.get('last_assistant_message') or ''

    ensure_server(cfg)

    # Repair
    try:
        _post(cfg, '/repair', {})
    except Exception as e:
        logger.warning(f"Repair failed during hook: {e}")

    # Save Claude's previous response
    if last_claude.strip():
        from .core.processor import chunk_text, add_chunk_prefixes, purify_text
        cleaned = purify_text(last_claude)
        chunks  = chunk_text(cleaned)
        prev_tid = _read_turn_id()
        for chunk in add_chunk_prefixes(chunks, 'claude'):
            _do_buffer(chunk, cfg, agent_name='claude-code', turn_id=prev_tid)

    # Search
    q = user_text[:cfg.get('search_query_max_chars', 2000)]
    _do_search_and_write(q, cfg)

    # Save user message
    if user_text.strip():
        from .core.processor import chunk_text, add_chunk_prefixes
        new_tid = str(uuid.uuid4())
        _write_turn_id(new_tid)
        chunks = chunk_text(user_text)
        for chunk in add_chunk_prefixes(chunks, 'user'):
            _do_buffer(chunk, cfg, agent_name='claude-code', turn_id=new_tid)


def cmd_save_claude_turn(cfg: dict) -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    last_claude = data.get('last_assistant_message') or ''
    if not last_claude.strip():
        return

    from .core.processor import chunk_text, add_chunk_prefixes, purify_text
    cleaned = purify_text(last_claude)
    chunks  = chunk_text(cleaned)

    tid = _read_turn_id() or str(uuid.uuid4())
    ensure_server(cfg)
    try:
        for chunk in add_chunk_prefixes(chunks, 'claude'):
            _do_buffer(chunk, cfg, agent_name='claude-code', turn_id=tid)
    finally:
        _clear_turn_id()


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def run_server(cfg: dict) -> None:
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=cfg.get('server_port', 18520),
                log_level='error')


# ---------------------------------------------------------------------------
# n3mc init  —  register hooks in ~/.claude/settings.json
# ---------------------------------------------------------------------------

def _resolve_hook_command(script_name: str) -> str:
    """Resolve the absolute path to the entry-point script (e.g. n3mc-hook).

    Always returned with forward slashes — the bash shell Claude Code spawns
    interprets backslashes as escape sequences (\\n -> newline, \\t -> tab),
    so a Windows-style \\Users\\... path silently corrupts when bash parses it.

    Falls back to `python -m n3memorycore.n3mc_hook` form if the entry-point
    script is not on PATH (e.g. running from a non-installed checkout).
    """
    import shutil
    import pathlib
    exe = shutil.which(script_name)
    if exe:
        return pathlib.Path(exe).as_posix()
    module = {
        'n3mc-hook':      'n3memorycore.n3mc_hook',
        'n3mc-stop-hook': 'n3memorycore.n3mc_stop_hook',
    }[script_name]
    py = pathlib.Path(sys.executable).as_posix()
    return f'"{py}" -m {module}'


def cmd_init() -> None:
    """Set up ~/.n3mc/ data dir and register hooks in ~/.claude/settings.json."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    print(f"Data directory: {HOME_DIR}")

    cfg = _load_config()
    print(f"Config: {CONFIG_FILE}")
    print(f"  owner_id = {cfg['owner_id']}")
    print(f"  port     = {cfg['server_port']}")

    settings_path = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    if os.path.exists(settings_path):
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
    else:
        settings = {}

    hooks = settings.setdefault('hooks', {})
    hook_cmd = _resolve_hook_command('n3mc-hook')
    stop_cmd = _resolve_hook_command('n3mc-stop-hook')

    def _replace_hook(event: str, markers: tuple, command: str) -> None:
        # Drop existing entries that reference our scripts (any path, any form),
        # then add the freshly-resolved one. Markers must cover BOTH the
        # hyphenated entry-point form (n3mc-hook.EXE) AND the underscored
        # module form (n3memorycore.n3mc_hook) — the resolved exe contains
        # the hyphen, the python -m fallback contains the underscore.
        existing = hooks.get(event, [])
        new_entries = []
        for entry in existing:
            inner = entry.get('hooks', [])
            keep = [
                h for h in inner
                if not any(m in h.get('command', '') for m in markers)
            ]
            if keep:
                new_entry = dict(entry)
                new_entry['hooks'] = keep
                new_entries.append(new_entry)
        new_entries.append({
            'hooks': [{'type': 'command', 'command': command}],
        })
        hooks[event] = new_entries

    _replace_hook('UserPromptSubmit', ('n3mc-hook', 'n3mc_hook'),           hook_cmd)
    _replace_hook('Stop',             ('n3mc-stop-hook', 'n3mc_stop_hook'), stop_cmd)

    with open(settings_path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    print(f"Hooks registered in: {settings_path}")
    print(f"  UserPromptSubmit -> {hook_cmd}")
    print(f"  Stop             -> {stop_cmd}")
    print("Done. Restart Claude Code for hooks to take effect.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description='N3MemoryCore CLI')
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument('--init',   action='store_true',
                     help='Set up ~/.n3mc/ and register Claude Code hooks')
    grp.add_argument('--buffer', metavar='TEXT', nargs='?', const='-')
    grp.add_argument('--search', metavar='QUERY')
    grp.add_argument('--repair', action='store_true')
    grp.add_argument('--list',   action='store_true')
    grp.add_argument('--stop',   action='store_true')
    grp.add_argument('--hook-submit',      action='store_true', dest='hook_submit')
    grp.add_argument('--save-claude-turn', action='store_true', dest='save_claude_turn')
    grp.add_argument('--run-server',       action='store_true', dest='run_server')
    parser.add_argument('--agent-id', dest='agent_id', default=None)
    args = parser.parse_args()

    if args.init:
        cmd_init()
        return

    cfg = _load_config()

    if args.run_server:
        run_server(cfg)
    elif args.buffer is not None:
        text = sys.stdin.read() if args.buffer == '-' else args.buffer
        if text.strip():
            cmd_buffer(text, cfg, agent_name=args.agent_id)
    elif args.search is not None:
        cmd_search(args.search, cfg)
    elif args.repair:
        cmd_repair(cfg)
    elif args.list:
        cmd_list(cfg)
    elif args.stop:
        cmd_stop(cfg)
    elif args.hook_submit:
        cmd_hook_submit(cfg)
    elif args.save_claude_turn:
        cmd_save_claude_turn(cfg)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
