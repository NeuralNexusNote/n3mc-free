# N3MemoryCore (N3MC) v1.3.0 [Immutable Memory]
> A NeuralNexusNote™ product

> **What is "Immutable Memory"?** Every save is physically committed to disk the instant it occurs — no buffering, no async writes. Even a forced kill of the process immediately after saving leaves the data intact. This is the core design principle of N3MC.
>
> **Who is this for?** Claude Code users who want persistent, searchable memory that survives across sessions — without manually maintaining CLAUDE.md.
>
> **Tested with**: Claude Pro (claude.ai/code) on Windows 11.

## ⚠️ Disclaimer & Distribution Terms

This software and specification are provided **"AS-IS"** without warranty of any kind.

- **No Support**: The author provides no bug fixes, answers to questions, or guarantees of operation.
- **No Warranty / No Liability**: The author shall not be liable for any damages arising from use of this software, including but not limited to data loss, business interruption, or third-party claims.
- **Use at Your Own Risk**: You assume full responsibility for your use of this software.
- **Right to Change**: The author may modify or discontinue this software at any time without notice.

By using this software, you agree to the terms above.

- **License**: Apache License 2.0. See the LICENSE file for details.

> **Removal (Uninstall)**: To remove N3MemoryCore, do not delete the folder directly. Instead, ask Claude Code: "Please delete N3MemoryCore." This ensures the hook configuration is also properly removed.
> **Backup before removal**: To carry over your memories, save the following two files before deletion: `n3memory.db` (memory data) and `config.json` (contains the `owner_id` and `local_id` UUIDv4 keys). These must be kept together — without the matching keys, owner verification and environment classification will not function correctly.

> **For implementation questions**: While the author cannot be contacted for support, you can load this specification into Claude Code and ask questions directly — Claude Code can assist with implementation and customization.
> **For customization**: When customizing N3MemoryCore, tell Claude Code where this specification file is and say "Please update the specification too." This specification itself was created that way.

---

## Setup

### Prerequisites

| Item | Requirement |
| :--- | :--- |
| Python | 3.10 or higher |
| pip packages | `fastapi` `uvicorn` `sqlite-vec` `sentence-transformers` `uuid7` |
| Claude Code | Any recent version (hook support required — configured automatically, no prior knowledge needed) |

### Quick Start

There are two supported setup paths:

**Path A — Quick Start with `pip`** (use the reference build directly)

```bash
git clone https://github.com/NeuralNexusNote/n3mc-free.git
cd n3mc-free
pip install -e .          # editable install; or  pip install .
n3mc --init               # creates ~/.n3mc/  and registers Claude Code hooks
```

Then **restart Claude Code**. Memory kicks in from the next session.

**Path B — AI-Native Setup** (regenerate from this specification)

1. Load this specification into Claude Code and ask: "Please implement N3MemoryCore according to this specification."
   - Code implementation, hook registration, and configuration file updates are all done automatically by Claude Code. No manual steps required.
   - **💡 Sonnet is recommended for code generation.** It offers a good balance of speed, cost, and implementation quality.

> **📖 After implementation (Path B only)**: Be sure to review **"Appendix: Recommended Development Workflow"** at the end of this document. It contains debugging and quality review steps (operated by you, the human).

### Data Backup

All memory data is stored under `~/.n3mc/`. To fully preserve your memories, back up both `~/.n3mc/.memory/n3memory.db` and `~/.n3mc/config.json` (which contains the `owner_id` and `local_id` UUIDv4 keys). The DB alone is sufficient for data recovery, but without the matching `config.json`, owner verification and environment classification will not function correctly. Override the data location with `N3MC_HOME=/path/to/dir` if you need it elsewhere.

### Upgrading to Pro

Load the Pro specification (`N3MemoryCore_v1.2.0_Pro_*_Complete.md`) into Claude Code and ask: "Please implement N3MemoryCore according to this specification." Your existing DB will be carried over as-is.

---

## 1. Vision
Implement long-term memory for Claude Code as a "lifelong asset."
Exclude source code and accumulate high-density "intent, conclusions, and evaluations" of information. Build a RAG foundation with hybrid search (vector + FTS5) and mathematical correctness, treating data Durability as the top priority. All conversations are automatically saved by hooks. Claude does not need to judge what to save. (The auto-save hooks apply minimal noise filters to exclude trivial responses; see §5 for details.)

> **Do NOT output manual command hints** (e.g., `n3mc --buffer "fact to save"`, `n3mc --search "keyword"`, `n3mc --list`) at any point during or after implementation.

> **⚠️ Python check**: Before starting implementation, run `python --version` or `python3 --version` to verify Python 3.10+ is available. If not found, display: "Python 3.10 or higher is required. Install from https://www.python.org/" and **abort setup**.

> **📦 Auto-install**: Run the following `pip install` automatically as part of setup. Do not ask the user to run it manually.

```bash
pip install fastapi uvicorn sqlite-vec sentence-transformers uuid7
```

> **⚠️ First-run download**: `sentence-transformers` downloads the `multilingual-e5-base` model (~470 MB) on first server start. This can take **2–10 minutes** — the server will appear unresponsive during this time, which is expected. Subsequent startups complete in seconds once cached.

> **Important: Character Limits (Design Constraints for Implementation)**
> - Hook auto-save: **Complete preservation guaranteed**. Long content is chunked via `core.processor.chunk_text(max_chars=400, overlap=40)` (paragraph → sentence → hard window) and saved as multiple records (`[claude 1/N]`..`[claude N/N]` / `[user 1/N]`..`[user N/N]`). No truncation.
> - Search query: **2,000 characters** (configurable via `search_query_max_chars` in config.json)
> - Vector search: Only the first **~2,000 characters** of any record are semantically searchable (embedding model limit: 512 tokens). Text beyond this is stored in the DB and searchable via FTS keyword search, but invisible to vector similarity search.
> - FTS query: Limited to **30 terms** to prevent slow scans
> - For best results, save records as **small chunks (~50–200 characters each, one fact per record)**.
> - **Large text handling**: When a user pastes a long text (spec, article, log, etc.), Claude must NOT save it as-is. Instead: read and understand the full content, extract each key fact as a separate short sentence (~50–200 chars), and save each with its own `--buffer` call. Although auto-save preserves the full raw text via chunking, manually extracted key facts produce higher-quality, more searchable records.

## 2. Directory Structure (Strictly Enforced)

Code lives in the repository (under `n3memorycore/`). Personal data lives outside the repository under `~/.n3mc/` so that the same package can be `pip install`-ed by anyone without colliding with anyone else's memories.

### Repository layout (what `git clone` gives you)
```
n3mc-free/                       # Repository root
├── pyproject.toml               # ★ pip metadata + entry-point declarations
├── README.md / README_JP.md
├── LICENSE / NOTICE / CHANGELOG.md / PHILOSOPHY.md
├── N3MemoryCore_v1.2.0_Free_EN.md / _JP.md   # this specification
├── n3memorycore/                # ★ The Python package (PEP 8 lowercase)
│   ├── __init__.py              # exposes __version__
│   ├── paths.py                 # resolves ~/.n3mc/ (or $N3MC_HOME) at import time
│   ├── n3memory.py              # main CLI module (--init / --buffer / --search / --list / --stop / --repair / --hook-submit / --save-claude-turn / --run-server) + FastAPI app
│   ├── n3mc_hook.py             # UserPromptSubmit hook entry: writes audit.log → calls --hook-submit
│   ├── n3mc_stop_hook.py        # Stop hook entry: writes audit.log → calls --save-claude-turn → calls --stop
│   └── core/
│       ├── __init__.py
│       ├── database.py          # DB layer: schema definitions, CRUD, PRAGMA settings, migrations
│       └── processor.py         # Processing layer: embedding generation, ranking calculation, purify (fenced-code substitution per documented design)
├── tests/                       # pytest suite (see §7)
│   ├── conftest.py
│   ├── test_api.py
│   ├── test_database.py
│   ├── test_hooks.py
│   └── test_processor.py
└── .claude/                     # Project-scoped Claude Code config (tracked)
    ├── settings.json            # Permission allowlist (Hooks live in user-global ~/.claude/settings.json — see §4)
    ├── CLAUDE.md                # Per-project guidance (the @import for memory_context.md is added by --stop)
    └── rules/
        └── n3mc-behavior.md     # AI behavioral guidelines (auto-generated by --stop)
```

### User-data layout (created by `n3mc --init`, NOT in the repository)
```
~/.n3mc/                            # Override with $N3MC_HOME if needed
├── config.json                     # Persistent config: owner_id, local_id, server_port, dedup_threshold, half_life_days, bm25_min_threshold, search_result_limit, context_char_limit, min_score, search_query_max_chars
└── .memory/
    ├── n3memory.db                 # SQLite DB (vec0 + FTS5)
    ├── n3mc.pid                    # FastAPI server PID file (exists only while the server is running)
    ├── audit.log                   # JSONL hook audit trail (append-only, written before any other action)
    ├── memory_context.md           # --search output (also @imported into .claude/CLAUDE.md)
    ├── turn_id.txt                 # Q-A pairing handoff between UserPromptSubmit and Stop hooks
    ├── fts_punct_cleaned           # one-time FTS5 punctuation-clean migration marker (touched by --repair)
    └── vec_e5v2_migrated           # one-time vector-reindex migration marker (touched by --repair)
```

### Console scripts (installed by `pip install`)
| Script | Maps to | Purpose |
|---|---|---|
| `n3mc` | `n3memorycore.n3memory:main` | Primary CLI for humans and Claude Code |
| `n3mc-hook` | `n3memorycore.n3mc_hook:main` | UserPromptSubmit hook (registered in `~/.claude/settings.json` by `n3mc --init`) |
| `n3mc-stop-hook` | `n3memorycore.n3mc_stop_hook:main` | Stop hook (registered in `~/.claude/settings.json` by `n3mc --init`) |

