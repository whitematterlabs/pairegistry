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

- `--to` takes a thread slug, a `memory/people/` slug (`handles:` phone
  number in about.yaml), or a raw phone number. New contacts are set up
  automatically; for someone with no people entry, run `addcontact`
  first or pass their phone number directly. WhatsApp recipients are
  phone-based — emails don't work. (Known limit: contacts reachable
  only via a WhatsApp `@lid` — not a phone number — can't be addressed
  yet.)
- One invocation = exactly one message. Multi-line bodies stay one
  message — pass real newlines (`--body-file`, stdin, or `$'...\n...'`)
  and the tool handles the rest. To send several messages, invoke
  several times.
- It waits (default 15s) and prints the outcome: `state: sent`,
  `pending_approval`, `send_blocked`, or `failed` (with a `detail`
  reason). Trust the state — under `pending_approval`, tell the owner
  you sent it for approval, never that it was delivered.

Whether you may send is decided by the owner's `whatsapp_send` capability,
stated in your `<capabilities>` block. Invoke `write-whatsapp` the same
way regardless of mode:
- Send granted (`yes`) — delivered via the WhatsApp socket.
- Approval required (`ask`) — the driver queues your message in the
  owner's approval tray instead of sending (see the `approvals` skill).
- Not granted (`no`, the default) — the send is frozen and nothing is
  delivered (`state: send_blocked`, plus a `whatsapp-out:send_failed`
  event). The spool defaults to DENY: with no grant, nothing you write
  is ever sent.

If your `<capabilities>` block says WhatsApp is read-only, don't attempt
sends; never retry a frozen or rejected send.

**Read a thread.** For a live `whatsapp:new` event, read the event's
`day_file`. Without an event path, read today's day-file first, then
yesterday's, and so on. Don't grep all threads unless asked.

# Under the hood (the day-file protocol)

`write-whatsapp` just writes the protocol for you: a **bare line**
(no `[HH:MM] sender:` prefix) appended to today's day-file is a send
request; the driver sends it and writes back the canonical
`[HH:MM] me: <text>` record. Bracketed lines are log entries only and
never get sent. One bare line = exactly one message; the ` ↵ ` marker
(space, ↵, space) is a line break *inside* one outbound message,
expanded to a real newline at send time. (Asymmetry to know about:
*inbound* multi-line texts appear as several `[HH:MM] Sender:` lines
with the same prefix, not as one ↵-marked line.)

You'll see driver verdicts in the day-file as `[HH:MM] kernel: ...`
notes (`queued for owner approval`, `send frozen`, `send failed`).

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
