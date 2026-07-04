#!/usr/bin/env bash
# email one-time archive population — paiman `hooks.setup` step.
#
# Reads the owner's Mail.app Envelope Index (TCC-protected: needs Full Disk
# Access granted to the terminal) and hard-copies the entire mail history into
# the nested communication/email/ yaml tree. This is the step that makes the
# archive COMPLETE — the live email-in driver only ingests forward of its
# cursor, so without this the tree would start empty and only ever hold mail
# that arrives after the driver starts.
#
# Why this is a `setup` hook and NOT an `install` hook:
#   * It needs Full Disk Access, which is granted to the terminal. A headless
#     paifs-init / CI install has no FDA and would hit a TCC permission wall.
#   * On a large mailbox it can run longer than the 120s install-hook cap.
# paiman runs setup hooks only in a TTY (offered default-yes, skippable); on a
# headless install it prints "run later" and this never fires.
#
# Safe to run directly at any time — it is resumable (checkpoints the last
# ROWID to sys/drivers/email/backfill_progress.yaml) and idempotent (a
# completed run re-processes zero rows). If the 300s setup cap kills it midway,
# just run it again — it resumes from the checkpoint:
#   cd "$PAI_ROOT" && usr/bin/python -m drivers.email.macmail.backfill

set -euo pipefail

real_home() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import os
import pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
    return
  fi
  if command -v dscl >/dev/null 2>&1; then
    dscl . -read "/Users/$(id -un)" NFSHomeDirectory | awk '{print $2}'
    return
  fi
  echo "real_home: could not resolve OS account home" >&2
  return 1
}
REAL_HOME="$(real_home)" || exit 1
if [[ -z "$REAL_HOME" ]]; then
  echo "email: could not resolve OS account home." >&2
  exit 1
fi
PAI_ROOT="${PAI_ROOT:-$REAL_HOME/.pai}"
cd "$PAI_ROOT"

PY="usr/bin/python"
ENVELOPE="$REAL_HOME/Library/Mail/V10/MailData/Envelope Index"

# --- Full Disk Access gate -------------------------------------------------
# Without FDA the TCC layer makes the DB unreadable; bail cleanly (exit 0) so
# the install isn't marked failed — the owner re-runs after granting access.
if [ ! -r "$ENVELOPE" ]; then
  echo "email: cannot read Mail's Envelope Index (Full Disk Access needed)." >&2
  echo "        Grant your terminal Full Disk Access in System Settings →" >&2
  echo "        Privacy & Security → Full Disk Access, then run:" >&2
  echo "          cd \"$PAI_ROOT\" && usr/bin/python -m drivers.email.macmail.backfill" >&2
  exit 0
fi

# --- migrate first, only if an old FLAT tree is present --------------------
# The legacy email/macmail layout is <account>/YYYY-MM-DD/. If any flat day
# dir exists, convert it to nested YYYY/MM/DD (snapshots first) BEFORE the
# backfill, so already-captured bodies don't regress to header-only stubs.
if find var/spool/communication/email -maxdepth 2 -type d \
     -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' 2>/dev/null | grep -q .; then
  echo "email: flat archive detected — migrating to nested layout first…"
  "$PY" -m drivers.email.macmail.migrate
fi

# --- backfill the full history --------------------------------------------
# Resumable + idempotent; on completion bootstraps the live cursor to
# MAX(ROWID) so the first `email-in` start won't re-announce the archive.
echo "email: backfilling mail history into communication/email/ …"
"$PY" -m drivers.email.macmail.backfill
echo "email: backfill complete. Start the driver with: paictl start email-in email-out"
