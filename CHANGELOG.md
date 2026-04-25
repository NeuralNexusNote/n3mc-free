# Changelog

All notable changes to N3MemoryCore Free are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
