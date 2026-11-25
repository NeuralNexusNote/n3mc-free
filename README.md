# N3MemoryCore Free (n3mc-free) v1.9.4

**Give Claude Code a memory that does not fade — free, on your own PC.**

Claude Code forgets what you were working on when you close the conversation.
N3MemoryCore saves every message and reply in a file on your own PC, and brings
the related ones back the next time you talk about the same thing. You do not
have to say "remember this", and Claude does not have to call a search tool:
the hooks do both on every turn.
Of the AI's memory, N3MC covers the **conversation history** layer (what was said, when, and in which conversation). It does not cover the rules that are always loaded, such as CLAUDE.md, or connections to external tools.

1. In File Explorer, create an empty folder
2. Put the specification `N3MemoryCore_v1.9.4_Free_EN.md` into it (put nothing else in it)
3. In the Claude desktop app, open the **Code** tab, start a new session with **+ New**, choose **Local**, then **Open folder…** and pick that folder (in a terminal, change to that folder and run `claude`)
4. Send: "Please implement N3MemoryCore according to the specification `N3MemoryCore_v1.9.4_Free_EN.md`."
5. When it is done, restart Claude Code (closing the window may not be enough; see [Restart Claude Code](#4-restart-claude-code))

After the restart, open the same session and send "I restarted". Claude Code checks that memory works and tells you what to do next.

Memory works from the next session. The first run
downloads the search model (about 1.1 GB) once. Details: [Setup](#setup).
To install from PyPI instead (bonus), see [Method B: Install the package (bonus)](#method-b-install-the-package-bonus).

> **⚠️ Disclaimer**
>
> - **No support, no claims**: This project is provided as-is with no support. Questions, bug reports, and feature requests are not guaranteed a response. Use entirely at your own discretion.
> - **Use at your own risk**: This system runs on AI-generated code. The author is not responsible for any data changes or issues caused by its use.
> - **Backup first**: Always back up your current environment before running this on important projects.

> **⚠️ How long memories are kept, and data size**
>
> - **Memories are not deleted.** Free has no feature that deletes old records.
> - **More data makes it slower.** The more records there are, the longer a search takes.

> A NeuralNexusNote™ product
>
> 🇯🇵 **[Click here for the Japanese Documentation (日本語版)](./README_JP.md)**
> 🛡️ **[Development Philosophy & AI-Native Policy](./PHILOSOPHY.md)**

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

- **What is saved** — every message you send and every reply of Claude, as written. A fenced code block is saved as `[code omitted]`: N3MC remembers what was said and decided, and the code is in your project's files. Input that Claude Code itself injects (task notifications, agent messages, system reminders) is not saved.
- **What is recalled** — with every message, the hook searches the memory and adds the related records to Claude's context, before Claude reads your message.

### Three scenes

> **Monday** — You and Claude Code decide to use PostgreSQL for the website, and say why.
>
> **Wednesday** — In a new session you ask which database you chose. Monday's decision and its reason arrive in Claude's context together with your question. You do not explain it again.
>
> **Friday** — You are working in another folder. When the subject comes up, Monday's decision is recalled there too: Free keeps one memory for all your project folders.

### What Claude receives

This is the block the hook added for the Wednesday question. It was captured from a real run of the hooks (a temporary data folder; the second session was started in a different folder from the first).

```text
# Recalled Memory Context
Search query: Which database did we decide to use for the website, and why?

## Top matches (use these to answer the question)

_If several versions of the same content appear, the one with the newest `saved` time is the current one._

## [1] score=0.5283 saved=2026-11-22 15:58 (UTC+0900) claude-code
[user] Let's use PostgreSQL for the website's database. Several people will edit at the same time, and SQLite locks the whole file on every write.

## Possibly related (weak matches — may be unrelated)

## [2] score=0.3339 saved=2026-11-22 15:58 (UTC+0900) claude-code
[claude] Agreed, PostgreSQL it is. The reason, for the record: several editors at the same time, and SQLite allows one writer at a time. Next step: write the schema for the articles table.

## Previous matching Q-A exchanges (supplementary context)

### turn_id: 9df50a6f-8a92-4401-b096-9e3daec22566 saved=2026-11-22 15:58 (UTC+0900)
[user] Let's use PostgreSQL for the website's database. Several people will edit at the same time, and SQLite locks the whole file on every write.
[claude] Agreed, PostgreSQL it is. The reason, for the record: several editors at the same time, and SQLite allows one writer at a time. Next step: write the schema for the articles table.
```

`Top matches` are the records that passed the relevance floors. `Possibly related` are the closest of the records that did not. The last part shows the whole exchange a top match belongs to.

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

## About this project

🛡️ AI-Native Development Policy
This project prioritizes Instructions over Static Code. Please read our [Development Philosophy](./PHILOSOPHY.md) to understand why you should let AI generate your execution environment.

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

**Two ways to install**: give the specification to Claude Code and let it build the system (primary), or `pip install` the reference build and run `n3mc --init` (bonus). Either way, no manual hook editing. (See [Setup](#setup) below.)

## Free or Lite? Which one to use

There are two free N3MemoryCore builds. **If this is your first time, start with Free (this repository).**

| | **N3MC Free** (this repository) | **[N3MC MCP Lite](https://github.com/NeuralNexusNote/n3mcmcp-lite)** |
|---|---|---|
| Works with | Claude Code only | Claude Code (hooks), and the Claude desktop app and other MCP clients (MCP) |
| What gets saved | Every message and reply, automatically (hooks) | In Claude Code, every message and reply (hooks); in other applications, what the AI decides to save (`save_memory`) |
| How long it is kept | No expiry | 7 days, then deleted automatically |
| What you need | Python 3.10+ | Python 3.10+ and Docker (Redis Stack) |
| Setup | `pip install n3memorycore-free`, then `n3mc --init` | Start Redis Stack, then `pip install n3memorycore-mcp-lite` and `n3mc-lite --init` (or the Claude Code plugin); other applications register the MCP entry |
| Good for | Beginners; long-term memory for Claude Code | Users who can run Docker; short-lived working memory for a project, shared with the Claude desktop app |

- **Choose Free** if you use Claude Code and want it to remember your conversations without extra software.
- **Choose Lite** if you also use the Claude desktop app or another MCP client (Cursor, …), or you want memory that forgets by itself after a short project, and you are comfortable starting Docker.

**Free is not the right tool if:**

- you use an application other than Claude Code — Free works through Claude Code's hooks only (see Lite above);
- you want old records removed for you, or want to delete single records — Free has no delete command (see [Free vs Pro](#free-vs-pro));
- you want the same memory on two PCs — Free keeps one file on one PC and does not synchronize it.

---

## Features

| You want to… | N3MemoryCore… | How |
|---|---|---|
| Stop explaining the same thing again | saves every message and reply, and recalls the related ones on every turn | Claude Code hooks (`UserPromptSubmit`, `Stop`). No tool call, no manual step |
| Find things in your own words | finds a past conversation even when the exact words differ | Vector search with `intfloat/multilingual-e5-base` (Japanese, English, Chinese, Korean and ~100 other languages) plus keyword search |
| Pick up tomorrow where you left off | keeps the memory across sessions and across project folders | One database for every folder; the conversation you are in is ranked first |
| Keep your data at home | stores the memory in a file on your own PC, not in any cloud service | SQLite. Only the memories recalled for a turn are sent to Claude, together with your message ([details](#your-memory-stays-on-your-pc)) |
| Spend fewer tokens | does the search with local embeddings (zero Claude tokens) and injects only the related records | The recalled block is capped (`top_matches_max`, `min_results`) |
| Let several agents share one memory | keeps who wrote each record | `agent_name`, `local_id`. Ranking each agent's own memories first is a Pro feature |
| Move up later | builds a database that Pro can take over | Databases built from the same specification are compatible; Pro reads Free's with `n3mc-pro --migrate-from-free` |

To use another embedding model, set it in `config.json` and run `n3mc --reembed`; the model must produce 768-dimension vectors.

## Your Memory Stays on Your PC

An AI that remembers you is convenient, but it is natural to ask where those memories are kept. N3MemoryCore keeps them in one place you control: a folder on your own PC.

- **One local file** — Every memory is stored in a single SQLite file, `~/.n3mc/.memory/n3memory.db`. There is no N3MC account and no N3MC cloud service. To move it, set `N3MC_HOME` (see [Optional: change where data lives](#optional-change-where-data-lives)).
- **The raw input is kept next to it** — `~/.n3mc/.memory/audit.log` holds the raw text of every hook call (your messages and Claude's replies, code blocks included). It is rotated at 50 MB, and `n3mc --list` does not show it. Treat it as private data, like the database.
- **Search runs on your PC** — The embedding model and the search run locally. N3MC's server listens only on `127.0.0.1` and rejects requests for any other host name, so other machines on your network cannot reach it.
- **No telemetry** — N3MC sends no usage data anywhere. The code is open, so you can check this yourself.
- **You can see and remove everything** — `n3mc --list` shows what is stored. Deleting `~/.n3mc/` removes all of it (see [Uninstall](#uninstall)). Backing up is copying two files (see [Backup and restore](#backup-and-restore)).

What does go over the network:

- **The embedding model download** — On the first search, `intfloat/multilingual-e5-base` (about 1.1 GB) is downloaded from Hugging Face. After that, the Hugging Face library may still check for model updates when the model loads; set `HF_HUB_OFFLINE=1` to stop this once the model is downloaded.
- **What Claude reads** — The memories recalled for a turn are added to that turn's prompt, so they are sent to Anthropic together with your message, as everything you type into Claude Code is. N3MC does not upload your memory database anywhere, but the parts Claude uses in a turn are part of that conversation.

## Free vs Pro

| | Free | Pro |
|---|---|---|
| Core features (save, search, automatic recall) | ✅ | ✅ |
| Exact duplicates skipped; the newest version of the same content listed first | ✅ | ✅ |
| The conversation you are in ranked first (`b_session`) | ✅ | ✅ |
| The current project's memories recalled first | - | ✅ |
| Search that stays fast as the memory grows | - | ✅ Above 20,000 records an in-memory index narrows the candidates |
| The previous session's thread at session start | - | ✅ |
| Your own agent's / install's memories ranked first (`b_local`) | - | ✅ |
| Delete individual records | - | ✅ |
| Retention period (`--gc`) | - | ✅ Off by default; you run it yourself |
| Export / import | - | ✅ Data migration & sharing |
| Entity index | - | ✅ |

Nothing is deleted or replaced automatically in either edition.

**Start with Free.** You can upgrade to Pro later using the Upgrade specification.

## Specification Files

| File | Description |
|---|---|
| `N3MemoryCore_v1.9.4_Free_JP.md` | Free edition (Japanese) |
| `N3MemoryCore_v1.9.4_Free_EN.md` | Free edition (English) |

> 💡 **A note on annotations in the specification**
> Throughout the specification file (Markdown), you'll find annotations describing the design intent behind each instruction — written to give you precise control over AI behavior. Before having Claude generate the code, take a moment to read through them. They contain more than just code: the logic behind how to work with AI effectively.

## Setup

There are two ways to set it up. Method A, generating the code from the specification, is the primary one; Method B, installing the package, is a bonus. (The names are the same as in the specification.)

### Method A: Generate from the spec (primary, recommended)

Let Claude Code generate the code from the specification:

1. **Make a folder**: in File Explorer, create one empty folder (for example `C:\Users\<you>\n3mc-free`). The code is generated in this folder and stays in use after setup; do not delete or move it later.
2. **Put the specification in it**: copy the downloaded specification `N3MemoryCore_v1.9.4_Free_EN.md` into that folder (put nothing else in it).
3. **Open Claude Code in that folder**: in the Claude desktop app, open the **Code** tab at the top of the sidebar and start a new session with **+ New**. In the row under the prompt box, choose **Local** as the environment, then click the folder control beside it, choose **Open folder…** and pick the folder from step 1. (In a terminal, change to that folder and run `claude`.)
4. **Ask**: in the prompt box, send "Please implement N3MemoryCore according to the specification `N3MemoryCore_v1.9.4_Free_EN.md`."

Claude Code shows the folder and asks you once to confirm it; then it writes the code, configures the hooks, and sets everything up. It stops instead if the folder holds other files, so that a project of yours is not overwritten.
Setup takes roughly 15–30 minutes (varies by environment and model).
Memory kicks in from the next session. The generated code stays in that folder: deleting or moving it later stops the memory.

---

### Method B: Install the package (bonus)

Use the reference build directly:

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

With the editable install, the hooks run the code from that folder: deleting or moving it later stops the memory.

Requirements: Python 3.10+. The first server start will download the embedding model (`intfloat/multilingual-e5-base`, about 1.1 GB) — expect a one-time delay of several minutes, depending on your connection.

#### 2. Connect to Claude Code

```bash
n3mc --init
```

This single command does **everything** needed to wire N3MC into Claude Code:

- Creates `~/.n3mc/` (data lives here — DB, config, audit log, PID file).
- Writes `~/.n3mc/config.json` with auto-generated `owner_id` / `local_id`.
- Registers the `UserPromptSubmit` and `Stop` hooks in **your user-global** `~/.claude/settings.json`, pointing to the absolute paths of the installed `n3mc-hook` / `n3mc-stop-hook` entry-point scripts, each with a 120-second time limit. The "user-global" location is essential — it means hooks fire from **any** project directory, not just this repo.
- Adds the permission rules for the memory commands (`python -m n3memorycore.n3memory --search …`, `--show-turn`, …) to `permissions.allow` in the same file, so Claude can search memory in any project without a confirmation prompt. Existing rules are never removed.
- Leaves hooks of other programs alone — including another N3MC edition (e.g. Pro) registered on the same PC; `--init` prints a line when it finds one.
- Backs up `settings.json` (`settings.json.bak-n3mc-<date-time>`) before changing it. If the file is not valid JSON, it changes nothing and tells you.

The command is idempotent: re-running it is safe and won't create duplicates. If you registered the hooks with an earlier version, run it once more: it adds the time limit to the existing entries.

#### 3. Verify the hook registration

Run this one-liner — it should print `OK`:

```bash
python -c "import json,os; s=json.load(open(os.path.expanduser('~/.claude/settings.json'), encoding='utf-8')); h=s.get('hooks',{}); assert any('n3memorycore.n3mc_hook' in c.get('command','').lower() or 'n3mc-hook' in c.get('command','').lower() for e in h.get('UserPromptSubmit',[]) for c in e.get('hooks',[])), 'UserPromptSubmit hook missing'; assert any('n3memorycore.n3mc_stop_hook' in c.get('command','').lower() or 'n3mc-stop-hook' in c.get('command','').lower() for e in h.get('Stop',[]) for c in e.get('hooks',[])), 'Stop hook missing'; assert 'Bash(python -m n3memorycore.n3memory --search *)' in s.get('permissions',{}).get('allow',[]), 'permission rules missing'; print('OK')"
```

If you see `OK`, the hooks and permission rules are registered correctly. If you see an `AssertionError`, re-run `n3mc --init`.

#### 4. Restart Claude Code

Close **every** running Claude Code session (terminal CLI windows, IDE extensions like VS Code's "Claude Code" panel, etc.) and start a fresh one. Hooks are read at session startup; sessions started before `n3mc --init` will not pick up the new hooks.

Closing the window is not always enough: the app can keep running in the background. On Windows, open the "^" at the right end of the taskbar (near the clock); if a Claude icon is there, right-click it and quit. If unsure, sign out of Windows once and sign in again (Start menu → your account icon at the bottom left → **Sign out**), or restart the PC.

#### 5. Confirm it's working

In a fresh Claude Code session, send any message. After Claude responds, run:

```bash
n3mc --list
```

You should see at least two records — one with a `[user]` prefix (your message) and one with a `[claude]` prefix (Claude's response). If the list is empty, jump to **Troubleshooting** below.

#### Optional: change where data lives

Set `N3MC_HOME=/path/to/dir` to keep the data somewhere else. N3MC reads the variable every time it runs and does not store it, so set it **permanently**, where both Claude Code (which starts the hooks) and your terminal see it — for example as a user environment variable — before running `n3mc --init`. If it is set only in the terminal where you ran `--init`, the hooks use `~/.n3mc`.

---

### Troubleshooting (Method B)

**`n3mc: command not found` after `pip install`**
Your Python `Scripts/` (Windows) or `bin/` (Linux) directory is not on `PATH`. Either add it to `PATH`, or invoke as `python -m n3memorycore.n3memory --init` instead.

**`--list` shows zero records after a real conversation**
You opened the Claude Code session **before** running `n3mc --init`. Close it completely and open a new session — hooks are loaded at session start.

**Hooks register, but nothing saves**
Check the audit log:
```bash
cat ~/.n3mc/.memory/audit.log | tail -3
```
Each user prompt should produce one JSON line. If the file is empty, the hooks aren't firing — confirm Step 3's verification command prints `OK`, and confirm `~/.claude/settings.json` (user-global, not project-local) was the file modified.

**`n3mc --search` returns nothing the first time, but works after**
The `intfloat/multilingual-e5-base` model is downloading (about 1.1 GB) on the first call. Wait for the download to finish, then retry.

### What to expect after setup

Once setup is complete, everything works automatically from the next session:
- Past conversations relevant to your current message are retrieved and injected automatically
- Claude responds with full context ("Regarding what we discussed last time...")
- Saving and searching happen in the background — no action needed on your part
- The Stop hook writes a small rules file, `.claude/rules/n3mc-behavior.md`, into each project folder you work in. It tells Claude how to read the recalled block; a file you edited by hand is not replaced

### Search settings

How memories are recalled can be tuned in `~/.n3mc/config.json`. The main settings and what they do are described in the specification.

### Backup and restore

To migrate memories to another environment or keep a safe backup, save these 2 files
from `~/.n3mc/`:
- `.memory/n3memory.db` — the memory database
- `config.json` — contains `owner_id` and `local_id` UUIDv4 keys

Keep these 2 files together: `config.json` holds the IDs that are stamped on the records in the database.

### Uninstall

If you set up via Method A, ask Claude Code: "Please delete N3MemoryCore." — it also removes the hook configuration.

To do it yourself:

1. Remove the two hook entries (`n3mc-hook`, `n3mc-stop-hook`) and the permission rules that name `n3memorycore.n3memory` or `n3mc` from `~/.claude/settings.json`, and restart Claude Code.
2. Stop the memory server, which keeps running in the background: end the process whose command line contains `n3memorycore.n3memory --run-server` (its PID is in `~/.n3mc/.memory/n3mc.pid`), or restart the PC.
3. Remove the package and the data:

```bash
pip uninstall n3memorycore-free
rm -rf ~/.n3mc/                                      # delete data (irreversible)
```

4. `.claude/rules/n3mc-behavior.md` in your project folders, and the `settings.json.bak-n3mc-*` backups next to `settings.json`, can be deleted by hand.

## ID Hierarchy

N3MemoryCore uses 6 ID fields to identify the origin and context of each record:

| ID | Stored in | Generated | Granularity | Purpose |
|---|---|---|---|---|
| `id` (PK) | DB record | Per record (UUIDv7, time-ordered) | **One record** | Unique identifier for each memory |
| `owner_id` | `config.json` | First startup (UUIDv4) | **Owner / data folder** | Identifies whose data this is — for shared/multi-user scenarios and import provenance |
| `local_id` | `config.json` / API | First startup (UUIDv4), or per request | **Install** | One Claude Code install has one `local_id`; it is the same in every session. Stored on every record (in Free it is not used in ranking; `b_local` is a Pro feature) |
| `session_id` | Supplied by Claude Code (hook input), else made per process | Per conversation | **One conversation** | Groups the memories of one conversation. In Free, drives the `b_session` ranking bias (match=1.0, mismatch=0.6) so the ongoing exchange ranks above unrelated past sessions |
| `turn_id` | DB record | When your message is saved (UUIDv4) | **One exchange** | Ties one user message to the reply that answers it; `--show-turn` and `--recall-thread` take it |
| `agent_name` | DB record | Per save (free-form string) | **Display label** | Human-readable label of the writer (`"claude-code"` for the hooks). `--buffer --agent-id <name>` sets this field |

```
owner_id  (the data folder ~/.n3mc and its owner)
  └── local_id  (the install; the same in every session)
        └── session_id  (one Claude Code conversation)
              └── turn_id  (one exchange: one user message and the reply to it)
                    └── id  (one record = one chunk)

agent_name  (a display label, outside the hierarchy: "claude-code" for the hooks)
```

## Requirements

- **Claude Code** (required)
- **Python 3.10+**
- **OS**: Windows 11 and Ubuntu (tested). macOS is not supported.

> To check if Python is installed, run `python --version` in your terminal.
> If missing, Claude Code will guide you during setup.

## Extensibility

N3MemoryCore is built from a specification, so extending it is as simple as asking Claude Code.

- **MCP server** — Expose N3MC as an MCP server so other AI tools can access the memory
- **PostgreSQL migration** — Move from SQLite to PostgreSQL + pgvector (a ready-made edition for a team in one organization is N3MC Pro on PostgreSQL, `n3mc-pro-postgres`)
- **Custom search logic** — Tune bias coefficients and ranking formulas
- **Alternative embedding models** — Swap in a different model to suit your use case
- **Keyword-search tokenizer** — The default keyword index (FTS5 `trigram`, plus a 2-character index for Japanese, Chinese and Korean) was chosen because it works both for languages written without spaces and for languages like English (the PyPI package is used in every country). Adapt the final tokenizer to your own language and environment (e.g. `porter unicode61` to fold English word forms, or a morphological analyzer for higher Japanese precision). Claude Code can do it for you, including the start-up check that otherwise rebuilds the index as `trigram`, and rebuilding the existing DB's FTS index
- **Custom ID fields** — Add fields like `project_id` or `tag` to organize memories your way
- **PDF import** — Extract text from PDF files and store them as searchable memories
- **Hashtag support** — Claude auto-tags memories with `#hashtags` for filtered search (e.g. `--search "#AWS"`) — works with existing FTS, no DB changes needed

Just load the specification into Claude Code and say what you want to add.

---

Apache License 2.0 — Copyright (C) 2026 NeuralNexusNote™ / ArnolfJp019
See [LICENSE](./LICENSE) for details.
