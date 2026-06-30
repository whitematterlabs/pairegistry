// browse daemon — a persistent Playwright session driving PAI's own Chrome.
//
// PAI invokes `browse` as one-shot CLI verbs. This daemon, spawned lazily by
// browse.py on the first verb, owns a single headed Chrome (Playwright
// launchPersistentContext, channel:'chrome' → the real Google Chrome, no
// Chromium download) against a PAI-dedicated profile under $PAI_ROOT, plus a
// Page per PAI slug. Each verb is one JSON request over a unix socket; the
// daemon answers with one JSON line. Node startup + browser launch is paid
// once; verbs stay instant after that. Playwright gives us real keyboard/mouse
// input (the core fix — synthetic value-sets don't drive React comboboxes),
// auto-waiting, frame handling, and dialogs.
//
// Protocol: client connects AF_UNIX, sends one line `{slug,verb,args}\n`,
// reads one line reply `{ok:true,...}\n` or `{ok:false,error}\n`, disconnects.

import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import { chromium } from 'playwright-core';

const PAI_ROOT = process.env.PAI_ROOT || path.join(os.homedir(), '.pai');
const STATE_DIR = path.join(PAI_ROOT, 'var', 'lib', 'browse');
const SOCK = path.join(STATE_DIR, 'browse.sock');
// PAI keeps its OWN Chrome profile, seeded once from the owner's real profile
// so logged-in sessions carry over. After the seed the two diverge; re-seed by
// deleting this dir. Playwright owns the browser process bound to this dir, so
// there is no shared debug port and no foreign-Chrome guard to worry about.
const PROFILE_DIR = path.join(STATE_DIR, 'chrome-cdp-profile');
const REAL_CHROME_PROFILE = path.join(
  os.homedir(), 'Library', 'Application Support', 'Google', 'Chrome');
const LOG = path.join(PAI_ROOT, 'var', 'log', 'browse-daemon.log');

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try { fs.appendFileSync(LOG, line); } catch { /* best-effort */ }
}

// ---------- profile seeding (ported from browse.py) ----------

function seedProfileIfNeeded() {
  if (fs.existsSync(PROFILE_DIR)) return;
  if (!fs.existsSync(REAL_CHROME_PROFILE)) {
    log(`real Chrome profile not found at ${REAL_CHROME_PROFILE}; fresh profile`);
    fs.mkdirSync(PROFILE_DIR, { recursive: true });
    return;
  }
  log(`first-run: cloning Chrome profile → ${PROFILE_DIR} (preserves logins, ~1 min)`);
  fs.mkdirSync(PROFILE_DIR, { recursive: true });
  // rsync the bits that matter: top-level Local State (cookie crypto key +
  // profile registry) and the Default subprofile (cookies, login data,
  // bookmarks). Skip the gigabyte-scale Cache/ subdirs.
  const args = [
    '-a',
    '--exclude=Default/Cache/',
    '--exclude=Default/Code Cache/',
    '--exclude=Default/GPUCache/',
    '--exclude=Default/Service Worker/',
    '--exclude=Default/File System/',
    '--exclude=Default/DawnGraphiteCache/',
    '--exclude=Default/DawnWebGPUCache/',
    '--exclude=Default/Application Cache/',
    '--include=Local State',
    '--include=Default/***',
    '--exclude=*',
    `${REAL_CHROME_PROFILE}/`,
    `${PROFILE_DIR}/`,
  ];
  const r = spawnSync('rsync', args, { timeout: 300000 });
  if (r.status !== 0) {
    log(`profile seed rsync rc=${r.status}; continuing with partial copy`);
  }
}

function disableAutofillPrefs() {
  // The seeded profile carries the owner's saved addresses/passwords. On a
  // controlled (React/Vue) form Chrome will autofill those over whatever a
  // subagent typed the moment the field gains focus — clobbering values. Turn
  // off autofill + password-manager fill. Cookies / logged-in sessions are
  // untouched, so SSO still works. Written while Chrome is down; Chrome reads
  // Preferences only at launch.
  const prefsPath = path.join(PROFILE_DIR, 'Default', 'Preferences');
  let prefs = {};
  try {
    if (fs.existsSync(prefsPath)) prefs = JSON.parse(fs.readFileSync(prefsPath, 'utf8'));
  } catch { prefs = {}; }
  prefs.autofill = prefs.autofill || {};
  prefs.autofill.enabled = false;
  prefs.autofill.profile_enabled = false;
  prefs.autofill.credit_card_enabled = false;
  prefs.credentials_enable_service = false;
  prefs.credentials_enable_autosignin = false;
  try {
    fs.mkdirSync(path.dirname(prefsPath), { recursive: true });
    fs.writeFileSync(prefsPath, JSON.stringify(prefs));
  } catch (e) {
    log(`could not disable autofill prefs: ${e}`);
  }
}

