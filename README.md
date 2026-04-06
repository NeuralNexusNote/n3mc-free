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
| `N3MemoryCore_v1.0.0_Pro_JP_Complete.md` | Pro edition, full (Japanese) |
| `N3MemoryCore_v1.0.0_Pro_EN_Complete.md` | Pro edition, full (English) |
| `N3MemoryCore_v1.0.0_Pro_JP_Upgrade.md` | Free-to-Pro upgrade (Japanese) |
| `N3MemoryCore_v1.0.0_Pro_EN_Upgrade.md` | Free-to-Pro upgrade (English) |

## Setup

1. Pick a specification file from the table above
2. Load it into Claude Code
3. Say: "Please implement this."

That's it. Claude Code writes the code, configures the hooks, and sets everything up.
Memory kicks in from the next session.

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
- **OS**: Windows / macOS / Linux

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

---

Copyright (C) 2026 NeuralNexusNote™ / ArnolfJp019
All names and logos associated with N3MemoryCore and NeuralNexusNote are trademarks of the author.
