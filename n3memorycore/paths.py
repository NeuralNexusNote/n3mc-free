"""Paths for the N3MemoryCore data folder (resolved at import time)."""
import os

HOME_DIR = os.path.abspath(os.path.expanduser(os.environ.get("N3MC_HOME") or os.path.join("~", ".n3mc")))
MEMORY_DIR = os.path.join(HOME_DIR, ".memory")
DB_PATH = os.path.join(MEMORY_DIR, "n3memory.db")
PID_FILE = os.path.join(MEMORY_DIR, "n3mc.pid")
AUDIT_LOG = os.path.join(MEMORY_DIR, "audit.log")
CONFIG_FILE = os.path.join(HOME_DIR, "config.json")
NOTICES_FILE = os.path.join(MEMORY_DIR, "notices.json")
TURN_ID_FILE = os.path.join(MEMORY_DIR, "turn_id.txt")
SERVER_LOG = os.path.join(MEMORY_DIR, "server.log")
SERVER_START_FAILED_FILE = os.path.join(MEMORY_DIR, "server_start_failed")
CONTEXT_FILE = os.path.join(MEMORY_DIR, "memory_context.md")  # retired in v1.4.0; used only for cleanup
CONTEXT_RETIRED_MARKER = os.path.join(MEMORY_DIR, ".memory_context_retired")


def claude_paths(cwd=None):
    """Project-specific .claude/ paths, resolved from the current directory."""
    base = os.path.abspath(cwd or os.getcwd())
    claude_dir = os.path.join(base, ".claude")
    return {
        "claude_dir": claude_dir,
        "rules_dir": os.path.join(claude_dir, "rules"),
        "behavior_md": os.path.join(claude_dir, "rules", "n3mc-behavior.md"),
        "claude_md": os.path.join(claude_dir, "CLAUDE.md"),
        "settings": os.path.join(claude_dir, "settings.json"),
    }


def python_cmd():
    """The command that starts a child Python: [sys.executable], plus -P on Python 3.11 and later so
    that the child does not put the current folder on sys.path (see the safe-path block at the top of
    the entry modules). -P is left out when this package itself lives in the current folder (a source
    checkout run in place), because the child could then not find the package."""
    import sys
    cmd = [sys.executable]
    try:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        pkg_parent = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if sys.version_info >= (3, 11) and pkg_parent != cwd:
            cmd.append("-P")
    except Exception:  # noqa: BLE001
        pass
    return cmd
