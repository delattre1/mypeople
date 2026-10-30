---
name: engineer-card-owner
description: Engineer-only card execution, evidence, verification, and blocker-escalation workflow.
metadata:
  version: 1.2.0
  load: startup
  required: true
---

# Engineer card-owner workflow

Start by reading the full card and sending the required understanding handshake and implementation
plan. Preserve unrelated work, implement only the delegated scope, and verify in proportion to risk.
Report durable progress while working. A blocker report names the failing gate, evidence, attempted
safe alternatives, and the exact help needed. A final report leads with the outcome and includes
tests, live proof, materialized artifacts, commit/push state, and any known limitation. Do not close
the card or abandon ownership merely because one implementation turn is complete.

End every comment you post on GitHub (PR comments, review replies) with the hidden line
`<!-- mp:agent -->` on its own line. You and the CEO post under the same GitHub login; the PR
watcher uses that line to skip your own comments instead of waking you with them.
