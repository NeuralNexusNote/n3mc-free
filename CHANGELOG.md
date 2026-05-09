# Changelog

All notable changes to N3MemoryCore Free are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.3] - 2026-05-09

Patch release hardening the dedup window, aligning the server with the
v1.3.2 spec, and expanding platform documentation. No API changes, no
schema changes.

### Fixed
- **Dedup window too narrow**: cosine-similarity deduplication searched
  only `k=5` candidates, so near-duplicates (cos_sim ≥ 0.95) were missed
  when five or more highly-similar records crowded the top-k window.
  Widened to `k=20`.
- **Duplicate server spawn on missing PID file**: `ensure_server` now
  captures the `Popen` handle and writes the PID atomically, preventing
  a race that caused a second server process to spawn when the PID file
  was absent.
- **`n3mc_stop_hook` silent failures**: exit code was not propagated,
  so Claude Code never surfaced save failures. Exit code is now forwarded
  from `--save-claude-turn`.
- **Pro-only endpoint stubs**: `/delete/{id}`, `/gc`, and `/import` now
  return HTTP 403 "Pro feature" per spec §3 instead of 404.

### Tests
All tests updated to reflect the above fixes; layer counts aligned with
spec §7 (27/24/21/20). New coverage: `TestGC`, `TestRefresh`,
`TestStripImages`, `TestDelete` / `TestGC` / `TestImport` for the 403
stubs. Dead `TestMojibake*` classes removed.

### Docs
- Spec files renamed `v1.3.1 → v1.3.2` and synced with Retrieval
  Extensions v1 addendum.
- Ubuntu 22.04 LTS added as a tested platform alongside Windows 11.

## [1.3.2] - 2026-04-30

Patch release fixing three connected bugs that prevented `n3mc` from
working in Claude Code's production bash subprocess even when interactive
smoke tests passed. No API changes, no schema changes.

### Fixed
- **PATH miss for `n3mc --search`**: Claude Code's bash subshell does not
  reliably include the Python `Scripts/` directory. Switched Active RAG
  rule and project permissions to `python -m n3memorycore.n3memory ...`
  as the canonical invocation form (python is always on PATH when n3mc
  is installed).
