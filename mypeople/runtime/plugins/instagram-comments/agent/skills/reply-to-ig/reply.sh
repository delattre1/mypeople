#!/usr/bin/env bash
# reply.sh <ig_comment_id> <text> — reply publicly UNDER an Instagram comment as @danedelattre.
# Graph API POST /{comment_id}/replies. Token in ~/.config/seedbed/meta-ads.env (never in repo).
# Mirrors ~/.claude/skills/reply-to-x/reply.sh.
set -euo pipefail
CID="${1:?ig_comment_id required}"
TEXT="${2:?reply text required}"

# Guards
if [[ ${#TEXT} -gt 2200 ]]; then
  echo "IG_REPLY_REJECTED reason=too_long"
  exit 2
fi
# Never post a secret. Meta user tokens start EAA; anything else long+base64-ish is suspect.
if [[ "$TEXT" =~ EAA[A-Za-z0-9]{20,} ]] || [[ "$TEXT" =~ [A-Za-z0-9_-]{40,} ]]; then
  echo "IG_REPLY_REJECTED reason=looks_like_a_secret"
  exit 2
fi

# The container gets the token as env; a host install keeps it in a file.
ENV_FILE="${META_ENV:-$HOME/.config/seedbed/meta-ads.env}"
# shellcheck disable=SC1090
[[ -n "${META_ACCESS_TOKEN:-}" || ! -f "$ENV_FILE" ]] || { set -a; . "$ENV_FILE"; set +a; }
: "${META_ACCESS_TOKEN:?META_ACCESS_TOKEN missing}"

# Rate limit: min seconds between replies (file mtime), same shape as the X skill.
RL_FILE="${IG_RATE_LIMIT_FILE:-$HOME/.config/seedbed/ig-last-reply}"
RL_SEC="${IG_RATE_LIMIT_SEC:-3}"
if [[ -f "$RL_FILE" ]]; then
  now=$(date +%s)
  last=$(stat -f %m "$RL_FILE" 2>/dev/null || stat -c %Y "$RL_FILE" 2>/dev/null || echo 0)
  last=${last:-0}
  delta=$((now - last))
  if (( delta < RL_SEC )); then
    echo "IG_REPLY_REJECTED reason=rate_limit wait=$((RL_SEC - delta))s"
    exit 3
  fi
fi

resp=$(curl -sS -X POST "https://graph.facebook.com/v21.0/${CID}/replies" \
  --data-urlencode "message=${TEXT}" \
  --data-urlencode "access_token=${META_ACCESS_TOKEN}")

# Never echo the raw body: an error payload can quote the request, token included.
echo "$resp" | sed 's/EAA[A-Za-z0-9_-]*/<redacted>/g'
if echo "$resp" | grep -q '"id"'; then
  mkdir -p "$(dirname "$RL_FILE")"
  touch "$RL_FILE"
  echo "IG_REPLY_SENT"
  echo "$resp" | python3 -c "import sys,json; print('IG_REPLY_ID='+json.load(sys.stdin).get('id',''))" 2>/dev/null || true
else
  echo "IG_REPLY_FAILED"
  exit 1
fi
