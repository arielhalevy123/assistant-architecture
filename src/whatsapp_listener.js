#!/usr/bin/env node
/**
 * WhatsApp Web listener for the assistant.
 *
 * Uses whatsapp-web.js with LocalAuth so the linked session persists in
 * ~/.assistant/whatsapp_profile. On first run emits a QR (PNG + ASCII).
 * After linking, appends every NEW message (timestamp > link_time, not fromMe)
 * to ~/.assistant/inbox/stream.jsonl with channel="whatsapp".
 *
 * NEVER open the profile dir in a normal Chrome — will wipe the session.
 */

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode');
const qrTerm = require('qrcode-terminal');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFile } = require('child_process');

const HOME = os.homedir();
const STATE = path.join(HOME, '.assistant');
const PROFILE = path.join(STATE, 'whatsapp_profile');
const STREAM = path.join(STATE, 'inbox', 'stream.jsonl');
const MEDIA = path.join(STATE, 'inbox', 'media');
const HEARTBEAT = path.join(STATE, 'state', 'listener_whatsapp.heartbeat');
const LINK_TIME_PATH = path.join(STATE, 'state', 'wa_link_time.json');
const LOG = path.join(STATE, 'logs', 'listener_whatsapp.log');
const ACTIVITY = path.join(STATE, 'state', 'activity.jsonl');
const QR_PNG = '/tmp/assistant-wa-qr.png';

// Outbound lives here rather than in a separate process on purpose: the
// listener holds the only usable handle on the linked session, and a second
// Node process against PROFILE dies on chromium's SingletonLock.
const OUTBOX = path.join(STATE, 'outbox', 'whatsapp');
const OUT_PENDING = path.join(OUTBOX, 'pending');
const OUT_SENT = path.join(OUTBOX, 'sent');
const OUT_FAILED = path.join(OUTBOX, 'failed');
const SEND_GAP_MS = 6000; // human-pace: never burst, one message per ~6s
const MAX_TEXT = 4096;

for (const d of [PROFILE, path.dirname(STREAM), MEDIA, path.dirname(HEARTBEAT), path.dirname(LOG),
                 OUT_PENDING, OUT_SENT, OUT_FAILED]) {
  fs.mkdirSync(d, { recursive: true });
}

let clientReady = false; // outbound only drains once the session is live
let draining = false;

function log(level, msg) {
  const line = `${new Date().toISOString()} ${level} ${msg}\n`;
  fs.appendFileSync(LOG, line);
  process.stdout.write(line);
}

function loadLinkTime() {
  try {
    const j = JSON.parse(fs.readFileSync(LINK_TIME_PATH, 'utf8'));
    return Number(j.link_time_unix);
  } catch (_) {
    return null;
  }
}

function saveLinkTime(ts) {
  fs.mkdirSync(path.dirname(LINK_TIME_PATH), { recursive: true });
  fs.writeFileSync(LINK_TIME_PATH, JSON.stringify({ link_time_unix: ts, iso: new Date(ts * 1000).toISOString() }));
}

function emit(record) {
  fs.appendFileSync(STREAM, JSON.stringify(record) + '\n');
}

function journal(record) {
  try {
    fs.appendFileSync(ACTIVITY, JSON.stringify(record) + '\n');
  } catch (e) {
    log('WARN', `activity journal write failed: ${e.message}`);
  }
}

// Heartbeat every 10s so the healthcheck can see liveness.
setInterval(() => { try { fs.writeFileSync(HEARTBEAT, ''); } catch (_) {} }, 10000);
try { fs.writeFileSync(HEARTBEAT, ''); } catch (_) {}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: PROFILE, clientId: 'assistant' }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  }
});

client.on('qr', async (qr) => {
  log('INFO', 'QR event: rendering PNG + ASCII');
  qrTerm.generate(qr, { small: true });
  try {
    await qrcode.toFile(QR_PNG, qr, { width: 512, margin: 2 });
    log('INFO', `wrote QR PNG to ${QR_PNG}`);
    execFile('open', [QR_PNG], (err) => {
      if (err) log('WARN', `could not open PNG viewer: ${err.message}`);
    });
  } catch (e) {
    log('ERROR', `QR PNG write failed: ${e.message}`);
  }
});

