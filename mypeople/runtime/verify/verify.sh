#!/usr/bin/env bash
# MyPlow DISTRIBUTABLE acceptance harness (Fase-1 scope; see tools/scope-verify.py)
# MyPlow acceptance harness (§14/§15). Exit code = truth (0 = Done). Self-contained.
# STANDALONE mode: UPSTREAM_QUEUE_URL unset => J12/J13 skipped.
set -u
source "${MYPEOPLE_CONFIG_PATH:-$HOME/.config/mypeople/queue.env}" 2>/dev/null || true
export PATH="$HOME/.local/bin:${INSTALL_DIR:-$HOME/mypeople}/bin:$PATH"
export TMUX=
ID="${INSTALL_DIR:-$HOME/mypeople}"
BIN="$ID/bin"
QS="X-Queue-Secret: $QUEUE_SECRET"
HUD="http://127.0.0.1:${HUD_PORT:-9900}"
TODO="http://127.0.0.1:${TODO_PORT:-9933}"
TTYD_PORT="${TTYD_PORT:-7681}"
TTYD_BROWSER_PORT="${TTYD_BROWSER_PORT:-$TTYD_PORT}"
HOST="${HOST_ID}"
BOSS="$HOST/main:Boss"
CURL="curl -s --max-time 12"
FAILS=(); PASSES=0
CREATED=()
pass(){ PASSES=$((PASSES+1)); echo "PASS $1"; }
fail(){ FAILS+=("$1"); echo "FAIL $1"; }
chk(){ if [ "$1" = "1" ] || [ "$1" = "0" -a "${3:-}" = "invert" ]; then :; fi; }
jqget(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

cleanup(){
  for tid in "${CREATED[@]:-}"; do
    [ -n "$tid" ] && $CURL -X POST -H "$QS" -H 'Content-Type: application/json' \
      -d "{\"op\":\"del\",\"id\":\"$tid\"}" "$TODO/todo/update" >/dev/null 2>&1 || true
  done
  rm -f /tmp/p.png /tmp/p.mp4 /tmp/mpcj.txt
}
trap cleanup EXIT

echo "===== MyPlow Verify ($(date -u +%FT%TZ)) node=$HOST ====="
BACKEND="${DEFAULT_BACKEND:-claude}"

# ---------- J1 install one-shot ----------
h=$($CURL "$HUD/health" | jqget "d.get('status')")
[ "$h" = "ok" ] && [ -f "$BIN/queue-server.py" ] && [ -f "$BIN/mp" ] && [ -f "$BIN/todo-server.py" ] \
  && pass "J1 install-one-shot" || fail "J1 install-one-shot"

# ---------- J1b shared lifecycle contract; retired hooks must be absent ----------
hookok=$(python3 - <<'PY'
import json, os
expected = {"SessionStart", "UserPromptSubmit", "Stop"}
paths = [os.path.expanduser("~/.claude/settings.json"), os.path.expanduser("~/.codex/hooks.json")]
ok = True
for path in paths:
    try:
        hooks = json.load(open(path)).get("hooks", {})
    except Exception:
        ok = False
        continue
    mine = set()
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            commands = [h.get("command", "") for h in group.get("hooks", [])]
            if any("/plugins/tmux-boss-hooks/emit-event.sh" in c for c in commands):
                mine.add(event)
    ok = ok and mine == expected
print(int(ok))
PY
)
[ "$hookok" = "1" ] && pass "J1b lifecycle-hooks-three-only" || fail "J1b lifecycle-hooks-three-only"

# ---------- J2 Boss in HUD + doctrine summary ----------
AG=$($CURL -H "$QS" "$HUD/agents")
bstate=$(echo "$AG" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['agent_id']=='$BOSS'];print(a[0]['state'] if a else 'none')")
bsum=$(echo "$AG" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['agent_id']=='$BOSS'];print(a[0]['summary'] if a else '')")
kw=$(python3 -c "s='''$bsum'''.lower();K=['plan','approve','queue','mp','autonomous','verify','fire-and-forget'];print(sum(1 for k in K if k in s))")
[ "$bstate" = "alive" ] && [ "${kw:-0}" -ge 2 ] && pass "J2 boss-in-hud (kw=$kw)" || fail "J2 boss-in-hud state=$bstate kw=${kw:-0}"

# ---------- J2b steady-state (short re-assert) ----------
sleep 6
c_ok=1
for pat in "queue-server.py" "todo-server.py" "queue-client.py" "boss-supervisor.sh"; do
  n=$(pgrep -fc "python3 .*$pat" 2>/dev/null || pgrep -fc "$pat" 2>/dev/null || echo 0)
  # count real procs via ps to avoid self-match
  n=$(ps -eo cmd | grep -F "$pat" | grep -v grep | grep -v verify.sh | wc -l)
  [ "$n" = "1" ] || { c_ok=0; echo "  daemon $pat count=$n"; }
done
b2=$($CURL -H "$QS" "$HUD/agents" | python3 -c "import sys,json;print(len([x for x in json.load(sys.stdin) if x['agent_id']=='$BOSS' and x['state']=='alive']))")
h2=$($CURL "$HUD/health" | jqget "d.get('status')")
[ "$c_ok" = "1" ] && [ "$b2" = "1" ] && [ "$h2" = "ok" ] && pass "J2b steady-state" || fail "J2b steady-state daemons_ok=$c_ok boss=$b2"

# ---------- J3 board->boss ping ----------
R=$($CURL -X POST -H "$QS" -H 'Content-Type: application/json' -d '{"op":"add","text":"J3 ping check"}' "$TODO/todo/update")
TID=$(echo "$R" | jqget "d['id']")
CREATED+=("$TID")
ping_seen=0
for i in $(seq 1 10); do
  sleep 2
  tmux capture-pane -p -t mc-main:Boss -S -100 2>/dev/null | grep -q "$TID" && { ping_seen=1; break; }
done
[ "$ping_seen" = "1" ] && pass "J3 board->boss-ping" || fail "J3 board->boss-ping (tid $TID not in pane)"

# ---------- J3b agent completion notification + env-export + empty-boss guard ----------
# empty --boss must FAIL, omitted --boss must SUCCEED
mp spawn "$HOST/main:guard-x" --boss '' >/dev/null 2>&1; eb_rc=$?
mp spawn "$HOST/main:toplevel-x" >/dev/null 2>&1; ok_rc=$?
mp kill "$HOST/main:toplevel-x" >/dev/null 2>&1
[ "$eb_rc" != "0" ] && [ "$ok_rc" = "0" ] && pass "J3b empty-boss-guard" || fail "J3b empty-boss-guard eb=$eb_rc ok=$ok_rc"
# spawn throwaway with boss, check env then completion notification
TW="$HOST/main:tw-notify"
mp spawn "$TW" --boss "$BOSS" >/dev/null 2>&1
sleep 8
mp send "$TW" "printf 'ENVCHK AGENT_ID=[%s] BOSS_ID=[%s] QUEUE_URL=[%s]\n' \"\$AGENT_ID\" \"\$BOSS_ID\" \"\$QUEUE_URL\"" >/dev/null 2>&1
# that prompt makes the selected backend do something; env check reads the pane child
sleep 3
ppid=$(tmux list-panes -t mc-main:tw-notify -F '#{pane_pid}' 2>/dev/null | head -1)
envok=0
if [ -n "$ppid" ]; then
  # find the agent child under the pane shell
  for pid in $ppid $(pgrep -P "$ppid" 2>/dev/null); do
    aid=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | grep '^AGENT_ID=' | cut -d= -f2-)
    [ "$aid" = "$TW" ] && envok=1
  done
