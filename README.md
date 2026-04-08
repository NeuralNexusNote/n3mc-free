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
**Setup is handled entirely by Claude Code. No coding required.**

---

## Features

- 💾 **Fully local** — Your conversations stay on your machine. Nothing sent to the cloud.
- 🔍 **Semantic search** — Finds relevant past conversations even when the exact words differ.
- 🔄 **Persistent across sessions** — Pick up tomorrow where you left off today.
- ⚡ **Works automatically** — Saving and searching happen automatically. No manual steps needed.
- 🤖 **Multi-agent ready** — Multiple AI agents share one memory DB. Each agent prioritizes its own memories while accessing the team's collective knowledge.
- 🏢 **Team & organization support** — Deploy the server on your network and share memories across your entire team.

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
| `N3MemoryCore_v1.0.0_Free_JP.md` | Free edition (Japanese) |
| `N3MemoryCore_v1.0.0_Free_EN.md` | Free edition (English) |

Pro edition specification files are available in the [n3mc-pro repository](https://github.com/NeuralNexusNote/n3mc-pro).

> 💡 **A note on annotations in the specification**
> Throughout the specification file (Markdown), you'll find annotations describing the design intent behind each instruction — written to give you precise control over AI behavior. Before having Claude generate the code, take a moment to read through them. They contain more than just code: the logic behind how to work with AI effectively.

## Setup

1. Pick a specification file from the table above
2. Drop the file into the Claude Code prompt
3. Say: "Please implement this."

That's it. Claude Code writes the code, configures the hooks, and sets everything up.
Setup takes roughly 15–30 minutes (varies by environment and model).
Memory kicks in from the next session.

### What to expect after setup

Once setup is complete, everything works automatically from the next session:
- Past conversations relevant to your current message are retrieved and injected automatically
- Claude responds with full context ("Regarding what we discussed last time...")
- Saving and searching happen in the background — no action needed on your part

### Backup and restore

To migrate memories to another environment or keep a safe backup, save these 2 files:
- `n3memory.db` — the memory database
- `config.json` — contains `owner_id` and `local_id` UUIDv4 keys

These 2 files must be kept together. If the keys don't match, owner verification will fail.

### Uninstall

Do not delete the folder directly. Instead, ask Claude Code: "Please uninstall N3MemoryCore." It will safely remove hooks and clean up the configuration.

## ID Hierarchy

N3MemoryCore uses 5 ID fields to identify the origin and context of each record:

| ID | Stored in | Generated | Granularity | Purpose |
|---|---|---|---|---|
| `id` (PK) | DB record | Per record (UUIDv7, time-ordered) | **One record** | Unique identifier for each memory — used for deletion and dedup |
| `owner_id` | `config.json` | First startup (UUIDv4) | **Owner** | Identifies whose data this is — for shared/multi-user scenarios |
| `local_id` | `config.json` | First startup (UUIDv4) | **Agent / install** | UUIDv4 identifier for the agent. Each agent gets its own UUID in multi-agent setups |
| `session_id` | In-memory | Per server startup (UUIDv4) | **Server process** | Identifies which server session (stored for compatibility; not used in Free edition ranking) |
| `agent_id` | DB record | Per buffer call (free-form string) | **Agent display name** | Human-readable label for `local_id` (e.g. `"claude-code"`) |

```
owner_id  (one user)
  └── local_id  (agent's UUIDv4 identifier)
        ├── agent_id  (its display name: "claude-code", etc.)
        └── session_id  (one server startup)
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

Just load the specification into Claude Code and say what you want to add.

## ⚠️ Disclaimer

- **No support, no claims**: This project is provided as-is with no support. Questions, bug reports, and feature requests are not guaranteed a response. Use entirely at your own discretion.
- **Use at your own risk**: This system runs on AI-generated code. The author is not responsible for any data changes or issues caused by its use.
- **Backup first**: Always back up your current environment before running this on important projects.

---

Apache License 2.0 — Copyright (C) 2026 NeuralNexusNote™ / ArnolfJp019
See [LICENSE](./LICENSE) for details.