client.on('loading_screen', (percent, msg) => {
  log('INFO', `loading ${percent}% ${msg || ''}`);
});

client.on('authenticated', () => log('INFO', 'authenticated (session stored)'));

client.on('auth_failure', (m) => {
  log('ERROR', `auth_failure: ${m}`);
  process.exit(2);
});

client.on('ready', async () => {
  const now = Math.floor(Date.now() / 1000);
  let link = loadLinkTime();
  if (!link) {
    saveLinkTime(now);
    link = now;
    log('INFO', `first-run: link_time locked at ${now} (no history will be ingested)`);
  } else {
    log('INFO', `resumed: existing link_time=${link}`);
  }
  const me = client.info && client.info.wid ? client.info.wid._serialized : '(unknown)';
  const push = client.info && client.info.pushname ? client.info.pushname : '';
  log('INFO', `ready as ${me} pushname=${JSON.stringify(push)}`);
  try { fs.unlinkSync(QR_PNG); } catch (_) {}
  clientReady = true;
  log('INFO', 'outbound enabled: draining ~/.assistant/outbox/whatsapp/pending every 2s');

});

client.on('disconnected', (reason) => {
  log('WARN', `disconnected: ${reason}`);
  // Let launchd restart us.
  process.exit(3);
});

// Only real content types. Whitelist to drop notification_template,
// e2e_notification, ciphertext, revoked, gp2 (group changes), etc.
const ALLOWED_TYPES = new Set([
  'chat', 'image', 'video', 'audio', 'ptt', 'voice',
  'document', 'sticker', 'location', 'vcard', 'multi_vcard',
  'contact_card', 'contact_card_multi', 'poll_creation',
]);

async function handleMessage(msg) {
  try {
    if (!ALLOWED_TYPES.has(msg.type)) return; // drop system/sync noise
    // Skip WhatsApp status broadcasts (ephemeral stories, not direct messages).
    if (msg.from === 'status@broadcast' || (typeof msg.from === 'string' && msg.from.endsWith('@broadcast'))) {
      return;
    }
    const linkTime = loadLinkTime();
    if (linkTime && msg.timestamp && msg.timestamp <= linkTime) {
      return; // history from before we linked; ignore per golden rule #1
    }
    const chat = await msg.getChat().catch(() => null);
    const contact = await msg.getContact().catch(() => null);
    let mediaPath = null;
    if (msg.hasMedia) {
      try {
        const media = await msg.downloadMedia();
        if (media && media.data) {
          const dir = path.join(MEDIA, `whatsapp_${msg.id.id}`);
          fs.mkdirSync(dir, { recursive: true });
          const ext = (media.mimetype || '').split('/')[1] || 'bin';
          mediaPath = path.join(dir, `attachment.${ext.split(';')[0]}`);
          fs.writeFileSync(mediaPath, Buffer.from(media.data, 'base64'));
        }
      } catch (e) {
        log('WARN', `media download failed: ${e.message}`);
      }
    }
    // A location message's msg.body is a base64 JPEG thumbnail, not text. Storing it
    // raw put ~4KB of image data in `text` and threw the coordinates away entirely —
    // 21 location shares landed on the stream as blobs before this was noticed on
    // 2026-09-22. The coordinates are on msg.location and were simply never read.
    let bodyText = msg.body || '';
    if (msg.type === 'location') {
      const loc = msg.location || {};
      const where = [loc.latitude, loc.longitude].filter(v => v != null).join(',');
      const label = loc.description || loc.name || '';
      bodyText = [where, label].filter(Boolean).join(' — ') || '[location, no coordinates]';
    }

    const record = {
      ts: Date.now() / 1000,
      iso: new Date().toISOString(),
      channel: 'whatsapp',
      // For owner-sent messages `msg.from` is his own id, so the conversation
      // is identified by msg.to instead — otherwise every outgoing message
      // would look like it belongs to one giant chat with himself.
      chat_id: msg.fromMe ? (msg.to || msg.from) : msg.from,
      chat_name: chat ? chat.name : null,
      from_id: contact ? contact.id._serialized : msg.author || msg.from,
      from_name: contact ? (contact.pushname || contact.name || contact.number) : null,
      is_group: chat ? chat.isGroup : null,
      from_me: !!msg.fromMe, // owner's own outgoing message, ingested deliberately
      type: msg.type,
      text: bodyText,
      summary: bodyText ? bodyText.slice(0, 200) : `[${msg.type}]`,
      media_paths: mediaPath ? [mediaPath] : [],
      message_id: msg.id._serialized,
      timestamp: msg.timestamp,
    };
    emit(record);
    log('INFO', `${msg.fromMe ? 'sent' : 'inbox'} ${msg.fromMe ? 'to' : 'from'}=${record.chat_name || record.from_name || record.chat_id} type=${msg.type} chars=${(msg.body || '').length}`);
  } catch (e) {
    log('ERROR', `handleMessage failed: ${e.message}`);
  }
}

