"""Stop hook entry: write audit.log first, then `--save-claude-turn` (same stdin) and `--stop`."""
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
import os
import subprocess
from datetime import datetime, timezone

from .paths import AUDIT_LOG, python_cmd

HOOK_NAME = "Stop"
RAW_MAX = 4096


def _write_audit(raw):
    """Step 0: the raw input goes to audit.log before anything else."""
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {"raw": raw}
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "hook": HOOK_NAME,
              "raw": raw[:RAW_MAX], "payload": payload}
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            # ensure_ascii=True: a lone surrogate (e.g. from a "\ud83d" escape) cannot be encoded as UTF-8
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: audit.log could not be written ({type(e).__name__}: {e})", file=sys.stderr)


def _run(flag, raw=None):
    try:
        proc = subprocess.run([*python_cmd(), "-m", "n3memorycore.n3memory", flag],
                              input=(raw.encode("utf-8", errors="surrogatepass") if raw is not None else b""),
                              check=False)
        return proc.returncode
    except Exception as e:  # noqa: BLE001
        print(f"N3MC: {flag} could not be run ({type(e).__name__}: {e})", file=sys.stderr)
        return 1


def main():
    raw = sys.stdin.read()
    _write_audit(raw)
    rc = _run("--save-claude-turn", raw)
    _run("--stop")
    return rc


if __name__ == "__main__":
    sys.exit(main())
