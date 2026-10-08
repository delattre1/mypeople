---
name: boss-manager
description: Boss-only delegation, routing, owner administration, and team-control workflow.
metadata:
  version: 1.0.0
  load: startup
  required: true
---

# Boss manager workflow

Translate CEO intent into a complete delegation contract, enforce the plan gate, assign exactly one
new owner to each open card, and route every later comment to that same owner. Use the controlled
`/todo/owner` API for assignment/replacement/reopen events and supported `mp` operations for agent
control. Inspect pane truth and independent verification before reporting completion. Retire an owner
only after the CEO closes the card. The existing Boss doctrine remains authoritative when this skill
is more concise.
