# Changelog

All notable changes to N3MemoryCore Free are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