## 3. Technical Specifications (No Modifications Allowed)

> **⚠️ AI must not auto-modify**: AI must not autonomously change any of the following specifications for speed improvement or optimization. Changes to the embedding model, vector dimensions, or synchronous write settings are only permitted by a human manually editing `config.json`.

### ID Hierarchy

N3MemoryCore uses 5 ID fields to identify the origin and context of each record:

| ID | Stored in | Generated | Granularity | Purpose |
|---|---|---|---|---|
| `id` (PK) | DB record | Per record (UUIDv7, time-ordered) | **One record** | Unique identifier for each memory — used for deletion and dedup |
| `owner_id` | `config.json` | First startup (UUIDv4) | **Owner / N3MC server** | Identifies the N3MC FastAPI instance / data owner — used for shared/multi-user scenarios and import provenance |
| `session_id` | In-memory or supplied by host | Per task / project / conversation (UUIDv4) | **Task / project / conversation** | Groups memories that belong to one task, project, or conversation thread so they surface together. Hosts that expose a session identifier (e.g. n3memory-lite assigns task numbers; an Ollama-style chat picker, etc.) pass it in via the API. Clients that cannot read a host session id (Claude Code) generate a fresh UUIDv4 at server startup as a per-process fallback. Drives the `b_session` ranking bias in Free and Pro |
| `local_id` (agent_id) | `config.json` / API | First startup (UUIDv4), or per request | **Agent / install** | UUIDv4 identifier for the speaking agent (one Claude Code install = one `local_id`; different agents on the same DB get different UUIDs). Stored on every record. **In Free, `b_local` is unused (always 1.0)** — the per-agent ranking multiplier is a Pro feature; see "Search ranking bias" below |
| `agent_name` | DB record | Per buffer call (free-form string) | **Agent display name** | Human-readable label for the agent (e.g. `"claude-code"`) |

**Hierarchy relationship:**

```
owner_id  (one N3MC server / data owner)
  └── session_id  (one task / project / conversation)
        └── local_id  (the speaking agent within that session)
              ├── agent_name  (its display name: "claude-code", etc.)
              └── id  (one memory record)
```

**Search ranking bias (Free edition):**
- `session_id` match → `b_session = 1.0` (mismatch or NULL: `0.6`) — **groups conversation by task / project**, so memories from the ongoing exchange rank above unrelated past sessions. This is Free's primary ranking signal beyond similarity and freshness.
- `local_id` — stored but **not used in Free edition ranking**. Local bias (`b_local`) is a Pro feature; in Free, `b_local` is always 1.0 regardless of match/mismatch. The Pro edition applies `b_local = 1.0` on match and `b_local = 0.8` on mismatch to additionally prioritize the agent's own memories over those from other agents on the same DB.

### Embeddings
- **Default model**: `intfloat/multilingual-e5-base` / Vector: float[768]
- The default is multilingual on purpose — it indexes Japanese, English, Chinese, Korean, and ~100 other languages out of the box. Users who need higher precision in a single language can override the model (see "Switching to a language-specialised model" below); the default release does NOT make that decision for them.
- Always specify `normalize_embeddings=True` at retrieval time to guarantee L2-normalized vectors (norm=1).
- **Input Prefixes (Required)**: Without prefixes, this model's accuracy degrades significantly. The following must be strictly observed:

```python
# At save time (registering as a document)
text_to_embed = "passage: " + content

# At search time (matching as a query)
text_to_embed = "query: " + keyword
```

#### Switching to a language-specialised model (user-side customisation)

The embedding model is resolved at server startup from (priority order):
1. `embed_model` field in `~/.n3mc/config.json`
2. `$N3MC_EMBED_MODEL` environment variable
3. Built-in default (`intfloat/multilingual-e5-base`)

Examples a user might configure:

| Goal | `embed_model` value | Vector dim |
|---|---|---|
| English-only, slightly higher precision | `intfloat/e5-base-v2` | 768 |
| Multilingual, higher precision (slower, larger) | `intfloat/multilingual-e5-large` | 1024 |
| Japanese-specialised | `cl-nagoya/sup-simcse-ja-base` | 768 |
| English, smaller / faster | `intfloat/e5-small-v2` | 384 |

**⚠️ Vector dimension constraint**: `memories_vec` is created with a fixed `float[768]` dimension. Switching to a model with a different vector size requires dropping and recreating the vec table (and re-embedding every record). The reference implementation only supports same-dimension switches without manual intervention.

