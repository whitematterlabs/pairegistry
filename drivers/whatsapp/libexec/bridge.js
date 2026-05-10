#!/Users/arda/.pai/usr/bin/env node

/**
 * WhatsApp Baileys bridge — JSON-per-line protocol over stdin/stdout.
 *
 * Reads JSON commands from stdin, writes JSON events to stdout.
 * Auth state persists under /Users/arda/.pai/sys/drivers/whatsapp/auth/.
 *
 * Protocol (stdout → driver reads):
 *   {"type":"message","direction":"in","from":"+15551234567","body":"hey","timestamp":"..."}
 *   {"type":"message","direction":"out","to":"+15551234567","body":"ok","timestamp":"..."}
 *   {"type":"qr","qr":"BASE64-QR-STRING"}
 *   {"type":"status","state":"open"|"connecting"|"close"}
 *   {"type":"error","error":"..."}
 *
 * Protocol (stdin → bridge reads):
 *   {"type":"message","direction":"out","to":"+15551234567","body":"hey there"}
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import readline from 'readline';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import pino from 'pino';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PAI_ROOT = process.env.PAI_ROOT || path.join(process.env.HOME, '.pai');
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

// Active socket — replaced on each (re)connect. stdin/SIGTERM handlers below
// reference this via the module-level binding so they survive reconnects.
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

  sock.ev.on('messages.upsert', (m) => {
    for (const msg of m.messages) {
      if (msg.key.fromMe) continue;
      const chatJid = msg.key.remoteJid;
      if (!chatJid || chatJid.includes('@g.us')) continue;
      const body = msg.message?.conversation
        || msg.message?.extendedTextMessage?.text
        || '';
      if (!body) continue;

      const phone = chatJid.split('@')[0];
      emit({
        type: 'message',
        direction: 'in',
        from: phone,
        body,
        timestamp: new Date().toISOString(),
      });
    }
  });
}

// stdin readline + SIGTERM registered once; they reference currentSock so
// they keep working across in-process reconnects.
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  try {
    const cmd = JSON.parse(line);
    if (cmd.type === 'message' && cmd.direction === 'out') {
      if (!currentSock) {
        emit({ type: 'error', error: 'no active socket', command: cmd });
        return;
      }
      const toJid = cmd.to.includes('@') ? cmd.to : `${cmd.to}@s.whatsapp.net`;
      try {
        await currentSock.sendMessage(toJid, { text: cmd.body });
        emit({
          type: 'message',
          direction: 'out',
          to: cmd.to,
          body: cmd.body,
          timestamp: new Date().toISOString(),
          sent: true,
        });
      } catch (err) {
        emit({
          type: 'error',
          error: `send failed to ${cmd.to}: ${err.message}`,
          command: cmd,
        });
      }
    }
  } catch (err) {
    // Non-JSON input — ignore.
  }
});

process.on('SIGTERM', () => {
  try { currentSock?.end(undefined); } catch (_) {}
  process.exit(0);
});

connect().catch((err) => {
  emit({ type: 'error', error: `bridge start failed: ${err.message}` });
  process.exit(1);
});
