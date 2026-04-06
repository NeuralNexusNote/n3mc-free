"""
N3MemoryCore - n3mc_stop_hook.py
Stop hook: auto-saves Claude's last response + runs --stop
Called by Claude Code's Stop hook via settings.json.

stdin input spec (from Claude Code):
{
  "session_id": "<session ID>",
  "stop_hook_active": true,
  "last_assistant_message": "Full text of Claude's last response"
}
"""
import json
import re
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).parent.resolve()
_N3MEMORY = str(_THIS_DIR / "n3memory.py")

_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```', re.MULTILINE)


def _purify(text: str) -> str:
    return _CODE_BLOCK_RE.sub('[code omitted]', text)


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    for end_char in ('.', '!', '?', '\n'):
        idx = chunk.rfind(end_char)
        if idx > max_chars // 2:
            return chunk[:idx + 1]
    return chunk


def main():
    # Reconfigure stdin to UTF-8 before reading (Windows cp932 fix)
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')

    # Read hook JSON from stdin
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        data = {}

    last_response = data.get("last_assistant_message", "")

    # 1. Save Claude's last response (if substantial)
    if last_response and isinstance(last_response, str):
        cleaned = _purify(last_response.strip())
        truncated = _truncate_at_sentence(cleaned, 300)
        if truncated and len(truncated) >= 3:
            content = f"[claude] {truncated}"
            # Use synchronous subprocess — DO NOT use Popen
            subprocess.run(
                [sys.executable, _N3MEMORY, "--buffer", content, "--agent-id", "claude-code"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

    # 2. Run --stop (idempotent CLAUDE.md and n3mc-behavior.md setup)
    subprocess.run(
        [sys.executable, _N3MEMORY, "--stop"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
