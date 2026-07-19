---
name: using-imessage
description: Read inbound iMessage threads, draft replies, handle send failures.
driver: imessage
---

# Your messages directory

Everything lives under the owner runtime messages directory:
`~/.pai/home/pai/messages/`. In shell commands from a PAI, use the
FHS path `/home/pai/messages/`; the shell rewrites that to the same
runtime directory without relying on `~`.

One folder per thread (contact slug or group slug); inside, one markdown
file per day.

```
/home/pai/messages/<thread>/meta.yaml
/home/pai/messages/<thread>/2026-05-10.md
/home/pai/messages/<thread>/2026-05-11.md
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

**Send a message.** Use `write-imessage`:

```
write-imessage --to <thread> --body "thurs after 6 works"
```

- `--to`: a thread slug, a `memory/people/` slug, a phone number, or
  an email address. For someone with no people entry, run `addcontact`
  first or pass their phone/email directly.
- One invocation = exactly one message, even multi-line. Pass real
  newlines (`--body-file`, stdin, or `$'...\n...'`). To send several
  messages, invoke several times.
- The printed `state` is the outcome: `sent`, `pending_approval`,
  `send_blocked`, or `failed` (with a `detail` reason). Trust the
  state — under `pending_approval`, tell the owner you sent it for
  approval, never that it was delivered.

Invoke it the same way regardless of your `imessage_send` capability
mode; the state tells you what happened. If your `<capabilities>` block
says iMessage is read-only, don't attempt sends; never retry a
`send_blocked`, `failed`, or owner-rejected send.

**Read a thread.** Read today's day-file. For deeper history, read
yesterday's, and so on. Don't grep all threads unless asked. In the
log, ` ↵ ` marks a line break inside a single message.

# Events you wake on

- **`imessage:new`** — one inbound message. Read the day-file, then
  reply, surface, or stay silent (your role prompt decides).
- **`imessage:owner`** — the owner DM'd you through the console (`thread
  = "me"`). Treat it as a conversation with them.
- **`imessage:backlog`** — kernel just booted with unread messages.
  Write an offline report to the owner thread.
- **`imessage:multiple_messages`** — a live burst (>1 row collected
  during the short live quiet window). `context.messages` is the full
  ordered list with `thread`,
  `sender`, `text`, `day_file` per message. Day-files are already
  written; decide whether to reply, surface, or stay silent — same
  rules as `imessage:new`, just batched.
- **`imessage:send_failed`** — your outbound didn't deliver, either because
  sends aren't granted or because the owner rejected a queued approval.
  Surface the thread, text, and reason to the owner thread. Don't retry.