client.on('message', handleMessage);

// Ring buffer of our own outgoing messages, fed by the library's own event
// stream. This is how a send gets verified: getChatById() throws a minified
// error in this environment, so the chat cannot be read back directly.
const recentOutgoing = [];
client.on('message_create', (msg) => {
  try {
    if (!msg.fromMe) return;
    recentOutgoing.push({
      id: msg.id && msg.id._serialized ? msg.id._serialized : null,
      to: msg.to || null,
      body: msg.body || '',
      at: Date.now(),
    });
    while (recentOutgoing.length > 50) recentOutgoing.shift();
    // The `message` event never fires for our own messages, so owner-sent
    // messages reach the stream only through here. Requested by the owner
    // 2026-08-12 so the assistant can see both halves of a conversation.
    handleMessage(msg).catch((e) => log('WARN', `own-message ingest failed: ${e.message}`));
  } catch (_) { /* never let bookkeeping kill the listener */ }
});

/**
 * Match a send against our own message_create events.
 *
 * The `to` comparison used to be strict equality, which produced a FALSE
 * NEGATIVE on 2026-09-06: the send was requested for "<number>@c.us", the
 * event came back on "<id>@lid", the ids never matched, and the job
 * was reported as NOT delivered even though the message had arrived. That is
 * the dangerous direction to fail in — a false negative invites a resend, and
 * the recipient is a third party who then gets the message twice.
 *
 * WhatsApp's newer LID identifiers are opaque: "<id>@lid" is not
 * derived from the phone number, so no amount of string manipulation can relate
 * the two. When the formats differ we therefore fall back to matching on body
 * plus time window only, and say so in `verifiedBy` rather than pretending the
 * recipient was confirmed. Body equality inside a few seconds is a strong
 * signal on its own; the `to` check was only ever a secondary guard.
 */
function findOutgoing(to, text, sinceMs) {
  const candidates = recentOutgoing.filter((m) => m.body === text && m.at >= sinceMs);
  if (!candidates.length) return null;

  const exact = candidates.find((m) => !m.to || !to || m.to === to);
  if (exact) return { ...exact, idFormatMatched: true };

  // Same text, same moment, different id format (almost always @lid vs @c.us).
  const lidOnly = candidates.find((m) => m.to && to && m.to.endsWith('@lid') !== to.endsWith('@lid'));
  if (lidOnly) return { ...lidOnly, idFormatMatched: false };

  return null;
}

// ---------------------------------------------------------------- outbound

/**
 * Turn whatever the caller wrote in `to` into a real WhatsApp id, or throw.
 * Accepts an already-serialized id ("9725...@c.us", "...@g.us") or a raw
 * phone number in local/international form. Raw numbers are checked against
 * WhatsApp first, so a typo fails loudly here instead of vanishing.
 */
