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
import http from 'node:http';
import { spawnSync, spawn } from 'node:child_process';
import { chromium } from 'playwright-core';

// The real macOS user's home, not $HOME — the kernel overrides $HOME per-PAI
// for sandboxing, so os.homedir() (which honors $HOME) would resolve into the
// PAI-local sandbox home instead of the owner's real ~/Library. os.userInfo()
// queries the OS user database (getpwuid) directly, ignoring $HOME.
const REAL_HOME = os.userInfo().homedir;
const PAI_ROOT = process.env.PAI_ROOT || path.join(REAL_HOME, '.pai');
const STATE_DIR = path.join(PAI_ROOT, 'var', 'lib', 'browse');
const SOCK = path.join(STATE_DIR, 'browse.sock');
// browse runs its OWN second Chrome (its own profile dir → its own instance) so
// the owner's real Chrome is never touched, quit, or locked — the two coexist.
// The profile is seeded once for bulk (bookmarks/prefs/Local State), and its
// COOKIES are refreshed from the owner's real profile on every daemon start via
// SQLite online-backup, so browse always launches with current logins instead of
// drifting like the old seed-once design did. Reading the real cookie DB is
// read-only on the real profile; browse only ever writes its own copy — it can
// never damage the owner's cookies (unlike the symlink design, which did).
const PROFILE_DIR = path.join(STATE_DIR, 'chrome-profile');
const REAL_CHROME_PROFILE = path.join(
  REAL_HOME, 'Library', 'Application Support', 'Google', 'Chrome');
// Written once the bulk profile has been seeded; cookies refresh independently.
const SEED_MARKER = path.join(PROFILE_DIR, '.seed-ok');
const LOG = path.join(PAI_ROOT, 'var', 'log', 'browse-daemon.log');
// Chrome's OWN stderr (OSCrypt / "Chrome Safe Storage" Keychain errors surface
// here). Under var/log so it survives `pai update` (which only swaps the release
// dir). We stopped discarding Chrome's output so a cookie purge that otherwise
// only shows as a logged-out browser can be traced to the exact Keychain failure.
const CHROME_LOG = path.join(PAI_ROOT, 'var', 'log', 'browse-chrome.log');

// browse launches its OWN Chrome directly (the pre-Playwright "v1" CDP design)
// and Playwright only ATTACHES over CDP for input. Launching the ordinary Chrome
// binary ourselves — with none of Playwright's automation launch flags — is what
// lets Chrome reach the real macOS "Chrome Safe Storage" Keychain key and decrypt
// the owner's copied v10 cookies. Playwright's own launchPersistentContext put
// Chrome on a launch path where it couldn't find the Keychain at all ("A keychain
// cannot be found to store Chrome" modal), even with --use-mock-keychain stripped.
const CDP_PORT = Number(process.env.BROWSE_CDP_PORT) || 9333;
const CDP_BASE = `http://127.0.0.1:${CDP_PORT}`;
const CHROME_BIN = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const CHROME_LAUNCH_TIMEOUT_MS = 20000;
// Cold-boot keychain-readiness gate. After a fresh reboot the login keychain
// isn't findable for ~1-3 min; Chrome launched in that window can't reach its
// "Chrome Safe Storage" key, launches keyless, and PURGES every v10 cookie
// (browse comes up permanently logged out). We probe the same login keychain
// Chrome needs — `security find-generic-password -s "Chrome Safe Storage"` —
// and don't launch until the key is readable. See reference_browse_keychain_session.
const KEYCHAIN_PROBE_TIMEOUT_MS = 3000;    // per-probe hard timeout (never hang)
const KEYCHAIN_READY_TIMEOUT_MS = 180000;  // cap total wait before launching anyway
// Cap is 180s — comfortably under browse.py's REQUEST_TIMEOUT_S (300) so the first
// cold-boot verb can wait through the window without the CLI request timing out.

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try { fs.appendFileSync(LOG, line); } catch { /* best-effort */ }
}

// ---------- own-profile seed + cookie refresh (two-instance design) ----------

// SQLite online-backup: reads a consistent image even while the owner's Chrome
// holds the DB open (a plain cp can capture a torn snapshot). Sidecars are
// dropped so Chrome can't replay a stale journal over the snapshot.
function sqliteBackup(src, dst) {
  if (!fs.existsSync(src)) return false;
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  const r = spawnSync('sqlite3', [src, `.backup ${dst}`], { timeout: 60000, encoding: 'utf8' });
  if (r.status !== 0) {
    log(`sqlite backup ${src} rc=${r.status}: ${(r.stderr || '').trim()}`);
    return false;
  }
  for (const s of ['-wal', '-shm', '-journal']) fs.rmSync(dst + s, { force: true });
  return true;
}

