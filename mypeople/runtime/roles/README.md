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
