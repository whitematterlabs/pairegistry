#!/usr/bin/env bash
# mailv2 one-time archive population — paiman `hooks.setup` step.
#
# Reads the owner's Mail.app Envelope Index (TCC-protected: needs Full Disk
# Access granted to the terminal) and hard-copies the entire mail history into
# the nested communication/email/ yaml tree. This is the step that makes the
# archive COMPLETE — the live mailv2-in driver only ingests forward of its
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
# ROWID to sys/drivers/mailv2/backfill_progress.yaml) and idempotent (a
# completed run re-processes zero rows). If the 300s setup cap kills it midway,
# just run it again — it resumes from the checkpoint:
#   cd ~/.pai && usr/bin/python -m drivers.mailv2.macmail.backfill

set -euo pipefail

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
cd "$PAI_ROOT"

PY="usr/bin/python"
ENVELOPE="$HOME/Library/Mail/V10/MailData/Envelope Index"

# --- Full Disk Access gate -------------------------------------------------
# Without FDA the TCC layer makes the DB unreadable; bail cleanly (exit 0) so
# the install isn't marked failed — the owner re-runs after granting access.
if [ ! -r "$ENVELOPE" ]; then
  echo "mailv2: cannot read Mail's Envelope Index (Full Disk Access needed)." >&2
  echo "        Grant your terminal Full Disk Access in System Settings →" >&2
  echo "        Privacy & Security → Full Disk Access, then run:" >&2
  echo "          cd ~/.pai && usr/bin/python -m drivers.mailv2.macmail.backfill" >&2
  exit 0
fi

# --- migrate first, only if an old FLAT tree is present --------------------
# The legacy email/macmail layout is <account>/YYYY-MM-DD/. If any flat day
# dir exists, convert it to nested YYYY/MM/DD (snapshots first) BEFORE the
# backfill, so already-captured bodies don't regress to header-only stubs.
if find var/spool/communication/email -maxdepth 2 -type d \
     -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' 2>/dev/null | grep -q .; then
  echo "mailv2: flat archive detected — migrating to nested layout first…"
  "$PY" -m drivers.mailv2.macmail.migrate
fi

# --- backfill the full history --------------------------------------------
# Resumable + idempotent; on completion bootstraps the live cursor to
# MAX(ROWID) so the first `mailv2-in` start won't re-announce the archive.
echo "mailv2: backfilling mail history into communication/email/ …"
"$PY" -m drivers.mailv2.macmail.backfill
echo "mailv2: backfill complete. Start the driver with: paictl start mailv2-in mailv2-out"
