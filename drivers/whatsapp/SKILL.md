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

**Reply to a thread.** Append a **bare line** — just the message
text, no `[HH:MM] me:` prefix — to today's day-file. The driver picks
up bare lines, sends via the WhatsApp socket, then writes the canonical
`[HH:MM] me: <text>` record itself. Bracketed lines are log entries
only and never get sent. Create today's file if it doesn't exist.

```
echo "bakacam birazdan" >> /home/pai/whatsapp-messages/<thread>/<today>.md
```

Whether you may send is decided by the owner's `whatsapp_send` capability,
stated in your `<capabilities>` block. Always append the same bare line
regardless of mode:
- Send granted (`yes`) — delivered via the WhatsApp socket as described above.
- Approval required (`ask`) — the driver detects the gate and automatically
  queues your message in the owner's approval tray instead of sending (see
  the `approvals` skill); it appends a `kernel: queued for owner approval`
  note. Tell the owner you sent it for approval, not that it was delivered.
- Not granted (`no`, the default) — the driver consumes the line, appends a
  `kernel: send frozen` note, emits `whatsapp-out:send_failed`, and does not
  reach WhatsApp. The spool defaults to DENY: with no grant, nothing you
  write is ever sent.

If your `<capabilities>` block says WhatsApp is read-only, don't attempt
sends; never retry a frozen or rejected send.

**New contact (thread doesn't exist yet).** Create
`/home/pai/whatsapp-messages/<slug>/` and append to today's day-file. The
driver resolves the recipient from `memory/people/<slug>/about.yaml`
(`handles:` phone number) or, if the slug is a raw phone number, from the
slug itself. If it can't resolve a phone number it appends a
`kernel: send failed` note. (Known limit: contacts reachable only via a
WhatsApp `@lid` — not a phone number — can't be addressed yet.)

**Read a thread.** For a live `whatsapp:new` event, read the event's
`day_file`. Without an event path, read today's day-file first, then
yesterday's, and so on. Don't grep all threads unless asked.

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
