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

- `--to` takes a thread slug, a `memory/people/` slug, a raw phone
  number, or an email address. New contacts are set up automatically
  (thread dir + meta.yaml from the person's `about.yaml` handles);
  for someone with no people entry, run `addcontact` first or pass
  their phone/email directly.
- One invocation = exactly one message. Multi-line bodies stay one
  message — pass real newlines (`--body-file`, stdin, or `$'...\n...'`)
  and the tool handles the rest. To send several messages, invoke
  several times.
- It waits (default 15s) and prints the outcome: `state: sent`,
  `pending_approval`, `send_blocked`, or `failed` (with a `detail`
  reason). Trust the state — under `pending_approval`, tell the owner
  you sent it for approval, never that it was delivered.

Whether you may send is decided by the owner's `imessage_send` capability,
stated in your `<capabilities>` block. Invoke `write-imessage` the same
way regardless of mode:
- Send granted (`yes`) — delivered via Messages.app.
- Approval required (`ask`) — the driver queues your message in the
  owner's approval tray instead of sending (see the `approvals` skill).
- Not granted (`no`) — the send is frozen and nothing is delivered
  (`state: send_blocked`, plus an `imessage:send_failed` event).

If your `<capabilities>` block says iMessage is read-only, don't attempt
sends; never retry a frozen or rejected send.

**Read a thread.** Read today's day-file. For deeper history, read
yesterday's, and so on. Don't grep all threads unless asked.

# Under the hood (the day-file protocol)

`write-imessage` just writes the protocol for you: a **bare line**
(no `[HH:MM] sender:` prefix) appended to today's day-file is a send
request; the driver sends it and writes back the canonical
`[HH:MM] me: <text>` record. Bracketed lines are log entries only and
never get sent. One bare line = exactly one message; the ` ↵ ` marker
(space, ↵, space) is a line break *inside* one message, expanded to a
real newline at send time. Inbound multi-line texts are flattened the
same way, so the log round-trips: `Here's the plan: ↵ 1. rent ↵ 2. profit`
arrives as three lines in one message.

You'll see driver verdicts in the day-file as `[HH:MM] kernel: ...`
notes (`queued for owner approval`, `send frozen`, `send failed`).

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
- **`imessage:send_failed`** — your outbound didn't deliver, either because
  sends aren't granted or because the owner rejected a queued approval.
  Surface the thread, text, and reason to the owner thread. Don't retry.