/**
 * Resolve a saved-contact name (what the owner sees in his own address book)
 * to a WhatsApp id. Requires exactly one match: an ambiguous name is an error,
 * never a guess, because the cost of picking the wrong human is high.
 */
async function resolveByName(needle) {
  const want = String(needle || '').trim().toLowerCase();
  if (!want) throw new Error('empty contact name');

  const contacts = await client.getContacts();
  const matches = contacts.filter((c) => {
    if (c.isGroup) return false;
    const names = [c.name, c.pushname, c.shortName, c.verifiedName]
      .filter(Boolean).map((n) => String(n).toLowerCase());
    return names.some((n) => n === want || n.includes(want));
  });

  // getContacts() hands back duplicate entries for the same person (separate
  // @c.us / @lid records), so dedupe by id before judging ambiguity — otherwise
  // one contact reads as two and every lookup fails.
  const byId = new Map();
  for (const c of matches) {
    if (c.id && c.id._serialized) byId.set(c.id._serialized, c);
  }
  const scored = [...byId.values()];

  if (scored.length === 0) throw new Error(`no contact matches ${JSON.stringify(needle)}`);
  if (scored.length > 1) {
    const list = scored.slice(0, 8).map((c) => `${c.name || c.pushname} <${c.id._serialized}>`);
    throw new Error(`${scored.length} contacts match ${JSON.stringify(needle)}, refusing to guess: ${list.join(' | ')}`);
  }
  const only = scored[0];
  log('INFO', `resolved contact name ${JSON.stringify(needle)} -> ${only.name || only.pushname} ${only.id._serialized}`);
  return only.id._serialized;
}

async function resolveRecipient(to, allowGroup) {
  const raw = String(to || '').trim();
  if (!raw) throw new Error('empty recipient');

  if (raw.endsWith('@g.us')) {
    if (!allowGroup) {
      throw new Error('refusing to send to a group without allow_group:true');
    }
    return raw;
  }
  if (raw.endsWith('@c.us') || raw.endsWith('@lid')) return raw;

  let digits = raw.replace(/[^\d]/g, '');
  if (digits.startsWith('0')) digits = '972' + digits.slice(1); // local IL form
  if (digits.length < 8) throw new Error(`implausible phone number: ${raw}`);

  const numberId = await client.getNumberId(digits);
  if (!numberId) throw new Error(`${digits} is not registered on WhatsApp`);
  return numberId._serialized;
}