- **Backslash paths in hook commands on Windows**: `shutil.which()` returns
  `\`-separated paths, but Claude Code's bash interprets `\n`/`\t` as
  escape sequences, corrupting the path. Normalized via
  `pathlib.Path(...).as_posix()` for both the resolved exe and the
  `python -m` fallback.
- **Idempotency marker mismatch**: the dedupe marker `n3mc_hook`
  (underscore) never matched the resolved exe `n3mc-hook.EXE` (hyphen),
  causing each `n3mc --init` re-run to accumulate a duplicate hook entry.
  Markers now cover both hyphen and underscore forms.

### Tests
96 / 96 passing (unchanged).

## [1.3.1] - 2026-04-27

PyPI-readiness patch. Metadata-only change; no behavior, no API,
no file layout changes. Anyone already on v1.3.0 can stay on v1.3.0
without functional drawbacks — v1.3.1 only fixes packaging metadata
so the project is publishable to PyPI.

### Fixed
- **`pyproject.toml` license metadata** now matches the repository's
  `LICENSE` file. v1.3.0's `pyproject.toml` declared the project as
  MIT (both in `project.license` and in the trove classifier), while
  the actual `LICENSE` file is Apache 2.0. Without this fix,
  `pip install` would surface incorrect license information and PyPI
  would reject the upload (or display the wrong license to users).
  - `project.license`: `MIT` → `Apache-2.0`
  - classifier: `License :: OSI Approved :: MIT License` →
    `License :: OSI Approved :: Apache Software License`

### Tests
96 / 96 passing (unchanged from v1.3.0).

## [1.3.0] - 2026-04-27

Distribution release. The implementation is now `pip install`-able as a
proper Python package, the embedding model defaults to multilingual, and
data lives outside the repository so forks no longer ship anyone's personal
memory. The user-facing CLI surface is unchanged from v1.2.1; everything
new is opt-in or transparent after the documented migration steps.

### Added
- **`pip install` support** — `pyproject.toml` declares the project,
  `setuptools` finds the `n3memorycore` package automatically, and three
  console scripts are exposed: `n3mc`, `n3mc-hook`, `n3mc-stop-hook`.
  `pip install -e .` (editable) and `pip install .` (normal) both work
  from the repo root.
- **`n3mc --init` command** — single-shot setup that creates `~/.n3mc/`,
  writes `config.json` with auto-generated `owner_id` / `local_id`, and
  registers the `UserPromptSubmit` / `Stop` hooks in user-global
  `~/.claude/settings.json` pointing at the installed entry-point scripts.
  Idempotent — re-running replaces existing entries that reference our
  scripts (matched by script-name marker, not full path) instead of
  duplicating.
- **Multilingual default model** — `intfloat/multilingual-e5-base`
  (768-dim) replaces `intfloat/e5-base-v2`. Indexes Japanese, English,
  Chinese, Korean and ~100 other languages out of the box. Users who need
  higher precision in a single language can override via `embed_model`
  in `config.json` or the `$N3MC_EMBED_MODEL` environment variable; a new
  `~/.n3mc/.memory/vec_model.txt` marker records the model that built the
  on-disk vectors and `--repair` warns when the marker disagrees with
  current config (manual re-embed path documented in spec §3).
- **`embed_model` config field** — first-class user override for the
  embedding model, resolved at server startup with priority
  `config.json` → `$N3MC_EMBED_MODEL` → built-in default.
- **`paths.py` module** — centralises path resolution. `$N3MC_HOME`
  overrides the data root if `~/.n3mc/` is not where the user wants it.
- **Encoding regression suite restored** — `tests/test_hooks_encoding.py`
  rewritten for the new package layout (16 tests pinning surrogate
  stripping, SQLite acceptance, purify-applies-sanitization, and the
  cp932→utf-8 mojibake heuristic / recovery roundtrip).

### Changed (BREAKING — repository layout and data location)
- **Package layout**: code moved from root-level scripts (`n3memory.py`,
  `n3mc_hook.py`, `n3mc_stop_hook.py`, `core/`) into the lowercase
  `n3memorycore/` package. Imports are now package-relative
  (`from .core.database import ...`); the legacy `sys.path.insert(...)`
  hack at module top is removed. The server is launched as
  `python -m n3memorycore.n3memory --run-server` (file-path invocation
  no longer works because the relative imports require package context).
- **Data location**: persistent data moved from `<repo>/.memory/` and
  `<repo>/config.json` to `~/.n3mc/.memory/` and `~/.n3mc/config.json`.
  This means the repository can be `pip install`-ed without leaking
  per-user data into anyone else's checkout, and the same install
  serves all working directories. **Existing v1.2.x users must
  manually copy their old `.memory/n3memory.db` and `config.json` to
  the new location before upgrading.**
- **Vector model marker renamed**: `vec_e5v2_migrated` (empty marker)
  → `vec_model.txt` (records the embedding-model name in plain text).
  `--repair` now reads the marker and warns when it disagrees with
  `cfg['embed_model']` — search quality is degraded until the user
  rebuilds vectors against the new model. Auto re-embed is intentionally
  not triggered (can take many minutes on a large DB).
- **`@import` line in `.claude/CLAUDE.md`**: `--stop` now writes the
  **absolute path** to `~/.n3mc/.memory/memory_context.md` (resolved at
  hook execution time per machine). The old relative path
  `@../N3MemoryCore/.memory/memory_context.md` no longer makes sense
  because the data dir is outside the project tree. Migration: the
  relative form is detected and rewritten to the absolute form on the
  next `--stop` call.
- **Permission allow-list**: project `.claude/settings.json` permission
  patterns now reference the `n3mc` console script
  (`Bash(n3mc --search *)` etc.) instead of `Bash(python *n3memory.py* …)`.
- **README / spec restructured**: setup section now leads with two
  parallel paths (Path A: AI-Native regeneration; Path B: pip install).
  Path B includes a 5-step procedure (install → connect → verify →
  restart → confirm) and a dedicated troubleshooting section.

### Fixed
- **`init_db` order tolerance**: creating the `idx_memories_turn_id`
  index no longer fails when called against an old-schema DB that lacks
  the `turn_id` column. The index is created only after `migrate_schema`
  adds the column.
- **`strip_fts_punctuation` word boundary**: punctuation between word
  characters is now substituted with a single space, not deleted. This
  preserves the boundary so `Alpha-9` indexes as two tokens
  (`alpha` + `9`) and a query for just `Alpha` matches the record.
  Previously the dash was deleted, concatenating to `alpha9`, which
  required an exact-substring query to hit.
- **`cmd_repair` count surfacing**: the CLI now reads the HTTP
  `count` field from the `/repair` endpoint and prints
  `Repaired N record(s).` to stderr when any records were repaired.
  Previously the server-side stderr message was discarded by
  `subprocess.DEVNULL`, so `--repair` was completely silent on the
  client side regardless of how many records were re-indexed.
- **Repair-loop error visibility**: failures inside `cmd_hook_submit`'s
  pre-search `--repair` call are now logged via `logger.warning`
  instead of swallowed with `pass`, so degraded states are observable
  in the audit log.

### Restored from v1.2.1 (had been lost during the v1.3.0 refactor)
- `core/processor.sanitize_surrogates` — strips lone UTF-16 surrogates
  to prevent silent SQLite `UnicodeEncodeError` data loss on Windows
  subprocess pipes. Applied in `purify_text`, `_buffer_direct`, and
  the audit-log writers in both hook entry points.
- `run_mojibake_recovery` — one-time, idempotent (marker file
  `~/.n3mc/.memory/mojibake_recovered`) cp932→utf-8 roundtrip on
  existing rows from pre-1.2.0 installs. Prefixes recovered content
  with `[recovered] ` and resyncs FTS. Original timestamps are
  preserved so time-decay history is not zeroed. Invoked once at
  server startup (lifespan).

### Removed
- `tests/test_hooks_encoding.py` (old `sys.path` form) — replaced by
  the new package-relative version with the same coverage.
- Legacy root-level scripts (`n3memory.py`, `n3mc_hook.py`,
  `n3mc_stop_hook.py`, `core/`) — superseded by the `n3memorycore/`
  package.
- `<repo>/memory/` directory and the personal `n3memory.db` it
  contained — never appropriate to ship in a public repository.

### Migration from v1.2.x
1. Back up your existing data: copy `<old-repo>/.memory/n3memory.db`
   and `<old-repo>/config.json` aside.
2. `pip install -e .` (or `pip install .`) the new package.
3. `n3mc --init` to create `~/.n3mc/` and register the new hooks.
4. Move your backed-up files into `~/.n3mc/`:
   - `cp .memory/n3memory.db ~/.n3mc/.memory/n3memory.db`
   - `cp config.json ~/.n3mc/config.json`
5. Restart Claude Code (close every running session first — hooks
   are loaded at session start).
6. Run `n3mc --repair` once. If it warns about a model mismatch,
   either accept degraded search quality on old vectors or follow the
   "Switching to a language-specialised model" procedure in spec §3
   to rebuild vectors against the new default
   (`intfloat/multilingual-e5-base`).

### Tests
96 / 96 passing (80 baseline + 16 encoding regression).

## [1.2.1] - 2026-04-25

Maintenance release that completes the encoding-safety contract declared
in v1.2.0. The v1.2.0 tag shipped with the contract documented but several
of its safety features were missing from the binary; v1.2.1 brings the
implementation back into alignment with its own CHANGELOG.

### Added (restored from v1.2.0 CHANGELOG — were missing in v1.2.0 binary)
- `core/processor.sanitize_surrogates` — strips lone UTF-16 surrogates to
  prevent silent SQLite `UnicodeEncodeError` data loss on Windows
  subprocess pipes. Applied at every DB write entry point (`/buffer`,
  `_buffer_direct`, `cmd_hook_submit`, `--save-claude-turn`,
  `write_audit`).
- `run_mojibake_recovery` — one-time, idempotent (marker file)
  cp932 → utf-8 roundtrip on existing rows from pre-1.2.0 installs.
  Prefixes recovered content with `[recovered] ` and resyncs FTS.
  Original timestamps are preserved so time-decay history is not zeroed.
- `tests/test_hooks_encoding.py` — 13 regression tests pinning surrogate
  stripping, clean-Japanese passthrough, SQLite-accepts-sanitized, full
  hook roundtrips for both `UserPromptSubmit` and `Stop`, and mojibake
  heuristic + recovery.
- `scripts/smoke_ja.py` — Japanese encoding smoke test with `--quick`
  (<3s, `/health` + `/buffer` + `/search` roundtrip) and full mode
  (adds Stop-hook surrogate test + pytest encoding suite).

### Fixed (specification corrections, EN / JP)
- **FTS5 schema**: documented as **standalone** (drop
  `content='memories', content_rowid='rowid'`). External-content FTS5
  doesn't support the `DELETE FROM memories_fts WHERE rowid` pattern;
  literal-spec implementations corrupted the FTS index
  (`"database disk image is malformed"`) on first `--repair` /
  `delete_memory` call.
- **`b_local` Free contradiction**: §3 listed `b_local` mismatch=0.6
  while the same section's Identifiers Note declared `b_local` a Pro
  feature. Resolved in favor of the Note — Free always uses
  `b_local = 1.0`.
- **UTF-8 reconfigure scope**: spec only required reconfigure in
  `n3memory.py main()`. Hooks (`n3mc_hook.py`, `n3mc_stop_hook.py`)
  also read stdin; missing reconfigure silently mojibakes Japanese /
  em-dashes on Windows cp932 and persists corrupted bytes to DB. Spec
  now requires reconfigure at module top of every Python entry point.
- **Stop hook subprocess naming**: §5 said "Stop × 2 (buffer + stop)"
  without defining what "buffer" was. Documented `--save-claude-turn`
  as the canonical chunked-save subcommand and added it to §4.5.
- **`/search` failure → `memory_context.md` staleness**: spec didn't
  define failure-path behavior. `cmd_search` now MUST overwrite
  `memory_context.md` (and print to stdout) on every call, including
  empty-query and degraded states, so prior-turn results never leak
  into the next session via `@import`.
- **`--list` head clarification**: §6.2's "first 80 characters of
  content" was ambiguous (full content vs first line). Now explicit:
  `content[:80]` with newlines replaced by spaces, `agent_name=NULL`
  rendered as `-`, tab-separated layout.

### Changed (repo hygiene)
- `.gitignore` excludes `.claude/settings.local.json` (per-user Claude
  Code allow-list, never appropriate to commit).
- `.claude/CLAUDE.md`, `.claude/rules/n3mc-behavior.md`,
  `.claude/settings.json` committed as canonical scaffolding so a
  fresh fork has memory-context wiring active immediately.

### Migration
No manual migration. The mojibake recovery runs once on first server
start after upgrade; rewritten rows carry the `[recovered] ` prefix.
No schema changes, no config changes, no API changes.

### Tests
96 / 96 passing (83 baseline + 13 encoding regression).

## [1.2.0] - 2026-04-18

Reliability release focused on Windows + Japanese environments. All users on
Windows are **strongly encouraged** to upgrade — pre-1.2.0 builds silently
lost data in specific Japanese / cp932 code paths.

### Fixed
- **Windows cp932 lone-surrogate crash (data loss).** UTF-8 Japanese bytes
  arriving through subprocess stdin pipes could be decoded into lone
  `\udcXX` surrogate halves, which then crashed `sqlite3.execute` with
  `UnicodeEncodeError` on the `check_exact_duplicate` / `insert_memory`
  path. The entire Claude response was silently dropped — a direct violation
  of the complete-preservation contract.
  - Added `_sanitize_for_sqlite` in `n3memory.py` and `_sanitize_surrogates`
    in `core/processor.py`, applied at every DB write entry point
    (`/buffer`, `_buffer_direct`, `cmd_hook_submit`) and inside
    `purify` / `purify_text` for defense-in-depth.
- **Stop hook mojibake.** `n3mc_stop_hook.py` read stdin via
  `sys.stdin.read()`, which on Windows defaults to cp932 and misdecoded
  UTF-8 JSON payloads into mojibake (`縺`, `繧`, `菫`, `繝` …). Replaced
  with explicit `sys.stdin.buffer.read().decode("utf-8", errors="replace")`
  across all hook entry points.
- **Historical mojibake data.** Added a migration utility that detects
  pre-fix mojibake rows, performs a `cp932 → utf-8` roundtrip recovery
  (best-effort; lossy where `errors='replace'` had already destroyed bytes),
  prefixes recovered rows with `[recovered] `, and resyncs the FTS index.

### Added
- **`tests/test_hooks_encoding.py`** — 6 regression tests pinning the fixes:
  sanitizer unit tests (lone surrogate stripping, clean-Japanese
  preservation), SQLite-accepts-sanitized, and full hook round-trips for
  both `UserPromptSubmit` and `Stop`.
- **`scripts/smoke_ja.py`** — Encoding smoke-test with `--quick` mode
  (<3 s, `/health` + `/buffer` roundtrip) and full mode (adds surrogate
  Stop-hook test + pytest encoding suite).
- **`docs/manual_smoke_ja.md`** — Operator-facing smoke procedure for
  release / post-edit verification.
- Specification files renamed from `v1.1.0` to `v1.2.0`:
  `N3MemoryCore_v1.2.0_Free_JP.md`, `N3MemoryCore_v1.2.0_Free_EN.md`.

### Migration notes
- No manual migration required. The historical mojibake recovery runs
  automatically on first startup after upgrade; rows that were rewritable
  will carry the `[recovered]` prefix.
- No schema changes, no config changes, no API changes.

### Upgrade instructions
Drop the updated `N3MemoryCore_v1.2.0_Free_*.md` into Claude Code and ask:
"Please apply this upgrade." Your existing DB will be carried over.

## [1.1.0] - earlier

- Renamed `agent_id` to `agent_name`.
- Added `server_host` / `bind_host` to `config.json` for network deployments.
