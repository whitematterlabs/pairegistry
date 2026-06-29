---
name: using-imessage
description: Read inbound iMessage threads, draft replies, handle send failures.
driver: imessage
---

# Your messages directory

Everything lives under `~/messages/`. One folder per thread (contact
slug or group slug); inside, one markdown file per day.

```
~/messages/<thread>/meta.yaml
~/messages/<thread>/2026-05-10.md
~/messages/<thread>/2026-05-11.md
```

A day-file is a flat log. Each line is `[HH:MM] <sender>: <text>`.

```
[09:14] tuba: pasaport icin geldin mi
[09:15] me: evet hallettim
[17:22] tuba: tamam
```

`me` = the owner (sent from their phone or by you).
`<contact-slug>` = inbound from them.

`meta.yaml` describes the thread:

```yaml
display_name: Tuba
channel: imessage
group: false
handles: ["+13105551234"]
```

# How to do things

**Reply to a thread.** Append a **bare line** — just the message
text, no `[HH:MM] me:` prefix — to today's day-file. The driver picks
up bare lines, sends via Messages.app, then writes the canonical
`[HH:MM] me: <text>` record itself. Bracketed lines are log entries
only and never get sent. Create today's file if it doesn't exist.

Whether you may send is decided by the owner's `imessage_send` capability,
stated in your `<capabilities>` block. The kernel enforces it via a freeze
file: when send isn't granted, `~/.pai/sys/drivers/imessage/outbound.freeze`
exists and the driver consumes attempted bare lines, appends a
`kernel: send frozen` note, emits `imessage:send_failed`, and does not call
Messages.app. If your `<capabilities>` block says iMessage is read-only,
don't attempt sends; never retry a frozen send.

```
echo "thurs after 6 works" >> ~/messages/<thread>/<today>.md
```

**Read a thread.** Read today's day-file. For deeper history, read
yesterday's, and so on. Don't grep all threads unless asked.

**New contact (thread doesn't exist yet).** Create
`~/messages/<slug>/meta.yaml` with `display_name`, `channel:
imessage`, `group: false`, and the handle. Then append to today's
day-file as normal. Slug = lowercase first name, or lowercase
first-last if needed for disambiguation.

# Events you wake on

- **`imessage:new`** — one inbound message. Read the day-file, then
  reply, surface, or stay silent (your role prompt decides).
- **`imessage:owner`** — the owner DM'd you through the TUI (`thread
  = "me"`). Treat it as a conversation with them.
- **`imessage:backlog`** — kernel just booted with unread messages.
  Write an offline report to the owner thread.
- **`imessage:multiple_messages`** — a live burst (>1 row collected
  during the short live quiet window). `context.messages` is the full
  ordered list with `thread`,
  `sender`, `text`, `day_file` per message. Day-files are already
  written; decide whether to reply, surface, or stay silent — same
  rules as `imessage:new`, just batched.
- **`imessage:send_failed`** — your outbound didn't deliver. Surface
  the thread, text, and reason to the owner thread. Don't retry.