async function processJob(file) {
  const full = path.join(OUT_PENDING, file);
  let job;
  try {
    job = JSON.parse(fs.readFileSync(full, 'utf8'));
  } catch (e) {
    // Unparseable job: park it in failed/ so it can't spin forever.
    const dest = path.join(OUT_FAILED, file);
    try { fs.renameSync(full, dest); } catch (_) {}
    fs.writeFileSync(dest + '.result', JSON.stringify(
      { ok: false, error: `unparseable job: ${e.message}`, at: new Date().toISOString() }));
    log('ERROR', `outbox job ${file} unparseable: ${e.message}`);
    return;
  }

  const result = { ok: false, at: null, error: null, message_id: null, to_resolved: null };
  try {
    const text = String(job.text || '');
    if (!text.trim()) throw new Error('refusing to send an empty message');
    if (text.length > MAX_TEXT) throw new Error(`text too long: ${text.length} > ${MAX_TEXT}`);

    const to = job.to_name
      ? await resolveByName(job.to_name)
      : await resolveRecipient(job.to, job.allow_group === true);
    result.to_resolved = to;

    const startedAt = Date.now() - 1000; // small skew allowance
    const sent = await client.sendMessage(to, text).catch((e) => {
      // sendMessage itself can reject with a minified page-context error; that
      // does not by itself prove nothing was sent, so fall through to the
      // event-stream check rather than declaring failure here.
      log('WARN', `sendMessage threw (${e.message}); falling back to event verification`);
      return null;
    });
    let messageId = sent && sent.id ? sent.id._serialized : null;
    let verifiedBy = 'send-return';

    if (!messageId) {
      // whatsapp-web.js 1.34.x resolves sendMessage to undefined even when the
      // message is delivered, so the return value proves nothing either way.
      // The message_create event is the reliable signal.
      for (let waited = 0; waited < 10000 && !messageId; waited += 500) {
        await new Promise((r) => setTimeout(r, 500));
        const hit = findOutgoing(to, text, startedAt);
        if (hit) {
          // THE BUG, found by instrumentation 2026-09-08 after reading the code
          // three times failed to reveal it: this used to require `hit.id`, but
          // whatsapp-web.js delivers message_create for our OWN sends with
          // msg.id._serialized undefined. So the match succeeded every time and
          // was then thrown away for lacking an id it never had — which is why
          // the outbox reported "NOT delivered" on 9 consecutive genuinely
          // delivered messages and had never once succeeded since 2026-08-19.
          //
          // The id was only ever a receipt, never the evidence. The evidence is
          // that our own outgoing message, with this exact body, appeared in the
          // event stream inside the window. Confirm on that, and carry the id
          // when it happens to exist.
          messageId = hit.id || `confirmed-by-event:${hit.at}`;
          const idNote = hit.id ? '' : ' (no id on event; confirmed by body+time)';
          verifiedBy = hit.idFormatMatched
            ? `message_create-event${idNote}`
            : `message_create-event (id format differed: sent to ${to}, event on ${hit.to} — text+time matched, recipient not independently confirmed)${idNote}`;
        }
      }
    }

    if (!messageId) {
      throw new Error('no message_create event for this text within 10s: treat as NOT delivered');
    }
    result.ok = true;
    result.message_id = messageId;
    result.verified_by = verifiedBy;
    result.at = new Date().toISOString();
    log('INFO', `outbox sent to=${to} chars=${text.length} id=${messageId} verified=${verifiedBy}`);
  } catch (e) {
    result.error = e.message;
    result.at = new Date().toISOString();
    log('ERROR', `outbox send failed for ${file}: ${e.message}`);
  }

  const destDir = result.ok ? OUT_SENT : OUT_FAILED;
  const dest = path.join(destDir, file);
  try { fs.renameSync(full, dest); } catch (e) {
    log('WARN', `could not move ${file} to ${destDir}: ${e.message}`);
  }
  try {
    fs.writeFileSync(dest + '.result', JSON.stringify({ ...result, job }, null, 2));
  } catch (e) {
    log('WARN', `could not write result for ${file}: ${e.message}`);
  }

  journal({
    ts: Date.now() / 1000,
    iso: new Date().toISOString(),
    kind: 'notify_whatsapp',
    ok: result.ok,
    to: result.to_resolved || job.to,
    chars: String(job.text || '').length,
    message_id: result.message_id,
    reason: result.error,
    job_id: job.id || file,
  });
}

async function drainOutbox() {
  if (!clientReady || draining) return;
  draining = true;
  try {
    const files = fs.readdirSync(OUT_PENDING)
      .filter((f) => f.endsWith('.json'))
      .sort(); // filenames are timestamp-prefixed, so this is FIFO
    for (const f of files) {
      await processJob(f);
      // Human-pace outbound: deliberate gap between consecutive sends.
      await new Promise((r) => setTimeout(r, SEND_GAP_MS));
    }
  } catch (e) {
    log('ERROR', `drainOutbox failed: ${e.message}`);
  } finally {
    draining = false;
  }
}

setInterval(drainOutbox, 2000);

process.on('SIGINT', () => { log('INFO', 'SIGINT received'); process.exit(0); });
process.on('SIGTERM', () => { log('INFO', 'SIGTERM received'); process.exit(0); });

log('INFO', `whatsapp listener starting pid=${process.pid}`);
client.initialize();