function cookieRows(db) {
  const c = spawnSync('sqlite3', [db, 'select count(*) from cookies;'], { encoding: 'utf8' });
  return parseInt((c.stdout || '0').trim(), 10) || 0;
}

// One-time bulk clone of the owner's profile (bookmarks, prefs, Local State,
// history) into browse's own profile dir. Cookies come in too but are refreshed
// separately below. rsync skips the gigabyte caches.
function seedBulkIfNeeded() {
  if (fs.existsSync(SEED_MARKER)) return;
  if (!fs.existsSync(REAL_CHROME_PROFILE)) {
    log(`real Chrome profile not found at ${REAL_CHROME_PROFILE}; fresh profile`);
    fs.mkdirSync(PROFILE_DIR, { recursive: true });
    fs.writeFileSync(SEED_MARKER, '');
    return;
  }
  log(`first-run: cloning owner profile → ${PROFILE_DIR}`);
  fs.mkdirSync(PROFILE_DIR, { recursive: true });
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
  if (r.status !== 0) log(`bulk seed rsync rc=${r.status}; continuing`);
  fs.writeFileSync(SEED_MARKER, '');
}

// Refresh browse's cookies from the owner's real profile on every daemon start,
// so browse launches with current logins (the fix for the old design's drift).
// Consistent online-backup snapshots of both cookie-store locations. Must run
// while browse's own Chrome is DOWN (daemon start, before launch) so we never
// overwrite a live DB. The owner's Chrome may be running — online-backup handles
// that; we only READ the real profile.
function refreshCookies() {
  let n = 0;
  for (const rel of ['Default/Cookies', 'Default/Network/Cookies']) {
    const src = path.join(REAL_CHROME_PROFILE, rel);
    const dst = path.join(PROFILE_DIR, rel);
    if (sqliteBackup(src, dst)) n += cookieRows(dst);
  }
  log(`cookie refresh: ${n} cookies copied from owner profile`);
  return n;
}

// Kill browse's own Chrome (matched by its dedicated profile dir). Only ever
// targets browse's instance — the owner's Chrome (different --user-data-dir) is
// left running. Also used by the shutdown handlers so idle teardown doesn't
// leave a detached Chrome behind.
function killOwnChrome() {
  spawnSync('pkill', ['-f', `--user-data-dir=${PROFILE_DIR}`]);
  for (const lock of ['SingletonLock', 'SingletonSocket', 'SingletonCookie']) {
    fs.rmSync(path.join(PROFILE_DIR, lock), { force: true });
  }
}

