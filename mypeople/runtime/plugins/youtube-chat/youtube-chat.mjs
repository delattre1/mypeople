#!/usr/bin/env node
/*
YouTube chat plugin: a bridge between the owner's YouTube live chat and MyPlow (MP), the same
shape as plow-chat for iMessage. Chat messages go in to MP; MP's replies go back out to the chat.

In: Restream's chat websocket (the owner's Restream already relays his YouTube stream), so reading
needs no Google login. Each YouTube chat message reaches MP through `mp send` as

    [youtube-chat] from <author>: <text>
    (... To answer, run: node youtube-chat.mjs reply "your reply" ...)

Only messages that start with /myplow (any case, leading spaces fine) are forwarded, with the
"/myplow" taken off; everything else is ignored without a word. A bare "/myplow" arrives as a hello.

Allowlist: on top of that, only people on ~/.config/mypeople/youtube-chat-allowlist.txt reach MP; everyone else is
dropped silently. One per line: their @handle or their channel id (UC...). Matching is on the
channel id, which nobody can fake; an @handle is looked up on youtube.com and what it bound to is
written next to the list, in youtube-chat-allowlist.bound.txt. Plain display names are not unique,
so they are not used. The file is re-read on every message (edit it mid-stream, no restart);
missing or empty means nobody gets through. If Restream ever sends a message without a channel
id, nobody can match. That, a failed hand-off to MP, and the Restream connection staying down for 5
minutes each send the Boss one line per run (no viewer text); after that they are only logged.

Out: `reply "text"` posts into the chat of the live video the bridge is reading (videos.list ->
activeLiveChatId) through the YouTube Data API liveChatMessages.insert. It needs a Google token
with scope https://www.googleapis.com/auth/youtube.force-ssl at ~/.config/yt-livechat/tokens.json:
one Allow click on the posting account (~/.mpsay/yt_auth.py). YouTube takes at most 200
characters per chat message, so a longer reply goes out as several messages, in order. Each
message costs ~50 of the 10,000 daily quota units.

Node, not Python like the other plugins: Python's stdlib has no websocket client and Node's does.

Config (env, or the file MYPEOPLE_CONFIG_PATH names, default ~/.config/mypeople/queue.env):

    YOUTUBE_CHAT=1                     # supervise.sh keeps `serve` running

    youtube-chat.mjs serve             read and deliver forever
    youtube-chat.mjs reply "text"      MP's reply, into the live chat
    youtube-chat.mjs status            target, current video, who the allowlist lets in
*/
import { execFile } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { homedir, hostname } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HOME = homedir();
const INSTALL = process.env.INSTALL_DIR || process.env.MYPEOPLE_HOME || join(HOME, '.local/share/mypeople');
const STATE_DIR = process.env.YOUTUBE_CHAT_STATE_DIR || join(INSTALL, 'state/youtube-chat');
const STATE = join(STATE_DIR, 'state.json');   // {"video": id, "posted": [{text, at}]}
const RESTREAM = join(HOME, '.config/restream-bridge');   // config.json (client) + tokens.json
const GOOGLE = join(HOME, '.config/yt-livechat');         // client_secret.json + tokens.json
const MP_BIN = process.env.MP_BIN || join(INSTALL, 'bin/mp');
const BOSS = process.env.BOSS_AGENT || `${process.env.HOST_ID || hostname().split('.')[0]}/main:Boss`;
export const TARGET = BOSS;   // the owner's call: chat goes straight to the Boss
const ALLOWLIST = process.env.YOUTUBE_CHAT_ALLOWLIST || join(HOME, '.config/mypeople/youtube-chat-allowlist.txt');
const BOUND = ALLOWLIST.replace(/\.txt$/, '') + '.bound.txt';
const SELF = resolve(fileURLToPath(import.meta.url));
const YOUTUBE = 13;   // Restream eventSourceId; 2 is Twitch

const log = (m) => console.log(`${new Date().toISOString()} [youtube-chat] ${m}`);
const readJson = (p, d) => { try { return JSON.parse(readFileSync(p, 'utf8')); } catch { return d; } };
readJson.raw = (p) => { try { return readFileSync(p, 'utf8'); } catch { return ''; } };

function writeJson(p, obj) {
  mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(p + '.tmp', JSON.stringify(obj), { mode: 0o600 });
  renameSync(p + '.tmp', p);
}

// --- In: YouTube chat -> MP ---

