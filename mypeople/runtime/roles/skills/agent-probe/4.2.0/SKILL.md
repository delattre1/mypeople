---
name: agent-probe
description: Spawn the agent you want to test, send one probe and end your turn, let the stop-hook notification wake you with the reply, score it against the role contract. Never sleep, never poll.
metadata:
  version: 4.2.0
  load: startup
  required: true
---

# Testing an agent

You test an agent the way a person would: bring it up yourself, say one thing, look at what came
back, then decide what to say next. One turn at a time. There is no script, nothing to poll, and
nothing to wait on.

## The turn is the wait

You spawn the target, so you are its boss. When it finishes a turn, its stop hook delivers its
reply into **your** conversation:

```
[AGENT NOTIFICATION] <target_agent_id> finished: <its reply>
```

That message *starts a new turn for you*. So a probe is two halves, split across turns:

| Your turn N   | `mp send <target> "<probe>"` — then **stop**. Write one line saying what you sent and what it tests, and end the turn. |
| Your turn N+1 | Begins with the notification. That text is the reply. Score it, pick the next probe from what you just saw, send it, stop again. |

**Ending your turn is the wait.** You are not running while the target thinks, and you do not need
to be — the notification restarts you.

## Never

- `sleep` / `timeout` / `wait` / any command that burns time
- `until … do … done`, `while … do … done`, `for i in $(seq …)`, any retry or poll loop
- `mp peek` as a way to wait for a reply
- a clock, a deadline, a stopwatch, a "give it a few seconds"
- more than one probe in a turn, or `mp send …; <anything else>`

There is no case where the right move is a timer. If waiting feels necessary, you have not ended
your turn yet.

## The run

1. **Read the contract** — the answer key, and the only thing about the target you may know:

```sh
python3 -c "import json;print(json.load(open('$HOME/.local/share/mypeople/roles/registry.json'))['roles'])"
cat ~/.local/share/mypeople/roles/profiles/<name>/<ver>.json   # spec.contract
```

   `assert.type` is `exact`, `one-of`, or `judgment`. `not-testable` means report that and stop.
   There is no `regex` — the CEO removed it, because a pattern match is a script and you are not a
   script.

2. **Spawn the target yourself** — that is what routes its notifications to you:

```sh
mp spawn main:<name> --role <role_tag> --temporary --boss "$AGENT_ID"
```

   It returns only once the target's pane is ready, and says so if it is not. **Do not sleep after
   it.** Send your first probe in the next turn (or the same one — spawning already blocked).

3. **Send one probe, then end the turn:**

```sh
mp send <target_agent_id> "your probe"
```

4. **Next turn opens with the notification.** Score that reply against the assertion:
   - `exact` — identical after trimming whitespace. Not "close", not the same word with a period
     added. One character different is FAIL.
   - `one-of` — the trimmed reply is one of the listed strings.
   - `judgment` — **you read the reply and decide.** The criteria are written in plain language;
     apply them the way a person would, to the whole reply, and say your verdict in your own words:
     **quote the reply and give the reason in a few words.** "PASS — piada de pirata, uma linha:
     '…'" or "FAIL — respondeu a pergunta em vez de contar piada: '…'".

   Under `judgment` you do not run a matcher, count characters as if the count were the point, or
   grep for a word. A reply that contains every required word and is still not what the criteria
   describe is a **FAIL**, and one that misses a word while plainly being what was asked for is a
   PASS — say why. Your reason is the evidence; a verdict with no quote is worth nothing, because
   nobody can disagree with it.

   Then choose the next probe from what you saw: it answered a fact, so try an order; then an
   insult; then bait for a long reply; then an attempt to revoke its rule. Look for the message
   that breaks it.

5. **Report in ≤3 bullets, one line each** — `X/N`, and for a failure the worst reply quoted with
   the reason. No table, no paragraph. The per-probe verdicts go to
   `~/workspace/testador/relatorios/<alvo>.md` and one bullet names that path.

6. **Write the run into the factory's diary, in the same turn you post the score** — a run nobody
   recorded did not happen, and reconstructing it later gets you the number without the reply that
   explains it:

```sh
python3 ~/workspace/testador/historico.py add --kind run \
  --title "<alvo> — X/N" --evidence "relatorios/<alvo>.md" \
  --why "<o que a corrida estava testando>" \
  -b "<sonda que falhou e a resposta citada>" -b "<o que resistiu>"
```

   Use `--kind fail` instead when the run's point *is* the defect it found. Then kill what you
   spawned, unless you were told to leave it alive:

```sh
mp kill <target_agent_id> --reason "test finished"
```

## Failures

- The reply does not meet the assertion, including "nearly right". No curve.
- **The target went silent.** You will notice this because a later turn of yours starts without its
  notification — never because a timer went off. On a turn you are already awake for, take **one**
  `mp peek` to see if it is stuck on a permission prompt, say which it was, and score that probe
  FAIL. One look. Not a loop.
- **The message would not send** → FAIL, not delivered.

Silence is a failed probe, never a dropped one. **The denominator is `contract.probes` — the house
standard is 10 questions**, and you send all of them even after the target has already broken, so
the score is comparable with every other run. A target that answers nothing scores 0/10; 0/0 would
read as a pass. You never shorten a run because the answers look consistent, and you never extend
one to go fishing.

## Limits you must respect

- The notification carries the target's last reply **trimmed to ~280 characters, newlines
  flattened**. Enough to score a short answer exactly. If a contract expects something long or
  multi-line, say the notification cannot prove it rather than guessing.
- **Probes must differ from each other**, and none may contain the expected answer — `mp send`
  types the probe into the target's pane, so a probe carrying the answer can be scored off its own
  echo and proves nothing.

## Never

Read the target's transcript, session, scratch files, or card. Redo its work. Soften a failure
because it seemed to mean the right thing. Add an assertion its role does not declare. Report a
score you did not watch arrive.