// True iff a Chrome is answering the DevTools protocol on our CDP port.
function cdpAlive() {
  return new Promise((resolve) => {
    const req = http.get(`${CDP_BASE}/json/version`, { timeout: 1000 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

// Non-interactive read of Chrome's "Chrome Safe Storage" Keychain key. Returns
// true only once the login keychain is available (exit 0 + a non-empty key);
// exits 44 ("item could not be found") while the keychain isn't findable yet.
// The per-probe timeout guarantees this can never block the daemon.
function keychainReady() {
  // CRITICAL: force HOME=REAL_HOME. The kernel overrides $HOME per-PAI for
  // sandboxing, and `security` resolves the keychain search list from
  // $HOME/Library/Keychains — under the sandbox home there is no login keychain,
  // so the probe would return exit 44 ("not found") forever and the gate would
  // never let Chrome launch. REAL_HOME (os.userInfo, ignores $HOME) points the
  // probe at the owner's real login keychain, the same one Chrome ultimately uses.
  const res = spawnSync(
    'security', ['find-generic-password', '-w', '-s', 'Chrome Safe Storage'],
    { timeout: KEYCHAIN_PROBE_TIMEOUT_MS, encoding: 'utf8',
      env: { ...process.env, HOME: REAL_HOME } });
  return res.status === 0 && (res.stdout || '').trim().length > 0;
}

// Gate: block until the Safe Storage key is readable (so Chrome can decrypt the
// copied v10 cookies instead of purging them), or give up after the cap and let
// the launch proceed degraded — the seeded-vs-loaded canary catches that case.
async function waitForKeychainReady() {
  if (keychainReady()) return true;
  log('keychain not ready (Chrome Safe Storage key unreadable — likely cold boot); ' +
      'waiting before launching Chrome so cookies are not purged…');
  const deadline = Date.now() + KEYCHAIN_READY_TIMEOUT_MS;
  let backoff = 500;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, backoff));
    if (keychainReady()) {
      log('keychain ready — launching Chrome');
      return true;
    }
    backoff = Math.min(backoff * 2, 10000);
  }
  log(`WARNING: keychain still not ready after ${KEYCHAIN_READY_TIMEOUT_MS}ms — ` +
      'launching Chrome anyway (DEGRADED: cookies may be purged; watch the canary ' +
      `and ${CHROME_LOG}).`);
  return false;
}

// Close a prior browse Chrome so a fresh daemon can refresh cookies and relaunch,
// then wait for it to actually release the CDP port before we spawn a new one
// (pkill returns before Chrome has finished tearing down its listener).
async function closeOwnChrome() {
  killOwnChrome();
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline && (await cdpAlive())) {
    await new Promise((r) => setTimeout(r, 150));
  }
}

// Launch PAI's own Chrome directly (v1 design): the ordinary Chrome binary with a
// dedicated remote-debugging port and profile, and NONE of Playwright's automation
// launch flags — so Chrome uses the real macOS Keychain and the copied v10 cookies
// decrypt. Detached (start_new_session equivalent) so a daemon restart doesn't tear
// down mid-session; closeOwnChrome + the signal handlers clean it up deterministically.
async function launchChromeDirect() {
  if (await cdpAlive()) return;   // a prior browse Chrome already exposes CDP
  if (!fs.existsSync(CHROME_BIN)) throw new Error(`Chrome not found at ${CHROME_BIN}`);
  // Cold-boot gate: don't launch against the real cookies until the Safe Storage
  // key is readable, or we launch keyless and purge every cookie. Placed after the
  // cdpAlive early-return (skip the probe when Chrome is already healthy) and before
  // building chromeArgs — covers both the eager warmup and disconnect-driven relaunch.
  await waitForKeychainReady();
  log(`launching browse Chrome directly (CDP ${CDP_PORT}); Chrome stderr → ${CHROME_LOG}`);
  const chromeArgs = [
    `--remote-debugging-port=${CDP_PORT}`,
    '--remote-debugging-address=127.0.0.1',
    '--remote-allow-origins=*',
    `--user-data-dir=${PROFILE_DIR}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=AutofillServerCommunication',
    // Diagnostic backstop: emit Chrome's own logs to stderr so a Keychain/OSCrypt
    // failure (the post-reboot cookie-purge signature) is captured instead of
    // silently swallowed. The `Encryption is not available` ERROR still surfaces at
    // default verbosity; combined with the seeded-vs-loaded canary in startBrowser
    // this proves whether the readiness gate held. The verbose
    // `--v=1 --vmodule=*os_crypt*=2,*keychain*=2,*cookie*=1` capture was trimmed once
    // the gate shipped, to keep browse-chrome.log small. See
    // reference_browse_keychain_session.
    '--enable-logging=stderr',
    'about:blank',
  ];
  // Capture Chrome's stdout+stderr to CHROME_LOG (was `stdio:'ignore'`, which
  // threw away the exact Keychain error). A per-launch dated marker lets us line
  // the Chrome log up against browse-daemon.log timestamps.
  const chromeLogFd = fs.openSync(CHROME_LOG, 'a');
  fs.writeSync(chromeLogFd,
    `\n===== browse Chrome launch ${new Date().toISOString()} (CDP ${CDP_PORT}) =====\n`);
  // CRITICAL: force HOME=REAL_HOME for Chrome too. The kernel overrides $HOME
  // per-PAI for sandboxing; Chrome resolves the login keychain (its "Chrome Safe
  // Storage" key) from $HOME/Library/Keychains just like the `security` probe, so
  // under the sandbox home it logs "Encryption is not available" and PURGES every
  // v10 cookie — browse comes up logged out. Verified in isolation: sandbox $HOME →
  // Encryption-not-available + 0 cookies; REAL_HOME → decrypts cleanly. This makes
  // Chrome hit the SAME real keychain the readiness gate probes, so the gate is
  // valid. The profile is an absolute --user-data-dir, so $HOME doesn't relocate it.
  const child = spawn(CHROME_BIN, chromeArgs,
    { detached: true, stdio: ['ignore', chromeLogFd, chromeLogFd],
      env: { ...process.env, HOME: REAL_HOME } });
  child.unref();
  fs.closeSync(chromeLogFd);   // child keeps the dup'd fd; safe to close ours
  const deadline = Date.now() + CHROME_LAUNCH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await cdpAlive()) return;
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`Chrome did not expose CDP on ${CDP_BASE} within ${CHROME_LAUNCH_TIMEOUT_MS}ms`);
}

function disableAutofillPrefs() {
  // browse's profile carries the owner's saved addresses/passwords. On a
  // controlled (React/Vue) form Chrome will autofill those over whatever a
  // subagent typed the moment the field gains focus — clobbering values. Turn
  // off autofill + password-manager fill in browse's OWN profile (never the
  // owner's). Cookies / logged-in sessions untouched, so SSO still works.
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

async function startBrowser() {
  await closeOwnChrome();  // close a prior browse Chrome; leaves owner's running
  seedBulkIfNeeded();      // one-time bookmarks/prefs/Local State clone
  const seeded = refreshCookies();  // pull current cookies from the owner profile
  disableAutofillPrefs();
  await launchChromeDirect();  // asuser spawn → Aqua-session Keychain, CDP port
  log('attaching Playwright over CDP…');
  const browser = await chromium.connectOverCDP(CDP_BASE);
  // A CDP-attached Chrome exposes its persistent profile as the default context;
  // pages opened here carry the seeded cookies/logins. Fall back to a fresh
  // context only if Chrome somehow reports none.
  const context = browser.contexts()[0] || await browser.newContext();
  // Keychain canary (diagnostic only — no behavior change): if we seeded a real
  // cookie set but Chrome loaded almost none, it couldn't decrypt the v10 cookies
  // and purged them. Log it so the failure is visible in browse-daemon.log and we
  // know to read CHROME_LOG for Chrome's exact Keychain error. Threshold is
  // generous — a genuine failure drops the count to ~0, not "somewhat fewer".
  try {
    const loaded = (await context.cookies()).length;
    if (seeded >= 50 && loaded < seeded * 0.25) {
      log(`WARNING: keychain decrypt likely FAILED — seeded ${seeded} cookies but ` +
          `Chrome loaded only ${loaded}. browse is logged out; the copied v10 cookies ` +
          `could not be decrypted. Read ${CHROME_LOG} for Chrome's exact ` +
          `"Chrome Safe Storage" Keychain error (root cause still unconfirmed).`);
    }
  } catch (e) {
    log(`keychain canary check skipped: ${e}`);
  }
  return { browser, context };
}