// ---------- DOM tagger (kept byte-for-byte in sync with browse.py history) ----------

const DOM_TAGGER_JS = String.raw`
(() => {
  // Include ARIA widget roles (combobox/listbox/option/menu/switch/tab/slider/
  // spinbutton) and keyboard-focusable [tabindex] divs, not just native
  // controls. Modern sites build dropdowns and menus from <div role="...">; a
  // native-tag-only selector renders them invisible to the dom verb.
  const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"], [role="combobox"], [role="listbox"], [role="option"], [role="menu"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="switch"], [role="tab"], [role="slider"], [role="spinbutton"], [onclick], [contenteditable="true"], [tabindex]:not([tabindex="-1"])';
  const els = Array.from(document.querySelectorAll(sel));
  // Resolve the error message bound to a field flagged aria-invalid, the
  // standard framework-agnostic marker of an invalid control. Prefer explicit
  // ARIA linkage; fall back to the nearest container's text minus the field's
  // own label/placeholder.
  function errText(el) {
    const ids = (el.getAttribute('aria-errormessage') || el.getAttribute('aria-describedby') || '')
      .split(/\s+/).filter(Boolean);
    const parts = [];
    for (const id of ids) {
      const t = document.getElementById(id);
      if (t) { const s = (t.innerText || t.textContent || '').trim(); if (s) parts.push(s); }
    }
    if (parts.length) return parts.join(' ').replace(/\s+/g, ' ').slice(0, 140);
    const textOnly = (node) => {
      if (!node || !node.querySelector) return '';
      if (node.querySelector('input,select,textarea,button,a,[role="button"],[role="link"]')) return '';
      const s = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
      return s.length && s.length <= 140 ? s : '';
    };
    let node = el;
    for (let i = 0; i < 3 && node; i++) {
      let sib = node.nextElementSibling, hops = 0;
      while (sib && hops < 3) {
        const t = textOnly(sib);
        if (t) return t;
        sib = sib.nextElementSibling; hops++;
      }
      node = node.parentElement;
    }
    return '';
  }
  const out = [];
  let n = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const idx = ++n;
    el.setAttribute('data-pai-idx', String(idx));
    let label = (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                 el.value || el.innerText || el.textContent || '').trim();
    label = label.replace(/\s+/g, ' ').slice(0, 140);
    let err = '';
    if (el.getAttribute('aria-invalid') === 'true') err = errText(el) || 'invalid';
    out.push({
      idx,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      role: el.getAttribute('role') || '',
      name: el.getAttribute('name') || '',
      href: el.getAttribute('href') || '',
      text: label,
      err: err
    });
  }
  return JSON.stringify(out);
})()
`;

// ---------- browser + page registry ----------

function startBrowser() {
  seedProfileIfNeeded();
  disableAutofillPrefs();
  log('launching persistent Chrome (channel:chrome)…');
  return chromium.launchPersistentContext(PROFILE_DIR, {
    channel: 'chrome',
    headless: false,
    viewport: null,
    args: ['--no-first-run', '--no-default-browser-check'],
  });
}

// The owner closing the Chrome window (or a crash) closes the context, leaving
// the daemon up but every page dead. getContext relaunches a fresh context on
// demand — the daemon-level analogue of the old lazy `_ensure_chrome`. A single
// in-flight `launching` promise prevents two concurrent verbs from each
// spawning a Chrome window.
let currentContext = null;
let contextAlive = false;
let launching = null;

function getContext() {
  if (contextAlive && currentContext) return Promise.resolve(currentContext);
  if (!launching) {
    launching = (async () => {
      pages.clear();  // stale pages belonged to the dead context
      const ctx = await startBrowser();
      ctx.on('close', () => { contextAlive = false; currentContext = null; });
      currentContext = ctx;
      contextAlive = true;
      log('browser ready');
      return ctx;
    })().finally(() => { launching = null; });
  }
  return launching;
}

// One Page per slug. Each carries a serialization lock so two concurrent verbs
// for the same slug never interleave on the same page.
const pages = new Map();  // slug -> { page, created, lock: Promise }

function procDir(slug) {
  return path.join(PAI_ROOT, 'proc', slug);
}

