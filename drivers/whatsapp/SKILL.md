---
name: using-whatsapp
description: Read inbound WhatsApp threads. Receive-only — there is no way to send.
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

`meta.yaml` is minimal — `channel: whatsapp`. The driver maintains it.

# How to do things

This driver is **receive-only**. There is no send path: writing to a
day-file does nothing, and nothing you do here reaches WhatsApp. If
the owner needs a reply sent, surface it to them so they can send it
from their phone.

**Read a thread.** For a live `whatsapp:new` event, read the event's
`day_file`. Without an event path, read today's day-file first, then
yesterday's, and so on. Don't grep all threads unless asked.

# Events you wake on

- **`whatsapp:new`** — one inbound message. Payload has `thread`,
  `sender`, `text`, `day_file`. Read the day-file, then surface
  anything the owner needs to see. You can't reply.
- **`whatsapp:backlog`** — kernel just booted with unread messages.
  Payload has `threads` with per-thread counts, `last_text`, and
  `day_files`. Backlog messages keep their original WhatsApp timestamps,
  so today's sync may write older files like `2026-05-24.md` instead of
  today's file. Read the listed `day_files`, then output a short recap
  as your turn output — one bullet per thread that matters. Skip noise.
