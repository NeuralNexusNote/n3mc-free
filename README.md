# N3MemoryCore (N3MC)
> A NeuralNexusNote™ product

🛡️ AI-Native Development Policy
This project prioritizes Instructions over Static Code. Please read our Development Philosophy to understand why you should let AI generate your execution environment.

> 🇯🇵 **[Click here for the Japanese Documentation (日本語版)](./README_JP.md)**
> 🛡️ **[Development Philosophy & AI-Native Policy](./PHILOSOPHY.md)**

> I know Java and C#, but Python is completely new to me.
> The code Claude Code generated — I built it without fully understanding it.
>
> But I tested it, refined the specification, rebuilt it,
> and shipped both Japanese and English versions — Free and Pro.
>
> Because if I could do it, so can you.
>
> The first draft of the specification was written by Copilot.
> Claude Code turned it into working code. Gemini reviewed it.
> Iterated across three AIs — that process is N3MemoryCore.

N3MemoryCore gives Claude Code long-term memory across sessions.
**Two install paths supported**: drop the spec into Claude Code and let it build the system, or `pip install` the reference build and run `n3mc --init`. Either way, no manual hook editing. (See [Setup](#setup) below.)

---

## Features

- 💾 **Fully local** — Your conversations stay on your machine. Nothing sent to the cloud.
- 🔍 **Semantic search** — Finds relevant past conversations even when the exact words differ. Multilingual by default (`intfloat/multilingual-e5-base`) — works for Japanese, English, Chinese, Korean, and ~100 other languages out of the box. Swap to a language-specialised model via `config.json` if you need higher single-language precision.
- 🔄 **Persistent across sessions** — Pick up tomorrow where you left off today.
- ⚡ **Works automatically** — Saving and searching happen automatically. No manual steps needed.
- 🤖 **Multi-agent ready** — Multiple AI agents share one memory DB. Each agent prioritizes its own memories while accessing the team's collective knowledge.
- 🏢 **Team & organization support** — Deploy the server on your network and share memories across your entire team.
- 🔗 **DB merge ready** — Databases built from the same specification are fully compatible. Transfer knowledge when handing off roles, or import memories accumulated in other environments — the DB structure is designed for integration from the ground up.
- 💰 **Reduces token waste** — No more re-explaining past context. Memory search uses local embeddings (zero Claude tokens), and accurate context injection means fewer corrections and back-and-forth.

## How It Works

```
User's message
    │
    ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  1. Auto-save │────▶│ 2. Semantic   │────▶│ 3. Context    │
│  Save last    │     │    search     │     │    injection   │
│  response     │     │  Find related │     │  Feed to       │
│  to DB        │     │  memories     │     │  Claude        │
└──────────────┘     └──────────────┘     └──────────────┘
                                                 │
                                                 ▼
                                          Claude responds
                                          with full context
```

Powered by Claude Code's hook system. Everything happens automatically — no user action required.

### Relationship with Claude's built-in auto-memory

Claude Code has a built-in auto-memory system (`~/.claude/projects/.../memory/`). N3MemoryCore **complements it rather than competing with it**.

| | Claude auto-memory | N3MemoryCore RAG |
|---|---|---|
| **Strengths** | Reliable, loads every session, great for fixed facts | Conversation context, detailed history |
| **Weaknesses** | Cannot capture conversation flow or context | Depends on search quality; not guaranteed to surface |
| **Best for** | User profile, folder paths, stable settings | Conversation threads, past decisions, reasoning |

**Recommended usage:**
- **Fixed information needed every session** (folder paths, user preferences) → save to auto-memory
- **Conversation context and history** (discussion threads, past decisions) → N3MemoryCore accumulates automatically

## Free vs Pro

| | Free | Pro |
|---|---|---|
| Core features (save, search, auto) | ✅ | ✅ |
| Refresh (dedup & update) | - | ✅ Auto-replaces outdated memories |
| Session/environment bias | - | ✅ Prioritizes recent & local context |
| GC (garbage collection) | - | ✅ Auto-cleanup of old memories |
| Import/Export | - | ✅ Data migration & sharing |
| Delete | - | ✅ Individual record deletion |

**Start with Free.** You can upgrade to Pro later using the Upgrade specification.

## Specification Files

| File | Description |
|---|---|
| `N3MemoryCore_v1.3.2_Free_JP.md` | Free edition (Japanese) |
| `N3MemoryCore_v1.3.2_Free_EN.md` | Free edition (English) |

> 💡 **A note on annotations in the specification**
> Throughout the specification file (Markdown), you'll find annotations describing the design intent behind each instruction — written to give you precise control over AI behavior. Before having Claude generate the code, take a moment to read through them. They contain more than just code: the logic behind how to work with AI effectively.

## Setup

There are two supported setup paths. Pick whichever fits your style.

### Path A — AI-Native Setup (canonical)

1. Pick a specification file from the table above
2. Drop the file into the Claude Code prompt
3. Say: "Please implement this."

That's it. Claude Code writes the code, configures the hooks, and sets everything up.
Setup takes roughly 15–30 minutes (varies by environment and model).
Memory kicks in from the next session.

### Path B — Quick Start with `pip`

If you'd rather skip the AI implementation step and use the reference build directly:

#### 1. Install

The simplest install — straight from PyPI (no clone needed):

```bash
pip install n3memorycore-free
```

Or, if you want a local checkout to read / modify / contribute:

```bash
git clone https://github.com/NeuralNexusNote/n3mc-free.git
cd n3mc-free
pip install -e .          # editable install
```

Requirements: Python 3.10+. The first server start will download the embedding model (`intfloat/multilingual-e5-base`, ~470 MB) — expect a 2–10 minute one-time delay.

#### 2. Connect to Claude Code

```bash
n3mc --init
```

This single command does **everything** needed to wire N3MC into Claude Code:

- Creates `~/.n3mc/` (data lives here — DB, config, audit log, PID file).
- Writes `~/.n3mc/config.json` with auto-generated `owner_id` / `local_id`.
- Registers the `UserPromptSubmit` and `Stop` hooks in **your user-global** `~/.claude/settings.json`, pointing to the installed `n3mc-hook` / `n3mc-stop-hook` entry-point scripts. The "user-global" location is essential — it means hooks fire from **any** project directory, not just this repo.

The command is idempotent: re-running it is safe and won't create duplicates.

#### 3. Verify the hook registration

Run this one-liner — it should print `OK`:

```bash
python -c "import json,os; s=json.load(open(os.path.expanduser('~/.claude/settings.json'))); h=s.get('hooks',{}); assert any('n3mc' in c.get('command','').lower() for e in h.get('UserPromptSubmit',[]) for c in e.get('hooks',[])), 'UserPromptSubmit hook missing'; assert any('n3mc' in c.get('command','').lower() for e in h.get('Stop',[]) for c in e.get('hooks',[])), 'Stop hook missing'; print('OK')"
```

If you see `OK`, the hooks are registered correctly. If you see an `AssertionError`, re-run `n3mc --init`.

#### 4. Restart Claude Code

Close **every** running Claude Code session (terminal CLI windows, IDE extensions like VS Code's "Claude Code" panel, etc.) and start a fresh one. Hooks are read at session startup; sessions started before `n3mc --init` will not pick up the new hooks.

#### 5. Confirm it's working

In a fresh Claude Code session, send any message. After Claude responds, run:

```bash
n3mc --list
```

You should see at least two records — one with a `[user]` prefix (your message) and one with a `[claude]` prefix (Claude's response). If the list is empty, jump to **Troubleshooting** below.

#### Optional: change where data lives

Override the data location by setting `N3MC_HOME=/path/to/dir` before running `n3mc --init` (the chosen path is then permanent for that install).

---

### Troubleshooting (Path B)

**`n3mc: command not found` after `pip install`**
Your Python `Scripts/` (Windows) or `bin/` (macOS/Linux) directory is not on `PATH`. Either add it to `PATH`, or invoke as `python -m n3memorycore.n3memory --init` instead.

**`--list` shows zero records after a real conversation**
You opened the Claude Code session **before** running `n3mc --init`. Close it completely and open a new session — hooks are loaded at session start.

**Hooks register, but nothing saves**
Check the audit log:
```bash
cat ~/.n3mc/.memory/audit.log | tail -3
```
Each user prompt should produce one JSON line. If the file is empty, the hooks aren't firing — confirm Step 3's verification command prints `OK`, and confirm `~/.claude/settings.json` (user-global, not project-local) was the file modified.

**`n3mc --search` returns nothing the first time, but works after**
The `intfloat/multilingual-e5-base` model is downloading (~470 MB) on the first call. Wait 2–10 minutes for the download to finish, then retry.

### What to expect after setup

Once setup is complete, everything works automatically from the next session:
- Past conversations relevant to your current message are retrieved and injected automatically
- Claude responds with full context ("Regarding what we discussed last time...")
- Saving and searching happen in the background — no action needed on your part

### Backup and restore

To migrate memories to another environment or keep a safe backup, save these 2 files
from `~/.n3mc/`:
- `.memory/n3memory.db` — the memory database
- `config.json` — contains `owner_id` and `local_id` UUIDv4 keys

These 2 files must be kept together. If the keys don't match, owner verification will fail.

### Uninstall

```bash
pip uninstall n3memorycore-free
rm -rf ~/.n3mc/                                      # delete data (irreversible)
# Then manually remove the hook entries from ~/.claude/settings.json.
```

Or, if you set up via Path A, ask Claude Code: "Please uninstall N3MemoryCore."

## ID Hierarchy

N3MemoryCore uses 5 ID fields to identify the origin and context of each record:

| ID | Stored in | Generated | Granularity | Purpose |
|---|---|---|---|---|
| `id` (PK) | DB record | Per record (UUIDv7, time-ordered) | **One record** | Unique identifier for each memory — used for deletion and dedup |
| `owner_id` | `config.json` | First startup (UUIDv4) | **Owner / N3MC server** | Identifies whose data this is — for shared/multi-user scenarios and import provenance |
| `session_id` | In-memory or supplied by host | Per task / project / conversation (UUIDv4) | **Task / project / conversation** | Groups memories that belong to one task / project / conversation. In Free, drives the `b_session` ranking bias (match=1.0, mismatch=0.6) so the ongoing exchange ranks above unrelated past sessions |
| `local_id` (agent_id) | `config.json` / API | First startup (UUIDv4), or per request | **Agent / install** | UUIDv4 identifier for the speaking agent. Each agent gets its own UUID in multi-agent setups (in Free, stored but not used in ranking; `b_local` is a Pro feature) |
| `agent_name` | DB record | Per buffer call (free-form string) | **Agent display name** | Human-readable label for the agent (e.g. `"claude-code"`) |

```
owner_id  (one N3MC server / data owner)
  └── session_id  (one task / project / conversation)
        └── local_id  (the speaking agent within that session)
              ├── agent_name  (its display name: "claude-code", etc.)
              └── id  (one memory record)
```

## Requirements

- **Claude Code** (required)
- **Python 3.10+**
- **OS**: Windows 11 (tested) / macOS and Linux (designed for, but not yet verified)

> To check if Python is installed, run `python --version` in your terminal.
> If missing, Claude Code will guide you during setup.

## Extensibility

N3MemoryCore is built from a specification, so extending it is as simple as asking Claude Code.

- **MCP server** — Expose N3MC as an MCP server so other AI tools can access the memory
- **PostgreSQL migration** — Move from SQLite to PostgreSQL + pgvector for team-scale deployments
- **Custom search logic** — Tune bias coefficients and ranking formulas
- **Alternative embedding models** — Swap in a different model to suit your use case
- **Custom ID fields** — Add fields like `project_id` or `tag` to organize memories your way
- **PDF import** — Extract text from PDF files and store them as searchable memories
- **Hashtag support** — Claude auto-tags memories with `#hashtags` for filtered search (e.g. `--search "#AWS"`) — works with existing FTS, no DB changes needed

Just load the specification into Claude Code and say what you want to add.

## ⚠️ Disclaimer

- **No support, no claims**: This project is provided as-is with no support. Questions, bug reports, and feature requests are not guaranteed a response. Use entirely at your own discretion.
- **Use at your own risk**: This system runs on AI-generated code. The author is not responsible for any data changes or issues caused by its use.
- **Backup first**: Always back up your current environment before running this on important projects.

---

Apache License 2.0 — Copyright (C) 2026 NeuralNexusNote™ / ArnolfJp019
See [LICENSE](./LICENSE) for details.