// Replaces _close_orphan_tabs: a browse subagent's /proc/<slug>/ is deleted the
// instant it resolves, so a page whose proc dir is gone belongs to a finished
// subagent. Close it before serving a fresh verb so no one inherits stale state.
async function pruneOrphanPages(currentSlug) {
  for (const [slug, rec] of [...pages.entries()]) {
    if (slug === currentSlug) continue;
    if (!fs.existsSync(procDir(slug))) {
      try { await rec.page.close(); } catch { /* already gone */ }
      pages.delete(slug);
    }
  }
}

async function getPage(context, slug) {
  let rec = pages.get(slug);
  if (rec && !rec.page.isClosed()) return rec;
  const page = await context.newPage();
  rec = { page, created: Date.now(), lock: Promise.resolve() };
  pages.set(slug, rec);
  return rec;
}

// ---------- verb helpers ----------

function innerText(page) {
  return page.evaluate(() => (document.body && document.body.innerText) || '');
}

// Port of browse.py _report_new_text's diff: surface visible text that appeared
// after an in-page action (validation errors, next-step prompts, toasts) as a
// plain innerText line-diff — no per-site selectors. Returned full; the CLI
// applies its own display limit.
function diffNewLines(before, after) {
  if (!after || after === before) return [];
  const beforeSet = new Set(before.split('\n').map((s) => s.trim()).filter(Boolean));
  const seen = new Set();
  const out = [];
  for (const ln of after.split('\n')) {
    const s = ln.trim();
    if (!s || beforeSet.has(s) || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

// After an action that may navigate, let a load settle without hanging forever
// on long-polling pages.
async function settle(page) {
  try { await page.waitForLoadState('domcontentloaded', { timeout: 15000 }); }
  catch { /* interactive is enough */ }
}

const NAV_TIMEOUT = 30000;
const ACT_TIMEOUT = 15000;

// ---------- verb dispatch ----------

async function handle(context, slug, verb, args) {
  const rec = await getPage(context, slug);
  const page = rec.page;
  switch (verb) {
    case 'goto': {
      await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
      return { url: page.url(), title: await page.title() };
    }
    case 'text': {
      return { text: await innerText(page) };
    }
    case 'dom': {
      const raw = await page.evaluate(DOM_TAGGER_JS);
      return { items: JSON.parse(raw || '[]') };
    }
    case 'click': {
      const sel = `[data-pai-idx="${args.idx}"]`;
      const guard = await page.evaluate((s) => {
        const el = document.querySelector(s);
        if (!el) return 'NOT_FOUND';
        // A disabled or aria-disabled control swallows clicks silently; report
        // it so the caller fixes the form instead of looping on a dead button.
        if (el.disabled === true || el.getAttribute('aria-disabled') === 'true') {
          return 'DISABLED:' + el.tagName;
        }
        return 'OK';
      }, sel);
      if (guard === 'NOT_FOUND') return { status: 'NOT_FOUND' };
      if (guard.startsWith('DISABLED')) return { status: 'DISABLED', tag: guard.split(':')[1] };
      const beforeUrl = page.url();
      const beforeText = await innerText(page);
      // Real mouse click: auto-waits for actionability and scrolls into view.
      await page.locator(sel).first().click({ timeout: ACT_TIMEOUT });
      await settle(page);
      const url = page.url();
      const title = await page.title();
      let newLines = [];
      if (url === beforeUrl) newLines = diffNewLines(beforeText, await innerText(page));
      return { status: 'OK', url, title, new_lines: newLines };
    }
    case 'type': {
      const sel = `[data-pai-idx="${args.idx}"]`;
      const loc = page.locator(sel).first();
      if (await page.locator(sel).count() === 0) return { status: 'NOT_FOUND' };
      await loc.click({ timeout: ACT_TIMEOUT });   // focus
      await loc.fill('');                          // clear existing value
      // REAL per-character keystrokes — the core fix. React comboboxes only
      // fetch suggestions on genuine keyboard events; a synthetic value-set
      // reverts and never opens the dropdown.
      await loc.pressSequentially(args.text, { delay: 15 });
      let newLines = [];
      if (args.submit) {
        const beforeUrl = page.url();
        const beforeText = await innerText(page);
        await page.keyboard.press('Enter');
        await settle(page);
        if (page.url() === beforeUrl) newLines = diffNewLines(beforeText, await innerText(page));
      }
      return { status: 'OK', new_lines: newLines };
    }
    case 'press': {
      const beforeUrl = page.url();
      const beforeText = await innerText(page);
      await page.keyboard.press(args.key);
      await settle(page);
      let newLines = [];
      if (page.url() === beforeUrl) newLines = diffNewLines(beforeText, await innerText(page));
      return { code: args.key, new_lines: newLines };
    }
    case 'scroll': {
      await page.evaluate((a) => window.scrollBy(0, a), args.amount);
      return { amount: args.amount };
    }
    case 'screenshot': {
      const buf = await page.screenshot({ path: args.path });
      return { path: args.path, bytes: buf.length };
    }
    case 'url': {
      return { url: page.url() };
    }
    case 'title': {
      return { title: await page.title() };
    }
    case 'wait': {
      try {
        if (args.is_selector) {
          await page.waitForSelector(args.what, { timeout: args.timeout * 1000 });
        } else {
          await page.waitForFunction(
            (t) => ((document.body && document.body.innerText) || '').includes(t),
            args.what, { timeout: args.timeout * 1000 });
        }
        return { found: true };
      } catch {
        return { found: false };
      }
    }
    case 'eval': {
      try {
        const expr = args.await_promise ? `(async () => (${args.expr}))()` : args.expr;
        const value = await page.evaluate(expr);
        return { undef: value === undefined, value: value === undefined ? null : value };
      } catch (e) {
        return { js_error: String((e && e.message) || e) };
      }
    }
    default:
      throw new Error(`unknown verb ${verb}`);
  }
}

// tabs/close don't operate on a freshly-created page, so they bypass getPage
// (which would otherwise spawn an empty page just to list or close it).
async function handleTabs(slug) {
  const tabs = [];
  for (const [s, r] of pages.entries()) {
    if (r.page.isClosed()) continue;
    let url = '', title = '';
    try { url = r.page.url(); title = await r.page.title(); } catch { /* mid-nav */ }
    tabs.push({ slug: s, url, title, mine: s === slug });
  }
  return { tabs };
}

async function handleClose(slug) {
  const r = pages.get(slug);
  if (r) {
    try { await r.page.close(); } catch { /* already gone */ }
    pages.delete(slug);
    return { closed: true };
  }
  return { closed: false };
}

// Serialize requests per slug via a promise chain on the page record, so two
// concurrent verbs for the same subagent's page never interleave.
async function dispatch(slug, verb, args) {
  const context = await getContext();
  await pruneOrphanPages(slug);
  if (verb === 'tabs') return handleTabs(slug);
  if (verb === 'close') return handleClose(slug);
  const rec = await getPage(context, slug);
  const run = rec.lock.then(() => handle(context, slug, verb, args));
  // keep the chain alive even if this request throws
  rec.lock = run.then(() => {}, () => {});
  return run;
}

// ---------- socket server ----------

function serve() {
  try { fs.mkdirSync(STATE_DIR, { recursive: true }); } catch { /* exists */ }
  try { fs.mkdirSync(path.dirname(LOG), { recursive: true }); } catch { /* exists */ }
  try { fs.unlinkSync(SOCK); } catch { /* no stale sock */ }

  const server = net.createServer((conn) => {
    let buf = '';
    conn.on('data', async (chunk) => {
      buf += chunk;
      const nl = buf.indexOf('\n');
      if (nl < 0) return;
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      let reply;
      try {
        const req = JSON.parse(line);
        const result = await dispatch(req.slug, req.verb, req.args || {});
        reply = { ok: true, ...result };
      } catch (e) {
        reply = { ok: false, error: String((e && e.message) || e) };
      }
      try { conn.write(JSON.stringify(reply) + '\n'); } catch { /* client gone */ }
      conn.end();
    });
    conn.on('error', () => { /* client disconnected */ });
  });

  server.on('error', (e) => {
    log(`socket server error: ${e}`);
    process.exit(1);
  });

  server.listen(SOCK, () => {
    log(`listening on ${SOCK}`);
    // Bind the socket BEFORE the browser is up so the client's connect succeeds
    // immediately; the first verb's request then awaits getContext() (which may
    // include a one-time profile seed). Decouples "daemon alive" from "browser
    // ready" so _ensure_daemon's poll returns fast. Warm the browser eagerly so
    // the first verb isn't the one that pays the launch.
    getContext().catch((e) => log(`initial browser launch failed: ${e}`));
  });
}

// Guard against a double lazy-spawn race: if a live daemon already holds the
// socket, step aside. Otherwise take it over (unlink stale + bind).
function main() {
  const probe = net.connect(SOCK);
  probe.on('connect', () => { probe.destroy(); process.exit(0); });
  probe.on('error', () => { probe.destroy(); serve(); });
}

main();
