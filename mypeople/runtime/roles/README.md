# MyPlow role bundles

`mp spawn ... --role <tag>` resolves tags from `registry.json`. A role manifest composes a
backend-neutral personality, mandatory and role-specific Agent Skills, the existing lifecycle
hookset, a toolset, and an authority policy. Claude and Codex adapters materialize the same locked
sources into native files at spawn; the canonical sources here are never edited by a live agent.

The initial supported tags are `boss` and `engineer`. `boss` deliberately resolves its doctrine
through the installed `mp-boss-doctrine.md` (`mypeople://boss-doctrine`); the engineer's lives in
`doctrines/`. The file is mounted verbatim by every backend, so it is named for the role, not for a
provider -- the old `boss-CLAUDE.md` name read as Claude-specific while grok and codex mounted the
same bytes.

Profiles name it `doctrineRef`. Profiles pinned before the rename still use `personalityRef` and
remain resolvable, so an agent holding a locked ref can still be revived.

Skills that ship with the product are prefixed `mp-` (`mp-system`, `mp-boss-manager`, `mp-memory`)
so they are distinguishable from a user's own skills once mounted. `mp-boss-manager` is Boss-only
and owns every spawn/control operation; `mp-system` is shared by every role and deliberately does
NOT teach agent control.

Every role must include `mp-system` as a required startup skill. Resolution is fail-closed:
unknown roles, escaping references, missing files, invalid manifests, or digest drift abort before a
tmux window or roster entry is created.

## Pending changes

Shared-skill edits that no profile uses yet. The next merged `mypeople-system` version must carry all
of them in one version. A registry flip leaves every running agent of that role unable to revive
(`locked role drift`) until it is killed and respawned, so one flip, ordered by the Boss.

- `mypeople-system` 2.4.0 (this repo): MyPeople is renamed MyPlow in the skill text.
- `mypeople-system` 2.5.0 (live install only): a "Roadmap rules" section (`First asked:` line on
  filed requests, `Fixes plow-pbc/<repo>#N` in PRs, `Shipped:` comment on requests fixed in private
  repos).
- `mp-system` 3.0.0 (live install only): the split that moves agent control out of the shared skill
  into `mp-boss-manager`.
- `mp cron` (5.22.12; reply channel in 5.22.57): add to "Supported agent operations":
  `mp cron add "<cron>" <session:agent> "<task>" --for <name> --reply "<reply command>"` /
  `mp cron set <id> --reply "..."` / `mp cron ls` / `mp cron rm <id>`. Schedule a message to any agent,
  yourself included, instead of writing your own timer loop. A request that came from a chat names
  that chat's reply command, so each run answers the person where they asked.
