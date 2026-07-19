---
name: using-whatsapp
description: Read inbound WhatsApp threads, draft replies, handle send failures.
driver: whatsapp
---

# Your messages directory

Everything lives under `~/whatsapp-messages/`. One folder per thread
(contact slug, group slug, or raw phone number for unknown contacts);
inside, one markdown file per day.

```
~/whatsapp-messages/<thread>/meta.yaml
~/whatsapp-messages/<thread>/2026-05-10.md
~/whatsapp-messages/<thread>/2026-05-11.md
```

A day-file is a flat log. Each line is `[HH:MM] <sender>: <text>`.

```
[13:28] Alper Amerika: yok bakmadim daha iyi mi?
[13:30] me: bakacam birazdan
```

`me` = the owner (sent from their phone or by you).
`<DisplayName>` = inbound from them. For unknown contacts the slug
is the phone number; the display name will be the number too.

`meta.yaml` is minimal — `channel: whatsapp`. The driver maintains it and,
on your first send to a thread, writes the resolved `handles` (the
recipient's phone number) back into it.

# How to do things

**Send a message.** Use `write-whatsapp`:

```
write-whatsapp --to <thread> --body "bakacam birazdan"
```

- `--to`: a thread slug, a `memory/people/` slug (`handles:` phone
  number in about.yaml), or a raw phone number. For someone with no
  people entry, run `addcontact` first or pass their phone number
  directly. WhatsApp recipients are phone-based — emails don't work.
  (Known limit: contacts reachable only via a WhatsApp `@lid` — not a
  phone number — can't be addressed yet.)
- One invocation = exactly one message, even multi-line. Pass real
  newlines (`--body-file`, stdin, or `$'...\n...'`). To send several
  messages, invoke several times.
- The printed `state` is the outcome: `sent`, `pending_approval`,
  `send_blocked`, or `failed` (with a `detail` reason). Trust the
  state — under `pending_approval`, tell the owner you sent it for
  approval, never that it was delivered.

Invoke it the same way regardless of your `whatsapp_send` capability
mode; the state tells you what happened. If your `<capabilities>` block
says WhatsApp is read-only, don't attempt sends; never retry a
`send_blocked`, `failed`, or owner-rejected send.

**Read a thread.** For a live `whatsapp:new` event, read the event's
`day_file`. Without an event path, read today's day-file first, then
yesterday's, and so on. Don't grep all threads unless asked. In `me:`
lines, ` ↵ ` marks a line break inside a single message.

# Events you wake on

- **`whatsapp:new`** — one inbound message. Payload has `thread`,
  `sender`, `text`, `day_file`. Read the day-file, then reply, surface,
  or stay silent (your role prompt decides).
- **`whatsapp:backlog`** — kernel just booted with unread messages.
  Payload has `threads` with per-thread counts, `last_text`, and
  `day_files`. Backlog messages keep their original WhatsApp timestamps,
  so today's sync may write older files like `2026-05-24.md` instead of
  today's file. Read the listed `day_files`, then output a short recap
  as your turn output — one bullet per thread that matters. Skip noise.
- **`whatsapp-out:send_failed`** — your outbound didn't deliver, either
  because sends aren't granted, the recipient couldn't be resolved, the
  bridge was down, or the owner rejected a queued approval. Surface the
  thread, text, and reason to the owner. Don't retry.
