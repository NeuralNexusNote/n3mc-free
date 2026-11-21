"""Stop hook entry point (console script ``n3mc-stop-hook``).

Step 0: audit.log. Then, synchronously and in order:
  1. ``n3mc --save-claude-turn``  (stdin = the hook JSON) saves Claude's final answer
  2. ``n3mc --stop``              behavior-file upkeep and housekeeping
The exit status is that of --save-claude-turn.
"""
import os as _os
import sys as _sys

if __name__ == "__main__":
    # Safe import path. Started as `python -m <package>.<module>`, Python has put the current folder
    # first on sys.path, so a file there named like a module imported below would run inside this
    # program. Drop that entry, unless this package itself lives in the current folder (a source
    # checkout run in place). Nothing else may be imported before this block.
    try:
        _cwd = _os.path.normcase(_os.path.abspath(_os.getcwd()))
        _pkg_parent = _os.path.normcase(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if _sys.path and _os.path.normcase(_os.path.abspath(_sys.path[0])) == _cwd and _pkg_parent != _cwd:
            del _sys.path[0]
    except Exception:  # noqa: BLE001
        pass

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


from .paths import python_cmd  # noqa: E402


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
    _write_audit("Stop", raw)
    saved = subprocess.run(
        [*python_cmd(), "-m", "n3memorycore.n3memory", "--save-claude-turn"],
        input=raw, text=True, encoding="utf-8", errors="replace",
    )
    subprocess.run([*python_cmd(), "-m", "n3memorycore.n3memory", "--stop"])
    return saved.returncode


if __name__ == "__main__":
    sys.exit(main())