// The owner closing the Chrome window (or a crash) closes the context, leaving
// the daemon up but every page dead. getContext relaunches a fresh context on
// demand — the daemon-level analogue of the old lazy `_ensure_chrome`. A single
// in-flight `launching` promise prevents two concurrent verbs from each
// spawning a Chrome window.
let currentBrowser = null;
let currentContext = null;
let contextAlive = false;
let launching = null;

function getContext() {
  if (contextAlive && currentContext) return Promise.resolve(currentContext);
  if (!launching) {
    launching = (async () => {
      pages.clear();  // stale pages belonged to the dead context
      const { browser, context } = await startBrowser();
      // Chrome closing (owner quit / crash) or the CDP link dropping disconnects
      // the Playwright browser; getContext relaunches on the next verb.
      browser.on('disconnected', () => {
        contextAlive = false;
        currentContext = null;
        currentBrowser = null;
      });
      currentBrowser = browser;
      currentContext = context;
      contextAlive = true;
      log('browser ready');
      return context;
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

// Chrome is spawned detached, so it survives a daemon crash but is NOT reaped
// automatically when the kernel stops us for idle teardown. Kill it on a clean
// shutdown so no orphan browse Chrome lingers.
for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
  process.on(sig, () => { try { killOwnChrome(); } catch { /* best-effort */ } process.exit(0); });
}

// Guard against a double lazy-spawn race: if a live daemon already holds the
// socket, step aside. Otherwise take it over (unlink stale + bind).
function main() {
  const probe = net.connect(SOCK);
  probe.on('connect', () => { probe.destroy(); process.exit(0); });
  probe.on('error', () => { probe.destroy(); serve(); });
}

main();
