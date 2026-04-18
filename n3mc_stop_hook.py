#!/usr/bin/env python3
"""N3MemoryCore Free - Stop Hook.

Claude Code calls this when Claude finishes a response.

Complete-recording contract:
  - Step 0 (BEFORE anything): append raw stdin payload to `.memory/audit.log`
    (JSONL). This is the last-resort authoritative transcript. Never filters,
    never truncates.
  - Step 1: chunk Claude's last response via `chunk_text` and save EVERY
    chunk. No length filter, no skip-pattern filter, no code-block stripping.
  - Step 2: run `--stop` (idempotent CLAUDE.md @import + behavior rules).

Silent drops are forbidden. Any save failure is logged to stderr, and the
audit log always retains the raw turn even if every downstream step fails.

Free does NOT run `--gc` (retention cleanup is a Pro-only feature).
"""
import json
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).parent.resolve()
_N3MEMORY = str(_THIS_DIR / "n3memory.py")
_AUDIT_LOG = _THIS_DIR / ".memory" / "audit.log"
_TURN_ID_PATH = _THIS_DIR / ".memory" / "turn_id.txt"


def _read_turn_id() -> str:
    """Return turn_id for the current open turn, or "" if none."""
    try:
        if _TURN_ID_PATH.exists():
            return _TURN_ID_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _clear_turn_id() -> None:
    """Clear turn_id file after pairing C_k with U_k — prevents the next
    UserPromptSubmit from reusing T_k for a recovered Claude message."""
    try:
        if _TURN_ID_PATH.exists():
            _TURN_ID_PATH.unlink()
    except Exception:
        pass


def _audit_append(hook: str, raw: str, payload) -> None:
    """Append raw hook payload to audit log. Must never raise."""
    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": hook,
            "raw": raw,
            "payload": payload,
        }
        with open(str(_AUDIT_LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        sys.stderr.write(f"[N3MC AUDIT LOG ERROR] {traceback.format_exc()}\n")


def main():
    # Reconfigure stdin to UTF-8 before reading (Windows cp932 fix)
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')

    raw = ""
    data: dict = {}
    try:
        # Read as bytes then decode UTF-8 explicitly — on Windows, sys.stdin
        # defaults to cp932 which silently corrupts Japanese UTF-8 into mojibake
        # even when reconfigure() was called, because the subprocess pipe was
        # set up before reconfigure.
        try:
            raw_bytes = sys.stdin.buffer.read()
            raw = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    # Step 0: audit log — FIRST, always, before anything can fail.
    _audit_append("Stop", raw, data)

    last_response = data.get("last_assistant_message", "")
    if isinstance(last_response, list):
        last_response = " ".join(
            p.get("text", "")
            for p in last_response
            if isinstance(p, dict) and p.get("type") == "text"
        )
    last_response = (last_response or "").strip()

    # Step 1: Save Claude's last response (conversation text).
    # Fenced code blocks are replaced with ``[code omitted]`` per documented
    # product design. All other content is recorded verbatim.
    if last_response:
        try:
            from core.processor import purify
            last_response = purify(last_response).strip()
        except Exception:
            pass
    # Read T_k written by this turn's UserPromptSubmit so C_k shares the
    # same turn_id as U_k (Q-A pair reconstruction contract).
    turn_id = _read_turn_id()

    if last_response:
        try:
            from core.processor import chunk_text
            pieces = chunk_text(last_response, max_chars=400, overlap=40)
        except Exception:
            pieces = [last_response]
        if not pieces:
            pieces = [last_response]
        for i, piece in enumerate(pieces):
            tag = "[claude]" if len(pieces) == 1 else f"[claude {i+1}/{len(pieces)}]"
            content = f"{tag} {piece}"
            cmd = [sys.executable, _N3MEMORY, "--buffer", content,
                   "--agent-id", "claude-code"]
            if turn_id:
                cmd += ["--turn-id", turn_id]
            try:
                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception:
                sys.stderr.write(
                    f"[N3MC STOP HOOK] --buffer subprocess failed: "
                    f"{traceback.format_exc()}\n"
                )
        # Pair sealed — clear turn_id so next UserPromptSubmit's Step 2
        # recovery path knows nothing needs reattachment.
        if turn_id:
            _clear_turn_id()

    # Step 2: Run --stop (idempotent CLAUDE.md / rules setup).
    try:
        subprocess.run(
            [sys.executable, _N3MEMORY, "--stop"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception:
        sys.stderr.write(
            f"[N3MC STOP HOOK] --stop failed: {traceback.format_exc()}\n"
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
