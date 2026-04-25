"""Stop hook for Claude Code.

Workflow:
1. Audit log entry (in-process, written before anything can fail).
2. subprocess: `python n3memory.py --save-claude-turn` (saves Claude's last
   response with chunked complete-preservation; reads stdin JSON).
3. subprocess: `python n3memory.py --stop` (idempotent CLAUDE.md @import
   setup and behavioral-rules file).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
N3MEMORY = HERE / "n3memory.py"
AUDIT_LOG = HERE / ".memory" / "audit.log"

# UTF-8 console on Windows.
for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _audit(raw: str, payload: object) -> None:
    """Spec §5: audit.log captures raw + payload verbatim. No image stripping."""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": "Stop",
        "raw": raw,
        "payload": payload,
    }
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[N3MC] audit log write failed: {e}", file=sys.stderr)


def main() -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    _audit(raw, payload)

    # ① save Claude's last response (chunked, complete preservation)
    try:
        subprocess.run(
            [sys.executable, str(N3MEMORY), "--save-claude-turn"],
            input=raw,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except Exception as e:
        print(f"[N3MC] --save-claude-turn failed: {e}", file=sys.stderr)

    # ② run --stop (idempotent @import setup)
    try:
        subprocess.run(
            [sys.executable, str(N3MEMORY), "--stop"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except Exception as e:
        print(f"[N3MC] --stop failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
