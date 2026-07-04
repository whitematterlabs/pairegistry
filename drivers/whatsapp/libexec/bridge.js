#!/Users/arda/.pai/usr/bin/env node

/**
 * WhatsApp Baileys bridge — receive-only, JSON-per-line over stdout.
 *
 * Writes JSON events to stdout. There is no send path: the bridge does
 * not read commands from stdin. Auth state persists under
 * /Users/arda/.pai/sys/drivers/whatsapp/auth/.
 *
 * Protocol (stdout → driver reads):
 *   {"type":"message","direction":"in","from":"+15551234567","body":"hey","timestamp":"..."}
 *   {"type":"qr","qr":"BASE64-QR-STRING"}
 *   {"type":"status","state":"open"|"connecting"|"close"}
 *   {"type":"error","error":"..."}
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import path from 'path';
import fs from 'fs';
import os from 'os';
import { fileURLToPath } from 'url';
import pino from 'pino';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const REAL_HOME = os.userInfo().homedir;
const PAI_ROOT = process.env.PAI_ROOT || path.join(REAL_HOME, '.pai');
const AUTH_DIR = path.join(PAI_ROOT, 'sys', 'drivers', 'whatsapp', 'auth');
fs.mkdirSync(AUTH_DIR, { recursive: true });

const logger = pino(
  { level: 'info' },
  pino.destination(path.join(PAI_ROOT, 'sys', 'drivers', 'whatsapp', 'bridge.log'))
);

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function status(state) {
  emit({ type: 'status', state });
}

// Active socket — replaced on each (re)connect. The SIGTERM handler below
// references this via the module-level binding so it survives reconnects.
let currentSock = null;

async function connect() {
  status('connecting');

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version, isLatest } = await fetchLatestBaileysVersion();
  logger.info({ version, isLatest }, 'using WhatsApp Web version');

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger,
    browser: ['PAI', 'Desktop', '1.0'],
  });
  currentSock = sock;

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      emit({ type: 'qr', qr });
    }

    if (connection === 'open') {
      status('open');
      return;
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      const loggedOut = reason === DisconnectReason.loggedOut;
      const restartRequired = reason === DisconnectReason.restartRequired; // 515

      emit({
        type: 'status',
        state: 'close',
        reason: reason || 'unknown',
        shouldReconnect: !loggedOut,
      });

      if (loggedOut) {
        // Phone-side unlink — auth is dead, wipe it and exit so the Python
        // supervisor restarts us fresh and a new QR pairing kicks off.
        fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        fs.mkdirSync(AUTH_DIR, { recursive: true });
        process.exit(1);
      }

      if (restartRequired) {
        // 515 fires right after a fresh pair. Baileys requires an in-process
        // reconnect so saveCreds() runs on the post-pair connection — exiting
        // here would lose the just-paired credentials.
        logger.info('stream 515 (restart required) — reconnecting in-process');
        connect().catch((err) => {
          emit({ type: 'error', error: `reconnect failed: ${err.message}` });
          process.exit(1);
        });
        return;
      }

      // Any other close: exit and let the Python supervisor decide whether
      // to restart with backoff. We don't loop in-process for general drops
      // because backoff/state lives in the supervisor.
      process.exit(0);
    }
  });

  sock.ev.on('messages.upsert', async (m) => {
    for (const msg of m.messages) {
      const out = await extractIn(sock, msg);
      if (out) emit(out);
    }
  });

  sock.ev.on('messaging-history.set', async ({ messages, isLatest, syncType }) => {
    const cutoff = Math.floor(Date.now() / 1000) - HISTORY_CUTOFF_SECONDS;
    let kept = 0;
    for (const msg of messages || []) {
      const tsRaw = msg.messageTimestamp;
      const ts = Number(tsRaw?.low ?? tsRaw?.toNumber?.() ?? tsRaw ?? 0);
      if (ts && ts < cutoff) continue;
      const out = await extractIn(sock, msg, { history: true });
      if (out) { emit(out); kept++; }
    }
    logger.info({ total: messages?.length ?? 0, kept, isLatest, syncType }, 'history sync batch');
  });
}

const HISTORY_WINDOW_DAYS = 30;
const HISTORY_CUTOFF_SECONDS = HISTORY_WINDOW_DAYS * 24 * 60 * 60;

async function resolveToPhoneJid(sock, key) {
  const remote = key.remoteJid || '';
  if (!remote || remote.includes('@g.us')) return null;
  if (!remote.endsWith('@lid')) return remote;
  try {
    const pn = await sock.signalRepository.lidMapping.getPNForLID(remote);
    if (pn) return pn;
  } catch (err) {
    logger.warn({ err: err.message, lid: remote }, 'LID lookup failed');
  }
  return null;
}

async function extractIn(sock, msg, { history = false } = {}) {
  if (msg.key.fromMe) return null;
  const jid = await resolveToPhoneJid(sock, msg.key);
  if (!jid) return null;
  const body = msg.message?.conversation
    || msg.message?.extendedTextMessage?.text
    || '';
  if (!body) return null;
  const tsRaw = msg.messageTimestamp;
  const ts = Number(tsRaw?.low ?? tsRaw?.toNumber?.() ?? tsRaw ?? 0);
  return {
    type: 'message',
    direction: 'in',
    from: jid.split('@')[0],
    body,
    timestamp: ts ? new Date(ts * 1000).toISOString() : new Date().toISOString(),
    history,
  };
}

// SIGTERM references currentSock so it keeps working across in-process
// reconnects.
process.on('SIGTERM', () => {
  try { currentSock?.end(undefined); } catch (_) {}
  process.exit(0);
});

connect().catch((err) => {
  emit({ type: 'error', error: `bridge start failed: ${err.message}` });
  process.exit(1);
});
