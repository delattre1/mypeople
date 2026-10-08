#!/usr/bin/env python3
"""mprole - versioned role-bundle mount engine for `mp spawn --role` (card 070e43e842).

Implements the interface that eng-97's committed acceptance test (`tests/test_role_profiles.py`)
pins for the mount: resolve_role -> a validated, digest-locked role item; materialize_role -> a
derived per-agent bundle view with one lock and two native adapters; plus launch-flag helpers. A
layer INSIDE the existing backend-aware `mp spawn`, never a parallel spawn system.

FAIL-CLOSED: resolve_role validates every reference stays inside the read-only role store (except
the explicit Boss-doctrine URI), reads every required byte, and SHA-256s the profile/personality/
skills/hook/tool/policy into one role digest -- raising ValueError BEFORE any tmux/roster exists.
Missing/unknown/escaping/corrupt content is `unavailable`, never a degraded generic prompt.

Python 3 stdlib only. No QUEUE_SECRET or auth bytes ever land in a role file / attestation / env.
"""
import os, re, io, json, hashlib

BOSS_DOCTRINE_URI = "mypeople://boss-doctrine"


class RoleError(ValueError):
    """Fail-closed preflight/lock/materialize error (a ValueError so callers can catch either)."""
    pass


# ---------------------------------------------------------------- path safety / io
def _store_path(role_dir, ref, what="role file"):
    store = os.path.realpath(role_dir)
    p = os.path.realpath(os.path.join(store, ref))
    try:
        if os.path.commonpath((p, store)) != store:
            raise RoleError("%s %r escapes the role store (unavailable)" % (what, ref))
    except ValueError:
        raise RoleError("%s %r escapes the role store (unavailable)" % (what, ref))
    if not os.path.isfile(p):
        raise RoleError("required %s %r is unavailable" % (what, ref))
    return p


def _read_bytes(path, what):
    try:
        with open(path, "rb") as f:
            b = f.read()
    except OSError as e:
        raise RoleError("%s unavailable (%s): %s" % (what, path, e))
    if not b.strip():
        raise RoleError("%s is empty/unavailable: %s" % (what, path))
    return b


def _read_json(path, what):
    b = _read_bytes(path, what)
    try:
        return b, json.loads(b.decode("utf-8"))
    except Exception as e:
        raise RoleError("%s is corrupt/unavailable (%s): %s" % (what, path, e))


def _sha256(*chunks):
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.encode("utf-8") if isinstance(c, str) else c)
        h.update(b"\x00")
    return h.hexdigest()


