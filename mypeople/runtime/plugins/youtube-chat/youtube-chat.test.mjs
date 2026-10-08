// node youtube-chat.test.mjs -- no network, no real mp, nothing posted.
import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'yt-chat-test-'));
// A stub mp that records its argv, one JSON line per call.
const stub = join(dir, 'mp');
// It exits 1 for chat deliveries while <dir>/fail exists, to simulate the target not being there.
writeFileSync(stub, `#!/usr/bin/env node\nconst fs = require('fs'); fs.appendFileSync(${JSON.stringify(join(dir, 'argv'))}, JSON.stringify(process.argv.slice(2)) + '\\n');\n`
  + `if (fs.existsSync(${JSON.stringify(join(dir, 'fail'))}) && process.argv[4].startsWith('[youtube-chat] from')) process.exit(1);\n`);
chmodSync(stub, 0o755);
const LIST = join(dir, 'allow.txt');
Object.assign(process.env, { YOUTUBE_CHAT_STATE_DIR: dir, MP_BIN: stub, YOUTUBE_CHAT_ALLOWLIST: LIST,
  YOUTUBE_CHAT_DOWN_ALARM_MS: '50' });   // read at import
const { TARGET, admitted, chatLine, command, connection, deliver, envelope, ownEcho, parseAllowlist, parts } = await import('./youtube-chat.mjs');
const bossLines = () => readFileSync(join(dir, 'argv'), 'utf8').trim().split('\n').map((l) => JSON.parse(l)).filter((c) => /^\[youtube-chat\] cannot deliver/.test(c[2]));
// Fake youtube.com: @ana is a real channel, anything else 404s. Counts lookups.
let lookups = 0;
globalThis.fetch = async (url) => { lookups++; return url.endsWith('/@ana')
  ? new Response('<meta property="og:title" content="Ana Silva">..."externalId":"UCaaaaaaaaaaaaaaaaaaaaaa"...')
  : new Response('', { status: 404 }); };
const ANA = 'UCaaaaaaaaaaaaaaaaaaaaaa', BOB = 'UCbbbbbbbbbbbbbbbbbbbbbb';

