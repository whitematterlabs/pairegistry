---
name: drafting-emails
description: Draft Mail.app email by writing YAML files; triage inbound email, recap the backlog, and search the on-disk archive with rg.
driver: email
---

# When to use this

Use this skill whenever the owner asks you to draft an email, reply to
an email, prepare outbound mail, recap the inbox/backlog, or find old
mail. The `email` driver keeps a **complete on-disk archive** of every
message Mail.app has indexed, so you answer questions by globbing and
`rg`-ing files — never by hand-parsing a YAML dump in the shell.

For drafting, use `bin/draft-email`; it writes a draft YAML under
`~/drafts/`, and the `email-out` driver saves it into Mail.app's Drafts
folder for the owner to review and send.

Do not paste the full email into chat as the final result unless the
owner explicitly asks to see text only. For normal "draft an email"
requests, create the YAML draft and briefly tell the owner where it was
saved.

# Your filesystem

Everything is under `~/communication/`. The archive is partitioned by
date into a **nested `YYYY/MM/DD`** tree:

```
~/communication/email/<account>/<YYYY>/<MM>/<DD>/<thread-slug>.yaml   one message
~/communication/email/<account>/<YYYY>/<MM>/<DD>/<thread-slug>.prev   -> parent message
~/communication/email/<account>/threads/<thread-slug>/...            chronological index
~/communication/email/<account>/meta.yaml                            account info
~/communication/email/drafts/                                        drafts you write
```

`~/drafts/` is a shortcut to `~/communication/email/drafts/` — **one
shared dir, not per-account**. The `from:` field on the yaml picks which
Mail.app account owns the saved draft.

A message yaml looks like:

```yaml
message_id: <...@mail.example.com>
in_reply_to: <parent@example.com>
references:
- <root@example.com>
- <parent@example.com>
thread_slug: re-q3-budget-a9582e42
from: bob@example.com
from_name: Bob
to:
- owner@example.com
cc: []
bcc: []
subject: "Re: Q3 budget"
direction: inbound
content: |
  Hey — can you confirm the Q3 numbers by Friday?
body_state: present      # `absent` = header-only stub; Mail.app evicted the body
received_at: '2026-05-10T18:19:52-07:00'
```

**`body_state`** is the one field unique to email. `present` means the
full body is in `content`. `absent` means this is a header-only **stub**
written when Mail.app no longer had the `.emlx` body on disk — every
header is real and accurate, but `content` is empty. Treat a stub as a
genuine message you can see metadata for but can't quote the body of.

# Listing and searching — `inbox` + `rg`

**Reach for `inbox` first** for "what's in the inbox / backlog" questions.
It is count-first and bounded — it never floods you:

```sh
inbox                       # today, all accounts, counts + recent sample
inbox --since 7d            # last 7 days
inbox --since 2026-06-01
inbox --day 2026-06-25 --account icloud
inbox --direction inbound --limit 30
```

For detail or full-text search, `rg` the date globs directly. The tree
layout makes scoping trivial, and `rg` prints only matching lines (never
whole files):

```sh
# Everyone who wrote today, with subjects (one account or all):
rg --no-heading '^(from|subject):' communication/email/*/2026/06/25/

# Count messages on a day / in a month:
rg -c '^message_id:' communication/email/*/2026/06/25/ | awk -F: '{s+=$2} END{print s}'
rg -l '^message_id:' communication/email/*/2026/06/ | wc -l

# Find a sender across the whole archive:
rg -l '^from: .*bob@example.com' communication/email/

# Dedup / existence check for a specific Message-ID:
rg -l 'message_id: <abc@example.com>' communication/email/

# Header-only stubs only (no body available):
rg -l '^body_state: absent' communication/email/*/2026/06/
```

Keep output bounded: prefer `-c`/`-l` and a date scope over dumping a
whole month. There is **no `mailsearch` for email** — the archive is
complete, so `inbox` + `rg` answer everything; don't shell out to
`mailsearch`.

# Events you wake on

- **`email:new`** — read the yaml at `payload.path`, decide whether to
  stay silent, draft a reply, or surface to the owner. Event paths are
  home-view paths (`communication/email/...`). If an older/stale runtime
  emits `var/spool/communication/email/...`, read it as
  `/var/spool/communication/email/...` or rewrite it to
  `communication/email/...` before treating it as missing.
