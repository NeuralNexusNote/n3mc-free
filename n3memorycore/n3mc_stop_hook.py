import sys

for _s_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _s_name, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

import json
import os
import subprocess
from datetime import datetime, timezone

from .paths import MEMORY_DIR, AUDIT_LOG


def _write_audit(hook: str, raw: str, payload: dict) -> None:
    from .core.processor import sanitize_surrogates
    os.makedirs(MEMORY_DIR, exist_ok=True)
    record = json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'hook': hook,
        'raw': sanitize_surrogates(raw[:4096]),
        'payload': sanitize_surrogates(payload),
    }, ensure_ascii=False)
    try:
        with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
            f.write(record + '\n')
    except Exception as e:
        print(f"Warning: audit log write failed: {e}", file=sys.stderr)


def main():
    raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except Exception:
        payload = {'raw': raw}

    _write_audit('Stop', raw, payload)

    subprocess.run(
        [sys.executable, '-m', 'n3memorycore.n3memory', '--save-claude-turn'],
        input=raw,
        encoding='utf-8',
        capture_output=False,
    )

    subprocess.run(
        [sys.executable, '-m', 'n3memorycore.n3memory', '--stop'],
        capture_output=False,
    )


if __name__ == '__main__':
    main()
