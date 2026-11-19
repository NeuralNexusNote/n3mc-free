"""UserPromptSubmit hook entry point (console script ``n3mc-hook``).

Step 0: append the raw hook input to audit.log (before anything can fail).
Then run ``n3mc --hook-submit`` synchronously; its stdout goes to Claude's context.
"""
import sys

# Must run before the first stdin read, or non-ASCII input is mangled on Windows (cp932).
for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

import json
import os
import subprocess
from datetime import datetime, timezone

from .paths import AUDIT_LOG


def _write_audit(hook, raw):
    try:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        record = {"ts": datetime.now(timezone.utc).isoformat(), "hook": hook,
                  "raw": raw[:4096], "payload": payload}
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        try:
            data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        except (UnicodeEncodeError, ValueError):
            data = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
        with open(AUDIT_LOG, "ab") as f:
            f.write(data)
    except Exception as e:
        print(f"N3MC: audit.log write failed: {e}", file=sys.stderr)


def main():
    raw = sys.stdin.read()
    _write_audit("UserPromptSubmit", raw)
    proc = subprocess.run(
        [sys.executable, "-m", "n3memorycore.n3memory", "--hook-submit"],
        input=raw, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
