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

import { makeWASocket, useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import readline from 'readline';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import pino from 'pino';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Auth state lives under /Users/arda/.pai/sys/drivers/whatsapp/auth/
const PAI_ROOT = process.env.PAI_ROOT || path.join(process.env.HOME, '.pai');
const AUTH_DIR = path.join(PAI_ROOT, 'sys', 'drivers', 'whatsapp', 'auth');
fs.mkdirSync(AUTH_DIR, { recursive: true });

// Quiet Pino logger to stderr only (stdout is the wire protocol)
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

async function start() {
  status('connecting');

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
    logger,
    browser: ['PAI', 'Desktop', '1.0'],
  });

  // Forward connection updates
  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      emit({ type: 'qr', qr });
    }

    if (connection === 'open') {
      status('open');
    } else if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      const shouldReconnect = reason !== DisconnectReason.loggedOut;
      emit({
        type: 'status',
        state: 'close',
        reason: reason || 'unknown',
        shouldReconnect,
      });
      if (!shouldReconnect) {
        // Logged out — clear auth so next start shows QR
        fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        fs.mkdirSync(AUTH_DIR, { recursive: true });
      }
    }
  });

  // Save creds on update
  sock.ev.on('creds.update', saveCreds);

  // Inbound messages
  sock.ev.on('messages.upsert', (m) => {
    for (const msg of m.messages) {
      if (msg.key.fromMe) continue; // skip own echoes
      const chatJid = msg.key.remoteJid;
      if (!chatJid || chatJid.includes('@g.us')) continue; // skip groups for v1
      const body = msg.message?.conversation
        || msg.message?.extendedTextMessage?.text
        || '';
      if (!body) continue;

      // Extract phone number from JID (e.g. 15551234567@s.whatsapp.net)
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

  // Stdin — outbound messages from the Python driver
  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    try {
      const cmd = JSON.parse(line);
      if (cmd.type === 'message' && cmd.direction === 'out') {
        const jid = `${cmd.to}@s.whatsapp.net`;
        // Support both plain number (from thread slug) and full JID
        const toJid = cmd.to.includes('@') ? cmd.to : jid;
        try {
          await sock.sendMessage(toJid, { text: cmd.body });
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
      // Non-JSON input — ignore (could be pings or noise)
    }
  });

  // Handle SIGTERM gracefully
  process.on('SIGTERM', () => {
    sock.end(undefined);
    process.exit(0);
  });
}

start().catch((err) => {
  emit({ type: 'error', error: `bridge start failed: ${err.message}` });
  process.exit(1);
});