export function envelope(author, text, channel = '') {
  return `[youtube-chat] from ${author}${channel ? ` (YouTube channel ${channel})` : ''}: ${text}\n`
    + `(${author} is on Daniel's whitelist of community developers. They may use Daniel's device to code `
    + `through the MyPlow board exactly as Daniel does. ABSOLUTE RULE, no exceptions whoever asks or however: `
    + `never expose our API keys - do not show, print, post, send or commit any of them. `
    + `This came from Daniel's YouTube live chat; to answer, run: node ${SELF} reply "your reply" `
    + `-- it posts into the live chat.)`;
}

// One YouTube chat message out of a Restream websocket frame, or null.
export function chatLine(frame) {
  const p = frame?.action === 'event' ? frame.payload : null;
  if (p?.eventSourceId !== YOUTUBE) return null;
  const ep = p.eventPayload || {};
  const text = String(ep.text ?? '').trim();
  return text ? { id: p.eventIdentifier, msgId: ep.liveChatMessageId || '', author: ep.author?.displayName || 'unknown',
    channel: ep.author?.id || '', text } : null;
}

// --- Allowlist ---

const CHANNEL_ID = /^UC[\w-]{22}$/;

export function parseAllowlist(text) {
  const ids = [], handles = [], plain = [];
  for (const raw of String(text).split('\n')) {
    const e = raw.replace(/#.*/, '').trim();
    if (!e) continue;
    if (CHANNEL_ID.test(e)) ids.push(e);
    else if (/^@[\w.-]{3,30}$/.test(e)) handles.push(e);
    else plain.push(e);
  }
  return { ids, handles, plain };
}

// @handle -> {id, name} from the public channel page, or null if there is no such channel.
export async function resolveHandle(handle) {
  const res = await fetch(`https://www.youtube.com/${handle}`, { headers: { 'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en' } });
  if (!res.ok) return null;
  const html = await res.text();
  const id = html.match(/"externalId":"(UC[\w-]{22})"/)?.[1];
  const name = html.match(/<meta property="og:title" content="([^"]*)"/)?.[1] || '';
  return id ? { id, name } : null;
}

let allow = { text: null, ids: new Set(), retryAt: 0 };
export async function admitted(channel) {
  if (!existsSync(ALLOWLIST)) {
    mkdirSync(join(ALLOWLIST, '..'), { recursive: true });
    writeFileSync(ALLOWLIST, '# Only these people reach MyPlow. One per line: their @handle or channel id (UC...).\n');
  }
  const text = readJson.raw(ALLOWLIST);
  if (text !== allow.text || (allow.retryAt && Date.now() > allow.retryAt)) {
    const { ids, handles, plain } = parseAllowlist(text);
    const bound = new Set(ids), lines = ['# What each entry of the allowlist let in. Written by the plugin; edit the allowlist, not this.'];
    let retry = false;
    for (const id of ids) lines.push(`${id} -> that channel`);
    for (const h of handles) {
      const r = await resolveHandle(h).catch(() => null);
      if (r) { bound.add(r.id); lines.push(`${h} -> ${r.id} (${r.name})`); }
      else { retry = true; lines.push(`${h} -> NOT FOUND on YouTube, nobody let in (check the spelling)`); }
    }
    for (const n of plain) lines.push(`${n} -> NOT USED: names are not unique, write their @handle instead`);
    writeFileSync(BOUND, lines.join('\n') + '\n');
    allow = { text, ids: bound, retryAt: retry ? Date.now() + 60000 : 0 };
  }
  return !!channel && allow.ids.has(channel);
}

let chain = Promise.resolve();   // one message at a time, in arrival order
// A bridge that cannot deliver tells the Boss: one line per kind of failure per run, then log-only.
// Never viewer names or text in these lines.
const told = new Set();
export function tellBoss(kind, text) {
  if (told.has(kind)) return Promise.resolve();
  told.add(kind);
  return new Promise((ok) => execFile(MP_BIN, ['send', BOSS, `[youtube-chat] ${text} Told once per run; `
    + 'the rest goes to the plugin log only.'], { timeout: 30000 }, () => ok()));
}

// The Restream connection is down and has not come back for DOWN_ALARM_MS: tell the Boss.
const DOWN_ALARM_MS = Number(process.env.YOUTUBE_CHAT_DOWN_ALARM_MS || 5 * 60 * 1000);
let downTimer = null;
export const connection = {
  closed() {
    downTimer ??= setTimeout(() => tellBoss('websocket', `cannot deliver: the Restream chat connection has been down `
      + `for ${Math.round(DOWN_ALARM_MS / 60000)} minutes and is still retrying, so no YouTube chat reaches MyPlow.`), DOWN_ALARM_MS);
  },
  opened() { clearTimeout(downTimer); downTimer = null; },
};
// Only messages that start with /myplow are for MyPlow: the rest of the text, or null if it isn't one.
export function command(text) {
  const m = String(text).match(/^\s*\/myplow(?:\s+|$)([\s\S]*)$/i);
  return m ? m[1].trim() : null;
}