fi
[ "$envok" = "1" ] && pass "J3b env-export (AGENT_ID)" || fail "J3b env-export AGENT_ID not set to $TW"
# drive a real turn -> Stop hook -> boss notification
mp send "$TW" "Reply with the single word: done" >/dev/null 2>&1
notif=0
for i in $(seq 1 20); do
  sleep 4
  tmux capture-pane -p -t mc-main:Boss -S -80 2>/dev/null | grep -q "AGENT NOTIFICATION.*tw-notify" && { notif=1; break; }
done
[ "$notif" = "1" ] && pass "J3b completion-notification" || fail "J3b completion-notification (no [AGENT NOTIFICATION])"
mp kill "$TW" >/dev/null 2>&1

# ---------- J3c engineer sender receives the target's next Stop ----------
RP="$HOST/main:reply-parent"
RQ="$HOST/main:reply-peer"
mp spawn "$RP" --boss "$BOSS" >/dev/null 2>&1
mp spawn "$RQ" --boss "$BOSS" >/dev/null 2>&1
sleep 5
AGENT_ID="$RP" mp send "$RQ" "Reply with exactly J3C-PEER-DONE and do nothing else." >/dev/null 2>&1
reply_seen=0
for i in $(seq 1 20); do
  sleep 4
  peer_done=$(python3 -c "import json;d=json.load(open('$ID/status/mc-main/reply-peer.json'));print(d.get('status')=='idle' and 'J3C-PEER-DONE' in d.get('summary',''))" 2>/dev/null)
  parent_notice=$(tmux capture-pane -p -t mc-main:reply-parent -S -120 2>/dev/null | grep -c "AGENT NOTIFICATION.*reply-peer" || true)
  [ "$peer_done" = "True" ] && [ "${parent_notice:-0}" -gt 0 ] && { reply_seen=1; break; }
done
[ "$reply_seen" = "1" ] && pass "J3c engineer->engineer Stop reply" \
  || fail "J3c engineer->engineer Stop reply"
mp kill "$RQ" >/dev/null 2>&1
mp kill "$RP" >/dev/null 2>&1
python3 -c "import json;p='$ID/run/roster.json';d=json.load(open(p));[d.pop(k,None) for k in ('$RP','$RQ')];json.dump(d,open(p,'w'))" 2>/dev/null
rm -f "$ID/status/mc-main/reply-parent.json" "$ID/status/mc-main/reply-peer.json"

# ---------- J4 supervisor resurrection ----------
boss_sid_before=$(python3 -c "import json;print(json.load(open('$ID/run/roster.json')).get('$BOSS',{}).get('session_id',''))")
tmux kill-window -t mc-main:Boss 2>/dev/null
res=0
for i in $(seq 1 12); do
  sleep 4
  st=$($CURL -H "$QS" "$HUD/agents" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['agent_id']=='$BOSS'];print(a[0]['state'] if a else 'none')" 2>/dev/null)
  tmux list-windows -t mc-main -F '#{window_name}' 2>/dev/null | grep -qx Boss && [ "$st" = "alive" ] && { res=1; break; }
done
boss_sid_after=$(python3 -c "import json;print(json.load(open('$ID/run/roster.json')).get('$BOSS',{}).get('session_id',''))")
[ "$res" = "1" ] && [ -n "$boss_sid_before" ] && [ "$boss_sid_before" = "$boss_sid_after" ] \
  && pass "J4 supervisor-resurrection-same-session" \
  || fail "J4 supervisor-resurrection res=$res sid_same=$([ "$boss_sid_before" = "$boss_sid_after" ] && echo 1 || echo 0)"

