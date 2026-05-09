"""Path resolution for N3MemoryCore.

All persistent data lives under a single home directory, resolved at
import time from (in priority order):

1. `$N3MC_HOME` environment variable
2. `~/.n3mc/`  (default)

Per-project Claude Code config (`.claude/CLAUDE.md`, `.claude/rules/`) is
resolved against the current working directory at use time, since it is
project-scoped, not user-scoped.
"""
import os


def get_home_dir() -> str:
    env = os.environ.get('N3MC_HOME')
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser('~'), '.n3mc')


HOME_DIR     = get_home_dir()
MEMORY_DIR   = os.path.join(HOME_DIR,   '.memory')
DB_PATH      = os.path.join(MEMORY_DIR, 'n3memory.db')
PID_FILE     = os.path.join(MEMORY_DIR, 'n3mc.pid')
TURN_ID_FILE = os.path.join(MEMORY_DIR, 'turn_id.txt')
CONTEXT_FILE = os.path.join(MEMORY_DIR, 'memory_context.md')
AUDIT_LOG    = os.path.join(MEMORY_DIR, 'audit.log')
CONFIG_FILE  = os.path.join(HOME_DIR,   'config.json')


def claude_paths(cwd: str = None) -> dict:
    """Resolve per-project Claude Code config paths against `cwd`."""
    cwd = cwd or os.getcwd()
    claude_dir = os.path.join(cwd, '.claude')
    return {
        'CLAUDE_DIR':  claude_dir,
        'CLAUDE_MD':   os.path.join(claude_dir, 'CLAUDE.md'),
        'RULES_DIR':   os.path.join(claude_dir, 'rules'),
        'BEHAVIOR_MD': os.path.join(claude_dir, 'rules', 'n3mc-behavior.md'),
    }