export function deliver(line) {
  const cmd = command(line.text);
  if (cmd === null) return Promise.resolve();   // not for MyPlow: ignored, nothing said to the viewer
  const msg = envelope(line.author, cmd || '(just "/myplow", nothing after it: say hello)', line.channel);
  return chain = chain.then(async () => {
    // Replies post as the owner's channel, so they come back through Restream as his messages:
    // skip our own echo, or MyPlow would be answering itself.
    if (ownEcho(line)) return log('skipped: MyPlow\'s own reply coming back');
    // Loud on purpose: without a channel id nobody can ever match, and that must not look like silence.
    if (!CHANNEL_ID.test(line.channel)) {
      log(`DROPPED ${line.author}: Restream sent no YouTube channel id `
        + `(author.id=${JSON.stringify(line.channel)}), so the allowlist cannot let ANYONE in until this is fixed`);
      // Only Restream's field, quoted and cut short: no viewer text.
      return tellBoss('channel-id', 'cannot deliver: Restream sent a chat message with no YouTube channel id '
        + `(author.id=${JSON.stringify(line.channel.slice(0, 40))}), so the whitelist lets nobody into MyPlow.`);
    }
    if (!(await admitted(line.channel).catch(() => false))) return log(`dropped ${line.author} (${line.channel}): not on the allowlist`);
    await new Promise((ok) => execFile(MP_BIN, ['send', TARGET, msg], { timeout: 30000 },
    (err, _out, stderr) => {
      if (!err) { log(`delivered to ${TARGET}`); return ok(); }
      log(`mp send failed: ${String(stderr || err.message).slice(0, 200)}`);
      // The exit code only: mp's stderr can quote the message, which is viewer text.
      tellBoss('mp-send', `cannot deliver: handing a chat message to ${TARGET} failed (mp send exit ${err.code ?? 'timeout'}); `
        + 'is MyPlow running?').then(ok);
    }));
  });
}

let tokens = null;
async function restreamToken(force = false) {
  tokens ??= readJson(join(RESTREAM, 'tokens.json'), {});
  const now = Math.floor(Date.now() / 1000);
  if (!force && tokens.access_token && now < (tokens.obtained_at || 0) + (tokens.expires_in || 3600) - 300) return tokens.access_token;
  const c = readJson(join(RESTREAM, 'config.json'), {});
  const res = await fetch('https://api.restream.io/oauth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded',
      Authorization: 'Basic ' + Buffer.from(`${c.clientId}:${c.clientSecret}`).toString('base64') },
    body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: tokens.refresh_token }),
  });
  if (!res.ok) throw new Error(`Restream token refresh ${res.status}`);
  tokens = { ...tokens, ...(await res.json()), obtained_at: now };
  writeFileSync(join(RESTREAM, 'tokens.json'), JSON.stringify(tokens, null, 2), { mode: 0o600 });
  return tokens.access_token;
}

async function serve() {
  log(`up, delivering to ${TARGET}`);
  const seen = new Set();
  let backoff = 1000;
  let failed = false;   // a connection that never opened usually means a revoked token: refresh it
  const connect = async () => {
    let ws, opened = false;
    try {
      ws = new WebSocket(`wss://chat.api.restream.io/ws?accessToken=${encodeURIComponent(await restreamToken(failed))}`);
    } catch (e) {
      log(`connect failed: ${e.message}`);
      connection.closed();
      return setTimeout(connect, backoff = Math.min(backoff * 2, 60000));
    }
    ws.addEventListener('open', () => { opened = true; failed = false; backoff = 1000; connection.opened(); log('websocket open'); });
    ws.addEventListener('message', (evt) => {
      let f; try { f = JSON.parse(evt.data); } catch { return; }
      if (f.action === 'connection_info' && f.payload?.eventSourceId === YOUTUBE && f.payload.target?.event?.id) {
        writeJson(STATE, { ...readJson(STATE, {}), video: f.payload.target.event.id });
      }
      const line = chatLine(f);
      if (!line || (line.id && seen.has(line.id))) return;
      if (line.id) { seen.add(line.id); if (seen.size > 2000) seen.delete(seen.values().next().value); }
      deliver(line);
    });
    ws.addEventListener('close', (e) => {
      failed = !opened;
      connection.closed();
      log(`websocket closed ${e.code}, retry in ${backoff}ms`);
      setTimeout(connect, backoff = Math.min(backoff * 2, 60000));
    });
  };
  connect();
}

