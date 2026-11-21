"""Locations of the user's data. Resolved at import time.

Code lives in the repository; personal data lives in ``~/.n3mc/`` (or
``$N3MC_HOME``), so the same package can be installed by anyone.
"""
import os

_env_home = os.environ.get("N3MC_HOME")
HOME_DIR = os.path.abspath(os.path.expanduser(_env_home)) if _env_home else os.path.join(
    os.path.expanduser("~"), ".n3mc"
)
MEMORY_DIR = os.path.join(HOME_DIR, ".memory")

CONFIG_FILE = os.path.join(HOME_DIR, "config.json")
DB_PATH = os.path.join(MEMORY_DIR, "n3memory.db")
PID_FILE = os.path.join(MEMORY_DIR, "n3mc.pid")
AUDIT_LOG = os.path.join(MEMORY_DIR, "audit.log")
SERVER_LOG = os.path.join(MEMORY_DIR, "server.log")
NOTICES_FILE = os.path.join(MEMORY_DIR, "notices.json")
TURN_ID_FILE = os.path.join(MEMORY_DIR, "turn_id.txt")
VEC_MODEL_FILE = os.path.join(MEMORY_DIR, "vec_model.txt")  # v1.7.0 and earlier; read only
SERVER_START_FAILED = os.path.join(MEMORY_DIR, "server_start_failed")
# Only used to retire the file older versions wrote.
CONTEXT_FILE = os.path.join(MEMORY_DIR, "memory_context.md")
CONTEXT_RETIRED = os.path.join(MEMORY_DIR, ".memory_context_retired")


def claude_paths(cwd=None):
    """Project-local Claude Code paths, resolved from the current directory."""
    base = os.path.abspath(cwd or os.getcwd())
    claude_dir = os.path.join(base, ".claude")
    rules_dir = os.path.join(claude_dir, "rules")
    return {
        "claude_dir": claude_dir,
        "claude_md": os.path.join(claude_dir, "CLAUDE.md"),
        "rules_dir": rules_dir,
        "behavior_md": os.path.join(rules_dir, "n3mc-behavior.md"),
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
