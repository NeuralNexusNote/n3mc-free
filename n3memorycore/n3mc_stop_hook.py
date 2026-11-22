"""Stop hook entry: write audit.log, then `--save-claude-turn` and `--stop` synchronously."""
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

for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

import json
import subprocess
from datetime import datetime, timezone

from .paths import AUDIT_LOG, python_cmd


def write_audit(hook, raw_text):
    """Step 0: append {ts, hook, raw, payload} to audit.log. Never raises."""
    try:
        try:
            payload = json.loads(raw_text)
        except Exception:  # noqa: BLE001
            payload = {"raw": raw_text}
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "hook": hook,
               "raw": raw_text[:4096], "payload": payload}
        _os.makedirs(_os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: could not write audit.log ({type(e).__name__}: {e})", file=sys.stderr)


def _read_stdin():
    try:
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read()
        except Exception:  # noqa: BLE001
            return ""


def main():
    raw = _read_stdin()
    write_audit("Stop", raw)
    status = 1
    try:
        sys.stdout.flush()
        proc = subprocess.run([*python_cmd(), "-m", "n3memorycore.n3memory", "--save-claude-turn"],
                              input=raw.encode("utf-8", errors="replace"))
        status = proc.returncode
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: save-claude-turn failed ({type(e).__name__}: {e})", file=sys.stderr)
    try:
        subprocess.run([*python_cmd(), "-m", "n3memorycore.n3memory", "--stop"], stdin=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: --stop failed ({type(e).__name__}: {e})", file=sys.stderr)
    return status  # the result of --stop is not used


if __name__ == "__main__":
    sys.exit(main())