# ---------- J5 todo add round-trips ----------
bd=$($CURL -H "$QS" "$TODO/todo/board")
echo "$bd" | grep -q "$TID" && [ "$($CURL -o /dev/null -w '%{http_code}' "$TODO/")" = "200" ] && pass "J5 todo-roundtrip" || fail "J5 todo-roundtrip"

# ---------- J6 cross-nav (relative hrefs) ----------
# the nav is Board / Graph only (the HUD is off the bar but still serves by URL)
$CURL "$TODO/" | grep -q 'href="/terminal-graph"' && $CURL "$HUD/dashboard" | grep -q 'href="/"' && pass "J6 cross-nav" || fail "J6 cross-nav"

# ---------- J6b port-shifted origin ----------
python3 "$ID/verify/proxy.py" 38080 "${TODO_PORT:-9933}" >/dev/null 2>&1 &
PXP=$!; sleep 1
p1=$($CURL "http://127.0.0.1:38080/" | grep -c 'id="addInput"')
p2=$($CURL "http://127.0.0.1:38080/dashboard" | grep -c "MyPlow - HUD")
kill $PXP 2>/dev/null
[ "$p1" -ge 1 ] && [ "$p2" -ge 1 ] && pass "J6b port-shift-proxy" || fail "J6b port-shift-proxy p1=$p1 p2=$p2"

# ---------- J7 click-to-terminal resolver ----------
at=$($CURL -H "$QS" "$TODO/todo/attach?agent=$BOSS")
echo "$at" | python3 -c "import sys,json;d=json.load(sys.stdin);exit(0 if d.get('ok') and d.get('target')=='mc-main:Boss' and d.get('port')==int('$TTYD_BROWSER_PORT') else 1)" && pass "J7 attach-resolver" || fail "J7 attach-resolver"

# ---------- J8/J27/J47 attach-live-pane (SKIPPED — out of Fase-1 scope)
echo "SKIP J8/J27/J47 attach-live-pane: cross-host routing (single host uses localhost + published ports)"

# ---------- J9/J9c/J14/J29 PLOW tokens + no animations ----------
for pg in "$TODO/" "$HUD/dashboard"; do
  b=$($CURL "$pg")
  echo "$b" | grep -q "#D5EF8A" && echo "$b" | grep -q "Instrument Serif" && echo "$b" | grep -q "DM Sans" && echo "$b" | grep -q "DM Mono" || { fail "J9 plow-tokens ($pg)"; PLOW=0; }
done
[ -z "${PLOW:-}" ] && pass "J9 plow-identity"
TB=$($CURL "$TODO/"); HB=$($CURL "$HUD/dashboard")
echo "$TB" | grep -q -- "--volt:#D5EF8A" && echo "$TB" | grep -q -- "--midnight:#01000A" && echo "$TB" | grep -q -- "--dark-bg:#111110" \
  && echo "$TB" | grep -q -- "--surface:rgba(255,255,255,0.05)" && echo "$TB" | grep -q -- "--iris:#C4BFFF" \
  && pass "J9c exact-palette" || fail "J9c exact-palette"
if echo "$TB$HB" | grep -Eq "@keyframes|animation:"; then fail "J29 no-animations"; else pass "J29 no-animations"; fi
echo "$TB" | grep -q "op:'reorder'" && fail "J14 no-reorder" || pass "J14 generative-no-reorder"

# ---------- J9a titles ----------
# no title row at all since c6a13ce248 (CEO: "no need of the real estate"): the tab title names the page
echo "$TB" | grep -q "<title>MyPlow - Priorities</title>" && ! echo "$TB" | grep -q "<h1>MyPlow</h1>" \
  && ! echo "$TB" | grep -q "source-of-truth" && ! echo "$TB" | grep -q 'class="mark"' \
  && echo "$HB" | grep -q "MyPlow - HUD" && ! echo "$TB" | grep -q "<h1>MyPlow - Priorities" \
  && pass "J9a titles" || fail "J9a titles"

# ---------- J9g the filter bar is 3 controls, done+cancelled hidden by default ----------
VB=$(echo "$TB" | sed -n '/id="viewbar"/,/<\/div>/p')
nvb=$(echo "$VB" | grep -c 'data-toggle=\|data-lens=')
if [ "$nvb" = "3" ] && ! echo "$VB" | grep -q 'class="chip' \
   && ! echo "$TB" | grep -Eq 'data-view="all"|data-view="hide_done"|data-view="only_done"|enabledStates|ALL_STATES' \
   && echo "$TB" | grep -q 'let showDone = localStorage.getItem("mp_show_done")==="1"'; then
  pass "J9g filter-bar-3-controls"
else
  fail "J9g filter-bar-3-controls (n=$nvb)"
fi

# ---------- J9f no counters/clock in the header ----------
if echo "$TB" | grep -Eq 'id="cDone"|id="cOpen"|id="cTotal"|live-pill|id="clock"|setInterval\(tick'; then
  fail "J9f no-header-counters"
else
  pass "J9f no-header-counters"
fi

# ---------- J9d no proof-attach UI ----------
if echo "$TB" | grep -Eqi '<input[^>]*type="file"|add proof|choose file'; then fail "J9d no-proof-ui"; else pass "J9d no-proof-ui"; fi

# ---------- J9e .check toggle (JS-rendered; assert the behavior is coded, DOM verified in J49c) ----------
echo "$TB" | grep -q 'className="check"' && echo "$TB" | grep -q "stopPropagation" \
  && echo "$TB" | grep -q 'state: done?"working":"done"' && echo "$TB" | grep -q "task-top" \
  && pass "J9e check-toggle-markup" || fail "J9e check-toggle-markup"

