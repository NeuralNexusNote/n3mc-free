<!-- n3mc-behavior v2 -->
# N3MemoryCore Behavioral Guidelines

These guidelines apply to every Claude Code session in this project. They are
loaded automatically from `.claude/rules/` at session start.

## Fully Automatic Saving
All conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call
`--buffer` manually. Every non-empty user message and Claude response is
recorded character-for-character; there is no length filter and no skip-pattern
filter. Make NO acknowledgement when a save succeeds — silence is correct.

## Recalled Memory Context
Every turn, the UserPromptSubmit hook adds a `# Recalled Memory Context` block
to your context. Read `## Top matches` first; use
`## Previous matching Q-A exchanges` as supporting context. When several
versions of the same content appear, the one with the newest `saved` time is
the current one. If the block contains a `> ⚠️ N3MC notice:` line, tell the
user what it says.

## Active RAG
When prior context would help, run a search proactively. **Always invoke via
`python -m n3memorycore.n3memory --search "<keywords>"`**, NOT the bare `n3mc`
console script. Claude Code spawns a bash subprocess whose `PATH` does not
reliably include the Python `Scripts/` directory where the `n3mc` entry-point
lives, so `n3mc --search ...` works in interactive shells but fails in
production with `command not found`. `python -m` resolves through the Python
interpreter (which is on `PATH`) and is portable across venvs and platforms.
Both invocation forms are auto-allowed via `permissions.allow`.

## Cut Exchanges
A Q-A exchange may end with a line like
`_(N more chunk(s) of this exchange omitted. Full text: ...)_`. If the omitted
part is needed for your answer, run the command shown there
(`python -m n3memorycore.n3memory --show-turn <turn_id>`) and read the full
text before answering.

## Recall Acknowledgment
When recalled memory actually shapes your reply (you are recalling something
saved earlier), open the reply with a short acknowledgment in the user's
language — e.g. "Pulling this from earlier memory in this session." or
「前回の回答がメモリに保存されています。」 If no relevant memory was found,
or it did not influence your answer, do NOT announce anything.

## Fatal-Failure Warning
If a save fails physically, surface the warning prominently:
> ⚠️ Physical save failed. Current memories may be lost.
