---
name: drafting-emails
description: Draft Mail.app email by writing YAML files; triage inbound email and handle draft failures.
driver: email
---

# When to use this

Use this skill whenever the owner asks you to draft an email, reply to
an email, or prepare outbound mail. Use `bin/draft-email`; it writes a
draft YAML file under `~/drafts/`, and the `macmail-out` driver then
saves it into Mail.app's Drafts folder for the owner to review and send.

Do not paste the full email into chat as the final result unless the
owner explicitly asks to see text only. For normal "draft an email"
requests, create the YAML draft and briefly tell the owner where it was
saved.

# Your filesystem

Everything is under `~/communication/`.

```
~/communication/email/<account>/<date>/<thread-slug>.yaml   inbound messages
~/communication/email/<account>/meta.yaml                   account info
~/communication/email/drafts/                               drafts you write
```

`~/drafts/` is a shortcut to `~/communication/email/drafts/` — **one
shared dir, not per-account**. The `from:` field on the yaml picks
which Mail.app account owns the saved draft.

A message yaml looks like:

```yaml
message_id: <...@mail.example.com>
thread_slug: re-q3-budget-a9582e42
from: bob@example.com
from_name: Bob
to:
- owner@example.com
cc: []
subject: "Re: Q3 budget"
direction: inbound
received_at: '2026-05-10T18:19:52-07:00'
content: |
  Hey — can you confirm the Q3 numbers by Friday?
```

# Events you wake on

- **`email:new`** — read the yaml at `payload.path`, decide whether
  to stay silent, draft a reply, or surface to the owner. Event paths should
  be home-view paths (`communication/email/...`). If an older/stale runtime
  emits `var/spool/communication/email/...`, read it as
  `/var/spool/communication/email/...` or rewrite it to
  `communication/email/...` before treating it as missing.
- **`email:backlog`** — brief recap to the owner thread, grouped by
  account. Don't draft from backlog.
- **`email:draft_failed`** — read `draft_error` on the yaml at
  `payload.path`. Trivial fix → patch the yaml and clear
  `draft_state`/`draft_error`. Anything else → surface to the owner.

# Drafting

Your job is drafting only. Write draft YAMLs for Mail.app to save in
Drafts; the owner reviews and sends manually. Do not send email, click
Send, invoke AppleScript `send`, use SMTP/API sending paths, or treat
delivery as your responsibility.

Prefer the CLI:

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
outbound (not a reply), omit `in_reply_to` and `references`.

**`from:` discipline.** Use the canonical address of the account dir
the parent lives in (`~/communication/email/<account>/...`) — that
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

# Searching old mail

`mailsearch` queries Mail.app's full index for anything older than the
driver's ingest window. Results are materialized as yamls under
`~/communication/email/<account>/...`, ready to read or reply to.

```
mailsearch --from bob@example.com --limit 10
mailsearch --subject "Q3 budget" --since 2025-01-01
mailsearch --to owner@icloud.com --account owner@work.example --unread
mailsearch --flagged --since 2024-06-01
```

At least one of `--from`, `--to`, `--subject`, or `--since` is
required. Default limit 20, max 200. Re-running on the same hit is
idempotent.