# ---------- J10 reachable-remote (SKIPPED — out of Fase-1 scope)
echo "SKIP J10 reachable-remote: cross-host routing, off by default"

# ---------- J15 delete ----------
D=$($CURL -X POST -H "$QS" -d '{"op":"add","text":"to delete"}' "$TODO/todo/update" | jqget "d['id']")
$CURL -X POST -H "$QS" -d "{\"op\":\"del\",\"id\":\"$D\"}" "$TODO/todo/update" >/dev/null
$CURL -H "$QS" "$TODO/todo/board" | grep -q "$D" && fail "J15 delete" || pass "J15 delete"

# ---------- J16 inline edit ----------
E=$($CURL -X POST -H "$QS" -d '{"op":"add","text":"edit me"}' "$TODO/todo/update" | jqget "d['id']")
CREATED+=("$E")
$CURL -X POST -H "$QS" -d "{\"op\":\"set\",\"id\":\"$E\",\"text\":\"edited\",\"doneCondition\":\"cond1\",\"assignee\":\"$BOSS\"}" "$TODO/todo/update" >/dev/null
$CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;t=json.load(sys.stdin)['tasks']['$E'];exit(0 if t['text']=='edited' and t['doneCondition']=='cond1' and t['assignee']=='$BOSS' else 1)" && pass "J16 inline-edit" || fail "J16 inline-edit"

# ---------- J17 state enum + idle rejected ----------
for st in working review done blocked cancelled; do
  $CURL -X POST -H "$QS" -d "{\"op\":\"set\",\"id\":\"$E\",\"state\":\"$st\"}" "$TODO/todo/update" >/dev/null
  got=$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;print(json.load(sys.stdin)['tasks']['$E']['state'])")
  [ "$got" = "$st" ] || { fail "J17 state $st -> $got"; ENUMBAD=1; }
done
idlec=$($CURL -o /dev/null -w '%{http_code}' -X POST -H "$QS" -d "{\"op\":\"set\",\"id\":\"$E\",\"state\":\"idle\"}" "$TODO/todo/update")
[ "$idlec" = "400" ] && [ -z "${ENUMBAD:-}" ] && pass "J17 state-enum (idle rejected)" || fail "J17 state-enum idlecode=$idlec"

# ---------- J18 done toggle ----------
$CURL -X POST -H "$QS" -d "{\"op\":\"set\",\"id\":\"$E\",\"done\":true}" "$TODO/todo/update" >/dev/null
[ "$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;print(json.load(sys.stdin)['tasks']['$E']['state'])")" = "done" ] && pass "J18 done-toggle" || fail "J18 done-toggle"

# ---------- J19 cards are born working, needs_brainstorm is not a state ----------
NB=$($CURL -X POST -H "$QS" -d '{"op":"add","text":"born working"}' "$TODO/todo/update" | jqget "d['id']")
CREATED+=("$NB")
s0=$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;print(json.load(sys.stdin)['tasks']['$NB']['state'])")
nbc=$($CURL -o /dev/null -w '%{http_code}' -X POST -H "$QS" -d "{\"op\":\"set\",\"id\":\"$NB\",\"state\":\"needs_brainstorm\"}" "$TODO/todo/update")
[ "$s0" = "working" ] && [ "$nbc" = "400" ] && pass "J19 born-working nb-rejected" || fail "J19 born-working ($s0) nb-code=$nbc"

# ---------- J20 brainstorm gate gone ----------
bc=$($CURL -o /dev/null -w '%{http_code}' -X POST -H "$QS" -d '{}' "$TODO/todo/brainstorm")
ac=$($CURL -o /dev/null -w '%{http_code}' -X POST -H "$QS" -d '{}' "$TODO/todo/answer")
echo "$TB" | grep -qi "needs-brainstorm banner\|brainstorm-block" && BB=1
[ "$bc" = "404" ] && [ "$ac" = "404" ] && [ -z "${BB:-}" ] && pass "J20 brainstorm-gone" || fail "J20 brainstorm-gone bc=$bc ac=$ac"

# ---------- J21 unread ----------
U=$($CURL -X POST -H "$QS" -d '{"op":"add","text":"unread test"}' "$TODO/todo/update" | jqget "d['id']")
CREATED+=("$U")
$CURL -X POST -H "$QS" -d "{\"task_id\":\"$U\",\"by\":\"$BOSS\",\"body\":\"agent comment\"}" "$TODO/todo/comment" >/dev/null
un=$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;print(json.load(sys.stdin)['tasks']['$U']['unread'])")
[ "${un:-0}" -ge 1 ] && pass "J21 unread-count" || fail "J21 unread-count ($un)"

# ---------- J22 proofs image/video classify + shape ----------
P=$($CURL -X POST -H "$QS" -d '{"op":"add","text":"proof test"}' "$TODO/todo/update" | jqget "d['id']")
CREATED+=("$P")
# real png + mp4
printf '\x89PNG\r\n\x1a\n' > /tmp/p.png; head -c 200 /dev/urandom >> /tmp/p.png
printf '\x00\x00\x00\x18ftypmp42' > /tmp/p.mp4; head -c 200 /dev/urandom >> /tmp/p.mp4
$CURL -H "$QS" -F "task_id=$P" -F "file=@/tmp/p.png;type=image/png" "$TODO/todo/proof" >/dev/null
$CURL -H "$QS" -F "task_id=$P" -F "file=@/tmp/p.mp4;type=video/mp4" "$TODO/todo/proof" >/dev/null
pk=$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "
import sys,json
p=json.load(sys.stdin)['tasks']['$P']['proofs']
kinds=[x['kind'] for x in p]
shape=all(set(x.keys())>={'kind','url','body','ts'} for x in p)
print('image' in kinds, 'video' in kinds, shape)")
echo "$pk" | grep -q "True True True" && pass "J22 proofs-classify-shape" || fail "J22 proofs ($pk)"

