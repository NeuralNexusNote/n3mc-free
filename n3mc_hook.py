"""
N3MemoryCore - n3mc_hook.py
UserPromptSubmit hook: auto-runs --repair + --search + --buffer (auto-save user messages)
Called by Claude Code's UserPromptSubmit hook via settings.json.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).parent.resolve()
_N3MEMORY = str(_THIS_DIR / "n3memory.py")


def main():
    # Reconfigure stdin to UTF-8 before reading (Windows cp932 fix)
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')

    # Read stdin (Claude Code passes hook JSON here)
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    # Pass directly to n3memory.py --hook-submit via subprocess (synchronous)
    result = subprocess.run(
        [sys.executable, _N3MEMORY, "--hook-submit"],
        input=raw,
        capture_output=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