try {
  // In: YouTube chat messages, not Twitch or blanks, reach MyPlow as `mp send <MP> <envelope>`.
  assert.match(TARGET, /\/main:Boss$/);
  const frame = (sid, text) => ({ action: 'event', payload: { eventSourceId: sid, eventIdentifier: 'e1',
    eventPayload: { author: { displayName: 'Ana' }, text } } });
  assert.deepEqual(chatLine(frame(13, 'oi')), { id: 'e1', msgId: '', author: 'Ana', channel: '', text: 'oi' });
  const withId = frame(13, 'oi'); withId.payload.eventPayload.author.id = ANA;
  assert.equal(chatLine(withId).channel, ANA);
  assert.equal(chatLine(frame(2, 'twitch')), null);
  assert.equal(chatLine(frame(13, '  ')), null);
  assert.equal(chatLine({ action: 'heartbeat' }), null);
  assert.ok(envelope('Ana', 'oi').startsWith('[youtube-chat] from Ana: oi\n'));
  assert.ok(envelope('Ana', 'oi', ANA).startsWith(`[youtube-chat] from Ana (YouTube channel ${ANA}): oi\n`), 'verified channel id next to the name');
  assert.match(envelope('Ana', 'oi'), /youtube-chat\.mjs reply "your reply"/);
  // Allowlist: missing file = nobody (and it is created for him to edit).
  assert.equal(await admitted(ANA), false);
  assert.match(readFileSync(LIST, 'utf8'), /@handle or channel id/);
  assert.deepEqual(parseAllowlist('# c\n@ana\nUCbbbbbbbbbbbbbbbbbbbbbb  # bob\nAna Silva\n\n'),
    { ids: [BOB], handles: ['@ana'], plain: ['Ana Silva'] });
  writeFileSync(LIST, '@ana\nAna Silva\n@nobody-here\n');
  assert.equal(await admitted(ANA), true, '@handle binds to its channel id');
  assert.equal(await admitted(BOB), false);
  assert.equal(await admitted(''), false, 'no channel id = not admitted');
  const bound = readFileSync(join(dir, 'allow.bound.txt'), 'utf8');
  assert.match(bound, /@ana -> UCaaaaaaaaaaaaaaaaaaaaaa \(Ana Silva\)/);
  assert.match(bound, /Ana Silva -> NOT USED/);
  assert.match(bound, /@nobody-here -> NOT FOUND/);
  const n = lookups; await admitted(ANA); assert.equal(lookups, n, 'unchanged list is not looked up again');
  writeFileSync(LIST, BOB + '\n');   // edited mid-stream, no restart
  assert.equal(await admitted(BOB), true);
  assert.equal(await admitted(ANA), false, 'removed person is out at once');

  // Only listed people are delivered; the rest are dropped without a word.
  const quiet = console.log; console.log = () => {};
  await deliver({ author: 'Ana', channel: ANA, text: '/myplow let me in' });
  await deliver({ author: 'Bob', channel: BOB, text: '/myplow build me a todo app' });
  const logged = []; console.log = (m) => logged.push(m);
  await deliver({ author: 'Bob', channel: '', text: '/myplow renamed stranger' });
  await deliver({ author: 'Cy', channel: 'UCcccccccccccccccccccccc', text: '/myplow not listed' });
  await deliver({ author: 'Dee', channel: 'not-a-channel', text: '/myplow second missing id' });
  console.log = quiet;
  assert.match(logged[0], /DROPPED Bob: Restream sent no YouTube channel id .* cannot let ANYONE in/);
  assert.match(logged[1], /dropped Cy \(UCcccccccccccccccccccccc\): not on the allowlist/);
  assert.match(logged[2], /DROPPED Dee/, 'every missing id is still logged');
  // ...and the Boss hears it exactly once per run, with no viewer text.
  const sent = readFileSync(join(dir, 'argv'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const toBoss = sent.filter((c) => /^\[youtube-chat\] cannot deliver/.test(c[2]));
  assert.equal(toBoss.length, 1);
  assert.match(toBoss[0][2], /^\[youtube-chat\] cannot deliver: .*no YouTube channel id/);
  assert.ok(!/Bob|renamed stranger|Dee/.test(toBoss[0][2]), 'no viewer name or text reaches the Boss');
  assert.deepEqual(sent.filter((c) => c[1] === TARGET && c[2].startsWith('[youtube-chat] from')), [['send', TARGET, envelope('Bob', 'build me a todo app', BOB)]]);

  // MyPlow's own reply comes back as the owner's message: skipped by message id, or by same text
  // from the posting channel; a viewer typing the same words is not an echo; old posts expire.
  const now0 = Date.now();
  writeFileSync(join(dir, 'state.json'), JSON.stringify({ posted: [
    { key: 'a', text: '/myplow sure, on it', channel: BOB, id: 'MSG1', at: now0 },
    { key: 'b', text: 'old', channel: BOB, at: now0 - 700000 }] }));
  console.log = () => {};
  await deliver({ author: 'Bob', channel: BOB, msgId: 'zzz', text: '/myplow sure,  on it' });
  console.log = quiet;
  assert.equal(readFileSync(join(dir, 'argv'), 'utf8').trim().split('\n').filter((l) => JSON.parse(l)[1] === TARGET && JSON.parse(l)[2].startsWith('[youtube-chat] from')).length, 1, 'echo not delivered');
  assert.equal(ownEcho({ msgId: 'MSG1', channel: BOB, text: 'paraphrased differently' }), true, 'message id match wins');
  assert.equal(ownEcho({ msgId: '', channel: BOB, text: '/myplow sure, on it' }), true, 'same text from the posting channel');
  assert.equal(ownEcho({ msgId: '', channel: ANA, text: '/myplow sure, on it' }), false, 'a viewer saying the same words is not an echo');
  assert.equal(ownEcho({ msgId: '', channel: BOB, text: 'old' }), false, 'expired after 10 minutes');
  assert.deepEqual(chatLine({ action: 'event', payload: { eventSourceId: 13, eventIdentifier: 'e9',
    eventPayload: { author: { displayName: 'A', id: ANA }, text: 'x', liveChatMessageId: 'M9' } } }).msgId, 'M9');

  // A failed hand-off to MyPlow tells the Boss once per run (no viewer text), then logs only.
  writeFileSync(join(dir, 'fail'), '');
  console.log = () => {};
  await deliver({ author: 'Bob', channel: BOB, text: '/myplow secret viewer words one' });
  await deliver({ author: 'Bob', channel: BOB, text: '/myplow secret viewer words two' });
  console.log = quiet;
  rmSync(join(dir, 'fail'));
  const mpAlarm = bossLines().filter((c) => /handing a chat message/.test(c[2]));
  assert.equal(mpAlarm.length, 1);
  assert.ok(!/secret viewer words|Bob/.test(mpAlarm[0][2]));

  // The Restream connection: a quick reconnect is silent; staying down tells the Boss once.
  const wsAlarms = () => bossLines().filter((c) => /connection has been down/.test(c[2])).length;
  connection.closed(); connection.opened();
  await new Promise((r) => setTimeout(r, 120));
  assert.equal(wsAlarms(), 0, 'recovered in time: no alarm');
  connection.closed();
  await new Promise((r) => setTimeout(r, 300));
  connection.opened(); connection.closed();
  await new Promise((r) => setTimeout(r, 300));
  connection.opened();
  assert.equal(wsAlarms(), 1, 'down too long: told once per run');

  // Only /myplow messages, prefix stripped, any case, leading spaces; bare /myplow is a hello.
  assert.equal(command('/myplow what is the weather'), 'what is the weather');
  assert.equal(command('  /MyPlow   build it'), 'build it');
  assert.equal(command('/myplow'), '');
  assert.equal(command('/MYPLOW  '), '');
  for (const no of ['what is the weather', 'hey /myplow do it', '/myplowx hi', '/plow hi', ''])
    assert.equal(command(no), null, `not a command: ${no}`);
  const before = readFileSync(join(dir, 'argv'), 'utf8').trim().split('\n').filter((l) => JSON.parse(l)[1] === TARGET && JSON.parse(l)[2].startsWith('[youtube-chat] from')).length;
  console.log = () => {};
  await deliver({ author: 'Bob', channel: BOB, text: 'no prefix, from a listed person' });
  await deliver({ author: 'Cy', channel: 'UCcccccccccccccccccccccc', text: '/myplow prefix but not listed' });
  await deliver({ author: 'Bob', channel: BOB, text: ' /MyPlow   what is the weather' });
  await deliver({ author: 'Bob', channel: BOB, text: '/myplow' });
  console.log = quiet;
  const toMp = readFileSync(join(dir, 'argv'), 'utf8').trim().split('\n').map((l) => JSON.parse(l)).filter((c) => c[1] === TARGET && c[2].startsWith('[youtube-chat] from'));
  assert.deepEqual(toMp.slice(before).map((c) => c[2]),
    [envelope('Bob', 'what is the weather', BOB), envelope('Bob', '(just "/myplow", nothing after it: say hello)', BOB)],
    'both gates must pass; prefix stripped');

  // Out: long replies become several chat messages, each <=200 (YouTube's limit), nothing lost, in order.
  const long = Array.from({ length: 120 }, (_, i) => `word${i}`).join(' ') + '\nnext line ' + 'z'.repeat(450);
  const ps = parts(long);
  assert.ok(ps.length > 3 && ps.every((p) => p && p.length <= 200 && !/\n/.test(p)));
  assert.equal(ps.join(' ').replace(/\s+/g, ''), long.replace(/\s+/g, ''), 'nothing lost or reordered');
  assert.deepEqual(parts('see https://github.com/x/y'), ['see https://github.com/x/y']);
  assert.deepEqual(parts('a'.repeat(200)), ['a'.repeat(200)]);
  assert.deepEqual(parts('b'.repeat(201)), ['b'.repeat(200), 'b']);
  assert.deepEqual(parts('  '), []);

  console.log('youtube-chat: all checks pass');
} finally {
  rmSync(dir, { recursive: true, force: true });
}
