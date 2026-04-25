"""UserPromptSubmit hook for Claude Code.

Reads JSON from stdin, forwards to `python n3memory.py --hook-submit` as a
single synchronous subprocess. The subprocess writes the audit log, runs
--repair, saves the previous Claude turn, runs --search, and saves the user
message — in that order, in one process.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Spec §3 Clean CLI: force UTF-8 on stdio so non-ASCII (Japanese, em-dash,
# etc.) survives the Windows cp932 default. Must run before any stdin read.
for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
N3MEMORY = HERE / "n3memory.py"


def main() -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        # Synchronous: must complete before Claude runs (spec §5)
        proc = subprocess.run(
            [sys.executable, str(N3MEMORY), "--hook-submit"],
            input=raw,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        print("[N3MC] hook-submit timed out", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[N3MC] n3mc_hook.py failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