- **`email:backlog`** — the kernel booted and found mail it missed.
  Each account bucket carries `count`, `last_subject`, a capped
  `sample_subjects`, and the account's `since`. Give a brief recap to the
  owner thread grouped by account. For the **full** list run
  `inbox --since <event.since>` or `rg` the date dirs — don't try to put
  every subject in the event. Don't draft from backlog.
- **`email:draft_failed`** — read `draft_error` on the yaml at
  `payload.path`. Trivial fix → patch the yaml and clear
  `draft_state`/`draft_error`. Anything else → surface to the owner.

# Drafting and sending

You write draft YAMLs; the macmail-out driver hands them to Mail.app. By
default it `save`s them to Drafts for the owner to review and send. If a
draft sets `action: send`, the driver delivers it instead.

**Whether you may send is decided by the owner's `email_send` capability,
stated in your `<capabilities>` block — not by this skill.** Always use the
same `action: send` yaml regardless of mode:
- Send granted (`yes`) — set `action: send` at your discretion.
- Approval required (`ask`) — set `action: send` exactly as you would with
  send granted; the driver detects the gate and automatically queues it in
  the owner's approval tray instead of delivering (see the `approvals`
  skill). Tell the owner you sent it for approval, not that it was delivered.
- Not granted (`no`) — the driver ignores `action: send` and saves the
  message as a draft (recording `send_blocked`), so nothing leaves the
  machine.

Don't try to bypass this with your own AppleScript/SMTP — the driver is the
only outbound path.

Prefer the CLI (add `--send` to deliver, omit it to draft):

```sh
printf '%s\n' "Hi Alex,

I saw your listing and wanted to ask whether the room is still available." \
  | bin/draft-email \
      --from owner@example.com \
      --to alex@example.com \
      --subject "Interested in the room" \
      --wait
```

The command prints a small YAML result:

```yaml
path: drafts/interested-in-the-room-alex.yaml
spool_path: var/spool/communication/email/drafts/interested-in-the-room-alex.yaml
draft_state: drafted
```

For reply-shaped drafts, pass the parent's Message-ID:

```sh
printf '%s\n' "Thanks, Friday works for me." \
  | bin/draft-email \
      --from owner@example.com \
      --subject "Re: Q3 budget" \
      --in-reply-to "<message-id-of-parent>" \
      --reference "<root@example.com>" \
      --reference "<message-id-of-parent>" \
      --wait
```

If `bin/draft-email` is unavailable, write the draft manually to
`~/drafts/<name>.yaml`. Pick a descriptive `<name>` like
`re-bob-q3-budget` — it's just a filename, not exposed anywhere.

```yaml
from: owner@example.com               # must match a Mail.app account
to: [bob@example.com]
cc: []
bcc: []
subject: "Re: Q3 budget"
in_reply_to: <message-id-of-parent>   # required for replies
references:                           # parent's references + parent's message_id
  - <root@example.com>
  - <message-id-of-parent>
content: |
  Plain text body. Multi-paragraph is fine.

  Don't add a signature — Mail.app appends the owner's automatically.
```

For a brand-new outbound email, omit `in_reply_to` and `references`:

```yaml
from: owner@example.com
to: [alex@example.com]
cc: []
bcc: []
subject: "Interested in the room"
content: |
  Hi Alex,

  I saw your listing and wanted to ask whether the room is still
  available.
```

**Threading.** Copy parent's `message_id` → your `in_reply_to`. Copy
parent's `references` and append parent's `message_id` → your
`references`. Subject: prepend `Re: ` if not already there. For brand-new
outbound (not a reply), omit `in_reply_to` and `references`. A stub
(`body_state: absent`) still has accurate `message_id`/`references`, so
you can thread a reply off it even without the body.

**`from:` discipline.** Use the canonical address of the account dir the
parent lives in (`~/communication/email/<account>/...`) — that
`<account>` is your `from:`. Never read the parent's `to:` header; it
often contains a Hide-My-Email relay or forwarder that Mail.app rejects
as a sender. The driver validates `from:` at boot and rejects unknowns
with a clean `draft_error`.

**YAML quoting.** Quote any string containing `: `, `#`, leading `-`,
or starting with `[`/`{`. `subject: Re: Foo` unquoted parses as a
nested mapping and silently breaks the draft.

**Lifecycle (read-only — driver sets these, you don't).**

- `draft_state: drafted` + `drafted_at` — Mail.app accepted it. Done.
- `draft_state: pending_parent` + `draft_retries: N` — reply parent not
  synced yet; driver retries with backoff. Wait.
- `draft_state: failed` + `draft_error` — terminal; `email:draft_failed`
  fires.
