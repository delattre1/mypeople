---
name: mypeople-memory
description: Recall what the team already did by loading the whole board into a Python variable and searching it with code (RLM context-as-variable), instead of asking around.
metadata:
  version: 1.0.0
  load: startup
  required: true
---

# Team memory — recall by running code over the board

You are one of many agents on this team. Agents are spawned and retired constantly, so a fresh agent
(you) often does not know what the team already did. The board — every card and its comments — IS
that history. This skill is how you read it: **load the whole board into a Python variable and find
what you need by writing code over it.** No database, no vector search, no asking around.

## When to use
When a task might overlap prior work, or the CEO/Boss references something ("the librarian", "the
Second Brain", "that card about X"), consult team memory FIRST, before acting or asking.

## How to recall

1. **Refresh the corpus** (it is regenerated from the live board on demand — always refresh so you
   read current state):

   ```
   python3 "$INSTALL_DIR/bin/memory-dump.py"
   ```

   `$INSTALL_DIR` is this install's root (default `~/.local/share/mypeople`). The command prints the
   corpus path — by default `$INSTALL_DIR/memory/board-corpus.txt`.

2. **Load it into a variable in a PERSISTENT Python session** (`python3 -i`, a REPL, or a notebook —
   NOT a script you re-run from scratch, or you re-read the whole file every time and lose your
   intermediate findings):

   ```python
   context = open("<corpus path from step 1>").read()   # the entire board, in one string
   len(context)
   ```

3. **Search by writing just-in-time code over `context`** — `str.find` / slicing / `re` /
   list-comprehensions — and iterate: run a query, look at what came back, refine, repeat until you
   are sure. Keep intermediate results in variables (buffers) and build your answer up from them.
   You have a brain: once code narrows the candidates down to what fits your window, read them and
   judge the semantics yourself.

## Rules

- **NEVER print the whole corpus** (it is multi-megabyte and will flood your context). Print counts,
  a few matches, short slices, or `head`/`tail` of a result — never the raw variable.
- **Iterate a few times, then stop.** A handful of refined code passes is enough; don't loop forever.
- **Persistent session over re-run scripts** — reuse the loaded `context` and your buffers across
  passes.

## Future optimization (not needed now)
If a question ever requires semantically READING more text than fits your window (e.g. genuinely
reading the full text of hundreds of cards, not keyword-filtering), you can fan out `llm_query`-style
sub-calls — disposable agents that read chunks and hand back short summaries, so the reading volume
never fills your own context. At the current board size this is unnecessary: narrow by code, then
read the survivors yourself.