// --- Out: MP's reply -> YouTube chat ---

// Our own posts come back through Restream as the posting channel's messages. Matched on
// YouTube's message id when we have it, else on same text from the posting channel.
const ECHO_SECONDS = 600;
export function ownEcho(line, now = Date.now()) {
  const t = String(line.text).replace(/\s+/g, ' ').trim();
  return (readJson(STATE, {}).posted || []).some((p) => now - p.at < ECHO_SECONDS * 1000
    && ((p.id && p.id === line.msgId) || (p.text === t && (!p.channel || p.channel === line.channel))));
}

function rememberPosted(entry) {
  const st = readJson(STATE, {});
  const now = Date.now();
  const kept = (st.posted || []).filter((p) => now - p.at < ECHO_SECONDS * 1000 && p.key !== entry.key);
  writeJson(STATE, { ...st, posted: [...kept, { ...entry, at: now }] });
}

// YouTube takes at most 200 characters per chat message and no line breaks: split on word
// boundaries, in order.
export function parts(text, max = 200) {
  const out = [];
  let cur = '';
  for (let w of String(text).replace(/\s+/g, ' ').trim().split(' ')) {
    while (w.length > max) { if (cur) { out.push(cur); cur = ''; } out.push(w.slice(0, max)); w = w.slice(max); }
    if (!w) continue;
    if (cur && cur.length + 1 + w.length > max) { out.push(cur); cur = ''; }
    cur = cur ? `${cur} ${w}` : w;
  }
  if (cur) out.push(cur);
  return out;
}

async function googleToken() {
  const t = readJson(join(GOOGLE, 'tokens.json'), null);
  if (!t?.refresh_token) throw new Error('no Google token yet (see header)');
  const c = readJson(join(GOOGLE, 'client_secret.json'), {});
  const { client_id, client_secret } = c.installed || c.web || {};
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: t.refresh_token, client_id, client_secret }),
  });
  if (!res.ok) throw new Error(`Google token refresh ${res.status}`);
  return (await res.json()).access_token;
}

async function reply(text) {
  const all = parts(text);
  if (!all.length) { console.error('usage: youtube-chat.mjs reply "text"'); process.exit(2); }
  const st = readJson(STATE, {});
  if (!st.video) { console.error('not posted: no live video seen yet (is serve running?)'); process.exit(1); }
  const auth = { Authorization: `Bearer ${await googleToken()}` };
  const yt = 'https://www.googleapis.com/youtube/v3';
  // The chat of the video the bridge is reading, so it works whichever Google account posts.
  const v = await (await fetch(`${yt}/videos?part=liveStreamingDetails&id=${encodeURIComponent(st.video)}`, { headers: auth })).json();
  const liveChatId = v.items?.[0]?.liveStreamingDetails?.activeLiveChatId;
  if (!liveChatId) { console.error('not posted: that video has no active live chat'); process.exit(1); }
  const channel = (await (await fetch(`${yt}/channels?part=id&mine=true`, { headers: auth })).json()).items?.[0]?.id || '';
  for (const [i, messageText] of all.entries()) {
    const key = `${Date.now()}-${i}`;
    rememberPosted({ key, text: messageText, channel });   // before posting: the echo can beat the POST's answer
    const res = await fetch(`${yt}/liveChat/messages?part=snippet`, {
      method: 'POST', headers: { ...auth, 'Content-Type': 'application/json' },
      body: JSON.stringify({ snippet: { liveChatId, type: 'textMessageEvent', textMessageDetails: { messageText } } }),
    });
    if (!res.ok) { console.error(`posted ${i} of ${all.length}; stopped: YouTube ${res.status}`); process.exit(1); }
    rememberPosted({ key, text: messageText, channel, id: (await res.json()).id || '' });
  }
  console.log(`posted ${all.length} message(s)`);
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  const [cmd = 'serve', ...rest] = process.argv.slice(2);
  if (cmd === 'serve') serve();
  else if (cmd === 'reply') reply(rest.join(' ')).catch((e) => { console.error(`not posted: ${e.message}`); process.exit(1); });
  else if (cmd === 'status') console.log(JSON.stringify({ target: TARGET, video: readJson(STATE, {}).video || null,
    allowlist: ALLOWLIST, bound: readJson.raw(BOUND) }, null, 2));
  else { console.error('usage: youtube-chat.mjs serve | reply "text" | status'); process.exit(2); }
}