def _skill_name(body, ref):
    text = body.decode("utf-8", "replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        raise RoleError("skill %r has no YAML frontmatter (unavailable)" % ref)
    nm = re.search(r"(?m)^name:\s*(.+?)\s*$", m.group(1))
    if not nm:
        raise RoleError("skill %r frontmatter missing name (unavailable)" % ref)
    return nm.group(1).strip()


# ---------------------------------------------------------------- resolve + lock
def resolve_role(role, backend, role_dir, boss_doc, locked_role_ref=None):
    """Resolve a role tag (or the exact recorded name@semver on revive) into a validated lock item.

    Raises ValueError ('unknown role' / '... unavailable') fail-closed before any side effect.
    """
    role = (role or "").strip().lower()
    # registry.json IS the allowlist. A second hardcoded tuple here only ever went stale: it refused
    # tags the registry already carried, so an authored role was unspawnable. Still fail-closed --
    # an unregistered tag raises before any side effect, exactly as the tuple did.
    reg_b, reg = _read_json(os.path.join(role_dir, "registry.json"), "role registry")
    roles = reg.get("roles") if isinstance(reg, dict) else None
    if not isinstance(roles, dict):
        raise RoleError("role registry has no roles map (unavailable)")
    if role not in roles:
        raise RoleError("unknown role %r (known: %s)" % (role, ", ".join(sorted(roles))))
    profile_ref = roles[role]
    if locked_role_ref:
        want = "profiles/%s.json" % locked_role_ref.replace("@", "/")
        if want != profile_ref:
            raise RoleError("locked role drift: recorded %s but registry now points to %s"
                            % (locked_role_ref, profile_ref))
    prof_b, profile = _read_json(_store_path(role_dir, profile_ref, "role profile"), "role profile")
    if profile.get("kind") != "RoleProfile":
        raise RoleError("%s is not a RoleProfile (unavailable)" % profile_ref)
    meta = profile.get("metadata", {}) or {}
    spec = profile.get("spec", {}) or {}
    if meta.get("name") != role:
        raise RoleError("profile name %r != role %r (unavailable)" % (meta.get("name"), role))
    role_ref = "%s@%s" % (meta.get("name", role), meta.get("version", "?"))
    adapters = spec.get("adapters", {}) or {}
    adapter = adapters.get(backend)
    if not adapter:
        raise RoleError("role %s has no %s adapter (unavailable)" % (role_ref, backend))

    # `doctrineRef` is the current name; the file it points at is backend-neutral, so calling it a
    # "personality" (and shipping it as boss-CLAUDE.md) read as Claude-specific when every backend
    # mounts the same bytes. Profiles pinned by a still-running agent predate the rename, so the old
    # key stays readable -- dropping it would fail a revive closed on the locked ref.
    pref = spec.get("doctrineRef") or spec.get("personalityRef", "")
    if pref == BOSS_DOCTRINE_URI:
        if not (boss_doc and os.path.isfile(boss_doc)):
            raise RoleError("Boss doctrine (BOSS_DOC) is unavailable: %s" % boss_doc)
        pers_b = _read_bytes(boss_doc, "Boss doctrine")
        pers_src = BOSS_DOCTRINE_URI
    elif pref:
        pers_b = _read_bytes(_store_path(role_dir, pref, "doctrine"), "doctrine")
        pers_src = pref
    else:
        raise RoleError("profile %s has no doctrineRef (unavailable)" % role_ref)
    pers_digest = _sha256(pers_b)

    skills, seen = [], {}
    for s in (spec.get("skills") or []):
        ref = s.get("ref", "")
        sb = _read_bytes(_store_path(role_dir, ref, "skill"), "skill")
        nm = _skill_name(sb, ref)
        if nm in seen:
            raise RoleError("duplicate skill name %r (%s, %s) unavailable" % (nm, seen[nm], ref))
        seen[nm] = ref
        skills.append({"name": nm, "ref": ref, "bytes": sb, "sha256": _sha256(nm, sb),
                       "load": s.get("load", "startup"), "required": bool(s.get("required"))})
    # Renamed mypeople-system -> mp-system. Profiles pinned by a still-running agent name the old
    # one, so either satisfies the mandate; a profile carrying neither is still refused.
    if not seen.keys() & {"mp-system", "mypeople-system"}:
        raise RoleError("mandatory skill mp-system is unavailable for %s" % role_ref)

    hook_refs = spec.get("hookRefs") or []
    hook_digests = []
    for hr in hook_refs:
        hb, _ = _read_json(_store_path(role_dir, hr, "hookset"), "hookset")
        hook_digests.append(_sha256(hr, hb))
    tool_digest = ""
    if spec.get("toolsetRef"):
        tb, _ = _read_json(_store_path(role_dir, spec["toolsetRef"], "toolset"), "toolset")
        tool_digest = _sha256(spec["toolsetRef"], tb)
    policy_digest = ""
    if spec.get("policyRef"):
        pb, _ = _read_json(_store_path(role_dir, spec["policyRef"], "policy"), "policy")
        policy_digest = _sha256(spec["policyRef"], pb)

    # one lock over the role IDENTITY -- adapter EXCLUDED so both adapters reproduce the same digest
    skill_lock = "".join("%s:%s" % (x["name"], x["sha256"]) for x in
                         sorted(skills, key=lambda k: k["name"]))
    digest = _sha256(role_ref, prof_b, pers_src, pers_digest, skill_lock,
                     "".join(sorted(hook_digests)), tool_digest, policy_digest)
    return {
        "role": role, "role_ref": role_ref, "backend": backend, "digest": digest,
        "profile": profile, "profile_bytes": prof_b,
        "personality_bytes": pers_b, "personality_source": pers_src,
        "personality_digest": pers_digest, "skills": skills,
        "hook_refs": hook_refs, "hookset_digests": hook_digests,
        "toolset_digest": tool_digest, "policy_digest": policy_digest,
        "adapter": adapter, "adapter_version": adapter.get("version", ""),
        "role_dir": role_dir,
    }


# ---------------------------------------------------------------- materialize
# Entries of the operator's real ~/.grok that a per-agent GROK_HOME must reference to stay a
# working grok home. auth.json is the load-bearing one (a bare home is unauthenticated); the rest
# restore trust, resume, and grok's own bundled skills (probed: 102 skills without `bundled`, 118
# with it -- i.e. the 117 baseline plus the mounted role skill).
_GROK_HOME_LINKS = ("auth.json", "sessions", "trusted_folders.toml", "bundled", "version.json",
                    ".metadata_version", "models_cache.json", "agent_id", "installed-plugins",
                    "marketplace-cache")


def _grok_config_toml():
    """The operator's grok config with the project-picker hint forced on.

    Carried verbatim rather than synthesized: it holds permission_mode / model defaults, and a
    mounted agent must behave exactly like an unmounted one. It holds no credentials (those are in
    auth.json, mode 0600), so copying these bytes into the bundle leaks nothing.
    """
    src = os.path.expanduser("~/.grok/config.toml")
    s = ""
    if os.path.exists(src):
        try:
            with open(src) as f:
                s = f.read()
        except OSError:
            s = ""
    m = re.search(r'^\[hints\][ \t]*$', s, re.M)
    if re.search(r'^[ \t]*project_picker_disabled\s*=\s*true', s, re.M):
        return s
    if m:
        return s[:m.end()] + "\nproject_picker_disabled = true" + s[m.end():]
    # a bare key must precede the first [section] header
    return "[hints]\nproject_picker_disabled = true\n\n" + s


def _safe_aid(aid):
    return re.sub(r"[^A-Za-z0-9._-]", "_", aid)


def _link_grok_home_entry(grok_home, name):
    """Symlink ~/.grok/<name> into a per-agent GROK_HOME, promoting private auth first.

    auth.json is load-bearing. Grok may unlink the symlink and write a private auth.json into
    GROK_HOME during device-login; we must never discard a newer private token (root cause of the
    "Boss opens as Grok unauthenticated" flap: the old loop os.remove()'d the fresh token and
    re-linked the stale one on every respawn). Card f6339b85a2.
    """
    src = os.path.expanduser("~/.grok/%s" % name)
    dst = os.path.join(grok_home, name)
    if name == "auth.json" and os.path.lexists(dst) and (not os.path.islink(dst)) and os.path.isfile(dst):
        try:
            promote = False
            if not os.path.exists(src):
                promote = os.path.getsize(dst) > 0
            elif os.path.getsize(dst) > 0 and os.path.getmtime(dst) > (os.path.getmtime(src) + 0.5):
                promote = True
            if promote:
                os.makedirs(os.path.dirname(src) or ".", exist_ok=True)
                tmp = src + ".mypeople-promote-%d" % os.getpid()
                import shutil
                shutil.copy2(dst, tmp)
                os.chmod(tmp, 0o600)
                os.replace(tmp, src)
                os.chmod(src, 0o600)
        except OSError as e:
            raise RoleError("failed to promote private GROK_HOME auth.json into ~/.grok: %s" % e)
    if os.path.lexists(dst):
        try:
            os.remove(dst)
        except OSError as e:
            if name == "auth.json":
                raise RoleError("cannot replace GROK_HOME auth.json with symlink: %s" % e)
            return
    if not os.path.exists(src):
        if name == "auth.json":
            raise RoleError(
                "operator ~/.grok/auth.json missing — run `grok` once outside MyPlow and "
                "complete login, then re-spawn the Grok Boss")
        return
    try:
        os.symlink(src, dst)
    except OSError as e:
        if name == "auth.json":
            raise RoleError("cannot symlink GROK_HOME auth.json -> ~/.grok/auth.json: %s" % e)
        return
    if name == "auth.json":
        if not os.path.islink(dst) or not os.path.samefile(dst, src):
            raise RoleError("GROK_HOME auth.json is not a live symlink to ~/.grok/auth.json after mount")


def _write_ro(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.chmod(path, 0o644)
    with open(path, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)
    os.chmod(path, 0o444)  # generated regular files are read-only to the runtime agent


def _startup_doc(item):
    buf = io.StringIO()
    buf.write("# MyPlow locked role\n\n")
    buf.write("Role: `%s`\n" % item["role_ref"])
    buf.write("Role digest: `%s`\n" % item["digest"])
    buf.write("Personality: `%s` (sha256 `%s`)\n\n" % (item["personality_source"], item["personality_digest"]))
    buf.write("## Personality\n\n")
    buf.write(item["personality_bytes"].decode("utf-8", "replace").rstrip() + "\n")
    for s in sorted(item["skills"], key=lambda k: k["name"]):
        if s["load"] != "startup":
            continue
        buf.write("\n\n<!-- skill %s sha256 %s -->\n" % (s["name"], s["sha256"]))
        buf.write(s["bytes"].decode("utf-8", "replace").rstrip() + "\n")
    return buf.getvalue()


def materialize_role(item, aid, backend, bundle_root):
    """Materialize the derived per-agent view (idempotent + self-healing) and return the bundle."""
    bp = os.path.join(bundle_root, _safe_aid(aid), item["digest"][:12])
    startup_text = _startup_doc(item)
    startup_digest = _sha256(startup_text)

    # common layer (personality + startup + skills) -- always (re)written so a damaged/deleted
    # derived file is reconstructed from the canonical store on the next spawn/revive.
    personality_path = os.path.join(bp, "common", "personality.md")
    startup_path = os.path.join(bp, "common", "startup.md")
    _write_ro(personality_path, item["personality_bytes"])
    _write_ro(startup_path, startup_text)
    for s in item["skills"]:
        _write_ro(os.path.join(bp, "common", "skills", s["name"], "SKILL.md"), s["bytes"])

    # resolve hookset events once (already validated)
    events, handler = [], "plugins/tmux-boss-hooks/emit-event.sh"
    for hr in item["hook_refs"]:
        try:
            with open(os.path.join(item["role_dir"], hr)) as f:
                hs = json.load(f)
            events += hs.get("events", []); handler = hs.get("handler", handler)
        except Exception:
            pass
    events = sorted(set(events))
    handler_abs = os.path.join(os.path.dirname(item["role_dir"]), handler)

    plugin_path = os.path.join(bp, "claude", "plugin")
    settings_path = None
    codex_home = os.path.join(bp, "codex", "home")
    codex_profile = "mypeople-%s" % item["role"]
    grok_home = os.path.join(bp, "grok", "home")

    if backend == "claude":
        _write_ro(os.path.join(plugin_path, ".claude-plugin", "plugin.json"),
                  json.dumps({"name": "mypeople-role-%s" % item["role"], "version": "1.0.0",
                              "description": "MyPlow %s role skills (locked %s)"
                              % (item["role"], item["digest"][:12])}, indent=2))
        for s in item["skills"]:
            _write_ro(os.path.join(plugin_path, "skills", s["name"], "SKILL.md"), s["bytes"])
        settings_path = os.path.join(bp, "claude", "settings.json")
        # This file is handed to claude as --settings, which is the `flagSettings` layer. Claude
        # ORs skipDangerousModePermissionPrompt across user/local/flag/policy settings before it
        # decides whether to block on the Bypass Permissions modal, so declaring it here makes a
        # role-mounted spawn self-sufficient even if ~/.claude was never seeded (card 293fc81898).
        overlay = {"skipDangerousModePermissionPrompt": True,
                   "hooks": {ev: [{"hooks": [{"type": "command",
                                              "command": "%s %s" % (handler_abs, ev)}]}]
                             for ev in events}}
        _write_ro(settings_path, json.dumps(overlay, indent=2))
    elif backend == "codex":
        for s in item["skills"]:
            _write_ro(os.path.join(codex_home, "skills", s["name"], "SKILL.md"), s["bytes"])
        _write_ro(os.path.join(codex_home, "hooks.json"),
                  json.dumps({"events": events, "handler": handler_abs}, indent=2))
        # reference the user's existing codex session dir (never copy credential bytes)
        sessions_link = os.path.join(codex_home, "sessions")
        os.makedirs(codex_home, exist_ok=True)
        if os.path.islink(sessions_link) or os.path.exists(sessions_link):
            try: os.remove(sessions_link)
            except OSError: pass
        try:
            os.symlink(os.path.expanduser("~/.codex/sessions"), sessions_link)
        except OSError:
            pass
        # generated profile config carrying the SAME startup bytes as additive developer_instructions
        settings_path = os.path.join(bp, "codex", "config.toml")
        toml = ('[profiles.%s]\n# additive; never a replacement model_instructions_file\n'
                'developer_instructions = """\n%s"""\n'
                % (codex_profile, startup_text.replace('"""', '\\"\\"\\"')))
        _write_ro(settings_path, toml)
    elif backend == "grok":
        # grok has no per-agent personality/skills FLAGS; it keys everything off GROK_HOME, so the
        # adapter is shaped like codex's (a generated per-agent home) rather than claude's.
        #   AGENTS.md            -> read as GLOBAL instructions (probed: `grok inspect` reports
        #                           "Agents.md (global)"). GROK.md is NOT read -- do not use it.
        #   skills/<n>/SKILL.md  -> discovered as skills (probed).
        # Rejected: --rules (a large BOSS_DOC would blow argv), --system-prompt-override (replaces
        # grok's own tool prompt instead of adding to it), and writing AGENTS.md into the agent's
        # cwd (a side effect on a directory the agent shares with its work).
        os.makedirs(grok_home, exist_ok=True)
        _write_ro(os.path.join(grok_home, "AGENTS.md"), startup_text)
        for s in item["skills"]:
            _write_ro(os.path.join(grok_home, "skills", s["name"], "SKILL.md"), s["bytes"])
        _write_ro(os.path.join(grok_home, "hooks.json"),
                  json.dumps({"events": events, "handler": handler_abs}, indent=2))
        # GROK_HOME is grok's WHOLE home, not an overlay: a bare one is UNAUTHENTICATED
        # ("You are not authenticated") and would strand the agent on a login prompt, and it also
        # loses grok's bundled skills and trust. Reference the user's real home for each of these
        # -- symlinks, never copied bytes, so credentials are never duplicated into the bundle and
        # a token refresh writes through to the one real auth.json.
        for name in _GROK_HOME_LINKS:
            _link_grok_home_entry(grok_home, name)
        # A per-agent home starts with no config, which would re-arm the project picker that
        # swallows the first message (see mp.grok_pretrust) and drop the operator's
        # permission_mode -- a mounted Boss would hang on an approval prompt. Carry the operator's
        # config verbatim, with the picker hint forced on.
        settings_path = os.path.join(grok_home, "config.toml")
        _write_ro(settings_path, _grok_config_toml())
    else:
        raise RoleError("unsupported backend %r" % backend)

    attestation = {
        "agent_id": aid, "role": item["role"], "role_ref": item["role_ref"], "backend": backend,
        "adapter_version": item["adapter_version"], "role_digest": item["digest"],
        "personality_source": item["personality_source"], "personality_digest": item["personality_digest"],
        "skill_digests": {s["name"]: s["sha256"] for s in item["skills"]},
        "hookset_digests": item["hookset_digests"], "toolset_digest": item["toolset_digest"],
        "policy_digest": item["policy_digest"], "startup_digest": startup_digest, "bundle_path": bp,
    }
    _write_ro(os.path.join(bp, "attestation.json"), json.dumps(attestation, indent=2))

    return {
        "role": item["role"], "role_ref": item["role_ref"], "backend": backend,
        "digest": item["digest"], "personality_digest": item["personality_digest"],
        "skills": [{"name": s["name"], "sha256": s["sha256"]} for s in item["skills"]],
        "bundle_path": bp, "startup_path": startup_path, "personality_path": personality_path,
        "plugin_path": plugin_path, "settings_path": settings_path,
        "codex_home": codex_home, "codex_profile": codex_profile,
        "grok_home": grok_home,
        "startup_digest": startup_digest, "adapter_version": item["adapter_version"],
    }


# ---------------------------------------------------------------- adapter launch bits
def role_env(bundle):
    """Non-secret MYPEOPLE_ROLE* env, identical across adapters.

    codex and grok have no per-agent personality/skills flags -- both mount their whole bundle
    through a home dir, so the home is part of the launch env rather than the flag list.
    """
    env = {
        "MYPEOPLE_ROLE": bundle["role"],
        "MYPEOPLE_ROLE_REF": bundle["role_ref"],
        "MYPEOPLE_ROLE_DIGEST": bundle["digest"],
        "MYPEOPLE_ROLE_BUNDLE": bundle["bundle_path"],
    }
    if bundle["backend"] == "codex":
        env["CODEX_HOME"] = bundle["codex_home"]
    elif bundle["backend"] == "grok":
        env["GROK_HOME"] = bundle["grok_home"]
    return env


def claude_flags(bundle):
    flags = ["--append-system-prompt-file", bundle["startup_path"], "--plugin-dir", bundle["plugin_path"]]
    if bundle.get("settings_path"):
        flags += ["--settings", bundle["settings_path"]]
    return flags


def codex_flags(bundle):
    return ["--profile", bundle["codex_profile"]]


def grok_flags(bundle):
    """None: grok mounts the personality (AGENTS.md) and skills entirely via GROK_HOME.

    Kept so every adapter has the same (env, flags) shape at the call site.
    """
    return []