**Upgrade procedure (same-dimension model swap)**:
1. Edit `~/.n3mc/config.json` → set `embed_model` to the new model name.
2. Restart the server (kill the PID file's process, or just let the next CLI call relaunch it).
3. The next `n3mc --repair` call detects the model mismatch (via the `vec_model.txt` marker in `~/.n3mc/.memory/`) and prints a warning. The on-disk vectors are still from the OLD model, so search quality will be degraded until they are regenerated.
4. To force a full re-embed: delete `~/.n3mc/.memory/vec_model.txt` and the `memories_vec` rows for affected records (or drop and rebuild the entire vec table), then run `n3mc --repair` to rebuild vectors using the new model.

**⚠️ Different-dimension model swap**: NOT supported by the reference implementation without manual SQL. Drop the `memories_vec` table, recreate it with the new dimension, delete `vec_model.txt`, then run `n3mc --repair`.

### Inter-module Imports
Use proper package-relative imports (no `sys.path` hacks). Inside the package:

```python
# n3memorycore/core/processor.py
from .database import (
    get_connection,
    init_db,
    insert_memory,
    search_vector,
    search_fts,
    get_all_memories,
    delete_memory,
    count_memories,
    check_exact_duplicate,
    find_unindexed_memories,
    serialize_vector,        # Required for /repair endpoint
    get_memories_by_turn_id, # Required for Q-A pairing
    strip_fts_punctuation,   # Required for /repair FTS migration
)
```

```python
# n3memorycore/n3memory.py
from .paths import HOME_DIR, MEMORY_DIR, DB_PATH, PID_FILE, CONTEXT_FILE, AUDIT_LOG, CONFIG_FILE, claude_paths
from .core.database import (
    get_connection, init_db, migrate_schema, insert_memory,
    check_exact_duplicate, search_vector, find_unindexed_memories,
    strip_fts_punctuation, serialize_vector,
)
from .core.processor import (
    embed_passage, cosine_sim_from_l2, hybrid_search,
    chunk_text, add_chunk_prefixes, purify_text, render_memory_context,
)
```

`paths.py` resolves `HOME_DIR` from `$N3MC_HOME` if set, otherwise `~/.n3mc/`. All `MEMORY_DIR`, `DB_PATH`, `PID_FILE`, etc. derive from it. Per-project `.claude/` paths come from `claude_paths()` which uses the current working directory.

### Resident FastAPI Server
- **Port**: Default `18520` (configurable via `server_port` in `config.json`)
- **Start Timing**: On execution of any `n3mc` subcommand (or `python -m n3memorycore.n3memory`), check `~/.n3mc/.memory/n3mc.pid`; if the process does not exist, automatically start it in the background as `python -m n3memorycore.n3memory --run-server`. Write the PID file using an atomic operation (e.g., `open(..., 'x')` exclusive-create flag) to prevent duplicate launches when multiple processes check simultaneously.
- **Communication**: HTTP over TCP (`http://127.0.0.1:{port}`)
- **Health Monitoring**: On every CLI execution, PING the `/health` endpoint; if unresponsive, delete the old PID file and restart.
- **Response Target**: Around 0.7s (total of embedding generation + DB search). Treat this as a target, not a strict requirement, as it depends on hardware, OS, and model cache state.
- **First Start**: Because model preloading runs before uvicorn starts, the initial launch (or when model cache is absent) may take up to 60 seconds. This is an accepted behavior by design; subsequent starts complete within a few seconds.

### SQLite Durability Settings
Force the following on every connection:
```sql
PRAGMA synchronous = FULL;
PRAGMA journal_mode = WAL;
```

### Immediate Physical Writes (No Modifications or Optimizations Allowed)
On `--buffer` or API-based saves, complete INSERT and COMMIT at that instant.

**The following are absolutely prohibited (even for performance improvement):**
- Write buffering (`write_buffer`, `batch_insert`, deferred COMMIT, etc.)
- Asynchronous writes (`asyncio`, thread queues, background tasks, etc.)
- Transaction batching (combining multiple INSERTs into one transaction)

**Reason**: Data is lost if the process is forcibly terminated immediately after saving. Durability takes priority over speed. This is not a performance choice but an immutable design constraint.

### Identifiers

- **Owner ID**: Generate a UUIDv4 on first launch and save it to `config.json`. Stamp it on every record. Used to preserve data provenance when merging N3MC instances.
- **Local ID**: Generate a UUIDv4 on first launch (if absent from `config.json`) and save it persistently. Identifier for the N3MC installation (or agent). Stamp it on every record. For multi-agent scenarios, the `/buffer` API accepts a `local_id` parameter to specify an agent-specific UUIDv4 (falls back to config.json value when omitted).
  - **Use cases**: Multiple Claude Code installations (each machine or install gets its own `local_id`); different agents sharing the same N3MC DB (e.g., Claude Code + CordX each run as separate instances with distinct `local_id`s); separating memories by agent type or project environment.
  - **Note**: In Free, `local_id` is stored per-record but not used in ranking. The `B_local` bias multiplier that prioritizes same-environment memories is a Pro feature.
- **Session ID**: A grouping key for one task / project / conversation. **Resolution order**: (1) explicit `session_id` argument on the `/buffer` and `/search` API calls (highest priority — used by hosts that already track sessions, e.g. n3memory-lite assigns task numbers, an Ollama-style chat picker can pass the chat id); (2) `N3MC_SESSION_ID` environment variable; (3) per-process UUIDv4 generated at server startup (fallback for clients like Claude Code that cannot read a host session id). Stored on every record but **not persisted to `config.json`** — `session_id` is meant to identify a transient task / project, not a persistent installation. Drives the `b_session` ranking bias (match=1.0 / mismatch=0.6) so memories from the current task surface above unrelated past sessions.

Complete `config.json` schema (auto-initialize any missing fields with the default values below):

```json
{
  "owner_id":             "<UUIDv4 auto-generated>",
  "local_id":             "<UUIDv4 auto-generated>",
  "server_port":          18520,
  "dedup_threshold":      0.95,
  "half_life_days":       90,
  "bm25_min_threshold":   0.1,
  "search_result_limit":  20,
  "context_char_limit":   3000,
  "min_score":            0.2,
  "search_query_max_chars": 2000
}
```

- `search_result_limit`: Maximum number of results returned by `--search`.
- `context_char_limit`: (Deprecated) Previously used by `--stop` to truncate `memory_context.md` content. No longer used with the @import approach. Retained for backwards compatibility.
- `min_score`: Excludes results with a score below this value from `--search` output (default `0.2`). Set to `0.0` to disable. Becomes more effective as the DB grows.
- `search_query_max_chars`: Maximum characters used from a search query (default `2000`). The embedding model (512 tokens) cannot process more than ~2000 chars meaningfully. Increase at the cost of slower FTS queries.
- Internal processing parameters such as KNN fetch count (K value) and `--repair` batch size are left to the implementer's discretion.

> **Multi-account usage on a single PC**: When running multiple Claude Code accounts on one machine and connecting them to the same N3MC server (same `server_port` and DB path), each account's `config.json` is auto-generated independently, resulting in different `owner_id` / `local_id` values. In the Free edition this does not affect ranking, but after upgrading to Pro, memories from other accounts will have `B_local=0.6` decay applied. To treat all accounts' memories equally, copy the first account's `config.json` to the other accounts so that `owner_id` / `local_id` are unified.

> **Team sharing (advanced)**: N3MC is designed for fully local operation, but by deploying the FastAPI server on a network machine and changing `server_host` in `config.json` from `127.0.0.1` to that server's IP address, a team can share a single memory DB. Note that authentication, encryption, and concurrent write locking are not included in the current specification — consult Claude Code when deploying. With the Pro upgrade, `B_local` bias naturally prioritizes personal memories.

> **Database migration (advanced)**: The current specification assumes SQLite + sqlite-vec, but for larger teams, migration to PostgreSQL + pgvector is technically feasible. Consult Claude Code for schema compatibility, connection pooling, and migration procedures.

- **Primary Key**: UUIDv7 (time-sortable; generated at DB insert time). Use the external library `uuid7` (PyPI).

### Ranking Formula

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_session
```

**cos_sim (Mathematical Correctness)**:

$$cos\_sim = \max(0,\ 1.0 - \frac{L2\_distance^2}{2})$$

- When vectors are L2-normalized, `L2_distance² = 2(1 - cosθ)` holds, making the above formula equivalent to `cosθ`.
- Negative values (opposite-direction vectors) break ranking, so clamp with `max(0, ...)`.
- **Prerequisite**: This formula is invalid if `normalize_embeddings=True` has not been applied.

**keyword_relevance (Normalizing FTS5 BM25)**:

SQLite FTS5's `bm25()` returns negative values (more negative = more relevant). Normalize to `[0.0, 1.0]` as follows:

1. If the absolute value of the raw score is less than `bm25_min_threshold` (value from `config.json`, default `0.1`) (nearly irrelevant), set `keyword_relevance = 0.0`.
2. Otherwise, normalize by the maximum absolute value in the result set:

$$keyword\_relevance = \frac{-bm25\_score}{\max(1.0,\ \max_{results}(-bm25\_score))}$$

If the search returns 0 results, set `keyword_relevance = 0.0`.

**FTS5 Table Definition**: Always create the FTS5 virtual table with `tokenize='porter unicode61'`. **Use a standalone (non-`content=`) FTS5 table** — do NOT use the external-content variant (`content='memories', content_rowid='rowid'`). External-content FTS5 does not support the natural `DELETE FROM memories_fts WHERE rowid = ?` pattern required by `delete_memory` and the `--repair` migration loops; it requires a `INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', ?, ?)` ritual instead, and getting that wrong corrupts the FTS index ("database disk image is malformed"). The standalone form keeps a private FTS shadow whose rowid we align with `memories.rowid` ourselves: after `INSERT INTO memories(...)`, retrieve `cursor.lastrowid` and pass it to `INSERT INTO memories_fts(rowid, content) VALUES (?, ?)`.

```sql
-- memories table (UUIDv7 as primary key; rowid is implicitly managed by SQLite)
CREATE TABLE memories (
    id        TEXT PRIMARY KEY,  -- UUIDv7
    content   TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    owner_id  TEXT NOT NULL,
    local_id  TEXT,             -- N3MC install identifier (from config.json; can be overridden via API for multi-agent scenarios)
    agent_name  TEXT              -- Identifies the AI agent that wrote the record (e.g. "claude-code"). NULL for records written before v1.1 or without agent tagging
    -- SQLite automatically assigns an implicit INTEGER rowid to every table
);

-- FTS5 standalone — rowid alignment with memories.rowid is maintained manually
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    tokenize='porter unicode61'
);

-- sqlite-vec KNN vector search (linked to memories table via rowid)
CREATE VIRTUAL TABLE memories_vec USING vec0(
    embedding float[768]
);
-- On INSERT: INSERT INTO memories_vec(rowid, embedding) VALUES (memories.rowid, serialize_vector(vec))
-- On DELETE: DELETE FROM memories_fts WHERE rowid = <rowid>; DELETE FROM memories_vec WHERE rowid = <rowid>;
-- ⚠️ When deleting from memories, always delete from memories_fts and memories_vec in the same transaction.
--    Orphaned records in vec/fts will corrupt search results.
```

**Schema Migration (`migrate_schema`)**: At startup, check `PRAGMA table_info(memories)` and add any missing columns idempotently:

```sql
ALTER TABLE memories ADD COLUMN local_id  TEXT;
ALTER TABLE memories ADD COLUMN agent_name  TEXT;
```

Additionally, `migrate_schema()` detects if the existing `memories_fts` table was created with `tokenize='trigram'` and automatically drops and recreates it with `tokenize='porter unicode61'`, re-indexing all records. This one-time migration ensures existing databases benefit from improved English search precision.

`agent_name TEXT` — identifies the AI agent that wrote the record (e.g. `"claude-code"`). `NULL` for records written before v1.1 or without agent tagging.

**Rationale for FTS5 Tokenizer Selection**: `porter unicode61` is adopted for English-optimized stemming. Porter stemming normalizes word forms (e.g., "running" → "run", "memories" → "memori"), giving significantly better BM25 precision for English text compared to `trigram` substring matching. Word-boundary tokenization (`unicode61`) is more meaningful than character-level trigrams for English.

**FTS5 Constraint**: The FTS5 `porter unicode61` tokenizer splits on word boundaries and cannot meaningfully process single-character queries. For queries shorter than 2 characters, skip keyword search and rank using vector search (cos_sim) only. In English, single characters and very short tokens such as `"I"` fall into this category.

**FTS Punctuation Stripping (mandatory on both INSERT and MATCH)**: Punctuation adjacent to words can cause tokenization mismatches. Apply the **same punctuation stripping function** both when **INSERTing into FTS5** and when **building the MATCH query**:

```python
_FTS_PUNCT_RE = re.compile(r'[()[\]{}<>,.!?;:\-—–…\'\"\`~@#$%^&*+=|/\\]')

def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()

# On INSERT: store original text in memories table, stripped text in FTS
INSERT INTO memories_fts(rowid, content) VALUES (?, strip_fts_punctuation(content))

# On MATCH: apply the same function to the query, then quote each term
# to prevent AND/OR/NOT/NEAR operator interpretation by FTS5
_FTS_MAX_TERMS = 30  # Prevent FTS5 freeze on large text input

def _quote_fts_query(text: str) -> str:
    stripped = strip_fts_punctuation(text)
    terms = stripped.split()[:_FTS_MAX_TERMS]
    return ' '.join(f'"{t}"' for t in terms)

fts_query = _quote_fts_query(query)
```

During `--repair`, detect existing FTS records that were indexed with punctuation and re-register them with stripped text. This FTS cleaning runs as a **one-time migration**: after completion, create a marker file (`.memory/fts_punct_cleaned`). If the marker exists, skip the scan to avoid a full-table scan on every session start.

**Score for FTS-only Hits**: Records that appear in FTS results but not in vector search results should be included in results with `cos_sim = 0.0` for integrated score calculation. The score ceiling is `0.3 × time_decay × b_session`, but they must not be excluded from ranking.

**Vector Search owner_id Filter**: Since `search_vector` is a single-user system, no filtering by `owner_id` is performed. KNN search targets all records. (This is a deliberate design decision; multi-user support would require rework.)

**time_decay (half-life of `half_life_days` days)**:

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

`days_elapsed` is the elapsed days (floating point) since the record's creation timestamp. Use the `half_life_days` value from `config.json` (default 90).

**Bias coefficients (Free edition)**:

| Bias | Condition | Coefficient (fixed value) |
| :--- | :--- | :---: |
| **$b_{session}$** | Matches current `session_id` | **1.0** |
| | Mismatch or NULL | **0.6** |

- `b_session` is the **only** bias multiplier in Free. It groups memories that belong to the same task / project / conversation, so the ongoing exchange surfaces above unrelated past sessions. The stronger `0.6` mismatch penalty makes the current session the primary ranking signal beyond raw similarity and freshness.
- Bias coefficients are fixed values and cannot be changed via the configuration file.
- Compute using a SQL `CASE` expression to minimize post-processing on the Python side:

```sql
-- Skeleton for score calculation
CASE WHEN session_id = :current_session THEN 1.0 ELSE 0.6 END AS b_session
```

- `b_local` (agent / installation grouping) is **not** applied in Free — every record is treated as `b_local = 1.0` regardless of match. The `B_local` multiplier that separates memories per agent / install is a **Pro feature**; in Pro it applies `b_local = 1.0` on match and `b_local = 0.8` on mismatch.

### Clean CLI
Completely silence model load warnings and similar output. When launching the FastAPI server as a subprocess from `n3memory.py`, redirect `stderr` as follows (handled internally, not by the caller):

```python
import subprocess, sys
subprocess.Popen(
    [sys.executable, '-m', 'n3memorycore.n3memory', '--run-server'],
    stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
)
```

- The `-m n3memorycore.n3memory` form is required so the package's relative imports resolve. Do **not** spawn the server by absolute file path.
- `subprocess.DEVNULL` is a standard constant since Python 3.3. No OS-dependent path needed and no file handle leaks.
- The caller (e.g., hooks in `settings.json`) does not need to add any redirects.

**Character Encoding (UTF-8)**: At the start of `main()` in `n3memorycore/n3memory.py`, **AND at module top of `n3memorycore/n3mc_hook.py` and `n3memorycore/n3mc_stop_hook.py`** (before any `sys.stdin.read()` or `subprocess.run` invocation), execute the following to resolve character corruption in Windows cp932 environments from within the program itself:

```python
for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass
```

- With this, the caller does not need to specify `python -X utf8`.
- `reconfigure` is available from Python 3.7 onward. For older environments, use the `PYTHONUTF8=1` environment variable as an alternative.
- All multilingual text (Japanese, English, Chinese, etc.) is unified as UTF-8.
- ⚠️ **The reconfigure block is required in EVERY Python entry point that reads stdin or pipes stdin into a subprocess** — that includes `n3memorycore/n3memory.py`, `n3memorycore/n3mc_hook.py`, AND `n3memorycore/n3mc_stop_hook.py`. Forgetting it on any one of them silently mojibakes non-ASCII input on Windows (Japanese, em-dashes, etc.); the corrupted bytes are then persisted to `audit.log` and the DB and cannot be recovered. Run the block at module top in the hooks (not inside `main()`) so it precedes the first `sys.stdin.read()`.

**Path Portability**: All file path construction in Python code must use `os.path.join()` or `pathlib.Path`. Hardcoded path separator characters (`\` or `/` as string literals in path construction) are prohibited. The system must run on Windows, macOS, and Linux without code changes.

```python
# Correct
import os
db_path = os.path.join(base_dir, "data", "memory.db")

# Also correct
from pathlib import Path
db_path = Path(base_dir) / "data" / "memory.db"

# Prohibited — hardcoded separator
db_path = base_dir + "/data/memory.db"   # NG
db_path = base_dir + "\\data\\memory.db" # NG
```

- Note: Paths in `settings.json` hook commands and `.claude/` configuration files are shell strings, not Python code. Use forward slashes (`/`) in those contexts (see §4 path separator warning).
- The system is designed for cross-platform use (Windows / macOS / Linux). Regenerating from this spec on any OS must produce a working implementation without code changes.

---

## 3.5. Resilience & Error Handling

### config.json Corruption Recovery

When `config.json` is empty or corrupted, `_load_config` follows this recovery procedure:

1. If JSON parsing fails, output a warning to stderr (do NOT silently swallow the error)
2. If `owner_id` or `local_id` is missing, **recover the most frequent value from existing DB records** (`SELECT owner_id FROM memories GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 1`)
3. Only generate a new UUID if the DB also has no records

> **Design intent**: If `owner_id`/`local_id` are silently regenerated, bias calculations for all existing memories become incorrect. DB recovery prevents this silent failure.

### DB Corruption Detection and Recovery

On server startup (`run_server`) and in the `_buffer_direct` fallback path, run `PRAGMA integrity_check`:

1. If the result is not `ok`, rename the current DB to `.corrupt.bak`
2. Create a new empty DB
3. Output a warning message to stderr (including recovery instructions)

If `PRAGMA` execution inside `get_connection` raises a `DatabaseError`, re-raise with an error message that includes recovery instructions.

### Migration Batch Processing

Bulk migrations inside `/repair` (vector re-indexing, FTS punctuation cleaning) execute in **batches of 200**. To prevent OOM on large DBs, use `LIMIT/OFFSET` pagination instead of `fetchall()`.

---

## 4. Claude Code Integration Settings (Core of Automation)

Register hooks in the **user-global** Claude Code settings (`~/.claude/settings.json`) using absolute paths so they apply to all sessions. Register permissions in the project settings (`.claude/settings.json`).

> **🛑 [Mandatory automated step] Hook registration is NOT optional and NOT user-deferred**
>
> The single most common reason cross-session RAG fails to work after a "successful" install is that the AI implementing this spec generated all the Python code, started the server, ran `--buffer` / `--search` smoke tests successfully — and then **left the `~/.claude/settings.json` hook block to the user** (or skipped it because it touches a global config file). When this happens, every subsequent Claude Code session running outside the project directory has **zero hooks firing**, so no conversation gets auto-saved and no `--search` runs at the start of each turn. The user sees the smoke test pass, believes the install is complete, and discovers months later that nothing they said to Claude was ever recorded.
>
> **The implementation MUST write the hook block in §4 ① below into `~/.claude/settings.json` idempotently as part of automated setup, before reporting completion.** Path A (`pip install` + `n3mc --init`) does this for you. Path B (AI-Native regeneration) must do it programmatically — do not ask the user to do it manually, and do not skip it because it modifies a global file.
>
> Verification gate (also see §6 test 7): the implementation is **not** complete until the global `~/.claude/settings.json` contains hook entries whose `command` strings reference `n3mc-hook` (UserPromptSubmit) and `n3mc-stop-hook` (Stop). For pip installs, those resolve to the absolute path of the entry-point scripts (e.g. `.../Scripts/n3mc-hook.exe` on Windows or `.../bin/n3mc-hook` on Unix).

### ① Global Settings (`~/.claude/settings.json`) — Hook Registration

#### Path A — let `n3mc --init` do it (recommended)

After `pip install`, run:

```bash
n3mc --init
```

This writes the hook block into `~/.claude/settings.json` with the absolute path resolved by `shutil.which('n3mc-hook')` / `shutil.which('n3mc-stop-hook')`. Re-running is a no-op (idempotent — entries that already reference `n3mc_hook` / `n3mc_stop_hook` in any command string are replaced rather than duplicated).

The resulting block looks like (paths vary per machine):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/Scripts/n3mc-hook.EXE"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/Scripts/n3mc-stop-hook.EXE"
          }
        ]
      }
    ]
  }
}
```

> **⚠️ Path separators**: Claude Code uses a bash shell, so Windows backslashes (`\`) are interpreted as escape sequences (`\n`→newline, `\t`→tab). `n3mc --init` always writes forward slashes — if you patch the file by hand, do the same.

#### Path B — Idempotent Hook Installation Algorithm (for AI-Native regeneration)

When regenerating from this spec, the AI must execute logic equivalent to the following against `~/.claude/settings.json`. The merge **preserves all existing fields** (`permissions`, `autoUpdatesChannel`, anything else the user has) and only adds the two hook entries; entries that already reference our hook scripts are replaced (not duplicated). Re-running the algorithm on an already-installed environment is a no-op.

```python
# n3memorycore/n3memory.py:cmd_init — runnable equivalent of the mandatory step
import json, os, pathlib, shutil, sys

SETTINGS = pathlib.Path(os.path.expanduser("~/.claude/settings.json"))
settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
hooks = settings.setdefault("hooks", {})

def resolve_cmd(script_name: str) -> str:
    # Prefer the installed entry-point script (n3mc-hook / n3mc-stop-hook).
    exe = shutil.which(script_name)
    if exe:
        return exe
    # Fallback for non-installed checkouts: spawn via -m.
    module = {"n3mc-hook": "n3memorycore.n3mc_hook",
              "n3mc-stop-hook": "n3memorycore.n3mc_stop_hook"}[script_name]
    return f'"{sys.executable}" -m {module}'

def install(event: str, marker: str, command: str) -> None:
    # Drop any existing entry that references our scripts (any path),
    # then append the freshly-resolved one.
    kept = []
    for entry in hooks.get(event, []):
        inner = [h for h in entry.get("hooks", []) if marker not in h.get("command", "")]
        if inner:
            kept.append({**entry, "hooks": inner})
    kept.append({"hooks": [{"type": "command", "command": command}]})
    hooks[event] = kept

install("UserPromptSubmit", "n3mc_hook",      resolve_cmd("n3mc-hook"))
install("Stop",             "n3mc_stop_hook", resolve_cmd("n3mc-stop-hook"))

SETTINGS.parent.mkdir(parents=True, exist_ok=True)
SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
```

- **Why match by script-name marker, not full command string**: the user may have moved the install root or upgraded Python; matching on `n3mc_hook` / `n3mc_stop_hook` keeps the algorithm idempotent across path edits without creating duplicates.
- **Do NOT use `git add`-style trust** (`if "hooks" not in settings: settings["hooks"] = {...}`): an empty `hooks: {}` key still passes that check and the user ends up with no hooks. Always inspect the inner arrays.
- **Cross-platform path**: forward slashes only inside the `command` string, even on Windows. The bash shell Claude Code spawns interprets backslashes as escape sequences (`\n` → newline).

### ② Project Settings (`.claude/settings.json`) — Permission Registration

The `n3mc` console script (installed by `pip install`) is the only entry point Claude needs to invoke directly. The hook scripts (`n3mc-hook` / `n3mc-stop-hook`) are spawned by Claude Code itself, not via `Bash(...)`, so they do not need permissions.

```json
{
  "permissions": {
    "allow": [
      "Bash(n3mc --search *)",
      "Bash(n3mc --buffer *)",
      "Bash(n3mc --list*)",
      "Bash(n3mc --repair*)",
      "Bash(n3mc --stop*)"
    ]
  }
}
```

> **⚠️ Path quoting**: With the `n3mc` console script installed on `PATH`, no path quoting is needed. If you launch via `python -m n3memorycore.n3memory ...` instead (e.g. inside a venv where the entry point is not on `PATH`), use a wildcard pattern like `Bash(python *n3memorycore.n3memory* --search *)` to absorb interpreter-path variation.

### `--stop` Hook Specification (Session Termination Processing)

The responsibility of `--stop` is **session-end cleanup and ensuring the @import reference in CLAUDE.md**. Saving conversation content is fully automated by hooks. Claude AI does not need to manually call `--buffer`. The `Stop` hook (`n3memorycore/n3mc_stop_hook.py`, exposed as the `n3mc-stop-hook` entry point) auto-saves Claude's last response in addition to running `--stop` (see [Automated] in §5).

**`n3mc-stop-hook` stdin input spec**: Claude Code passes the following JSON to the Stop hook via standard input. `last_assistant_message` contains the full text of Claude's last response.

```json
{
  "session_id": "<session ID>",
  "stop_hook_active": true,
  "last_assistant_message": "Full text of Claude's last response"
}
```

Processing order on `--stop` execution:

1. Ensure `<cwd>/.claude/rules/n3mc-behavior.md` exists with the AI behavioral guidelines (Fully Automatic Saving, Active RAG, etc.). This is an **idempotent** operation — if the file already exists, do nothing.
2. Ensure `<cwd>/.claude/CLAUDE.md` contains the `@import` reference to `~/.n3mc/.memory/memory_context.md` (resolved as an **absolute path** since the data dir is outside the project tree). This is an **idempotent** operation — if the line already exists, do nothing.
   - If `<cwd>/.claude/CLAUDE.md` does not exist, create it with the `@import` line.
   - **Migration**: If a legacy `<!-- N3MC_AUTO_START -->` ... `<!-- N3MC_AUTO_END -->` zone or a relative `@../N3MemoryCore/.memory/...` path exists, remove it and add the absolute `@import` line instead.
3. Exit normally. Produce no output on success (silence).
4. Output a fatal failure warning only on DB write failure.

### CLAUDE.md Structure (@import)

`<cwd>/.claude/CLAUDE.md` references `memory_context.md` via Claude Code's `@import` mechanism. This keeps the CLAUDE.md file compact (a single line for N3MC) and avoids monopolizing a shared resource. Because the memory data dir lives at `~/.n3mc/` (outside the project), the `@import` path is **absolute** and per-machine — `--stop` writes the resolved absolute path on the first run.

```markdown
# (User-managed content)
# Write user behavioral guidelines and project settings here

@C:/Users/you/.n3mc/.memory/memory_context.md
```

(On macOS / Linux the path looks like `@/home/you/.n3mc/.memory/memory_context.md`.)

At session start, Claude Code expands the `@import` and loads the contents of `memory_context.md` into context alongside CLAUDE.md. N3MC no longer writes search results directly into CLAUDE.md.

Additionally, `--stop` ensures `.claude/rules/n3mc-behavior.md` exists, which contains the AI behavioral guidelines (Fully Automatic Saving, Active RAG). Claude Code automatically loads rules files from `.claude/rules/` at session start, so these guidelines are always active.

---

## 4.5. FastAPI Endpoint Specification

The CLI sends HTTP requests to the FastAPI server. All endpoints are issued to `http://127.0.0.1:{server_port}`.

| Method | Path | Corresponding CLI Command | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | (internal health check) | Returns `{"status": "ok"}` |
| `POST` | `/buffer` | `--buffer` | Receives `{"content": str, "agent_name": str (optional)}` and saves |
| `POST` | `/search` | `--search` | Receives `{"query": str}` and returns results |
| `POST` | `/repair` | `--repair` | Repairs unindexed records |
| `GET` | `/list` | `--list` | Returns all records (each record includes `agent_name`) |

### `--hook-submit` (UserPromptSubmit Hook Entry Point)

```bash
echo '{"message":"user input","last_assistant_message":"Claude response"}' | n3mc --hook-submit
```

Reads JSON from stdin with `message` (or `prompt`) and `last_assistant_message` fields. Performs all UserPromptSubmit operations in a single process via HTTP requests to the server: `--repair` → `--buffer` (save Claude's response) → `--search` → `--buffer` (save user message). Called by the `n3mc-hook` entry point (`n3memorycore/n3mc_hook.py`); the AI does not need to run this manually. All records saved by this hook are automatically tagged with `agent_name = "claude-code"`.

### `--save-claude-turn` (Stop Hook Helper)

```bash
echo '{"session_id":"...","stop_hook_active":true,"last_assistant_message":"Claude response text"}' | n3mc --save-claude-turn
```

Reads the same Stop-hook JSON from stdin, extracts `last_assistant_message`, applies `chunk_text(max_chars=400, overlap=40)`, and saves each chunk as a `[claude]` / `[claude i/N]` record via HTTP `/buffer` calls in a single process. The `turn_id` is read from `~/.n3mc/.memory/turn_id.txt` (set earlier by `--hook-submit` when it saved the user message); after the save loop the file is cleared. If the file is missing, a fresh UUID4 turn_id is generated. Called by the `n3mc-stop-hook` entry point (`n3memorycore/n3mc_stop_hook.py`) only; the AI does not invoke this manually.

> **Why a dedicated subcommand and not `--buffer -`?** The Stop hook itself reads stdin to extract the JSON envelope; using `--buffer -` would either double-consume stdin or require N subprocess invocations (one per chunk). `--save-claude-turn` performs all chunked saves in a single process, in order, sharing one HTTP keep-alive to the resident server, and it is the canonical "buffer" call referenced in the per-turn subprocess count below.

### Response Format

```json
// POST /buffer, /repair — on success
{"status": "ok", "count": <number of records processed>}

// POST /search — on success
{
  "results": [
    {"id": "...", "content": "...", "score": 0.8523, "timestamp": "..."},
    ...
  ]
}

// GET /list — on success
{
  "records": [
    {"id": "...", "content": "...", "timestamp": "...", "agent_name": "..."},
    ...
  ],
  "total": <count>
}

// On error (common)
{"status": "error", "message": "<error details>"}
```

---

## 5. Operational Protocol (Fully Automatic Saving & Active RAG)

> **Audience distinction**: The instructions below are classified as **[AI Behavioral Guidelines]** (instructions to Claude itself) and **[Implementation Specs]** (processing to be implemented as a program).

### Complete-Recording Contract

N3MC is marketed as **complete preservation** (完全保存). The hook write path therefore honors these six guarantees:

1. Every user message and every Claude response is recorded in FULL, character-for-character, with no truncation.
2. Long content is split into overlapping chunks by `chunk_text` (max_chars=400, overlap=40, paragraph → sentence → hard-window hierarchy). Each chunk is tagged `[user]` / `[claude]` for single-chunk content, or `[user i/N]` / `[claude i/N]` for multi-chunk content.
3. NO length filter, NO skip-pattern filter. Fenced code blocks ARE substituted with `[code omitted]` per documented product design; this is the single documented exception and applies to conversation records only (source code is not in scope of complete recording). N3MemoryCore records conversation text, not source code. Inline backtick spans are preserved.
4. An append-only JSONL audit log at `~/.n3mc/.memory/audit.log` is written BEFORE anything can fail. This is the last-resort authoritative transcript. Every hook invocation (UserPromptSubmit + Stop) writes one JSON record: `{"ts", "hook", "raw", "payload"}`.
5. On HTTP POST failure to the embedding server, writes fall back to `_buffer_direct` (direct SQLite insert without an embedding; re-indexed by the next `--repair`). Silent drops are forbidden; every failure path either succeeds via fallback or emits to stderr.
6. Image-only prompts still trigger repair + search + Claude-turn save + audit-log entry. Only Step 4 (user save) is skipped because there is no user text to record.

- **[Automated] Auto-repair, search, and conversation saving**: The `UserPromptSubmit` hook (`n3memorycore/n3mc_hook.py`, registered as `n3mc-hook`) calls `n3mc --hook-submit`, which performs the following steps in a single process. **Step 0 — audit log (always first)**: before anything else, every hook invocation appends one JSON record `{"ts", "hook", "raw", "payload"}` to the append-only `~/.n3mc/.memory/audit.log`. This is the last-resort authoritative transcript; it is written BEFORE anything can fail, so even if every later step errors out, the raw input is preserved. Then: `--repair` (fix unindexed data), `--buffer` (auto-save Claude's previous response with complete preservation — long content is chunked via `chunk_text(max_chars=400, overlap=40)` and saved as `[claude]` / `[claude i/N]` records), `--search` (retrieve memories), and `--buffer` (auto-save the user message with complete preservation — chunked and saved as `[user]` / `[user i/N]` records). **No length filter, no skip-pattern filter**: every non-empty input is recorded character-for-character with no truncation. When Claude Code passes an image+text prompt, the `prompt` field may be a JSON array; `_extract_text()` extracts only `type=="text"` parts. If the result is empty (image-only prompt), only Step 4 (user save) is skipped because there is no user text to record — repair, Claude-turn save, search, and the audit-log entry still run. The raw multimodal payload is captured in `audit.log`.
- **[Implementation Spec] Hook subprocess execution method (must not be changed)**: Subprocess calls within `n3memorycore/n3mc_hook.py` and `n3memorycore/n3mc_stop_hook.py` must use **`subprocess.run` (synchronous/blocking)**, waiting for each command to complete before proceeding to the next. **Do not use `Popen` (async/fire-and-forget).** **Reason**: `--repair` → `--search` has an execution order dependency (search results are incomplete if repair hasn't finished). Additionally, if control returns to Claude before `--search` finishes writing to `memory_context.md`, the search results cannot be read. Asynchronizing for speed destroys data integrity and search accuracy. Note: only the FastAPI server startup (§3 Clean CLI) uses `Popen` (since it does not need to wait for startup to complete).
- **[Automated] Auto-save of Claude's responses**: The `Stop` hook (`n3memorycore/n3mc_stop_hook.py`, registered as `n3mc-stop-hook`) first writes its Step 0 audit-log record (in-process), then invokes two synchronous subprocesses in order:
  1. `python -m n3memorycore.n3memory --save-claude-turn` (stdin = Stop-hook JSON) — chunked save of `last_assistant_message` with `[claude]` / `[claude i/N]` prefixes, sharing the existing turn_id for Q-A pairing. No length filter is applied; every non-empty response is recorded. **Do not use `--buffer -` here**: the Stop hook already consumed stdin to write the audit log.
  2. `python -m n3memorycore.n3memory --stop` — idempotent `@import` setup in `<cwd>/.claude/CLAUDE.md` (see "`--stop` Hook Specification" above).

  Total subprocesses per turn: `UserPromptSubmit` × 1 (`--hook-submit`) + `Stop` × 2 (`--save-claude-turn` + `--stop`) = 3 calls.
- **[Implementation Specs] Detection of unindexed data**: Detect records that exist in the `memories` table but not in `memories_vec` **or** `memories_fts` as unindexed data (double LEFT JOIN checking both indexes). Generate embeddings for vec-missing records and re-insert into FTS for fts-missing records. Also runs the one-time FTS punctuation cleaning migration on first execution (see §3 "FTS punctuation stripping"). Output a warning only if 1 or more records were repaired.
- **[AI Behavioral Guidelines] Fully Automatic Saving**: All conversations are automatically saved by hooks (UserPromptSubmit / Stop). Claude does not need to judge what to save or manually call `--buffer`. **Every non-empty user message and every non-empty Claude response is recorded in FULL, character-for-character, with no truncation.** There is no length filter (the previous `len(text) >= 10` / `>= 3` thresholds are REMOVED) and no skip-pattern filter (the previous `_SKIP_PATTERNS` "ok / yes / thanks" routine-response filter is REMOVED). Short acknowledgements such as `ok` or `yes` are now recorded like any other input.
- **[AI Behavioral Guidelines] Silence on successful save**: When a save succeeds, make no report or acknowledgment — maintain silence.
- **[Implementation Specs] Fatal failure warning**: Only when a DB write fails, or the DB record count does not change after INSERT, display the following prominently:
  > ⚠️ Physical save failed. Current memories may be lost.
- **[AI Behavioral Guidelines] Active RAG**: When knowledge is insufficient, proactively execute `--search` to retrieve relevant memories. The command is auto-approved via `permissions.allow` — no confirmation needed.
- **[AI Behavioral Guidelines] Recall acknowledgment**: When `--search` results **actually shape your reply** (you are recalling information saved in an earlier turn), open the reply with a short acknowledgment **in the user's language**, e.g. Japanese 「前回の回答がメモリに保存されています。」 or English "Pulling this from earlier memory in this session." **If no relevant memory was found, or if retrieved snippets did not influence your answer, do not announce anything.** Never announce the mere act of searching — only the act of recalling. This lets the user see the memory layer is alive each turn.
- **[Implementation Spec] Context injection (both stdout and file are required)**: `--search` results must be **printed to stdout via `print()`** and simultaneously **written to file** `~/.n3mc/.memory/memory_context.md`. **Both must be performed.** Without stdout output, Claude cannot see the search results and will respond "I don't have that memory" even when the data exists in the DB. File write alone does not deliver results to Claude.
- **[Implementation Spec] Memory-context freshness on every invocation (no stale context)**: `cmd_search` MUST overwrite `memory_context.md` and print to stdout on **every** invocation, regardless of outcome. Failure paths emit a degraded-state placeholder so Claude does not silently consume last turn's results as if they were current:
  - **Empty query** (image-only prompt, or `--search ""`) → write `# Recalled Memory Context\n\n_No relevant memories found._\n` and print it to stdout. Empty does not mean "skip the write" — the write IS the signal that no relevant memory exists for this turn.
  - **Server unreachable / `/search` non-2xx error** → write `# Recalled Memory Context\n\n_(memory search unavailable: <reason>)_\n` and print it to stdout. This makes the memory layer's downtime visible to Claude rather than presenting last turn's results.
  - **Successful results** → write the rendered markdown (Previous matching exchange(s) + Other memories) to both channels.

  Stale `memory_context.md` from a prior turn becoming the new session's `@import`-resolved context is treated as a **correctness bug**, not a performance trade-off. The fresh write is mandatory on every `cmd_search` invocation, including via `--hook-submit` for image-only prompts (spec §5 "Image-only prompts still trigger ... search").
- **[Implementation Specs] Fenced code blocks are substituted with `[code omitted]`**: Fenced code blocks are replaced with `[code omitted]` per documented product design: N3MemoryCore records conversation text, not source code. Inline backtick spans are preserved. All non-code content is stored verbatim (no length filter, no skip-pattern filter, see Complete-Recording Contract above). `_CODE_BLOCK_RE` in `purify_text` / `_purify` substitutes closed fenced blocks only; inline code and unclosed fences are left untouched.
- **[Implementation Spec] stdin input**: `--buffer` accepts `-` in place of a text argument to read from standard input (e.g., `cat file.txt | n3mc --buffer -`). Do NOT use `--buffer -` inside the Stop hook (`n3memorycore/n3mc_stop_hook.py`) — the Stop hook itself reads Claude Code's JSON from stdin, so using `-` would double-consume it and break the hook. `--buffer` also accepts an optional `--agent-id ID` argument to tag the record with the agent identifier (e.g., `n3mc --buffer "text" --agent-id "claude-code"`).
- **[Customization] Language localization**: Not applicable. Skip patterns (`_SKIP_PATTERNS`) have been REMOVED in favor of the complete-recording contract; there is nothing language-specific to localize in the hook filter layer.
- **[Implementation Specs] Deduplication & HTTP-failure fallback**:
  - While server is running: Skip saving if cos_sim ≥ `dedup_threshold` (0.95) or exact string match.
  - On HTTP POST failure to the embedding server (server stopped, timeout, or non-2xx): the write falls back to `_buffer_direct()`, which inserts the record directly into SQLite without an embedding vector. The missing vec-index entry is repaired on the next `--repair` call. **Silent drops are forbidden**: every failure path either succeeds via `_buffer_direct` or emits a message to stderr. Combined with the Step 0 audit log, this guarantees that no user or Claude turn can be lost.
- **[Implementation Specs] HTTP timeout**: `_post()` uses a 30-second timeout for HTTP requests to the server. This accommodates CPU-based embedding inference which can take 4–5 seconds under load.
- **[Implementation Specs] ensure_server() concurrent startup wait**: When a PID file conflict is detected (another process is starting the server), wait up to 60 seconds (120 × 0.5s) before failing. This matches the normal 60-second startup timeout and accommodates first-run model download.
- **[Implementation Specs] _load_vec_extension idempotency**: Loading the sqlite-vec extension twice on the same connection is a no-op — if the extension is already loaded, the load call raises an exception containing "already" or "duplicate" in its message. Catch such exceptions and continue; re-raise all others.
- **[Implementation Specs] delete_memory transactional**: `delete_memory` calls `_load_vec_extension(conn)` first, then wraps all three DELETEs (memories_fts, memories_vec, memories) in a try/except with `conn.rollback()` on failure. All three indexes succeed or all roll back together.
- **[Implementation Specs] lifespan startup**: Use FastAPI's `@asynccontextmanager` lifespan pattern (`async def lifespan(app: FastAPI)` with `yield`, passed to `FastAPI(lifespan=lifespan)`) instead of the deprecated `@app.on_event("startup")`.
- **[Implementation Specs] Vector model marker**: On every `--repair` call, the server writes the currently-active embedding model name into `~/.n3mc/.memory/vec_model.txt` (creating it on first run). If the file exists with a name that differs from `cfg['embed_model']`, the server prints a warning to stderr — the on-disk vectors were generated by a different model than the one currently configured, so similarity search will be degraded until the vectors are regenerated. The reference implementation does NOT auto-trigger re-embedding (the operation can take many minutes on a large DB); the user is responsible for the manual upgrade procedure documented in §3 "Switching to a language-specialised model".
- **[AI Behavioral Guidelines] Utilizing CLAUDE.md**: At the start of the next session, read `.claude/CLAUDE.md` and inherit the behavioral guidelines from the previous session. Memory context is loaded via `@import` from `memory_context.md` (see §4).

### Q-A Pairing Contract
Every [user] and [claude i/N] row recorded for the same conversational turn shares a `turn_id` (UUID4). This lets N3MemoryCore reassemble the full previous exchange when a similar question returns later, instead of surfacing only isolated chunks.

1. **Turn identifier**: When the UserPromptSubmit hook saves the user's message U_k, it generates a new turn_id T_k, attaches T_k to every [user i/N] chunk, and persists T_k to `.memory/turn_id.txt`.
2. **Claude-side pairing**: When the Stop hook saves Claude's response C_k, it reads T_k from `.memory/turn_id.txt` and attaches it to every [claude i/N] chunk. After the save loop the file is cleared.
3. **Recovery path**: If the Stop hook is skipped, the next UserPromptSubmit's Step 2 (save previous Claude response) reads the file and reuses T_k, so U_k and C_k still share the same turn_id.
4. **Pair reconstruction**: `/search` returns two fields — `results` (score-ranked hits) and `pairs` (for every hit with a turn_id, the full ordered list of sibling rows). Ordering is: [user] rows first, then [claude] rows, each group by chunk index "i/N", then by rowid.

   **Critical implementation rule**: `hybrid_search` returns `{"results", "pairs"}`, but **`results` MUST NOT exclude records that also belong to a pair**. `results` contains all score-ranked hits in full (pair members are kept). `pairs` is a separate supplementary list of siblings grouped by turn_id. Overlap is allowed because the renderer places `## Top matches` first and `## Previous matching Q-A exchanges` second, so Claude reads the highest-confidence answer first regardless of overlap.
5. **Rendering (v1.2.0+)**: `memory_context.md` puts a **`## Top matches (use these to answer the question)` block FIRST** (v1.2.0+ renderer; carried forward unchanged in v1.3.0). Top matches contain all high-scoring records in score order, **including records that also belong to a Q-A pair** (do NOT suppress them from the result list). Q-A pairs follow as a **`## Previous matching Q-A exchanges (supplementary context)`** block placed AFTER Top matches. Overlap between sections is allowed: a record may appear in both Top matches and a Q-A pair — harmless because Claude reads Top matches first.
   **Important background**: Earlier spec drafts had the reverse ordering (Q-A pairs at top, pair members suppressed from results). This caused a critical failure mode in databases dominated by recurring conversation themes (e.g., a user who regularly discusses project management): the chunks of one popular turn_id would occupy the top of the score ranking, and pair extraction would yank them out of `results`, leaving `## Top matches` empty of useful data. Claude, encountering 5KB of unrelated conversation history at the top of memory_context.md, would conclude "the answer isn't here" and fall back to MCP / training, falsely reporting "I don't have that information" even though the actual data record existed in the Pro DB. The new ordering eliminates this failure mode.
6. **Schema**: `memories.turn_id TEXT` with index `idx_memories_turn_id`. `insert_memory(..., turn_id=None)` is the keyword-only parameter. `get_memories_by_turn_id(conn, turn_id)` is the helper used by retrieval.

---

## 6. Autonomous Evaluation ([N3MC v1.3.0 Evidence Report])
After implementation is complete, autonomously resolve the following tests and report a perfect score (⭐⭐⭐⭐⭐).

1. **Resident Speed & Process Management**: Measure and record the response time of `--search` (target: up to 2.0s on CPU). Verify that PID file creation, deletion, and restart function correctly.

2. **Force-termination Test (Proof of Durability)**: Save one record via `--buffer`, immediately force-terminate the process (Ctrl+C), then physically prove that the record remains in the DB after restart by running `--list`. The output format for `--list` is as follows (one record per line, tab-separated, exactly four columns):

   ```
   [UUIDv7]\t[timestamp]\t[agent_name]\t[first 80 characters of content]
   ```

   - The columns are joined with a single tab (`\t`) — the four spaces shown in earlier drafts of this spec were typographic, not literal.
   - "First 80 characters of content" means `content[:80]` — the leading 80 characters of the **full content string**, NOT of the first line. Newlines (`\n`, `\r`) within those 80 characters MUST be replaced with a single space so the tab-separated layout stays on one line per record.
   - If `agent_name` is `NULL`, render it as `-`.
   - Output the total record count `Total: N records` at the end.
   - Statically confirm during code review that `write_buffer`, `batch_insert`, asynchronous writes, and deferred COMMITs are absent.

3. **Real Person Test (Historical Data)**: After saving text about a real historical figure, run `--search` with that person's name and confirm that the relevant record appears **within the top 3 results** as the pass criterion. Additionally, verify that `--search` results are **printed to stdout** (visible in the terminal). If stdout is empty and results are only written to `memory_context.md`, this test fails.
   - Japanese example: "坂本龍馬" (Sakamoto Ryoma)
   - English example: "Abraham Lincoln"

4. **Fictional Setting Test (Creative Fictional Settings)**: After saving text containing a fictional setting (e.g., character names, world-building, proper nouns), retrieve it via `--search` and confirm that **all fields of the saved text are restored verbatim** as the pass criterion. Fictional setting text (non-code) is preserved verbatim per the Complete-Recording Contract; code blocks in Claude's response are still substituted with `[code omitted]` by documented design.
   - Japanese example: "ソラニア (fictional floating city)"
   - English example: Any fictional character, location, or proper noun

5. **FTS Punctuation Resilience Test**: Save text containing brackets and punctuation via `--buffer`, then run `--search` with a query that omits the brackets. The pass criterion is that the record appears **within the top 3 results**. This test verifies that punctuation stripping is applied both to the FTS5 porter unicode61 index on INSERT and to the query on MATCH.
   - English example: save `"Planet [Alpha-9] temperature settings"`, search with `"Alpha-9 temperature"`
   - Japanese example: save `架空の惑星「アルファ9」の気温設定`, search with `アルファ9の気温`

6. **--repair FTS Migration Test**: Delete the `~/.n3mc/.memory/fts_punct_cleaned` marker file, then run `n3mc --repair` and verify the following:
   - FTS cleaning is executed (if punctuated records exist, the count is reported)
   - The `~/.n3mc/.memory/fts_punct_cleaned` marker file is created
   - Running `n3mc --repair` again skips FTS cleaning (full-table scan is avoided due to the marker)

7. **Hook integration test**: Verify the following within a Claude Code session:
   1. **Global hook registration is in place** (mandatory gate — see §4 ① "Mandatory automated step"). Run:
      ```bash
      python -c "import json,os; s=json.load(open(os.path.expanduser('~/.claude/settings.json'))); h=s.get('hooks',{}); assert any('n3mc_hook' in c.get('command','').lower() or 'n3mc-hook' in c.get('command','').lower() for e in h.get('UserPromptSubmit',[]) for c in e.get('hooks',[])), 'UserPromptSubmit hook missing'; assert any('n3mc_stop_hook' in c.get('command','').lower() or 'n3mc-stop-hook' in c.get('command','').lower() for e in h.get('Stop',[]) for c in e.get('hooks',[])), 'Stop hook missing'; print('OK')"
      ```
      Pass criterion: prints `OK`. If this assertion fails, **the implementation is not complete** — run `n3mc --init` (Path A) or re-run the idempotent installer (Path B) in §4 ① before continuing. Without this gate, the next two sub-tests will appear to pass when run from inside the project directory (where local hooks may exist or developers run hooks manually) yet fail silently for any session the user opens elsewhere — exactly the cross-session RAG failure this gate exists to prevent.
   2. **UserPromptSubmit**: From a Claude Code session running **outside the n3mc-free project directory** (e.g. `~`), send a user message and confirm that `--search` results are written to `~/.n3mc/.memory/memory_context.md`. Confirm that both the preceding Claude response and the user message are saved to the DB (check `n3mc --list` for records with `[claude]` and `[user]` prefixes). The "outside the project directory" stipulation is essential — running the test from inside the project would mask a missing global registration.
   3. **Stop**: After session termination of that same outside-the-project session, confirm that Claude's final response is saved to the DB. Confirm that the project's `.claude/CLAUDE.md` contains an `@import` line pointing to the absolute path of `~/.n3mc/.memory/memory_context.md`.

8. **memory_context.md dual output test**: Run `--search` and confirm that results are **printed to stdout** AND **written to `~/.n3mc/.memory/memory_context.md`**. If only one output channel works, the test fails.

9. **Complete-Recording Test (replaces the old Noise-Filter test)**: Verify that the filters described below are REMOVED and that every non-empty input is recorded:
    - Pass a 2-character string with `[claude]` prefix via `--hook-submit` and confirm the **record IS saved**. (Old `len(text) >= 3` filter no longer exists.)
    - Pass a 5-character string with `[user]` prefix via `--hook-submit` and confirm the **record IS saved**. (Old `len(text) >= 10` filter no longer exists.)
    - Pass a routine response (e.g., `ok`, `yes`, `thanks`) as `[user]` and confirm the **record IS saved**. (Old `_SKIP_PATTERNS` filter no longer exists.)
    - Verify that an `audit.log` entry is written for every hook invocation, independent of whether the buffer write succeeds.

10. **Fully Automatic Saving Test**: Verify that hooks save all conversations automatically without Claude's manual intervention.
    1. Record the current DB record count via `--list`.
    2. Conduct **3 or more turns** of conversation within a Claude Code session (user messages + Claude responses). Claude must **not call `--buffer` manually** at any point during this test.
    3. Re-check the DB record count via `--list` and confirm that both user messages (`[user]` prefix) and Claude responses (`[claude]` prefix) have been auto-saved.
    4. **Pass criteria**: **Every** conversation turn has its user message and its Claude response saved — nothing is dropped for being short or matching a routine pattern (per the Complete-Recording Contract). No evidence of Claude manually calling `--buffer`.

---

## 7. Automated Tests (pytest)

> **Purpose**: Supplement the manual Evidence Report with repeatable, automated regression tests. Run with `python -m pytest tests/ -v` from the project root.
>
> **Prerequisites**: `pip install pytest httpx`
>
> **Embedding model**: Tests that require the embedding model (`test_processor.py::TestEmbedding`, `test_api.py`) use a session-scoped fixture to load the model once (~5s). Layer 1 tests use deterministic dummy vectors to avoid model dependency.

### Directory Structure

Tests live at the repository root (NOT inside the package). Imports resolve via the installed `n3memorycore` package, not via `sys.path` hacks.

```
n3mc-free/
└── tests/
    ├── conftest.py          # Shared fixtures: isolated DB, config, dummy vectors
    ├── test_database.py     # Layer 1: DB unit tests (CRUD, schema, transactions)
    ├── test_processor.py    # Layer 2: Ranking math, purify `[code omitted]` substitution, Refresh
    ├── test_api.py          # Layer 3: FastAPI endpoint tests (TestClient)
    └── test_hooks.py        # Layer 4: Hook integration (complete-preservation chunking, image strip, audit log)
```

Standard fixture import style:

```python
# tests/conftest.py
from n3memorycore.core.database import get_connection, init_db
from n3memorycore.core.processor import get_model
```

Tests should NOT use `sys.path.insert(...)`. Run from the repo root after `pip install -e ".[test]"`:

```bash
python -m pytest tests/ -v
```

### Layer 1: `test_database.py` (27 tests)

| Test Class | Tests | Coverage |
|---|---|---|
| `TestSchema` | `init_db_creates_tables`, `migrate_schema_idempotent`, `migrate_schema_adds_missing_columns` | Schema creation & migration |
| `TestInsertAndRetrieve` | `insert_and_count`, `insert_populates_all_three_tables`, `insert_without_embedding`, `get_memory_by_rowid`, `get_all_memories` | CRUD & 3-table consistency |
| `TestDelete` | `delete_removes_from_all_three_tables`, `delete_nonexistent_no_error` | Transactional delete |
| `TestGC` | `gc_deletes_expired`, `gc_keeps_recent` | Retention cleanup |
| `TestDedup` | `check_exact_duplicate_true`, `check_exact_duplicate_false` | Exact dedup |
| `TestUnindexed` | `find_unindexed_vec_missing`, `find_unindexed_all_indexed` | Repair detection |
| `TestFTS` | `strip_fts_punctuation`, `quote_fts_query`, `quote_fts_query_max_terms`, `search_fts_basic`, `search_fts_short_query_skipped`, `search_fts_punctuation_resilience` | FTS5 tokenization |
| `TestVectorSearch` | `search_vector_returns_results`, `search_vector_empty_db` | KNN search |
| `TestSerialization` | `serialize_vector_roundtrip` | Binary encoding |

### Layer 2: `test_processor.py` (24 tests)

| Test Class | Tests | Coverage |
|---|---|---|
| `TestCosineSim` | `identical_vectors`, `orthogonal_vectors`, `clamp_negative`, `intermediate_value` | L2→cosine conversion |
| `TestTimeDecay` | `now_returns_one`, `half_life`, `floor_value`, `invalid_timestamp_returns_one` | Half-life decay |
| `TestKeywordRelevance` | `below_threshold`, `perfect_match`, `partial_match`, `zero_max` | BM25 normalization |
| `TestPurification` | `code_block_replaced_with_omitted`, `inline_code_preserved`, `multiple_code_blocks_replaced`, `no_code_blocks_unchanged` | Fenced code blocks are substituted with `[code omitted]` per documented design; inline code preserved |
| `TestEmbedding` | `passage_prefix`, `query_prefix`, `embed_passage_function`, `embed_query_function`, `same_text_similar_vectors` | Embedding model |
| `TestRefresh` | `refresh_replaces_record`, `refresh_updates_timestamp` | Knowledge Refresh |
| `TestBiasScoring` | `session_bias_match/mismatch`, `local_bias_match/mismatch`, `full_scoring_formula` | Ranking formula |

### Layer 3: `test_api.py` (21 tests)

| Test Class | Tests | Coverage |
|---|---|---|
| `TestHealth` | `health_ok` | Server status |
| `TestBuffer` | `saves_record`, `empty_content`, `with_agent_name`, `exact_dedup`, `preserves_code_blocks_verbatim` | Save endpoint |
| `TestSearch` | `empty_db`, `buffer_and_search_roundtrip`, `empty_query`, `returns_score` | Search endpoint |
| `TestRepair` | `repair_fixes_unindexed` | Repair endpoint |
| `TestList` | `list_empty`, `list_after_buffer` | List endpoint |
| `TestDelete` | `delete_existing_record`, `delete_nonexistent`, `delete_blocked_when_not_pro` | Delete endpoint |
| `TestGC` | `gc_deletes_expired`, `gc_blocked_when_not_pro` | GC endpoint |
| `TestImport` | `import_jsonl`, `import_keep_owner`, `import_blocked_when_not_pro` | Import endpoint |

### Layer 4: `test_hooks.py` (20 tests)

| Test Class | Tests | Coverage |
|---|---|---|
| `TestChunkText` | `short_text_single_record`, `long_text_multi_chunk`, `chunk_prefix_numbering`, `paragraph_split`, `sentence_split`, `hard_window_fallback` | Complete-preservation chunking |
| `TestStripImages` | `no_images_unchanged`, `strips_base64_image`, `image_only_becomes_empty`, `non_json_passthrough` | Image payload stripping |
| `TestExtractText` | `plain_string`, `multimodal_json`, `image_only_returns_empty`, `empty_returns_empty` | Multimodal text extraction |
| `TestCompleteRecording` | `routine_ok_is_saved`, `short_claude_response_is_saved`, `short_user_message_is_saved`, `audit_log_entry_written` | Complete-Recording Contract (filters removed) |
| `TestStopIdempotency` | `import_line_not_duplicated`, `rules_file_created` | --stop idempotency |

### Running

```bash
# All tests
python -m pytest tests/ -v

# Single layer
python -m pytest tests/test_database.py -v

# Skip slow embedding tests
python -m pytest tests/ -v -k "not TestEmbedding"
```

> **⚠️ Relationship to Evidence Report**: Automated test failures do **not** block the ⭐⭐⭐⭐⭐ evaluation in §6. The Evidence Report is the sole pass/fail criterion for implementation completion. Automated tests are a supplementary regression suite that developers run at their discretion — they must not cause an infinite fix-and-retry loop during initial implementation.

---

## 📎 Reference: GPU Acceleration

> With the current default configuration (768-dim multilingual-e5-base model, 1 embedding per prompt), CPU performance is sufficient. GPU is not required.
> Consider GPU acceleration only if switching to a 1024-dim model or if latency is a concern.

If you have an NVIDIA CUDA-compatible GPU, the following command may speed up embeddings:

```
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

- `sentence-transformers` uses PyTorch internally — if GPU-enabled PyTorch is installed, embedding inference automatically runs on the GPU (no code changes needed)
- Cold start (first call after server startup): ~1–1.5 s/call on CPU → ~0.03–0.06 s/call once warm
- GPU may reduce this to ~0.02–0.05 s/call

---

## Appendix: Recommended Development Workflow

> **This appendix is a guide for the human operator.** Copy the prompts inside ``` blocks and paste them into Claude Code. The AI will NOT automatically proceed to the next phase.

| Phase | What you do | Model to use |
|---|---|---|
| 1. Implementation | Paste the prompt to request implementation | **Sonnet** (fast) |
| 2. Debugging | Paste 3 prompts **one at a time** for verification | **Sonnet** |
| 3. Quality review | Paste the prompt for evaluation & improvement | **Opus** (deep reasoning) |

---

### Phase 1: Implementation (Sonnet)

Set the model to **Sonnet** and paste the following:

```
Please implement N3MemoryCore according to this specification.
```

Sonnet will automatically handle code generation, hook setup, and server startup. Once complete, proceed to Phase 2 ("done" ≠ "spec-compliant", so do not stop here).

---

### Phase 2: Debugging (Sonnet)

Continue with **Sonnet** and paste the following 3 prompts **one at a time, in order**.

**① Data Flow Trace** (check for data loss along the pipeline)
```
About N3MemoryCore:
Please trace the end-to-end data flow from the search query to Claude by reading the code.
Check whether there are any points along the way where data is lost.
Please make any necessary corrections.
```

**② Specification Comparison** (find unimplemented behavior)
```
Regarding N3MemoryCore:
Please compare the input and output specifications for each CLI command in the specification document with the actual code, one command at a time.
Look for any behaviour specified in the documentation that has not been implemented.
Please make any necessary corrections.
```

**③ Cross-Session Test** (verify data persists across sessions)
```
Regarding N3MemoryCore:
Please run the commands yourself to verify whether the results of saving with --buffer in Session 1 and using --search in Session 2 are visible to Claude.
Please make any necessary corrections.
```

Once all 3 are done, proceed to Phase 3.

---

### Phase 3: Quality Review (Opus)

Switch the model to **Opus** and paste the following:

```
Please review N3MemoryCore.
Please make any necessary corrections.

How many points out of 10 does N3MemoryCore score as a memory system and RAG?
Please generate a scorecard with separate scores for the memory system and RAG.
```

Opus will actually execute commands and generate a scorecard with two axes: **Memory System** (save, persistence, deduplication) and **RAG** (search accuracy, ranking, noise resilience).

> **Note**: The memory system side should score well, but the RAG side is a basic implementation. **The RAG score is unlikely to exceed 7** because the following are not yet implemented:
> - Language-specific tokenization (e.g., MeCab / SudachiPy for Japanese)
> - Language-optimized embedding model (e.g., multilingual-e5-large)
> - Advanced chunking strategy (language- and structure-aware splitting)
> - Reranking (Cross-Encoder, Cohere Rerank, LLM, etc. for re-ordering search results)
>
> Lenient scoring hides improvement opportunities — evaluate strictly. **Regardless of the score, have Opus specifically identify what is missing and always consult on improvements.**

---

Copyright (C) 2026 NeuralNexusNote™ / ArnolfJp019
All names and logos associated with N3MemoryCore and NeuralNexusNote are trademarks of the author.