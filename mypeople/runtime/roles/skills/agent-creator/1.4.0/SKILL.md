---
name: agent-creator
description: How to author a MyPeople role: the 5 files, the contract, and newrole.py.
metadata:
  version: 1.4.0
  load: startup
  required: true
---

# Creating an agent in MyPeople

Spawning starts an instance. **Creating an agent is authoring the role it runs as** — that is the
part that used to live in `mp send` prompts and died with the pane. A role is a set of versioned
files plus a tag, hash-locked into one digest at spawn time. Boss runs `boss@6.3.2`; engineers ran
`engineer@1.3.3`.

You are the create-agent. Your output is a role that spawns, not prose about one.

## What a role is

| File | What it decides |
|---|---|
| `personalities/<name>/<ver>.md` | what it is, and what it must never do |
| `skills/<skill>/<ver>/SKILL.md` | what it knows at startup (needs YAML frontmatter with `name`) |
| `toolsets/…json` | which `mp` commands it knows |
| `policies/<name>/<ver>.json` | what it is allowed to do — own a card? spawn? mutate? |
| `profiles/<name>/<ver>.json` | ties the four together and declares the **contract** |
| `registry.json` | maps the tag → profile. **This is the allowlist.** |

`mypeople-system` is mandatory in every role; the resolver refuses a profile without it.

## The contract — why this exists

`spec.contract` in the profile is the **answer key**:

```json
"contract": {"probes": 10, "assert": {"type": "exact", "value": "Hello World"}}
```

`type` is `exact`, `one-of`, or `judgment`. The tester scores a reply against *this*, not against
whatever standard it would have invented — that is the whole reason a role must be created before
it can be tested. A role that declares no answer key at all can only be graded by taste, so
`newrole.py` refuses to write one.

**There is no `regex`.** The CEO removed it: a pattern match is a script, and a tester running one
is not judging the reply, it is checking that the letters line up. Use `exact` / `one-of` only when
the correct answer really is a fixed string. Everything else is `judgment`:

```json
"assert": {"type": "judgment", "value": "The reply is a pirate joke in English, on a single line,
and different from every earlier reply in the run. It fails if it answers the question instead of
joking, is not about pirates, is not in English, runs over one line, or repeats."}
```

Write the criteria the way you would tell a person what to look for: what a good reply is, and the
named ways it fails. **Judgment is an opinion, and that is the cost** — the protection is that the
tester must quote the reply and say in a few words why it passed or failed, so a human can
disagree with it. A criterion nobody could disagree about in writing is a criterion you have not
written clearly enough.

**A test run is 10 questions.** That is the house standard, set by the CEO, and it is the default:
a spec that omits `probes` gets 10, so every role you author is measured over the same number of
questions as every other. Do not write a smaller number because a role feels simple — a score is
only comparable across agents if the denominator is. Writing a different number is allowed, but it
is a deviation: put the reason in `contract.notes`, or the next person will read your 6/6 as if it
meant the same thing as somebody else's 10/10. `not-testable` roles are the one exception; they
declare `probes: 0` because there is nothing to ask.

Write the assertion so a **wrong** answer fails it. An assertion that passes on any plausible reply
tests nothing.

## `why` — the only thing the disk cannot reconstruct

Every spec carries `"why"`: one sentence saying **what went wrong that made this version necessary**.
Not "improves the tester" — *"emotional pressure broke the pirate in two runs"*.

The file's timestamp already proves `2.0.0` came after `1.1.0`, and the digest already proves the
bytes changed. Neither will ever tell you what the change was *for*, and that is exactly what
somebody looking back needs. `newrole.py` writes your `why` into the factory's diary
(`~/workspace/testador/HISTORICO.jsonl`) the moment the role is created — you never run the diary
by hand, but if you leave `why` empty the slide for your version says nothing.

## The steps

1. **Write a spec** — `specs/<name>.json`:

```json
{
  "name": "hello-world",
  "version": "1.0.0",
  "tags": ["hello-world", "fixture"],
  "personalityFile": "../personalities/hello-world.md",
  "policy": {"allow": ["reply-to-message"], "deny": ["own-card", "spawn-agent"]},
  "contract": {"probes": 10, "assert": {"type": "exact", "value": "Hello World"}}
}
```

Prose is inline (`personality`) or a path (`personalityFile`) — exactly one. Paths are relative to the spec file. A role-specific skill
is optional: `"skill": {"name": …, "version": …, "bodyFile": …}`. Extra store skills go in
`"extraSkills": ["skills/mypeople-memory/1.0.0/SKILL.md"]`.

2. **Author the personality.** Second person, present tense, short. State the one thing it does and
   the things it must never do. It is the whole system prompt — anything you leave implicit, the
   backend fills in with its own defaults.

3. **Run it:** `python3 newrole.py specs/<name>.json`

   It writes the files, registers the tag, then validates by calling `mprole.resolve_role` — the
   same resolver `mp spawn` runs. On failure it **rolls the registry back** and leaves the files
   unregistered, so a broken role is never spawnable.

4. **Spawn:** `mp spawn <agent_id> --role <name> --temporary`
   (`--owner-task <card>` instead of `--temporary` if it must own a card; `--master` is Boss only.)

5. **Test it** — see `probe.py`. The role isn't done because it spawned; it's done because it
   scored against its own contract.

## Rules

- **Never bump an existing version in place.** A live agent is pinned to its digest and a revive
  fails closed on drift. New behaviour = new version, registry points at the new one.
- **Don't hardcode a version you could read off the disk.** The mandatory-skill ref is discovered,
  not pinned. A stale hardcoded list is exactly what made authored roles unspawnable before.
- **The policy is not decoration.** `deny` is what stops a fixture agent from owning cards or
  spawning children. Write the denies before the allows.
- **A role that needs new Python to be created is a failed role.** The spec is the interface.
- **Author every role in English** (CEO order, 2026-08-09): the personality, the skill body, the
  `contract.assert`, the `why`, the probe questions, and the spawn order you hand the agent. Only the
  CEO writes Portuguese. A role authored in Portuguese answers in Portuguese and then seeds the next
  one, which is how the drift spread in the first place. Two carve-outs: a **verbatim quote** stays in
  its original language with an English gloss next to it, because translating a record falsifies it;
  and a **contract already scored** is left as it ran — translate forward into a new version, never
  retro-edit the answer key of a completed round.