# ---------- J23 no subtasks/deps/hardgate ----------
$CURL -X POST -H "$QS" -d "{\"op\":\"set\",\"id\":\"$P\",\"parent\":\"x\",\"dependsOn\":\"y\",\"hardGate\":true}" "$TODO/todo/update" >/dev/null
noban=$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;t=json.load(sys.stdin)['tasks']['$P'];print(not any(k in t for k in ('parent','dependsOn','hardGate')))")
echo "$TB" | grep -Eqi "add subtask|add a dependency|blocked by|hard gate" && UIBAN=1
[ "$noban" = "True" ] && [ -z "${UIBAN:-}" ] && pass "J23 no-subtasks-deps-hardgate" || fail "J23 subtasks/deps present"

# ---------- J24 verified badge ----------
$CURL -X POST -H "$QS" -d "{\"op\":\"set\",\"id\":\"$U\",\"verified\":true}" "$TODO/todo/update" >/dev/null
[ "$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;print(json.load(sys.stdin)['tasks']['$U']['verified'])")" = "True" ] \
  && echo "$TB" | grep -q "verified" && pass "J24 verified-badge" || fail "J24 verified-badge"

# ---------- J25 retired + strict session resume (no fresh-session fallback) ----------
RT="$HOST/main:retiretest"
mp spawn "$RT" --boss "$BOSS" >/dev/null 2>&1; sleep 5
mp send "$RT" "Reply exactly REVIVE-READY" >/dev/null 2>&1
sid_before=""
for i in $(seq 1 20); do
  sleep 2
  sid_before=$(python3 -c "import json;print(json.load(open('$ID/run/roster.json')).get('$RT',{}).get('session_id',''))" 2>/dev/null)
  if [ -n "$sid_before" ]; then
    if [ "$BACKEND" = "codex" ]; then
      find ~/.codex/sessions -name "*$sid_before.jsonl" -print -quit 2>/dev/null | grep -q . && break
    else
      find ~/.claude/projects -name "$sid_before.jsonl" -print -quit 2>/dev/null | grep -q . && break
    fi
  fi
done
mp kill "$RT" >/dev/null 2>&1; sleep 2
rretired=$($CURL -H "$QS" "$HUD/roster" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['agent_id']=='$RT'];print(a[0]['retired'] if a else 'none')")
rev=$($CURL -X POST -H "$QS" -H 'Content-Type: application/json' -d "{\"agent_id\":\"$RT\"}" "$HUD/revive")
rtid=$(echo "$rev" | python3 -c "import sys,json;print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null)
rok=""; rresult=""
for i in $(seq 1 30); do
  sleep 1
  state=$($CURL -H "$QS" "$HUD/task/$rtid")
  rok=$(echo "$state" | python3 -c "import sys,json;print(json.load(sys.stdin).get('ok'))" 2>/dev/null)
  rresult=$(echo "$state" | python3 -c "import sys,json;print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
  [ "$rok" != "None" ] && [ -n "$rok" ] && break
done
sid_after=$(python3 -c "import json;print(json.load(open('$ID/run/roster.json')).get('$RT',{}).get('session_id',''))" 2>/dev/null)
rrev=$($CURL -H "$QS" "$HUD/roster" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['agent_id']=='$RT'];print(a[0]['retired'] if a else 'none')")
[ "$rretired" = "True" ] && [ "$rok" = "True" ] && [ "$rrev" = "False" ] \
  && [ -n "$sid_before" ] && [ "$sid_before" = "$sid_after" ] \
  && pass "J25 strict-session-resume" \
  || fail "J25 strict-session-resume retired=$rretired revived=$rrev ok=$rok sid_same=$([ "$sid_before" = "$sid_after" ] && echo 1 || echo 0) result=$rresult"
mp kill "$HOST/main:retiretest" >/dev/null 2>&1
# remove phantom from roster
python3 -c "import json,os;p='$ID/run/roster.json';d=json.load(open(p));d.pop('$HOST/main:retiretest',None);d.pop('$HOST/main:guard-x',None);json.dump(d,open(p,'w'))" 2>/dev/null

# ---------- J25a spawn/revive cmd visible in /agents ----------
mp spawn "$HOST/main:eng-scv" --boss "$BOSS" >/dev/null 2>&1; sleep 5
sc=$($CURL -H "$QS" "$HUD/agents" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['agent_id']=='$HOST/main:eng-scv'];print(bool(a and a[0]['spawn_cmd']) and a[0]['revive_cmd']=='mp revive $HOST/main:eng-scv')")
[ "$sc" = "True" ] && pass "J25a spawncmd-in-agents" || fail "J25a spawncmd-in-agents ($sc)"
mp kill "$HOST/main:eng-scv" >/dev/null 2>&1
python3 -c "import json;p='$ID/run/roster.json';d=json.load(open(p));d.pop('$HOST/main:eng-scv',None);json.dump(d,open(p,'w'))" 2>/dev/null

# ---------- J25b runtime isolation + backup ----------
bpath="$ID/todos/board.v2.json"
gitok=0; git -C "$ID" rev-parse >/dev/null 2>&1 && { git -C "$ID" check-ignore todos/board.v2.json >/dev/null 2>&1 && gitok=1; } || gitok=1
$CURL -X POST -H "$QS" -d '{"op":"add","text":"bakA"}' "$TODO/todo/update" >/dev/null
$CURL -X POST -H "$QS" -d '{"op":"add","text":"bakB"}' "$TODO/todo/update" >/dev/null
nbak=$(ls "$ID/todos/"board.v2.json.bak.* 2>/dev/null | wc -l)
# shrink guard test in sandbox
SB=/tmp/mpsb; rm -rf $SB; mkdir -p $SB; cp "$bpath" $SB/board.v2.json
python3 - <<PY
import json
b=json.load(open("$SB/board.v2.json"))
# ensure >5 tasks
for i in range(8): b['tasks']['g%d'%i]={'id':'g%d'%i,'state':'working','text':'x','order':0}
b['order']=list(b['tasks'].keys())
json.dump(b,open("$SB/board.v2.json","w"))
PY
# use the real todo-server save_board() implementation against the sandbox
python3 - <<PY
import sys, importlib.util, os, json
sys.path.insert(0,"$BIN")
spec=importlib.util.spec_from_file_location("ts","$BIN/todo-server.py")
ts=importlib.util.module_from_spec(spec); spec.loader.exec_module(ts)
ts.TODOS_DIR="$SB"; ts.BOARD_PATH="$SB/board.v2.json"
ondisk=json.load(open("$SB/board.v2.json"))
new={'version':2,'order':[],'pinSeq':0,'tasks':{'g0':ondisk['tasks']['g0']}}
before=open(ts.BOARD_PATH,'rb').read()
ok=ts.save_board(new)
after=open(ts.BOARD_PATH,'rb').read()
assert ok is False and before==after
assert any(x.startswith('board.v2.json.SUSPECT.') for x in os.listdir("$SB"))
print("SHRINK_REFUSE", not ok)
PY
[ "$gitok" = "1" ] && [ "$nbak" -ge 1 ] && [[ "$bpath" == "$ID/todos/board.v2.json" ]] && pass "J25b isolation+backup (baks=$nbak)" || fail "J25b isolation+backup gitok=$gitok baks=$nbak"

# ---------- J25c git export + restore + per-instance ----------
rm -rf /tmp/mpx-board /tmp/mpx-repo; mkdir -p /tmp/mpx-board
echo '{"version":2,"order":["a","b","c","d","e","f","g","h"],"pinSeq":0,"tasks":{"a":{"id":"a"},"b":{"id":"b"},"c":{"id":"c"},"d":{"id":"d"},"e":{"id":"e"},"f":{"id":"f"},"g":{"id":"g"},"h":{"id":"h"}}}' > /tmp/mpx-board/board.v2.json
export BOARD_PATH=/tmp/mpx-board/board.v2.json EXPORT_REPO=/tmp/mpx-repo
sha_before=$(sha256sum /tmp/mpx-board/board.v2.json | cut -d' ' -f1)
python3 "$BIN/board-exporter.py" --once >/dev/null 2>&1
c1=$(git -C /tmp/mpx-repo show HEAD:board.v2.json 2>/dev/null | python3 -c "import sys,json;print(len(json.load(sys.stdin)['tasks']))")
sha_after=$(sha256sum /tmp/mpx-board/board.v2.json | cut -d' ' -f1)
# change -> new commit
python3 -c "import json;p='/tmp/mpx-board/board.v2.json';d=json.load(open(p));d['tasks']['i']={'id':'i'};d['order'].append('i');json.dump(d,open(p,'w'))"
python3 "$BIN/board-exporter.py" --once >/dev/null 2>&1
c2=$(git -C /tmp/mpx-repo show HEAD:board.v2.json 2>/dev/null | python3 -c "import sys,json;print(len(json.load(sys.stdin)['tasks']))")
# wipe -> quarantine, HEAD unchanged full
echo '{"version":2,"order":["a"],"pinSeq":0,"tasks":{"a":{"id":"a"}}}' > /tmp/mpx-board/board.v2.json
python3 "$BIN/board-exporter.py" --once >/dev/null 2>&1
c3=$(git -C /tmp/mpx-repo show HEAD:board.v2.json 2>/dev/null | python3 -c "import sys,json;print(len(json.load(sys.stdin)['tasks']))")
susp=$(ls /tmp/mpx-repo/board.v2.json.SUSPECT.* 2>/dev/null | wc -l)
# restore HEAD -> full incl change
python3 "$BIN/board-restore" HEAD >/dev/null 2>&1
c4=$(python3 -c "import json;print(len(json.load(open('/tmp/mpx-board/board.v2.json'))['tasks']))")
pre=$(ls /tmp/mpx-board/board.v2.json.bak.prerestore.* 2>/dev/null | wc -l)
# per-instance path distinct (unset EXPORT_REPO override first so the discriminator is exercised)
unset BOARD_PATH EXPORT_REPO
d1=$(python3 -c "import sys;sys.path.insert(0,'$BIN');import mpcommon;print(mpcommon.export_repo_path({'HOST_ID':'h','TODO_PORT':'9933','INSTALL_DIR':'/a'}))")
d2=$(python3 -c "import sys;sys.path.insert(0,'$BIN');import mpcommon;print(mpcommon.export_repo_path({'HOST_ID':'h','TODO_PORT':'9933','INSTALL_DIR':'/b'}))")
[ "$c1" = "8" ] && [ "$c2" = "9" ] && [ "$c3" = "9" ] && [ "$susp" -ge 1 ] && [ "$c4" = "9" ] && [ "$pre" -ge 1 ] && [ "$sha_before" = "$sha_after" ] && [ "$d1" != "$d2" ] \
  && pass "J25c git-export-restore" || fail "J25c git-export c1=$c1 c2=$c2 c3=$c3 susp=$susp c4=$c4 pre=$pre readonly=$([ "$sha_before" = "$sha_after" ] && echo 1) perinst=$([ "$d1" != "$d2" ] && echo 1)"

# ---------- J28 running tmux config ----------
bi=$(tmux show-options -g base-index 2>/dev/null | awk '{print $2}')
hl=$(tmux show-options -g history-limit 2>/dev/null | awk '{print $2}')
rw=$(tmux show-options -g renumber-windows 2>/dev/null | awk '{print $2}')
et=$(tmux show-options -g escape-time 2>/dev/null | awk '{print $2}')
dt=$(tmux show-options -g default-terminal 2>/dev/null | awk '{print $2}' | tr -d '"')
wheel=$(tmux list-keys -T root 2>/dev/null | grep -c WheelUpPane)
mde=$(tmux list-keys 2>/dev/null | grep -c "MouseDragEnd1Pane.*copy-pipe-and-cancel")
# Dracula proof = the RUNNING status bar is Dracula's (not the default), via its injected #(script) calls.
# (TPM clones dracula/tmux into ~/.tmux/plugins/tmux — repo basename, so don't grep a fixed dir name.)
sr=$(tmux show-options -g status-right 2>/dev/null)
dp=$(tmux show-options -g @dracula-plugins 2>/dev/null | grep -c "cpu-usage")
if echo "$sr" | grep -Eq "cpu_info|ram_info|plugins/tmux/scripts" && [ "$dp" -ge 1 ]; then drac=1; else drac=0; fi
[ "$bi" = "1" ] && [ "$hl" = "50000" ] && [ "$rw" = "on" ] && [ "$et" = "10" ] && [ "$dt" = "tmux-256color" ] \
  && [ "$wheel" = "0" ] && [ "$mde" -ge 1 ] \
  && [ "$drac" = "1" ] \
  && pass "J28 running-tmux-config+Dracula" || fail "J28 tmux bi=$bi hl=$hl rw=$rw et=$et dt=$dt wheel=$wheel mde=$mde drac=$drac"

# ---------- J30 no secret in browser + cookie auth ----------
leak=0
for pg in "$TODO/" "$TODO/todos" "$HUD/dashboard"; do
  $CURL "$pg" | grep -qF "$QUEUE_SECRET" && leak=1
done
sc1=$(curl -sI --max-time 8 "$TODO/" | grep -ic '^set-cookie:')
sc2=$(curl -sI --max-time 8 "$HUD/dashboard" | grep -ic '^set-cookie:')
# cookie auth works, cookieless rejected
cj=/tmp/mpcj.txt; rm -f $cj
curl -s --max-time 8 -c $cj "$TODO/" >/dev/null
cauth=$(curl -s --max-time 8 -b $cj -o /dev/null -w '%{http_code}' "$TODO/todo/board")
cnone=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' "$TODO/todo/board")
[ "$leak" = "0" ] && [ "$sc1" = "1" ] && [ "$sc2" = "1" ] && [ "$cauth" = "200" ] && [ "$cnone" = "401" ] \
  && pass "J30 no-secret+cookie-auth" || fail "J30 leak=$leak sc1=$sc1 sc2=$sc2 cauth=$cauth cnone=$cnone"

# ---------- J35 folder-trust vanilla ----------
if [ "$BACKEND" = "claude" ]; then
  cp ~/.claude.json /tmp/claude.json.bak
  python3 -c "import json,os;p=os.path.expanduser('~/.claude.json');d=json.load(open(p));d['projects']={};json.dump(d,open(p,'w'))"
  # re-run install trust step (mp spawn pretrust) in a fresh cwd
  FR=/tmp/freshcwd-$RANDOM; mkdir -p $FR
  mp spawn "$HOST/main:trust-x" --boss "$BOSS" --cwd "$FR" >/dev/null 2>&1
  sleep 8
  trusted=$(python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.claude.json')));print(d['projects'].get('$FR',{}).get('hasTrustDialogAccepted'))")
  tpane=$(tmux capture-pane -p -t mc-main:trust-x -S -20 2>/dev/null)
  echo "$tpane" | grep -qi "trust this folder\|do you trust" && trustprompt=1
  tstate=$($CURL -H "$QS" "$HUD/agents" | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['agent_id']=='$HOST/main:trust-x'];print(a[0]['state'] if a else 'none')")
  [ "$trusted" = "True" ] && [ -z "${trustprompt:-}" ] && [ "$tstate" = "alive" ] && pass "J35 folder-trust-vanilla" || fail "J35 folder-trust trusted=$trusted prompt=${trustprompt:-0} state=$tstate"
  mp kill "$HOST/main:trust-x" >/dev/null 2>&1
  python3 -c "import json;p='$ID/run/roster.json';d=json.load(open(p));d.pop('$HOST/main:trust-x',None);json.dump(d,open(p,'w'))" 2>/dev/null
  cp /tmp/claude.json.bak ~/.claude.json
else
  codexflags=$(python3 -c "import json;d=json.load(open('$ID/run/roster.json'));print(d.get('$BOSS',{}).get('spawn_cmd',''))")
  echo "$codexflags" | grep -q -- '--backend codex' \
    && pass "J35 codex-backend-recorded" || fail "J35 codex-backend-recorded ($codexflags)"
fi

# ---------- J36 nested spawn no disconnect ----------
before_wins=$(tmux list-windows -t mc-main -F '#{window_name}' | sort | tr '\n' ',')
# attach a ttyd client to Boss window via a grouped session (simulate viewer)
tmux new-session -d -t mc-main -s _vtest \; select-window -t Boss 2>/dev/null
mp spawn "$HOST/main:parent-eng" --boss "$BOSS" >/dev/null 2>&1; sleep 5
# From inside parent-eng, require the model to execute the exact command rather than merely
# discuss it. This proves the spawned agent environment can call mp without driving raw tmux.
mp send "$HOST/main:parent-eng" "Use your Bash tool now to run exactly: mp spawn $HOST/main:eng-child --boss $BOSS --cwd $ID/run/eng . Then reply SPAWNED." >/dev/null 2>&1
child=0
for i in $(seq 1 12); do
  sleep 4
  tmux list-windows -t mc-main -F '#{window_name}' 2>/dev/null | grep -qx eng-child && { child=1; break; }
done
has=$(tmux has-session -t mc-main 2>/dev/null && echo 1 || echo 0)
vtest=$(tmux list-clients -t mc-main 2>/dev/null | grep -c _vtest || echo 0)
bosswin=$(tmux list-windows -t mc-main -F '#{window_name}' | grep -qx Boss && echo 1 || echo 0)
tmux kill-session -t _vtest 2>/dev/null
[ "$child" = "1" ] && [ "$has" = "1" ] && [ "$bosswin" = "1" ] && pass "J36 nested-spawn-no-disconnect" || fail "J36 nested-spawn child=$child has=$has boss=$bosswin"
mp kill "$HOST/main:parent-eng" >/dev/null 2>&1; mp kill "$HOST/main:eng-child" >/dev/null 2>&1
python3 -c "import json;p='$ID/run/roster.json';d=json.load(open(p));[d.pop(k,None) for k in ('$HOST/main:parent-eng','$HOST/main:eng-child')];json.dump(d,open(p,'w'))" 2>/dev/null

# ---------- J37 pinning (uncapped, insertion order, persist) ----------
declare -a PT=()
for i in 1 2 3 4 5 6; do
  pid=$($CURL -X POST -H "$QS" -d "{\"op\":\"add\",\"text\":\"pin$i\"}" "$TODO/todo/update" | jqget "d['id']")
  PT+=("$pid")
  CREATED+=("$pid")
done
for pid in "${PT[@]}"; do $CURL -X POST -H "$QS" -d "{\"op\":\"pin\",\"id\":\"$pid\"}" "$TODO/todo/update" >/dev/null; done
# all 6 pinned, insertion order by pinRank
porder=$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "
import sys,json
b=json.load(sys.stdin)
pins=sorted([t for t in b['tasks'].values() if t.get('pinned')],key=lambda x:x['pinRank'])
ids=[t['id'] for t in pins]
want='${PT[*]}'.split()
# check our 6 are all pinned and in insertion order among themselves
sub=[i for i in ids if i in want]
print(len([t for t in b['tasks'].values() if t.get('pinned')]), sub==want)")
echo "$porder" | grep -q "True" && [ "$(echo $porder | awk '{print $1}')" -ge 6 ] && pass "J37 pinning-uncapped" || fail "J37 pinning ($porder)"
# unpin restores + persist across restart
$CURL -X POST -H "$QS" -d "{\"op\":\"unpin\",\"id\":\"${PT[0]}\"}" "$TODO/todo/update" >/dev/null
up=$($CURL -H "$QS" "$TODO/todo/board" | python3 -c "import sys,json;t=json.load(sys.stdin)['tasks']['${PT[0]}'];print(t['pinned'],t['pinRank'])")
[ "$up" = "False None" ] && pass "J37b unpin-restore" || fail "J37b unpin ($up)"

# ---------- J39-J44 removed: external messaging bridge stripped from this build ----------

# ---------- J48 boss quickstart (static) ----------
grep -q "mp send" "$ID/boss-CLAUDE.md" && grep -q "mp spawn" "$ID/boss-CLAUDE.md" && grep -qi "todo/comment" "$ID/boss-CLAUDE.md" && grep -q "todo/board" "$ID/boss-CLAUDE.md" \
  && pass "J48 boss-quickstart-static" || fail "J48 boss-quickstart-static"

echo ""
echo "===== deterministic gates done: $PASSES passed, ${#FAILS[@]} failed ====="
[ ${#FAILS[@]} -gt 0 ] && printf 'FAILED: %s\n' "${FAILS[@]}"

# ---------- browser gates J31/J33/J34/J38/J45/J46/J49 ----------
echo "===== browser suite (webkit+chromium) ====="
BROWSER_VERIFY_DIR="${MYPEOPLE_BROWSER_VERIFY_DIR:-$ID/verify}"
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update >/dev/null && apt-get install -y nodejs npm >/dev/null
  fi
fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  fail "J31/J49 browser-suite (node/npm unavailable)"
else
  browser_ready=1
  if [ ! -d "$BROWSER_VERIFY_DIR/node_modules/playwright" ]; then
    (cd "$BROWSER_VERIFY_DIR" && npm ci --no-audit --no-fund --silent) || browser_ready=0
  fi
  if [ "$browser_ready" = "1" ]; then
    (cd "$BROWSER_VERIFY_DIR" && npx playwright install chromium webkit >/dev/null) || browser_ready=0
  fi
  if [ "$browser_ready" = "1" ]; then
    node "$BROWSER_VERIFY_DIR/browser.mjs" 2>&1 | tee "$ID/verify/browser.out"
    brc=${PIPESTATUS[0]}
    [ "$brc" = "0" ] && pass "J31/J49 browser-suite" || fail "J31/J49 browser-suite (rc=$brc)"
  else
    fail "J31/J49 browser-suite (Playwright install failed)"
  fi
fi

echo ""
echo "===== FINAL: $PASSES passed, ${#FAILS[@]} failed ====="
if [ ${#FAILS[@]} -gt 0 ]; then printf 'FAILED: %s\n' "${FAILS[@]}"; exit 1; fi
echo "ALL GATES GREEN"
exit 0
